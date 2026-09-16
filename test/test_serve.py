#!/usr/bin/env python3
"""End-to-end tests for `serve` (../serve.zsh) against real caddy and cloudflared
binaries, driven through an interactive zsh on a real pseudo-terminal.

Every scenario checks two things after it ends, regardless of what else it
asserts: no caddy/cloudflared/stub process is left running (no orphans), and
an unrelated "decoy" process started in the same shell is still alive (no
process outside serve's own process group was ever signalled).

cloudflared is replaced by a sleeping stub in every scenario by default,
because Cloudflare rate-limits how often one IP can provision quick tunnels
and a routine test run would otherwise start hitting 429s after a handful of
runs. Pass --real-tunnel to use the real binary for the scenarios that care
about tunnel content (single instance, concurrency); everything that tests
signal handling or process lifecycle behaves identically either way, since
that logic never looks at what cloudflared actually is.

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
SERVE = os.path.join(ROOT, 'serve.zsh')
ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
LOCAL_URL_RE = re.compile(r'Local: http://([^:/]+)\.local:(\d+)/')
TUNNEL_URL_RE = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
HOST = subprocess.run(['scutil', '--get', 'LocalHostName'], capture_output=True).stdout.decode().strip()

REAL_TUNNEL = False  # set from argv in main()


# --------------------------------------------------------------------- pty shell

class Shell:
    """An interactive `zsh -f -i` on its own pseudo-terminal.

    A background thread drains the pty continuously so output can never sit
    in the kernel tty queue, where a Ctrl-C would flush (discard) it before
    the test gets to read it.
    """

    def __init__(self, env_extra=None):
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            env['TERM'] = 'xterm-256color'
            if env_extra:
                env.update(env_extra)
            os.execvpe('zsh', ['zsh', '-f', '-i'], env)
        os.set_inheritable(self.fd, False)  # never leak into a later pty.fork()
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


def no_orphans(stub_pattern=None):
    left = pids_exact('caddy') + pids_exact('cloudflared')
    if stub_pattern:
        left += pids_matching(stub_pattern)
    return left


class Workspace:
    """Scratch area for one test run: real site directories, stub binaries."""

    STUB_SLEEP = '#!/bin/sh\nexec sleep 7200\n'
    STUB_IGNORES_SIGNALS = (
        '#!/bin/sh\n'
        'exec python3 -c "import signal, time\n'
        'for s in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT): signal.signal(s, signal.SIG_IGN)\n'
        'time.sleep(7200)"\n'
    )
    STUB_EXIT_1 = '#!/bin/sh\nexit 1\n'
    STUB_NEVER_LISTENS = '#!/bin/sh\nexec sleep 7200\n'

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

    `behavior`: 'sleep' (default, harmless placeholder), 'ignore-signals', or
    'missing' (caddy only, no cloudflared at all).
    """
    if behavior == 'missing':
        return WS.stub_dir('caddy-only', {'caddy': None})
    body = WS.STUB_IGNORES_SIGNALS if behavior == 'ignore-signals' else WS.STUB_SLEEP
    return WS.stub_dir(f'cf-{behavior}', {'caddy': None, 'cloudflared': body})


def start_serve(sh, site_letter, path_prefix='real-or-stub', decoy_port=7101, replace_path=False):
    """Source serve.zsh, cd into a per-scenario site dir, start a decoy
    background job, then invoke serve. Returns the decoy's PID.

    `path_prefix` is normally prepended to the existing PATH. Pass
    `replace_path=True` for a stub directory that must HIDE a real binary
    (e.g. the "cloudflared is missing" scenario) rather than just shadow it:
    prepending would still leave the real one reachable later on PATH.
    """
    if path_prefix == 'real-or-stub':
        path_prefix = None if REAL_TUNNEL else cf_path('sleep')
    if path_prefix:
        if replace_path:
            sh.cmd(f'export PATH="{path_prefix}:/usr/bin:/bin:/usr/sbin:/sbin"')
        else:
            sh.cmd(f'export PATH="{path_prefix}:$PATH"')
    sh.cmd(f'source "{SERVE}"')
    sh.cmd(f'cd "{WS.site(site_letter)}"')
    sh.cmd(f'sleep {decoy_port} &')
    m = re.search(r'\[\d+\] (\d+)', sh.text()[-200:])
    decoy = m.group(1) if m else None
    sh.cmd('serve; print EXIT=$?', settle=0.05)
    return decoy


def exit_code(sh):
    m = re.findall(r'EXIT=(\d+)', sh.text())
    return int(m[-1]) if m else None


def local_port(sh):
    m = LOCAL_URL_RE.search(sh.text())
    return int(m.group(2)) if m else None


def tunnel_url(sh):
    m = TUNNEL_URL_RE.search(sh.text())
    return m.group(0) if m else None


def get(letter, port, host='localhost'):
    try:
        return http_get(f'http://{host}:{port}/who.txt').strip() == f'content of site {letter}'
    except Exception as e:
        return f'ERR {e}'


