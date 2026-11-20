// Compress the page's GLBs with EXT_meshopt_compression, keeping every node, mesh, material, index and vertex.
//
//   node tools/models/compress.js [--bits N] [--no-reorder] [--only name,name] [--out dir]
//
// Reads models/raw/uncompressed/<name>.glb (the untouched originals), writes web/models/<name>.glb.
// Why not gltfpack: gltfpack welds equal vertices and drops zero-area triangles, and the page's stretch depends on
// index connectivity (cadIslands): the zero-area chain triangles of the skirt runs (lod.chain) and parts that are not
// welded to each other must stay as they are. This script only re-encodes the buffer views:
//   - positions stay FLOAT32 (no KHR_mesh_quantization, no node scale/offset: the raw arrays stay plain mm);
//     and by default bit-exact (lossless). --bits N (1..24) would round them with the meshopt exponential filter to an
//     N-bit mantissa (error <= max|xyz| * 2^-(N-1)): DO NOT use it for the page's files. The models have vertices and
//     islands exactly on the stretch thresholds (Ender-3 frame island 99.999998 mm long vs rigid 100, VzBot frame
//     islands centred on the Y cut 260, Voron 2.4 vertices on its cuts): any rounding, even 23 bits, flips the
//     stretch side of hundreds of vertices (tools/models/verify.js shows it). 16-bit int quantization (gltfpack -vp 16)
//     is worse, and gltfpack also welds vertices (rx islands 37 -> 29) and drops zero-area triangles.
//   - indices: meshopt triangle codec (lossless, same triangles, degenerate ones kept)
//   - reorder (default on, --no-reorder to keep the order): triangles go in vertex-cache order and vertices are
//     renumbered in fetch order (meshopt reorderMesh; a permutation, nothing merged or dropped: unused vertices are
//     kept at the end). Islands, triangles (same corners, same winding) and every vertex position are unchanged; only
//     the order of triangles and vertices in the arrays differs.
// Needs the meshoptimizer npm package 0.18.1 (the version of web/third_party/meshopt_decoder.js), outside the
// project, e.g.  npm i --prefix %TEMP%\mo meshoptimizer@0.18.1   then   set NODE_PATH=%TEMP%\mo\node_modules
'use strict';
const fs = require('fs'), path = require('path');
const { MeshoptEncoder } = require('meshoptimizer');

const ROOT = path.resolve(__dirname, '..', '..');
const SRC = path.join(ROOT, 'models', 'raw', 'uncompressed');
const args = process.argv.slice(2), opt = (k, d) => { const i = args.indexOf(k); return i < 0 ? d : args[i + 1]; };
const BITS = +opt('--bits', 0), REORDER = !args.includes('--no-reorder');
const OUT = path.resolve(opt('--out', path.join(ROOT, 'web', 'models')));
const ONLY = opt('--only', '') ? opt('--only').split(',') : null;
const EXT = 'EXT_meshopt_compression';

function readGlb(buf) {
  if (buf.readUInt32LE(0) !== 0x46546c67) throw new Error('not a GLB');
  const jl = buf.readUInt32LE(12), json = JSON.parse(buf.slice(20, 20 + jl).toString('utf8'));
  const bo = 20 + jl, bin = buf.readUInt32LE(bo + 4) === 0x004e4942 ? buf.slice(bo + 8, bo + 8 + buf.readUInt32LE(bo)) : null;
  return { json, bin };
}
function writeGlb(json, bin) {
  const pad = (b, c) => Buffer.concat([b, Buffer.alloc((4 - b.length % 4) % 4, c)]);
  const J = pad(Buffer.from(JSON.stringify(json), 'utf8'), 0x20), B = pad(bin, 0);
  const h = Buffer.alloc(12); h.writeUInt32LE(0x46546c67, 0); h.writeUInt32LE(2, 4); h.writeUInt32LE(12 + 8 + J.length + 8 + B.length, 8);
  const ch = (len, type) => { const c = Buffer.alloc(8); c.writeUInt32LE(len, 0); c.writeUInt32LE(type, 4); return c; };
  return Buffer.concat([h, ch(J.length, 0x4e4f534a), J, ch(B.length, 0x004e4942), B]);
}
const view = (json, bin, ai) => {  // accessor -> typed copy of its data (tightly packed)
  const a = json.accessors[ai], v = json.bufferViews[a.bufferView], n = { SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4 }[a.type];
  const T = { 5126: Float32Array, 5125: Uint32Array, 5123: Uint16Array, 5121: Uint8Array }[a.componentType];
  if (!T || (v.byteStride && v.byteStride !== n * T.BYTES_PER_ELEMENT)) throw new Error('unsupported accessor ' + ai);
  const off = bin.byteOffset + (v.byteOffset || 0) + (a.byteOffset || 0);
  return new T(bin.buffer.slice(off, off + a.count * n * T.BYTES_PER_ELEMENT));
};

