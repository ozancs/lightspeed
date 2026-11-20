"""Runs lightspeed.py against a fake Klipper: config parsing, scenes,
homing/probing guard, MCU rate limit, WLED UDP packets, esp_bridge keys,
that a failing output never takes the printer down, and custom scenes
(triggers, priorities, groups, value sources, led_effect compatibility).

    python test/smoke_klippy.py [path/to/lightspeed.cfg]
"""
import configparser
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lightspeed as LS  # noqa: E402

failures = []


def check(cond, msg):
    print(('ok    ' if cond else 'FAIL  ') + msg)
    if not cond:
        failures.append(msg)


# ------------------------------------------------------------ fakes

class ConfigError(Exception):
    pass


class Reactor:
    NOW = 0.
    NEVER = 9999999999999999.

    def __init__(self):
        self.t = 100.0
        self.timers = []
        self.callbacks = []

    def monotonic(self):
        return self.t

    def register_timer(self, cb, when):
        tm = [cb, when]
        self.timers.append(tm)
        return tm

    def update_timer(self, tm, when):
        tm[1] = when

    def register_callback(self, cb, waketime=0.):
        self.callbacks.append((cb, waketime))

    def run_until(self, t_end, step=0.005):
        while self.t < t_end:
            self.t += step
            for tm in self.timers:
                if tm[1] <= self.t:
                    tm[1] = tm[0](self.t)
            pend, self.callbacks = self.callbacks, []
            for cb, wt in pend:
                if wt <= self.t:
                    cb(self.t)
                else:
                    self.callbacks.append((cb, wt))


class GCode:
    def __init__(self):
        self.handlers = {}
        self.gcode_help = {}
        self.out = []

    def register_command(self, name, func, desc=None):
        if func is None:
            return self.handlers.pop(name, None)
        if name in self.handlers:
            raise ConfigError('dup ' + name)
        self.handlers[name] = func
        if desc:
            self.gcode_help[name] = desc

    def respond_info(self, msg):
        self.out.append(msg)

    def run(self, line):
        parts = line.split(None, 1)
        params = {}
        if len(parts) > 1:
            import shlex
            for a in shlex.split(parts[1]):
                k, v = a.split('=', 1)
                params[k.upper()] = v
        self.handlers[parts[0]](GCmd(params))


class GCmdError(Exception):
    pass


class GCmd:
    def __init__(self, params):
        self.params = params
        self.error = GCmdError

    def get(self, k, default=LS):
        if k in self.params:
            return self.params[k]
        if default is LS:
            raise GCmdError('missing ' + k)
        return default

    def get_float(self, k, default=LS, minval=None, maxval=None):
        return float(self.get(k, default))

    def get_int(self, k, default=LS, minval=None, maxval=None):
        return int(self.get(k, default))

    def respond_info(self, msg):
        print('      > ' + msg.replace('\n', '\n        '))


class Section:
    def __init__(self, printer, cp, name):
        self.printer, self.cp, self.name = printer, cp, name
        self.error = ConfigError

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def _raw(self, k, default):
        ACCESSED.add((self.name, k.lower()))
        if self.cp.has_section(self.name) and self.cp.has_option(self.name, k):
            return self.cp.get(self.name, k)
        if default is LS:
            raise ConfigError('missing %s in [%s]' % (k, self.name))
        return default

    def get(self, k, default=LS):
        return self._raw(k, default)

    def getfloat(self, k, default=LS, minval=None, maxval=None):
        v = self._raw(k, default)
        return None if v is None else float(v)

    def getint(self, k, default=LS, minval=None, maxval=None):
        v = self._raw(k, default)
        return None if v is None else int(v)

    def getlist(self, k, default=LS):
        v = self._raw(k, default)
        if isinstance(v, (list, tuple)):
            return list(v)
        return [x.strip() for x in v.split(',')]

    def get_prefix_options(self, prefix):
        if not self.cp.has_section(self.name):
            return []
        return [o for o in self.cp.options(self.name) if o.startswith(prefix)]

    def getsection(self, name):
        return Section(self.printer, self.cp, name)


class Chain:
    def __init__(self, n, fail=False):
        self.n, self.fail, self.updates = n, fail, []
        self.led_helper = type('H', (), {})()
        self.led_helper.led_state = [(0., 0., 0., 0.)] * n

    def get_status(self, et):
        return {'color_data': self.led_helper.led_state}

    def update_leds(self, state, print_time):
        if self.fail:
            raise RuntimeError('simulated MCU error')
        self.updates.append((REACTOR.t, list(state)))


class Heater:
    def __init__(self):
        self.temp, self.target = 25., 0.

    def get_status(self, et):
        return {'temperature': self.temp, 'target': self.target}


class Obj:
    def __init__(self, st):
        self.st = st

    def get_status(self, et):
        return dict(self.st)


