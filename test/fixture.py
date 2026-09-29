"""What the cases run on: terminals, a process ledger, process accounting,
and stand-ins for the helpers serve runs.

This module only observes what serve did; what it *should* do is spec.py,
taken from the test plan (PLAN.md). Observing has to be airtight, because a fixture that
misses a process reports a leak-free run that is not one. Two independent
records make that hold:

* Every case runs in a pseudo-terminal of its own, which is a session of its
  own. Whatever serve starts, and whatever those start, stays in that session
  unless it calls setsid() itself, and the kernel answers membership directly
  (getsid). "Nothing left behind" is checked as "nothing but the shell and
  the decoy left in the session": no process tree to sample, so nothing
  short-lived can slip between two samples.

* serve's PATH holds nothing but ledger shims. caddy, cloudflared, lsof,
  scutil, ps and curl each write down who they are (PID, parent, arguments) and only then
  exec the real program under the same PID, so everything serve runs is on
  record before it can do anything. A shim uses nothing but shell builtins:
  serve runs lsof ten times a second while it waits for a port, and a shim
  that ran ps (as an earlier one did) made serve itself slow whenever ps
  stalled, which is the fixture disturbing what it measures. The start time
  that pins a PID to the recorded process is therefore taken by the fixture,
  the first time it sees the process alive in its session; from then on a
  PID only counts while that start time still matches, so PID reuse can fake
  neither a leak nor a clean exit. A process never seen alive (a helper that
  finished within milliseconds) counts only while it is in the session.

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
REAL_PS = '/bin/ps'
REAL_CURL = '/usr/bin/curl'

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


def process_start(pid):
    """When `pid` was created, in seconds since the epoch, to the microsecond,
    as the kernel recorded it (the fork, so before the process could do
    anything); None if it is gone. ps only gives whole seconds."""
    import ctypes
    import ctypes.util
    libc = ctypes.CDLL(ctypes.util.find_library('c'), use_errno=True)
    buf = ctypes.create_string_buffer(1024)   # struct kinfo_proc (648 bytes)
    size = ctypes.c_size_t(len(buf))
    mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
    if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0 or size.value == 0:
        return None
    # kp_proc.p_starttime, a struct timeval, opens the structure
    sec = int.from_bytes(buf.raw[0:8], 'little')
    usec = int.from_bytes(buf.raw[8:12], 'little')
    return sec + usec / 1e6


def session_of(pid):
    try:
        return os.getsid(pid)
    except OSError:
        return None


def session_members(sid, table=None):
    table = proc_table() if table is None else table
    return {pid: p for pid, p in table.items() if session_of(pid) == sid}


OPEN_TERMINALS = []

# The longest this process went between two turns of any wait loop: those
# turns come every 20-100 ms, so a much longer gap means the whole machine
# stalled, and a timing measured across it says little about serve.
MAX_GAP = [0.0]


def register_session(sid):
    """Note a session this run created, the moment it exists, in the file
    named by NA_SESSIONS_FILE: whoever cleans up after the run (even one cut
    short, with cases still in flight) can then find everything in it."""
    path = os.environ.get('NA_SESSIONS_FILE')
    if path:
        with open(path, 'a') as f:
            f.write(f'{sid}\n')


def pump_all():
    for t in list(OPEN_TERMINALS):
        t.pump()


def wait_until(predicate, timeout, interval=0.05):
    """Poll until `predicate` holds or `timeout` passes, reading every open
    terminal meanwhile. Returns whether it held."""
    deadline = time.time() + timeout
    last = time.time()
    while True:
        pump_all()
        if predicate():
            return True
        now = time.time()
        MAX_GAP[0] = max(MAX_GAP[0], now - last)
        if now >= deadline:
            return False
        time.sleep(interval)
        last = time.time()


def sleep(seconds):
    """A pause that keeps the terminals read."""
    wait_until(lambda: False, seconds)


# --------------------------------------------------------------------- ledger

Entry = collections.namedtuple('Entry', 'name pid ppid args')

SHIM = """#!/bin/sh
# Ledger shim: record who this process is, then become {name} (same PID).
# Builtins only: nothing here may slow serve down.
printf '%s\\t%s\\t%s\\t%s\\n' {name} $$ $PPID "$*" >> {ledger}
{action}
"""


class Ledger:
    """The shims' record. `sids` are the sessions this ledger's processes
    belong to (the case's); it may grow as the case opens more."""

    def __init__(self, path, sids):
        self.path = path
        self.sids = sids
        self.seen = {}        # PID -> start time, from the first sighting

    def entries(self, name=None):
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path) as f:
            for line in f:
                if not line.endswith('\n'):
                    continue      # a line still being written
                parts = line.rstrip('\n').split('\t')
                if len(parts) != 4:
                    continue
                out.append(Entry(parts[0], int(parts[1]), int(parts[2]), parts[3]))
        return [e for e in out if name is None or e.name == name]

    def first(self, name):
        e = self.entries(name)
        return e[0] if e else None

    def running(self, entry, table=None):
        """Whether the recorded process itself (not a reused PID) is alive."""
        table = proc_table() if table is None else table
        p = table.get(entry.pid)
        if p is None:
            return False
        if entry.pid in self.seen:
            return p.lstart == self.seen[entry.pid]
        if session_of(entry.pid) in self.sids:
            self.seen[entry.pid] = p.lstart
            return True
        return False

    def still_running(self, table=None):
        table = proc_table() if table is None else table
        return [e for e in self.entries() if self.running(e, table)]


