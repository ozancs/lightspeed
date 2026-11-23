# Lightspeed - effect engine
#
# Pure Python, no Klipper import. The same maths lives in
# web/lightspeed_engine.js so the page shows exactly what the printer
# will show; test/parity checks the two against each other.
#
# An effect is a function (n, ctx, params) -> list of n (r, g, b) floats
# in 0..1. Nothing here keeps state between frames: a frame depends only
# on the time and the printer context, so skipping frames (MCU busy,
# homing guard) never desynchronises anything.
#
# ctx keys:
#   t         seconds (monotonic)
#   progress  0..1 print progress
#   heat      0..1 how far the heaters are on their way to target
#   pos       (x, y, z) toolhead position, each 0..1 of the axis range
#   vars      optional {source: raw number} for value sources such as
#             'heater_bed.temperature' (the module reads them, the engine
#             only maps them with min/max)
#   ranges    optional {source: (lo, hi)} default min/max per source (the
#             module knows each heater's max_temp); else default_range()
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math
import re

__version__ = "0.2.2"

STATES = ('shutdown', 'error', 'homing', 'probing', 'paused', 'heating',
          'printing', 'complete', 'idle')

# Which scene to use when a state has none of its own
STATE_FALLBACK = {
    'shutdown': 'error',
    'error': 'idle',
    'homing': 'idle',
    'probing': 'homing',
    'paused': 'printing',
    'heating': 'idle',
    'printing': 'idle',
    'complete': 'idle',
}

# Effect name -> parameter defaults. Every parameter is optional.
EFFECTS = {
    'off':      {},
    'solid':    {'color': 'ffffff'},
    'breathe':  {'color': 'ffffff', 'color2': '000000', 'period': 3.0},
    'blink':    {'color': 'ffffff', 'color2': '000000', 'period': 1.0},
    'rainbow':  {'period': 5.0},
    'chase':    {'color': 'ffffff', 'color2': '000000', 'period': 2.0,
                 'size': 2},
    'comet':    {'color': 'ffffff', 'color2': '000000', 'period': 1.5,
                 'size': 0},
    'scan':     {'color': 'ff0000', 'color2': '000000', 'period': 2.0,
                 'size': 2},
    'twinkle':  {'color': 'ffffff', 'color2': '000000', 'period': 1.2,
                 'density': 0.3},
    'fire':     {},
    # progress: lit part in color (A); gradient=1 runs A -> color2 (B).
    # The unlit part is off. colors=a,b,c...: the lit part runs through
    # these stops along the strip (gradient=1: coloured by the value).
    # source: progress | heat | <object>.<field> mapped min..max -> 0..1
    'progress': {'color': 'ffffff', 'color2': 'ff0000',
                 'source': 'progress', 'gradient': 0, 'min': 'auto',
                 'max': 'auto', 'colors': ''},
    # heat: one colour by value, color2 (cold) -> color (hot), or the
    # colors= stops from first (min) to last (max)
    'heat':     {'color': 'ff3000', 'color2': '0030ff', 'source': 'heat',
                 'min': 'auto', 'max': 'auto', 'colors': ''},
    'follow':   {'color': 'ffffff', 'color2': '000000', 'axis': 'x',
                 'size': 1.5},
}
COMMON_PARAMS = {'reverse': 0}

# What every printer state shows when the config does not say otherwise.
# Colours follow the convention already used on the author's printer:
# homing green, probing purple, printing white, heating red, idle blue.
DEFAULT_SCENES = {
    'idle':     'breathe color=1a3cff color2=000814 period=4',
    'heating':  'progress source=heat gradient=1 color=0030ff color2=ff2000',
    'homing':   'scan color=00ff40 color2=001404 period=1.6',
    'probing':  'scan color=a000dc color2=0c0012 period=1.6',
    'printing': 'solid color=ffffff',
    'paused':   'breathe color=ff6e00 color2=140800 period=2',
    'complete': 'breathe color=00ff40 color2=001a08 period=1.5',
    'error':    'blink color=ff0000 color2=000000 period=0.6',
    'shutdown': 'solid color=ff0000',
}