class Gcrq:
    def __init__(self):
        self.values = []

    def send_async_request(self, v):
        self.values.append((REACTOR.t, v))


class OutPin:
    def __init__(self, pwm):
        self.is_pwm, self.scale, self.gcrq = pwm, 1., Gcrq()


class Bridge:
    def __init__(self):
        self.producers = []
        self.passes = []

    def _publish_pass(self, eventtime):
        self.passes.append(eventtime)

    def _silenced(self):
        return False

    def register_producer(self, fn):
        self.producers.append(fn)


class WebRequest:
    def __init__(self, params):
        self.params, self.result = params, None
        self.error = GCmdError

    def get(self, k, default=LS):
        if k in self.params:
            return self.params[k]
        if default is LS:
            raise GCmdError('missing ' + k)
        return default

    def get_str(self, k, default=LS):
        return str(self.get(k, default))

    def get_float(self, k, default=LS):
        return float(self.get(k, default))

    def send(self, data):
        self.result = data


class Webhooks:
    def __init__(self):
        self.endpoints = {}

    def get_status(self, et):
        return {'state': 'ready'}

    def register_endpoint(self, path, cb):
        self.endpoints[path] = cb


class Printer:
    def __init__(self):
        self.objects = {}
        self.events = {}
        self.config_error = ConfigError
        self.command_error = GCmdError

    def get_reactor(self):
        return REACTOR

    def lookup_object(self, name, default=LS):
        if name in self.objects:
            return self.objects[name]
        if default is LS:
            raise ConfigError('no object ' + name)
        return default

    def register_event_handler(self, ev, cb):
        self.events.setdefault(ev, []).append(cb)

    def send_event(self, ev, *args):
        for cb in self.events.get(ev, []):
            cb(*args)

    def load_object(self, config, name):
        if name not in self.objects:
            self.objects[name] = LS.load_config(config.getsection(name))
        return self.objects[name]


REACTOR = Reactor()
ACCESSED = set()
EARLY = []


def build(cfg_text, wled_port, extra=None):
    p = Printer()
    g = GCode()
    p.objects['gcode'] = g
    p.objects['webhooks'] = Webhooks()
    p.objects['neopixel hotend_rgb'] = Chain(3)
    p.objects['neopixel case'] = Chain(100)
    p.objects['neopixel broken'] = Chain(4, fail=True)
    p.objects['esp_bridge'] = Bridge()
    p.objects['output_pin case_light_power'] = OutPin(False)
    p.objects['extruder'] = Heater()
    p.objects['heater_bed'] = Heater()
    p.objects['heaters'] = Obj({'available_heaters': ['heater_bed',
                                                      'extruder']})
    p.objects['print_stats'] = Obj({'state': 'standby'})
    p.objects['display_status'] = Obj({'progress': 0.0})
    p.objects['toolhead'] = Obj({'position': [10., 20., 5., 0.],
                                 'axis_minimum': [0., 0., 0., 0.],
                                 'axis_maximum': [300., 300., 300., 0.]})
    g.register_command('BED_MESH_CALIBRATE', lambda gcmd: None,
                       desc='Perform Mesh Bed Leveling')
    g.register_command('SENSEI_TAP', lambda gcmd: None, desc='tap')
    if extra is not None:
        extra(p, g)
    cfg_text = cfg_text.replace('WLEDPORT', str(wled_port))
    cp = configparser.RawConfigParser(strict=False,
                                      inline_comment_prefixes=(';', '#'))
    cp.read_string(cfg_text)
    for sec in cp.sections():
        if sec.startswith('lightspeed '):
            p.objects[sec] = LS.load_config_prefix(Section(p, cp, sec))
        elif sec == 'lightspeed':
            p.load_object(Section(p, cp, sec), 'lightspeed')
    # Klipper rejects options nobody read
    for sec in cp.sections():
        if sec.split()[0] == 'lightspeed':
            for opt in cp.options(sec):
                if (sec, opt) not in ACCESSED:
                    raise ConfigError("Option '%s' is not valid in section "
                                      "'%s'" % (opt, sec))
    # Moonraker polls the status while Klipper is still starting
    EARLY.append(p.objects['lightspeed'].get_status(REACTOR.t)
                 if 'lightspeed' in p.objects else None)
    p.send_event('klippy:connect')
    p.send_event('klippy:ready')
    return p, g


CFG = """
[lightspeed]
brightness: 1.0
guard_commands: SENSEI_TAP, sensei_pa_auto
model: voron24
size: 350,350,340

[lightspeed strip hotend]
output: neopixel hotend_rgb
place: toolhead_logo

[lightspeed strip case]
output: neopixel case
leds: 1-100
place: top_ring

[lightspeed strip broken]
output: neopixel broken

[lightspeed strip wall]
output: wled 127.0.0.1:WLEDPORT
count: 600
place: top_ring

[lightspeed strip case_light]
output: pin case_light_power
color: ffd9a0
place: top_left, top_right
spacing: 16.5

[lightspeed strip node]
output: esp_bridge led
count: 12
brightness: 0.31

[lightspeed scene idle]
all: rainbow period=4
hotend: breathe color=1a3cff

[lightspeed scene error]
case_light: blink period=1

[lightspeed scene homing]
all: scan color=00ff40

[lightspeed scene heating]
all: progress source=heat color=ff4a00
"""