# ------------------------------------------------------------ helper stand-ins

# How long a stand-in that "hangs" actually runs. Long enough to outlast every
# time bound in the plan, so a serve that fails to end it leaves it behind and
# fails the case; short enough that nothing waits on it forever, and nothing
# outlives a crashed run for long.
STALL = 30
# How long the cloudflared stand-in lives: far longer than any case.
STUB_LIFETIME = 300


def exec_real(path):
    return f'exec {q(path)} "$@"'


CADDY = {
    'real': lambda: exec_real(REAL_CADDY),
    # A deterministic startup window: caddy launched, not yet listening
    'slow': lambda: f'/bin/sleep 2\n{exec_real(REAL_CADDY)}',
    'exits': lambda: 'exit 1',
    'never-listens': lambda: f'exec /bin/sleep {STALL}',
}

LSOF = {
    'real': lambda: exec_real(REAL_LSOF),
    # Hangs only when asked about a cloudflared the ledger knows (whatever
    # way its PID is written among the arguments); every other call is real
    'hangs-for-cloudflared': lambda: f'''while IFS='	' read -r name pid rest; do
  if [ "$name" = cloudflared ]; then
    case " $* " in *[!0-9]"$pid"[!0-9]*) exec /bin/sleep {STALL} ;; esac
  fi
done < "$LEDGER"
{exec_real(REAL_LSOF)}''',
    'not-a-number': lambda: 'echo "n*:http"',
    'zero': lambda: 'echo "n*:0"',
    'too-big': lambda: 'echo "n*:65536"',
    'way-too-big': lambda: 'echo "n*:99999999999999999999999"',
    'says-nothing': lambda: 'exit 0',
    'fails': lambda: 'echo "lsof: broken" >&2; exit 1',
    'hangs': lambda: f'exec /bin/sleep {STALL}',
}

