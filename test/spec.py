"""Every case in the test plan (PLAN.md), and what it expects.

The plan, not the README and not serve's code, is where each expectation
comes from: every case cites the plan items (ARG-1, CLEAN-2, PROC-1, ...) it
checks, and run.py refuses to run unless every plan item is cited by some
case and every cited item exists. Where the plan lists things (signals,
events, helpers, arguments), the list is taken whole and crossed with every
mode, phase and child behaviour it applies to.

Terms used below:

mode       'lan' (no arguments) or 'share' (--share)
phase      when the case's event fires:
             'none'     no event; serve ends (or keeps running) by itself
             'pretrap'  during startup step 2, before serve traps anything
             'startup'  during step 5, while serve waits for caddy's port
             'addrwait' (--share only) after startup, while serve waits for
                        the tunnel's address, which never comes
             'running'  after startup, with everything up (and, with
                        --share, the tunnel's address shown)
             'cleanup'  one second into a cleanup started by Ctrl-C
behaviour  how serve's children take being stopped:
             'idle'     nothing in flight: they exit on the first signal
             'draining' a download is in flight (and, with --share, a tunnel
                        request): they only stop at once on a repeat signal
             'stubborn' (--share only) cloudflared ignores every signal
event      ('key', '^C' | '^\\' | '^Z')      typed on the terminal
           ('signal', NAME)                 sent to serve alone
           ('group', NAME)                  sent to serve's whole group
           ('hangup',)                      the terminal is closed
           ('kill-parent',)                 the shell serve was started from
                                            is SIGKILLed (the session leader,
                                            or a nested shell if `nested`)
           ('kill', 'caddy'|'cloudflared')  that service is SIGKILLed
"""
import signal

MODES = ('lan', 'share')
BEHAVIOURS = {'lan': ('idle', 'draining'), 'share': ('idle', 'draining', 'stubborn')}

# The plan's stop signals (section 2), in its order
STOP_SIGNALS = 'INT HUP TERM QUIT PIPE ALRM USR1 USR2 VTALRM PROF XCPU XFSZ ABRT EMT SYS'.split()
# PAUSE-2 and PAUSE-3
PAUSE_SIGNALS = 'TSTP TTIN TTOU STOP'.split()
NO_EFFECT_SIGNALS = 'CHLD WINCH URG IO INFO'.split()
# The plan's fatal signals (section 2): serve does not survive them to clean up
FATAL_SIGNALS = 'KILL SEGV BUS ILL FPE TRAP'.split()

# Signals whose default action dumps core. A process ended by one of them
# makes macOS's crash reporter step in, which holds up other processes'
# bookkeeping for seconds; cases where that happens carry crashes=True, and
# run.py runs them on their own, after the rest. (It changes nothing about
# what they expect.)
CORE_SIGNALS = set('QUIT ILL TRAP ABRT EMT FPE BUS SEGV SYS'.split())

# The plan's stop events (section 2)
STOP_EVENTS = ([('key', '^C'), ('key', '^\\'), ('hangup',)]
               + [('signal', s) for s in STOP_SIGNALS])

# Time bounds, in seconds; PLAN.md section 4 says where each comes from.
# run.py adds the plan's fixed tolerance to every upper bound.
# CLEAN-1..3: from the stop event to serve's exit, by how the services take
# being stopped
WINDOW = {'idle': (0.0, 1.0), 'draining': (0.0, 2.0), 'stubborn': (5.0, 6.5)}
CLEAN = {'idle': 'CLEAN-1', 'draining': 'CLEAN-2', 'stubborn': 'CLEAN-3'}
PORT_WAIT = (10.0, 11.5)   # PORT-1, from serve's start
QUICK = (0.0, 0.5)         # DEP-1, PRE-1: nothing to wait for
PORT_GONE = (0.0, 2.0)     # PORT-2
PIPE_BOTH = (0.0, 2.5)     # PIPE-2, PIPE-3, from serve's start
PIPE_SERVICE = (0.0, 3.5)  # PIPE-4, from serve's start
PUBLIC = 1.0               # PUB-1, from the tunnel's address becoming available
PUBLIC_GIVE_UP = (30.0, 31.5)  # PUB-3, from cloudflared's start
PIPE_PUBLIC = (0.0, 2.0)   # PIPE-1 with --share, from the address becoming available
LEFTOVER = 1.0             # PROC-1, PROC-2: how long the last processes may take


def signum(name):
    return int(getattr(signal, 'SIG' + name))


def event_id(event):
    return ':'.join(str(x) for x in event)


