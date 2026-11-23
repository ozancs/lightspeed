#!/usr/bin/env python3
"""Move a klipper-led_effect setup to Lightspeed.

Reads printer.cfg and every file it includes, finds the [led_effect ...]
sections and writes a lightspeed.cfg:

  * one [lightspeed strip] per group of LEDs the effects share (strips never
    overlap), and a [lightspeed group] where one effect spans several strips
  * one [lightspeed scene <name>] with `trigger: command` per led_effect, so
    SET_LED_EFFECT EFFECT=<name> / STOP_LED_EFFECTS in your macros keep
    working unchanged (Lightspeed answers those commands itself)
  * autostart effects become the built-in idle scene, run_on_error effects
    the built-in error scene

    python3 tools/migrate_led_effect.py --dry-run   show what it would do
    python3 tools/migrate_led_effect.py             write lightspeed.cfg only
    python3 tools/migrate_led_effect.py --apply     also switch over: comment
        out the [led_effect] sections (and an include of a file that holds
        nothing else) and add [include lightspeed.cfg] to printer.cfg

Every changed file is backed up as <file>.lightspeed-backup-<time> first.
After --apply the whole config is read again the way Klipper reads it; on
any problem every file is put back from its backup.

Exit codes: 0 done, 1 nothing to do (--detect: no led_effect found),
2 error, 3 --apply needs a manual merge (only a separate draft was written).

This file may be distributed under the terms of the GNU GPLv3 license.
"""

import argparse
import colorsys
import configparser
import fnmatch
import glob
import io
import os
import re
import shutil
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
try:
    import lightspeed_engine as engine
except ImportError:  # pragma: no cover
    sys.stderr.write("migrate_led_effect: run it from the Lightspeed folder "
                     "(lightspeed_engine.py not found)\n")
    sys.exit(2)

MARK = '[lightspeed]'
SECTCRE = configparser.RawConfigParser.SECTCRE
REF_RE = re.compile(r'''printer\s*\[\s*['"]led_effect\b''', re.I)
SET_RE = re.compile(r'\bSET_LED_EFFECT\b([^\n]*)', re.I)
STOP_RE = re.compile(r'\bSTOP_LED_EFFECTS\b([^\n]*)', re.I)
NAME_RE = re.compile(r'^[a-z0-9_]+$')
CHAIN_KINDS = ('neopixel', 'dotstar')
STRIP_OPTIONS = ('output', 'leds', 'count', 'start', 'place', 'spacing',
                 'brightness', 'color')


class MigrateError(Exception):
    pass


######################################################################
# Reading the config the way Klipper does
######################################################################

def read_file(path):
    with open(path, 'r', encoding='utf-8', newline='') as f:
        return f.read()


def strip_comment(line):
    """Klipper drops everything after the first '#' on every line."""
    pos = line.find('#')
    return line if pos < 0 else line[:pos]


def header_of(line):
    mo = SECTCRE.match(strip_comment(line))
    return mo.group('header').strip() if mo else None


def resolve_include(src, spec):
    pat = os.path.join(os.path.dirname(src), spec.strip())
    names = glob.glob(pat)
    if not names and not glob.has_magic(pat):
        raise MigrateError("Include file '%s' does not exist" % (pat,))
    return [os.path.abspath(n) for n in sorted(names)]


def new_parser():
    return configparser.RawConfigParser(strict=False,
                                        inline_comment_prefixes=(';', '#'))


def klipper_load(printer_cfg):
    """Same steps as Klipper's configfile: -> (parser, files read)."""
    cp = new_parser()
    files = []

    def flush(buf, fn):
        try:
            cp.read_string('\n'.join(buf), fn)
        except configparser.Error as e:
            raise MigrateError("%s: %s" % (fn, str(e).splitlines()[0]))

    def parse(fn, visited):
        fn = os.path.abspath(fn)
        if fn in visited:
            raise MigrateError("Recursive include of config file '%s'" % fn)
        visited.add(fn)
        if fn not in files:
            files.append(fn)
        buf = []
        for line in read_file(fn).split('\n'):
            line = strip_comment(line)
            mo = SECTCRE.match(line)
            head = mo and mo.group('header')
            if head and head.startswith('include '):
                flush(buf, fn)
                buf = []
                for inc in resolve_include(fn, head[8:]):
                    parse(inc, visited)
            else:
                buf.append(line)
        flush(buf, fn)
        visited.remove(fn)

    if not os.path.isfile(printer_cfg):
        raise MigrateError("%s not found" % printer_cfg)
    parse(printer_cfg, set())
    return cp, files


class CfgFile(object):
    """One config file as lines, split into sections and include lines."""

    def __init__(self, path):
        self.path = path
        self.text = read_file(path)
        self.lines = self.text.splitlines(True)
        self.nl = '\r\n' if '\r\n' in self.text else '\n'
        self.items = []
        cur = None
        for i, line in enumerate(self.lines):
            if line[:1].isspace():
                continue
            head = header_of(line)
            if head is None:
                continue
            if cur is not None:
                cur['end'] = i
            if head.startswith('include '):
                self.items.append({'kind': 'include', 'name': head[8:].strip(),
                                   'start': i, 'end': i + 1})
                cur = None
            else:
                cur = {'kind': 'section', 'name': ' '.join(head.split()),
                       'start': i, 'end': len(self.lines)}
                self.items.append(cur)
        for it in self.items:
            if it['kind'] == 'section':
                it['options'] = self._options(it)

    def _options(self, it):
        cp = new_parser()
        body = [strip_comment(l.rstrip('\r\n'))
                for l in self.lines[it['start']:it['end']]]
        try:
            cp.read_string('\n'.join(body))
        except configparser.Error:
            return {}
        sec = cp.sections()
        return dict(cp.items(sec[0])) if sec else {}

    def sections(self):
        return [it for it in self.items if it['kind'] == 'section']

    def section_text(self, it):
        return ''.join(self.lines[it['start']:it['end']]).rstrip()


######################################################################
# Effect specs (today's engine params plus the value-effect params)
######################################################################

VALUE_KEYS = ('source', 'min', 'max', 'colors')
SOURCE_RE = re.compile(r'^[a-z_][a-z0-9_]*(\.[a-z_][a-z0-9_]*)?$')


def _fmt(v):
    if isinstance(v, float):
        v = round(v, 2)
        if v == int(v):
            v = int(v)
    return str(v)


def make_spec(name, params, extra=()):
    full = dict(engine.EFFECTS[name])
    full.update(engine.COMMON_PARAMS)
    for k, v in params.items():
        full[k] = round(v, 2) if isinstance(v, float) else v
    spec = engine.format_spec(name, full)
    for k, v in extra:
        spec += ' %s=%s' % (k, _fmt(v))
    return spec


def check_spec(text):
    """Error text or None. Value-effect keys are checked here, everything
    else by lightspeed_engine.parse_spec."""
    parts = str(text).split()
    if not parts:
        return 'empty effect'
    name = parts[0].lower()
    keep = [parts[0]]
    for p in parts[1:]:
        k, _, v = p.partition('=')
        k = k.lower()
        if k == 'source' and v.lower() in ('progress', 'heat'):
            keep.append(p)
        elif k in VALUE_KEYS:
            if name not in ('progress', 'heat'):
                return "'%s' only works with progress and heat" % k
            if k == 'source' and not SOURCE_RE.match(v):
                return "source must be progress, heat or object.field"
            if k in ('min', 'max'):
                try:
                    float(v)
                except ValueError:
                    return '%s must be a number' % k
            if k == 'colors':
                try:
                    stops = [engine.parse_color(c) for c in v.split(',')]
                except engine.SpecError as e:
                    return str(e)
                if not stops:
                    return 'colors is empty'
        else:
            keep.append(p)
    try:
        engine.parse_spec(' '.join(keep))
    except engine.SpecError as e:
        return str(e)
    return None