# --------------------------------------------------------------------- scenarios

RESULTS = []


def record(name, ok, detail=''):
    RESULTS.append((name, ok))
    tag = 'PASS' if ok else 'FAIL'
    print(f'[{tag}] {name}' + (f': {detail}' if detail and not ok else ''))


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
        try:
            fn()
        except Exception as e:
            record(fn.__name__, False, repr(e))
        finally:
            # pgrep on macOS uses POSIX extended regex: no \d shorthand, use [0-9]
            leftover = no_orphans(r'sleep 720[0-9]') + pids_matching(r'^sleep 71[0-9][0-9]$')
            for p in leftover:
                try:
                    os.kill(int(p), signal.SIGTERM)
                except (ValueError, ProcessLookupError):
                    pass
            time.sleep(0.3)
    return wrapped


@scenario
def single_instance():
    sh = Shell()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    if REAL_TUNNEL:
        sh.wait(TUNNEL_URL_RE.pattern, 40)
    t = sh.text()
    port = local_port(sh)
    checks = {
        'red serving line names the right directory': f'Serving: {WS.site("A")}' in t,
        'red serving line is actually red': b'\x1b[31mServing: ' in sh.raw_bytes(),
        'lan hostname correct': bool(LOCAL_URL_RE.search(t)) and LOCAL_URL_RE.search(t).group(1) == HOST,
        'port in ephemeral range': bool(port) and 49152 <= port <= 65535,
    }
    time.sleep(0.3)
    checks['serves over 127.0.0.1'] = get('A', port, '127.0.0.1') is True
    checks['serves over ::1'] = get('A', port, '[::1]') is True
    checks['serves over .local hostname'] = get('A', port, f'{HOST}.local') is True
    try:
        checks['directory listing shows the file'] = 'who.txt' in http_get(f'http://localhost:{port}/')
    except Exception as e:
        checks['directory listing shows the file'] = f'ERR {e}'
    cf_args = subprocess.run(
        ['ps', '-o', 'args=', '-p', ','.join(pids_exact('cloudflared') or ['0'])],
        capture_output=True).stdout.decode()
    if REAL_TUNNEL:
        checks['tunnel targets localhost, not 127.0.0.1'] = f'--url http://localhost:{port}' in cf_args
        checks['tunnel URL printed'] = bool(tunnel_url(sh))
    pgids = subprocess.run(
        ['ps', '-o', 'pgid=', '-p', ','.join(pids_exact('caddy') + (pids_exact('cloudflared') or pids_matching('sleep 7200')))],
        capture_output=True).stdout.decode().split()
    checks['caddy and cloudflared share one process group'] = len(set(pgids)) == 1 and len(pgids) == 2
    sh.send('\x03')
    sh.wait(r'EXIT=\d+', 15)
    checks['clean exit on Ctrl-C'] = exit_code(sh) == 0
    sh.finish()
    time.sleep(0.5)
    checks['no leftover processes'] = not no_orphans(r'sleep 7200')
    checks['decoy process untouched'] = alive(decoy) is True
    failed = {k: v for k, v in checks.items() if v is not True}
    record('single instance: full business + lifecycle check', not failed, failed)


@scenario
def concurrent_instances():
    shells, decoys, ports = {}, {}, {}
    for i, letter in enumerate('ABC'):
        shells[letter] = Shell()
        decoys[letter] = start_serve(shells[letter], letter, decoy_port=7110 + i)
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
    checks['no leftover processes'] = not no_orphans(r'sleep 7200')
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
           ec == 0 and not no_orphans(r'sleep 7200') and alive(decoy) is True,
           {'exit': ec})


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
    sh.send('\x03', settle=0.0)
    sh.wait(r'EXIT=\d+', 15)
    t = sh.text().split('serve; print')[-1]
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    ok = (ec == 0 and 'serve: caddy failed to start' not in t
          and not no_orphans(r'sleep 7200') and alive(decoy) is True)
    record('Ctrl-C racing the startup window never misreports "caddy failed to start"',
           ok, {'exit': ec, 'output_tail': t[-200:]})


def _service_crashes(which):
    sh = Shell()
    decoy = start_serve(sh, 'A')
    sh.wait('Local: http://', 10)
    time.sleep(1.5)
    victims = pids_exact(which) if which == 'caddy' else (pids_exact('cloudflared') or pids_matching('sleep 7200'))
    for p in victims:
        os.kill(int(p), signal.SIGKILL)
    sh.wait(r'EXIT=\d+', 15)
    ec = exit_code(sh)
    msg = f'serve: {which} exited' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and not no_orphans(r'sleep 7200') and alive(decoy) is True
    record(f'{which} crashing brings the other one down too', ok, {'exit': ec, 'saw_message': msg})


@scenario
def caddy_crashes():
    _service_crashes('caddy')


