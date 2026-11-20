// Compare the original GLBs with the compressed ones the way the page measures them (no browser, no rendering).
//   node tools/models/verify.js [compressedDir] [originalDir] [--exact]
// defaults: web/models vs models/raw/uncompressed. Loads both with web/third_party three.min.js + GLTFLoader.js +
// meshopt_decoder.js, runs glbFloat (tools/models/glb_float.js) on the compressed scene, then runs the page's own
// cadIslands / lowestTip / cadPrep / cadPrepTh / CAD_ADAPT (cut out of web/lightspeed.html) on both and compares.
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = path.resolve(__dirname, '..', '..'), WEB = path.join(ROOT, 'web');
const ARGS = process.argv.slice(2).filter(a => !a.startsWith('--'));
const CMP = path.resolve(ARGS[0] || path.join(WEB, 'models')), ORG = path.resolve(ARGS[1] || path.join(ROOT, 'models', 'raw', 'uncompressed'));
global.THREE = require(path.join(WEB, 'third_party', 'three.min.js'));
vm.runInThisContext(fs.readFileSync(path.join(WEB, 'third_party', 'GLTFLoader.js'), 'utf8'));
const MeshoptDecoder = require(path.join(WEB, 'third_party', 'meshopt_decoder.js'));
const glbFloat = require(path.join(__dirname, 'glb_float.js'));
const STRICT = process.argv.includes('--exact');  // also require bit-exact positions and triangles
const html = fs.readFileSync(path.join(WEB, 'lightspeed.html'), 'utf8');
const a0 = html.indexOf('const CAD = {'), a1 = html.indexOf('const thKey =');
if (a0 < 0 || a1 < a0) throw new Error('page code not found');
const PAGE = new Function('THREE', html.slice(a0, a1) + '\nreturn { CAD, CAD_ADAPT, CAD_FILES, TH_FILES, cadIslands, lowestTip, cadPrep, cadPrepTh };');