TRIGGER_WORDS = ('command', 'print_cancelled', 'extruding', 'retracting')
COND_RE = re.compile(r'^[a-z_][a-z0-9_ ]*(\.[a-z_][a-z0-9_]*)?\s*'
                     r'(<=|>=|==|!=|<|>)\s*-?\d+(\.\d+)?$', re.I)


def check_trigger(text):
    t = ' '.join(str(text).split())
    if t.lower() in TRIGGER_WORDS:
        return None
    if re.match(r'^macro\s+\S+$', t, re.I):
        return None
    if COND_RE.match(t):
        return None
    return "trigger '%s' is not command, macro <NAME>, print_cancelled, " \
        "extruding, retracting or '<object>[.<field>] <op> <number>'" % t


######################################################################
# led_effect layer -> Lightspeed effect
######################################################################

def _num(text, default):
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def parse_palette(text):
    out = []
    for grp in re.findall(r'\(([^()]*)\)', text):
        vals = [float(x) for x in grp.split(',') if x.strip()]
        if not vals:
            continue
        scale = 255.0 if max(vals) > 1.0 else 1.0
        vals = [v / scale for v in vals] + [0.0] * 4
        w = vals[3]                     # RGBW: white folded into r, g, b
        out.append(tuple(_clamp(c + w, 0.0, 1.0) for c in vals[:3]))
    return out


def hexc(c):
    return engine.color_hex(c)


def detemplate(raw):
    """'{params.RATE|default(3)}' -> '3' (the layer's default values)."""
    return re.sub(r'\{\{?[^{}]*?default\(\s*([^()]*?)\s*\)[^{}]*\}?\}',
                  lambda m: m.group(1).strip('\'"'), raw)


def parse_layers(text):
    layers = []
    for line in text.split('\n'):
        parms = line.split()
        if not parms:
            continue
        if len(parms) < 4:
            raise ValueError("layer '%s' needs: effect rate cutoff blend "
                             "palette" % line.strip())
        layers.append({'effect': parms[0].lower(), 'rate': _num(parms[1], 0.),
                       'cutoff': _num(parms[2], 0.), 'blend': parms[3].lower(),
                       'palette': parse_palette(''.join(parms[4:]))})
    return layers


def _hue_spread(colors):
    hsv = [colorsys.rgb_to_hsv(*c) for c in colors]
    hues = sorted(h for h, s, v in hsv if s > 0.2 and v > 0.02)
    if len(hues) < 3:
        return 0.0
    gaps = [hues[i + 1] - hues[i] for i in range(len(hues) - 1)]
    gaps.append(1.0 - hues[-1] + hues[0])
    return 1.0 - max(gaps)


def heater_source(opts):
    """'extruder' -> ('extruder', 250); None when it cannot be a source."""
    h = (opts.get('heater') or '').strip().strip('"\'')
    if not h or not SOURCE_RE.match(h + '.temperature'):
        return None, None
    top = 250.0 if h.startswith('extruder') else 100.0 \
        if h == 'heater_bed' else 60.0
    return h, top


BLACK = (0.0, 0.0, 0.0)
NO_INPUT = {'analogpin': 'reads an analog pin',
            'switchbutton': 'reads a button', 'togglebutton': 'reads a button',
            'flashbutton': 'reads a button'}


def map_layer(layer, n, fps, opts):
    """-> (spec, notes) or raises ValueError(reason)."""
    eff, rate, cut = layer['effect'], layer['rate'], layer['cutoff']
    pal = layer['palette'] or [(1.0, 1.0, 1.0)]
    c0 = hexc(pal[0])
    c1 = hexc(pal[1]) if len(pal) > 1 else None
    last = hexc(pal[-1])
    many = len(pal) > 1
    notes = []
    speed = abs(rate) * fps          # LEDs per second (comet, chase)
    if eff in NO_INPUT:
        raise ValueError("%s %s; Lightspeed has no such effect"
                         % (eff, NO_INPUT[eff]))
    if eff == 'static':
        if pal[0] == BLACK and not many:
            return make_spec('off', {}), notes
        if many:
            notes.append('static with %d colours: first colour used'
                         % len(pal))
        return make_spec('solid', {'color': c0}), notes
    if eff == 'linearfade':
        if not many:
            return make_spec('solid', {'color': c0}), notes
        if len(pal) > 2:
            notes.append('fade through %d colours: first two used' % len(pal))
        return make_spec('breathe', {'color': c0, 'color2': c1, 'period':
                                     max(0.2, rate or 3.0)}), notes
    if eff == 'breathing':
        if many:
            notes.append('breathes %d colours in turn: first used' % len(pal))
        return make_spec('breathe', {'color': c0,
                                     'period': max(0.2, rate or 3.0)}), notes
    if eff == 'blink':
        if many:
            notes.append('blinks %d colours in turn: first used' % len(pal))
        if cut and abs(cut - 0.5) > 0.1:
            notes.append('on-time %.0f%% becomes 50%%' % (cut * 100))
        return make_spec('blink', {'color': c0,
                                   'period': max(0.1, rate or 1.0)}), notes
    if eff == 'strobe':
        notes.append('strobe shown as blink')
        return make_spec('blink', {'color': c0, 'period': _clamp(
            1.0 / rate if rate > 0 else 1.0, 0.1, 10.0)}), notes
    if eff == 'twinkle':
        if many:
            notes.append('random palette colours: first used')
        p = _clamp(rate, 0.0, 254.0) / 256.0
        dens = 1.0 - (1.0 - p) ** (fps * 1.2)
        return make_spec('twinkle', {'color': c0, 'density': round(
            _clamp(dens, 0.05, 0.9), 2)}), notes
    if eff == 'gradient':
        period = _clamp(1.0 / abs(rate), 0.5, 60.0) if rate else 60.0
        if _hue_spread(pal) >= 0.5:
            if max(max(c) for c in pal) < 0.9:
                notes.append('rainbow runs at full brightness (the palette '
                             'was dimmer): lower the strip brightness if '
                             'needed')
            return make_spec('rainbow', {'period': period, 'reverse':
                                         1 if rate < 0 else 0}), notes
        notes.append('gradient of close colours shown as breathe %s/%s'
                     % (c0, last))
        return make_spec('breathe', {'color': c0, 'color2': last,
                                     'period': period}), notes
    if eff == 'pattern':
        step = cut if cut >= 1 else 1.0
        return make_spec('chase', {'color': c0, 'color2': c1 or '000000',
                                   'period': _clamp(n * (rate or 1.0) / step,
                                                    0.2, 30.0)}), notes
    if eff in ('comet', 'chase'):
        if not rate:
            notes.append('%s with rate 0: still' % eff)
            return make_spec('solid', {'color': c0}), notes
        size = max(2.0, n / 3.0) if eff == 'comet' else 0.0
        return make_spec(eff, {'color': c0, 'period': _clamp(
            (n + size) / speed, 0.2, 30.0),
            'reverse': 1 if rate < 0 else 0}), notes
    if eff == 'cylon':
        return make_spec('scan', {'color': c0, 'period': max(
            0.2, 2.0 * (rate or 1.0))}), notes
    if eff in ('heater', 'temperature', 'heatergauge', 'temperaturegauge'):
        src, top = heater_source(opts)
        if eff == 'heater':
            stops = pal if many else pal * 2
            lo, hi = max(0.0, rate), top
            if src:
                notes.append('led_effect ran to the live target; max=%g is a '
                             'guess' % hi)
        elif eff == 'temperature':
            stops = pal if many else [BLACK] + pal
            lo, hi = rate, cut if cut > rate else rate + 1.0
        elif eff == 'heatergauge':
            stops, lo, hi = pal[:1], 0.0, top
            notes.append('gauge shown as a bar; max=%g is a guess' % (hi or 0))
        else:
            stops, lo, hi = pal[:1], rate, cut if cut > rate else rate + 1.0
            notes.append('gauge shown as a bar')
        if not src:
            notes.append("heater '%s' cannot be a source: follows all heaters"
                         % (opts.get('heater') or '?'))
            if eff in ('heater', 'temperature'):
                return make_spec('heat', {'color': hexc(stops[-1]),
                                          'color2': hexc(stops[0])}), notes
            return make_spec('progress', {'color': c0, 'source': 'heat'}), \
                notes
        if eff in ('heater', 'temperature'):
            return make_spec('heat', {}, [
                ('source', src + '.temperature'), ('min', lo), ('max', hi),
                ('colors', ','.join(hexc(c) for c in stops))]), notes
        return make_spec('progress', {'color': c0}, [
            ('source', src + '.temperature'), ('min', lo),
            ('max', hi)]), notes
    if eff == 'fire':
        return make_spec('fire', {}), notes
    if eff == 'heaterfire':
        notes.append('fire does not depend on the heater here')
        return make_spec('fire', {}), notes
    if eff == 'progress':
        notes.append('progress dot becomes a progress bar')
        return make_spec('progress', {'color': c0},
                         [('source', 'progress')]), notes
    if eff in ('stepper', 'steppercolor'):
        axis = (opts.get('stepper') or 'x').strip().lower()[-1:]
        if axis not in ('x', 'y', 'z'):
            axis = 'x'
        return make_spec('follow', {'color': c0, 'axis': axis}), notes
    if eff == 'homing':
        notes.append('endstop flash shown as a scan')
        return make_spec('scan', {'color': c0}), notes
    raise ValueError("unknown led_effect layer '%s'" % eff)