# Checked by run.py in every running-phase lifecycle case, before its event
BUSINESS = ('OUT-2', 'OUT-3', 'NET-1', 'NET-2', 'NET-3')


class Case:
    def __init__(self, id, plan, kind='lifecycle', mode='lan', args=None, shims=None,
                 behaviour='idle', pipe='', nested=False, phase='running', event=None,
                 second=None, **expect):
        self.id = id
        # the plan items this case checks: its own, plus those run.py checks
        # in every case of its kind (PLAN.md section 6)
        plan = [plan] if isinstance(plan, str) else list(plan)
        plan.append('PROC-2' if expect.get('ends') == 'leaks' else 'PROC-1')
        plan.append('PROC-3')
        if kind == 'lifecycle' and phase in ('running', 'cleanup'):
            plan.extend(BUSINESS + (('TUN-1', 'PUB-1') if mode == 'share' else ()))
        self.plan = tuple(dict.fromkeys(plan))
        self.kind = kind
        self.mode = mode
        self.args = ('--share' if mode == 'share' else '') if args is None else args
        self.shims = dict(shims or {})
        if mode == 'share' and 'cloudflared' not in self.shims:
            self.shims['cloudflared'] = 'stubborn' if behaviour == 'stubborn' else \
                                        'draining' if behaviour == 'draining' else 'idle'
        self.behaviour = behaviour
        self.pipe = pipe
        self.nested = nested
        self.phase = phase
        self.event = event
        self.second = second          # the event fired one second into cleanup
        self.expect = expect

    def __repr__(self):
        return self.id


def normal_exit(behaviour):
    """README cleanup / exit codes: a stop ends in the table's code, unless something is
    still running at about 5 s and the whole group is SIGKILLed (137)."""
    return 137 if behaviour == 'stubborn' else 0


def observable(event, nested=False):
    """Whether any shell survives to report serve's exit status."""
    return event not in (('hangup',), ('kill-parent',))


# ------------------------------------------------------------------ arguments

