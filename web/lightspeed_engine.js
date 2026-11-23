// Lightspeed - effect engine (browser copy)
//
// Line-for-line port of lightspeed_engine.py. Keep the two identical:
// test/parity.py renders the same frames with both and compares bytes.
// Works in the page (window.Lightspeed) and in node (module.exports).
(function (root) {
  'use strict';

  const VERSION = '0.2.1';

  const STATES = ['shutdown', 'error', 'homing', 'probing', 'paused', 'heating',
    'printing', 'complete', 'idle'];

  const STATE_FALLBACK = {
    shutdown: 'error', error: 'idle', homing: 'idle', probing: 'homing',
    paused: 'printing', heating: 'idle', printing: 'idle', complete: 'idle',
  };

  const EFFECTS = {
    off: {},
    solid: { color: 'ffffff' },
    breathe: { color: 'ffffff', color2: '000000', period: 3.0 },
    blink: { color: 'ffffff', color2: '000000', period: 1.0 },
    rainbow: { period: 5.0 },
    chase: { color: 'ffffff', color2: '000000', period: 2.0, size: 2 },
    comet: { color: 'ffffff', color2: '000000', period: 1.5, size: 0 },
    scan: { color: 'ff0000', color2: '000000', period: 2.0, size: 2 },
    twinkle: { color: 'ffffff', color2: '000000', period: 1.2, density: 0.3 },
    fire: {},
    progress: { color: 'ffffff', color2: 'ff0000', source: 'progress', gradient: 0, min: 'auto', max: 'auto', colors: '' },
    heat: { color: 'ff3000', color2: '0030ff', source: 'heat', min: 'auto', max: 'auto', colors: '' },
    follow: { color: 'ffffff', color2: '000000', axis: 'x', size: 1.5 },
  };
  const COMMON_PARAMS = { reverse: 0 };

  const DEFAULT_SCENES = {
    idle: 'breathe color=1a3cff color2=000814 period=4',
    heating: 'progress source=heat gradient=1 color=0030ff color2=ff2000',
    homing: 'scan color=00ff40 color2=001404 period=1.6',
    probing: 'scan color=a000dc color2=0c0012 period=1.6',
    printing: 'solid color=ffffff',
    paused: 'breathe color=ff6e00 color2=140800 period=2',
    complete: 'breathe color=00ff40 color2=001a08 period=1.5',
    error: 'blink color=ff0000 color2=000000 period=0.6',
    shutdown: 'solid color=ff0000',
  };

  const PARAM_TYPES = {
    color: 'color', color2: 'color', gradient: 'bool', period: 'float', size: 'float',
    density: 'float', source: 'source', axis: ['x', 'y', 'z'],
    reverse: 'bool', min: 'number', max: 'number', colors: 'colors',
  };

  const SOURCE_RE = /^[A-Za-z0-9_]+( [A-Za-z0-9_-]+)?(\.[A-Za-z0-9_]+)+$/;
  const REF_RE = /^[A-Za-z0-9_]+( [A-Za-z0-9_-]+)?(\.[A-Za-z0-9_]+)*$/;

  const BUILTIN_PRIORITY = {
    shutdown: 100, error: 100, homing: 80, probing: 80,
    heating: 20, printing: 20, paused: 20, complete: 20, idle: 0,
  };
  const DEFAULT_PRIORITY = 50;
  const COMMAND_PRIORITY = 90;
  const TRIGGERS = ['command', 'macro', 'print_cancelled', 'extruding', 'retracting', 'condition'];
  const TRIGGER_OPS = ['>=', '<=', '==', '!=', '>', '<'];
  const CANCEL_HOLD = 30.0;
  const MOTION_SPEED = 0.5;

  class SpecError extends Error {}

  // ------------------------------------------------------------ parsing

  const colorCache = new Map();
  function parseColor(text) {
    const key = String(text);
    const hit = colorCache.get(key);
    if (hit) return hit;
    let s = key.trim();
    if (s.startsWith('#')) s = s.slice(1);
    if (s.length === 3) s = s.split('').map(c => c + c).join('');
    if (s.length !== 6 || !/^[0-9a-fA-F]{6}$/.test(s)) {
      throw new SpecError(`colour must be rrggbb, got '${text}'`);
    }
    const v = parseInt(s, 16);
    const c = [((v >> 16) & 255) / 255.0, ((v >> 8) & 255) / 255.0, (v & 255) / 255.0];
    colorCache.set(key, c);
    return c;
  }

  function colorHex(c) {
    return c.map(x => toByte(x).toString(16).padStart(2, '0')).join('');
  }

  function parseSpec(text) {
    const parts = String(text).split(/\s+/).filter(Boolean);
    if (!parts.length) throw new SpecError('empty effect');
    const name = parts[0].toLowerCase();
    if (!(name in EFFECTS)) {
      throw new SpecError(`unknown effect '${name}' (one of ${Object.keys(EFFECTS).sort().join(', ')})`);
    }
    const params = Object.assign({}, EFFECTS[name], COMMON_PARAMS);
    const tokens = [];
    for (const p of parts.slice(1)) {
      // 'source=temperature_sensor chamber.temperature': the second word belongs to source
      if (p.indexOf('=') < 0 && tokens.length && tokens[tokens.length - 1].toLowerCase().startsWith('source=')
        && tokens[tokens.length - 1].indexOf(' ') < 0) { tokens[tokens.length - 1] += ' ' + p; continue; }
      tokens.push(p);
    }
    for (const p of tokens) {
      const eq = p.indexOf('=');
      if (eq < 0) throw new SpecError(`'${p}' should be key=value`);
      const k = p.slice(0, eq).toLowerCase();
      const v = p.slice(eq + 1);
      if (!(k in params)) throw new SpecError(`effect '${name}' has no parameter '${k}'`);
      const t = PARAM_TYPES[k];
      if (t === 'color') {
        params[k] = colorHex(parseColor(v));
      } else if (t === 'float') {
        const f = Number(v);
        if (v === '' || !isFinite(f)) throw new SpecError(`${k} must be a number, got '${v}'`);
        if (f < 0) throw new SpecError(`${k} must not be negative`);
        params[k] = f;
      } else if (t === 'bool') {
        params[k] = ['1', 'true', 'yes', 'on'].includes(v.toLowerCase()) ? 1 : 0;
      } else if (t === 'number') {
        if (v.toLowerCase() === 'auto') params[k] = 'auto';
        else {
          const f = Number(v);
          if (v === '' || !isFinite(f)) throw new SpecError(`${k} must be a number, got '${v}'`);
          params[k] = f;
        }
      } else if (t === 'colors') {
        params[k] = v.split(',').filter(x => x.trim()).map(x => colorHex(parseColor(x))).join(',');
      } else if (t === 'source') {
        if (['progress', 'heat'].includes(v.toLowerCase())) params[k] = v.toLowerCase();
        else if (SOURCE_RE.test(v)) params[k] = v;
        else throw new SpecError(`source must be progress, heat or <object>.<field>, got '${v}'`);
      } else {
        if (!t.includes(v.toLowerCase())) throw new SpecError(`${k} must be one of ${t.join(', ')}`);
        params[k] = v.toLowerCase();
      }
    }
    return [name, params];
  }

  function formatSpec(name, params) {
    const out = [name];
    const defaults = Object.assign({}, EFFECTS[name], COMMON_PARAMS);
    for (const k of Object.keys(params).sort()) {
      if (k in defaults && params[k] !== defaults[k]) {
        let v = params[k];
        if (typeof v === 'number') v = String(Math.round(v * 1000) / 1000);
        out.push(`${k}=${v}`);
      }
    }
    return out.join(' ');
  }

  // ------------------------------------------------------------ helpers

  function toByte(x) {
    if (x <= 0.0) return 0;
    if (x >= 1.0) return 255;
    return Math.floor(x * 255.0 + 0.5);
  }

  function frac(x) { return x - Math.floor(x); }

  // Python's float % (result has the sign of the divisor)
  function pmod(a, b) { return a - b * Math.floor(a / b); }

  function mix(a, b, v) {
    if (v <= 0.0) return a;
    if (v >= 1.0) return b;
    return [a[0] + (b[0] - a[0]) * v, a[1] + (b[1] - a[1]) * v, a[2] + (b[2] - a[2]) * v];
  }

  function hash01(i, k) {
    let x = (Math.imul(i, 0x9E3779B1) + Math.imul(k, 0x85EBCA77) + 0x27D4EB2F) >>> 0;
    x = (x ^ (x >>> 16)) >>> 0;
    x = Math.imul(x, 0x7feb352d) >>> 0;
    x = (x ^ (x >>> 15)) >>> 0;
    x = Math.imul(x, 0x846ca68b) >>> 0;
    x = (x ^ (x >>> 16)) >>> 0;
    return x / 4294967296.0;
  }

  function hsv(h, s, v) {
    h = frac(h) * 6.0;
    const i = Math.floor(h);
    const f = h - i;
    const p = v * (1.0 - s);
    const q = v * (1.0 - s * f);
    const t = v * (1.0 - s * (1.0 - f));
    return [[v, t, p], [q, v, p], [p, v, t], [p, q, v], [t, p, v], [v, p, q]][((i % 6) + 6) % 6];
  }

  function period(p, def) {
    const v = ('period' in p) ? p.period : def;
    return v > 0.05 ? v : 0.05;
  }

  function num(x) {
    if (typeof x === 'boolean') return x ? 1.0 : 0.0;
    if (typeof x === 'number' && isFinite(x)) return x;
    return null;
  }

  function defaultRange(src) {
    if (src === 'progress' || src === 'heat') return [0.0, 1.0];
    const parts = src.split('.'), obj = parts[0], field = parts[parts.length - 1];
    if (field === 'temperature' || field === 'target') {
      if (obj.startsWith('extruder')) return [20.0, 300.0];
      if (obj === 'heater_bed') return [20.0, 120.0];
      return [20.0, 80.0];
    }
    if (field === 'live_extruder_velocity') return [0.0, 10.0];
    return [0.0, 1.0];
  }

  function sourceRange(p, ctx) {
    const src = ('source' in p) ? p.source : 'progress';
    const r = (ctx.ranges && ctx.ranges[src]) || defaultRange(src);
    const lo = (p.min === undefined || p.min === 'auto') ? r[0] : p.min;
    const hi = (p.max === undefined || p.max === 'auto') ? r[1] : p.max;
    return [Number(lo), Number(hi)];
  }

  function sourceValue(p, ctx) {
    const src = ('source' in p) ? p.source : 'progress';
    let raw;
    if (src === 'progress' || src === 'heat') raw = num((src in ctx) ? ctx[src] : 0.0);
    else raw = num(ctx.vars ? ctx.vars[src] : undefined);
    if (raw === null) return 0.0;
    const [lo, hi] = sourceRange(p, ctx);
    if (hi === lo) return raw >= hi ? 1.0 : 0.0;
    return Math.max(0.0, Math.min(1.0, (raw - lo) / (hi - lo)));
  }

  function stops(p) {
    if (!p.colors) return null;
    return p.colors.split(',').map(parseColor);
  }

  function gradientAt(st, v) {
    if (st.length === 1) return st[0];
    const x = Math.max(0.0, Math.min(1.0, v)) * (st.length - 1);
    const i = Math.floor(x);
    if (i >= st.length - 1) return st[st.length - 1];
    return mix(st[i], st[i + 1], x - i);
  }

  function valueColor(name, p, ctx) {
    const v = sourceValue(p, ctx), st = stops(p);
    if (st !== null) return gradientAt(st, v);
    if (name === 'heat') return mix(parseColor(p.color2), parseColor(p.color), v);
    if (p.gradient) return mix(parseColor(p.color), parseColor(p.color2), v);
    return parseColor(p.color);
  }

  const fill = (n, c) => { const o = new Array(n); for (let i = 0; i < n; i++) o[i] = c; return o; };

  // ------------------------------------------------------------ effects

  const FX = {
    off: (n) => fill(n, [0.0, 0.0, 0.0]),
    solid: (n, ctx, p) => fill(n, parseColor(p.color)),
    breathe: (n, ctx, p) => {
      const v = (1.0 - Math.cos(2.0 * Math.PI * ctx.t / period(p, 3.0))) * 0.5;
      return fill(n, mix(parseColor(p.color2), parseColor(p.color), v));
    },
    blink: (n, ctx, p) => {
      const on = frac(ctx.t / period(p, 1.0)) < 0.5;
      return fill(n, parseColor(on ? p.color : p.color2));
    },
    rainbow: (n, ctx, p) => {
      const ph = ctx.t / period(p, 5.0);
      const o = [];
      for (let i = 0; i < n; i++) o.push(hsv(i / n + ph, 1.0, 1.0));
      return o;
    },
    chase: (n, ctx, p) => {
      const a = parseColor(p.color2), b = parseColor(p.color);
      const size = Math.max(1.0, p.size);
      const pos = ctx.t / period(p, 2.0) * n;
      const o = [];
      for (let i = 0; i < n; i++) o.push(pmod(i - pos, 2.0 * size) < size ? b : a);
      return o;
    },
    comet: (n, ctx, p) => {
      const a = parseColor(p.color2), b = parseColor(p.color);
      const size = p.size >= 1.0 ? p.size : Math.max(2.0, n / 3.0);
      const head = frac(ctx.t / period(p, 1.5)) * (n + size);
      const o = [];
      for (let i = 0; i < n; i++) {
        const d = head - i;
        if (d >= 0.0 && d < size) { const v = 1.0 - d / size; o.push(mix(a, b, v * v)); } else o.push(a);
      }
      return o;
    },
    scan: (n, ctx, p) => {
      const a = parseColor(p.color2), b = parseColor(p.color);
      const size = Math.max(0.5, p.size);
      const ph = frac(ctx.t / period(p, 2.0));
      const tri = ph < 0.5 ? ph * 2.0 : 2.0 - ph * 2.0;
      const x = tri * (n - 1);
      const o = [];
      for (let i = 0; i < n; i++) o.push(mix(a, b, 1.0 - Math.abs(i - x) / size));
      return o;
    },
    twinkle: (n, ctx, p) => {
      const a = parseColor(p.color2), b = parseColor(p.color);
      const per = period(p, 1.2);
      const o = [];
      for (let i = 0; i < n; i++) {
        const s = ctx.t / per + hash01(i, -1);
        const k = Math.floor(s);
        if (hash01(i, k) < p.density) o.push(mix(a, b, Math.sin(Math.PI * (s - k)))); else o.push(a);
      }
      return o;
    },
    fire: (n, ctx) => {
      const s = ctx.t * 12.0;
      const k = Math.floor(s);
      let f = s - k;
      f = f * f * (3.0 - 2.0 * f);
      const o = [];
      for (let i = 0; i < n; i++) {
        const h0 = hash01(i, k), h1 = hash01(i, k + 1);
        o.push(fireColor(0.35 + 0.65 * (h0 + (h1 - h0) * f)));
      }
      return o;
    },
    progress: (n, ctx, p) => {
      const off = [0.0, 0.0, 0.0], a = parseColor(p.color);
      const v = sourceValue(p, ctx);
      const lit = v * n;
      const o = [];
      const st = stops(p);
      if (st !== null) {
        for (let i = 0; i < n; i++) {
          const pos = (n === 1 || p.gradient) ? v : i / (n - 1);
          o.push(mix(off, gradientAt(st, pos), lit - i));
        }
        return o;
      }
      if (!p.gradient) {
        for (let i = 0; i < n; i++) o.push(mix(off, a, lit - i));
        return o;
      }
      const b = parseColor(p.color2);
      for (let i = 0; i < n; i++) {
        const pos = n === 1 ? v : i / (n - 1);
        o.push(mix(off, mix(a, b, pos), lit - i));
      }
      return o;
    },
    heat: (n, ctx, p) => fill(n, valueColor('heat', p, ctx)),
    follow: (n, ctx, p) => {
      const a = parseColor(p.color2), b = parseColor(p.color);
      const axis = { x: 0, y: 1, z: 2 }[p.axis];
      const x = Math.max(0.0, Math.min(1.0, ctx.pos[axis])) * (n - 1);
      const size = Math.max(0.5, p.size);
      const o = [];
      for (let i = 0; i < n; i++) o.push(mix(a, b, 1.0 - Math.abs(i - x) / size));
      return o;
    },
  };

  function fireColor(h) {
    if (h < 0.4) return [h / 0.4, 0.0, 0.0];
    if (h < 0.8) return [1.0, (h - 0.4) / 0.4 * 0.55, 0.0];
    return [1.0, 0.55 + (h - 0.8) / 0.2 * 0.35, (h - 0.8) / 0.2 * 0.15];
  }

  function render(name, params, n, ctx) {
    if (n <= 0) return [];
    let out = FX[name](n, ctx, params);
    if (params.reverse) out = out.slice().reverse();
    return out;
  }

  function toBytes(frame, brightness = 1.0) {
    const buf = new Uint8Array(frame.length * 3);
    let j = 0;
    for (const [r, g, b] of frame) {
      buf[j] = toByte(r * brightness);
      buf[j + 1] = toByte(g * brightness);
      buf[j + 2] = toByte(b * brightness);
      j += 3;
    }
    return buf;
  }

  function crossfade(old, nw, v) {
    if (!old || old.length !== nw.length || v >= 1.0) return nw;
    return nw.map((b, i) => mix(old[i], b, v));
  }

  function resolveScene(state, scenes) {
    const seen = new Set();
    while (!(state in scenes) && (state in STATE_FALLBACK) && !seen.has(state)) {
      seen.add(state);
      state = STATE_FALLBACK[state];
    }
    return (state in scenes) ? state : null;
  }

  // ------------------------------------------------------ custom scenes

  function parseTrigger(text) {
    const s = String(text).split(/\s+/).filter(Boolean).join(' ');
    const low = s.toLowerCase();
    if (['command', 'print_cancelled', 'extruding', 'retracting'].includes(low)) return [low, null];
    if (low.startsWith('macro ') || low === 'macro') {
      const name = s.slice(5).trim().toUpperCase();
      if (!/^[A-Z0-9_]+$/.test(name)) throw new SpecError(`trigger 'macro <NAME>' needs a G-code command name, got '${s}'`);
      return ['macro', name];
    }
    const m = s.match(/^(.+?) ?(>=|<=|==|!=|>|<) ?(-?[0-9]*\.?[0-9]+)$/);
    if (m && REF_RE.test(m[1])) return ['condition', [m[1], m[2], Number(m[3])]];
    throw new SpecError(`unknown trigger '${s}' (command, macro <NAME>, print_cancelled, extruding, retracting or <object>.<field> <op> <number>)`);
  }

  function formatTrigger(kind, arg) {
    if (kind === 'macro') return 'macro ' + arg;
    if (kind === 'condition') return `${arg[0]} ${arg[1]} ${String(Math.round(arg[2] * 1000) / 1000)}`;
    return kind;
  }

  function compare(value, op, number) {
    switch (op) {
      case '>': return value > number;
      case '>=': return value >= number;
      case '<': return value < number;
      case '<=': return value <= number;
      case '==': return value === number;
    }
    return value !== number;
  }

  // strips [[name, count]], groups [[name, [members]]], candidates [[scene, priority, since, {key: spec}]]
  // -> {strip: [scene, key, spec, offset, total]} (same rules as plan_strips in the Python engine)
  function planStrips(strips, groups, candidates) {
    const counts = {};
    for (const [n, c] of strips) counts[n] = c;
    const order = candidates.slice().sort((a, b) => (b[1] - a[1]) || (b[2] - a[2]));
    const out = {};
    for (const [name, count] of strips) {
      let hit = null;
      for (const [scene, , , items] of order) {
        if (Object.prototype.hasOwnProperty.call(items, name)) hit = [scene, name, items[name], 0, count];
        else {
          for (const [g, members] of groups) {
            if (Object.prototype.hasOwnProperty.call(items, g) && members.includes(name)) {
              const k = members.indexOf(name);
              let off = 0, total = 0;
              members.forEach((m, j) => { const c = counts[m] || 0; if (j < k) off += c; total += c; });
              hit = [scene, g, items[g], off, total];
              break;
            }
          }
          if (hit === null && Object.prototype.hasOwnProperty.call(items, 'all')) hit = [scene, 'all', items.all, 0, count];
        }
        if (hit !== null) break;
      }
      out[name] = hit || [null, null, null, 0, count];
    }
    return out;
  }

  const US_PER_BYTE = 10.0;
  const MCU_BUDGET = 0.03;
  const MCU_FPS_CAP = 25.0;
  const NET_FPS = 40.0;

  function mcuFps(nbytes) {
    if (nbytes <= 0) return MCU_FPS_CAP;
    return Math.max(1.0, Math.min(MCU_FPS_CAP, MCU_BUDGET / (nbytes * US_PER_BYTE * 1e-6)));
  }

  const api = {
    VERSION, STATES, STATE_FALLBACK, EFFECTS, COMMON_PARAMS, DEFAULT_SCENES, PARAM_TYPES,
    SpecError, parseColor, colorHex, parseSpec, formatSpec, toByte, mix, hash01, hsv,
    render, toBytes, crossfade, resolveScene, mcuFps,
    US_PER_BYTE, MCU_BUDGET, MCU_FPS_CAP, NET_FPS,
    SOURCE_RE, REF_RE, BUILTIN_PRIORITY, DEFAULT_PRIORITY, COMMAND_PRIORITY, TRIGGERS, TRIGGER_OPS, CANCEL_HOLD, MOTION_SPEED,
    defaultRange, sourceRange, sourceValue, gradientAt, valueColor, parseTrigger, formatTrigger, compare, planStrips,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.Lightspeed = api;
})(typeof window !== 'undefined' ? window : this);