ANIMATED_BG = ('breathe', 'blink', 'chase', 'comet', 'scan', 'twinkle',
               'follow')


def map_effect(opts, n):
    """Picks the layer that matters and maps it.
    -> {'spec', 'notes', 'layer', 'exact'} ; raises ValueError."""
    raw = detemplate(opts.get('layers', ''))
    if '{' in raw:
        raise ValueError('layers use templates without defaults')
    layers = parse_layers(raw)
    if not layers:
        raise ValueError('no layers')
    fps = _clamp(_num(opts.get('frame_rate'), 24.0), 1.0, 60.0)
    useful = [l for l in layers if not (l['effect'] == 'static' and all(
        c == BLACK for c in l['palette']))] or layers
    main = next((l for l in useful if l['effect'] != 'static'), useful[0])
    spec, notes = map_layer(main, n, fps, opts)
    if len(layers) > 1:
        bg = next((l for l in layers if l is not main and l['effect'] ==
                   'static' and l['palette'] and l['palette'][0] != BLACK),
                  None)
        name = spec.split()[0]
        if bg is not None and name in ANIMATED_BG and 'color2=' not in spec:
            spec += ' color2=%s' % hexc(bg['palette'][0])
            notes.append('static layer used as background colour')
        notes.append('%d layers: blended look approximated by the "%s" layer'
                     % (len(layers), main['effect']))
    err = check_spec(spec)
    if err:
        raise ValueError('internal: %s -> %s' % (spec, err))
    return {'spec': spec, 'notes': notes, 'layer': main['effect'],
            'exact': not notes}


def fallback_spec(opts):
    """A solid in the effect's first colour, so its scene still exists."""
    pal = parse_palette(opts.get('layers', ''))
    return make_spec('solid', {'color': hexc(pal[0]) if pal else 'ffffff'})


######################################################################
# Names and places
######################################################################

STATE_WORDS = set((
    'shutdown', 'error', 'err', 'critical', 'fault', 'failure', 'fail',
    'alert', 'panic', 'complete', 'completed', 'done', 'finish', 'finished',
    'end', 'pause', 'paused', 'home', 'homing', 'homed', 'probe', 'probing',
    'level', 'leveling', 'levelling', 'mesh', 'meshing', 'qgl', 'tilt',
    'calibrate', 'calibrating', 'calibration', 'heat', 'heating', 'preheat',
    'warmup', 'heater', 'print', 'printing', 'progress', 'idle', 'standby',
    'ready', 'startup', 'default', 'busy', 'cleaning', 'cooling', 'part',
    'z', 'error', 'on', 'off'))
GENERIC_WORDS = set(('led', 'leds', 'effect', 'effects', 'status', 'set',
                     'color', 'colour', 'state', 'fx', 'start', 'stop'))

PLACE_WORDS = [
    ('toolhead_logo', ('logo',)),
    ('nozzle', ('nozzle', 'nozzles')),
    ('toolhead_logo', ('hotend', 'toolhead', 'stealthburner', 'sb', 'head',
                       'extruder', 'tool', 'afterburner')),
    ('under_bed', ('underbed', 'under')),
    ('bed_skirt', ('bed',)),
    ('door_top', ('door',)),
    ('x_gantry', ('gantry', 'xgantry')),
    ('top_ring', ('chamber', 'case', 'caselight', 'enclosure', 'ceiling',
                  'ring', 'panel', 'cabinet')),
    ('top_rear', ('rear', 'back')),
    ('top_left', ('left',)),
    ('top_right', ('right',)),
    ('top_front', ('top', 'front')),
    ('frame_bottom', ('bottom', 'base', 'floor', 'frame', 'skirt')),
]


def tokens(name):
    return [x for x in re.split(r'[^a-z0-9]+', name.lower()) if x]


def guess_place(names):
    for name in names:
        for t in tokens(name):
            for place, words in PLACE_WORDS:
                if t in words or any(t.startswith(w) or t.endswith(w)
                                     for w in words if len(w) >= 4):
                    return place, t
    return 'top_front', None


def clean_name(name):
    return re.sub(r'[^a-z0-9_]+', '_', name.lower()).strip('_')


def unique(name, used):
    base, k = name, 2
    while name in used:
        name = '%s_%d' % (base, k)
        k += 1
    used.add(name)
    return name


def leds_text(leds):
    """0-based list -> '1-3,5' (1-based, runs keep their direction)."""
    out = []
    i = 0
    while i < len(leds):
        j = i
        if i + 1 < len(leds) and abs(leds[i + 1] - leds[i]) == 1:
            step = leds[i + 1] - leds[i]
            while j + 1 < len(leds) and leds[j + 1] - leds[j] == step:
                j += 1
        out.append('%d-%d' % (leds[i] + 1, leds[j] + 1) if j > i
                   else '%d' % (leds[i] + 1))
        i = j + 1
    return ','.join(out)


