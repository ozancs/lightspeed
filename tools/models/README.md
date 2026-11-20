# Printer models from CAD

1. Put the STEP file in `models/source/` (not in git; large).
2. `python -I tools/models/convert.py` - STEP -> raw GLB in `models/raw/` (cascadio).
3. `python -I tools/models/build_model.py voron24` (or `rx`) - drops fasteners/electronics/belts/rails,
   groups parts (frame, glass, panels, bed, gantry_z, gantry_y, toolhead), applies the palette
   (black body, dark grey extrusions, red accents), decimates, writes `web/models/<name>.glb` + `.json`.
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
