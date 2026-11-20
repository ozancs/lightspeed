// glbFloat(root): run right after GLTFLoader, before the page reads geometry.attributes.position.array.
// For every mesh: bakes its transform relative to root into a plain Float32 position (and normal) attribute and resets
// the transforms on the mesh and its parents, so world positions stay the same and the raw arrays are float mm.
// Handles quantized (KHR_mesh_quantization: int/normalized attributes + node scale/offset), interleaved and float input.
// The current files (tools/models/compress.js) keep float positions on untransformed root nodes, so for them it only
// recomputes the bounds; it is the safety net if a file ever comes quantized (e.g. from gltfpack).
// A mesh that lands on an unnamed child node (gltfpack -kn) takes its named parent's name (<group>__<rrggbb>).
function glbFloat(root) {
  const T = THREE, v = new T.Vector3(), nm = new T.Matrix3(), inv = new T.Matrix4(), M = new T.Matrix4();
  root.updateMatrixWorld(true); inv.copy(root.matrixWorld).invert();
  const meshes = [], seen = new Set();
  root.traverse(o => { if (o.isMesh) meshes.push(o); });
  for (const m of meshes) {
    M.multiplyMatrices(inv, m.matrixWorld);  // mesh -> root
    if (!/__[0-9a-f]{6}$/.test(m.name) && m.parent && m.parent !== root && /__[0-9a-f]{6}$/.test(m.parent.name)) m.name = m.parent.name;
    let g = m.geometry;
    if (seen.has(g)) g = m.geometry = g.clone();  // shared geometry: each mesh bakes its own
    seen.add(g);
    const p = g.attributes.position, plain = a => a && !a.isInterleavedBufferAttribute && !a.normalized && a.array instanceof Float32Array;
    if (!M.equals(new T.Matrix4()) || !plain(p) || (g.attributes.normal && !plain(g.attributes.normal))) {
      const out = new Float32Array(p.count * 3);
      for (let i = 0; i < p.count; i++) { v.fromBufferAttribute(p, i).applyMatrix4(M); out[i * 3] = v.x; out[i * 3 + 1] = v.y; out[i * 3 + 2] = v.z; }
      g.setAttribute('position', new T.BufferAttribute(out, 3));
      const n = g.attributes.normal;
      if (n) {
        nm.getNormalMatrix(M); const on = new Float32Array(n.count * 3);
        for (let i = 0; i < n.count; i++) { v.fromBufferAttribute(n, i).applyMatrix3(nm).normalize(); on[i * 3] = v.x; on[i * 3 + 1] = v.y; on[i * 3 + 2] = v.z; }
        g.setAttribute('normal', new T.BufferAttribute(on, 3));
      }
      if (M.determinant() < 0 && g.index) {  // a mirroring transform flips the winding: swap two corners back
        const ix = g.index.array; for (let i = 0; i < ix.length; i += 3) { const t = ix[i + 1]; ix[i + 1] = ix[i + 2]; ix[i + 2] = t; }
      }
    }
    for (let o = m; o && o !== root; o = o.parent) { o.position.set(0, 0, 0); o.quaternion.identity(); o.scale.set(1, 1, 1); }
    g.computeBoundingBox(); g.computeBoundingSphere();
  }
  root.updateMatrixWorld(true);
  return root;
}
if (typeof module === 'object' && module.exports) module.exports = glbFloat;
