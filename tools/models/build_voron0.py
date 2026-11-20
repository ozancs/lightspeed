"""Voron 0.2 r1 (120 mm) raw CAD GLB -> web/models/voron0.glb + .json (pipeline in build_trident.py).

    python -I tools/models/build_voron0.py [--list GROUP]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # python -I drops the script dir
from build_model import RAW
from build_trident import DROP, build

CFG = {
    'title': 'Voron 0.2 r1',
    'source': 'github.com/VoronDesign/Voron-0 (branch Voron0.2r1) CAD/V0.2R1_Master_Assembly_STEP.zip (GPLv3)',
    'src': RAW + r"\voron0_raw.glb",
    'scale': 1000.0,
    'drop': DROP,
    'groups': [          # first match wins; unmatched (Tools, Banana, Filament Spool, Electronics) drop
        ('toolhead', r"^Mini Stealthburner:"),
        ('toolhead', r"PATH:MGN7H Carriage:1 < MGN7 Rail \(4\):1 < MGN7 Rail 150mm:1 < Linear Rail:1 < X Axis"),
        ('gantry_y', r"^X Axis:"),
        ('gantry_y', r"PATH:MGN7H Carriage.* < Y Axis:1 < Linear Rails"),   # Y rail blocks ride with the beam
        ('bed', r"^Bed Assembly:"),
        ('bed', r"PATH:MGN7H Carriage.* < Z Axis \(1\):1"),   # the Z rail blocks ride with the bed
        ('frame', r"^Tophat Frame:"),
        ('sheet', r"^(Panels & Clips|Extrusion Tophat):"),
        ('frame', r"^(Frame|Skirts|Z Axis|Gantry):"),
    ],
    'glass': r"^(Door Assembly|Side Panels|Panels):",
    'pei': r"^Flex Plate:",
    'plate': r"^120x120mm MIC6:",
    'cube': r"^1515_200mm - (C|D|E):",
    'profile': 15,
    'beam': r"^Extrusions \(2\):",
    'carriage': r"PATH:MGN7H Carriage:1 < MGN7 Rail \(4\):1 < MGN7 Rail 150mm:1 < Linear Rail:1 < X Axis",
    'mount_normal': [0, 0, 1],      # the Mini Stealthburner sits on top of the MGN7 block
    'nozzle': r"^Revo Voron:",
    'build': [120, 120, 120],
    'budget': 250000,    # triangles, whole model (lod.py)
    'min_size': 8.0,
    # cube = corner posts + bottom/top-side (Y rail) extrusions; the side rails on the drives/idlers sit higher
    'extra_boxes': {'tophat': r"^Extrusion Tophat:", 'top_side_rails': r"^1515_200mm - B:(2|3)$"},
}

if __name__ == '__main__':
    build('voron0', CFG, sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == '--list' else None)
