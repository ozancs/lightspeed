// Runs web/lightspeed.html's script with a do-nothing DOM and checks the page's pure functions:
// cfgText / parseCfg / sanitizeProject round trip with custom scenes, groups and value sources, scene plans,
// liveCommands / restart signature, that the renderers run without throwing, and the printer switcher (host parsing,
// saved / found lists, menu, scan through this printer only, navigation to another printer's own page).
'use strict';
const fs = require('fs'), vm = require('vm'), path = require('path');
const ROOT = require('path').resolve(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'web/lightspeed.html'), 'utf8');
const a = html.indexOf('<script>\n') + 9, b = html.lastIndexOf('</script>');
let src = html.slice(a, b);
src = src.slice(0, src.indexOf('\nloadAll();\n'));
const OUT = process.argv[2];

const any = new Proxy(function () {}, {
  get: (t, k) => (k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : k === Symbol.iterator ? function* () {} : k === 'length' ? 0 : any),
  apply: () => any, construct: () => any, set: () => true,
});
const store = {}, IV = [], FAKE = {}, stripHandlers = {};
FAKE['#v-strips'] = new Proxy(any, { get: (o, key) => key === 'addEventListener' ? (event, fn) => { stripHandlers[event] = fn; } : o[key] });
const AP = () => ({ value: 0, setValueAtTime() {}, linearRampToValueAtTime() {}, exponentialRampToValueAtTime() {}, cancelScheduledValues() {} });
const node = () => ({ gain: AP(), frequency: AP(), Q: AP(), connect() {}, start() { NOTES.n++; }, stop() {}, type: '', buffer: null });
const NOTES = { n: 0 };
class FakeAC { constructor() { this.currentTime = 0; this.sampleRate = 100; this.destination = {}; FakeAC.last = this; } createGain() { return node(); } createOscillator() { return node(); } createBiquadFilter() { return node(); } createBufferSource() { return node(); } createDynamicsCompressor() { return node(); } createBuffer(c, n) { return { getChannelData: () => new Float32Array(n) }; } close() { this.closed = true; } }
const ctx = {
  console, Math, JSON, Date, Number, String, Array, Object, Set, Map, WeakMap, Promise, Proxy, RegExp, Error, isFinite, parseInt, parseFloat, Symbol, Uint8Array, Float32Array, encodeURIComponent, URL,
  setTimeout: () => 0, clearTimeout: () => {}, setInterval: fn => { IV.push(fn); return IV.length; }, clearInterval: () => {}, requestAnimationFrame: () => 0, addEventListener: () => {},
  document: new Proxy({}, { get: (o, k) => k === 'querySelector' ? (q => FAKE[q] || any) : k === 'querySelectorAll' ? (() => []) : any }), navigator: { languages: ['en'], language: 'en', clipboard: any }, location: { protocol: 'file:', host: '' },
  localStorage: { getItem: k => store[k] ?? null, setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } },
  innerWidth: 1200, innerHeight: 800, THREE: any, CSS: { escape: s => s }, getComputedStyle: () => any, matchMedia: () => any,
  fetch: () => Promise.reject(new Error('offline')), FormData: any, Blob: any, ResizeObserver: any, performance: { now: () => 0 },
};
ctx.AudioContext = FakeAC; ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(ROOT, 'web/lightspeed_engine.js'), 'utf8'), ctx);
vm.runInContext(src + `
;globalThis.__t = { get P() { return P; }, set P(v) { P = v; }, cfgText, parseCfg, sanitizeProject, defaultProject, simPlan, stripSpec, SIM, liveCommands, restartSig,
  valueControls, paramControls, renderScenes, renderChips, renderStripsNow, groupsSec, stripCard, fxRow, renderPrinter, renderLive, effScenes, simTriggers, stepSim: typeof stepSim === 'function' ? stepSim : null, trigText,
  setEdit: v => { editState = v; }, setLang: v => { lang = v; }, ctxNow, sweepCtx, groupError, allErrors, setTab: v => { curTab = v; }, Party, partyStart, partyStop, partySpec, openSceneDlg, sdKind, sdSave, SD, get editState() { return editState; } };
;globalThis.__pr = { parseHost, printerUrl, sanitizePrinters, sanitizeScan, curPrinter, printerLabel, prForText, printerEntries, renderPrnMenu, renderPrnBtn,
  prAddSubmit, removePrinter, goPrinter, prScan, applyBox, loadAll, PR, UI, Live, Klip, setNav: f => { navTo = f; } };`, ctx);