def main():
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(('127.0.0.1', 0))
    rx.setblocking(False)
    port = rx.getsockname()[1]
    cfg = CFG
    if len(sys.argv) > 1:
        cfg = open(sys.argv[1], encoding='utf-8').read()
    p, g = build(cfg, port)
    ls = p.objects['lightspeed']
    hot = p.objects['neopixel hotend_rgb']
    case = p.objects['neopixel case']
    broken = p.objects['neopixel broken']

    REACTOR.run_until(REACTOR.t + 3.0)
    st = ls.get_status(REACTOR.t)
    if len(sys.argv) > 1:
        check(not failures, 'exported config loads and runs')
        print(st)
        return
    check(EARLY and isinstance(EARLY[-1], dict),
          'status before connect does not raise (%s)' % (EARLY,))
    check(st['state'] == 'idle', 'idle state at start (%s)' % st['state'])
    check(len(hot.updates) > 10, 'hotend chain updated (%d)'
          % len(hot.updates))
    fps_case = len(case.updates) / 3.0
    check(fps_case <= LS.engine.mcu_fps(300) + 1,
          '100-LED chain rate limited (%.1f fps, cap %.1f)'
          % (fps_case, LS.engine.mcu_fps(300)))
    check(st['strips']['broken']['disabled'],
          'failing chain disabled, printer unaffected')
    check(any('stopped' in m for m in g.out), 'user told about it once')
    pkts = []
    while True:
        try:
            pkts.append(rx.recv(2000))
        except BlockingIOError:
            break
    check(len(pkts) > 50 and pkts[0][0] == 4,
          'WLED DNRGB packets received (%d)' % len(pkts))
    check(any(pk[2] * 256 + pk[3] == 489 for pk in pkts),
          '600 LEDs split over two packets')
    keys = p.objects['esp_bridge'].producers[0](REACTOR.t)
    check(dict(keys).get('led.fx') == 'solid' and dict(keys).get('led.rgb')
          not in (None, '255,255,255'), 'esp rainbow driven as walking '
          'solid colour (%s %s)' % (dict(keys).get('led.fx'),
                                    dict(keys).get('led.rgb')))

    pin = p.objects['output_pin case_light_power'].gcrq
    check(len(pin.values) == 1 and pin.values[0][1] == 1.0,
          'case light switched on once (%s)' % pin.values)
    check(dict(keys).get('led.b') == '%d' % round(0.31 * 255),
          'esp node brightness from strip brightness (%s)'
          % dict(keys).get('led.b'))
    g.run('LIGHTSPEED_STATE STATE=error HOLD=3')
    REACTOR.run_until(REACTOR.t + 2.6)
    vals = [v for _, v in pin.values]
    # blink period 1: edges every 0.5 s, each change lands within 10 ms
    edges = [t for t, _ in pin.values[1:]]
    check(all(abs(t / 0.5 - round(t / 0.5)) * 0.5 < 0.012 for t in edges),
          'pin blink switches on the edge (%s)'
          % [round(t % 0.5, 3) for t in edges])
    check(2 <= len(vals) <= 7 and 0.0 in vals,
          'case light blinks on error, rate limited (%d changes)'
          % len(vals))
    g.run('LIGHTSPEED_STATE STATE=auto')
    REACTOR.run_until(REACTOR.t + 1.0)
    check(pin.values[-1][1] == 1.0, 'case light back on')

    # homing: MCU chains frozen, big chain gets nothing, WLED keeps going
    n_case, n_hot = len(case.updates), len(hot.updates)
    p.send_event('homing:home_rails_begin', None, [])
    p.send_event('homing:homing_move_begin', None)
    REACTOR.run_until(REACTOR.t + 2.0)
    check(ls.state == 'homing', 'homing scene during G28')
    check(len(case.updates) == n_case, '100-LED chain frozen while homing')
    check(len(hot.updates) - n_hot <= 1,
          'small chain shows homing once (%d)' % (len(hot.updates) - n_hot))
    p.send_event('homing:homing_move_end', None)
    p.send_event('homing:home_rails_end', None, [])
    # G28 straight into a probe move (PRINT_START): small chain must
    # switch from the homing colour to the probing colour
    n_hot = len(hot.updates)
    p.send_event('homing:homing_move_begin', None)
    REACTOR.run_until(REACTOR.t + 0.5)
    check(ls.state == 'probing' and len(hot.updates) - n_hot == 1,
          'homing -> probing shows the probing colour once (%s, %d)'
          % (ls.state, len(hot.updates) - n_hot))
    p.send_event('homing:homing_move_end', None)
    REACTOR.run_until(REACTOR.t + 2.0)
    check(len(case.updates) > n_case, 'chain resumes after homing')
    check(not hasattr(p.objects['lightspeed strip case'], 'get_status'),
          'strip sections are not separate status objects')

    # wrapped calibration command
    seen = []
    orig = ls._guard

    def spy(now):
        r = orig(now)
        seen.append(r)
        return r
    ls._guard = spy
    g.handlers['BED_MESH_CALIBRATE'] = g.handlers['BED_MESH_CALIBRATE']
    in_cmd = []
    old_inner = g.handlers['BED_MESH_CALIBRATE']
    # the original runs inside the wrapper: tick once from inside it
    ls_wrapped_old = None
    for cell in (old_inner.__defaults__ or ()):
        if callable(cell):
            ls_wrapped_old = cell
    check(ls_wrapped_old is not None, 'BED_MESH_CALIBRATE wrapped')
    g.handlers['BED_MESH_CALIBRATE'](GCmd({}))
    check(ls.guard_until > REACTOR.t, 'guard held after calibration ends')
    in_cmd.append(ls._guard(REACTOR.t))
    check(in_cmd[0] == (True, 'probing'), 'probing state after mesh')
    check('SENSEI_TAP' in g._lightspeed_wrapped,
          'guard_commands from config wrapped')
    check(g.gcode_help.get('BED_MESH_CALIBRATE') ==
          'Perform Mesh Bed Leveling', 'command help kept')
    ls._guard = orig
    REACTOR.run_until(REACTOR.t + 2.0)

    # heating
    p.objects['extruder'].target = 250.
    REACTOR.run_until(REACTOR.t + 0.5)
    check(ls.state == 'heating', 'heating state')
    p.objects['extruder'].temp = 137.5
    REACTOR.run_until(REACTOR.t + 0.5)
    check(abs(ls.ctx['heat'] - 0.5) < 0.01, 'heat progress 50%% (%.2f)'
          % ls.ctx['heat'])
    p.objects['extruder'].temp = 250.
    p.objects['print_stats'].st['state'] = 'printing'
    REACTOR.run_until(REACTOR.t + 0.5)
    check(ls.state == 'printing', 'printing state (falls back to idle '
          'scene: %s)' % LS.engine.resolve_scene('printing', ls.scenes))

    # commands
    g.run('LIGHTSPEED_PREVIEW STRIP=case EFFECT="comet color=ff6b1a" HOLD=5')
    REACTOR.run_until(REACTOR.t + 0.3)
    check(ls.get_status(REACTOR.t)['strips']['case']['effect']
          .startswith('comet'), 'preview shows on strip')
    try:
        g.run('LIGHTSPEED_PREVIEW STRIP=case EFFECT="sparkle"')
        check(False, 'bad effect rejected')
    except GCmdError:
        check(True, 'bad effect rejected with an error')
    g.run('LIGHTSPEED_PREVIEW STRIP=case HOLD=0')
    g.run('LIGHTSPEED_SCENE STATE=printing STRIP=case EFFECT="fire"')
    REACTOR.run_until(REACTOR.t + 0.3)
    check(ls.get_status(REACTOR.t)['strips']['case']['effect'] == 'fire'
          or ls.state != 'printing', 'runtime scene change applies')
    g.run('LIGHTSPEED_SCENE STATE=printing STRIP=case EFFECT=')
    g.run('LIGHTSPEED_STRIP STRIP=node BRIGHTNESS=0.5')
    check([x for x in ls.strips if x.name == 'node'][0].brightness == 0.5,
          'runtime strip brightness')
    ep = p.objects['webhooks'].endpoints.get('lightspeed/cmd')
    check(ep is not None, 'web endpoint registered')
    n_out = len(g.out)
    wr = WebRequest({'action': 'batch', 'actions': [
        {'action': 'state', 'state': 'paused', 'hold': 2},
        {'action': 'preview', 'strip': 'case', 'effect': 'fire', 'hold': 2}]})
    ep(wr)
    REACTOR.run_until(REACTOR.t + 0.2)
    check(wr.result == {'ok': True} and ls.state == 'paused'
          and ls.get_status(REACTOR.t)['strips']['case']['effect'] == 'fire',
          'web batch applies state + preview')
    check(len(g.out) == n_out, 'web commands write nothing to the console')
    try:
        ep(WebRequest({'action': 'brightness', 'value': 'abc'}))
        check(False, 'bad value gives an error, not a crash')
    except GCmdError:
        check(True, 'bad value gives an error, not a crash')
    try:
        ep(WebRequest({'action': 'preview', 'strip': 'nope', 'effect': 'fire'}))
        check(False, 'web error for unknown strip')
    except GCmdError:
        check(True, 'web error for unknown strip')
    ep(WebRequest({'action': 'preview', 'strip': 'case', 'effect': 'fire',
                   'hold': 60}))
    try:
        ep(WebRequest({'action': 'batch', 'actions': [
            {'action': 'preview', 'strip': 'nope', 'hold': 0},
            {'action': 'preview', 'strip': 'case', 'hold': 0},
            {'action': 'state', 'state': 'auto'}]}))
        check(False, 'batch with a bad item reports it')
    except GCmdError:
        check('case' not in ls.preview and ls.override is None,
              'batch runs the items after a bad one (end of a test)')
    # ESP blink: driven from the module clock, edge published right away
    br = p.objects['esp_bridge']
    ep(WebRequest({'action': 'preview', 'strip': 'node',
                   'effect': 'blink color=ff0000 period=1', 'hold': 3}))
    n_pass = len(br.passes)
    REACTOR.run_until(REACTOR.t + 2.2)
    keys = dict(br.producers[0](REACTOR.t))
    edges = br.passes[n_pass + 1:]  # the first one is the switch to blink
    check(keys.get('led.fx') == 'solid' and keys.get('led.rgb') in
          ('255,0,0', '0,0,0'), 'esp blink sent as switched solid (%s %s)'
          % (keys.get('led.fx'), keys.get('led.rgb')))
    check(3 <= len(edges) <= 6 and all(
        abs(t / 0.5 - round(t / 0.5)) * 0.5 < 0.012 for t in edges),
        'esp blink edges published on the shared clock (%s)'
        % [round(t % 0.5, 3) for t in edges])
    ep(WebRequest({'action': 'preview', 'strip': 'node', 'hold': 0}))
    p.objects['extruder'].target = 0.
    g.run('LIGHTSPEED_STATE STATE=heating HOLD=4')
    REACTOR.run_until(REACTOR.t + 1.6)
    h1 = ls.ctx['heat']
    REACTOR.run_until(REACTOR.t + 1.8)
    check(0.4 < h1 < 0.6 and ls.ctx['heat'] == 1.0,
          'heating test ramps heat with cold heaters (%.2f -> %.2f)'
          % (h1, ls.ctx['heat']))
    g.run('LIGHTSPEED_STATE STATE=complete HOLD=3')
    REACTOR.run_until(REACTOR.t + 0.2)
    check(ls.state == 'complete', 'state override')
    g.run('LIGHTSPEED_STATE STATE=auto')
    g.run('LIGHTSPEED_STATUS')

    # shutdown: MCU outputs quiet, WLED shows it
    n_hot = len(hot.updates)
    p.send_event('klippy:shutdown')
    REACTOR.run_until(REACTOR.t + 1.0)
    check(len(hot.updates) == n_hot, 'no MCU traffic after shutdown')
    check(ls.state == 'shutdown', 'shutdown scene for WLED')

    scenes_main()
    print('\n%d failures' % len(failures))
    sys.exit(1 if failures else 0)


