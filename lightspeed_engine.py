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
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import math

__version__ = "0.1.3"

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
    # The unlit part is off.
    'progress': {'color': 'ffffff', 'color2': 'ff0000',
                 'source': 'progress', 'gradient': 0},
    'heat':     {'color': 'ff3000', 'color2': '0030ff'},
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
    'density': 'float', 'source': ('progress', 'heat'),
    'axis': ('x', 'y', 'z'), 'reverse': 'bool',
}


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
    for p in parts[1:]:
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


def fx_progress(n, ctx, p):
    off = (0.0, 0.0, 0.0)
    a = parse_color(p['color'])
    v = ctx['heat'] if p['source'] == 'heat' else ctx['progress']
    v = max(0.0, min(1.0, v))
    lit = v * n
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
    return [mix(parse_color(p['color2']), parse_color(p['color']),
                max(0.0, min(1.0, ctx['heat'])))] * n


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