PARAM_TYPES = {
    'color': 'color', 'color2': 'color',
    'gradient': 'bool', 'period': 'float', 'size': 'float',
    'density': 'float', 'source': 'source',
    'axis': ('x', 'y', 'z'), 'reverse': 'bool',
    'min': 'number', 'max': 'number', 'colors': 'colors',
}

# A value source other than progress/heat: '<object>.<field>', the object
# name may hold one space ('temperature_sensor chamber.temperature');
# in an effect spec the part after the space simply follows source=.
SOURCE_RE = re.compile(r'^[A-Za-z0-9_]+( [A-Za-z0-9_-]+)?(\.[A-Za-z0-9_]+)+$')
# Trigger condition reference: same, the field is optional
REF_RE = re.compile(r'^[A-Za-z0-9_]+( [A-Za-z0-9_-]+)?(\.[A-Za-z0-9_]+)*$')

# Scene priorities: a strip shows the highest-priority active scene that
# names it; built-in states have these unless their section says otherwise
BUILTIN_PRIORITY = {
    'shutdown': 100, 'error': 100, 'homing': 80, 'probing': 80,
    'heating': 20, 'printing': 20, 'paused': 20, 'complete': 20, 'idle': 0,
}
DEFAULT_PRIORITY = 50
# A command scene (LIGHTSPEED_SCENE_ON / SET_LED_EFFECT from a macro) wins
# over every printer state except error / shutdown unless it says otherwise
COMMAND_PRIORITY = 90
TRIGGERS = ('command', 'macro', 'print_cancelled', 'extruding',
            'retracting', 'condition')
TRIGGER_OPS = ('>=', '<=', '==', '!=', '>', '<')
CANCEL_HOLD = 30.0        # s a print_cancelled scene shows without hold:
MOTION_SPEED = 0.5        # mm/s extruder speed for extruding / retracting


class SpecError(ValueError):
    pass


# ---------------------------------------------------------------- parsing