def parse_led_list(spec):
    """led_effect index text '(1-3,5)' / '(16-9)' -> 0-based list."""
    out = []
    for part in spec.strip().strip('()').split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = [int(x) for x in part.split('-', 1)]
            out.extend(range(a - 1, b) if b >= a
                       else reversed(range(b - 1, a)))
        else:
            out.append(int(part) - 1)
    return out


######################################################################
# The migration model
######################################################################

class Migration(object):
    def __init__(self, config_dir, printer_cfg='printer.cfg'):
        self.config_dir = os.path.abspath(os.path.expanduser(config_dir))
        self.printer_cfg = os.path.join(self.config_dir, printer_cfg)
        self.cp, paths = klipper_load(self.printer_cfg)
        self.files = [CfgFile(p) for p in paths]
        self.warnings = []
        self.manual = []           # things the user must fix by hand
        self.effects = []
        self.chains = {}           # 'neopixel sb_leds' -> chain_count
        self.strips = []
        self.groups = []           # {'name', 'strips'}
        self.scenes = []           # {'name', 'effect', 'key', 'spec', ...}
        self.builtin = {}          # 'idle'/'error' -> [(key, spec, why)]
        self.unplaced = []         # (effect, reason): no scene at all
        self.calls = {}            # effect name -> [section names]
        self._collect()
        if self.effects:
            self._map()

    def _collect(self):
        for cf in self.files:
            for it in cf.sections():
                parts = it['name'].split()
                kind = parts[0].lower()
                if kind in CHAIN_KINDS and len(parts) == 2:
                    self.chains[it['name']] = max(
                        1, int(_num(it['options'].get('chain_count'), 1)))
                if kind == 'led_effect':
                    self.effects.append({
                        'name': ' '.join(parts[1:]), 'file': cf, 'item': it,
                        'options': it['options'], 'targets': []})
        names = set(e['name'].lower() for e in self.effects)
        for cf in self.files:
            for it in cf.sections():
                if it['name'].split()[0].lower() == 'led_effect':
                    continue
                for i in range(it['start'], it['end']):
                    line = strip_comment(cf.lines[i])
                    where = '%s line %d' % (os.path.basename(cf.path), i + 1)
                    if REF_RE.search(line):
                        self.manual.append(
                            "%s ([%s]) reads a led_effect object, which is "
                            "gone after the switch: %s"
                            % (where, it['name'], line.strip()))
                    for mo in SET_RE.finditer(line):
                        em = re.search(r'EFFECT\s*=\s*"?(\{|[^\s"{}%]+)',
                                       mo.group(1), re.I)
                        if not em:
                            continue
                        ename = em.group(1).lower()
                        if ename == '{':
                            self.warnings.append(
                                "%s: SET_LED_EFFECT with a computed name "
                                "(%s): it must match one of the scene names"
                                % (where, mo.group(0).strip()))
                            continue
                        self.calls.setdefault(ename, []).append(it['name'])
                        if ename not in names:
                            self.manual.append(
                                "%s ([%s]) starts led_effect '%s', which "
                                "does not exist" % (where, it['name'],
                                                    em.group(1)))
                    for mo in STOP_RE.finditer(line):
                        if re.search(r'\bLEDS\s*=', mo.group(1), re.I):
                            self.warnings.append(
                                "%s: STOP_LED_EFFECTS LEDS=... : check that "
                                "Lightspeed's STOP_LED_EFFECTS does what you "
                                "want there" % where)

    # --------------------------------------------------------------- map
    def _map(self):
        for idx, eff in enumerate(self.effects):
            eff['id'] = idx
            bad = []
            for line in eff['options'].get('leds', '').split('\n'):
                parms = line.split()
                if not parms:
                    continue
                kind, _, cname = parms[0].partition(':')
                key = '%s %s' % (kind.lower(), cname)
                if kind.lower() not in CHAIN_KINDS:
                    bad.append("'%s' is not a neopixel/dotstar chain"
                               % parms[0])
                    continue
                if key not in self.chains:
                    bad.append('no [%s] section found' % key)
                    continue
                count = self.chains[key]
                try:
                    leds = parse_led_list(''.join(parms[1:])) if parms[1:] \
                        else list(range(count))
                except ValueError:
                    bad.append("bad LED list '%s'" % line.strip())
                    continue
                keep = []
                for i in leds:
                    if i < 0 or i >= count:
                        self.warnings.append(
                            'led_effect %s: LED %d is outside [%s] '
                            '(chain_count %d), skipped'
                            % (eff['name'], i + 1, key, count))
                    elif (key, i) not in keep:
                        keep.append((key, i))
                for k in keep:
                    if k not in eff['targets']:
                        eff['targets'].append(k)
            if not eff['targets']:
                self.unplaced.append((eff, '; '.join(bad) or 'no LEDs'))
                continue
            for b in bad:
                self.warnings.append('led_effect %s: %s (that part skipped)'
                                     % (eff['name'], b))
            try:
                eff['map'] = map_effect(eff['options'], len(eff['targets']))
            except (ValueError, engine.SpecError) as e:
                eff['map'] = {'spec': fallback_spec(eff['options']),
                              'notes': ['could not map (%s): shows its first '
                                        'colour' % e],
                              'layer': '?', 'exact': False, 'fallback': True}
        live = [e for e in self.effects if 'map' in e]
        self._build_strips(live)
        self._build_scenes(live)
        for eff, why in self.unplaced:
            for sec in sorted(set(self.calls.get(eff['name'].lower(), []))):
                self.manual.append(
                    "[%s] starts led_effect '%s', which gets no scene (%s): "
                    "remove that SET_LED_EFFECT or add the scene by hand"
                    % (sec, eff['name'], why))

    def _build_strips(self, live):
        # Split every chain into pieces that the same set of effects drive:
        # Lightspeed strips must not share LEDs.
        sig = {}
        first = []
        for eff in live:
            for t in eff['targets']:
                if t not in sig:
                    sig[t] = []
                    first.append(t)
                if eff['id'] not in sig[t]:
                    sig[t].append(eff['id'])
        groups, order = {}, []
        for t in first:
            g = (t[0], tuple(sig[t]))
            if g not in groups:
                groups[g] = []
                order.append(g)
            groups[g].append(t[1])
        by_id = dict((e['id'], e) for e in live)
        used = set(['all'])
        for chain, ids in order:
            leds = groups[(chain, ids)]
            effs = [by_id[i] for i in ids]
            lset = set(leds)
            only = [e for e in effs
                    if all(k == chain and i in lset for k, i in e['targets'])]
            name = self._strip_name(chain, leds, only, used)
            place, word = guess_place([name, chain.split()[1]] +
                                      [e['name'] for e in effs])
            self.strips.append({
                'name': name, 'chain': chain, 'leds': leds, 'effects': effs,
                'place': place, 'place_word': word,
                'whole': len(leds) == self.chains[chain]})
        self.used_names = used

    def _strip_name(self, chain, leds, only, used):
        common = None
        for e in only:
            t = [x for x in tokens(e['name'])
                 if x not in STATE_WORDS and x not in GENERIC_WORDS]
            common = t if common is None else [x for x in common if x in t]
        name = '_'.join(common or [])
        if not name or name.isdigit():
            name = chain.split()[1]
            if len(leds) != self.chains[chain]:
                name += '_' + leds_text(leds).replace(',', '_') \
                    .replace('-', '_')
        name = clean_name(name) or 'strip'
        if name == 'all':
            name = 'all_leds'
        return unique(name, used)

    def _key_for(self, eff):
        """Strip or group name an effect's scene line uses, with reverse."""
        strip_of = {}
        for s in self.strips:
            for i in s['leds']:
                strip_of[(s['chain'], i)] = s
        seq = []
        for t in eff['targets']:
            s = strip_of[t]
            if s not in seq:
                seq.append(s)
        notes = []
        if len(seq) == 1:
            s = seq[0]
            mine = [i for k, i in eff['targets'] if k == s['chain']]
            if len(mine) > 1 and mine == s['leds'][::-1]:
                return s['name'], 1, notes
            return s['name'], 0, notes
        flat = [(s['chain'], i) for s in seq for i in s['leds']]
        if flat != eff['targets']:
            notes.append('LED order differs a little from led_effect (the '
                         'strips %s run one after the other)'
                         % ', '.join(s['name'] for s in seq))
        names = tuple(s['name'] for s in seq)
        for g in self.groups:
            if g['strips'] == names:
                return g['name'], 0, notes
        chains = set(s['chain'] for s in seq)
        if len(chains) == 1 and sum(len(s['leds']) for s in seq) == \
                self.chains[seq[0]['chain']]:
            gname = clean_name(seq[0]['chain'].split()[1])
        else:
            gname = '_'.join(names)[:40].strip('_')
        gname = unique(gname or 'group', self.used_names)
        self.groups.append({'name': gname, 'strips': names})
        return gname, 0, notes

    def _build_scenes(self, live):
        used = set(engine.STATES) | set(['all'])
        for eff in live:
            key, rev, notes = self._key_for(eff)
            spec = eff['map']['spec']
            if rev:
                spec = re.sub(r'\s*\breverse=\d', '', spec) + (
                    '' if 'reverse=1' in eff['map']['spec'] else ' reverse=1')
            want = clean_name(eff['name']) or 'effect'
            name = want
            if want in used:
                name = unique(want + '_fx', used)
            else:
                used.add(want)
            # Lightspeed finds a scene from SET_LED_EFFECT EFFECT=<old name>
            # case-blind, with its name rules applied and with '_fx' added,
            # so only other renames (a clash between two cleaned names) need
            # the macros changed
            if name not in (want, want + '_fx'):
                self.manual.append(
                    "led_effect '%s' is called scene '%s' (Lightspeed name "
                    "rules): change SET_LED_EFFECT EFFECT=%s in your macros "
                    "to EFFECT=%s" % (eff['name'], name, eff['name'], name))
            eff['scene'] = {'name': name, 'key': key, 'spec': spec,
                            'notes': eff['map']['notes'] + notes}
            self.scenes.append(eff['scene'])
        # built-in scenes: autostart -> idle, run_on_error -> error
        for state, opt in (('idle', 'autostart'), ('error', 'run_on_error')):
            covered = set()
            lines = []
            for eff in live:
                if str(eff['options'].get(opt, '')).strip().lower() not in (
                        'true', '1', 'yes'):
                    continue
                mine = set(eff['targets'])
                if mine & covered:
                    self.warnings.append(
                        "%s: led_effect '%s' (%s) overlaps an earlier one; "
                        "it stays a command scene only" % (state, eff['name'],
                                                          opt))
                    continue
                covered |= mine
                lines.append((eff['scene']['key'], eff['scene']['spec'],
                              'from %s (%s)' % (eff['name'], opt)))
            if state == 'error' and not lines:
                continue
            for s in self.strips:
                if not any((s['chain'], i) in covered for i in s['leds']):
                    lines.append((s['name'], 'off',
                                  'no %s effect on this strip' % opt))
            self.builtin[state] = lines

    # ------------------------------------------------------------ output
    def config_text(self, stamp, global_section=None):
        out = ['# Lightspeed config made from led_effect by '
               'tools/migrate_led_effect.py (%s)' % stamp,
               '# Every led_effect is a scene below with "trigger: command": '
               'SET_LED_EFFECT EFFECT=<name>',
               '# and STOP_LED_EFFECTS in your macros keep working. Check it '
               'on the Lightspeed page.',
               '# Colours are rrggbb without #. Reference: docs/CONTRACT.md',
               '',
               global_section if global_section is not None else
               '[lightspeed]\n# led_effect colours already hold their brightness\nbrightness: 1.0',
               '']
        for s in self.strips:
            out.append('# from led_effect: %s'
                       % ', '.join(e['name'] for e in s['effects']))
            out.append('# place guessed from %s' % (
                "'%s'" % s['place_word'] if s['place_word']
                else 'nothing (default)'))
            out += ['[lightspeed strip %s]' % s['name'],
                    'output: %s' % s['chain'],
                    'leds: %s' % leds_text(s['leds']),
                    'place: %s' % s['place'], '']
        for g in self.groups:
            out += ['# one effect runs over these strips in this order',
                    '[lightspeed group %s]' % g['name'],
                    'strips: %s' % ', '.join(g['strips']), '']
        for state in ('idle', 'error'):
            if state not in self.builtin:
                continue
            out.append('[lightspeed scene %s]' % state)
            for key, spec, why in self.builtin[state]:
                out += ['# ' + why, '%s: %s' % (key, spec)]
            out.append('')
        for sc in self.scenes:
            for n in sc['notes']:
                out.append('# approx: %s' % n)
            out += ['[lightspeed scene %s]' % sc['name'], 'trigger: command',
                    '%s: %s' % (sc['key'], sc['spec']), '']
        rest = list(self.unplaced) + [
            (e, e['map']['notes'][0]) for e in self.effects
            if e.get('map', {}).get('fallback')]
        if rest:
            out.append('# ---- led_effect sections that could not be '
                       'migrated as they were (original text) ----')
            for eff, why in rest:
                out += ['#', '# %s: %s' % (eff['name'], why)]
                for line in eff['file'].section_text(eff['item']).split('\n'):
                    out.append(('# ' + line.rstrip('\r')).rstrip())
            out.append('')
        return '\n'.join(out)

    def report(self):
        r = ['led_effect migration report',
             '  config: %s' % self.printer_cfg,
             '  files read: %s' % ', '.join(
                 os.path.relpath(f.path, self.config_dir) for f in self.files),
             '  led_effect sections: %d   strips: %d   groups: %d   '
             'scenes: %d' % (len(self.effects), len(self.strips),
                             len(self.groups), len(self.scenes))]
        if self.strips:
            r += ['', 'Strips (pieces of a chain the same effects drive):']
            for s in self.strips:
                r.append('  %-14s %s leds %s%s  place %s%s'
                         % (s['name'], s['chain'], leds_text(s['leds']),
                            ' (whole chain)' if s['whole'] else '',
                            s['place'], '' if s['place_word'] else
                            ' (default)'))
        for g in self.groups:
            r.append('  group %-8s %s' % (g['name'], ', '.join(g['strips'])))
        if self.scenes:
            r += ['', 'Scenes (trigger: command, SET_LED_EFFECT EFFECT=<name>'
                  ' starts them):']
            for e in self.effects:
                sc = e.get('scene')
                if not sc:
                    continue
                r.append('  %-22s %-10s %s: %s' % (sc['name'],
                                                   e['map']['layer'],
                                                   sc['key'], sc['spec']))
                calls = sorted(set(self.calls.get(e['name'].lower(), [])))
                if calls:
                    r.append('  %-22s started in: %s' % ('', ', '.join(
                        '[%s]' % c for c in calls)))
                for n in sc['notes']:
                    r.append('  %-22s approx: %s' % ('', n))
        for state in ('idle', 'error'):
            for key, spec, why in self.builtin.get(state, []):
                r.append('  built-in %-6s %-14s %s   [%s]'
                         % (state, key, spec, why))
        if self.unplaced:
            r += ['', 'Not migrated (kept as comments at the end of the '
                  'file):']
            for e, why in self.unplaced:
                r.append('  %-22s %s' % (e['name'], why))
        if self.warnings:
            r += ['', 'Notes:'] + ['  ' + w for w in self.warnings]
        if self.manual:
            r += ['', 'FIX BY HAND:'] + ['  ' + m for m in self.manual]
        return '\n'.join(r)