const load = f => new Promise((ok, no) => {
  const b = fs.readFileSync(f), ab = b.buffer.slice(b.byteOffset, b.byteOffset + b.length);
  const L = new THREE.GLTFLoader(); L.setMeshoptDecoder(MeshoptDecoder); L.parse(ab, '', ok, no);
});
const meshesOf = r => { const a = []; r.traverse(o => { if (o.isMesh) a.push(o); }); return a; };
const bb = a => { const q = [1e9, 1e9, 1e9, -1e9, -1e9, -1e9]; for (let i = 0; i < a.length; i++) { const j = i % 3; if (a[i] < q[j]) q[j] = a[i]; if (a[i] > q[j + 3]) q[j + 3] = a[i]; } return q; };
const dmax = (p, q) => p.reduce((s, x, i) => Math.max(s, Math.abs(x - q[i])), 0);
const b3 = b => [b.min.x, b.min.y, b.min.z, b.max.x, b.max.y, b.max.z];
const hex = m => m.material.color.getHexString();
// triangles matched one to one, order-free: every compressed triangle must equal an unused original one within eps
// at every corner, with the same winding (any cyclic rotation)
function sameTris(ga, gb, eps) {
  const A = ga.attributes.position.array, IA = ga.index.array, B = gb.attributes.position.array, IB = gb.index.array;
  if (IA.length !== IB.length) return false;
  const cell = 0.25, ck = (P, I, t) => { let x = 0, y = 0, z = 0; for (let k = 0; k < 3; k++) { x += P[I[t + k] * 3]; y += P[I[t + k] * 3 + 1]; z += P[I[t + k] * 3 + 2]; } return [x / 3 / cell, y / 3 / cell, z / 3 / cell].map(Math.floor); };
  const H = new Map(), used = new Uint8Array(IA.length / 3);
  for (let t = 0; t < IA.length; t += 3) { const k = ck(A, IA, t).join(','); (H.get(k) || H.set(k, []).get(k)).push(t); }
  const err = (i, j) => Math.max(Math.abs(A[i * 3] - B[j * 3]), Math.abs(A[i * 3 + 1] - B[j * 3 + 1]), Math.abs(A[i * 3 + 2] - B[j * 3 + 2]));
  for (let t = 0; t < IB.length; t += 3) {  // the closest unused original triangle (coincident tiny triangles exist)
    const c = ck(B, IB, t); let best = -1, be = Infinity;
    for (let dx = -1; dx <= 1 && be > 0; dx++) for (let dy = -1; dy <= 1 && be > 0; dy++) for (let dz = -1; dz <= 1 && be > 0; dz++) {
      for (const u of H.get(`${c[0] + dx},${c[1] + dy},${c[2] + dz}`) || []) {
        if (used[u / 3]) continue;
        for (let r = 0; r < 3; r++) {
          const e = Math.max(err(IA[u + r], IB[t]), err(IA[u + (r + 1) % 3], IB[t + 1]), err(IA[u + (r + 2) % 3], IB[t + 2]));
          if (e < be) { be = e; best = u; }
        }
        if (be === 0) break;
      }
    }
    if (best < 0 || be > eps) return false;
    used[best / 3] = 1;
  }
  return true;
}
// bit-exact, order-free: the multiset of vertex positions (unused vertices too) and of triangles (corner positions,
// winding kept, any rotation)
function exactSame(ga, gb) {
  const A = ga.attributes.position.array, B = gb.attributes.position.array;
  const verts = a => { const k = []; for (let i = 0; i < a.length; i += 3) k.push(a[i] + ',' + a[i + 1] + ',' + a[i + 2]); return k.sort().join(';'); };
  if (A.length !== B.length || verts(A) !== verts(B)) return false;
  const tris = (a, ix) => { const k = []; for (let t = 0; t < ix.length; t += 3) { const c = [0, 1, 2].map(j => a[ix[t + j] * 3] + ',' + a[ix[t + j] * 3 + 1] + ',' + a[ix[t + j] * 3 + 2]); k.push([0, 1, 2].map(r => c[r] + '|' + c[(r + 1) % 3] + '|' + c[(r + 2) % 3]).sort()[0]); } return k.sort().join(';'); };
  return tris(A, ga.index.array) === tris(B, gb.index.array);
}
// islands as a multiset of (vertex count, triangle-free bbox) sorted
function islandSig(P, g) {
  const { root, box } = P.cadIslands(g), cnt = new Map();
  for (let i = 0; i < root.length; i++) cnt.set(root[i], (cnt.get(root[i]) || 0) + 1);
  return [...box.entries()].map(([r, q]) => [cnt.get(r), ...q]).sort((x, y) => { for (let i = 0; i < 7; i++) if (x[i] !== y[i]) return x[i] - y[i]; return 0; });
}
function islandCmp(A, B) {  // same count, same vertex counts; bbox difference after matching in sorted order
  if (A.length !== B.length) return { ok: false, d: Infinity };
  const key = s => s.slice().sort((x, y) => x[0] - y[0] || x[1] - y[1] || x[2] - y[2] || x[3] - y[3]);
  const a = key(A), b = key(B); let d = 0, ok = true;
  // sorted by vertex count then min corner: tiny rounding can swap neighbours with equal counts, so match greedily
  const used = new Uint8Array(b.length);
  for (const x of a) {
    let best = -1, bd = Infinity;
    for (let j = 0; j < b.length; j++) if (!used[j] && b[j][0] === x[0]) { const e = dmax(x.slice(1), b[j].slice(1)); if (e < bd) { bd = e; best = j; if (e < 1e-3) break; } }
    if (best < 0) { ok = false; break; } used[best] = 1; d = Math.max(d, bd);
  }
  return { ok, d };
}
// stretch codes: per mesh, per axis, the -1/0/1 histogram; per compressed vertex, the code must be one of the codes
// of the original vertices at that place (coincident vertices of different islands may carry different codes)
function codeCmp(mo, mc, eps) {
  const co = mo.geometry.userData.codes, cc = mc.geometry.userData.codes; if (!co && !cc) return { n: 0, bad: 0, hist: true };
  const ao = mo.geometry.userData.orig, ac = mc.geometry.userData.orig, cell = k => k.map(x => Math.floor(x / eps));
  const H = new Map();
  for (let i = 0; i < ao.length / 3; i++) { const c = cell([ao[i * 3], ao[i * 3 + 1], ao[i * 3 + 2]]).join(','); (H.get(c) || H.set(c, []).get(c)).push(i); }
  let bad = 0, hist = true;
  for (let j = 0; j < 3; j++) {
    if (!co[j]) continue;
    const h = c => [-1, 0, 1].map(v => c.filter(x => x === v).length).join('/');
    if (h(Array.from(co[j])) !== h(Array.from(cc[j]))) hist = false;
  }
  for (let i = 0; i < ac.length / 3; i++) {
    const p = [ac[i * 3], ac[i * 3 + 1], ac[i * 3 + 2]], c = cell(p); let hit = false, near = false;
    for (let dx = -1; dx <= 1 && !hit; dx++) for (let dy = -1; dy <= 1 && !hit; dy++) for (let dz = -1; dz <= 1 && !hit; dz++) {
      for (const k of H.get(`${c[0] + dx},${c[1] + dy},${c[2] + dz}`) || []) {
        if (dmax(p, [ao[k * 3], ao[k * 3 + 1], ao[k * 3 + 2]]) > eps) continue;
        near = true; if ([0, 1, 2].every(j => !co[j] || co[j][k] === cc[j][i])) { hit = true; break; }
      }
    }
    if (!hit) bad++;
  }
  return { n: ac.length / 3, bad, hist };
}
// how close the original comes to flipping a code: vertex to cut plane, rigid island centre to plane, extent to rigid
function cutMargin(P, m, axes, cut, rigid) {
  const a = m.geometry.attributes.position.array, { root, box } = P.cadIslands(m.geometry); let mv = Infinity, mc = Infinity, mr = Infinity;
  const near = (v, j) => Math.min(...cut[j].map(c => Math.abs(v - c)));
  for (const q of box.values()) for (let j = 0; j < 3; j++) if (axes[j]) { const e = q[j + 3] - q[j]; mr = Math.min(mr, Math.abs(e - rigid)); if (e < rigid) mc = Math.min(mc, near((q[j] + q[j + 3]) / 2, j)); }
  for (let i = 0; i < a.length / 3; i++) { const q = box.get(root[i]); for (let j = 0; j < 3; j++) if (axes[j] && q[j + 3] - q[j] >= rigid) mv = Math.min(mv, near(a[i * 3 + j], j)); }
  return Math.min(mv, mc, mr);
}

