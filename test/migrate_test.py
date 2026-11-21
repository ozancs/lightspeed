"""Tests for tools/migrate_led_effect.py on realistic led_effect setups.

    python test/migrate_test.py

For every fixture: the made lightspeed.cfg passes the migration's own
checks, every scene line parses with lightspeed_engine.parse_spec (value
effect keys source/min/max/colors are checked here), the strips load
through lightspeed.py with fake chains, and --apply leaves a config that a
Klipper-style loader reads with the same sections and unchanged macros,
with byte-exact backups. Also: dry run, --detect, an existing
lightspeed.cfg, --force, rollback and the Moonraker entry.
"""
import configparser
import contextlib
import glob
import inspect
import io
import os
import re
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, HERE)
import lightspeed_engine as engine  # noqa: E402
import migrate_led_effect as M  # noqa: E402
import smoke_klippy as K  # noqa: E402

failures = []
SKIPPED = set()


def check(cond, msg):
    print(('ok    ' if cond else 'FAIL  ') + msg)
    if not cond:
        failures.append(msg)


######################################################################
# Fixtures
######################################################################

# 1. Stealthburner: logo (1) + nozzle (2,3) on one chain, effects on the
#    whole chain, status macros, PRINT_START calling them, SAVE_CONFIG block.
SB = {
    'printer.cfg': """\
[include mainsail.cfg]
[include stealthburner_leds.cfg]

[printer]
kinematics: corexy
max_velocity: 300
max_accel: 3000

[gcode_macro PRINT_START]
gcode:
    STATUS_HOMING
    G28
    STATUS_HEATING
    M190 S{params.BED|default(100)}
    STATUS_PRINTING

[gcode_macro PRINT_END]
gcode:
    STATUS_PART_READY

#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [probe]
#*# z_offset = 1.234
""",
    'mainsail.cfg': """\
[virtual_sdcard]
path: ~/printer_data/gcodes

[pause_resume]
""",
    'stealthburner_leds.cfg': """\
[neopixel sb_leds]
pin: PB0
chain_count: 3
color_order: GRBW
initial_RED: 1.0
initial_BLUE: 1.0

[led_effect sb_logo_busy]
autostart:              false
frame_rate:             24
leds:
    neopixel:sb_leds (1)
layers:
    breathing  3 1 top (1,0,0)

[led_effect sb_logo_heating]
leds:
    neopixel:sb_leds (1)
autostart:                          false
frame_rate:                         24
layers:
     breathing  3 1 top (1, 0.18, 0)

[led_effect sb_logo_homing]
leds:
    neopixel:sb_leds (1)
layers:
        breathing  3 1 top (0.0, 0.6, 0.2)

[led_effect sb_logo_printing]
leds:
    neopixel:sb_leds (1)
layers:
    gradient  0.3  1 add (0.3, 0.0, 0.0),(0.3, 0.3, 0.0),(0.3, 0.1, 0.0)

[led_effect sb_logo_part_ready]
leds:
    neopixel:sb_leds (1)
layers:
        breathing  3 1 top (0.0, 1.0, 0.0)

[led_effect sb_nozzle_heating]
leds:
    neopixel:sb_leds (2,3)
layers:
        breathing  3 1 top (1.0, 0.18, 0.0, 0.0)

[led_effect sb_nozzle_part_ready]
leds:
    neopixel:sb_leds (2,3)
layers:
        breathing  3 1 top (0.6, 1.0, 0.0, 0.1)

[led_effect sb_critical_error]
leds:
    neopixel:sb_leds
layers:
    strobe         1  1.5   add        (1.0,  1.0, 1.0)
    breathing      2  0     difference (0.95, 0.0, 0.0)
    static         1  0     top        (1.0,  0.0, 0.0)
autostart:                             false
frame_rate:                            24
run_on_error:                          true

[led_effect rainbow]
leds:
    neopixel:sb_leds
autostart:                          true
frame_rate:                         24
layers:
    gradient  0.3  1 add (0.3, 0.0, 0.0),(0.0, 0.3, 0.0),(0.0, 0.0, 0.3)

[led_effect set_nozzle_leds]
leds:
    neopixel:sb_leds (2,3)
   #neopixel:caselight
layers:
    static         0  0     top        (0.0,  0.0, 0.0, 1.0)

[gcode_macro set_nozzle_leds_on]
gcode:
    SET_LED_EFFECT EFFECT=set_nozzle_leds

[gcode_macro set_nozzle_leds_off]
gcode:
    SET_LED_EFFECT EFFECT=set_nozzle_leds STOP=1

[gcode_macro status_off]
gcode:
    STOP_LED_EFFECTS

[gcode_macro status_ready]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=rainbow

[gcode_macro status_busy]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=sb_logo_busy
    set_nozzle_leds_on

[gcode_macro status_heating]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=sb_logo_heating
    SET_LED_EFFECT EFFECT=sb_nozzle_heating

[gcode_macro status_homing]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=sb_logo_homing
    set_nozzle_leds_on

[gcode_macro status_printing]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=sb_logo_printing
    set_nozzle_leds_on

[gcode_macro status_part_ready]
gcode:
    STOP_LED_EFFECTS
    SET_LED_EFFECT EFFECT=sb_nozzle_part_ready
    SET_LED_EFFECT EFFECT=sb_logo_part_ready
""",
}

