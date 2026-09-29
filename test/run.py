#!/usr/bin/env python3
"""Run every case in spec.py against the real serve and report.

The test plan (PLAN.md) is what the cases check; before anything runs, every
plan item must be cited by at least one case and every cited item must exist
in the plan, or the run stops there.

Each case gets a fresh terminal session, its own site directory, its own
ledger and shims, and a decoy: a process started from the same shell, in
the same session but outside serve's process group, that ignores SIGHUP.
Besides what spec.py expects of the case, every case must end with:

  - no recorded process (ledger) still running;
  - nothing left in the session but its shell(s) and the decoy;
  - the decoy still alive (nothing outside serve's group was signalled);

and, throughout the run, a bystander serve started before any case must
keep serving untouched. A case the README says leaves processes behind
(known limitations) must leave exactly serve's own group behind, and the README's
remedy must clear it.

    python3 test/run.py                  # everything, cloudflared stubbed
    python3 test/run.py -k lsof -k pretrap
    python3 test/run.py --list
    python3 test/run.py --real-tunnel    # the few real-tunnel cases only
"""
import argparse
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx          # noqa: E402
import spec                   # noqa: E402

KEYS = {'^C': b'\x03', '^\\': b'\x1c', '^Z': b'\x1a'}
PLAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'PLAN.md')


def plan_coverage():
    """Problems between PLAN.md and spec.py: plan items no case cites, and
    cited items the plan does not have. Items are the `- **ID**` entries."""
    with open(PLAN) as f:
        items = set(re.findall(r'^- \*\*([A-Z]+-\d+)\b', f.read(), re.M))
    cited = {i for c in spec.all_cases() + spec.all_cases(real_tunnel=True) for i in c.plan}
    problems = [f'plan item {i} is not checked by any case' for i in sorted(items - cited)]
    problems += [f'cases cite {i}, which is not in the plan' for i in sorted(cited - items)]
    return problems
MARGIN = 1.0                  # added to upper time bounds; raised with -j


class Failed(Exception):
    pass


# ---------------------------------------------------------------- one case

