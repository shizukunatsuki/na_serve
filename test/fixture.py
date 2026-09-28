"""What the cases run on: terminals, a process ledger, process accounting,
and stand-ins for the helpers serve runs.

This module only observes what serve did; what it *should* do is spec.py,
taken from README.md. Observing has to be airtight, because a fixture that
misses a process reports a leak-free run that is not one. Two independent
records make that hold:

* Every case runs in a pseudo-terminal of its own, which is a session of its
  own. Whatever serve starts, and whatever those start, stays in that session
  unless it calls setsid() itself, and the kernel answers membership directly
  (getsid). "Nothing left behind" is checked as "nothing but the shell and
  the decoy left in the session": no process tree to sample, so nothing
  short-lived can slip between two samples.

* serve's PATH holds nothing but ledger shims. caddy, cloudflared, lsof and
  scutil each write down who they are (PID, parent, process group, start
  time, arguments) and only then exec the real program under the same PID.
  So everything serve runs is on record before it can do anything, even if
  it later leaves the session, and a PID only counts as the recorded process
  while its start time still matches: PID reuse can fake neither a leak nor
  a clean exit.

Terminals must be read continuously. A terminal nobody reads fills up, and
then whatever writes to it blocks: a service mid-log, serve mid-print, or a
shell that is exiting and has to flush first. Such a shell never finishes
exiting, so the kernel never treats its jobs as orphaned, and the fixture
would report a leak that no real terminal would ever produce. Every wait in
this module therefore pumps every terminal that is still open.
"""
import collections
import os
import pty
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVE = os.environ.get('NA_SERVE_UNDER_TEST') or os.path.join(ROOT, 'serve')
PYTHON = sys.executable

REAL_CADDY = shutil.which('caddy')
REAL_CLOUDFLARED = shutil.which('cloudflared')
REAL_LSOF = '/usr/sbin/lsof'
REAL_SCUTIL = '/usr/sbin/scutil'

HOST = subprocess.run([REAL_SCUTIL, '--get', 'LocalHostName'],
                      capture_output=True, text=True).stdout.strip()

ANSI = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
RED = b'\x1b[1;31m'


def q(s):
    return shlex.quote(str(s))


# ------------------------------------------------------------------ processes

Proc = collections.namedtuple('Proc', 'pid ppid pgid stat lstart command')


def proc_table(commands=False):
    """Every live process by PID. Zombies are left out: they have already
    exited and hold nothing but an entry for their parent to collect.

    Command lines are only read when asked for. Reading them means asking
    about every process's arguments, and that blocks for seconds while
    macOS's crash reporter holds a process that just crashed (which some
    cases cause on purpose); everything else here comes back at once."""
    fields = 'pid=,ppid=,pgid=,stat=,lstart=' + (',command=' if commands else '')
    out = subprocess.run(['/bin/ps', '-A', '-ww', '-o', fields], capture_output=True, text=True).stdout
    table = {}
    for line in out.splitlines():
        f = line.split()
        if len(f) < 9 or 'Z' in f[3]:
            continue
        table[int(f[0])] = Proc(int(f[0]), int(f[1]), int(f[2]), f[3], ' '.join(f[4:9]), ' '.join(f[9:]))
    return table


def commands(pids):
    """Command lines of just these processes, for reports."""
    pids = [str(p) for p in pids]
    if not pids:
        return {}
    out = subprocess.run(['/bin/ps', '-ww', '-o', 'pid=,command=', '-p', ','.join(pids)],
                         capture_output=True, text=True).stdout
    return {int(l.split(None, 1)[0]): (l.split(None, 1) + [''])[1] for l in out.splitlines() if l.strip()}


def session_of(pid):
    try:
        return os.getsid(pid)
    except OSError:
        return None


def session_members(sid, table=None):
    table = proc_table() if table is None else table
    return {pid: p for pid, p in table.items() if session_of(pid) == sid}


OPEN_TERMINALS = []


def pump_all():
    for t in list(OPEN_TERMINALS):
        t.pump()


def wait_until(predicate, timeout, interval=0.05):
    """Poll until `predicate` holds or `timeout` passes, reading every open
    terminal meanwhile. Returns whether it held."""
    deadline = time.time() + timeout
    while True:
        pump_all()
        if predicate():
            return True
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def sleep(seconds):
    """A pause that keeps the terminals read."""
    wait_until(lambda: False, seconds)


# --------------------------------------------------------------------- ledger

Entry = collections.namedtuple('Entry', 'name pid ppid pgid lstart args')

SHIM = """#!/bin/sh
# Ledger shim: record who this process is, then become {name} (same PID).
printf '%s\\t%s\\t%s\\t%s\\t%s\\n' {name} $$ $PPID "$(/bin/ps -o pgid=,lstart= -p $$)" "$*" >> {ledger}
{action}
"""