# 2. A case strip on one neopixel (no WLED): idle white, bed heater colour,
#    print progress, a temperature gauge, all in files found by a glob.
CASE = {
    'printer.cfg': """\
[printer]
kinematics: cartesian

[include leds/*.cfg]

[heater_bed]
heater_pin: PA1

[gcode_macro START_PRINT]
gcode:
  M190 S{params.BED}
  SET_LED_EFFECT EFFECT=case_heating REPLACE=1
  M109 S{params.EXTRUDER}
  SET_LED_EFFECT EFFECT=case_progress REPLACE=1

[gcode_macro END_PRINT]
gcode:
  STOP_LED_EFFECTS
  SET_LED_EFFECT EFFECT=case_done
""",
    'leds/case_strip.cfg': """\
[neopixel case]
pin: PC7
chain_count: 40
color_order: GRB

[led_effect case_idle]
autostart: true
frame_rate: 24
leds:
    neopixel:case
layers:
    static 0 0 top (0.8, 0.8, 0.8)
""",
    'leds/case_effects.cfg': """\
[led_effect case_heating]
leds:
    neopixel:case
heater: heater_bed
layers:
    heater  50 0 add    (1,1,0),(1,0,0)
    static  0  0 top    (1,0,0)

[led_effect case_progress]
leds:
    neopixel:case
layers:
    progress  -1  0 add         ( 0, 0,   1),( 0, 0.1, 0.6)
    static     0  0 top         ( 0, 0, 0.1)

[led_effect case_done]
leds:
    neopixel:case (1-40)
layers:
    comet  0.5 4 add (0,1,0),(0,0.2,0)

[led_effect bed_gauge]
leds:
    neopixel:case (21-40)
heater: "temperature_fan chamber"
layers:
    temperature 20 60 top (0,0,1),(1,0,0)

[led_effect nozzle_temp]
leds:
    neopixel:case (1-20)
heater: extruder
layers:
    temperaturegauge 30 250 top (1,0.3,0)
""",
}

# 3. Several chains: a status bar spans the end of one chain and a hotend
#    chain, a reversed range, overlapping ranges, a dotstar.
MULTI = {
    'printer.cfg': """\
[printer]
kinematics: corexy

[neopixel my_led]
pin: PA8
chain_count: 18

[neopixel hotend_rgb]
pin: EBBCan:PD3
chain_count: 2

[dotstar bed_lights]
data_pin: PB5
clock_pin: PB6
chain_count: 10

[led_effect status_bar]
autostart: true
leds:
    neopixel:my_led (16-18)
    neopixel:hotend_rgb (1-2)
layers:
    chase 1 2 top (0,0,1)

[led_effect panel_scan]
leds:
    neopixel:my_led (1-8)
    neopixel:my_led (16-9)
layers:
    cylon 2 0 top (1,0,0),(0,1,0)

[led_effect panel_half]
leds:
    neopixel:my_led (1-4)
layers:
    blink 1 0.5 top (1,1,1)

[led_effect bed_glow]
leds:
    dotstar:bed_lights (1,3,5)
    dotstar:bed_lights (2,4,6-10)
layers:
    twinkle 100 0.5 top (1,0.5,0)

[led_effect x_follow]
leds:
    dotstar:bed_lights
stepper: x
layers:
    stepper 4 4 top (0,1,1)

[homing_override]
gcode:
  SET_LED_EFFECT EFFECT=panel_scan
  G28 X0 Y0
  G28 Z0
  SET_LED_EFFECT EFFECT=panel_scan STOP=1
""",
}