@scenario
def cloudflared_crashes():
    _service_crashes('cloudflared')


@scenario
def caddy_exits_immediately():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir('caddy-exit1', {'caddy': WS.STUB_EXIT_1, 'cloudflared': None}))
    sh.wait(r'EXIT=\d+', 15)
    t = sh.text()
    ec = exit_code(sh)
    ok = ec == 1 and 'serve: caddy failed to start' in t and 'cloudflared' not in t.split('serve; print')[-1]
    sh.finish()
    time.sleep(0.5)
    ok = ok and not no_orphans(r'sleep 7200') and alive(decoy) is True
    record('caddy exiting immediately is reported and nothing else starts', ok, {'exit': ec})


@scenario
def caddy_never_listens():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=WS.stub_dir('caddy-hang', {'caddy': WS.STUB_NEVER_LISTENS, 'cloudflared': None}))
    t0 = time.time()
    sh.wait(r'EXIT=\d+', 20)
    dt = time.time() - t0
    ec = exit_code(sh)
    msg = 'serve: caddy failed to start' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and 2.5 <= dt <= 6 and not no_orphans(r'sleep 7200') and alive(decoy) is True
    record('caddy alive but never listening times out', ok, {'exit': ec, 'seconds': round(dt, 1)})


@scenario
def cloudflared_missing():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=cf_path('missing'), replace_path=True)
    sh.wait(r'EXIT=\d+', 10)
    ec = exit_code(sh)
    msg = 'serve: cloudflared not found' in sh.text()
    sh.finish()
    time.sleep(0.5)
    ok = ec == 1 and msg and not no_orphans(r'sleep 7200') and alive(decoy) is True
    record('missing cloudflared refuses to start anything', ok, {'exit': ec})


@scenario
def cloudflared_ignores_signals():
    sh = Shell()
    decoy = start_serve(sh, 'A', path_prefix=cf_path('ignore-signals'))
    sh.wait('Local: http://', 10)
    time.sleep(1.0)
    t0 = time.time()
    sh.send('\x03')
    sh.wait(r'EXIT=\d+', 20)
    dt = time.time() - t0
    ec = exit_code(sh)
    sh.finish()
    time.sleep(0.5)
    ok = ec == 137 and 4.5 <= dt <= 7 and not no_orphans(r'sleep 7200') and alive(decoy) is True
    record('a cloudflared that ignores TERM/HUP/INT is force-killed after a grace period', ok,
           {'exit': ec, 'seconds': round(dt, 1)})


def _other_instance_survives(kill_how):
    x, y = Shell(), Shell()
    dx = start_serve(x, 'A', decoy_port=7150)
    dy = start_serve(y, 'B', decoy_port=7151)
    x.wait('Local: http://', 10)
    y.wait('Local: http://', 10)
    time.sleep(1.0)
    port_x, port_y = local_port(x), local_port(y)
    before = {'caddy': len(pids_exact('caddy')), 'stub_cf': len(pids_matching(r'sleep 7200'))}
    if kill_how == 'ctrl-z-then-kill':
        x.send('\x1a')
        x.wait('suspended', 5)
    if kill_how == 'hangup':
        x.close_pty()
    else:
        os.kill(x.pid, signal.SIGKILL)
    time.sleep(3)
    x.finish()
    after = {'caddy': len(pids_exact('caddy')), 'stub_cf': len(pids_matching(r'sleep 7200'))}
    x_closed = get('A', port_x) is not True
    y_serving = get('B', port_y) is True
    y.send('\x03')
    y.wait(r'EXIT=\d+', 15)
    y_exit = exit_code(y)
    y.finish()
    time.sleep(0.5)
    ok = (before == {'caddy': 2, 'stub_cf': 2} and after == {'caddy': 1, 'stub_cf': 1}
          and x_closed and y_serving and y_exit == 0 and not no_orphans(r'sleep 7200')
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


@scenario
def refuses_without_a_tty():
    p = subprocess.run(
        ['zsh', '-c', f'source "{SERVE}"; cd "{WS.site("A")}"; serve; print EXIT=$?'],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
    out = p.stdout.decode() + p.stderr.decode()
    ok = 'refusing to start' in out and 'EXIT=1' in out and not no_orphans()
    record('refuses to start with no controlling terminal', ok, {'output': out.strip()})


SCENARIOS = [
    single_instance,
    concurrent_instances,
    triple_ctrl_c,
    ctrl_c_during_startup,
    caddy_crashes,
    cloudflared_crashes,
    caddy_exits_immediately,
    caddy_never_listens,
    cloudflared_missing,
    cloudflared_ignores_signals,
    hangup_other_instance_survives,
    sigkill_other_instance_survives,
    ctrl_z_then_sigkill_other_instance_survives,
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
        sys.exit('serve.zsh is macOS-only; these tests assume a macOS host')

    if no_orphans():
        sys.exit('a caddy or cloudflared process is already running; stop it before testing')

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