def argument_cases():
    yield Case('args/--help', 'ARG-1', kind='args', args='--help', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/-h', 'ARG-1', kind='args', args='-h', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/--help --x', ('ARG-1', 'ARG-3'), kind='args', args='--help --x', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/--x --help', ('ARG-2', 'ARG-3'), kind='args', args='--x --help', exit=2, stream='stderr', text='serve: unknown option: --x')
    yield Case('args/--shar', 'ARG-2', kind='args', args='--shar', exit=2, stream='stderr', text='serve: unknown option: --shar')
    yield Case('args/share', 'ARG-2', kind='args', args='share', exit=2, stream='stderr', text='serve: unknown option: share')
    yield Case('args/--share --x', 'ARG-2', kind='args', args='--share --x', exit=2, stream='stderr', text='serve: unknown option: --x')
    yield Case("args/'a\\tb' (printed as is)", 'ARG-2', kind='args', args="'a\\tb'", exit=2, stream='stderr', text='serve: unknown option: a\\tb')
    yield Case("args/'' (empty)", 'ARG-2', kind='args', args="''", exit=2, stream='stderr', text='serve: unknown option: \n')
    # --share may be repeated; without it cloudflared need not be installed
    yield Case('args/--share --share', ('ARG-4', 'STOP-1', 'CLEAN-1'), mode='share', args='--share --share',
               phase='running', event=('key', '^C'), ends='itself', exit=0)
    yield Case('args/no cloudflared installed, no --share', ('ARG-5', 'STOP-1', 'CLEAN-1'), mode='lan', shims={'missing': ('cloudflared',)},
               phase='running', event=('key', '^C'), ends='itself', exit=0)
    # DEP-1 only asks for curl with --share
    yield Case('args/no curl installed, no --share', ('DEP-1', 'STOP-1', 'CLEAN-1'), mode='lan', shims={'missing': ('curl',)},
               phase='running', event=('key', '^C'), ends='itself', exit=0)


# -------------------------------------------------------------------- startup

def dependency_cases():
    for mode in MODES:
        needed = ('caddy', 'cloudflared', 'curl', 'scutil', 'lsof', 'ps') if mode == 'share' else ('caddy', 'scutil', 'lsof', 'ps')
        for cmd in needed:
            yield Case(f'startup/{mode}/missing {cmd}', 'DEP-1', mode=mode, shims={'missing': (cmd,)},
                       phase='none', ends='itself', exit=1, window=QUICK, messages=(f'serve: {cmd} not found',),
                       printed=False, nothing_ran=True)


def hostname_cases():
    for mode in MODES:
        yield Case(f'startup/{mode}/LocalHostName not set', ('OUT-3', 'STOP-1', 'CLEAN-1'), mode=mode,
                   shims={'scutil': 'fails'}, phase='running', event=('key', '^C'), ends='itself', exit=0,
                   host='localhost')


def leader_cases():
    yield Case('startup/not group leader: true | serve', 'LEAD-1', kind='leader', how='pipeline',
               exit=1, messages=('serve: not a process group leader, refusing to start',))
    yield Case('startup/not group leader: plain command in a script', 'LEAD-1', kind='leader', how='script',
               exit=1, messages=('serve: not a process group leader, refusing to start',))
    yield Case('startup/group leader via exec in a script', 'LEAD-2', kind='leader', how='exec', exit=0)


def port_cases():
    for mode in MODES:
        yield Case(f'startup/{mode}/caddy exits at once', 'PORT-2', mode=mode, shims={'caddy': 'exits'},
                   phase='none', ends='itself', exit=1, window=PORT_GONE, messages=('serve: caddy failed to start',),
                   printed=False, cf=False)
        yield Case(f'startup/{mode}/caddy never listens', 'PORT-1', mode=mode, shims={'caddy': 'never-listens'},
                   phase='none', ends='itself', exit=1, window=PORT_WAIT,
                   messages=('serve: could not read the port caddy is listening on',), printed=False, cf=False)
        for lsof in ('not-a-number', 'zero', 'too-big', 'way-too-big', 'says-nothing', 'fails'):
            yield Case(f'startup/{mode}/lsof {lsof}', 'PORT-1', mode=mode, shims={'lsof': lsof},
                       phase='none', ends='itself', exit=1, window=PORT_WAIT,
                       messages=('serve: could not read the port caddy is listening on',), printed=False, cf=False)
        # a stop signal while waiting for the port: straight to cleanup, nothing
        # printed, no cloudflared, exit 0
        for event in STOP_EVENTS + [('kill-parent',)]:
            # closing the terminal or killing the session leader delivers a
            # SIGHUP, which is one of the stop signals
            yield Case(f'startup/{mode}/{event_id(event)} while waiting for the port', 'PORT-3', mode=mode,
                       shims={'caddy': 'slow'}, phase='startup', event=event, ends='itself',
                       exit=0 if observable(event) else None, window=WINDOW['idle'], printed=False, cf=False)


def pretrap_cases():
    for mode in MODES:
        for event in STOP_EVENTS:
            name = {'^C': 'INT', '^\\': 'QUIT'}.get(event[1]) if event[0] == 'key' else \
                'HUP' if event == ('hangup',) else event[1]
            # before the trap, serve dies of a core signal sent to it; Ctrl-\
            # also reaches the scutil it waits on, which does
            crashes = name in CORE_SIGNALS and (event[0] == 'key' or name != 'QUIT')
            # scutil never finishes by itself here: if serve does not end it,
            # PROC-1 sees it left behind, however long the case waits
            yield Case(f'pretrap/{mode}/{event_id(event)}', 'PRE-1', mode=mode, shims={'scutil': 'hangs'},
                       phase='pretrap', event=event, ends='itself',
                       exit=128 + signum(name) if observable(event) else None, window=QUICK,
                       printed=False, nothing_started=True, crashes=crashes)


# ------------------------------------------------------------ when serve stops

def running_cases():
    for mode in MODES:
        for b in BEHAVIOURS[mode]:
            events = STOP_EVENTS + [('group', s) for s in ('TERM', 'INT', 'HUP', 'QUIT')]
            for event in events:
                stop = 'STOP-2' if event[0] == 'group' else 'STOP-1'
                yield Case(f'running/{mode}/{b}/{event_id(event)}', (stop, CLEAN[b]), mode=mode, behaviour=b,
                           phase='running', event=event, ends='itself',
                           exit=normal_exit(b) if observable(event) else None, window=WINDOW[b])
            for nested in (False, True):
                lo, hi = WINDOW[b]
                yield Case(f'running/{mode}/{b}/kill-parent ({"nested shell" if nested else "session leader"})',
                           ('STOP-3', CLEAN[b]), mode=mode, behaviour=b, nested=nested, phase='running',
                           event=('kill-parent',), ends='itself', exit=None, window=(lo, hi))
            services = ('caddy', 'cloudflared') if mode == 'share' else ('caddy',)
            for svc in services:
                # the stubborn one is cloudflared: kill it and nothing is left to wait for
                left = 'idle' if (b == 'stubborn' and svc == 'cloudflared') else b
                yield Case(f'running/{mode}/{b}/{svc} exits', ('STOP-5', CLEAN[left]), mode=mode, behaviour=b,
                           phase='running', event=('kill', svc), ends='itself',
                           exit=137 if left == 'stubborn' else 1, window=WINDOW[left],
                           messages=(f'serve: {svc} exited',))
        # STOP-4: a parent gone during startup is a stop event like any other
        yield Case(f'startup/{mode}/kill-parent (nested shell) while waiting for the port',
                   'STOP-4', mode=mode, shims={'caddy': 'slow'}, nested=True,
                   phase='startup', event=('kill-parent',), ends='itself', exit=None,
                   window=WINDOW['idle'], printed=False, cf=False)


def other_signal_cases():
    for mode in MODES:
        for phase in ('startup', 'running'):
            shims = {'caddy': 'slow'} if phase == 'startup' else {}
            for s in PAUSE_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} pauses serve', 'PAUSE-2', mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='continues', then='resume', exit=0)
            for s in NO_EFFECT_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} has no effect', 'PAUSE-3', mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='continues', then='ctrl-c', exit=0)