# 4. Odd formatting: CRLF, tabs, comments everywhere, ';' comments, upper
#    case names, a name that is a built-in state, template layers, an
#    analog pin effect, an unknown chain, a led_effect-only include,
#    printer['led_effect ...'] in a macro.
ODD_PRINTER = """\
# my printer
[include  led-effects.cfg]   # all my leds
[include macros.cfg]

[printer]
kinematics: corexy   ; inline

[neopixel Toolhead]
pin: PB0
chain_count: 4   # four leds
"""
ODD_LEDS = """\
# --- LEDs ---
[led_effect Logo_Idle]   # the logo
autostart:\ttrue
leds:
\tneopixel:Toolhead (1)
    # neopixel:Toolhead (2)
layers:
\tbreathing  {params.RATE|default(4)}  1  top  (0.0, 0.0, 1.0)

[led_effect idle]
leds:
  neopixel:Toolhead (2 - 4)
layers:
  linearfade 2 0 top (1,0,0),(0,0,1)   ; fades

[led_effect knob]
leds:
  neopixel:Toolhead (2-4)
analog_pin: PA3
layers:
  analogpin 10 0 top (1,1,1)

[led_effect ghost]
leds:
  neopixel:not_there (1-3)
layers:
  static 0 0 top (1,1,1)
"""
ODD_MACROS = """\
[gcode_macro LIGHTS_CHECK]
gcode:
  {% if printer['led_effect Logo_Idle'].enabled %}
    RESPOND MSG="logo on"
  {% endif %}
  SET_LED_EFFECT EFFECT=Logo_Idle
  SET_LED_EFFECT EFFECT=ghost
  {% if params.X %}SET_LED_EFFECT EFFECT=idle{% endif %}
"""
ODD = {
    'printer.cfg': ODD_PRINTER.replace('\n', '\r\n'),
    'led-effects.cfg': ODD_LEDS.replace('\n', '\r\n'),
    'macros.cfg': ODD_MACROS,
}


######################################################################
# Helpers
######################################################################