class World:
    """Everything one case runs in."""

    def __init__(self, case, root, big):
        self.case = case
        self.dir = tempfile.mkdtemp(prefix='case_', dir=root)
        self.site = os.path.join(self.dir, 'site')
        os.makedirs(self.site)
        self.content = f'content for {case.id}'
        with open(os.path.join(self.site, 'who.txt'), 'w') as f:
            f.write(self.content)
        with open(os.path.join(self.site, '.hidden'), 'w') as f:
            f.write('dotfile')
        os.link(big, os.path.join(self.site, 'big.bin'))
        self.shims = os.path.join(self.dir, 'bin')
        self.ledger = fx.Ledger(os.path.join(self.dir, 'ledger'))
        shims = dict(case.shims)
        missing = shims.pop('missing', ())
        fx.write_shims(self.shims, self.ledger.path, missing=missing, **shims)
        self.failures = []
        self.downloads = []
        self.term = None
        self.serve = None           # (pid, lstart) once known
        self.sids = []

    # -- setup / teardown

    def open_terminal(self, nested=False):
        t = fx.Terminal()
        self.sids.append(t.sid)
        mark = t.mark()
        t.run("(trap '' HUP; exec /bin/sleep 3600) & print DECOY=$!")
        m = t.expect(r'DECOY=(\d+)', 5, mark)
        if not m:
            raise Failed('decoy never started')
        t.decoy = int(m.group(1))
        if nested:
            t.start_nested()
        mark = t.mark()
        t.run(f'cd {fx.q(self.site)}')
        t.expect(r'[ON]\$ $', 5, mark)
        return t

    def teardown(self):
        for d in self.downloads:
            try:
                d.kill()
                d.wait()
            except OSError:
                pass
        for t in list(getattr(self, 'terms', [])) or ([self.term] if self.term else []):
            t.destroy()
        shutil.rmtree(self.dir, ignore_errors=True)

    # -- checks

    def check(self, ok, what):
        if ok is not True:
            self.failures.append(what if ok in (False, None) else f'{what}: {ok}')
        return ok is True

    # -- serve

    def launch(self, t, args=None, pipe=None):
        """Start serve. The same command line has the shell report, the
        moment serve's pipeline ends, the time and every exit status: the
        shell collects serve at once, so this does not depend on how often
        the fixture looks. (A job killed by SIGINT makes the shell drop the
        rest of the line; query_exit then asks separately.)"""
        args = self.case.args if args is None else args
        pipe = self.case.pipe if pipe is None else pipe
        pipe = pipe.replace('{tee}', fx.q(os.path.join(self.dir, 'tee.log')))
        self.mark = t.mark()
        self.t0 = time.time()
        t.run(f'PATH={fx.q(self.shims)} {fx.q(fx.SERVE)} {args}{pipe}; '
              'print "END=$EPOCHREALTIME EXIT=${pipestatus[1]}:${pipestatus[2]}"')

    def end_report(self, t, timeout=0.0, since=None):
        m = t.expect(r'END=([\d.]+) EXIT=(\d+):(\d*)', timeout, self.mark if since is None else since)
        return (float(m.group(1)), int(m.group(2)), m.group(3)) if m else None

    def query_exit(self, t, timeout=10, since=None):
        """(status of serve, status of the next command in its pipeline).
        Only a report after `since` counts: a job that was suspended has
        already had the rest of its line run, with the suspension's status."""
        r = self.end_report(t, min(timeout, 3.0), since)
        if r:
            return r[1], r[2]
        mark = t.mark()
        t.run('print "STATUS=${pipestatus[1]}:${pipestatus[2]}"')
        m = t.expect(r'STATUS=(\d+):(\d*)', timeout, mark)
        return (int(m.group(1)), m.group(2)) if m else None

    def serve_identity(self, timeout=5):
        """serve's PID, from the first thing it ran: scutil, run in a command
        substitution, has serve itself as its parent."""
        if self.serve:
            return self.serve
        found = []

        def look():
            e = self.ledger.first('scutil')
            if e:
                p = fx.proc_table().get(e.ppid)
                if p:
                    found.append((e.ppid, p.lstart))
                    return True
            return False
        fx.wait_until(look, timeout)
        self.serve = found[0] if found else None
        return self.serve

    def serve_alive(self, table=None):
        if not self.serve:
            return False
        table = fx.proc_table() if table is None else table
        p = table.get(self.serve[0])
        return p is not None and p.lstart == self.serve[1]

    def wait_serve_gone(self, timeout):
        """Wait for serve to end, keeping a timeline (every 0.5 s: which
        recorded processes still run, and in what state) for the report if
        it takes too long."""
        self.timeline = []
        start = [time.time()]

        def gone():
            now = time.time()
            if now - start[0] >= 0.5 * len(self.timeline):
                t_ps = time.time()
                table = fx.proc_table()
                t_ps = time.time() - t_ps
                if t_ps > 0.3:
                    self.timeline.append(f'(ps took {t_ps:.1f}s)')
                alive = [f'{e.name}:{table[e.pid].stat}' for e in self.ledger.entries()
                         if e.name in ('caddy', 'cloudflared') and fx.running(e, table)]
                if self.serve_alive(table):
                    alive.insert(0, f'serve:{table[self.serve[0]].stat}')
                self.timeline.append(f'{now - start[0]:.1f}s ' + (' '.join(alive) or '-'))
            return not self.serve_alive()
        fx.wait_until(gone, timeout, 0.02)
        return not self.serve_alive()

    def leftovers(self, table=None):
        table = fx.proc_table() if table is None else table
        keep = set()
        for t in self.all_terms():
            keep |= {t.pid, t.decoy, t.nested or -1}
        out = {}
        for sid in self.sids:
            out.update({pid: p for pid, p in fx.session_members(sid, table).items() if pid not in keep})
        return out

    def describe_states(self, procs):
        cmds = fx.commands(procs)
        return ', '.join(f'{cmds.get(pid, "?")[:20]}:{p.stat}' for pid, p in procs.items())

    def all_terms(self):
        return getattr(self, 'terms', None) or ([self.term] if self.term else [])

    def ledger_running(self, table=None):
        table = fx.proc_table() if table is None else table
        return [e for e in self.ledger.entries() if fx.running(e, table)]

    def wait_clean(self, timeout=3.0):
        fx.wait_until(lambda: not self.leftovers() and not self.ledger_running(), timeout, 0.1)
        return not self.leftovers() and not self.ledger_running()

    def describe(self, procs):
        cmds = fx.commands(procs)
        return ', '.join(f'{pid} {cmds.get(pid, "?")[:60]}' for pid in procs) or 'none'

    # -- business (README: output, startup steps 4-6, security, process boundary)

    def business(self, t, stdout_tty=True, host=None, via_tunnel=False):
        """Everything the README says holds while serve is up; returns the port."""
        text = t.text(self.mark)
        raw = t.raw[self.mark:]
        m = re.search(r'Local: (http://(\S+?):(\d+)/)(.*)', text)
        if not self.check(bool(m), 'Local: line printed'):
            return None
        url, host_seen, port, rest = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        want_host = host or (f'{fx.HOST}.local' if fx.HOST else 'localhost')
        self.check(host_seen == want_host or f'{host_seen} (want {want_host})', 'Local: host')
        if want_host == 'localhost':
            self.check('LocalHostName is not set' in rest, 'Local: says why there is no .local name')
        self.check(re.search(r'(?m)^Serving: ' + re.escape(self.site) + r'$', text) is not None
                   or [l for l in text.splitlines() if 'Serving' in l], 'Serving: names the directory')
        self.check((fx.RED + b'Serving: ' in raw) == stdout_tty,
                   'Serving: in bold red exactly when stdout is a terminal')
        sp = self.serve_identity()
        table = fx.proc_table()
        self.check(sp is not None and table.get(sp[0]) is not None and table[sp[0]].pgid == sp[0],
                   'serve leads its own process group')
        caddy = self.ledger.entries('caddy')
        self.check(len(caddy) == 1 or f'{len(caddy)} launched', 'caddy launched once')
        if caddy:
            c = caddy[0]
            self.check(c.args == 'file-server --browse --listen :0' or c.args, 'caddy arguments')
            self.check(c.pgid == sp[0] if sp else False, 'caddy in serve\'s group')
            self.check(fx.session_of(c.pid) == t.sid, 'caddy in the terminal\'s session')
            socks = fx.listening(c.pid)
            self.check(socks == [f'*:{port}'] or socks, 'caddy listens on one socket, all interfaces, printed port')
        for name, base in (('127.0.0.1', f'http://127.0.0.1:{port}'), ('[::1]', f'http://[::1]:{port}'),
                           (want_host, f'http://{want_host}:{port}')):
            got = fx.http_get(f'{base}/who.txt')
            self.check(got == self.content.encode() or got[:80], f'who.txt over {name}')
        listing = fx.http_get(f'http://127.0.0.1:{port}/')
        self.check(b'who.txt' in listing and b'.hidden' in listing, 'directory listing shows files, dotfiles too')
        cf = self.ledger.entries('cloudflared')
        if self.case.mode == 'share':
            self.check(len(cf) == 1 or f'{len(cf)} launched', 'cloudflared launched once')
            if cf:
                self.check(cf[0].args == f'tunnel --url http://localhost:{port}' or cf[0].args,
                           'cloudflared tunnel --url http://localhost:PORT')
                self.check(cf[0].pgid == sp[0] if sp else False, 'cloudflared in serve\'s group')
            if self.case.shims.get('cloudflared') == 'real':
                u = t.expect(r'https://[a-z0-9-]+\.trycloudflare\.com', 40, self.mark)
                self.check(bool(u), 'tunnel URL in cloudflared\'s log')
                self.tunnel = u.group(0) if u else None
                if u:
                    ok = fx.wait_until(lambda: fx.http_get(f'{self.tunnel}/who.txt', 10) == self.content.encode(),
                                       90, 1)
                    self.check(ok, 'tunnel serves the directory')
        else:
            self.check(not cf or f'{len(cf)} launched', 'no cloudflared without --share')
        return port

    # -- events

    def fire(self, t, event):
        kind = event[0]
        sp = self.serve_identity()
        if kind == 'key':
            t.send(KEYS[event[1]])
        elif kind == 'signal':
            os.kill(sp[0], spec.signum(event[1]))
        elif kind == 'group':
            os.killpg(sp[0], spec.signum(event[1]))
        elif kind == 'hangup':
            t.hang_up()
        elif kind == 'kill-parent':
            os.kill(t.nested or t.pid, signal.SIGKILL)
            if t.nested:
                t.nested_dead = True
        elif kind == 'kill':
            e = self.ledger.first(event[1])
            os.kill(e.pid, signal.SIGKILL)
        else:
            raise ValueError(event)