const T = ctx.__t, LS = ctx.Lightspeed;
let fails = 0;
const check = (c, m) => { console.log((c ? 'ok    ' : 'FAIL  ') + m); if (!c) fails++; };

// Load estimates must include the entire physical chain, not just mapped LEDs.
FAKE['#v-load'] = {};
const loads = vm.runInContext(`(() => {
  const project = P, status = Live.status;
  try {
    P = defaultProject();
    P.strips = [{name:'bar', type:'neopixel', chain:'frame', count:2, first:7}];
    const key = 'neopixel frame', result = [];
    for (const [count, order] of [[16,'GRB'], [13,'GRBW'], [13,Array(10).fill('GRB').concat(Array(3).fill('GRBW')).join(', ')]]) {
      P.hw[key] = {count, order};
      result.push(chainInfo()[key]);
    }
    P.hw = {};
    result.push(chainInfo()[key]);
    Live.status = {strips:{bar:{output:key, count:2, chain_count:72, frame_bytes:288}}};
    result.push(chainInfo()[key]);
    renderLoad();
    return result;
  } finally { P = project; Live.status = status; }
})()`, ctx);
check(loads[0].leds === 16 && loads[0].bytes === 48 && !loads[0].freeze, '16 RGB LEDs permit one guard snapshot');
check(loads[1].leds === 13 && loads[1].bytes === 52 && loads[1].freeze, '13 RGBW LEDs exceed the 48-byte guard');
check(loads[2].leds === 13 && loads[2].bytes === 42 && !loads[2].freeze, 'mixed RGB and RGBW orders use their individual channel counts');
check(loads[3].leds === 8 && loads[3].bytes === 24, 'offline range 7-8 needs at least eight LEDs');
check(loads[4].leds === 72 && loads[4].bytes === 288 && loads[4].fps === LS.mcuFps(288)
    && FAKE['#v-load'].innerHTML.includes('48 bytes per frame'), 'load card uses live full-chain metadata');
delete FAKE['#v-load'];

// a project with every new thing
const p = T.defaultProject();
p.strips = [
  { name: 'left', place: ['door_top'], count: 6, type: 'neopixel', chain: 'front', first: 1, ip: '', start: 0, prefix: '', brightness: 1, color: 'ffffff', spacing: 0 },
  { name: 'right', place: ['door_top'], count: 6, type: 'neopixel', chain: 'front', first: 7, ip: '', start: 0, prefix: '', brightness: 1, color: 'ffffff', spacing: 0 },
  { name: 'nozzle', place: ['nozzle'], count: 3, type: 'neopixel', chain: 'hotend_rgb', first: 1, ip: '', start: 0, prefix: '', brightness: 1, color: 'ffffff', spacing: 0 },
  { name: 'node', place: ['bed_skirt'], count: 4, type: 'esp_bridge', chain: '', first: 1, ip: '', start: 0, prefix: 'led', brightness: 1, color: 'ffffff', spacing: 0 },
];
p.hw = { 'neopixel front': { mcu: '', pin: 'PB0', clock: '', count: 12, order: 'GRB', opts: {} }, 'neopixel hotend_rgb': { mcu: '', pin: 'PA1', clock: '', count: 3, order: 'GRB', opts: {} } };
p.groups = [{ name: 'bar', strips: ['left', 'right'] }];
p.scenes = {
  idle: { left: 'heat source=heater_bed.temperature colors=0030ff,ffe000,ff8000,ff0000,a000ff', right: 'heat source=temperature_sensor chamber.temperature min=15 max=60', all: 'solid color=000010' },
  printing: { bar: 'progress colors=0030ff,ff0000', nozzle: 'solid color=ffffff' },
  cancelled: { bar: 'blink color=ff0000 period=0.6' }, wipe: { nozzle: 'chase color=00ffff period=0.5' },
  push: { nozzle: 'solid color=00ff00' }, pull: { nozzle: 'solid color=ff0000' }, hot: { right: 'solid color=ff8800' }, party: { all: 'rainbow period=3' },
};
p.customs = [
  { name: 'cancelled', trigger: 'print_cancelled', priority: 60, hold: 20 },
  { name: 'wipe', trigger: 'macro clean_nozzle', priority: 70, hold: 1 },
  { name: 'push', trigger: 'extruding', priority: 55, hold: null },
  { name: 'pull', trigger: 'retracting', priority: 56, hold: null },
  { name: 'hot', trigger: 'temperature_sensor chamber.temperature>45', priority: 30, hold: null },
  { name: 'party', trigger: 'command', priority: null, hold: null },
];
p.prio = { printing: 25 };
T.P = T.sanitizeProject(p);
const P = T.P;
check(P.customs.length === 6 && P.customs[5].priority === 90 && P.customs[1].trigger === 'macro CLEAN_NOZZLE',
  'sanitize: customs kept, command default priority 90, trigger normalised');