(async () => {
  await MeshoptDecoder.ready;
  const P = PAGE(THREE), models = Object.values(P.CAD_FILES), heads = Object.values(P.TH_FILES), names = [...models, ...heads];
  const keyOf = f => Object.keys(P.CAD_FILES).find(k => P.CAD_FILES[k] === f);
  let fail = 0; const rows = [];
  for (const f of names) {
    const fo = path.join(ORG, f + '.glb'), fc = path.join(CMP, f + '.glb');
    const t0 = Date.now(), go = await load(fo), tLo = Date.now() - t0, t1 = Date.now(), gc = await load(fc), tLc = Date.now() - t1;
    const so = go.scene, sc = gc.scene, bakedNoop = (() => { const before = meshesOf(sc).map(m => m.geometry.attributes.position); glbFloat(sc); return meshesOf(sc).every((m, i) => m.geometry.attributes.position === before[i]); })();
    glbFloat(so);
    const mo = meshesOf(so), mc = meshesOf(sc), err = [];
    const no = mo.map(m => m.name).join(), nc = mc.map(m => m.name).join();
    if (no !== nc) err.push('mesh names/order differ');
    let exact = true, dBox = 0, dWorld = 0, tris = 0, isl = 0, islD = 0, vtx = [0, 0];
    for (let i = 0; i < mo.length && !err.length; i++) {
      const A = mo[i], B = mc[i];
      if (hex(A) !== hex(B) || A.material.roughness !== B.material.roughness || A.material.metalness !== B.material.metalness) err.push(A.name + ': material differs');
      if (!/^[a-z_]+__[0-9a-f]{6}$/.test(B.name)) err.push(B.name + ': not a <group>__<rrggbb> name');
      if (!(B.geometry.attributes.position.array instanceof Float32Array)) err.push(B.name + ': positions not Float32');
      const ta = A.geometry.index.count / 3, tb = B.geometry.index.count / 3; tris += tb; if (ta !== tb) err.push(`${A.name}: triangles ${ta} -> ${tb}`);
      vtx[0] += A.geometry.attributes.position.count; vtx[1] += B.geometry.attributes.position.count;
      if (A.geometry.attributes.position.count !== B.geometry.attributes.position.count) err.push(A.name + ': vertex count differs');
      if (!!A.geometry.attributes.normal !== !!B.geometry.attributes.normal) err.push(A.name + ': normal attribute added/removed');
      dBox = Math.max(dBox, dmax(bb(A.geometry.attributes.position.array), bb(B.geometry.attributes.position.array)));
      dWorld = Math.max(dWorld, dmax(b3(new THREE.Box3().setFromObject(A)), b3(new THREE.Box3().setFromObject(B))));
      const ia = islandSig(P, A.geometry), ib = islandSig(P, B.geometry), ic = islandCmp(ia, ib); isl += ib.length;
      if (!ic.ok) err.push(`${A.name}: islands ${ia.length} -> ${ib.length} (or vertex counts per island differ)`); islD = Math.max(islD, ic.d);
      const ex = exactSame(A.geometry, B.geometry); exact = exact && ex;
      if (!ex && !sameTris(A.geometry, B.geometry, 0.01)) err.push(A.name + ': triangle set differs (0.01 mm)');
    }
    // the page's own preparation on both
    let tip, dTip = 0, extra = '', codes = { n: 0, bad: 0, hist: true }, margin = Infinity;
    const json = fs.existsSync(path.join(WEB, 'models', f + '.json')) ? JSON.parse(fs.readFileSync(path.join(WEB, 'models', f + '.json'), 'utf8')) : null;
    if (models.includes(f) && !err.length) {
      const key = keyOf(f), C1 = P.CAD_ADAPT[key](json), C2 = P.CAD_ADAPT[key](json);
      const p1 = P.cadPrep(so, C1), p2 = P.cadPrep(sc, C2);
      tip = p2.tip; dTip = dmax(p1.tip, p2.tip);
      const dd = [dmax(b3(p1.thBox), b3(p2.thBox))];
      if (p1.cube) dd.push(dmax(b3(p1.cube), b3(p2.cube)), dmax(b3(p1.beam), b3(p2.beam)), dmax(C1.nozzle, C2.nozzle));
      if (C1.beam && C2.beam) dd.push(dmax([...C1.beam.min, ...C1.beam.max], [...C2.beam.min, ...C2.beam.max]));
      extra = 'prep boxes ' + Math.max(...dd).toFixed(4);
      if (Math.max(...dd) >= 0.05) err.push('cadPrep boxes differ ' + Math.max(...dd));
      for (let i = 0; i < p1.meshes.length; i++) {
        const A = p1.meshes[i], B = p2.meshes[i], c = codeCmp(A, B, 0.05); codes.n += c.n; codes.bad += c.bad; codes.hist = codes.hist && c.hist;
        const ax = C1.axes[A.userData.group]; if (ax) margin = Math.min(margin, cutMargin(P, A, ax, (C1.gcut && C1.gcut[A.userData.group]) || C1.cut, C1.rigid));
      }
      if (codes.bad) err.push(`stretch codes differ on ${codes.bad} vertices`);
    } else if (!err.length) {
      const r1 = P.cadPrepTh(so, json), r2 = P.cadPrepTh(sc, json);
      tip = r2.tip; dTip = dmax(r1.tip, r2.tip);
      const d = dmax(b3(r1.box), b3(r2.box)); extra = 'root box ' + d.toFixed(4); if (d >= 0.05) err.push('toolhead box differs ' + d);
    }
    if (STRICT && !exact) err.push('not bit-exact');
    if (dBox >= 0.05 || dWorld >= 0.05) err.push(`bbox differs ${dBox} / ${dWorld}`);
    if (dTip >= 0.05) err.push('tip differs ' + dTip);
    if (err.length) fail++;
    const so_ = fs.statSync(fo).size, sc_ = fs.statSync(fc).size;
    rows.push({ f, meshes: mc.length, tris, vtx: vtx.join('/'), isl, size: `${(so_ / 1e6).toFixed(2)}->${(sc_ / 1e6).toFixed(2)} MB`, dBox: dBox.toFixed(4), dWorld: dWorld.toFixed(4),
      islD: islD.toFixed(4), tip: tip ? tip.map(x => x.toFixed(3)).join(',') : '', dTip: dTip ? dTip.toExponential(1) : '0', exact, codes: codes.n ? `${codes.bad}/${codes.n}${codes.hist ? '' : ' hist!'}` : '-',
      margin: isFinite(margin) ? margin.toFixed(4) : '-', extra, glbFloatNoop: bakedNoop, load: `${tLo}/${tLc} ms`, result: err.length ? 'FAIL: ' + err.slice(0, 4).join('; ') : 'ok' });
  }
  console.table(rows);
  console.log(fail ? `${fail} file(s) FAILED` : 'all files match');
  process.exitCode = fail ? 1 : 0;
})().catch(e => { console.error(e); process.exitCode = 2; });
