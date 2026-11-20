"""Python engine vs JS engine: same frames, byte for byte (+-1 rounding),
same scene plans (priorities, ties, groups) and the same trigger parsing.

    python test/parity.py            (needs node on PATH or NODE=<path>)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import lightspeed_engine as E  # noqa: E402

NODE = os.environ.get('NODE', 'node')

SPECS = [
    'off', 'solid color=ff6b1a', 'breathe', 'breathe color=00ff00 period=0.7',
    'blink period=0.3', 'rainbow', 'rainbow period=1 reverse=1', 'chase size=3',
    'chase period=0.5 size=1 reverse=1', 'comet', 'comet size=4 color=ff0000',
    'scan', 'scan size=1 period=0.9', 'twinkle density=0.8', 'twinkle', 'fire',
    'progress', 'progress source=heat reverse=1',
    'progress gradient=1 source=heat color=0030ff color2=ff0000',
    'progress gradient=1 color2=000000', 'heat', 'follow axis=y size=3',
    'follow axis=x',
    # value sources, min/max, colour stops
    'heat source=heater_bed.temperature colors=0000ff,ffff00,ff8000,ff0000,a000ff',
    'heat source=extruder.temperature min=20 max=250',
    'heat source=temperature_sensor chamber.temperature min=15 max=60 color=ff0000',
    'heat source=fan.speed',
    'progress source=motion_report.live_extruder_velocity min=-5 max=5',
    'progress colors=ff0000,00ff00,0000ff',
    'progress colors=ff0000,00ff00,0000ff gradient=1 reverse=1',
    'progress source=heater_bed.temperature colors=0000ff,ff0000 min=30 max=30',
    'progress source=extruder.temperature max=10 min=200',
    'heat source=print_stats.info.current_layer min=0 max=100 colors=ffffff',
    'progress source=missing.value colors=ff0000,00ff00',
]
COUNTS = [1, 3, 8, 37, 120]
TIMES = [0.0, 0.016, 0.5, 1.234, 7.77, 59.99, 1234.5678, 86400.125]
VARS = {'heater_bed.temperature': 71.3, 'extruder.temperature': 180.0,
        'temperature_sensor chamber.temperature': 41.0, 'fan.speed': 0.6,
        'motion_report.live_extruder_velocity': -2.5,
        'print_stats.info.current_layer': 37}
CTXS = [
    {'progress': 0.0, 'heat': 0.0, 'pos': [0.0, 0.0, 0.0]},
    {'progress': 0.437, 'heat': 0.61, 'pos': [0.3, 0.75, 0.1], 'vars': VARS},
    {'progress': 1.0, 'heat': 1.0, 'pos': [1.0, 1.0, 1.0],
     'vars': {'heater_bed.temperature': 130.0, 'fan.speed': True},
     'ranges': {'heater_bed.temperature': [20.0, 110.0]}},
]

# scene plans: [strips, groups, candidates]
PLANS = [
    [[['a', 5], ['b', 7], ['c', 3]], [['front', ['a', 'b']]],
     [['idle', 0, 1.0, {'all': 'x'}], ['cancel', 50, 5.0, {'front': 'g'}]]],
    [[['a', 5], ['b', 7], ['c', 3]], [['front', ['b', 'a']]],
     [['idle', 0, 1.0, {'c': 'y'}], ['s1', 50, 5.0, {'all': 'p'}],
      ['s2', 50, 9.0, {'a': 'q'}], ['s3', 60, 2.0, {'front': 'r', 'b': 's'}]]],
    [[['a', 1], ['b', 2]], [],
     [['error', 100, 3.0, {}], ['s1', 100, 3.0, {'a': 'z'}]]],
    [[['a', 4]], [['g1', ['a']], ['g2', ['a']]],
     [['heating', 20, 0.0, {'g2': 'm', 'g1': 'n'}]]],
    [[['a', 4], ['b', 4]], [['g', ['a', 'zz', 'b']]], []],
]
TRIGGERS = ['command', 'macro clean_nozzle', 'MACRO  PRINT_START', 'print_cancelled',
            'Extruding', 'retracting', 'heater_bed.temperature > 60',
            'temperature_sensor chamber.temperature>=40.5', 'fan<0.25',
            'extruder.target != 0', 'print_stats.progress == 1', 'x.y <= -3',
            'macro', 'bogus', 'heater_bed.temperature ~ 3', '> 3']

JS = r"""
const E = require(process.argv[process.argv.length - 1]);
const job = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const frames = job.cases.map(c => {
  const [name, params] = E.parseSpec(c.spec);
  const ctx = Object.assign({ t: c.t }, c.ctx);
  return Array.from(E.toBytes(E.render(name, params, c.n, ctx), c.bri));
});
const plans = job.plans.map(([s, g, c]) => E.planStrips(s, g, c));
const triggers = job.triggers.map(t => {
  try { const [k, a] = E.parseTrigger(t); return [k, a, E.formatTrigger(k, a)]; } catch (e) { return 'ERR'; }
});
const specs = job.specs.map(s => { const [n, p] = E.parseSpec(s); return [n, p, E.formatSpec(n, p)]; });
process.stdout.write(JSON.stringify({ frames, plans, triggers, specs }));
"""


def py_trigger(t):
    try:
        k, a = E.parse_trigger(t)
        return [k, list(a) if isinstance(a, tuple) else a, E.format_trigger(k, a)]
    except E.SpecError:
        return 'ERR'


def norm(o):
    """JSON round trip (tuples -> lists, ints == floats compare equal)."""
    return json.loads(json.dumps(o))


def main():
    cases = []
    for spec in SPECS:
        for n in COUNTS:
            for t in TIMES:
                for ctx in CTXS:
                    cases.append({'spec': spec, 'n': n, 't': t, 'ctx': ctx,
                                  'bri': 0.8 if n == 37 else 1.0})
    py = []
    for c in cases:
        name, params = E.parse_spec(c['spec'])
        ctx = dict(c['ctx'], t=c['t'])
        py.append(list(E.to_bytes(E.render(name, params, c['n'], ctx),
                                  c['bri'])))
    js_path = os.path.join(ROOT, 'web', 'lightspeed_engine.js')
    job = {'cases': cases, 'plans': PLANS, 'triggers': TRIGGERS, 'specs': SPECS}
    res = subprocess.run([NODE, '-e', JS, js_path],
                         input=json.dumps(job).encode(),
                         stdout=subprocess.PIPE, check=True)
    out = json.loads(res.stdout)
    js = out['frames']
    worst = 0
    bad = 0
    for c, a, b in zip(cases, py, js):
        if len(a) != len(b):
            print('LENGTH', c)
            bad += 1
            continue
        d = max([abs(x - y) for x, y in zip(a, b)] or [0])
        worst = max(worst, d)
        if d > 1:
            bad += 1
            if bad <= 5:
                print('DIFF %d' % d, c)
    # scene plans
    for pl, jp in zip(PLANS, out['plans']):
        pp = norm(E.plan_strips([tuple(x) for x in pl[0]], pl[1], pl[2]))
        if pp != jp:
            bad += 1
            print('PLAN', pl, pp, jp)
    # expected plan results (the rules, not only the twins agreeing)
    p0 = E.plan_strips(*PLANS[0])
    assert p0['a'] == ('cancel', 'front', 'g', 0, 12), p0
    assert p0['b'] == ('cancel', 'front', 'g', 5, 12), p0
    assert p0['c'] == ('idle', 'all', 'x', 0, 3), p0
    p1 = E.plan_strips(*PLANS[1])
    assert p1['b'][:3] == ('s3', 'b', 's'), p1           # own line beats group
    assert p1['a'] == ('s3', 'front', 'r', 7, 12), p1    # group order b, a
    assert p1['c'][:2] == ('s1', 'all'), p1              # 50 beats idle
    p2 = E.plan_strips(*PLANS[2])
    assert p2['a'][0] == 's1' and p2['b'][0] is None, p2  # tie: list order
    assert E.plan_strips(*PLANS[4])['a'] == (None, None, None, 0, 4)
    # triggers
    for t, jt in zip(TRIGGERS, out['triggers']):
        pt = norm(py_trigger(t))
        if pt != jt:
            bad += 1
            print('TRIGGER', t, pt, jt)
    assert E.parse_trigger('macro clean_nozzle') == ('macro', 'CLEAN_NOZZLE')
    assert py_trigger('bogus') == 'ERR' and py_trigger('macro') == 'ERR'
    # parsed params + formatted spec
    for s, js_s in zip(SPECS, out['specs']):
        n, p = E.parse_spec(s)
        if norm([n, p, E.format_spec(n, p)]) != js_s:
            bad += 1
            print('SPEC', s, [n, p, E.format_spec(n, p)], js_s)
    # spec round trip
    for spec in SPECS:
        name, params = E.parse_spec(spec)
        assert E.parse_spec(E.format_spec(name, params)) == (name, params), spec
    for badspec in ('heat source=nope', 'progress min=x', 'heat colors=12345',
                    'progress source=a b'):
        try:
            E.parse_spec(badspec)
            bad += 1
            print('ACCEPTED', badspec)
        except E.SpecError:
            pass
    print('%d frames, %d plans, %d triggers, worst byte difference %d, '
          'failures %d' % (len(cases), len(PLANS), len(TRIGGERS), worst, bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
