"""Every case README.md commits to, and what it says must happen.

Nothing here is read off serve's code or its current behaviour: each case
names the README passage its expectation comes from. Where the README lists
things (signals, events, helpers, arguments), the list is taken whole and
crossed with every mode, phase and child behaviour it applies to.

Terms used below:

mode       'lan' (no arguments) or 'share' (--share)
phase      when the case's event fires:
             'none'     no event; serve ends (or keeps running) by itself
             'pretrap'  during startup step 2, before serve traps anything
             'startup'  during step 5, while serve waits for caddy's port
             'running'  after startup, with everything up
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

# When serve stops (README): the stop signals, in the README's order
STOP_SIGNALS = 'INT HUP TERM QUIT PIPE ALRM USR1 USR2 VTALRM PROF XCPU XFSZ ABRT EMT SYS'.split()
# When serve stops / other signals (README)
PAUSE_SIGNALS = 'TSTP TTIN TTOU STOP'.split()
NO_EFFECT_SIGNALS = 'CHLD WINCH URG IO INFO'.split()
# Known limitations (README): SIGKILL and the fatal signals serve does not catch
FATAL_SIGNALS = 'KILL SEGV BUS ILL FPE TRAP'.split()

# Signals whose default action dumps core. A process ended by one of them
# makes macOS's crash reporter step in, which holds up other processes'
# bookkeeping for seconds; cases where that happens carry crashes=True, and
# run.py runs them on their own, after the rest. (It changes nothing about
# what they expect.)
CORE_SIGNALS = set('QUIT ILL TRAP ABRT EMT FPE BUS SEGV SYS'.split())

# Every way the README's first stop-table row can reach serve
STOP_EVENTS = ([('key', '^C'), ('key', '^\\'), ('hangup',)]
               + [('signal', s) for s in STOP_SIGNALS])

# Cleanup (README): how long serve takes from being told to stop to having exited. The
# README gives the schedule (services checked every 0.1 s, a repeat INT+TERM
# at about 0.5 s and 1 s, SIGKILL at about 5 s); the bounds below allow for
# that schedule plus scheduling noise, and run.py widens the upper bounds
# further under parallel load.
WINDOW = {'idle': (0.0, 1.5), 'draining': (0.0, 2.5), 'stubborn': (4.5, 6.5)}
PARENT_POLL = 0.5          # README: when serve stops / parent check
PORT_WAIT = (9.5, 11.5)    # README: startup step 5, at most 10 s


def signum(name):
    return int(getattr(signal, 'SIG' + name))


def event_id(event):
    return ':'.join(str(x) for x in event)


class Case:
    def __init__(self, id, readme, kind='lifecycle', mode='lan', args=None, shims=None,
                 behaviour='idle', pipe='', nested=False, phase='running', event=None,
                 second=None, **expect):
        self.id = id
        self.readme = readme
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
    readme = 'Behavior spec / Arguments'
    yield Case('args/--help', readme, kind='args', args='--help', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/-h', readme, kind='args', args='-h', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/--help --x', readme, kind='args', args='--help --x', exit=0, stream='stdout', text='usage: serve [--share]')
    yield Case('args/--x --help', readme, kind='args', args='--x --help', exit=2, stream='stderr', text='serve: unknown option: --x')
    yield Case('args/--shar', readme, kind='args', args='--shar', exit=2, stream='stderr', text='serve: unknown option: --shar')
    yield Case('args/share', readme, kind='args', args='share', exit=2, stream='stderr', text='serve: unknown option: share')
    yield Case('args/--share --x', readme, kind='args', args='--share --x', exit=2, stream='stderr', text='serve: unknown option: --x')
    yield Case("args/'a\\tb' (printed as is)", readme, kind='args', args="'a\\tb'", exit=2, stream='stderr', text='serve: unknown option: a\\tb')
    yield Case("args/'' (empty)", readme, kind='args', args="''", exit=2, stream='stderr', text='serve: unknown option: \n')
    # --share may be repeated; without it cloudflared need not be installed
    yield Case('args/--share --share', readme, mode='share', args='--share --share',
               phase='running', event=('key', '^C'), ends='itself', exit=0)
    yield Case('args/no cloudflared installed, no --share', readme, mode='lan', shims={'missing': ('cloudflared',)},
               phase='running', event=('key', '^C'), ends='itself', exit=0)


# -------------------------------------------------------------------- startup

def dependency_cases():
    readme = 'Behavior spec / Startup, step 1'
    for mode in MODES:
        needed = ('caddy', 'cloudflared', 'scutil', 'lsof') if mode == 'share' else ('caddy', 'scutil', 'lsof')
        for cmd in needed:
            yield Case(f'startup/{mode}/missing {cmd}', readme, mode=mode, shims={'missing': (cmd,)},
                       phase='none', ends='itself', exit=1, window=(0, 2), messages=(f'serve: {cmd} not found',),
                       printed=False, nothing_ran=True)


def hostname_cases():
    for mode in MODES:
        yield Case(f'startup/{mode}/LocalHostName not set', 'Behavior spec / Startup, step 2; Output / Local:', mode=mode,
                   shims={'scutil': 'fails'}, phase='running', event=('key', '^C'), ends='itself', exit=0,
                   host='localhost')


def leader_cases():
    readme = 'Behavior spec / Startup, step 3'
    yield Case('startup/not group leader: true | serve', readme, kind='leader', how='pipeline',
               exit=1, messages=('serve: not a process group leader, refusing to start',))
    yield Case('startup/not group leader: plain command in a script', readme, kind='leader', how='script',
               exit=1, messages=('serve: not a process group leader, refusing to start',))
    yield Case('startup/group leader via exec in a script', readme, kind='leader', how='exec', exit=0)


def port_cases():
    readme = 'Behavior spec / Startup, step 5'
    for mode in MODES:
        yield Case(f'startup/{mode}/caddy exits at once', readme, mode=mode, shims={'caddy': 'exits'},
                   phase='none', ends='itself', exit=1, window=(0, 2), messages=('serve: caddy failed to start',),
                   printed=False, cf=False)
        yield Case(f'startup/{mode}/caddy never listens', readme, mode=mode, shims={'caddy': 'never-listens'},
                   phase='none', ends='itself', exit=1, window=PORT_WAIT,
                   messages=('serve: could not read the port caddy is listening on',), printed=False, cf=False)
        for lsof in ('not-a-number', 'zero', 'too-big', 'way-too-big', 'says-nothing', 'fails'):
            yield Case(f'startup/{mode}/lsof {lsof}', readme, mode=mode, shims={'lsof': lsof},
                       phase='none', ends='itself', exit=1, window=PORT_WAIT,
                       messages=('serve: could not read the port caddy is listening on',), printed=False, cf=False)
        # a stop signal while waiting for the port: straight to cleanup, nothing
        # printed, no cloudflared, exit 0
        for event in STOP_EVENTS + [('kill-parent',)]:
            # closing the terminal or killing the session leader delivers a
            # SIGHUP, which is one of the stop signals
            yield Case(f'startup/{mode}/{event_id(event)} while waiting for the port', readme, mode=mode,
                       shims={'caddy': 'slow'}, phase='startup', event=event, ends='itself',
                       exit=0 if observable(event) else None, window=WINDOW['idle'], printed=False, cf=False)


def pretrap_cases():
    readme = 'When it stops / Signals before startup'
    for mode in MODES:
        for event in STOP_EVENTS:
            name = {'^C': 'INT', '^\\': 'QUIT'}.get(event[1]) if event[0] == 'key' else \
                'HUP' if event == ('hangup',) else event[1]
            # before the trap, serve dies of a core signal sent to it; Ctrl-\
            # also reaches the scutil it waits on, which does
            crashes = name in CORE_SIGNALS and (event[0] == 'key' or name != 'QUIT')
            yield Case(f'pretrap/{mode}/{event_id(event)}', readme, mode=mode, shims={'scutil': 'slow'},
                       phase='pretrap', event=event, ends='itself',
                       exit=128 + signum(name) if observable(event) else None, window=(0, 1.0),
                       printed=False, nothing_started=True, crashes=crashes)


# ------------------------------------------------------------ when serve stops

def running_cases():
    readme = 'Behavior spec / When it stops'
    for mode in MODES:
        for b in BEHAVIOURS[mode]:
            events = STOP_EVENTS + [('group', s) for s in ('TERM', 'INT', 'HUP', 'QUIT')]
            for event in events:
                yield Case(f'running/{mode}/{b}/{event_id(event)}', readme, mode=mode, behaviour=b,
                           phase='running', event=event, ends='itself',
                           exit=normal_exit(b) if observable(event) else None, window=WINDOW[b])
            for nested in (False, True):
                lo, hi = WINDOW[b]
                yield Case(f'running/{mode}/{b}/kill-parent ({"nested shell" if nested else "session leader"})',
                           readme, mode=mode, behaviour=b, nested=nested, phase='running', event=('kill-parent',),
                           ends='itself', exit=None, window=(lo, hi + (PARENT_POLL if nested else 0)))
            services = ('caddy', 'cloudflared') if mode == 'share' else ('caddy',)
            for svc in services:
                # the stubborn one is cloudflared: kill it and nothing is left to wait for
                left = 'idle' if (b == 'stubborn' and svc == 'cloudflared') else b
                yield Case(f'running/{mode}/{b}/{svc} exits', readme, mode=mode, behaviour=b,
                           phase='running', event=('kill', svc), ends='itself',
                           exit=137 if left == 'stubborn' else 1, window=WINDOW[left],
                           messages=(f'serve: {svc} exited',))
        # the parent check: not during startup; noticed once startup is done
        yield Case(f'startup/{mode}/kill-parent (nested shell) while waiting for the port',
                   'When it stops / Parent check', mode=mode, shims={'caddy': 'slow'}, nested=True,
                   phase='startup', event=('kill-parent',), ends='itself', exit=None,
                   window=(1.0, 2.0 + WINDOW['idle'][1] + PARENT_POLL), printed=True)


def other_signal_cases():
    readme = 'When it stops / Other signals'
    for mode in MODES:
        for phase in ('startup', 'running'):
            shims = {'caddy': 'slow'} if phase == 'startup' else {}
            for s in PAUSE_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} pauses serve', readme, mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='continues', then='resume', exit=0)
            for s in NO_EFFECT_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} has no effect', readme, mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='continues', then='ctrl-c', exit=0)


def ctrl_z_cases():
    readme = 'When it stops / Ctrl-Z'
    for mode in MODES:
        yield Case(f'startup/{mode}/^Z then fg', readme, mode=mode, shims={'caddy': 'slow'},
                   phase='startup', event=('key', '^Z'), ends='continues', then='fg', exit=0)
        for b in BEHAVIOURS[mode]:
            yield Case(f'running/{mode}/{b}/^Z then fg', readme, mode=mode, behaviour=b,
                       phase='running', event=('key', '^Z'), ends='continues', then='fg', exit=normal_exit(b))
            for nested in (False, True):
                yield Case(f'running/{mode}/{b}/^Z then kill-parent ({"nested shell" if nested else "session leader"})',
                           readme, mode=mode, behaviour=b, nested=nested, phase='running',
                           event=('key', '^Z'), then='kill-parent', ends='itself', exit=None, window=WINDOW[b])


def pipe_cases():
    readme = 'Output; When it stops / Output pipe reader exits early; Process boundary'
    for mode in MODES:
        yield Case(f'output/{mode}/| cat (plain text, no colour)', readme, mode=mode, pipe=' | cat',
                   phase='running', event=('key', '^C'), ends='itself', exit=0, stdout_tty=False)
        yield Case(f'output/{mode}/| head -2 (reader leaves, nothing more is written)', readme, mode=mode,
                   pipe=' | head -2', phase='none', ends='continues', then='ctrl-c', exit=0, stdout_tty=False)
        yield Case(f'output/{mode}/| true (serve writes first)', readme, mode=mode, pipe=' | true',
                   phase='none', ends='itself', exit=0, window=(0, 3), messages=('write error: broken pipe',))
        yield Case(f'output/{mode}/|& true (whoever writes first)', readme, mode=mode, pipe=' |& true',
                   phase='none', ends='itself', exit=(0, 1), window=(0, 3))
        yield Case(f'output/{mode}/|& tee (pipeline peer in the group)', readme, mode=mode, pipe=' |& tee {tee}',
                   phase='running', event=('signal', 'TERM'), ends='itself', exit=0, stdout_tty=False, tee=True)
    # a service hits the dead pipe first: the stub logs again a second in, after
    # serve's own output has gone to the terminal
    yield Case('output/share/2>&1 >/dev/tty | head -1 (a service writes first)', readme, mode='share',
               pipe=' 2>&1 >/dev/tty | head -1', phase='none', ends='itself', exit=1, window=(0, 4))


# -------------------------------------------------------------------- cleanup

def cleanup_cases():
    readme = 'Behavior spec / Cleanup, steps 1 and 5'
    lo, hi = WINDOW['stubborn']
    for event in (STOP_EVENTS + [('group', s) for s in ('TERM', 'INT', 'HUP', 'QUIT')] + [('kill-parent',)]):
        yield Case(f'cleanup/share/stubborn/{event_id(event)} during cleanup', readme, mode='share',
                   behaviour='stubborn', phase='cleanup', event=('key', '^C'), second=event, ends='itself',
                   exit=137 if observable(event) else None, window=(lo, hi))


# ---------------------------------------------------------- known limitations

def limitation_cases():
    readme = 'Known limitations'
    for mode in MODES:
        for phase in ('startup', 'running'):
            shims = {'caddy': 'slow'} if phase == 'startup' else {}
            for s in FATAL_SIGNALS:
                yield Case(f'{phase}/{mode}/signal:{s} skips cleanup', readme, mode=mode, shims=shims,
                           phase=phase, event=('signal', s), ends='leaks', exit=128 + signum(s), window=(0, 1.0),
                           crashes=s in CORE_SIGNALS)
        yield Case(f'startup/{mode}/lsof hangs', readme, mode=mode, shims={'lsof': 'hangs'},
                   phase='none', ends='continues', then='ctrl-c', hold=12, exit=0, printed=False)
        yield Case(f'startup/{mode}/scutil hangs', readme, mode=mode, shims={'scutil': 'hangs'},
                   phase='none', ends='continues', then='ctrl-c', hold=2, exit=130, printed=False,
                   nothing_started=True)
    yield Case('cleanup/share/stubborn/signal:KILL during cleanup skips the rest', readme, mode='share',
               behaviour='stubborn', phase='cleanup', event=('key', '^C'), second=('signal', 'KILL'),
               ends='leaks', exit=137, window=(0, 1.0))


# ------------------------------------------------------------ usage and output

def usage_cases():
    for mode in MODES:
        yield Case(f'usage/{mode}/three instances at once', 'Usage', kind='concurrency', mode=mode)
        yield Case(f'output/{mode}/streams', 'Output', kind='streams', mode=mode)


def tunnel_cases():
    """--real-tunnel only: the few cases worth a real quick tunnel."""
    readme = 'Output; Arguments; Cleanup, step 4'
    yield Case('tunnel/real/business, Ctrl-C', readme, mode='share', shims={'cloudflared': 'real'},
               phase='running', event=('key', '^C'), ends='itself', exit=0, window=(0, 3))
    for event in (('key', '^C'), ('signal', 'TERM')):
        yield Case(f'tunnel/real/draining through the tunnel/{event_id(event)}', readme, mode='share',
                   behaviour='draining', shims={'cloudflared': 'real'}, phase='running', event=event,
                   ends='itself', exit=0, window=WINDOW['draining'], via_tunnel=True)


def all_cases(real_tunnel=False):
    if real_tunnel:
        return list(tunnel_cases())
    out = []
    for gen in (argument_cases, dependency_cases, hostname_cases, leader_cases, port_cases, pretrap_cases,
                running_cases, other_signal_cases, ctrl_z_cases, pipe_cases, cleanup_cases,
                limitation_cases, usage_cases):
        out.extend(gen())
    ids = [c.id for c in out]
    assert len(ids) == len(set(ids)), 'duplicate case ids'
    return out