# -------------------------------------------------------------- lifecycle

def run_lifecycle(w):
    c, e = w.case, w.case.expect
    t = w.term = w.open_terminal(nested=c.nested)
    stdout_tty = e.get('stdout_tty', True)
    w.launch(t)

    # ---- bring serve to the phase the event belongs to
    port = None
    if c.phase == 'pretrap':
        fx.wait_until(lambda: w.ledger.first('scutil'), 5)
        w.serve_identity()
    elif c.phase == 'startup':
        fx.wait_until(lambda: w.ledger.first('caddy'), 5)
        w.serve_identity()
    elif c.phase in ('running', 'cleanup'):
        if not t.expect(r'Local: http://', 15, w.mark):
            raise Failed('serve never finished starting')
        if c.mode == 'share':
            fx.wait_until(lambda: w.ledger.first('cloudflared'), 5)
        port = w.business(t, stdout_tty=stdout_tty, host=e.get('host'), via_tunnel=e.get('via_tunnel'))
        if e.get('tee'):
            peers = [p for p in fx.session_members(t.sid, fx.proc_table(commands=True)).values()
                     if p.command.startswith('tee ')]
            sp = w.serve_identity()
            w.check(bool(peers) and all(p.pgid == sp[0] for p in peers), 'tee is in serve\'s process group')
        if c.behaviour == 'draining':
            base = getattr(w, 'tunnel', None) if e.get('via_tunnel') else f'http://127.0.0.1:{port}'
            w.downloads.append(fx.start_download(f'{base}/big.bin'))
            fx.sleep(3.0 if e.get('via_tunnel') else 1.0)
            w.check(w.downloads[-1].poll() is None, 'download in flight when the event fires')
        if c.behaviour == 'stubborn':
            fx.wait_until(lambda: 'INF |' in t.text(w.mark), 5)
    else:
        w.serve_identity(timeout=2)

    # ---- the event
    t_event = time.time()
    if c.event:
        w.fire(t, c.event)
    if c.phase == 'cleanup':
        fx.sleep(1.0)
        w.fire(t, c.second)
    if e.get('then') == 'kill-parent':           # ^Z first, then the parent dies
        fx.wait_until(lambda: all('T' in p.stat for p in group_members(w).values()), 3)
        w.check(all('T' in p.stat for p in group_members(w).values()) or
                w.describe_states(group_members(w)), '^Z stops serve and its services')
        t_event = time.time()
        w.fire(t, ('kill-parent',))

    ends = e['ends']
    shell_alive = c.event not in (('hangup',), ('kill-parent',)) and c.second not in (('hangup',), ('kill-parent',)) \
        and e.get('then') != 'kill-parent'

    if ends == 'itself':
        lo, hi = e.get('window', (0, 3))
        t_ref = w.t0 if c.phase == 'none' else t_event
        gone, dt, source = True, None, None
        w.timeline = []
        if w.serve or w.serve_identity(timeout=0.5):
            gone = w.wait_serve_gone(hi + MARGIN + 1)
            dt, source = time.time() - t_ref, 'polled'
            w.check(gone, 'serve ends by itself')
        # the shell's own clock, not the poll's; it also times a serve that
        # ended before any helper ran, whose PID the fixture never learnt
        report = w.end_report(t, hi + MARGIN + 2) if gone and shell_alive else None
        if report:
            dt, source = report[0] - t_ref, 'reported by the shell'
        if gone and dt is not None:
            w.check(lo <= dt <= hi + MARGIN or f'{dt:.2f}s ({source}), want {lo}-{hi}; timeline: '
                    + ' | '.join(w.timeline), 'serve ends in time')
        elif gone:
            w.check(False, 'serve\'s end could be timed')
        if shell_alive:
            got = w.query_exit(t, timeout=hi + MARGIN + 5)
            want = e.get('exit')
            if want is not None:
                ok = got is not None and (got[0] in want if isinstance(want, tuple) else got[0] == want)
                w.check(ok or f'got {got and got[0]}, want {want}', 'exit status')
        w.check(w.wait_clean(3.0) or w.describe(w.leftovers()), 'nothing left behind')

    elif ends == 'continues':
        fx.sleep(e.get('hold', 1.5))
        w.check(w.serve_alive() or w.serve is None, 'serve still running')
        if e.get('printed') is False:
            w.check('Serving:' not in t.text(w.mark), 'nothing printed yet')
        then = e['then']
        if then == 'resume':
            sp = w.serve_identity()
            w.check('T' in fx.proc_table().get(sp[0], fx.Proc(0, 0, 0, '', '', '')).stat, f'{c.event[1]} pauses serve')
            if c.phase == 'running':
                w.check(fx.http_get(f'http://127.0.0.1:{port}/who.txt') == w.content.encode(),
                        'the services keep serving while serve is paused')
            os.kill(sp[0], signal.SIGCONT)
            fx.sleep(0.5)
            w.check('T' not in fx.proc_table().get(sp[0], fx.Proc(0, 0, 0, 'T', '', '')).stat, 'CONT resumes serve')
            t.run('fg')
        elif then == 'fg':
            if c.event == ('key', '^Z'):
                w.check(all('T' in p.stat for p in group_members(w).values()) or
                        w.describe_states(group_members(w)), '^Z stops serve and its services')
            t.run('fg')
        if then in ('resume', 'fg', 'ctrl-c') and c.phase in ('pretrap', 'startup', 'running') \
                and e.get('printed') is not False:
            m = t.expect(r'Local: http://(\S+?):\d+/', 15, w.mark)
            if not m:
                w.check(False, 'serve finishes starting / keeps running')
            elif e.get('host'):
                w.check(m.group(1) == e['host'] or m.group(1), 'Local: host')
            fx.sleep(0.5)
            port = port or local_port(t, w)
            if port:
                w.check(fx.http_get(f'http://127.0.0.1:{port}/who.txt') == w.content.encode(), 'still serving')
        t_stop = time.time()
        stop_mark = t.mark()
        t.send(KEYS['^C'])
        lo, hi = spec.WINDOW[c.behaviour]
        if w.serve:
            w.check(w.wait_serve_gone(hi + MARGIN + 1), 'serve stops on Ctrl-C')
        got = w.query_exit(t, timeout=10, since=stop_mark)
        w.check(got is not None and got[0] == e['exit'] or f'got {got and got[0]}, want {e["exit"]}',
                'exit status after Ctrl-C')
        w.check(w.wait_clean(3.0) or w.describe(w.leftovers()), 'nothing left behind')

    elif ends == 'leaks':
        gone = w.wait_serve_gone(e['window'][1] + MARGIN)
        w.check(gone, 'serve ends')
        if shell_alive:
            got = w.query_exit(t, timeout=5)
            w.check(got is not None and got[0] == e['exit'] or f'got {got and got[0]}, want {e["exit"]}', 'exit status')
        fx.sleep(0.5)
        left = w.leftovers()
        sp = w.serve[0] if w.serve else None
        w.check(bool(left), 'services left running (no cleanup)')
        w.check(all(p.pgid == sp for p in left.values()) or w.describe(left),
                'everything left is in serve\'s original group')
        if sp:
            os.killpg(sp, signal.SIGKILL)                    # the README's remedy
        w.check(w.wait_clean(3.0) or w.describe(w.leftovers()), 'kill -KILL -<PGID> clears it')

    # ---- facts about what did or did not happen
    text = t.text(w.mark)
    if e.get('printed') is not None and ends != 'continues':
        w.check(('Serving:' in text) == e['printed'], 'Serving: printed' if e['printed'] else 'nothing printed')
    if e.get('cf') is False:
        w.check(not w.ledger.entries('cloudflared'), 'cloudflared never started')
    if e.get('nothing_ran'):
        w.check(not w.ledger.entries(), 'nothing ran at all')
    if e.get('nothing_started'):
        w.check(not w.ledger.entries('caddy') and not w.ledger.entries('cloudflared'), 'no service started')
    for msg in e.get('messages', ()):
        w.check(msg in text, f'message "{msg}"')
    if e.get('tee'):
        log = open(os.path.join(w.dir, 'tee.log'), errors='replace').read()
        w.check(f'Serving: {w.site}' in log and '\x1b[' not in log.split('Serving:')[1][:200],
                'tee got the plain Serving: line')


