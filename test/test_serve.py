#!/usr/bin/env python3
"""End-to-end tests for `serve` (../serve) against real caddy and cloudflared
binaries, driven through an interactive zsh on a real pseudo-terminal.

Every scenario checks two things after it ends, regardless of what else it
asserts: no caddy/cloudflared/stub process is left running (no orphans), and
an unrelated "decoy" process started in the same shell is still alive (no
process outside serve's own process group was ever signalled).

cloudflared is replaced by a sleeping stub in every scenario by default,
because Cloudflare rate-limits how often one IP can provision quick tunnels
and a routine test run would otherwise start hitting 429s after a handful of
runs. Pass --real-tunnel to use the real binary wherever a scenario runs
with a working cloudflared: that checks the tunnel itself (URL, --url target)
and, in stop_during_download, a request the real cloudflared is still
proxying when serve stops. The stub cannot stand in for that last part: the
real binary reacts to a repeated signal differently from a plain process,
which is exactly what serve's cleanup has to get right.

Requires: macOS, python3, a real `caddy` binary on PATH. `cloudflared` is only
required on PATH when --real-tunnel is passed.
"""
import argparse
import functools
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.path.join(ROOT, 'serve')
ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
TUNNEL_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
HOST = subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True).stdout.decode().strip()
# What the Local: line must name: the Bonjour name, or localhost (with a note
# saying why) on a machine that has no LocalHostName set
EXPECTED_HOST = f'{HOST}.local' if HOST else 'localhost'
LOCAL_URL_RE = re.compile(rf'Local: http://({re.escape(EXPECTED_HOST)}):(\d+)/')

REAL_TUNNEL = False  # set from argv in main()


# --------------------------------------------------------------------- pty shell