######################################################################
# Checking a Lightspeed config (strip rules of lightspeed.py, plus the
# structure of groups and custom scenes)
######################################################################

def strip_leds(spec):
    """lightspeed.py Strip._parse_leds without Klipper."""
    out = []
    for part in spec.split(','):
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            a, b = [int(x) for x in part.split('-', 1)]
            step = 1 if b >= a else -1
            out.extend(range(a - 1, b - 1 + step, step))
        else:
            out.append(int(part) - 1)
    if not out or min(out) < 0:
        raise ValueError('leds are numbered from 1')
    return out


def validate_lightspeed(cp, chains):
    """-> list of errors (empty when the config is fine)."""
    errs = []
    strips, groups, used = {}, {}, {}
    secs = [(s, s.split()) for s in cp.sections()
            if s.split()[0] == 'lightspeed']
    for sec, parts in secs:
        if len(parts) == 1:
            continue
        if len(parts) != 3 or parts[1] not in ('strip', 'scene', 'group'):
            errs.append('bad section [%s]' % sec)
            continue
        name = parts[2]
        if not NAME_RE.match(name) or name == 'all':
            errs.append('bad name in [%s]' % sec)
        opts = dict(cp.items(sec))
        if parts[1] == 'strip':
            if name in strips:
                errs.append('strip %s twice' % name)
            for k in opts:
                if k not in STRIP_OPTIONS:
                    errs.append("strip %s: option '%s' not valid" % (name, k))
            out = opts.get('output', '').split()
            if len(out) != 2:
                errs.append('strip %s: output' % name)
                continue
            strips[name] = out
            key = '%s %s' % (out[0], out[1])
            if out[0] not in CHAIN_KINDS:
                continue
            if key not in chains:
                errs.append('strip %s: no [%s]' % (name, key))
                continue
            try:
                leds = strip_leds(opts['leds']) if opts.get('leds') else \
                    list(range(chains[key]))
            except ValueError as e:
                errs.append('strip %s: %s' % (name, e))
                continue
            for i in leds:
                if i >= chains[key]:
                    errs.append('strip %s: LED %d > chain_count %d'
                                % (name, i + 1, chains[key]))
                if (key, i) in used:
                    errs.append('LED %d of %s in strips %s and %s'
                                % (i + 1, key, used[(key, i)], name))
                used[(key, i)] = name
        elif parts[1] == 'group':
            for k in opts:
                if k != 'strips':
                    errs.append("group %s: option '%s' not valid" % (name, k))
            groups[name] = [x.strip() for x in opts.get('strips', '')
                            .split(',') if x.strip()]
    for name, members in groups.items():
        if name in strips:
            errs.append('group %s has the name of a strip' % name)
        if not members:
            errs.append('group %s has no strips' % name)
        for m in members:
            if m not in strips:
                errs.append('group %s: no strip %s' % (name, m))
    keys = set(strips) | set(groups) | set(['all'])
    for sec, parts in secs:
        if len(parts) != 3 or parts[1] != 'scene':
            continue
        name = parts[2]
        opts = dict(cp.items(sec))
        builtin = name in engine.STATES
        if builtin and 'trigger' in opts:
            errs.append('built-in scene %s must not have a trigger' % name)
        if not builtin:
            if 'trigger' not in opts:
                errs.append('scene %s: custom scenes need a trigger' % name)
            else:
                e = check_trigger(opts['trigger'])
                if e:
                    errs.append('scene %s: %s' % (name, e))
        if 'priority' in opts and not re.match(r'^-?\d+$',
                                               opts['priority'].strip()):
            errs.append('scene %s: priority must be a whole number' % name)
        if 'hold' in opts:
            try:
                if float(opts['hold']) < 0:
                    raise ValueError
            except ValueError:
                errs.append('scene %s: hold must be seconds' % name)
        lines = 0
        for k, v in opts.items():
            if k in ('trigger', 'priority', 'hold'):
                continue
            lines += 1
            if k not in keys:
                errs.append('scene %s: no strip or group %s' % (name, k))
            e = check_spec(v)
            if e:
                errs.append('scene %s, %s: %s' % (name, k, e))
        if not lines:
            errs.append('scene %s has no effect lines' % name)
    return errs