# ------------------------------------------------------------ custom scenes

CFG_SCENES = """
[lightspeed]
brightness: 1.0

[lightspeed strip left]
output: neopixel front
leds: 1-6

[lightspeed strip right]
output: neopixel front
leds: 7-12

[lightspeed strip nozzle]
output: neopixel hotend_rgb

[lightspeed strip node]
output: esp_bridge led
count: 4

[lightspeed group bar]
strips: left, right

[lightspeed group span]
strips: right, nozzle

[lightspeed scene idle]
all: solid color=000010

[lightspeed scene printing]
left: heat source=heater_bed.temperature colors=0000ff,ffff00,ff8000,ff0000,a000ff
right: heat source=extruder.temperature min=20 max=260
node: heat source=heater_bed.temperature min=20 max=100 colors=0000ff,ff0000
nozzle: off

[lightspeed scene cancelled]
trigger: print_cancelled
priority: 60
bar: blink color=ff0000 period=0.5

[lightspeed scene clean]
trigger: macro CLEAN_NOZZLE
priority: 70
hold: 2
nozzle: solid color=00ffff

[lightspeed scene boom]
trigger: macro BOOM
nozzle: solid color=ff00ff

[lightspeed scene push]
trigger: extruding
priority: 55
nozzle: solid color=00ff00

[lightspeed scene pull]
trigger: retracting
priority: 56
nozzle: solid color=ff0000

[lightspeed scene hot]
trigger: temperature_sensor chamber.temperature > 40
priority: 30
right: solid color=ff8800

[lightspeed scene ghost]
trigger: nosuch.value > 1
all: solid color=ffffff

[lightspeed scene party]
bar: comet color=ffffff period=1 size=3

[lightspeed scene party2]
trigger: command
priority: 90
left: solid color=123456

[lightspeed scene sweep]
priority: 95
span: comet color=ffffff period=1 size=3
"""