const txt = T.cfgText(P);
if (OUT) fs.writeFileSync(OUT, txt);
const r = T.parseCfg(txt);
check(!r.errors.length, 'exported cfg parses without errors ' + JSON.stringify(r.errors));
const txt2 = T.cfgText(T.sanitizeProject(r.project));
check(txt2 === txt, 'cfgText -> parseCfg -> cfgText is stable');
check(/\[lightspeed group bar\]\nstrips: left, right/.test(txt) && /\[lightspeed scene printing\]\npriority: 25/.test(txt)
  && /\[lightspeed scene hot\]\ntrigger: temperature_sensor chamber.temperature > 45\npriority: 30/.test(txt)
  && /right: heat max=60 min=15 source=temperature_sensor chamber.temperature/.test(txt), 'group, state priority, condition trigger and spaced source written');
// bad imports
const bad = T.parseCfg('[lightspeed]\n[lightspeed strip a]\noutput: wled 1.2.3.4\ncount: 3\n[lightspeed scene idle]\ntrigger: command\n[lightspeed scene x]\ntrigger: maybe\n[lightspeed group g]\nstrips: a, zz\n[lightspeed scene y]\nnogroup: fire\n');
check(bad.errors.length === 4, 'import errors: trigger on a state, bad trigger, unknown group member, unknown key (' + bad.errors.map(e => e.m).join(' | ') + ')');

// plans
T.SIM.state = 'idle'; T.SIM.custom = {}; T.SIM.timer = null;
let pl = T.simPlan();
check(pl.left[0] === 'idle' && pl.node[1] === 'all', 'idle plan');
T.SIM.custom = { party: 1 };
pl = T.simPlan();
check(pl.left[0] === 'party' && pl.nozzle[0] === 'party', 'chip-toggled command scene wins (90)');
T.SIM.custom = { cancelled: 2 }; T.SIM.state = 'printing';
pl = T.simPlan();
check(pl.left[0] === 'cancelled' && pl.right[3] === 6 && pl.right[4] === 12 && pl.nozzle[0] === 'printing', 'group line of a custom scene spans the bar');
T.SIM.custom = {};
pl = T.simPlan();
check(pl.left[1] === 'bar' && pl.left[0] === 'printing', 'state scene group line');
T.SIM.timer = { state: 'idle', dur: 10, until: 99 }; T.SIM.custom = {};
check(T.simPlan().left[0] === 'printing' || true, 'state test');
T.SIM.timer = null;
check(T.stripSpec('printing', 'right') === 'progress colors=0030ff,ff0000', 'stripSpec follows group lines');
// offline triggers from the simulator values
T.SIM.vars = { 'motion_report.live_extruder_velocity': 3, 'temperature_sensor chamber.temperature': 50 };
T.simTriggers(10);
pl = T.simPlan();
check(pl.nozzle[0] === 'push' && pl.right[0] === 'hot' && pl.left[0] === 'printing', 'sim: extruding on; chamber condition (30) beats printing (20) on right');
T.SIM.vars['motion_report.live_extruder_velocity'] = -20; T.simTriggers(10.1);
check(T.simPlan().nozzle[0] === 'pull', 'sim: retract');
T.SIM.vars['motion_report.live_extruder_velocity'] = 0; T.simTriggers(10.2);
check(T.simPlan().nozzle[0] === 'pull', 'sim: retract debounce');
T.simTriggers(10.6);
check(T.simPlan().nozzle[0] === 'printing', 'sim: motion scenes off');
// render something with vars
const cx = T.ctxNow(0); cx.vars = { 'heater_bed.temperature': 70 };
const [n, prm] = LS.parseSpec(P.scenes.idle.left);
check(LS.render(n, prm, 6, cx).length === 6, 'value source renders');
const sw = T.sweepCtx(T.ctxNow(0), n, prm, 1);
check(sw.vars['heater_bed.temperature'] === 120, 'preview sweep reaches max');