def text_parser(text):
    cp = new_parser()
    cp.read_string('\n'.join(strip_comment(l) for l in text.split('\n')))
    return cp


######################################################################
# Editing the config files (--apply)
######################################################################

def is_led_include(spec):
    s = spec.lower().replace('-', '_')
    return 'led_effect' in s or 'ledeffect' in s


class Editor(object):
    """Comments out [led_effect] sections and a led_effect-only include,
    adds [include lightspeed.cfg]. Macros are not touched."""

    def __init__(self, mig, target_cfg, stamp):
        self.mig = mig
        self.stamp = stamp
        self.target = target_cfg
        self.changes = {}          # path -> {line index: new text}
        self.inserts = {}          # path -> {line index: [texts]}
        self.log = []              # (path, line no, before, after)
        self.edited_lines = 0

    def _set(self, cf, i, new, log=True):
        self.changes.setdefault(cf.path, {})[i] = new
        if log:
            self.edited_lines += 1
            self.log.append((cf.path, i + 1, cf.lines[i].rstrip('\r\n'),
                             new.rstrip('\r\n')))

    def plan(self, add_include):
        led_only = set()
        for cf in self.mig.files:
            led_here = False
            for it in cf.sections():
                if it['name'].split()[0].lower() != 'led_effect':
                    continue
                led_here = True
                self.inserts.setdefault(cf.path, {})[it['start']] = [
                    '# %s led_effect section moved to lightspeed.cfg (%s)'
                    % (MARK, self.stamp) + cf.nl]
                n = 0
                for i in range(it['start'], it['end']):
                    s = cf.lines[i].strip()
                    if s and not s.startswith('#') and not s.startswith(';'):
                        self._set(cf, i, '#' + cf.lines[i], log=False)
                        n += 1
                self.log.append((cf.path, it['start'] + 1, '[%s]'
                                 % it['name'], '#[%s]  (%d lines commented '
                                 'out)' % (it['name'], n)))
                self.edited_lines += n
            others = [it for it in cf.items if it['kind'] == 'include' or
                      it['name'].split()[0].lower() != 'led_effect']
            if led_here and not others:
                led_only.add(cf.path)
        for cf in self.mig.files:
            for it in cf.items:
                if it['kind'] != 'include' or not is_led_include(it['name']):
                    continue
                try:
                    inc = resolve_include(cf.path, it['name'])
                except MigrateError:
                    continue
                if inc and all(p in led_only for p in inc):
                    line = cf.lines[it['start']]
                    body = line.rstrip('\r\n')
                    self._set(cf, it['start'], '#%s   # %s led_effect is '
                              'replaced by lightspeed.cfg%s'
                              % (body, MARK, line[len(body):]))
                else:
                    self.mig.warnings.append(
                        '%s keeps [include %s]: that file holds more than '
                        'led_effect sections (those are commented out inside '
                        'it)' % (os.path.basename(cf.path), it['name']))
        if add_include:
            self._add_include()

    def _add_include(self):
        cf = next(f for f in self.mig.files
                  if f.path == os.path.abspath(self.mig.printer_cfg))
        target = os.path.abspath(self.target)
        for f in self.mig.files:
            for it in f.items:
                if it['kind'] == 'include':
                    try:
                        if target in resolve_include(f.path, it['name']):
                            return
                    except MigrateError:
                        pass
        rel = os.path.relpath(target, os.path.dirname(cf.path)) \
            .replace(os.sep, '/')
        text = '[include %s]%s' % (rel, cf.nl)
        save = next((i for i, l in enumerate(cf.lines)
                     if l.startswith('#*#')), len(cf.lines))
        incs = [it['start'] for it in cf.items
                if it['kind'] == 'include' and it['start'] < save]
        firsts = [it['start'] for it in cf.items if it['start'] < save]
        at = incs[-1] + 1 if incs else firsts[0] if firsts else save
        ins = [text]
        if not incs and firsts:
            ins.append(cf.nl)               # blank line before the section
        if at > 0 and not cf.lines[at - 1].endswith(('\n', '\r')):
            ins.insert(0, cf.nl)
        elif at == save and at > 0 and cf.lines[at - 1].strip():
            ins.insert(0, cf.nl)
        self.inserts.setdefault(cf.path, {}).setdefault(at, []).extend(ins)
        self.log.append((cf.path, at + 1, '', text.strip()))
        self.edited_lines += 1

    def new_text(self, cf):
        ch = self.changes.get(cf.path, {})
        ins = self.inserts.get(cf.path, {})
        out = []
        for i in range(len(cf.lines) + 1):
            out.extend(ins.get(i, []))
            if i < len(cf.lines):
                out.append(ch.get(i, cf.lines[i]))
        return ''.join(out)

    def changed_files(self):
        return [cf for cf in self.mig.files
                if cf.path in self.changes or cf.path in self.inserts]