def ctrl_z_cases():
    for mode in MODES:
        yield Case(f'startup/{mode}/^Z then fg', 'PAUSE-1', mode=mode, shims={'caddy': 'slow'},
                   phase='startup', event=('key', '^Z'), ends='continues', then='fg', exit=0)
        for b in BEHAVIOURS[mode]:
            yield Case(f'running/{mode}/{b}/^Z then fg', ('PAUSE-1', CLEAN[b]), mode=mode, behaviour=b,
                       phase='running', event=('key', '^Z'), ends='continues', then='fg', exit=normal_exit(b))
            for nested in (False, True):
                yield Case(f'running/{mode}/{b}/^Z then kill-parent ({"nested shell" if nested else "session leader"})',
                           ('PAUSE-1', CLEAN[b]), mode=mode, behaviour=b, nested=nested, phase='running',
                           event=('key', '^Z'), then='kill-parent', ends='itself', exit=None, window=WINDOW[b])


def pipe_cases():
    for mode in MODES:
        yield Case(f'output/{mode}/| cat (plain text, no colour)', ('OUT-2', 'STOP-1', 'CLEAN-1'), mode=mode, pipe=' | cat',
                   phase='running', event=('key', '^C'), ends='itself', exit=0, stdout_tty=False)
        if mode == 'lan':
            yield Case(f'output/{mode}/| head -2 (reader leaves, nothing more is written)', 'PIPE-1', mode=mode,
                       pipe=' | head -2', phase='none', ends='continues', then='ctrl-c', exit=0, stdout_tty=False)
        else:
            yield Case(f'output/{mode}/| head -2 (reader leaves, the summary block finds it gone)', 'PIPE-1',
                       mode=mode, pipe=' | head -2', phase='none', ends='itself', exit=0, window=PIPE_PUBLIC,
                       since='address', messages=('write error: broken pipe',))
        yield Case(f'output/{mode}/| true (serve writes first)', 'PIPE-2', mode=mode, pipe=' | true',
                   phase='none', ends='itself', exit=0, window=PIPE_BOTH, messages=('write error: broken pipe',))
        yield Case(f'output/{mode}/|& true (whoever writes first)', 'PIPE-3', mode=mode, pipe=' |& true',
                   phase='none', ends='itself', exit=(0, 1), window=PIPE_BOTH)
        yield Case(f'output/{mode}/|& tee (pipeline peer in the group)', ('OUT-2', 'STOP-1', 'CLEAN-1'), mode=mode, pipe=' |& tee {tee}',
                   phase='running', event=('signal', 'TERM'), ends='itself', exit=0, stdout_tty=False, tee=True)
    # a service hits the dead pipe first: the stub logs again a second in, after
    # serve's own output has gone to the terminal
    yield Case('output/share/2>&1 >/dev/tty | head -1 (a service writes first)', 'PIPE-4', mode='share',
               pipe=' 2>&1 >/dev/tty | head -1', phase='none', ends='itself', exit=1, window=PIPE_SERVICE)


# -------------------------------------------------------------------- cleanup

def cleanup_cases():
    lo, hi = WINDOW['stubborn']
    for event in (STOP_EVENTS + [('group', s) for s in ('TERM', 'INT', 'HUP', 'QUIT')] + [('kill-parent',)]):
        yield Case(f'cleanup/share/stubborn/{event_id(event)} during cleanup', ('CLEAN-4', 'CLEAN-3'), mode='share',
                   behaviour='stubborn', phase='cleanup', event=('key', '^C'), second=event, ends='itself',
                   exit=137 if observable(event) else None, window=(lo, hi))