SCUTIL = {
    'real': lambda: exec_real(REAL_SCUTIL),
    'fails': lambda: 'exit 1',
    # A deterministic window before serve traps anything; it finishes by
    # itself, so whatever serve does meanwhile, it leaves nothing hanging
    'slow': lambda: f'/bin/sleep 2\n{exec_real(REAL_SCUTIL)}',
    'hangs': lambda: f'exec /bin/sleep {STALL}',
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
# Its tunnel address is reported the way the real one does it, also as
# confirmed against the real binary: once the address is known, it is logged
# and a metrics server opens on 127.0.0.1, on the first free port of
# 20241-20245 (any free port if none is), answering GET /quicktunnel with
# {"hostname":"<host>"}. Before that, nothing listens. `address` picks when
# and what it reports:
#
#   'ok'       at once, stub-<PID>.trycloudflare.com
#   'late'     the same, 3 seconds in
#   'never'    no address at all: nothing logged, nothing listening
#   '404'      logged, but /quicktunnel answers 404
#   'not-json' logged, but /quicktunnel answers something else entirely
#   'empty'    logged, but the hostname is empty
#   'bad-host' logged, but the hostname has an escape sequence and a space
#
# It also notes, for the fixture's clock, when its address is about to become
# available (.cloudflared-<PID>.ready next to it): just before, never after,
# so a time measured from it can only come out longer. When it started is
# the kernel's to say (process_start).
#
# 'stubborn' is not modelled on cloudflared: it ignores everything a process
# can ignore, to exercise serve's last resort.
CLOUDFLARED_STUB = """#!{python}
import json, os, signal, sys, threading, time
from http.server import HTTPServer, BaseHTTPRequestHandler
MODE = {mode!r}
ADDRESS = {address!r}
LIFETIME = {lifetime!r}
HOST = 'stub-%d.trycloudflare.com' % os.getpid()
NOTE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.cloudflared-%d.' % os.getpid())
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def note(what):
    with open(NOTE + what, 'w') as f:
        f.write(repr(time.time()))

ANSWERS = {{
    'not-json': b'<html><body>metrics</body></html>',
    'empty': b'{{"hostname":""}}',
    'bad-host': b'{{"hostname":"stub\\x1b[31m x.trycloudflare.com"}}',
}}

class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/quicktunnel' or ADDRESS == '404':
            self.send_response(404)
            self.end_headers()
            return
        body = ANSWERS.get(ADDRESS) or json.dumps({{'hostname': HOST}}, separators=(',', ':')).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body + b'\\n')
    def log_message(self, *args):
        pass

def open_metrics():
    for port in (20241, 20242, 20243, 20244, 20245, 0):
        try:
            server = HTTPServer(('127.0.0.1', port), Metrics)
        except OSError:
            continue
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return

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
if ADDRESS == 'late':
    time.sleep(3)
if ADDRESS != 'never':
    note('ready')
    log('INF |  https://%s  |' % HOST)
    open_metrics()
time.sleep(1)
log('INF Registered tunnel connection connIndex=0')
time.sleep(LIFETIME)
"""


def write_shims(directory, ledger, caddy='real', lsof='real', scutil='real',
                cloudflared='idle', address='ok', missing=(), lifetime=STUB_LIFETIME):
    """One shim per helper (ps and curl included) in `directory`.
    `cloudflared` is a stub mode ('idle', 'draining', 'stubborn') or 'real',
    `address` how the stub reports its tunnel address (see above); names in
    `missing` get no shim, so serve cannot find them."""
    os.makedirs(directory, exist_ok=True)
    actions = {'caddy': CADDY[caddy](), 'lsof': LSOF[lsof](), 'scutil': SCUTIL[scutil](),
               'ps': exec_real(REAL_PS), 'curl': exec_real(REAL_CURL)}
    if cloudflared == 'real':
        actions['cloudflared'] = exec_real(REAL_CLOUDFLARED)
    else:
        stub = os.path.join(directory, '.cloudflared-stub')
        with open(stub, 'w') as f:
            f.write(CLOUDFLARED_STUB.format(python=PYTHON, mode=cloudflared, address=address,
                                            lifetime=lifetime))
        os.chmod(stub, 0o755)
        actions['cloudflared'] = exec_real(stub)
    for name, action in actions.items():
        if name in missing:
            continue
        path = os.path.join(directory, name)
        with open(path, 'w') as f:
            f.write(SHIM.format(name=name, ledger=q(ledger), action=action.replace('"$LEDGER"', q(ledger))))
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
        register_session(self.sid)
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


def stub_notes(directory):
    """What the cloudflared stand-ins in `directory` noted: PID -> {'ready':
    time}, in seconds since the epoch."""
    out = {}
    for name in os.listdir(directory):
        m = re.fullmatch(r'\.cloudflared-(\d+)\.(ready)', name)
        if m:
            try:
                with open(os.path.join(directory, name)) as f:
                    out.setdefault(int(m.group(1)), {})[m.group(2)] = float(f.read())
            except (OSError, ValueError):
                pass              # still being written
    return out


def start_download(url):
    """A throttled download that stays in flight for minutes."""
    return subprocess.Popen(['/usr/bin/curl', '-s', '-o', '/dev/null', '--limit-rate', '200k', url],
                            stdin=subprocess.DEVNULL, start_new_session=True)