def group_members(w):
    sp = w.serve_identity()
    return {pid: p for pid, p in fx.proc_table().items() if sp and p.pgid == sp[0]}


def local_port(t, w):
    m = re.search(r'Local: http://\S+?:(\d+)/', t.text(w.mark))
    return int(m.group(1)) if m else None


# ----------------------------------------------------------- other kinds

def run_args(w):
    e = w.case.expect
    t = w.term = w.open_terminal()
    w.launch(t)
    got = w.query_exit(t)
    w.check(got is not None and got[0] == e['exit'] or f'got {got}', 'exit status')
    w.check(e['text'] in t.text(w.mark), f'prints "{e["text"].strip()}"')
    w.check(not w.ledger.entries(), 'nothing ran')
    # which stream: silence the other one and look again
    hide = '2>/dev/null' if e['stream'] == 'stdout' else '>/dev/null'
    w.launch(t, pipe=f' {hide}')
    w.query_exit(t)
    w.check(e['text'].strip() in t.text(w.mark), f'written to {e["stream"]}')
    w.check(w.wait_clean(1.0) or w.describe(w.leftovers()), 'nothing left behind')


def run_leader(w):
    e, how = w.case.expect, w.case.expect['how']
    env = dict(os.environ, PATH=w.shims)
    if how == 'pipeline':
        t = w.term = w.open_terminal()
        w.mark = t.mark()
        t.run(f'true | PATH={fx.q(w.shims)} {fx.q(fx.SERVE)}; print "STATUS=${{pipestatus[1]}}:${{pipestatus[2]}}"')
        m = t.expect(r'STATUS=(\d+):(\d+)', 10, w.mark)
        w.check(m is not None and int(m.group(2)) == e['exit'] or f'got {m and m.group(0)}', 'exit status')
        text = t.text(w.mark)
    else:
        # 'script': serve is not the last command, so zsh cannot exec it;
        # 'exec': it is, so zsh replaces itself with serve
        script = f'cd {fx.q(w.site)}; {fx.q(fx.SERVE)}' + ('; exit $?' if how == 'script' else '')
        p = subprocess.Popen(['/bin/zsh', '-fc', script], env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        w.sids.append(p.pid)
        if how == 'exec':
            ok = fx.wait_until(lambda: w.ledger.first('caddy'), 10)
            w.check(ok, 'serve starts')
            w.serve_identity()
            w.check(w.serve is not None and w.serve[0] == p.pid, 'serve took over the script\'s process')
            port = None
            if fx.wait_until(lambda: fx.listening(w.ledger.first('caddy').pid), 10):
                port = int(fx.listening(w.ledger.first('caddy').pid)[0].rsplit(':', 1)[1])
            w.check(port and fx.http_get(f'http://127.0.0.1:{port}/who.txt') == w.content.encode(), 'serves')
            os.kill(p.pid, signal.SIGTERM)
        try:
            text = p.communicate(timeout=15)[0].decode(errors='replace')
        except subprocess.TimeoutExpired:
            p.kill()
            text = p.communicate()[0].decode(errors='replace')
        w.check(p.returncode == e['exit'] or f'got {p.returncode}', 'exit status')
    for msg in e.get('messages', ()):
        w.check(msg in text, f'message "{msg}"')
    if how != 'exec':
        w.check(not w.ledger.entries('caddy'), 'no service started')
    w.check(w.wait_clean(3.0) or w.describe(w.leftovers()), 'nothing left behind')


def run_concurrency(w):
    w.terms = [w.open_terminal() for _ in range(3)]
    ledgers, ports = [], []
    for i, t in enumerate(w.terms):
        t.mark0 = t.mark()
        t.run(f'PATH={fx.q(w.shims)} {fx.q(fx.SERVE)} {w.case.args}')
    for t in w.terms:
        m = t.expect(r'Local: http://\S+?:(\d+)/', 15, t.mark0)
        ports.append(int(m.group(1)) if m else None)
    w.check(None not in ports and len(set(ports)) == 3 or str(ports), 'three instances, three ports')
    stopped = w.terms[1]
    q = stopped.mark()
    stopped.send(KEYS['^C'])
    fx.sleep(1.0)
    stopped.run('print "EXIT=${pipestatus[1]}"')
    m = stopped.expect(r'EXIT=(\d+)', 10, q)
    w.check(m is not None and m.group(1) == '0', 'the stopped one exits 0')
    for i in (0, 2):
        w.check(fx.http_get(f'http://127.0.0.1:{ports[i]}/who.txt') == w.content.encode(),
                f'instance {i + 1} keeps serving')
    w.check(fx.http_get(f'http://127.0.0.1:{ports[1]}/who.txt').startswith(b'ERROR'), 'the stopped one is gone')
    for i in (0, 2):
        w.terms[i].send(KEYS['^C'])
    w.check(w.wait_clean(5.0) or w.describe(w.leftovers()), 'nothing left behind')
    w.check(all(fx.proc_table().get(t.decoy) for t in w.terms), 'decoys untouched')


def run_streams(w):
    t = w.term = w.open_terminal()
    w.launch(t, pipe=' 2>/dev/null')
    ok = t.expect(r'Local: http://', 15, w.mark)
    w.check(bool(ok), 'Serving:/Local: on stdout')
    fx.sleep(1.5)
    quiet = t.text(w.mark)
    # past the echo of the command line itself, stdout holds exactly two lines
    lines = [l for l in quiet.splitlines()[1:] if l.strip()]
    w.check(len(lines) == 2 and lines[0] == f'Serving: {w.site}' and re.match(r'Local: http://\S+/$', lines[1])
            is not None or lines, 'stdout holds exactly the Serving: and Local: lines')
    t.send(KEYS['^C'])
    w.wait_serve_gone(5)
    w.serve = None
    w.ledger = fx.Ledger(w.ledger.path)
    fx.sleep(0.5)
    w.launch(t)
    t.expect(r'Local: http://', 15, w.mark)
    fx.sleep(1.5)
    loud = t.text(w.mark)
    w.check('server running' in loud, 'caddy\'s log on the terminal')
    if w.case.mode == 'share':
        w.check(re.search(r'INF \|  https://\S+\.trycloudflare\.com  \|', loud) is not None,
                'tunnel URL in cloudflared\'s log')
    t.send(KEYS['^C'])
    w.check(w.wait_clean(5.0) or w.describe(w.leftovers()), 'nothing left behind')


RUNNERS = {'lifecycle': run_lifecycle, 'args': run_args, 'leader': run_leader,
           'concurrency': run_concurrency, 'streams': run_streams}


def run_case(case, root, big):
    w = World(case, root, big)
    t0 = time.time()
    try:
        RUNNERS[case.kind](w)
    except Failed as ex:
        w.failures.append(str(ex))
    except Exception:
        w.failures.append('fixture error: ' + traceback.format_exc(limit=3).strip().splitlines()[-1])
    finally:
        try:
            for t in w.all_terms():
                w.check(fx.proc_table().get(t.decoy) is not None, 'decoy untouched')
        except Exception:
            pass
        w.teardown()
    return {'id': case.id, 'plan': case.plan, 'failures': w.failures,
            'seconds': round(time.time() - t0, 1), 'sids': w.sids}


# ----------------------------------------------------------------- workers

def _init(margin):
    global MARGIN
    MARGIN = margin
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _work(args):
    case, root, big = args
    return run_case(case, root, big)


class Bystander:
    """A serve --share left running for the whole run, in a terminal of its
    own: no case may disturb it."""

    def __init__(self, root, big):
        self.case = spec.Case('bystander', 'PROC-3', mode='share')
        self.w = World(self.case, root, big)
        self.t = self.w.open_terminal()
        self.w.term = self.t
        self.w.launch(self.t)
        if not self.t.expect(r'Local: http://\S+?:(\d+)/', 15, self.w.mark):
            raise SystemExit('bystander serve did not start')
        fx.wait_until(lambda: self.w.ledger.first('cloudflared'), 5)
        self.w.serve_identity()
        self.port = local_port(self.t, self.w)
        self.procs = self.w.ledger.entries('caddy') + self.w.ledger.entries('cloudflared')

    def healthy(self):
        fx.pump_all()
        table = fx.proc_table()
        return (all(fx.running(p, table) for p in self.procs) and self.w.serve_alive(table)
                and fx.http_get(f'http://127.0.0.1:{self.port}/who.txt') == self.w.content.encode())

    def stop(self):
        self.t.send(KEYS['^C'])
        clean = self.w.wait_clean(5.0)
        self.w.teardown()
        return clean


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-k', action='append', default=[], help='only cases whose id contains this (repeatable)')
    ap.add_argument('-j', type=int, default=min(6, os.cpu_count() or 2), help='cases run in parallel')
    ap.add_argument('--list', action='store_true', help='list the cases and exit')
    ap.add_argument('--real-tunnel', action='store_true', help='run the real-tunnel cases instead')
    ap.add_argument('--serve', metavar='PATH', help='test this script instead of ../serve')
    opts = ap.parse_args()
    if opts.serve:
        os.environ['NA_SERVE_UNDER_TEST'] = fx.SERVE = os.path.abspath(opts.serve)

    problems = plan_coverage()
    if problems:
        sys.exit('PLAN.md and spec.py disagree:\n  ' + '\n  '.join(problems))
    cases = [c for c in spec.all_cases(opts.real_tunnel) if not opts.k or any(k in c.id for k in opts.k)]
    if opts.list:
        for c in cases:
            print(f'{c.id}    [{" ".join(c.plan)}]')
        print(f'{len(cases)} cases')
        return
    if not fx.REAL_CADDY:
        sys.exit('caddy not found on PATH (brew install caddy)')
    if opts.real_tunnel and not fx.REAL_CLOUDFLARED:
        sys.exit('cloudflared not found on PATH (brew install cloudflared)')
    margin = MARGIN + 0.25 * max(0, opts.j - 1)

    root = tempfile.mkdtemp(prefix='na_serve_test_')
    big = os.path.join(root, 'big.bin')
    with open(big, 'wb') as f:
        f.write(os.urandom(64 << 20))
    results, bystander_ok = [], True
    started = time.time()
    try:
        bystander = Bystander(root, big)
        ctx = multiprocessing.get_context('spawn')
        # Cases that crash a process on purpose run last and one at a time:
        # the crash reporter they bring in would otherwise hold up whatever
        # else is running, which no real use of serve ever faces
        calm = [c for c in cases if not c.expect.get('crashes')]
        crashing = [c for c in cases if c.expect.get('crashes')]
        for batch, jobs in ((calm, opts.j), (crashing, 1)):
            if not batch:
                continue
            with ctx.Pool(jobs, initializer=_init, initargs=(margin,)) as pool:
                pending = pool.imap_unordered(_work, [(c, root, big) for c in batch])
                while True:
                    try:
                        r = pending.next(timeout=0.2)
                    except multiprocessing.TimeoutError:
                        fx.pump_all()
                        continue
                    except StopIteration:
                        break
                    if not bystander.healthy():
                        bystander_ok = False
                        r['failures'].append('bystander serve disturbed')
                    results.append(r)
                    mark = 'FAIL' if r['failures'] else 'ok  '
                    print(f'[{len(results):3}/{len(cases)}] {mark} {r["id"]}  ({r["seconds"]}s)', flush=True)
                    for f in r['failures']:
                        print(f'             - {f}', flush=True)
        bystander_ok = bystander.stop() and bystander_ok
    finally:
        # Anything still in a session this run created is the fixture's own
        # leftover, whatever the cases concluded
        sids = {sid for r in results for sid in r['sids']}
        stray = {pid: p for pid, p in fx.proc_table().items() if fx.session_of(pid) in sids}
        stray = {pid: fx.commands([pid]).get(pid, '?') for pid in stray}
        for pid in stray:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        shutil.rmtree(root, ignore_errors=True)

    failed = [r for r in results if r['failures']]
    print()
    if any(c.expect.get('crashes') for c in cases):
        print(f'reminder: this run crashed processes on purpose between {time.strftime("%Y-%m-%d %H:%M", time.localtime(started))}'
              f' and {time.strftime("%H:%M")}. macOS may keep adding zsh-/bash-/sh-/sleep-*.ips reports to'
              f' ~/Library/Logs/DiagnosticReports for about half an hour; see test/README.md to clean them up.')
    print(f'{len(results) - len(failed)}/{len(results)} cases passed in {time.time() - started:.0f}s'
          f' (-j {opts.j}); bystander {"untouched" if bystander_ok else "DISTURBED"}')
    if stray:
        print(f'fixture leftovers swept after the run: {len(stray)} ('
              + ', '.join(c[:40] for c in stray.values()) + ')')
    for r in sorted(failed, key=lambda r: r['id']):
        print(f'FAIL {r["id"]}  [{" ".join(r["plan"])}]')
        for f in r['failures']:
            print(f'     - {f}')
    sys.exit(0 if not failed and bystander_ok and not stray else 1)


if __name__ == '__main__':
    main()