def check_after(before_cp, printer_cfg, chains, expected):
    """Re-reads the whole config like Klipper. -> list of errors."""
    try:
        cp, files = klipper_load(printer_cfg)
    except MigrateError as e:
        return ['Klipper cannot read the config: %s' % e]
    errs = []
    old = set(s for s in before_cp.sections()
              if s.split()[0] not in ('led_effect', 'lightspeed'))
    new = set(s for s in cp.sections() if s.split()[0] != 'lightspeed')
    for s in sorted(new):
        if s.split()[0] == 'led_effect':
            errs.append('[%s] is still active' % s)
    for s in sorted(old - new):
        errs.append('section [%s] went missing' % s)
    for s in sorted(new - old):
        if s.split()[0] != 'led_effect':
            errs.append('new section [%s] appeared' % s)
    for s in sorted(old & new):
        a, b = dict(before_cp.items(s)), dict(cp.items(s))
        if a != b:
            diff = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            errs.append('[%s] changed: %s' % (s, ', '.join(diff)))
    errs += validate_lightspeed(cp, chains)
    for section in expected.sections():
        if not cp.has_section(section):
            errs.append('generated section [%s] is not loaded' % section)
            continue
        for key, value in expected.items(section):
            if cp.get(section, key, fallback=None) != value:
                errs.append('generated option [%s] %s is not active' %
                            (section, key))
    return errs


######################################################################
# Moonraker's update_manager entry
######################################################################

def moonraker_entry(path):
    """-> (CfgFile, item) of [update_manager led_effect] or (None, None)."""
    if not os.path.isfile(path):
        return None, None
    cf = CfgFile(path)
    for it in cf.sections():
        name = it['name'].lower()
        if name.startswith('update_manager') and (
                'led_effect' in name or 'klipper-led_effect' in
                it['options'].get('path', '')):
            return cf, it
    return None, None


def moonraker_comment(path, stamp):
    cf, it = moonraker_entry(path)
    if cf is None:
        return False
    backup = back_up(path, stamp)
    lines = list(cf.lines)
    for i in range(it['start'], it['end']):
        s = lines[i].strip()
        if s and not s.startswith('#'):
            lines[i] = '#' + lines[i]
    atomic_write(path, ''.join(lines))
    print('Commented out [%s] in %s (backup: %s). Restart Moonraker.'
          % (it['name'], path, os.path.basename(backup)))
    return True


######################################################################
# Main
######################################################################

def atomic_write(path, text):
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, '.lightspeed-' + uuid.uuid4().hex + '.tmp')
    temp_file = open(tmp, 'x', encoding='utf-8', newline='')
    try:
        with temp_file as f:
            f.write(text)
        if os.path.exists(path):
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def is_template(text):
    example = os.path.join(os.path.dirname(HERE), 'config',
                           'lightspeed.example.cfg')
    return not text.strip() or text.splitlines() == read_file(example).splitlines()


def global_settings(cp):
    if not cp.has_section('lightspeed'):
        return None
    result = new_parser()
    result.add_section('lightspeed')
    for name, value in cp.items('lightspeed'):
        result.set('lightspeed', name, value)
    text = io.StringIO()
    result.write(text)
    return text.getvalue().rstrip()


def included_path(mig, path):
    if path in {cf.path for cf in mig.files}:
        return True
    for cf in mig.files:
        for it in cf.items:
            if it['kind'] == 'include':
                pat = os.path.join(os.path.dirname(cf.path), it['name'])
                if fnmatch.fnmatch(path, pat):
                    return True
    return False


def choose_target(mig, force):
    cfg = os.path.join(mig.config_dir, 'lightspeed.cfg')
    if not os.path.lexists(cfg):
        return cfg, True, None
    if not os.path.islink(cfg) and cfg != os.path.abspath(mig.printer_cfg):
        original = read_file(cfg)
        if is_template(original):
            return cfg, True, None
        try:
            cp = text_parser(original)
        except configparser.Error:
            cp = None
        if cp is not None and cp.sections() == ['lightspeed']:
            return cfg, True, original.rstrip('\r\n')
        if force and cp is not None and cp.sections() and all(
                s.split()[0] == 'lightspeed' for s in cp.sections()):
            return cfg, True, global_settings(cp)
    alt = os.path.join(mig.config_dir, 'lightspeed.migrated.cfg')
    for draft in (alt, alt + '.txt'):
        if not os.path.islink(draft) and not included_path(mig, draft):
            return draft, False, None
    raise MigrateError('both possible draft files are active includes or links; '
                       'use --dry-run on a separate copy of the configuration')