def make_dir(files):
    d = tempfile.mkdtemp(prefix='ls_migrate_')
    for name, text in files.items():
        p = os.path.join(d, name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    return d


def run_tool(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = M.main(list(argv))
    return code, buf.getvalue()


def snapshot(d):
    out = {}
    for root, _, names in os.walk(d):
        for n in names:
            p = os.path.join(root, n)
            with open(p, 'rb') as f:
                out[os.path.relpath(p, d).replace(os.sep, '/')] = f.read()
    return out


def klipper_read(printer_cfg):
    """An independent copy of Klipper's configfile include/parse steps."""
    cp = configparser.RawConfigParser(strict=False,
                                      inline_comment_prefixes=(';', '#'))

    def parse(fn, visited):
        fn = os.path.abspath(fn)
        assert fn not in visited, 'recursive include'
        visited.add(fn)
        with open(fn, encoding='utf-8') as f:
            data = f.read()
        buf = []
        for line in data.split('\n'):
            pos = line.find('#')
            if pos >= 0:
                line = line[:pos]
            mo = configparser.RawConfigParser.SECTCRE.match(line)
            header = mo and mo.group('header')
            if header and header.startswith('include '):
                cp.read_string('\n'.join(buf))
                buf = []
                pat = os.path.join(os.path.dirname(fn), header[8:].strip())
                names = sorted(glob.glob(pat))
                assert names or glob.has_magic(pat), 'missing ' + pat
                for n in names:
                    parse(n, visited)
            else:
                buf.append(line)
        cp.read_string('\n'.join(buf))
        visited.remove(fn)
    parse(printer_cfg, set())
    return cp


def chains_of(cp):
    out = {}
    for s in cp.sections():
        p = s.split()
        if p[0] in ('neopixel', 'dotstar') and len(p) == 2:
            out[s] = int(cp.get(s, 'chain_count', fallback='1'))
    return out


def scene_lines(cp):
    for s in cp.sections():
        p = s.split()
        if len(p) == 3 and p[:2] == ['lightspeed', 'scene']:
            for k, v in cp.items(s):
                if k not in ('trigger', 'priority', 'hold'):
                    yield s, k, v


def known_part(spec):
    """The spec without the value-effect keys today's engine does not know
    yet (those are checked by hand here)."""
    parts = spec.split()
    keep = [parts[0]]
    for p in parts[1:]:
        k, _, v = p.partition('=')
        if k == 'source' and v not in ('progress', 'heat'):
            assert re.match(r'^[a-z_][a-z0-9_]*\.[a-z_]+$', v), p
        elif k in ('min', 'max'):
            float(v)
        elif k == 'colors':
            for c in v.split(','):
                engine.parse_color(c)
        else:
            keep.append(p)
    return ' '.join(keep)


def load_strips(cfg_text, chains):
    """Loads [lightspeed] and every strip through the real lightspeed.py with
    fake chains, then connects (overlap, unknown chain, unread options)."""
    K.ACCESSED.clear()
    p = K.Printer()
    p.objects['gcode'] = K.GCode()
    p.objects['webhooks'] = K.Webhooks()
    for name, n in chains.items():
        p.objects[name] = K.Chain(n)
    for name in ('extruder', 'heater_bed'):
        p.objects[name] = K.Heater()
    p.objects['heaters'] = K.Obj({'available_heaters': ['heater_bed',
                                                        'extruder']})
    p.objects['print_stats'] = K.Obj({'state': 'standby'})
    p.objects['display_status'] = K.Obj({'progress': 0.0})
    p.objects['toolhead'] = K.Obj({'position': [0., 0., 0., 0.],
                                   'axis_minimum': [0., 0., 0., 0.],
                                   'axis_maximum': [300., 300., 300., 0.]})
    cp = configparser.RawConfigParser(strict=False,
                                      inline_comment_prefixes=(';', '#'))
    cp.read_string(cfg_text)
    full = 'group' in inspect.getsource(K.LS.load_config_prefix)
    p.load_object(K.Section(p, cp, 'lightspeed'), 'lightspeed')
    for sec in cp.sections():
        parts = sec.split()
        if parts[0] != 'lightspeed' or len(parts) == 1:
            continue
        if parts[1] == 'strip' or full:
            p.objects[sec] = K.LS.load_config_prefix(K.Section(p, cp, sec))
    p.send_event('klippy:connect')
    return p.objects['lightspeed'], full


######################################################################
# Checks
######################################################################

def check_generated(label, d, cfg_name='lightspeed.cfg'):
    path = os.path.join(d, cfg_name)
    text = open(path, encoding='utf-8').read()
    cp = M.text_parser(text)
    chains = chains_of(klipper_read(os.path.join(d, 'printer.cfg')))
    errs = M.validate_lightspeed(cp, chains)
    check(not errs, '%s: generated config passes the checks %s'
          % (label, errs or ''))
    bad = []
    n = 0
    for sec, k, v in scene_lines(cp):
        n += 1
        try:
            engine.parse_spec(known_part(v))
        except Exception as e:   # noqa: BLE001
            bad.append('%s %s: %s (%s)' % (sec, k, v, e))
    check(n > 0 and not bad, '%s: all %d scene lines parse with '
          'parse_spec %s' % (label, n, bad or ''))
    for s in cp.sections():
        p = s.split()
        if len(p) == 3 and p[:2] == ['lightspeed', 'scene'] and \
                p[2] not in engine.STATES:
            if M.check_trigger(cp.get(s, 'trigger', fallback='')):
                bad.append(s)
    check(not bad, '%s: every custom scene has a valid trigger' % label)
    try:
        ls, full = load_strips(text, chains)
        check(len(ls.strips) == sum(1 for s in cp.sections()
                                    if s.startswith('lightspeed strip ')),
              '%s: strips load in lightspeed.py with fake chains%s'
              % (label, ' (whole config)' if full else ''))
        if not full:
            SKIPPED.add('whole-config load in lightspeed.py (groups and '
                        'custom scenes are not in lightspeed.py yet)')
    except Exception as e:   # noqa: BLE001
        check(False, '%s: strips load in lightspeed.py: %s' % (label, e))
    return cp


def check_apply(label, d, before):
    pcfg = os.path.join(d, 'printer.cfg')
    try:
        after_cp = klipper_read(pcfg)
    except Exception as e:   # noqa: BLE001
        check(False, '%s: config loads after --apply: %s' % (label, e))
        return
    check(True, '%s: config loads after --apply (Klipper-style reader)'
          % label)
    before_cp = before['cp']
    check(not any(s.startswith('led_effect') for s in after_cp.sections()),
          '%s: no led_effect section left active' % label)
    same = True
    for s in before_cp.sections():
        if s.startswith('led_effect'):
            continue
        if not after_cp.has_section(s) or \
                dict(before_cp.items(s)) != dict(after_cp.items(s)):
            same = False
            print('      changed: [%s]' % s)
    check(same, '%s: every other section (macros too) is unchanged' % label)
    check(any(s.startswith('lightspeed strip') for s in after_cp.sections()),
          '%s: lightspeed.cfg is included' % label)
    snap = snapshot(d)
    backups = [k for k in snap if '.lightspeed-backup-' in k]
    ok = bool(backups)
    for b in backups:
        orig = b.split('.lightspeed-backup-')[0]
        if before['files'].get(orig) != snap[b]:
            ok = False
            print('      backup differs: ' + b)
    check(ok, '%s: %d byte-exact backups' % (label, len(backups)))
    untouched = [k for k in before['files'] if k + '.lightspeed-backup-' not
                 in ' '.join(backups) and before['files'][k] != snap.get(k)]
    check(not untouched, '%s: files without backup are unchanged %s'
          % (label, untouched or ''))
    ptext = open(pcfg, encoding='utf-8', newline='').read()
    check(ptext.count('[include lightspeed.cfg]') == 1,
          '%s: printer.cfg includes lightspeed.cfg once' % label)
    if '#*#' in ptext:
        check(ptext.index('[include lightspeed.cfg]') < ptext.index('#*#'),
              '%s: include is above the SAVE_CONFIG block' % label)
    return after_cp


def migrate_case(label, files, expect):
    d = make_dir(files)
    try:
        before = {'cp': klipper_read(os.path.join(d, 'printer.cfg')),
                  'files': snapshot(d)}
        code, out = run_tool('--config', d, '--detect')
        check(code == 0, '%s: --detect finds led_effect' % label)
        code, out = run_tool('--config', d, '--dry-run')
        check(code == 0 and snapshot(d) == before['files'],
              '%s: --dry-run writes nothing' % label)
        check('--- end ---' in out and 'migration report' in out,
              '%s: --dry-run prints the report and the config' % label)
        code, out = run_tool('--config', d, '--apply')
        check(code == 0, '%s: --apply succeeds' % label)
        if code != 0:
            print(out)
            return
        cp = check_generated(label, d)
        check_apply(label, d, before)
        expect(cp, out, d)
        code, out = run_tool('--config', d, '--detect')
        check(code == 1, '%s: --detect finds nothing after --apply' % label)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def get(cp, sec, key):
    return cp.get(sec, key) if cp.has_option(sec, key) else None


def strip_for(cp, output, leds):
    """Name of the strip on <output> with exactly these leds (or None)."""
    for s in cp.sections():
        if s.startswith('lightspeed strip ') and \
                get(cp, s, 'output') == output and get(cp, s, 'leds') == leds:
            return s.split()[2]
    return None


def group_for(cp, strips):
    for s in cp.sections():
        if s.startswith('lightspeed group ') and [
                x.strip() for x in get(cp, s, 'strips').split(',')] == strips:
            return s.split()[2]
    return None


def scene_key(cp, scene):
    """(key, spec) of the one strip/group line of a migrated scene."""
    sec = 'lightspeed scene ' + scene
    if not cp.has_section(sec):
        return None, None
    keys = [k for k in cp.options(sec) if k != 'trigger']
    return (keys[0], cp.get(sec, keys[0])) if len(keys) == 1 else (None, None)


def expect_sb(cp, out, d):
    logo = strip_for(cp, 'neopixel sb_leds', '1')
    nozzle = strip_for(cp, 'neopixel sb_leds', '2-3')
    check(logo and nozzle and len([s for s in cp.sections() if s.startswith(
        'lightspeed strip')]) == 2, 'sb: two strips, LED 1 (%s) and LEDs 2-3 '
          '(%s)' % (logo, nozzle))
    check(get(cp, 'lightspeed strip %s' % logo, 'place') == 'toolhead_logo'
          and get(cp, 'lightspeed strip %s' % nozzle, 'place') == 'nozzle',
          'sb: places guessed from the names')
    check(group_for(cp, [logo, nozzle]) == 'sb_leds',
          'sb: whole-chain effects use group sb_leds')
    check(get(cp, 'lightspeed scene sb_logo_heating', 'trigger') == 'command'
          and scene_key(cp, 'sb_logo_heating') == (logo,
                                                   'breathe color=ff2e00'),
          'sb: sb_logo_heating -> command scene on the logo')
    check(scene_key(cp, 'sb_nozzle_heating') == (nozzle,
                                                 'breathe color=ff2e00'),
          'sb: RGBW colour folded to rrggbb')
    check(scene_key(cp, 'set_nozzle_leds') == (nozzle, 'solid'),
          'sb: white-channel static -> solid ffffff')
    check(scene_key(cp, 'sb_logo_printing')[1] ==
          'breathe color=4d0000 color2=4d1a00 period=3.33',
          'sb: gradient of close colours -> breathe, period 1/rate')
    check(get(cp, 'lightspeed scene idle', 'sb_leds') == 'rainbow period=3.33',
          'sb: autostart rainbow is the idle scene')
    check(get(cp, 'lightspeed scene error', 'sb_leds') ==
          'blink color2=ff0000', 'sb: run_on_error effect is the error scene')
    txt = open(os.path.join(d, 'stealthburner_leds.cfg'),
               encoding='utf-8').read()
    check('    SET_LED_EFFECT EFFECT=sb_logo_heating\n' in txt and
          '    STOP_LED_EFFECTS\n' in txt, 'sb: macros keep their calls')
    check('#[led_effect rainbow]' in txt, 'sb: led_effect section commented')
    ptxt = open(os.path.join(d, 'printer.cfg'), encoding='utf-8').read()
    check('[include stealthburner_leds.cfg]\n' in ptxt,
          'sb: include with macros stays active')


def expect_case(cp, out, d):
    check(get(cp, 'lightspeed scene idle', 'case') == 'solid color=cccccc',
          'case: autostart static -> idle solid')
    lo = strip_for(cp, 'neopixel case', '1-20')
    hi = strip_for(cp, 'neopixel case', '21-40')
    check(lo and hi and group_for(cp, [lo, hi]) == 'case',
          'case: strip split in two by partial effects, group "case"')
    check(scene_key(cp, 'case_heating') == (
        'case', 'heat source=heater_bed.temperature min=50 max=100 '
        'colors=ffff00,ff0000'), 'case: heater layer -> heat source=...')
    check(scene_key(cp, 'case_progress') == (
        'case', 'progress color=0000ff source=progress'),
        'case: progress layer -> progress source=progress')
    check(scene_key(cp, 'nozzle_temp') == (
        lo, 'progress color=ff4d00 source=extruder.temperature min=30 '
        'max=250'), 'case: temperaturegauge -> progress bar of the extruder')
    check(scene_key(cp, 'bed_gauge') == (hi, 'heat color=ff0000 '
                                         'color2=0000ff'),
          'case: quoted heater falls back to heat without source')
    check(scene_key(cp, 'case_done')[1] == 'comet color=00ff00 period=4.44',
          'case: comet speed rate*fps -> period')
    check('started in: [gcode_macro START_PRINT]' in out,
          'case: report says where effects start')


def expect_multi(cp, out, d):
    hot = strip_for(cp, 'neopixel hotend_rgb', '1-2')
    half = strip_for(cp, 'neopixel my_led', '1-4')
    rest = strip_for(cp, 'neopixel my_led', '5-8,15-9')
    check(hot and half and rest, 'multi: overlapping ranges split into '
          'disjoint strips, reversed run kept (5-8,15-9)')
    key, spec = scene_key(cp, 'status_bar')
    members = [x.strip() for x in (get(cp, 'lightspeed group %s' % key,
                                       'strips') or '').split(',')]
    check(members[-1] == hot and len(members) >= 2 and all(
        get(cp, 'lightspeed strip %s' % m, 'output') == 'neopixel my_led'
        for m in members[:-1]), 'multi: two chains -> strips + one group '
          '(%s)' % members)
    check(spec == 'chase color=0000ff period=0.21',
          'multi: chase speed rate*fps -> period')
    key, spec = scene_key(cp, 'panel_scan')
    check(key and cp.has_section('lightspeed group %s' % key) and
          spec == 'scan period=4', 'multi: cylon -> scan on a group')
    bed = strip_for(cp, 'dotstar bed_lights', '1,3,5,2,4,6-10')
    check(bed is not None, 'multi: dotstar LED order is kept')
    check(scene_key(cp, 'x_follow') == (bed, 'follow color=00ffff'),
          'multi: stepper -> follow (axis x)')
    check(get(cp, 'lightspeed scene idle', bed) == 'off' and
          get(cp, 'lightspeed scene idle', scene_key(cp, 'status_bar')[0]),
          'multi: idle has the autostart group, the rest off')


def expect_odd(cp, out, d):
    check(scene_key(cp, 'logo_idle')[1] == 'breathe color=0000ff period=4',
          'odd: template default used (period=4)')
    check(cp.has_section('lightspeed scene idle_fx'),
          'odd: led_effect named "idle" becomes scene idle_fx')
    check('FIX BY HAND' in out and 'EFFECT=idle_fx' not in out and
          "printer['led_effect Logo_Idle']" in out,
          'odd: printer[led_effect] use is listed; idle -> idle_fx needs no '
          'macro change (Lightspeed finds it)')
    check('started in: [gcode_macro LIGHTS_CHECK]' in out.split('idle_fx')[1],
          'odd: call inside a Jinja one-liner is found')
    check("starts led_effect 'ghost', which gets no scene" in out and
          '(lower case)' not in out, 'odd: calls without a scene are listed; '
          'case-only renames are not (Lightspeed matches case-blind)')
    check(scene_key(cp, 'knob')[1] == 'solid',
          'odd: analogpin effect kept as a solid scene')
    txt = open(os.path.join(d, 'lightspeed.cfg'), encoding='utf-8').read()
    check('# [led_effect ghost]' in txt and '# [led_effect knob]' in txt,
          'odd: unmapped effects kept as original text')
    raw = open(os.path.join(d, 'printer.cfg'), encoding='utf-8',
               newline='').read()
    check('#[include  led-effects.cfg]' in raw,
          'odd: include of a led_effect-only file commented out')
    check('\r\n[include lightspeed.cfg]\r\n' in raw and
          raw.count('\n') == raw.count('\r\n'), 'odd: CRLF line ends kept')
    raw = open(os.path.join(d, 'led-effects.cfg'), encoding='utf-8',
               newline='').read()
    check(raw.count('\n') == raw.count('\r\n'),
          'odd: CRLF kept in the edited include')


def test_existing_and_force():
    d = make_dir(CASE)
    try:
        own = '[lightspeed]\n[lightspeed strip x]\noutput: neopixel case\n'
        with open(os.path.join(d, 'lightspeed.cfg'), 'w') as f:
            f.write(own)
        code, out = run_tool('--config', d)
        check(code == 0 and os.path.exists(os.path.join(
            d, 'lightspeed.migrated.cfg')),
            'existing lightspeed.cfg: result goes to lightspeed.migrated.cfg')
        check_generated('migrated', d, 'lightspeed.migrated.cfg')
        check(open(os.path.join(d, 'lightspeed.cfg')).read() == own,
              'existing lightspeed.cfg is not overwritten')
        before = snapshot(d)
        code, out = run_tool('--config', d, '--apply')
        after = snapshot(d)
        check(code == 0 and 'NOT switched' in out and
              all(after.get(k) == v for k, v in before.items()),
              'existing lightspeed.cfg + --apply: config left unchanged, '
              'only the migrated file is written')
        code, out = run_tool('--config', d, '--apply', '--force')
        check(code == 0 and glob.glob(os.path.join(
            d, 'lightspeed.cfg.lightspeed-backup-*')),
            '--force overwrites lightspeed.cfg with a backup')
        check_generated('force', d)
        check('lightspeed strip' in str(klipper_read(os.path.join(
            d, 'printer.cfg')).sections()) and
            run_tool('--config', d, '--detect')[0] == 1,
            '--apply --force switches the config over')
    finally:
        shutil.rmtree(d, ignore_errors=True)
    d = make_dir(SB)
    try:
        shutil.copy(os.path.join(ROOT, 'config', 'lightspeed.example.cfg'),
                    os.path.join(d, 'lightspeed.cfg'))
        code, out = run_tool('--config', d)
        check(code == 0 and 'lightspeed group sb_leds' in open(
            os.path.join(d, 'lightspeed.cfg')).read(),
            'the installer\'s example lightspeed.cfg is replaced (backed up)')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_rollback():
    d = make_dir(SB)
    try:
        before = snapshot(d)
        real = M.check_after
        M.check_after = lambda *a: ['simulated failure']
        try:
            code, out = run_tool('--config', d, '--apply')
        finally:
            M.check_after = real
        check(code == 2 and 'put back' in out, 'rollback: error reported')
        check(snapshot(d) == before,
              'rollback: every file byte-identical, no new files left')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_led_effect():
    d = make_dir({'printer.cfg': '[printer]\nkinematics: none\n'})
    try:
        check(run_tool('--config', d, '--detect')[0] == 1 and
              run_tool('--config', d, '--apply')[0] == 1 and
              snapshot(d) == {'printer.cfg': b'[printer]\nkinematics: none\n'},
              'no led_effect: nothing to do, nothing written')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_moonraker():
    d = make_dir({'moonraker.conf': """\
[server]
host: 0.0.0.0

[update_manager led_effect]
type: git_repo
path: ~/klipper-led_effect
origin: https://github.com/julianschill/klipper-led_effect.git
is_system_service: False

[update_manager mainsail]
type: web
path: ~/mainsail
"""})
    try:
        mc = os.path.join(d, 'moonraker.conf')
        code, out = run_tool('--moonraker', mc)
        check(code == 0 and out.startswith('[update_manager led_effect]') and
              'mainsail' not in out, 'moonraker: shows only the led_effect '
              'entry')
        code, out = run_tool('--moonraker', mc, '--apply')
        cp = configparser.RawConfigParser(inline_comment_prefixes=('#',))
        cp.read(mc)
        check(code == 0 and not cp.has_section('update_manager led_effect')
              and cp.has_section('update_manager mainsail') and
              glob.glob(mc + '.lightspeed-backup-*'),
              'moonraker: entry commented out, the rest kept, backed up')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_units():
    check(M.leds_text([0, 1, 2, 4, 15, 14, 13]) == '1-3,5,16-14',
          'leds_text keeps runs and direction')
    check(M.parse_led_list('(16-9)') == list(range(15, 7, -1)),
          'reversed led_effect range')
    check(M.check_spec('heat source=heater_bed.temperature min=40 max=90 '
                       'colors=0000ff,ff0000') is None and
          M.check_spec('solid colors=ff0000') is not None,
          'check_spec: value keys only on progress/heat')
    check(M.check_trigger('command') is None and
          M.check_trigger('macro CLEAN_NOZZLE') is None and
          M.check_trigger('heater_bed.temperature > 75') is None and
          M.check_trigger('sometimes') is not None, 'trigger forms')


def test_unsupported(real):
    """--apply refuses while lightspeed.py cannot run migrated scenes."""
    d = make_dir(SB)
    try:
        before = snapshot(d)
        saved = M.lightspeed_supports_scenes
        M.lightspeed_supports_scenes = lambda: False
        try:
            code, out = run_tool('--config', d, '--apply')
            dry = run_tool('--config', d, '--dry-run')[1]
        finally:
            M.lightspeed_supports_scenes = saved
        check(code == 2 and snapshot(d) == before and 'Update' in out and
              'NOTE:' in dry, 'older lightspeed.py: --apply refuses and '
              'changes nothing, --dry-run says why')
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print('info  this lightspeed.py %s run migrated scenes (--can-apply)'
          % ('CAN' if real else 'can NOT yet'))


def main():
    real = M.lightspeed_supports_scenes()
    test_unsupported(real)
    # The other tests run --apply as if lightspeed.py already had custom
    # scenes, groups and SET_LED_EFFECT (the contract this tool targets).
    M.lightspeed_supports_scenes = lambda: True
    test_units()
    migrate_case('sb', SB, expect_sb)
    migrate_case('case', CASE, expect_case)
    migrate_case('multi', MULTI, expect_multi)
    migrate_case('odd', ODD, expect_odd)
    test_existing_and_force()
    test_rollback()
    test_no_led_effect()
    test_moonraker()
    for s in sorted(SKIPPED):
        print('SKIP  ' + s)
    print('\n%d failures' % len(failures))
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