class Shell:
    """An interactive `zsh -f -i` on its own pseudo-terminal.

    A background thread drains the pty continuously so output can never sit
    in the kernel tty queue, where a Ctrl-C would flush (discard) it before
    the test gets to read it.
    """

    def __init__(self):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ, TERM='xterm-256color')
            os.execvpe('zsh', ['zsh', '-f', '-i'], env)
        os.set_inheritable(self.fd, False)  # never leak into a later pty.fork()
        SHELL_PIDS.append(self.pid)
        self._buf = b''
        self._lock = threading.Lock()
        self._closed = False
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        self.wait(r'\$ |% ', 5)

    def _drain(self):
        while not self._closed:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.05)
                if r:
                    chunk = os.read(self.fd, 65536)
                    if not chunk:
                        return
                    with self._lock:
                        self._buf += chunk
            except (OSError, ValueError):
                return

    def text(self):
        with self._lock:
            return ANSI.sub('', self._buf.decode(errors='replace'))

    def raw_bytes(self):
        with self._lock:
            return bytes(self._buf)

    def wait(self, pattern, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if re.search(pattern, self.text()):
                return True
            time.sleep(0.02)
        return False

    def send(self, data, settle=0.3):
        try:
            os.write(self.fd, data.encode() if isinstance(data, str) else data)
        except OSError:
            return
        time.sleep(settle)

    def cmd(self, line, settle=0.3):
        self.send(line + '\n', settle)

    def close_pty(self):
        if not self._closed:
            self._closed = True
            self._reader.join(0.5)
            try:
                os.close(self.fd)
            except OSError:
                pass

    def kill(self):
        if self.pid in SHELL_PIDS:
            SHELL_PIDS.remove(self.pid)   # its pid may be recycled after this
        try:
            os.kill(self.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass

    def finish(self):
        if not self._closed:
            self.send('exit\n')
        self.kill()
        self.close_pty()


# --------------------------------------------------------------------- helpers

SHELL_PIDS = []       # pty shells this suite created, while they are alive
KNOWN_OURS = set()    # every pid ever observed as a descendant of one of them
KNOWN_GROUPS = set()  # process groups ever observed with one of ours as leader


def _descendants(roots):
    seen, frontier = set(), [str(r) for r in roots]
    while frontier:
        out = subprocess.run(['pgrep', '-P', ','.join(frontier)],
                             capture_output=True).stdout.decode().split()
        new = [p for p in out if p not in seen]
        seen.update(new)
        frontier = new
    return seen


def refresh_ours():
    """Record everything currently descending from our shells.

    Sampled continuously while a scenario runs, because a process has to be
    claimed BEFORE its shell is killed: once the shell dies its children are
    reparented and the tree no longer leads back to us.
    """
    SHELL_PIDS[:] = [p for p in SHELL_PIDS if alive(p)]
    KNOWN_OURS.update(_descendants(SHELL_PIDS))
    # A process can also escape before the walk above ever sees it: serve
    # dying right after launching cloudflared, say, which is exactly the leak
    # these tests exist to catch. Reparented to launchd, it no longer leads
    # back to us, but it keeps its process group. Claiming by group too keeps
    # such a leak visible to no_orphans() and reapable, instead of passing
    # every "no leftover processes" check unseen and sleeping on for hours.
    pairs = [l.split() for l in subprocess.run(['ps', '-A', '-o', 'pid=,pgid='],
                                              capture_output=True).stdout.decode().splitlines()]
    KNOWN_GROUPS.update(g for p, g in pairs if p == g and p in KNOWN_OURS)
    KNOWN_OURS.update(p for p, g in pairs if g in KNOWN_GROUPS)


def our_pids(name=None, pattern=None):
    """Matching processes THIS SUITE started -- never anything else.

    The machine may well be running the user's own serve, or another caddy
    entirely. Treating those as leaked test processes would report phantom
    failures, and killing them would be exactly the mistake serve itself
    is built to never make.
    """
    found = pids_exact(name) if name else pids_matching(pattern)
    return [p for p in found if p in KNOWN_OURS]


def pids_exact(name):
    return subprocess.run(['pgrep', '-x', name], capture_output=True).stdout.decode().split()


def pids_matching(pattern):
    return subprocess.run(['pgrep', '-f', pattern], capture_output=True).stdout.decode().split()


def alive(pid):
    if not pid:
        return None
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False


def http_get(url, timeout=5):
    import urllib.request
    return urllib.request.urlopen(url, timeout=timeout).read().decode()


def wait_until(predicate, timeout=8, interval=0.05):
    """Poll until `predicate` is true. Stub binaries are shell scripts that
    exec into their real process, so a scenario that inspects or signals one
    the instant serve launches it can catch /bin/sh instead -- which behaves
    nothing like the process the scenario means to test."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# pgrep -f on macOS matches a POSIX extended regex against the full command
# line, and has no \d shorthand. Every stub below ends up as `/bin/sleep 7200`
# (however it got there), so one pattern finds any of them; decoys sleep for a
# distinct 71xx tag so the two can never be confused.
STUB_PATTERN = r'sleep 7200$'
DECOY_PATTERN = r'sleep 71[0-9][0-9]$'


def cf_pids():
    """Our cloudflared process, whether it is the real binary or a stub."""
    return our_pids(name='cloudflared') or our_pids(pattern=STUB_PATTERN)


def no_orphans():
    """Every caddy, cloudflared or stub process of ours still running. Covers
    all stub forms, so a leak cannot hide behind the shape of one stub."""
    return our_pids(name='caddy') + our_pids(name='cloudflared') + our_pids(pattern=STUB_PATTERN)


def reap(pids):
    """Force-kill harness-owned leftovers. SIGKILL, because one stub exists
    precisely to ignore everything else."""
    for p in pids:
        try:
            os.kill(int(p), signal.SIGKILL)
        except (ValueError, ProcessLookupError):
            pass


# Every command serve --share shells out to; its pre-flight check must cover
# all of them (without --share, cloudflared is not needed)
REQUIRED_COMMANDS = ('caddy', 'cloudflared', 'scutil', 'lsof')


class Workspace:
    """Scratch area for one test run: real site directories, stub binaries."""

    # Every long-running stub execs /bin/sleep by absolute path: scenarios that
    # replace PATH wholesale leave /bin/sh nothing to look sleep up in. Each
    # is a shell script until the exec completes (see wait_until).
    STUB_SLEEP = '#!/bin/sh\nexec /bin/sleep 7200\n'
    # Reports how it was invoked, so the tunnel URL can be checked without a tunnel
    STUB_CF_ANNOUNCES = '#!/bin/sh\necho "CF_INVOKED_WITH: $*" >&2\nexec /bin/sleep 7200\n'
    # Ignored dispositions survive exec, so this is a plain sleep that shrugs
    # off everything short of SIGKILL
    STUB_IGNORES_SIGNALS = '#!/bin/sh\ntrap "" TERM HUP INT QUIT\nexec /bin/sleep 7200\n'
    STUB_EXIT_1 = '#!/bin/sh\nexit 1\n'
    # Keeps logging after its first line, like the real cloudflared does
    STUB_CF_CHATTY = ('#!/bin/sh\necho "CF_INVOKED_WITH: $*" >&2\n'
                      '/bin/sleep 1\necho "still logging" >&2\nexec /bin/sleep 7200\n')

    def __init__(self):
        self.root = tempfile.mkdtemp(prefix='na_serve_test_')
        self._sites = {}
        self._stub_dirs = {}

    def site(self, letter):
        """A directory containing one file, unique to `letter`."""
        if letter not in self._sites:
            d = os.path.join(self.root, f'site{letter}')
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'who.txt'), 'w') as f:
                f.write(f'content of site {letter}')
            self._sites[letter] = d
        return self._sites[letter]

    def stub_dir(self, name, files):
        """A PATH-prefix directory built once per (name, files) pair.

        `files` maps a command name to its script body, or to None to mean
        "symlink the real binary" (used so a stub directory can override just
        one of caddy/cloudflared while still finding the other on PATH).
        """
        key = name
        if key in self._stub_dirs:
            return self._stub_dirs[key]
        d = os.path.join(self.root, f'stub_{name}')
        os.makedirs(d, exist_ok=True)
        for cmd, body in files.items():
            path = os.path.join(d, cmd)
            if body is None:
                real = shutil.which(cmd)
                os.symlink(real, path)
            else:
                with open(path, 'w') as f:
                    f.write(body)
                os.chmod(path, 0o755)
        self._stub_dirs[key] = d
        return d

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


WS = None  # set in main()


def cf_path(behavior='sleep'):
    """PATH prefix with a real caddy and a substitute cloudflared.

    `behavior`: 'sleep' (default: echoes its arguments, then sleeps),
    'ignore-signals', or 'missing' (caddy only, no cloudflared at all).
    """
    if behavior == 'missing':
        return WS.stub_dir('caddy-only', {'caddy': None})
    body = WS.STUB_IGNORES_SIGNALS if behavior == 'ignore-signals' else WS.STUB_CF_ANNOUNCES
    return WS.stub_dir(f'cf-{behavior}', {'caddy': None, 'cloudflared': body})


def start_serve(sh, site_letter, path_prefix='real-or-stub', decoy_tag=7101, path_mode='prefix',
                args='--share'):
    """cd into a per-scenario site dir, start a decoy background job (a sleep
    whose 71xx duration tags it as a decoy), then invoke serve with `args`.
    Returns the decoy's PID.

    `args` defaults to --share: most scenarios are about running caddy and
    cloudflared side by side. Pass '' for serve's default, LAN-only mode.

    `path_mode` decides how the stub directory relates to the real PATH:
      'prefix'  prepend it, so a stub shadows the real binary of that name
      'system'  the stub directory plus the standard system directories, which
                HIDES a binary installed elsewhere (e.g. cloudflared under
                /opt/homebrew/bin) while ordinary tools stay reachable
      'only'    the stub directory and nothing else, the only way to hide a
                tool that lives in a system directory (lsof, scutil...)
    """
    if path_prefix == 'real-or-stub':
        path_prefix = None if REAL_TUNNEL else cf_path('sleep')
    if path_prefix:
        if path_mode == 'only':
            sh.cmd(f'export PATH="{path_prefix}"')
        elif path_mode == 'system':
            sh.cmd(f'export PATH="{path_prefix}:/usr/bin:/bin:/usr/sbin:/sbin"')
        else:
            sh.cmd(f'export PATH="{path_prefix}:$PATH"')
    sh.cmd(f'cd "{WS.site(site_letter)}"')
    sh.cmd(f'sleep {decoy_tag} &')
    m = re.search(r'\[\d+\] (\d+)', sh.text()[-200:])
    decoy = m.group(1) if m else None
    sh.cmd(' '.join(filter(None, [f'"{SERVE}"', args])) + '; print EXIT=$?', settle=0.05)
    return decoy


def exit_code(sh):
    m = re.findall(r'EXIT=(\d+)', sh.text())
    return int(m[-1]) if m else None


def serve_output(sh):
    """Only what serve itself produced: the transcript after the echo of the
    invocation line, which ends in the literal `EXIT=$?`."""
    return sh.text().rsplit('EXIT=$?', 1)[-1]


def local_port(sh):
    m = LOCAL_URL_RE.search(sh.text())
    return int(m.group(2)) if m else None


def tunnel_url(sh):
    m = TUNNEL_URL_RE.search(sh.text())
    return m.group(0) if m else None


def get(letter, port, host='localhost', base=None):
    try:
        url = f'{base or f"http://{host}:{port}"}/who.txt'
        return http_get(url).strip() == f'content of site {letter}'
    except Exception as e:
        return f'ERR {e}'


# --------------------------------------------------------------------- scenarios

RESULTS = []


def record(name, ok, detail=''):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if detail and not ok:
        if isinstance(detail, dict):
            for k, v in detail.items():
                print(f'       {k}: {v}')
        else:
            print(f'       {detail}')


def scenario(fn):
    """Run one scenario, then sweep two kinds of process this test harness
    itself is responsible for reaping (neither is serve's job to clean up):
    leftover caddy/cloudflared/stub processes if the scenario's own
    assertions failed partway through, and every decoy background job the
    scenario started. A decoy must outlive the scenario body for its "still
    alive" assertion to mean anything, and it is deliberately never a child
    of serve's process group -- so nothing inside serve ever terminates it.
    Left alone it would just keep sleeping for its full duration (up to two
    hours) on whatever machine runs this suite.
    """
    @functools.wraps(fn)
    def wrapped():
        sampling = True

        def sample():
            while sampling:
                refresh_ours()
                time.sleep(0.1)

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        try:
            fn()
        except Exception as e:
            record(fn.__name__, False, repr(e))
        finally:
            refresh_ours()
            sampling = False
            sampler.join(0.5)
            reap(no_orphans() + our_pids(pattern=DECOY_PATTERN))
            time.sleep(0.3)
    return wrapped


@scenario
def single_instance():
    sh = Shell()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    if REAL_TUNNEL:
        sh.wait(TUNNEL_URL_RE.pattern, 40)
    else:
        sh.wait('CF_INVOKED_WITH: ', 5)
    t = sh.text()
    port = local_port(sh)
    checks = {
        'red serving line names the right directory': f'Serving: {WS.site("A")}' in t,
        'red serving line is actually bold red': b'\x1b[1;31mServing: ' in sh.raw_bytes(),
        'local url names this machine': bool(LOCAL_URL_RE.search(t))
                                         and (bool(HOST) or '(LocalHostName is not set' in t),
        'port in ephemeral range': bool(port) and 49152 <= port <= 65535,
    }
    time.sleep(0.3)
    checks['serves over 127.0.0.1'] = get('A', port, '127.0.0.1') is True
    checks['serves over ::1'] = get('A', port, '[::1]') is True
    checks['serves over the local url host'] = get('A', port, EXPECTED_HOST) is True
    try:
        checks['directory listing shows the file'] = 'who.txt' in http_get(f'http://localhost:{port}/')
    except Exception as e:
        checks['directory listing shows the file'] = f'ERR {e}'
    if REAL_TUNNEL:
        cf_args = subprocess.run(
            ['ps', '-o', 'args=', '-p', ','.join(our_pids(name='cloudflared') or ['0'])],
            capture_output=True).stdout.decode()
        checks['tunnel URL printed'] = bool(tunnel_url(sh))
    else:
        cf_args = t   # the stub echoes its arguments instead
    checks['tunnel targets localhost, not 127.0.0.1'] = f'tunnel --url http://localhost:{port}' in cf_args
    wait_until(lambda: our_pids(name='caddy') and cf_pids())
    pgids = subprocess.run(
        ['ps', '-o', 'pgid=', '-p', ','.join(our_pids(name='caddy') + cf_pids())],
        capture_output=True).stdout.decode().split()
    checks['caddy and cloudflared share one process group'] = len(set(pgids)) == 1 and len(pgids) == 2
    sh.send('\x03')
    sh.wait(r'EXIT=\d+', 15)
    checks['clean exit on Ctrl-C'] = exit_code(sh) == 0
    sh.finish()
    time.sleep(0.5)
    checks['no leftover processes'] = not no_orphans()
    checks['decoy process untouched'] = alive(decoy) is True
    failed = {k: v for k, v in checks.items() if v is not True}
    record('single instance: full business + lifecycle check', not failed, failed)


@scenario
def concurrent_instances():
    shells, decoys, ports = {}, {}, {}
    for i, letter in enumerate('ABC'):
        shells[letter] = Shell()
        decoys[letter] = start_serve(shells[letter], letter, decoy_tag=7110 + i)
    for sh in shells.values():
        sh.wait('Local: http://', 10)
    time.sleep(0.5)
    for letter, sh in shells.items():
        ports[letter] = local_port(sh)
    checks = {'three distinct ports': len(set(ports.values())) == 3 and None not in ports.values()}
    checks['each instance serves its own directory'] = all(get(l, p) is True for l, p in ports.items())

    # stop the middle one first; the other two must be unaffected
    shells['B'].send('\x03')
    shells['B'].wait(r'EXIT=\d+', 15)
    time.sleep(0.5)
    checks['B stopped cleanly'] = exit_code(shells['B']) == 0
    checks['A and C still serving after B stops'] = get('A', ports['A']) is True and get('C', ports['C']) is True
    checks['B no longer listening'] = get('B', ports['B']) is not True

    shells['A'].send('\x03')
    shells['A'].wait(r'EXIT=\d+', 15)
    checks['C still serving after A stops'] = get('C', ports['C']) is True
    shells['C'].send('\x03')
    shells['C'].wait(r'EXIT=\d+', 15)
    checks['C stopped cleanly'] = exit_code(shells['C']) == 0

    for sh in shells.values():
        sh.finish()
    time.sleep(0.5)
    checks['no leftover processes'] = not no_orphans()
    checks['all decoys untouched'] = all(alive(p) is True for p in decoys.values())
    failed = {k: v for k, v in checks.items() if v is not True}
    record('three concurrent instances, stopped in mixed order', not failed, failed)


@scenario
def triple_ctrl_c():
    sh = Shell()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    time.sleep(1.0)
    sh.send('\x03\x03\x03')
    sh.wait(r'EXIT=\d+', 15)
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    record('triple Ctrl-C still exits cleanly once',
           ec == 0 and not no_orphans() and alive(decoy) is True,
           {'exit': ec})


@scenario
def ctrl_backslash():
    # Ctrl-\ sends SIGQUIT to the foreground group. Untrapped, it ended serve
    # before any cleanup ran; caddy happens to quit on SIGQUIT by itself, but
    # anything that ignores it (the stub does, and background jobs inherit
    # SIGQUIT ignored) was left behind.
    sh = Shell()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    wait_until(lambda: cf_pids())
    sh.send('\x1c')
    sh.wait(r'EXIT=\d+', 15)
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    record('Ctrl-\\ (SIGQUIT) is handled like Ctrl-C',
           ec == 0 and not no_orphans() and alive(decoy) is True, {'exit': ec})


@scenario
def ctrl_c_during_startup():
    # Send Ctrl-C as early as possible, racing caddy's own startup. Before a
    # fix, a signal landing in this narrow window was misreported as "caddy
    # failed to start" (exit 1) even though caddy was shutting down cleanly
    # because it received the same terminal SIGINT directly. Since the exact
    # timing of the race isn't controllable from here, this only asserts the
    # invariant that must hold regardless of which side of the race wins:
    # never a false "failed to start", never a leftover process.
    sh = Shell()
    decoy = start_serve(sh, 'A')
    wait_until(lambda: our_pids(name='caddy'))   # serve is under way; race its port read
    sh.send('\x03', settle=0.0)
    sh.wait(r'EXIT=\d+', 15)
    t = serve_output(sh)
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    ok = (ec == 0 and 'serve: caddy failed to start' not in t
          and not no_orphans() and alive(decoy) is True)
    record('Ctrl-C racing the startup window never misreports "caddy failed to start"',
           ok, {'exit': ec, 'output_tail': t[-200:]})


def _service_crashes(which, args='--share'):
    sh = Shell()
    decoy = start_serve(sh, 'A', args=args)
    sh.wait('Local: http://', 10)
    time.sleep(1.5)
    # only ever the process this scenario started: `which` names a program that
    # the person running these tests may well have running for real
    victims = our_pids(name='caddy') if which == 'caddy' else cf_pids()
    for p in victims:
        os.kill(int(p), signal.SIGKILL)
    sh.wait(r'EXIT=\d+', 15)
    ec = exit_code(sh)
    msg = f'serve: {which} exited' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and not no_orphans() and alive(decoy) is True
    if args:
        record(f'{which} crashing brings the other one down too', ok, {'exit': ec, 'saw_message': msg})
    else:
        record(f'{which} crashing without --share is reported', ok, {'exit': ec, 'saw_message': msg})


@scenario
def caddy_crashes():
    _service_crashes('caddy')


@scenario
def cloudflared_crashes():
    _service_crashes('cloudflared')


@scenario
def caddy_crashes_without_share():
    _service_crashes('caddy', args='')


@scenario
def caddy_exits_immediately():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir('caddy-exit1', {'caddy': WS.STUB_EXIT_1, 'cloudflared': None}))
    sh.wait(r'EXIT=\d+', 15)
    t = serve_output(sh)
    ec = exit_code(sh)
    ok = ec == 1 and 'serve: caddy failed to start' in t and 'cloudflared' not in t
    sh.finish()
    time.sleep(0.5)
    ok = ok and not no_orphans() and alive(decoy) is True
    record('caddy exiting immediately is reported and nothing else starts', ok, {'exit': ec})


@scenario
def caddy_never_listens():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir('caddy-hang', {'caddy': WS.STUB_SLEEP, 'cloudflared': None}))
    t0 = time.time()
    sh.wait(r'EXIT=\d+', 25)
    dt = time.time() - t0
    ec = exit_code(sh)
    # a live caddy whose port cannot be read is a different failure from a
    # caddy that died, and has to be reported as its own thing
    msg = 'serve: could not read the port caddy is listening on' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and 9.5 <= dt <= 13 and not no_orphans() and alive(decoy) is True
    record('caddy alive but never listening times out', ok, {'exit': ec, 'seconds': round(dt, 1)})


@scenario
def cloudflared_missing():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=cf_path('missing'), path_mode='system')
    sh.wait(r'EXIT=\d+', 10)
    ec = exit_code(sh)
    msg = 'serve: cloudflared not found' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and not no_orphans() and alive(decoy) is True
    record('with --share, missing cloudflared refuses to start anything', ok, {'exit': ec})


@scenario
def local_only_by_default():
    """Without --share nothing is published beyond the LAN: cloudflared is
    never started, and does not even have to be installed."""
    results = {}
    for label, prefix, mode in (('stub cloudflared on PATH', cf_path('sleep'), 'prefix'),
                                ('no cloudflared at all', cf_path('missing'), 'system')):
        sh = Shell()
        decoy = start_serve(sh, 'A', path_prefix=prefix, path_mode=mode, args='')
        sh.wait('Local: http://', 10)
        time.sleep(1.5)   # room for a wrongly started cloudflared to show up
        port = local_port(sh)
        r = {
            'serves the directory': bool(port) and get('A', port) is True,
            'cloudflared never started': 'CF_INVOKED_WITH' not in sh.text() and not cf_pids(),
            'no complaint about cloudflared': 'cloudflared' not in serve_output(sh),
        }
        sh.send('\x03')
        sh.wait(r'EXIT=\d+', 15)
        r['clean exit on Ctrl-C'] = exit_code(sh) == 0
        sh.finish()
        time.sleep(0.5)
        r['no leftover processes'] = not no_orphans()
        r['decoy untouched'] = alive(decoy) is True
        reap(our_pids(pattern=DECOY_PATTERN))
        failed = {k: v for k, v in r.items() if v is not True}
        if failed:
            results[label] = failed
    record('without --share only caddy runs, and cloudflared is optional', not results, results)


@scenario
def command_line_arguments():
    """An unknown option (a typo of --share, say) is refused before anything
    starts, rather than silently serving without the tunnel that was asked
    for; --help prints usage and starts nothing either."""
    results = {}
    for args, want_exit, want_text in (('--shar', 2, 'serve: unknown option: --shar'),
                                       ('share', 2, 'serve: unknown option: share'),
                                       ('--help', 0, 'usage: serve [--share]')):
        sh = Shell()
        decoy = start_serve(sh, 'A', args=args)
        sh.wait(r'EXIT=\d+', 10)
        t, ec = serve_output(sh), exit_code(sh)
        sh.finish()
        time.sleep(0.4)
        r = {
            'exit': ec == want_exit or f'got {ec}, want {want_exit}',
            'message': want_text in t,
            'nothing served': 'Serving:' not in t,
            'nothing left running': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        reap(our_pids(pattern=DECOY_PATTERN))
        failed = {k: v for k, v in r.items() if v is not True}
        if failed:
            results[args] = failed
    record('bad arguments are refused and --help starts nothing', not results, results)


@scenario
def cloudflared_ignores_signals():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=cf_path('ignore-signals'))
    sh.wait('Local: http://', 10)
    wait_until(lambda: cf_pids())
    t0 = time.time()
    sh.send('\x03')
    sh.wait(r'EXIT=\d+', 20)
    dt = time.time() - t0
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    ok = ec == 137 and 4.5 <= dt <= 7 and not no_orphans() and alive(decoy) is True
    record('a cloudflared that ignores TERM/HUP/INT is force-killed after a grace period', ok,
           {'exit': ec, 'seconds': round(dt, 1)})


def _other_instance_survives(kill_how):
    x, y = Shell(), Shell()
    dx = start_serve(x, 'A', decoy_tag=7150)
    dy = start_serve(y, 'B', decoy_tag=7151)
    x.wait('Local: http://', 10)
    y.wait('Local: http://', 10)
    time.sleep(1.0)
    port_x, port_y = local_port(x), local_port(y)
    before = {'caddy': len(our_pids(name='caddy')), 'stub_cf': len(our_pids(pattern=STUB_PATTERN))}
    if kill_how == 'ctrl-z-then-kill':
        x.send('\x1a')
        x.wait('suspended', 5)
    if kill_how == 'hangup':
        x.close_pty()
    else:
        os.kill(x.pid, signal.SIGKILL)
    time.sleep(3)
    x.finish()
    after = {'caddy': len(our_pids(name='caddy')), 'stub_cf': len(our_pids(pattern=STUB_PATTERN))}
    x_closed = get('A', port_x) is not True
    y_serving = get('B', port_y) is True
    y.send('\x03')
    y.wait(r'EXIT=\d+', 15)
    y_exit = exit_code(y)
    y.finish()
    time.sleep(0.5)
    ok = (before == {'caddy': 2, 'stub_cf': 2} and after == {'caddy': 1, 'stub_cf': 1}
          and x_closed and y_serving and y_exit == 0 and not no_orphans()
          and alive(dy) is True and (kill_how == 'hangup' or alive(dx) is True))
    record(f"instance X's shell dies ({kill_how}) while instance Y keeps serving", ok,
           {'before': before, 'after': after, 'y_exit': y_exit})


@scenario
def hangup_other_instance_survives():
    _other_instance_survives('hangup')


@scenario
def sigkill_other_instance_survives():
    _other_instance_survives('sigkill')


@scenario
def ctrl_z_then_sigkill_other_instance_survives():
    _other_instance_survives('ctrl-z-then-kill')


def serve_pid(parent_pid):
    """The zsh running the serve script, as a direct child of `parent_pid`."""
    kids = subprocess.run(['pgrep', '-P', str(parent_pid)], capture_output=True).stdout.decode().split()
    for p in kids:
        args = subprocess.run(['ps', '-o', 'args=', '-p', p], capture_output=True).stdout.decode()
        if SERVE in args:
            return int(p)
    return None


@scenario
def signalled_directly():
    """A signal sent to serve alone (say, `kill <pid>` from another terminal),
    not to its whole group: caddy and cloudflared never see it themselves, so
    only serve's own cleanup can stop them. Besides TERM and HUP, this covers
    every rarer signal whose default action would end serve outright."""
    results = {}
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM, signal.SIGUSR1, signal.SIGUSR2,
                signal.SIGVTALRM, signal.SIGPROF, signal.SIGXCPU, signal.SIGXFSZ):
        sh = Shell()
        decoy = start_serve(sh, 'A')
        sh.wait('Local: http://', 10)
        wait_until(lambda: our_pids(name='caddy') and cf_pids())
        pid = serve_pid(sh.pid)
        if pid:
            os.kill(pid, sig)
        sh.wait(r'EXIT=\d+', 15)
        ec = exit_code(sh)
        sh.finish()
        time.sleep(0.5)
        r = {
            'found serve': bool(pid),
            'clean exit': ec == 0 or f'got {ec}',
            'no leftover processes': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        reap(no_orphans() + our_pids(pattern=DECOY_PATTERN))
        failed = {k: v for k, v in r.items() if v is not True}
        if failed:
            results[sig.name] = failed
    record('any terminating signal sent to serve alone still stops everything', not results, results)


@scenario
def parent_dies_without_hangup():
    """The parent shell is SIGKILLed but is not the session leader (a nested
    shell inside the terminal's own), so the kernel sends no SIGHUP to anyone:
    serve has to notice its reparenting by itself. The other parent-death
    scenarios kill the session leader, where the kernel's SIGHUP alone would
    already stop serve and hide a broken parent check."""
    sh = Shell()
    sh.cmd('zsh -f -i')
    sh.wait(r'(?s)(\$ |% ).*(\$ |% )', 5)
    nested = subprocess.run(['pgrep', '-P', str(sh.pid)], capture_output=True).stdout.decode().split()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    wait_until(lambda: our_pids(name='caddy') and cf_pids())
    port = local_port(sh)
    running = bool(our_pids(name='caddy')) and bool(cf_pids())
    for p in nested:
        os.kill(int(p), signal.SIGKILL)
    t0 = time.time()
    gone = wait_until(lambda: not no_orphans(), timeout=10)
    dt = time.time() - t0
    # serve itself exits a moment after its children, once its cleanup sees them gone
    wait_until(lambda: not our_pids(pattern=SERVE), timeout=2)
    serve_left = our_pids(pattern=SERVE)
    closed = get('A', port) is not True
    decoy_ok = alive(decoy) is True
    sh.finish()
    time.sleep(0.3)
    ok = (len(nested) == 1 and running and gone and dt < 3 and not serve_left
          and closed and decoy_ok)
    record('parent shell dies without any SIGHUP: serve notices and cleans up', ok,
           {'nested shells': nested, 'was running': running, 'cleaned up': gone,
            'seconds': round(dt, 1), 'serve left': serve_left, 'port closed': closed,
            'decoy untouched': decoy_ok})


@scenario
def stop_during_download():
    """Stopping while someone is still downloading. Caddy's graceful shutdown
    waits for transfers in progress with no time limit, so a single TERM left
    serve waiting out its whole grace period and then reporting a SIGKILL
    (137), as if a child had ignored the signal. It must stop promptly and
    cleanly instead.

    With --real-tunnel the download goes through the public tunnel, so the
    real cloudflared is draining a request too: it reacts to repeated signals
    differently from caddy, and a stub cannot stand in for that."""
    big = os.path.join(WS.site('A'), 'big.bin')
    if not os.path.exists(big):
        with open(big, 'wb') as f:
            f.write(os.urandom(64 * 1024 * 1024))
    results = {}
    for how in ('Ctrl-C', 'TERM to serve alone'):
        sh = Shell()
        decoy = start_serve(sh, 'A')
        sh.wait('Local: http://', 10)
        wait_until(lambda: our_pids(name='caddy') and cf_pids())
        base = f'http://localhost:{local_port(sh)}'
        reachable = True
        if REAL_TUNNEL:
            sh.wait(TUNNEL_URL_RE.pattern, 40)
            base = tunnel_url(sh)
            # a fresh quick tunnel can take a good while to resolve and route
            reachable = wait_until(lambda: bool(base) and get('A', None, base=base) is True,
                                   timeout=90, interval=1)
        # throttled to well under the time this takes, so it is still in flight
        curl = subprocess.Popen(['curl', '-s', '-o', '/dev/null', '--limit-rate', '200k',
                                 f'{base}/big.bin'])
        time.sleep(3.0 if REAL_TUNNEL else 1.0)
        downloading = curl.poll() is None
        t0 = time.time()
        if how == 'Ctrl-C':
            sh.send('\x03', settle=0)
        else:
            os.kill(serve_pid(sh.pid), signal.SIGTERM)
        sh.wait(r'EXIT=\d+', 15)
        dt = time.time() - t0
        ec = exit_code(sh)
        curl.kill()
        curl.wait()
        sh.finish()
        time.sleep(0.5)
        r = {
            'tunnel reachable': reachable,
            'download was in flight': downloading,
            'clean exit': ec == 0 or f'got {ec}',
            'stopped promptly': dt < 3 or f'{dt:.1f}s',
            'no leftover processes': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        reap(no_orphans() + our_pids(pattern=DECOY_PATTERN))
        failed = {k: v for k, v in r.items() if v is not True}
        if failed:
            results[how] = failed
    record('stopping mid-download is prompt and clean, not a forced kill', not results, results)


@scenario
def missing_helper_commands():
    """Layer 1: every command serve shells out to is checked up front, and the
    error names the command that is actually missing."""
    results = {}
    for missing in REQUIRED_COMMANDS:
        present = {c: None for c in REQUIRED_COMMANDS if c != missing}
        if 'cloudflared' in present:
            present['cloudflared'] = WS.STUB_SLEEP
        present['sleep'] = None          # the decoy background job needs it
        sh = Shell()
        decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir(f'without-{missing}', present),
                            path_mode='only')
        sh.wait(r'EXIT=\d+', 10)
        t, ec = sh.text(), exit_code(sh)
        sh.finish()
        time.sleep(0.4)
        results[missing] = {
            'exit': ec,
            'named the missing command': f'serve: {missing} not found' in t,
            'nothing left running': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        reap(our_pids(pattern=DECOY_PATTERN))
    failed = {k: v for k, v in results.items()
              if not (v['exit'] == 1 and v['named the missing command']
                      and v['nothing left running'] and v['decoy untouched'])}
    record('a missing helper command is refused up front, by name', not failed, failed)


@scenario
def helper_misbehaves_at_run_time():
    """Layer 2: the pre-flight check only proves a command exists. One that
    exists but returns nonsense (broken, hijacked, or removed after the check)
    must never have its output spliced into a URL, and must never leave a
    service process behind."""
    cases = {
        'lsof prints a non-numeric port': ('lsof', '#!/bin/sh\necho "n*:not-a-port"\n'),
        'lsof prints an out-of-range port': ('lsof', '#!/bin/sh\necho "n*:999999"\n'),
        'lsof succeeds but reports no listener': ('lsof', '#!/bin/sh\nexit 0\n'),
        'lsof fails every time it runs': ('lsof', '#!/bin/sh\necho "lsof: broken" >&2\nexit 127\n'),
        'lsof prints an absurdly long number': ('lsof', '#!/bin/sh\necho "n*:99999999999999999999999"\n'),
    }
    results = {}
    for label, (cmd, body) in cases.items():
        files = {c: None for c in REQUIRED_COMMANDS}
        files[cmd] = body
        files['cloudflared'] = WS.STUB_CF_ANNOUNCES
        files['sleep'] = None            # the decoy background job needs it
        sh = Shell()
        decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir(f'broken-{label}', files),
                            path_mode='only')
        sh.wait(r'EXIT=\d+', 25)
        t, ec = sh.text(), exit_code(sh)
        sh.finish()
        time.sleep(0.4)
        results[label] = {
            'exit': ec,
            'refused with an accurate message': 'serve: could not read the port caddy is listening on' in t,
            'cloudflared never started': 'CF_INVOKED_WITH' not in t,
            'no zsh warning leaked': 'truncated' not in t,
            'nothing left running': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        reap(our_pids(pattern=DECOY_PATTERN))
    failed = {k: v for k, v in results.items()
              if not (v['exit'] == 1 and v['refused with an accurate message']
                      and v['cloudflared never started'] and v['no zsh warning leaked']
                      and v['nothing left running'] and v['decoy untouched'])}
    record('a helper that exists but misbehaves never reaches the tunnel URL', not failed, failed)


@scenario
def output_reader_exits():
    """serve's output piped into something that exits early. Whatever serve
    prints next hits a closed pipe: that must neither kill serve outright
    (SIGPIPE) nor make zsh abort the script on the write error, since either
    one skips the cleanup and leaves caddy serving with no parent."""
    results = {}
    chatty = WS.stub_dir('cf-chatty', {'caddy': None, 'cloudflared': WS.STUB_CF_CHATTY})
    for label, prefix, args in (
            ('--share 2>&1 | grep -m1 (the reader leaves mid-run)', chatty,
             '--share 2>&1 | grep -m1 CF_INVOKED_WITH'),
            ('| true (the reader is gone before serve prints)', cf_path('sleep'), '| true')):
        sh = Shell()
        decoy = start_serve(sh, 'A', path_prefix=prefix, args=args)
        returned = sh.wait(r'EXIT=\d+', 15)
        time.sleep(0.5)
        r = {
            'serve ended by itself': returned,
            'no leftover processes': not no_orphans(),
            'decoy untouched': alive(decoy) is True,
        }
        sh.finish()
        reap(our_pids(pattern=DECOY_PATTERN))
        failed = {k: v for k, v in r.items() if v is not True}
        if failed:
            results[label] = failed
    record('output piped into a reader that exits never orphans caddy', not results, results)


@scenario
def refuses_without_a_tty():
    # Not a pty shell, so it is registered by hand for the ownership sampler:
    # otherwise a caddy started here would never be recognised as ours
    p = subprocess.Popen(
        ['zsh', '-c', f'cd "{WS.site("A")}"; "{SERVE}"; print EXIT=$?'],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    SHELL_PIDS.append(p.pid)
    try:
        out = p.communicate(timeout=20)[0].decode()
    except subprocess.TimeoutExpired:
        p.kill()
        out = p.communicate()[0].decode()
    ok = 'refusing to start' in out and 'EXIT=1' in out and not no_orphans()
    record('refuses to start with no controlling terminal', ok, {'output': out.strip()})


SCENARIOS = [
    single_instance,
    concurrent_instances,
    triple_ctrl_c,
    ctrl_backslash,
    ctrl_c_during_startup,
    caddy_crashes,
    cloudflared_crashes,
    caddy_crashes_without_share,
    caddy_exits_immediately,
    caddy_never_listens,
    cloudflared_missing,
    local_only_by_default,
    command_line_arguments,
    cloudflared_ignores_signals,
    hangup_other_instance_survives,
    sigkill_other_instance_survives,
    ctrl_z_then_sigkill_other_instance_survives,
    signalled_directly,
    parent_dies_without_hangup,
    stop_during_download,
    missing_helper_commands,
    helper_misbehaves_at_run_time,
    output_reader_exits,
    refuses_without_a_tty,
]


def main():
    global WS, REAL_TUNNEL
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--real-tunnel', action='store_true',
                         help='use the real cloudflared binary for scenarios that check tunnel content')
    parser.add_argument('--only', nargs='*', metavar='NAME',
                         help='run only these scenario function names')
    args = parser.parse_args()
    REAL_TUNNEL = args.real_tunnel

    if not shutil.which('caddy'):
        sys.exit('caddy not found on PATH (brew install caddy)')
    if REAL_TUNNEL and not shutil.which('cloudflared'):
        sys.exit('cloudflared not found on PATH (brew install cloudflared)')
    if sys.platform != 'darwin':
        sys.exit('serve is macOS-only; these tests assume a macOS host')

    foreign = pids_exact('caddy') + pids_exact('cloudflared')
    if foreign:
        print(f'note: {len(foreign)} caddy/cloudflared process(es) already running '
              f'(pids {" ".join(foreign)}). They are not ours: this suite ignores them '
              f'and will never signal them.\n')

    WS = Workspace()
    try:
        to_run = [s for s in SCENARIOS if not args.only or s.__name__ in args.only]
        for s in to_run:
            s()
    finally:
        WS.cleanup()

    print()
    passed = sum(ok for _, ok in RESULTS)
    for name, ok in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print(f'{passed}/{len(RESULTS)} passed')
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == '__main__':
    main()