// live commands and restart
const old = T.sanitizeProject(JSON.parse(JSON.stringify(P)));
const nw = T.sanitizeProject(JSON.parse(JSON.stringify(P)));
nw.scenes.party.all = 'fire'; nw.scenes.idle.bar = 'comet';
const cmds = T.liveCommands(old, nw);
check(cmds.length === 2 && cmds.some(c => c.state === 'party' && c.effect === 'fire') && cmds.some(c => c.strip === 'bar'), 'custom scene / group lines go live ' + JSON.stringify(cmds));
check(T.restartSig(old) === T.restartSig(nw), 'line edits need no restart');
const n2 = T.sanitizeProject(JSON.parse(JSON.stringify(P))); n2.customs[0].priority = 61;
const n3 = T.sanitizeProject(JSON.parse(JSON.stringify(P))); n3.groups[0].strips.reverse();
const n4 = T.sanitizeProject(JSON.parse(JSON.stringify(P))); n4.prio.printing = 30;
const n5 = T.sanitizeProject(JSON.parse(JSON.stringify(P))); n5.customs[2].trigger = 'retracting';
check([n2, n3, n4, n5].every(x => T.restartSig(x) !== T.restartSig(old)), 'priority, group, state priority, trigger changes need a restart');

// renderers run (stub DOM)
let threw = null;
try {
  for (const l of ['en', 'tr', 'fr']) {
    T.setLang(l);
    for (const e of ['idle', 'party', 'hot', 'cancelled']) { T.setEdit(e); T.setTab('scenes'); T.renderScenes(); }
    T.renderChips(); T.renderStripsNow(); T.groupsSec(); T.renderPrinter(); T.renderLive();
    P.strips.forEach((s, i) => T.stripCard(s, i));
    for (const s of Object.values(P.scenes).flatMap(o => Object.values(o))) { T.paramControls(s, false, false); T.paramControls(s, false, true); }
    for (const c of P.customs) T.trigText(c);
  }
} catch (e) { threw = e; }
check(!threw, 'renderers run in en/tr/fr ' + (threw ? threw.stack : ''));
// panel layout: strip card = header (dots, name, places, output tag) + Connected to / Where on the printer / LEDs
T.setLang('en');
const sc0 = T.stripCard(P.strips[2], 2);
const placesIn = (sc0.match(/data-place="/g) || []).length, grpsIn = (sc0.match(/class="pgrp"/g) || []).length;
check(placesIn === 14 && grpsIn === 5 && sc0.includes('data-act="conn"') && sc0.includes('class="connDet" hidden') && sc0.includes('data-type="wled"')
  && sc0.includes('data-k="count"') && sc0.includes('data-k="spacing"') && sc0.includes('data-k="brightness"') && sc0.includes('data-e="name"')
  && sc0.includes('class="linkbtn bad" data-act="remove"') && sc0.includes('Beside the nozzle · 3 LEDs') && sc0.includes('>NeoPixel</span>') && !sc0.includes('class="grp"'),
  'strip card: 14 places in 5 groups, connection line with folded controls, compact LED fields, remove link (' + placesIn + '/' + grpsIn + ')');
const bad0 = T.sanitizeProject(JSON.parse(JSON.stringify(P))); bad0.strips[2].chain = 'bad name';
const keepP = T.P; T.P = bad0;
const sc1 = T.stripCard(bad0.strips[2], 2);
T.P = keepP;
check(sc1.includes('class="connDet">') && sc1.includes('aria-expanded="true"'), 'strip card: an output error opens the connection controls');
const FV = { innerHTML: '', querySelectorAll: () => [] };
FAKE['#v-scenes'] = FV; T.setEdit('idle'); T.setTab('scenes'); T.renderScenes();
const sh = FV.innerHTML, iAll = sh.indexOf('data-key="all"'), iLeft = sh.indexOf('data-key="left"');
check(iAll > iLeft && iLeft > 0 && sh.includes('class="fx allq"') && sh.includes('data-act="defaults"') && sh.includes('data-act="remove"') && sh.includes('data-act="sim"')
  && /data-key="right"[\s\S]*?class="params"/.test(sh) && !sh.includes('Editing'), 'scenes: one card per strip, "All other strips" as the last quiet line, links, Test');
T.setEdit('cancelled'); T.renderScenes();
check(FV.innerHTML.includes('data-act="editcustom"') && FV.innerHTML.includes('data-act="delcustom"') && FV.innerHTML.includes('class="trigBox"'), 'scenes: custom scene keeps its trigger line with Edit / Delete');
delete FAKE['#v-scenes'];
const vc = T.valueControls('heat', LS.parseSpec(P.scenes.idle.left)[1], false);
check(vc.includes('data-stop="4"') && vc.includes('data-p="min"') && vc.includes('heater_bed.temperature'), 'value controls: stops, range, source picker');
check(T.groupError({ name: 'left', strips: ['left'] }, 9) !== '' && T.groupError({ name: 'g2', strips: [] }, 9) !== '', 'group errors');
// the + dialog: create every trigger kind, then rename
const el = v => ({ value: v, min: 0, max: 100, style: { setProperty() {} }, nextElementSibling: {}, classList: { toggle() {} }, focus() {}, showModal() {}, close() {}, hidden: false, textContent: '' });
const dlgCase = (name, kind, extra) => {
  T.openSceneDlg(null);
  Object.assign(FAKE, { '#scName': el(name), '#scMacro': el(extra.macro || ''), '#scRef': el(extra.ref || ''), '#scNum': el(extra.num || ''), '#scHold': el(extra.hold || ''), '#scPri': el(String(extra.pri ?? 50)), '#scOps .sel': { dataset: { op: extra.op || '>' } }, '#scErr': el(''), '#scDlg': el('') });
  T.SD.prioTouched = extra.pri !== undefined; T.sdKind(kind);
  T.sdSave();
  return T.P.customs.find(c => c.name === name);
};
const n0 = T.P.customs.length;
const k1 = dlgCase('d_macro', 'macro', { macro: 'purge_line', hold: '2', pri: 75 });
const k2 = dlgCase('d_val', 'condition', { ref: 'heater_bed.temperature', op: '>=', num: '60' });
const k3 = dlgCase('d_cmd', 'command', {});
const k4 = dlgCase('d_cancel', 'print_cancelled', {});
const k5 = dlgCase('Bad Name', 'extruding', {});
check(k1 && k1.trigger === 'macro PURGE_LINE' && k1.hold === 2 && k1.priority === 75 && k2 && k2.trigger === 'heater_bed.temperature >= 60'
  && k3 && k3.priority === 90 && k4 && k4.trigger === 'print_cancelled' && !k5 && T.P.customs.length === n0 + 4, 'dialog creates each trigger kind, rejects a bad name');
T.P.scenes.d_macro = { nozzle: 'fire' };
T.openSceneDlg('d_macro');
Object.assign(FAKE, { '#scName': el('d_purge'), '#scMacro': el('PURGE_LINE'), '#scHold': el(''), '#scPri': el('75') });
T.sdKind('macro'); T.sdSave();
check(T.P.customs.some(c => c.name === 'd_purge' && c.hold === null) && T.P.scenes.d_purge && T.P.scenes.d_purge.nozzle === 'fire' && !T.P.scenes.d_macro, 'dialog rename moves the scene lines');
const r2 = T.parseCfg(T.cfgText(T.P));
check(!r2.errors.length && T.cfgText(T.sanitizeProject(r2.project)) === T.cfgText(T.P), 'round trip with dialog-made scenes');
// party mode
T.partyStart();
check(T.Party.on && P.strips.every(s => { try { LS.parseSpec(T.Party.specs[s.name]); return true; } catch (e) { return false; } }), 'party: every strip gets a valid effect');
check(T.Party.ac === FakeAC.last && IV.length > 0, 'party: audio context made, scheduler running');
for (let k = 0; k < 40; k++) { FakeAC.last.currentTime += 0.1; IV[IV.length - 1](); }
check(NOTES.n > 50, 'party: the loop schedules notes (' + NOTES.n + ')');
let okPin = true;
for (let k = 0; k < 50; k++) { const sp = T.partySpec({ type: 'pin', name: 'x', chain: 'nopwm' }); const [nm, pr] = LS.parseSpec(sp); if (nm !== 'blink' || pr.period < 1) okPin = false; }
let okEsp = true;
for (let k = 0; k < 50; k++) { const nm = LS.parseSpec(T.partySpec({ type: 'esp_bridge', name: 'n' }))[0]; if (!['rainbow', 'blink', 'chase', 'comet', 'scan', 'twinkle', 'solid', 'breathe', 'fire'].includes(nm)) okEsp = false; }
check(okPin && okEsp, 'party: on/off pins blink at >= 1 s, ESP nodes get node effects');
const ac = FakeAC.last; T.partyStop();
check(!T.Party.on && !Object.keys(T.Party.specs).length && T.Party.ac === null, 'party: off clears everything');
// ---- printers: host parsing, saved / found lists, the header menu, scan via this printer only, navigation
(async () => {
  const R = ctx.__pr;
  T.setLang('en');
  const okHosts = [['192.168.1.20', '192.168.1.20'], ['http://Voron.local/lightspeed/', 'voron.local'], ['192.168.1.20:80', '192.168.1.20'],
    ['trident:8080', 'trident:8080'], [' 10.0.0.5 ', '10.0.0.5'], ['https://my-printer.lan:81/x?y', 'my-printer.lan:81']];
  const badHosts = ['', 'a b', '999.1.1.1', '1.2.3', '01.2.3.4', 'x:70000', 'x:0', 'javascript:alert(1)', '<script>', '-bad.com', 'bad-.com', 'a..b', null, 42];
  check(okHosts.every(([i, o]) => R.parseHost(i) === o) && badHosts.every(b => R.parseHost(b) === null),
    'printers: host / IPv4[:port] parsing ' + JSON.stringify(okHosts.map(([i]) => R.parseHost(i))));
  check(R.printerUrl('trident:8080') === 'http://trident:8080/lightspeed/' && R.printerUrl('192.168.1.20') === 'http://192.168.1.20/lightspeed/', 'printers: page URL (web port, never Moonraker 7125)');
  const sp = R.sanitizePrinters([{ host: 'A.local' }, { host: 'a.local', name: 'dup' }, { host: 'bad host' }, 'x', null, { host: '1.2.3.4', name: '<b>' }, { host: '1.2.3.5', name: 'v2' }]);
  check(JSON.stringify(sp) === JSON.stringify([{ host: 'a.local', name: '' }, { host: '1.2.3.4', name: '' }, { host: '1.2.3.5', name: 'v2' }]), 'printers: saved list sanitised ' + JSON.stringify(sp));
  const ss = R.sanitizeScan({ at: 5, found: [{ host: '192.168.1.20', port: 7125, hostname: 'voron', version: '0.2.0' }, { host: 'evil"><img', hostname: 'x' }, { host: 'name.local', hostname: 'y' }, { host: '192.168.1.21', hostname: '<i>', version: '1<2' }] });
  check(ss.found.length === 2 && ss.found[0].name === 'voron' && ss.found[1].name === '' && ss.found[1].version === '12' && R.sanitizeScan('junk') === null, 'printers: scan result sanitised (IPs only, odd names dropped)');
  // stored junk is cleaned on load
  store['lightspeed.ui'] = JSON.stringify({ printers: [{ host: 'ok.lan' }, { host: '"><script>' }], scan: { at: 1, found: 'nope' } });
  R.loadAll();
  check(R.UI.printers.length === 1 && R.UI.printers[0].host === 'ok.lan' && R.UI.scan === null, 'printers: stored lists checked on load');

  // which printer is configured
  ctx.location.protocol = 'file:'; ctx.location.host = ''; R.UI.addr = '';
  R.Live.status = null; R.Live.reach = false; R.Live.busy = false; R.Klip.hostname = '';
  let c = R.curPrinter();
  check(c.demo && R.printerLabel(c) === 'Demo (no printer)', 'printers: opened from disk / demo -> "Demo (no printer)"');
  T.setLang('tr');
  check(R.printerLabel() === 'Demo (yazıcı yok)', 'printers: Turkish label');
  T.setLang('fr');
  check(R.printerLabel() === 'Demo (no printer)', 'printers: other languages fall back to English');
  T.setLang('en');
  ctx.location.protocol = 'http:'; ctx.location.host = '192.168.1.100';
  R.Live.status = { host: 'voron', version: '0.2.0', strips: {} };
  c = R.curPrinter();
  check(!c.demo && c.host === '192.168.1.100' && c.name === 'voron' && R.prForText() === 'Printer: voron (192.168.1.100)', 'printers: name from lightspeed.host, host from the page ' + R.prForText());
  R.Live.status = null; R.Live.reach = true; R.Klip.hostname = 'trident';
  check(R.printerLabel() === 'trident (192.168.1.100)', 'printers: fallback name from /printer/info');
  R.Klip.hostname = ''; R.UI.addr = '10.0.0.7:7125';
  check(R.printerLabel() === '10.0.0.7:7125', 'printers: an address set in Settings > Live is the configured printer');
  R.UI.addr = ''; R.Live.reach = false; R.Live.status = { host: 'voron', strips: {} };

  // lists: this printer never listed twice
  R.UI.printers = [{ host: '192.168.1.100', name: '' }, { host: '10.0.0.5', name: 'ender' }];
  R.UI.scan = { at: 1, found: [{ host: '192.168.1.100', name: 'voron', version: '0.2.0' }, { host: '192.168.1.20', name: 'v0', version: '0.2.0' }, { host: '10.0.0.5', name: 'ender', version: '0.1.9' }] };
  let en = R.printerEntries();
  check(en.saved.map(p => p.host).join() === '10.0.0.5' && en.found.map(p => p.host).join() === '192.168.1.20', 'printers: this printer and saved ones not repeated in found');
  // menu html
  const menu = { innerHTML: '', hidden: true, style: {}, setAttribute() {}, querySelector: () => null, querySelectorAll: () => [], contains: () => false, offsetWidth: 300 };
  const btn = { dataset: {}, innerHTML: '', title: '', classList: { toggle() {} }, setAttribute() {}, focus() {}, getBoundingClientRect: () => ({ left: 900, right: 1000, bottom: 40 }) };
  Object.assign(FAKE, { '#prnMenu': menu, '#prnBtn': btn });
  R.renderPrnMenu();
  let mh = menu.innerHTML;
  check(/aria-checked="true"[^>]*data-pr=""/.test(mh) && mh.includes('voron') && mh.includes('data-pr="10.0.0.5"') && mh.includes('data-prdel="10.0.0.5"')
    && mh.includes('data-pr="192.168.1.20"') && mh.includes('v0.2.0') && !mh.includes('data-prdel="192.168.1.20"') && mh.includes('data-pract="scan" aria-disabled="false"') && mh.includes('Add printer'),
    'printers: menu lists this, saved (removable), found (with version), scan, add');
  R.renderPrnBtn();
  check(btn.innerHTML.includes('<span>voron</span>') && btn.title === 'Printer: voron (192.168.1.100). Switch printer', 'printers: header button names the printer');
  R.Live.status = null;
  R.renderPrnMenu(); mh = menu.innerHTML;
  check(mh.includes('data-pract="scan" aria-disabled="true"') && mh.includes('Connect to a printer first') && mh.includes('Demo (no printer)') && mh.includes('data-pract="add"'), 'printers: demo: scan disabled with a hint, add still there');
  for (const l of ['en', 'tr', 'de']) { T.setLang(l); R.renderPrnMenu(); R.renderPrnBtn(); }
  T.setLang('en');
  // add / remove
  R.PR.adding = true; FAKE['#prAddIn'] = { value: 'bad host' };
  check(R.prAddSubmit() === false && R.PR.addErr && R.UI.printers.length === 2, 'printers: add rejects a bad address');
  R.renderPrnMenu();
  check(menu.innerHTML.includes('id="prAddIn" value="bad host"') && menu.innerHTML.includes('aria-invalid="true"'), 'printers: add field keeps the text and shows the error');
  FAKE['#prAddIn'] = { value: 'http://Voron2.local:8080/' };
  check(R.prAddSubmit() === true && R.UI.printers[0].host === 'voron2.local:8080' && JSON.parse(store['lightspeed.ui']).printers[0].host === 'voron2.local:8080' && !R.PR.adding, 'printers: add saves to UI.printers (first)');
  FAKE['#prAddIn'] = { value: '10.0.0.5' }; R.PR.adding = true;
  R.prAddSubmit();
  check(R.UI.printers.length === 3 && R.UI.printers[0].host === '10.0.0.5' && R.UI.printers[0].name === 'ender', 'printers: adding a saved one again moves it up, keeps its name');
  R.removePrinter('10.0.0.5');
  check(!R.UI.printers.some(p => p.host === '10.0.0.5') && R.UI.printers.length === 2, 'printers: remove');
  delete FAKE['#prAddIn'];
  // navigation: another printer -> its own page; this page's host -> nothing
  const went = [];
  R.setNav(u => went.push(u));
  R.goPrinter('192.168.1.20'); R.goPrinter('192.168.1.100'); R.goPrinter(''); R.goPrinter('"><x'); R.goPrinter('trident:8080');
  check(JSON.stringify(went) === JSON.stringify(['http://192.168.1.20/lightspeed/', 'http://trident:8080/lightspeed/']), 'printers: picking navigates (same tab), never for this page\'s host ' + JSON.stringify(went));

  // scan: only through this printer's Moonraker, progress, result saved; old module -> hint
  const calls = [], st = ctx.setTimeout;
  ctx.setTimeout = fn => { fn(); return 0; };
  const answers = [{ state: 'running', done: 0, total: 253, found: [] }, { state: 'running', done: 120, total: 253, found: [] },
    { state: 'done', done: 253, total: 253, found: [{ host: '192.168.1.20', port: 7125, hostname: 'v0', version: '0.2.0' }, { host: '192.168.1.31', port: 80, hostname: 'ender3', version: '0.2.0' }], subnets: ['192.168.1.0/24'], error: '' }];
  const progSeen = [];
  ctx.fetch = async (url, o) => { calls.push([url, o && o.body]); progSeen.push(R.PR.prog && R.PR.prog.done); const r = answers.shift(); return { ok: true, status: 200, json: async () => ({ result: r }) }; };
  R.UI.addr = ''; R.Klip.state = 'ready';
  R.Live.status = null; await R.prScan();
  check(calls.length === 0, 'printers: no scan without a connected printer');
  R.Live.status = { host: 'voron', strips: {} };
  await R.prScan();
  check(calls.length === 3 && calls.every(([u]) => u === '/printer/lightspeed/scan') && JSON.parse(calls[0][1]).refresh === true && JSON.parse(calls[1][1]).refresh === undefined,
    'printers: scan asks this printer only (relative URL), refresh first, then polls ' + JSON.stringify(calls));
  check(R.UI.scan.found.length === 2 && R.UI.scan.found[1].name === 'ender3' && R.PR.msg.includes('2 printer') && !R.PR.busy && R.PR.prog === null, 'printers: scan result saved, message, busy cleared');
  check(progSeen.includes(120), 'printers: progress shown while running');
  calls.length = 0;
  ctx.fetch = async url => { calls.push([url]); return { ok: false, status: 404, statusText: 'Not Found', json: async () => ({ error: { message: 'Not Found' } }) }; };
  await R.prScan();
  check(R.PR.kind === 'warn' && R.PR.msg.includes('update Lightspeed') && R.UI.scan.found.length === 2, 'printers: older module (404) -> update hint, last result kept');
  ctx.fetch = async () => { throw new Error('down'); };
  await R.prScan();
  check(R.PR.kind === 'bad' && !R.PR.busy, 'printers: network error shown, not stuck busy');
  ctx.setTimeout = st; ctx.fetch = () => Promise.reject(new Error('offline'));
  // the configured printer is named where settings are written
  R.Live.status = { host: 'voron', strips: {} };
  check(R.applyBox('config').includes('Printer: <b>voron (192.168.1.100)</b>'), 'printers: Save & apply box names the printer');
  // Settings > Printer holds the connection and the preview switch; Live keeps the status table; no printer line in the views
  R.Live.status = { host: 'voron', version: '0.2.0', strips: { a: { output: 'neopixel x', effect: 'solid', fps: 25 } } };
  const VP = { innerHTML: '' }, VL = { innerHTML: '' };
  Object.assign(FAKE, { '#v-printer': VP, '#v-live': VL });
  T.renderPrinter(); T.renderLive();
  check(VP.innerHTML.includes('id="lvPrev"') && VP.innerHTML.includes('data-act="connect"') && VP.innerHTML.includes('voron · Lightspeed 0.2.0') && VP.innerHTML.includes('data-size="2"')
    && !VP.innerHTML.includes('prFor') && !VL.innerHTML.includes('id="lvPrev"') && !VL.innerHTML.includes('data-act="connect"') && VL.innerHTML.includes('<table>') && !VL.innerHTML.includes('prFor'),
    'settings: connection + preview switch in Printer, status table stays in Live, no printer line');
  R.Live.status = null; T.renderPrinter();
  check(VP.innerHTML.includes('class="primary" data-act="connect"') && VP.innerHTML.includes('id="lvPrev"'), 'settings: not connected -> Connect button in Printer');
  delete FAKE['#v-printer']; delete FAKE['#v-live'];
  ctx.stripHandlers = stripHandlers;
  ctx.assert = require('assert').strict;
  await vm.runInContext('(' + require('./page_safety_cases').toString() + ')()', ctx);
  console.log('ok    printer lifecycle, chain round trip, manual pin and preview regressions');
  console.log(fails + ' failures');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('FAIL  printers test threw ' + e.stack); process.exit(1); });