function compress(name) {
  const src = fs.readFileSync(path.join(SRC, name + '.glb')), { json, bin } = readGlb(src);
  if ((json.extensionsUsed || []).includes(EXT)) throw new Error(name + ': already compressed, use the original');
  if (json.buffers.length !== 1 || json.buffers[0].uri !== undefined) throw new Error(name + ': expected one GLB buffer');
  if (json.images || json.textures || json.skins || json.animations) throw new Error(name + ': only plain meshes are handled');
  // every accessor: one bufferView of its own, rebuilt per primitive
  const views = [], parts = [];
  let packed = 0;
  const addView = (data, stride, count, mode, filter, target) => {
    const enc = filter ? MeshoptEncoder.encodeGltfBuffer(filter.data, count, stride, mode)
      : MeshoptEncoder.encodeGltfBuffer(new Uint8Array(data.buffer, data.byteOffset, data.byteLength), count, stride, mode);
    const off = parts.reduce((s, p) => s + p.length, 0), b = Buffer.from(enc), padded = Buffer.concat([b, Buffer.alloc((4 - b.length % 4) % 4)]);
    parts.push(padded);
    const ext = { buffer: 0, byteOffset: off, byteLength: b.length, byteStride: stride, count, mode };
    if (filter) ext.filter = filter.name;
    const bv = { buffer: 1, byteOffset: packed, byteLength: count * stride, extensions: { [EXT]: ext } };
    if (mode === 'ATTRIBUTES') bv.byteStride = stride;
    if (target) bv.target = target;
    packed += count * stride;
    views.push(bv);
    return views.length - 1;
  };
  const newAcc = json.accessors.map(a => Object.assign({}, a));
  const done = new Set();
  for (const mesh of json.meshes) for (const pr of mesh.primitives) {
    if ((pr.mode ?? 4) !== 4 || pr.indices === undefined) throw new Error(name + ': expected indexed triangles');
    const keys = Object.keys(pr.attributes);
    if (keys.some(k => done.has(pr.attributes[k])) || done.has(pr.indices)) throw new Error(name + ': shared accessors not handled');
    let idx = view(json, bin, pr.indices);
    const atts = keys.map(k => [k, view(json, bin, pr.attributes[k])]);
    const nv = json.accessors[pr.attributes.POSITION].count;
    if (REORDER) {  // triangles in vertex-cache order, vertices in fetch order; never merges, unused vertices go last
      const i32 = Uint32Array.from(idx), [r, unique] = MeshoptEncoder.reorderMesh(i32, true, false);
      const remap = new Uint32Array(nv).fill(0xffffffff); let next = unique;
      for (let i = 0; i < r.length; i++) remap[i] = r[i];
      for (let i = 0; i < nv; i++) if (remap[i] === 0xffffffff) remap[i] = next++;
      if (next !== nv) throw new Error(name + ': remap is not a permutation');
      idx = idx.constructor.from(i32);
      for (const at of atts) {
        const a = at[1], n = a.length / nv, o = new a.constructor(a.length);
        for (let i = 0; i < nv; i++) for (let j = 0; j < n; j++) o[remap[i] * n + j] = a[i * n + j];
        at[1] = o;
      }
    }
    for (const [k, a] of atts) {
      const ai = pr.attributes[k], acc = json.accessors[ai], n = a.length / acc.count;
      if (!(a instanceof Float32Array)) throw new Error(name + ': only float attributes handled');
      let filter = null;
      if (k === 'POSITION' && BITS > 0) {  // exp filter: 4-byte stride per component, vec3 -> stride 12
        filter = { name: 'EXPONENTIAL', data: MeshoptEncoder.encodeFilterExp(a, acc.count, n * 4, BITS) };
      }
      newAcc[ai].bufferView = addView(a, n * 4, acc.count, 'ATTRIBUTES', filter, 34962);
      newAcc[ai].byteOffset = 0;
      if (k === 'POSITION') {  // min/max of what the decoder will return
        const d = filter ? decodeExp(filter.data, acc.count, n) : a, mn = [1e30, 1e30, 1e30], mx = [-1e30, -1e30, -1e30];
        for (let i = 0; i < d.length; i++) { const j = i % 3; if (d[i] < mn[j]) mn[j] = d[i]; if (d[i] > mx[j]) mx[j] = d[i]; }
        newAcc[ai].min = mn.map(Math.fround); newAcc[ai].max = mx.map(Math.fround);
      }
      done.add(ai);
    }
    const isz = idx.BYTES_PER_ELEMENT;
    if (isz === 1) throw new Error(name + ': 8-bit indices not handled');
    newAcc[pr.indices].bufferView = addView(idx, isz, idx.length, 'TRIANGLES', null, 34963);
    newAcc[pr.indices].byteOffset = 0;
    done.add(pr.indices);
  }
  if (done.size !== json.accessors.length) throw new Error(name + ': accessors outside the meshes');
  const out = Object.assign({}, json, { accessors: newAcc, bufferViews: views });
  const binOut = Buffer.concat(parts);
  out.buffers = [{ byteLength: binOut.length }, { byteLength: packed, extensions: { [EXT]: { fallback: true } } }];
  out.extensionsUsed = [...new Set([...(json.extensionsUsed || []), EXT])];
  out.extensionsRequired = [...new Set([...(json.extensionsRequired || []), EXT])];
  const glb = writeGlb(out, binOut);
  fs.writeFileSync(path.join(OUT, name + '.glb'), glb);
  return [src.length, glb.length];
}
// the decoder side of the exp filter (meshopt_decodeFilterExp), for accessor min/max
function decodeExp(u8, count, n) {
  const i32 = new Int32Array(u8.buffer, u8.byteOffset, count * n), o = new Float32Array(count * n);
  for (let i = 0; i < i32.length; i++) { const v = i32[i], e = v >> 24, m = (v << 8) >> 8; o[i] = Math.fround(Math.pow(2, e) * m); }
  return o;
}

