"""STEP -> raw GLB with cascadio (coarse tessellation: shape matters, not detail)."""
import sys
import pathlib
import time
import cascadio

SRC = str(pathlib.Path(__file__).resolve().parents[2] / "models" / "source")
OUT = str(pathlib.Path(__file__).resolve().parents[2] / "models" / "raw")
jobs = [
    (SRC + r"\voron24_step\Voron_2.4r2_Assembly.step", OUT + r"\voron24_raw.glb"),
    (SRC + r"\oznlab_gearbox.step", OUT + r"\rx_raw.glb"),
]
for src, dst in jobs:
    t = time.time()
    print("converting", src, flush=True)
    r = cascadio.step_to_glb(src, dst, tol_linear=0.2, tol_angular=0.5)
    print("done", dst, r, "%.0fs" % (time.time() - t), flush=True)