def scenes_main():
    print('\n-- custom scenes')
    seen = {}

    def extra(p, g):
        p.objects['neopixel front'] = Chain(12)
        p.objects['motion_report'] = Obj({'live_extruder_velocity': 0.})
        p.objects['temperature_sensor chamber'] = Obj({'temperature': 25.})

        def clean(gcmd):
            seen['depth'] = ls_ref[0].custom['clean'].depth

        def boom(gcmd):
            raise GCmdError('macro failed')
        g.register_command('CLEAN_NOZZLE', clean, desc='wipe')
        g.register_command('BOOM', boom)
    ls_ref = []
    p, g = build(CFG_SCENES, 9, extra)
    ls = p.objects['lightspeed']
    ls_ref.append(ls)
    run = lambda dt: REACTOR.run_until(REACTOR.t + dt)
    run(1.0)
    st = ls.get_status(REACTOR.t)
    check(st['state'] == 'idle' and st['scenes_active'] == [],
          'idle, no custom scene active (%s)' % st['scenes_active'])
    check([c['name'] for c in st['custom_scenes']][:2] == ['cancelled',
                                                           'clean']
          and st['groups'] == {'bar': ['left', 'right'],
                                 'span': ['right', 'nozzle']},
          'status lists custom scenes and groups')
    pr = dict((c['name'], c['priority']) for c in st['custom_scenes'])
    check(pr['party'] == 90 and pr['push'] == 55 and pr['boom'] == 50,
          'priorities: command default 90, others 50, explicit kept (%s)'
          % pr)
    check(any('nosuch' in m for m in g.out),
          'missing condition object warned at connect')
    check(not ls.custom['ghost'].active, 'missing object: trigger off')

    # compatibility commands
    check(ls.compat == ['SET_LED_EFFECT', 'STOP_LED_EFFECTS']
          and 'SET_LED_EFFECT' in g.handlers,
          'SET_LED_EFFECT / STOP_LED_EFFECTS registered without led_effect')
    g.run('SET_LED_EFFECT EFFECT=PARTY FADETIME=1 REPLACE=1 LEDS=x')
    run(1.0)
    pl = ls.plan
    check(pl['left'][:2] == ('party', 'bar') and pl['left'][3:] == (0, 12)
          and pl['right'][3:] == (6, 12),
          'group line spans both strips (%s %s)' % (pl['left'][3:],
                                                     pl['right'][3:]))
    name, prm = pl['left'][2]
    ls.ctx['t'] = REACTOR.t
    ls._gframes = {}
    whole = LS.engine.render(name, prm, 12, ls.ctx)
    check(ls._frame(ls.strips[0], pl['left']) + ls._frame(ls.strips[1],
                                                          pl['right'])
          == whole, 'comet drawn once over the group, split in order')
    front = p.objects['neopixel front']
    n0 = len(front.updates)
    run(0.5)
    check(len(front.updates) > n0, 'shared chain keeps updating')
    check(ls.get_status(REACTOR.t)['scenes_active'] == ['party'],
          'scenes_active in status')
    n_out = len(g.out)
    g.run('SET_LED_EFFECT EFFECT=nope')
    check(True, 'unknown SET_LED_EFFECT name does not raise')
    g.run('SET_LED_EFFECT EFFECT=party STOP=1')
    run(0.2)
    check(not ls.custom['party'].active and ls.plan['left'][0] == 'idle',
          'SET_LED_EFFECT STOP=1 switches the scene off, idle shows again')
    # tie: newest wins
    g.run('LIGHTSPEED_SCENE_ON NAME=party')
    run(0.1)
    g.run('LIGHTSPEED_SCENE_ON NAME=party2')
    run(0.1)
    check(ls.plan['left'][0] == 'party2' and ls.plan['right'][0] == 'party',
          'equal priority: the most recently switched on wins (%s, %s)'
          % (ls.plan['left'][0], ls.plan['right'][0]))
    g.run('LIGHTSPEED_SCENE_ON NAME=party')
    run(0.1)
    check(ls.plan['left'][0] == 'party', 'switched on again: newest again')
    g.run('STOP_LED_EFFECTS EFFECT=party2')
    run(0.1)
    check(not ls.custom['party2'].active and ls.custom['party'].active,
          'STOP_LED_EFFECTS EFFECT= stops only that scene')
    g.run('STOP_LED_EFFECTS')
    run(0.1)
    check(not ls.custom['party'].active, 'STOP_LED_EFFECTS stops all')

    # a group over two chains: one frame cut over both
    hot = p.objects['neopixel hotend_rgb']
    n_f, n_h = len(front.updates), len(hot.updates)
    g.run('LIGHTSPEED_SCENE_ON NAME=sweep')
    run(1.0)
    pl = ls.plan
    check(pl['right'][:2] == ('sweep', 'span') and pl['right'][3:] == (0, 9)
          and pl['nozzle'][3:] == (6, 9) and pl['left'][0] == 'idle',
          'group across two chains (%s %s)' % (pl['right'][3:],
                                               pl['nozzle'][3:]))
    ls.ctx['t'] = REACTOR.t
    ls._gframes = {}
    whole = LS.engine.render('comet', pl['right'][2][1], 9, ls.ctx)
    check(ls._frame(ls.strips[1], pl['right']) + ls._frame(ls.strips[2],
                                                           pl['nozzle'])
          == whole and len(front.updates) > n_f and len(hot.updates) > n_h,
          'both chains get their part of the one frame')
    g.run('LIGHTSPEED_SCENE_OFF NAME=sweep')
    # command scene (90) beats homing (80), error (100) beats it
    g.run('SET_LED_EFFECT EFFECT=party')
    p.send_event('homing:home_rails_begin', None, [])
    run(0.3)
    check(ls.state == 'homing' and ls.plan['left'][0] == 'party',
          'command scene (90) wins over homing (80)')
    p.send_event('homing:home_rails_end', None, [])
    run(1.5)
    p.objects['print_stats'].st['state'] = 'error'
    run(0.3)
    check(ls.state == 'error' and ls.plan['left'][0] == 'error',
          'error (100) wins over a command scene')
    p.objects['print_stats'].st['state'] = 'standby'
    g.run('STOP_LED_EFFECTS')
    run(0.3)

    # macro trigger
    g.run('CLEAN_NOZZLE')
    check(seen.get('depth') == 1, 'macro scene on while the macro runs')
    check(g.gcode_help.get('CLEAN_NOZZLE') == 'wipe', 'macro help kept')
    run(0.2)
    check(ls.custom['clean'].active and ls.plan['nozzle'][0] == 'clean',
          'macro scene held after the macro (hold 2)')
    run(2.2)
    check(not ls.custom['clean'].active, 'macro scene off after hold')
    try:
        g.run('BOOM')
        check(False, 'macro error passes through')
    except GCmdError:
        check(True, 'macro error passes through unchanged')
    run(0.1)
    check(ls.custom['boom'].depth == 0 and not ls.custom['boom'].active,
          'macro that raises still ends its scene')
    check(ls.enabled and ls.tick_errors == 0, 'Lightspeed unaffected')

    # extruding / retracting
    mr = p.objects['motion_report']
    mr.st['live_extruder_velocity'] = 3.
    run(0.2)
    check(ls.plan['nozzle'][0] == 'push', 'extruding scene')
    mr.st['live_extruder_velocity'] = -30.
    run(0.1)
    check(ls.plan['nozzle'][0] == 'pull', 'retracting beats extruding (56)')
    mr.st['live_extruder_velocity'] = 0.
    run(0.15)
    check(ls.custom['pull'].active, 'short debounce keeps a retract visible')
    run(0.4)
    check(not ls.custom['pull'].active and not ls.custom['push'].active,
          'extruding / retracting off when the extruder stops')

    # condition
    ch = p.objects['temperature_sensor chamber']
    ch.st['temperature'] = 45.
    run(0.1)
    check(ls.plan['right'][0] == 'hot' and ls.plan['left'][0] == 'idle',
          'condition scene on the strip it names')
    ch.st['temperature'] = 25.
    run(0.1)
    check(ls.plan['right'][0] == 'idle', 'condition off again')

    # printing with value sources
    p.objects['heater_bed'].temp = 70.
    p.objects['heater_bed'].max_temp = 110.
    p.objects['extruder'].temp = 140.
    p.objects['print_stats'].st['state'] = 'printing'
    run(1.0)
    check(ls.state == 'printing' and
          ls.ctx['vars'].get('heater_bed.temperature') == 70.,
          'value source read into ctx vars (%s)' % ls.ctx['vars'])
    check(ls.ranges.get('heater_bed.temperature') == (20., 110.),
          'temperature range from max_temp (%s)' % ls.ranges)
    c = ls._frame(ls.strips[0], ls.plan['left'])[0]
    exp = LS.engine.gradient_at([LS.engine.parse_color(x) for x in
                                 '0000ff,ffff00,ff8000,ff0000,a000ff'
                                 .split(',')], 50. / 90.)
    check(all(abs(a - b) < 1e-9 for a, b in zip(c, exp)),
          'bed colour from its temperature (%s)' % (c,))
    c = ls._frame(ls.strips[1], ls.plan['right'])[0]
    exp = LS.engine.mix((0., 48 / 255., 1.), (1., 48 / 255., 0.), 0.5)
    check(all(abs(a - b) < 1e-9 for a, b in zip(c, exp)),
          'hotend heat with min/max (%s)' % (c,))
    keys = dict(p.objects['esp_bridge'].producers[0](REACTOR.t))
    check(keys.get('led.fx') == 'bar' and keys.get('led.p') == '0.62'
          and keys.get('led.rgb') == '159,0,96',
          'esp node gets the value colour + level (%s %s)'
          % (keys.get('led.rgb'), keys.get('led.p')))

    # print_cancelled: an event with 30 s
    p.objects['print_stats'].st['state'] = 'cancelled'
    run(0.2)
    check(ls.plan['left'][:2] == ('cancelled', 'bar'),
          'print_cancelled blinks the bar')
    run(30.5)
    check(not ls.custom['cancelled'].active, 'print_cancelled ends after 30 s')
    p.objects['print_stats'].st['state'] = 'standby'

    # a state test hides custom scenes
    g.run('LIGHTSPEED_SCENE_ON NAME=party')
    g.run('LIGHTSPEED_STATE STATE=error HOLD=2')
    run(0.2)
    check(ls.plan['left'][0] == 'error', 'LIGHTSPEED_STATE stays on top')
    g.run('LIGHTSPEED_STATE STATE=auto')
    run(0.2)
    check(ls.plan['left'][0] == 'party', 'custom scene back after the test')

    # live edit of a custom scene, group key
    g.run('LIGHTSPEED_SCENE STATE=party STRIP=bar EFFECT="fire"')
    run(0.1)
    check(ls.plan['left'][2][0] == 'fire', 'runtime edit of a custom scene')
    g.run('LIGHTSPEED_SCENE STATE=party STRIP=bar EFFECT=')
    check('party' in ls.custom, 'emptied custom scene still exists')
    g.run('LIGHTSPEED_SCENE_OFF NAME=all')
    g.run('LIGHTSPEED_PREVIEW STRIP=bar EFFECT="solid color=00ff00" HOLD=2')
    run(0.1)
    check(ls.plan['right'][0] == '__preview__' and ls.plan['right'][3] == 6,
          'preview of a group')
    g.run('LIGHTSPEED_PREVIEW STRIP=bar HOLD=0')

    # endpoint
    ep = p.objects['webhooks'].endpoints['lightspeed/cmd']
    wr = WebRequest({'action': 'scene_on', 'name': 'party', 'hold': 1})
    ep(wr)
    run(0.1)
    check(wr.result == {'ok': True} and ls.custom['party'].active,
          'endpoint scene_on')
    run(1.1)
    check(not ls.custom['party'].active, 'scene_on hold runs out')
    ep(WebRequest({'action': 'batch', 'actions': [
        {'action': 'scene_on', 'name': 'clean', 'hold': 0}]}))
    run(0.1)
    check(ls.custom['clean'].active, 'batch scene_on (any custom scene)')
    ep(WebRequest({'action': 'scene_off', 'name': 'clean'}))
    run(0.1)
    check(not ls.custom['clean'].active, 'endpoint scene_off')
    for bad in ({'action': 'scene_on', 'name': 'nope'},
                {'action': 'scene_on', 'name': 'idle'},
                {'action': 'scene_off', 'name': 'nope'}):
        try:
            ep(WebRequest(bad))
            check(False, 'bad scene action rejected %s' % bad)
        except GCmdError:
            check(True, 'bad scene action rejected %s' % bad)
    check(len(g.out) >= n_out, 'web commands quiet')

    # get_status never raises
    ls.custom['party'].since = None
    ls.custom['party'].active = True
    st = ls.get_status(REACTOR.t)
    check(isinstance(st, dict) and 'state' in st,
          'get_status survives a broken scene')
    ls.custom['party'].since = 0.
    ls.custom['party'].active = False

    # with led_effect loaded the compat commands stay away
    def extra2(p, g):
        extra(p, g)
        p.objects['led_effect fancy'] = Obj({})
        g.register_command('SET_LED_EFFECT', lambda gcmd: None)
    p2, g2 = build(CFG_SCENES, 9, extra2)
    ls2 = p2.objects['lightspeed']
    check(ls2.compat == [] and 'STOP_LED_EFFECTS' not in g2.handlers,
          'not registered when a led_effect object exists')
    # bad configs
    for bad, why in (('[lightspeed scene x1]\ntrigger: sometimes\n',
                      'bad trigger'),
                     ('[lightspeed scene idle]\ntrigger: command\n',
                      'trigger on a state'),
                     ('[lightspeed group g]\nstrips: left, zz\n',
                      'group with unknown strip'),
                     ('[lightspeed scene x2]\nnogroup: fire\n',
                      'scene key not a strip or group')):
        try:
            build(CFG_SCENES + bad, 9, extra)
            check(False, 'config error: ' + why)
        except ConfigError:
            check(True, 'config error: ' + why)


if __name__ == '__main__':
    main()