def parse_color(text):
    s = str(text).strip()
    if s.startswith('#'):
        s = s[1:]
    if len(s) == 3:
        s = ''.join(c + c for c in s)
    if len(s) != 6:
        raise SpecError("colour must be rrggbb, got %r" % (text,))
    try:
        v = int(s, 16)
    except ValueError:
        raise SpecError("colour must be rrggbb, got %r" % (text,))
    return ((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, \
        (v & 255) / 255.0


def color_hex(c):
    return '%02x%02x%02x' % tuple(to_byte(x) for x in c)


def parse_spec(text):
    """'comet color=ff6b1a period=1.5' -> ('comet', {params})"""
    parts = str(text).split()
    if not parts:
        raise SpecError("empty effect")
    name = parts[0].lower()
    if name not in EFFECTS:
        raise SpecError("unknown effect '%s' (one of %s)"
                        % (name, ', '.join(sorted(EFFECTS))))
    params = dict(EFFECTS[name])
    params.update(COMMON_PARAMS)
    tokens = []
    for p in parts[1:]:
        # 'source=temperature_sensor chamber.temperature': the object name's
        # second word belongs to source
        if '=' not in p and tokens and \
                tokens[-1].lower().startswith('source=') and \
                ' ' not in tokens[-1]:
            tokens[-1] += ' ' + p
            continue
        tokens.append(p)
    for p in tokens:
        if '=' not in p:
            raise SpecError("'%s' should be key=value" % (p,))
        k, v = p.split('=', 1)
        k = k.lower()
        if k not in params:
            raise SpecError("effect '%s' has no parameter '%s'" % (name, k))
        t = PARAM_TYPES[k]
        if t == 'color':
            params[k] = color_hex(parse_color(v))
        elif t == 'float':
            try:
                params[k] = float(v)
            except ValueError:
                raise SpecError("%s must be a number, got %r" % (k, v))
            if params[k] < 0:
                raise SpecError("%s must not be negative" % (k,))
        elif t == 'bool':
            params[k] = 1 if v.lower() in ('1', 'true', 'yes', 'on') else 0
        elif t == 'number':
            if v.lower() == 'auto':
                params[k] = 'auto'
            else:
                try:
                    params[k] = float(v)
                except ValueError:
                    raise SpecError("%s must be a number, got %r" % (k, v))
                if params[k] != params[k] or abs(params[k]) == float('inf'):
                    raise SpecError("%s must be a number, got %r" % (k, v))
        elif t == 'colors':
            cs = [x for x in v.split(',') if x.strip()]
            params[k] = ','.join(color_hex(parse_color(x)) for x in cs)
        elif t == 'source':
            if v.lower() in ('progress', 'heat'):
                params[k] = v.lower()
            elif SOURCE_RE.match(v):
                params[k] = v
            else:
                raise SpecError("source must be progress, heat or "
                                "<object>.<field>, got %r" % (v,))
        else:
            if v.lower() not in t:
                raise SpecError("%s must be one of %s" % (k, ', '.join(t)))
            params[k] = v.lower()
    return name, params


def format_spec(name, params):
    """Inverse of parse_spec; only writes values that differ from defaults."""
    out = [name]
    defaults = dict(EFFECTS[name])
    defaults.update(COMMON_PARAMS)
    for k in sorted(params):
        if k in defaults and params[k] != defaults[k]:
            v = params[k]
            if isinstance(v, float) and v == int(v):
                v = int(v)
            out.append('%s=%s' % (k, v))
    return ' '.join(out)


# ---------------------------------------------------------------- helpers

def to_byte(x):
    if x <= 0.0:
        return 0
    if x >= 1.0:
        return 255
    return int(x * 255.0 + 0.5)


def frac(x):
    return x - math.floor(x)


def mix(a, b, v):
    if v <= 0.0:
        return a
    if v >= 1.0:
        return b
    return (a[0] + (b[0] - a[0]) * v, a[1] + (b[1] - a[1]) * v,
            a[2] + (b[2] - a[2]) * v)


def hash01(i, k):
    """Deterministic 0..1 noise; identical in the JS engine."""
    x = (i * 0x9E3779B1 + k * 0x85EBCA77 + 0x27D4EB2F) & 0xffffffff
    x ^= x >> 16
    x = (x * 0x7feb352d) & 0xffffffff
    x ^= x >> 15
    x = (x * 0x846ca68b) & 0xffffffff
    x ^= x >> 16
    return x / 4294967296.0


def hsv(h, s, v):
    h = frac(h) * 6.0
    i = int(math.floor(h))
    f = h - i
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t = v * (1.0 - s * (1.0 - f))
    return ((v, t, p), (q, v, p), (p, v, t),
            (p, q, v), (t, p, v), (v, p, q))[i % 6]


def _period(p, default):
    v = p.get('period', default)
    return v if v > 0.05 else 0.05


# ---------------------------------------------------------------- effects

def fx_off(n, ctx, p):
    return [(0.0, 0.0, 0.0)] * n


def fx_solid(n, ctx, p):
    return [parse_color(p['color'])] * n


def fx_breathe(n, ctx, p):
    v = (1.0 - math.cos(2.0 * math.pi * ctx['t'] / _period(p, 3.0))) * 0.5
    return [mix(parse_color(p['color2']), parse_color(p['color']), v)] * n


def fx_blink(n, ctx, p):
    on = frac(ctx['t'] / _period(p, 1.0)) < 0.5
    return [parse_color(p['color'] if on else p['color2'])] * n


def fx_rainbow(n, ctx, p):
    ph = ctx['t'] / _period(p, 5.0)
    return [hsv(i / float(n) + ph, 1.0, 1.0) for i in range(n)]


def fx_chase(n, ctx, p):
    a, b = parse_color(p['color2']), parse_color(p['color'])
    size = max(1.0, p['size'])
    pos = ctx['t'] / _period(p, 2.0) * n
    out = []
    for i in range(n):
        d = (i - pos) % (2.0 * size)
        out.append(b if d < size else a)
    return out


def fx_comet(n, ctx, p):
    a, b = parse_color(p['color2']), parse_color(p['color'])
    size = p['size'] if p['size'] >= 1.0 else max(2.0, n / 3.0)
    head = frac(ctx['t'] / _period(p, 1.5)) * (n + size)
    out = []
    for i in range(n):
        d = head - i
        if 0.0 <= d < size:
            v = 1.0 - d / size
            out.append(mix(a, b, v * v))
        else:
            out.append(a)
    return out


def fx_scan(n, ctx, p):
    a, b = parse_color(p['color2']), parse_color(p['color'])
    size = max(0.5, p['size'])
    ph = frac(ctx['t'] / _period(p, 2.0))
    tri = ph * 2.0 if ph < 0.5 else 2.0 - ph * 2.0
    x = tri * (n - 1)
    return [mix(a, b, 1.0 - abs(i - x) / size) for i in range(n)]


def fx_twinkle(n, ctx, p):
    a, b = parse_color(p['color2']), parse_color(p['color'])
    period = _period(p, 1.2)
    out = []
    for i in range(n):
        s = ctx['t'] / period + hash01(i, -1)
        k = int(math.floor(s))
        if hash01(i, k) < p['density']:
            out.append(mix(a, b, math.sin(math.pi * (s - k))))
        else:
            out.append(a)
    return out


def _fire_color(h):
    # black -> red -> orange -> yellow
    if h < 0.4:
        return (h / 0.4, 0.0, 0.0)
    if h < 0.8:
        return (1.0, (h - 0.4) / 0.4 * 0.55, 0.0)
    return (1.0, 0.55 + (h - 0.8) / 0.2 * 0.35, (h - 0.8) / 0.2 * 0.15)


def fx_fire(n, ctx, p):
    s = ctx['t'] * 12.0
    k = int(math.floor(s))
    f = s - k
    f = f * f * (3.0 - 2.0 * f)
    out = []
    for i in range(n):
        h0, h1 = hash01(i, k), hash01(i, k + 1)
        out.append(_fire_color(0.35 + 0.65 * (h0 + (h1 - h0) * f)))
    return out


def _num(x):
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    if isinstance(x, (int, float)) and x == x and abs(x) != float('inf'):
        return float(x)
    return None


def default_range(src):
    """min..max a source maps to 0..1 when the spec does not say."""
    if src in ('progress', 'heat'):
        return (0.0, 1.0)
    obj = src.split('.')[0]
    field = src.split('.')[-1]
    if field in ('temperature', 'target'):
        if obj.startswith('extruder'):
            return (20.0, 300.0)
        if obj == 'heater_bed':
            return (20.0, 120.0)
        return (20.0, 80.0)
    if field == 'live_extruder_velocity':
        return (0.0, 10.0)
    return (0.0, 1.0)


def source_range(p, ctx):
    src = p.get('source', 'progress')
    r = (ctx.get('ranges') or {}).get(src) or default_range(src)
    lo = r[0] if p.get('min', 'auto') == 'auto' else p['min']
    hi = r[1] if p.get('max', 'auto') == 'auto' else p['max']
    return float(lo), float(hi)


def source_value(p, ctx):
    """0..1: the effect's source mapped from min..max."""
    src = p.get('source', 'progress')
    if src == 'progress' or src == 'heat':
        raw = _num(ctx.get(src, 0.0))
    else:
        raw = _num((ctx.get('vars') or {}).get(src))
    if raw is None:
        return 0.0
    lo, hi = source_range(p, ctx)
    if hi == lo:
        return 1.0 if raw >= hi else 0.0
    return max(0.0, min(1.0, (raw - lo) / (hi - lo)))


def _stops(p):
    c = p.get('colors')
    if not c:
        return None
    return [parse_color(x) for x in c.split(',')]


def gradient_at(stops, v):
    """Colour at 0..1 along evenly spaced stops."""
    if len(stops) == 1:
        return stops[0]
    x = max(0.0, min(1.0, v)) * (len(stops) - 1)
    i = int(math.floor(x))
    if i >= len(stops) - 1:
        return stops[-1]
    return mix(stops[i], stops[i + 1], x - i)


def value_color(name, p, ctx):
    """The one colour a progress / heat effect stands for right now
    (outputs that cannot draw a bar, e.g. an esp_bridge node)."""
    v = source_value(p, ctx)
    stops = _stops(p)
    if stops is not None:
        return gradient_at(stops, v)
    if name == 'heat':
        return mix(parse_color(p['color2']), parse_color(p['color']), v)
    if p.get('gradient'):
        return mix(parse_color(p['color']), parse_color(p['color2']), v)
    return parse_color(p['color'])


def fx_progress(n, ctx, p):
    off = (0.0, 0.0, 0.0)
    a = parse_color(p['color'])
    v = source_value(p, ctx)
    lit = v * n
    stops = _stops(p)
    if stops is not None:
        out = []
        for i in range(n):
            pos = v if (n == 1 or p.get('gradient')) else i / float(n - 1)
            out.append(mix(off, gradient_at(stops, pos), lit - i))
        return out
    if not p.get('gradient'):
        return [mix(off, a, lit - i) for i in range(n)]
    # gradient: the fill runs from A (start / cold) to B (end / hot) along
    # the strip; a single LED takes the colour of the value itself
    b = parse_color(p['color2'])
    out = []
    for i in range(n):
        pos = v if n == 1 else i / float(n - 1)
        out.append(mix(off, mix(a, b, pos), lit - i))
    return out


def fx_heat(n, ctx, p):
    return [value_color('heat', p, ctx)] * n


def fx_follow(n, ctx, p):
    a, b = parse_color(p['color2']), parse_color(p['color'])
    axis = {'x': 0, 'y': 1, 'z': 2}[p['axis']]
    x = max(0.0, min(1.0, ctx['pos'][axis])) * (n - 1)
    size = max(0.5, p['size'])
    return [mix(a, b, 1.0 - abs(i - x) / size) for i in range(n)]


FX = {
    'off': fx_off, 'solid': fx_solid, 'breathe': fx_breathe,
    'blink': fx_blink, 'rainbow': fx_rainbow, 'chase': fx_chase,
    'comet': fx_comet, 'scan': fx_scan, 'twinkle': fx_twinkle,
    'fire': fx_fire, 'progress': fx_progress, 'heat': fx_heat,
    'follow': fx_follow,
}


def render(name, params, n, ctx):
    """One frame of n LEDs as a list of (r, g, b) floats."""
    if n <= 0:
        return []
    out = FX[name](n, ctx, params)
    if params.get('reverse'):
        out = out[::-1]
    return out


def to_bytes(frame, brightness=1.0):
    """Frame -> bytearray r,g,b,... with a brightness factor."""
    buf = bytearray(len(frame) * 3)
    j = 0
    for r, g, b in frame:
        buf[j] = to_byte(r * brightness)
        buf[j + 1] = to_byte(g * brightness)
        buf[j + 2] = to_byte(b * brightness)
        j += 3
    return buf


def crossfade(old, new, v):
    if old is None or len(old) != len(new) or v >= 1.0:
        return new
    return [mix(a, b, v) for a, b in zip(old, new)]


# Physical LEDs: a 2.2 gamma so dim levels look as dim as on screen
GAMMA = bytearray(int((i / 255.0) ** 2.2 * 255.0 + 0.5) for i in range(256))


def resolve_scene(state, scenes):
    """First state in the fallback chain that has a scene."""
    seen = set()
    while state not in scenes and state in STATE_FALLBACK \
            and state not in seen:
        seen.add(state)
        state = STATE_FALLBACK[state]
    return state if state in scenes else None


# ------------------------------------------------------- custom scenes

def parse_trigger(text):
    """'macro CLEAN_NOZZLE' -> ('macro', 'CLEAN_NOZZLE');
    'heater_bed.temperature > 60' -> ('condition', (ref, op, number))"""
    s = ' '.join(str(text).split())
    low = s.lower()
    if low in ('command', 'print_cancelled', 'extruding', 'retracting'):
        return (low, None)
    if low.startswith('macro ') or low == 'macro':
        name = s[5:].strip().upper()
        if not re.match(r'^[A-Z0-9_]+$', name):
            raise SpecError("trigger 'macro <NAME>' needs a G-code command "
                            "name, got %r" % (s,))
        return ('macro', name)
    m = re.match(r'^(.+?) ?(>=|<=|==|!=|>|<) ?(-?[0-9]*\.?[0-9]+)$', s)
    if m and REF_RE.match(m.group(1)):
        return ('condition', (m.group(1), m.group(2), float(m.group(3))))
    raise SpecError("unknown trigger %r (command, macro <NAME>, "
                    "print_cancelled, extruding, retracting or "
                    "<object>.<field> <op> <number>)" % (s,))


def format_trigger(kind, arg):
    if kind == 'macro':
        return 'macro ' + arg
    if kind == 'condition':
        v = arg[2]
        if v == int(v):
            v = int(v)
        return '%s %s %s' % (arg[0], arg[1], v)
    return kind


def compare(value, op, number):
    if op == '>':
        return value > number
    if op == '>=':
        return value >= number
    if op == '<':
        return value < number
    if op == '<=':
        return value <= number
    if op == '==':
        return value == number
    return value != number


def plan_strips(strips, groups, candidates):
    """Which scene each strip shows.

    strips: [(name, count)] in order; groups: [(name, [member strips])];
    candidates: active scenes [(scene, priority, since, {key: spec})], a key
    being a strip, a group or 'all'. Highest priority wins, ties go to the
    most recently activated. Within a scene a strip's own line beats a
    group line, which beats 'all'. A group line is drawn once over the
    group's LEDs: offset/total place the strip in that frame.
    -> {strip: (scene, key, spec, offset, total)}; scene None = off
    """
    counts = dict(strips)
    order = sorted(candidates, key=lambda c: (-c[1], -c[2]))
    out = {}
    for name, count in strips:
        hit = None
        for scene, _pri, _since, items in order:
            if name in items:
                hit = (scene, name, items[name], 0, count)
            else:
                for g, members in groups:
                    if g in items and name in members:
                        k = members.index(name)
                        off = sum(counts.get(m, 0) for m in members[:k])
                        total = sum(counts.get(m, 0) for m in members)
                        hit = (scene, g, items[g], off, total)
                        break
                if hit is None and 'all' in items:
                    hit = (scene, 'all', items['all'], 0, count)
            if hit is not None:
                break
        out[name] = hit or (None, None, None, 0, count)
    return out


# ------------------------------------------------- load estimate (MCU)

# WS2812: 24 bits of 1.25 us per LED with interrupts mostly masked; the
# MCU's command loop is stuck for that long on every update.
US_PER_BYTE = 10.0
# Share of MCU time LED updates may take. A 100 LED strip on the step MCU
# ends up at ~10 fps, a 3 LED toolhead at the frame cap.
MCU_BUDGET = 0.03
MCU_FPS_CAP = 25.0
NET_FPS = 40.0


def mcu_fps(nbytes):
    if nbytes <= 0:
        return MCU_FPS_CAP
    return max(1.0, min(MCU_FPS_CAP,
                        MCU_BUDGET / (nbytes * US_PER_BYTE * 1e-6)))