class Ledger:
    def __init__(self, path):
        self.path = path

    def entries(self, name=None):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                parts = line.rstrip('\n').split('\t')
                if len(parts) != 5 or len(parts[3].split()) < 6:
                    continue      # a line still being written
                name_, pid, ppid, pl, args = parts
                pl = pl.split()
                out.append(Entry(name_, int(pid), int(ppid), int(pl[0]), ' '.join(pl[1:6]), args))
        return [e for e in out if name is None or e.name == name]

    def first(self, name):
        e = self.entries(name)
        return e[0] if e else None


def running(entry, table=None):
    """Whether the recorded process itself (not a reused PID) is still alive."""
    table = proc_table() if table is None else table
    p = table.get(entry.pid)
    return p is not None and p.lstart == entry.lstart


# ------------------------------------------------------------ helper stand-ins

def exec_real(path):
    return f'exec {q(path)} "$@"'


CADDY = {
    'real': lambda: exec_real(REAL_CADDY),
    # A deterministic startup window: caddy launched, not yet listening
    'slow': lambda: f'/bin/sleep 2\n{exec_real(REAL_CADDY)}',
    'exits': lambda: 'exit 1',
    'never-listens': lambda: 'exec /bin/sleep 3600',
}

LSOF = {
    'real': lambda: exec_real(REAL_LSOF),
    'not-a-number': lambda: 'echo "n*:http"',
    'zero': lambda: 'echo "n*:0"',
    'too-big': lambda: 'echo "n*:65536"',
    'way-too-big': lambda: 'echo "n*:99999999999999999999999"',
    'says-nothing': lambda: 'exit 0',
    'fails': lambda: 'echo "lsof: broken" >&2; exit 1',
    'hangs': lambda: 'exec /bin/sleep 3600',
}

SCUTIL = {
    'real': lambda: exec_real(REAL_SCUTIL),
    'fails': lambda: 'exit 1',
    # A deterministic window before serve traps anything; it finishes by
    # itself, so whatever serve does meanwhile, it leaves nothing hanging
    'slow': lambda: f'/bin/sleep 2\n{exec_real(REAL_SCUTIL)}',
    'hangs': lambda: 'exec /bin/sleep 3600',
}

# A stand-in for cloudflared that reproduces what serve's lifecycle depends
# on, without a tunnel (Cloudflare rate-limits quick tunnels, and a full run
# starts hundreds). It treats signals the way the real binary does, per Go's
# runtime rules and cloudflared's shutdown code, as confirmed against the
# real binary:
#
# - SIGINT is re-enabled even though a background job starts with it ignored
#   (cloudflared asks for it); SIGQUIT stays ignored; SIGHUP is not handled,
#   so its default action ends the process.
# - The first TERM or INT starts a graceful shutdown: immediate with no
#   request in flight ('idle'), a grace period with one ('draining'). From
#   then on it no longer handles signals: another TERM ends it by default
#   action, while SIGINT is back to ignored, as it started.
# - A write to a closed pipe on stdout/stderr ends it with SIGPIPE, as in
#   any Go program; any other failed write (EIO once its terminal is gone)
#   is ignored. It logs the tunnel URL at once and one more line a second
#   later.
#
# 'stubborn' is not modelled on cloudflared: it ignores everything a process
# can ignore, to exercise serve's last resort.
CLOUDFLARED_STUB = """#!{python}
import os, signal, sys, time
MODE = {mode!r}
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def log(msg):
    # a failed write (EIO once the terminal is gone) is shrugged off, as Go's
    # logger does; only a closed pipe is fatal, through SIGPIPE above
    try:
        sys.stderr.write(time.strftime('%Y-%m-%dT%H:%M:%SZ ') + msg + '\\n')
        sys.stderr.flush()
    except OSError:
        pass

if MODE == 'stubborn':
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(s, signal.SIG_IGN)
else:
    inherited_int = signal.getsignal(signal.SIGINT)
    def graceful(sig, frame):
        if MODE == 'idle':
            os._exit(0)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, inherited_int)
        time.sleep(30)
        os._exit(0)
    signal.signal(signal.SIGTERM, graceful)
    signal.signal(signal.SIGINT, graceful)

log('INF Requesting new quick Tunnel on trycloudflare.com...')
log('INF |  https://stub-%d.trycloudflare.com  |' % os.getpid())
time.sleep(1)
log('INF Registered tunnel connection connIndex=0')
while True:
    time.sleep(3600)
"""


