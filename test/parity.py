"""Python engine vs JS engine: same frames, byte for byte (+-1 rounding).

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
]
COUNTS = [1, 3, 8, 37, 120]
TIMES = [0.0, 0.016, 0.5, 1.234, 7.77, 59.99, 1234.5678, 86400.125]
CTXS = [
    {'progress': 0.0, 'heat': 0.0, 'pos': [0.0, 0.0, 0.0]},
    {'progress': 0.437, 'heat': 0.61, 'pos': [0.3, 0.75, 0.1]},
    {'progress': 1.0, 'heat': 1.0, 'pos': [1.0, 1.0, 1.0]},
]

JS = r"""
const E = require(process.argv[process.argv.length - 1]);
const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = cases.map(c => {
  const [name, params] = E.parseSpec(c.spec);
  const ctx = Object.assign({ t: c.t }, c.ctx);
  return Array.from(E.toBytes(E.render(name, params, c.n, ctx), c.bri));
});
process.stdout.write(JSON.stringify(out));
"""


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
    res = subprocess.run([NODE, '-e', JS, js_path],
                         input=json.dumps(cases).encode(),
                         stdout=subprocess.PIPE, check=True)
    js = json.loads(res.stdout)
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
    # spec round trip
    for spec in SPECS:
        name, params = E.parse_spec(spec)
        assert E.parse_spec(E.format_spec(name, params)) == (name, params), spec
    print('%d frames, worst byte difference %d, failures %d'
          % (len(cases), worst, bad))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