MeshoptEncoder.ready.then(() => {
  // a plain (not yet compressed) GLB in web/models is a fresh build: it becomes the new original first, so an old
  // backup never overwrites a new build
  const WEBM = path.join(ROOT, 'web', 'models');
  fs.mkdirSync(SRC, { recursive: true });
  for (const f of fs.readdirSync(WEBM).filter(f => f.endsWith('.glb'))) {
    const n = f.slice(0, -4); if (ONLY && !ONLY.includes(n)) continue;
    const b = fs.readFileSync(path.join(WEBM, f));
    if (!(readGlb(b).json.extensionsUsed || []).includes(EXT)) { fs.copyFileSync(path.join(WEBM, f), path.join(SRC, f)); console.log(`${n}: new build in web/models -> models/raw/uncompressed`); }
  }
  const names = fs.readdirSync(SRC).filter(f => f.endsWith('.glb')).map(f => f.slice(0, -4)).filter(n => !ONLY || ONLY.includes(n));
  const t0 = Date.now(); let a = 0, b = 0;
  for (const n of names) {
    const t = Date.now(), [x, y] = compress(n); a += x; b += y;
    console.log(`${n.padEnd(14)} ${(x / 1e6).toFixed(2).padStart(6)} MB -> ${(y / 1e6).toFixed(2).padStart(6)} MB  (${(100 * y / x).toFixed(1)} %, ${Date.now() - t} ms)`);
  }
  console.log(`${'total'.padEnd(14)} ${(a / 1e6).toFixed(2).padStart(6)} MB -> ${(b / 1e6).toFixed(2).padStart(6)} MB  (${(100 * b / a).toFixed(1)} %)  bits ${BITS} reorder ${REORDER}  ${((Date.now() - t0) / 1000).toFixed(1)} s`);
});