def back_up(path, stamp):
    base = '%s.lightspeed-backup-%s' % (path, stamp)
    backup = base
    suffix = 0
    while True:
        try:
            with open(backup, 'xb'):
                pass
            break
        except FileExistsError:
            suffix += 1
            backup = '%s-%d' % (base, suffix)
    try:
        shutil.copy2(path, backup)
    except Exception:
        os.remove(backup)
        raise
    return backup


def lightspeed_supports_scenes():
    """True when the lightspeed.py next to this tool knows groups, custom
    scenes with triggers and answers SET_LED_EFFECT itself. Older versions
    would refuse the migrated config."""
    try:
        src = read_file(os.path.join(os.path.dirname(HERE), 'lightspeed.py'))
    except (OSError, UnicodeDecodeError):
        return False
    return all(w in src for w in ("'group'", 'trigger', 'SET_LED_EFFECT',
                                  'STOP_LED_EFFECTS'))


NOT_SUPPORTED = ('This lightspeed.py does not run migrated led_effect scenes '
                 'yet (groups, custom scenes, SET_LED_EFFECT). Update '
                 'Lightspeed, then run --apply again.')


def run(args):
    stamp = time.strftime('%Y%m%d-%H%M%S')
    mig = Migration(args.config, args.printer_cfg)
    if args.detect:
        return 0 if mig.effects else 1
    if args.apply and not lightspeed_supports_scenes():
        raise MigrateError(NOT_SUPPORTED)
    if not mig.effects:
        print('No [led_effect] sections found in %s or its includes. Nothing '
              'to do.' % mig.printer_cfg)
        return 1
    target, is_main, global_section = choose_target(mig, args.force)
    if args.apply and not is_main:
        args.apply = False
        held = ('The active config was NOT switched over. Review %s and '
                'merge its Lightspeed sections into your configuration before '
                'retiring the old effects. --apply --force can replace a '
                'dedicated Lightspeed config after backup; it cannot replace '
                'a shared input file or a link.' % os.path.basename(target))
    else:
        held = None
    if global_section is None:
        global_section = global_settings(mig.cp)
    text = mig.config_text(time.strftime('%Y-%m-%d %H:%M'), global_section)
    expected = text_parser(text)
    errs = validate_lightspeed(expected, mig.chains)
    if errs:
        raise MigrateError('the generated config is not valid (a bug, please '
                           'report it): %s' % '; '.join(errs))
    ed = None
    if args.apply or args.dry_run:
        ed = Editor(mig, target, stamp)
        ed.plan(add_include=is_main)
    print(mig.report())
    if args.dry_run:
        print('\n--- %s (dry run: not written) ---' % target)
        print(text)
        print('--- end ---\n')
        print('With --apply these lines change (macros stay as they are):')
        for path, no, before, after in ed.log:
            print('  %s:%d' % (os.path.relpath(path, mig.config_dir), no))
            if before:
                print('    - %s' % before)
            print('    + %s' % after)
        if not is_main:
            print('  None of these: --apply only writes %s for manual merge.'
                  % os.path.basename(target))
        if not lightspeed_supports_scenes():
            print('\nNOTE: ' + NOT_SUPPORTED)
        if mig.manual:
            print('\nFIX BY HAND (see above): %d item(s)' % len(mig.manual))
        print('\nDry run: nothing was written.')
        return 0

    backups = []      # (path, backup or None when the file is new)
    try:
        if ed:
            for cf in ed.changed_files():
                if os.path.islink(cf.path) or not os.access(cf.path, os.W_OK):
                    raise MigrateError('%s is a link or read-only; edit it by '
                                       'hand' % cf.path)
        if os.path.exists(target):
            b = back_up(target, stamp)
            backups.append((target, b))
        else:
            backups.append((target, None))
        atomic_write(target, text + '\n')
        if ed:
            for cf in ed.changed_files():
                b = back_up(cf.path, stamp)
                backups.append((cf.path, b))
                atomic_write(cf.path, ed.new_text(cf))
            errs = check_after(mig.cp, mig.printer_cfg, mig.chains, expected)
            if errs:
                raise MigrateError('check after editing failed: ' +
                                   '; '.join(errs))
    except Exception as e:
        failures = []
        for path, b in reversed(backups):
            try:
                if b is None:
                    if os.path.exists(path):
                        os.remove(path)
                else:
                    shutil.copy2(b, path)
                    os.remove(b)
            except OSError as err:
                failures.append('%s (backup: %s): %s' % (path, b, err))
        print('\nERROR: %s' % e)
        if failures:
            print('Rollback incomplete; check these files manually:\n' +
                  '\n'.join(failures))
        else:
            print('Every file was put back as it was. Nothing changed.')
        return 2

    print('\nWrote %s' % target)
    for path, b in backups:
        if b:
            print('  backup: %s' % os.path.relpath(b, mig.config_dir))
    if ed:
        print('Changed %d line(s) in: %s' % (ed.edited_lines, ', '.join(
            sorted(set(os.path.relpath(p, mig.config_dir)
                       for p, _, _, _ in ed.log)))))
        print('The whole config was read again: Klipper can load it.')
        print('Your macros were not changed: SET_LED_EFFECT now starts the '
              'Lightspeed scenes.')
        print('Restart Klipper when the printer is idle (FIRMWARE_RESTART).')
    elif held:
        print(held)
    else:
        print('Next: run again with --apply to switch the config over (it '
              'comments out the led_effect sections and includes %s).'
              % os.path.basename(target))
    if mig.manual:
        print('\nFIX BY HAND:')
        for m in mig.manual:
            print('  ' + m)
    return 3 if held else 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Move a klipper-led_effect setup to Lightspeed.')
    ap.add_argument('--config', default='~/printer_data/config',
                    help='Klipper config folder (default %(default)s)')
    ap.add_argument('--printer-cfg', default='printer.cfg',
                    help='main config file in that folder')
    ap.add_argument('--dry-run', action='store_true',
                    help='show the result and the planned edits, write '
                    'nothing')
    ap.add_argument('--apply', action='store_true',
                    help='also comment out the led_effect sections and '
                    'include lightspeed.cfg in printer.cfg')
    ap.add_argument('--force', action='store_true',
                    help='overwrite an existing lightspeed.cfg (backed up)')
    ap.add_argument('--detect', action='store_true',
                    help='exit 0 when led_effect sections are found, else 1')
    ap.add_argument('--can-apply', action='store_true',
                    help='exit 0 when this Lightspeed can run migrated '
                    'scenes, else 1')
    ap.add_argument('--moonraker', metavar='MOONRAKER_CONF',
                    help='print the [update_manager led_effect] entry of this '
                    'file (with --apply: comment it out, backed up)')
    args = ap.parse_args(argv)
    try:
        if args.moonraker:
            path = os.path.expanduser(args.moonraker)
            cf, it = moonraker_entry(path)
            if cf is None:
                return 1
            if args.apply:
                moonraker_comment(path, time.strftime('%Y%m%d-%H%M%S'))
            else:
                print(cf.section_text(it))
            return 0
        if args.can_apply:
            return 0 if lightspeed_supports_scenes() else 1
        args.config = os.path.expanduser(args.config)
        return run(args)
    except MigrateError as e:
        print('ERROR: %s' % e)
        print('Nothing was changed.')
        return 2


if __name__ == '__main__':
    sys.exit(main())
