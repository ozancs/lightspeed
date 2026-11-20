# Printer models from CAD

1. Put the STEP file in `models/source/` (not in git; large).
2. `python -I tools/models/convert.py` - STEP -> raw GLB in `models/raw/` (cascadio).
3. `python -I tools/models/build_model.py voron24` (or `rx`) - drops fasteners/electronics/belts/rails,
   groups parts (frame, glass, panels, bed, gantry_z, gantry_y, toolhead), applies the palette
   (black body, dark grey extrusions, red accents), decimates per part (`lod.py`), writes `web/models/<name>.glb` + `.json`.
4. `tree.py` / `parts.py` list the CAD tree and parts to tune the group and drop patterns.

Needs: `pip install cascadio trimesh fast-simplification networkx scipy`.
Sources: Voron 2.4 r2 STEP from github.com/VoronDesign/Voron-2 (GPLv3); RX STEP from the owner.

Extra models (shared helpers in `cad_common.py`, one script each, run with `python -I`):
- `build_bedslinger.py` - Creality Ender-3 (github.com/Creality3DPrinting/Ender-3, GPLv3) -> `bedslinger.glb`
  (groups frame, bed, gantry_z, carriage, toolhead; CAD mm, Z up, bed top z 0).
- `build_stealthburner.py` - Voron Stealthburner + CW2.5 + Revo Voron + X carriage (VoronDesign/Voron-Stealthburner, GPLv3).
- `build_downdraft.py` - Voron DownDraft (VoronDesign/voron-downdraft, GPLv3), one configuration picked from the
  parts catalogue; hotend and X carriage from the Stealthburner STEP.
Toolhead files are in the rx.glb frame (mm, Y up, front +Z), nozzle tip at the origin; the .json holds the
nozzle tip, logo/nozzle LED anchors and the X carriage rail-block mount.
Raw files: `cascadio.step_to_glb(src, models/raw/<name>_raw.glb, tol_linear=0.2, tol_angular=0.5)`.

Decimation (`lod.py`, used by every script): each CAD part is decimated on its own, never one ratio per
colour bucket. Per part a ladder of candidates (border-safe lossless, QEM 65..4 %, vertex clustering
0.15..2.4 mm) is measured (two-sided surface deviation, exact point-triangle distance, mm); one global
tolerance is searched so the model fits its triangle budget, capped by a share of each part's middle
extent, so small printed parts stay exact and motors, threads, plates and panels pay for the budget.
Parts under 1500 triangles keep at least 65 %. Extrusions the page measures and the nozzle stay exact.
Parts are not welded to each other, so every part is its own island for the page's stretch.
Ladders are cached in `models/raw/<key>_lod.pkl`. Quality check without rendering:
`LOD_DUMP=1 python -I tools/models/build_model.py voron24`, then
`python -I tools/models/quality.py voron24 old.glb web/models/voron24.glb`.
Dragon Burner fans (not in its STEP): 3010 axial from the Voron 0.2 STEP, 4010 blowers from the DownDraft
STEP, placed in the measured cowl pockets (see `dragonburner.json` `added_parts`).