def write_shims(directory, ledger, caddy='real', lsof='real', scutil='real',
                cloudflared='idle', missing=()):
    """One shim per helper in `directory`. `cloudflared` is a stub mode
    ('idle', 'draining', 'stubborn') or 'real'; names in `missing` get no
    shim, so serve cannot find them."""
    os.makedirs(directory, exist_ok=True)
    actions = {'caddy': CADDY[caddy](), 'lsof': LSOF[lsof](), 'scutil': SCUTIL[scutil]()}
    if cloudflared == 'real':
        actions['cloudflared'] = exec_real(REAL_CLOUDFLARED)
    else:
        stub = os.path.join(directory, '.cloudflared-stub')
        with open(stub, 'w') as f:
            f.write(CLOUDFLARED_STUB.format(python=PYTHON, mode=cloudflared))
        os.chmod(stub, 0o755)
        actions['cloudflared'] = exec_real(stub)
    for name, action in actions.items():
        if name in missing:
            continue
        path = os.path.join(directory, name)
        with open(path, 'w') as f:
            f.write(SHIM.format(name=name, ledger=q(ledger), action=action))
        os.chmod(path, 0o755)


# ------------------------------------------------------------------ terminals

class Terminal:
    """An interactive `zsh -f -i` on a pseudo-terminal of its own, so it leads
    a new session. Output is collected by pump(), which every wait calls."""

    def __init__(self):
        pid, fd = pty.fork()
        if pid == 0:
            env = {k: os.environ[k] for k in ('PATH', 'HOME', 'USER', 'LANG') if k in os.environ}
            env['TERM'] = 'dumb'
            os.execve('/bin/zsh', ['zsh', '-f', '-i'], env)
        self.pid = self.sid = pid
        self.fd = fd
        os.set_blocking(fd, False)
        self.raw = b''
        self.open = True
        OPEN_TERMINALS.append(self)
        self.prompt('O')
        self.shell = pid          # the shell serve's command line goes to
        self.nested = None

    def prompt(self, tag):
        """Switch off line editing and set a prompt unique to this shell
        level, then wait until that prompt shows."""
        mark = self.mark()
        self.run(f"zmodload zsh/datetime; unsetopt zle; PS1='{tag}$ '; PS2=''")
        if not self.expect(re.escape(tag) + r'\$ $', 5, mark):
            raise RuntimeError(f'shell {tag} never became ready')

    def start_nested(self):
        """A second interactive zsh inside this one, which then receives the
        command lines. Unlike the outer shell it does not lead the session, so
        when it dies the kernel sends nobody a SIGHUP."""
        mark = self.mark()
        self.run('/bin/zsh -f -i')
        found = []
        wait_until(lambda: found.extend(p for p in proc_table(commands=True).values()
                                        if p.ppid == self.pid and p.command == '/bin/zsh -f -i') or found, 5)
        if not found:
            raise RuntimeError('nested shell never started')
        self.expect(r'\S', 5, mark)
        self.prompt('N')
        self.nested = self.shell = found[0].pid

    def pump(self):
        while self.open:
            try:
                chunk = os.read(self.fd, 65536)
            except BlockingIOError:
                return
            except OSError:          # EIO: the slave side is gone
                return
            if not chunk:
                return
            self.raw += chunk

    def mark(self):
        self.pump()
        return len(self.raw)

    def text(self, since=0):
        self.pump()
        return ANSI.sub('', self.raw[since:].decode(errors='replace')).replace('\r', '')

    def expect(self, pattern, timeout, since=0):
        found = []

        def check():
            m = re.search(pattern, self.text(since))
            if m:
                found.append(m)
            return bool(m)
        wait_until(check, timeout, 0.02)
        return found[0] if found else None

    def send(self, data):
        if self.open:
            try:
                os.write(self.fd, data.encode() if isinstance(data, str) else data)
            except OSError:
                pass

    def run(self, line):
        self.send(line + '\n')

    def hang_up(self):
        """Close the master side: the terminal goes away under its session."""
        if self.open:
            self.pump()
            self.open = False
            OPEN_TERMINALS.remove(self)
            os.close(self.fd)

    def destroy(self):
        """Tear down everything in this terminal's session, whatever state it
        is in. The master is closed first: a shell cannot finish exiting while
        output it still has to flush sits unread."""
        self.hang_up()
        for pid in session_members(self.sid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.waitpid(self.pid, 0)
        except ChildProcessError:
            pass


# ------------------------------------------------------------------- network

def http_get(url, timeout=5):
    try:
        return urllib.request.urlopen(url, timeout=timeout).read()
    except Exception as e:           # reported as a failed check, not raised
        return f'ERROR {e}'.encode()


def listening(pid):
    """(addresses, ports) of the TCP sockets `pid` listens on, per real lsof."""
    out = subprocess.run([REAL_LSOF, '-nP', '-a', '-p', str(pid), '-iTCP', '-sTCP:LISTEN', '-Fn'],
                         capture_output=True, text=True).stdout
    return [line[1:] for line in out.splitlines() if line.startswith('n')]


def start_download(url):
    """A throttled download that stays in flight for minutes."""
    return subprocess.Popen(['/usr/bin/curl', '-s', '-o', '/dev/null', '--limit-rate', '200k', url],
                            stdin=subprocess.DEVNULL, start_new_session=True)