# ---------------------------------------------------------- known limitations

def limitation_cases():
    for mode in MODES:
        for phase in ('startup', 'running'):
            shims = {'caddy': 'slow'} if phase == 'startup' else {}
            for s in FATAL_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} skips cleanup', (), mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='leaks', exit=128 + signum(s), window=QUICK,
                           crashes=s in CORE_SIGNALS)
        yield Case(f'startup/{mode}/lsof hangs', 'LIM-1', mode=mode, shims={'lsof': 'hangs'},
                   phase='none', ends='continues', then='ctrl-c', hold=12, exit=0, printed=False)
        yield Case(f'startup/{mode}/scutil hangs', 'LIM-2', mode=mode, shims={'scutil': 'hangs'},
                   phase='none', ends='continues', then='ctrl-c', hold=2, exit=130, printed=False,
                   nothing_started=True)
    yield Case('running/share/lsof hangs reading the address', ('LIM-4', 'STOP-1', 'CLEAN-1'), mode='share',
               shims={'lsof': 'hangs-for-cloudflared'}, phase='none', ends='continues', then='ctrl-c', hold=3,
               exit=0, public=False, serving=True)
    yield Case('cleanup/share/stubborn/signal:KILL during cleanup skips the rest', 'LIM-3', mode='share',
               behaviour='stubborn', phase='cleanup', event=('key', '^C'), second=('signal', 'KILL'),
               ends='leaks', exit=137, window=QUICK)


# ------------------------------------------------------------ public address

def public_cases():
    # PUB-1: shown, whether the address comes at once or seconds later
    for address in ('ok', 'late'):
        yield Case(f'public/share/address {"at once" if address == "ok" else "3 s in"} is shown',
                   ('PUB-1', 'STOP-1', 'CLEAN-1'), kind='public', mode='share', shims={'address': address})
    # PUB-3: never readable, however it fails
    for address in ('never', '404', 'not-json', 'empty', 'bad-host'):
        yield Case(f'public/share/address {address}: serve says so and keeps serving',
                   ('PUB-3', 'STOP-1', 'CLEAN-1'), kind='public', mode='share', shims={'address': address})
    # PUB-4: stopped while still waiting for it
    for event in STOP_EVENTS:
        yield Case(f'addrwait/share/{event_id(event)} while waiting for the address', ('PUB-4', 'CLEAN-1'),
                   mode='share', shims={'address': 'never'}, phase='addrwait', event=event, ends='itself',
                   exit=0 if observable(event) else None, window=WINDOW['idle'], public=False)


# ------------------------------------------------------------ usage and output

def warning_cases():
    for mode in MODES:
        yield Case(f'warn/{mode}/leftovers are pointed out, nothing else is', ('WARN-1', 'WARN-2'),
                   kind='warn', mode=mode)


def usage_cases():
    for mode in MODES:
        yield Case(f'usage/{mode}/three instances at once', ('MULTI-1', 'PUB-2') if mode == 'share' else 'MULTI-1',
                   kind='concurrency', mode=mode)
        yield Case(f'output/{mode}/streams', ('OUT-1', 'OUT-4', 'PUB-1') if mode == 'share' else 'OUT-1',
                   kind='streams', mode=mode)


def tunnel_cases():
    """--real-tunnel only: the few cases worth a real quick tunnel."""
    yield Case('tunnel/real/business, Ctrl-C', ('TUN-2', 'STOP-1', 'CLEAN-1'), mode='share',
               shims={'cloudflared': 'real'}, phase='running', event=('key', '^C'), ends='itself', exit=0,
               window=WINDOW['idle'])
    for event in (('key', '^C'), ('signal', 'TERM')):
        yield Case(f'tunnel/real/draining through the tunnel/{event_id(event)}', ('TUN-2', 'CLEAN-2'), mode='share',
                   behaviour='draining', shims={'cloudflared': 'real'}, phase='running', event=event,
                   ends='itself', exit=0, window=WINDOW['draining'], via_tunnel=True)


def all_cases(real_tunnel=False):
    if real_tunnel:
        return list(tunnel_cases())
    out = []
    for gen in (argument_cases, dependency_cases, hostname_cases, leader_cases, port_cases, pretrap_cases,
                running_cases, other_signal_cases, ctrl_z_cases, pipe_cases, cleanup_cases,
                limitation_cases, public_cases, warning_cases, usage_cases):
        out.extend(gen())
    ids = [c.id for c in out]
    assert len(ids) == len(set(ids)), 'duplicate case ids'
    return out
