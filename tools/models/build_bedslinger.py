"""Creality Ender-3 (bed slinger) -> web/models/bedslinger.glb/.json

Source: github.com/Creality3DPrinting/Ender-3 'Ender-3 Mechanical/STP/01 MAIN ASM Ender_3.STEP' (GPL-3.0).
The raw file is Y up, front +Z, metres; nodes are 'NAUO<n>' so parts are picked by part name.
Output like voron24.glb: CAD mm, Z up, front -Y, bed top at z = 0, bed plate x/y 0..235.

Groups: frame (static), bed (moves in Y), gantry_z (X beam, moves in Z), carriage (X carriage plate
and its wheels: moves in X with the toolhead but stays when the toolhead is swapped), toolhead (stock
hotend, fans and shroud: swappable).

    python -I tools/models/build_bedslinger.py
"""
import os
import re
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cad_common import (RAW, iter_parts, build, r1, BLACK, DARK_GREY, RAIL_STEEL, PEI_GOLD,
                        FAN_GREY, RED, BRASS)

SRC = RAW + r"\bedslinger_raw.glb"
# raw (Y up, front +Z) -> CAD (Z up, front -Y)
ROT = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], float)
DROP = re.compile(r"(Screw|T-Slot Nut|Self-locking nut|M\d+ nut|washer|Washer|pillow|belt$|M3X40-P)", re.I)
HOTEND = {'Heater', 'Nozzle thermistor', 'Pneumatic joint', 'Heat block', 'nozzle', 'Radiator',
          'Nozzle fan', 'Nozzle fan duct', 'Fan cover', 'Cold section fan'}
BED = {'Hotbed_Surface', 'Hot bed', 'Bed level springs', 'Hand Twisted Nut', 'Y plate', 'Rollers',
       'Spacers for rollers', 'Eccentric column', 'Copper cover'}
FIXED = {'Hot bed', 'Hotbed_Surface', 'Y plate', 'E plate', 'Sheetmetal enclosure top',
         'Sheetmetal enclosure bottom', 'Support frame', 'LCD support', 'Power holder'}
CARRIAGE = {'E plate', 'Rollers', 'Spacers for rollers', 'Eccentric column', 'Copper cover'}


def colour(part, c):
    if 'profile' in part:
        return DARK_GREY
    if part == 'Hotbed_Surface':
        return PEI_GOLD
    if part in ('Hand Twisted Nut', 'X Adjustment Block', 'Y Adjustment Block'):
        return RED
    if part in ('nozzle', 'z nut'):
        return BRASS
    if part in ('Heat block', 'Radiator', 'Heater', 'Z threaded rod', '2GT-pulley'):
        return RAIL_STEEL
    if part in ('Nozzle fan', 'Cold section fan', 'Case fan'):
        return FAN_GREY
    if 'motor' in part and 'bracket' not in part and 'holder' not in part:
        return DARK_GREY
    if part in ('Hot bed', 'E plate', 'Spacers for rollers', 'Eccentric column', 'Z coupler',
                'Support tube', 'Control panel', '24V15A power supply'):
        return DARK_GREY
    return BLACK


def main():
    parts = []
    for names, part, c, m in iter_parts(SRC, ROT):
        sub = names[-3] if len(names) >= 3 else ''   # NAUO1..8 under the root assembly
        if DROP.search(part) or sub == 'NAUO7':     # NAUO7 = spool holder
            continue
        if np.linalg.norm(m.extents) < 4.0:
            continue
        cx = m.bounds.mean(0)[0]
        if sub == 'NAUO2':
            grp = ('toolhead' if part in HOTEND else
                   'carriage' if part in CARRIAGE and -50 < cx < 60 else 'gantry_z')
        elif sub == 'NAUO3':
            grp = 'bed' if part in BED else 'frame'
        else:
            grp = 'frame'
        if part == 'Hotbed_Surface':
            m.apply_translation((0, 0, 0.25))       # coplanar with the plate top: lift it
        fixed = 'profile' in part or part in FIXED or part == 'Z threaded rod'
        parts.append([grp, colour(part, c), m, fixed])
    # origin: bed plate front-left corner, bed top z = 0
    bed = np.vstack([m.bounds for g, _, m, *_ in parts if g == 'bed' and m.extents[0] > 200])
    off = -np.array([bed[:, 0].min(), bed[:, 1].min(), bed[:, 2].max()])
    for p in parts:
        p[2].apply_translation(off)
    B = lambda g: np.vstack([m.bounds for gg, _, m, *_ in parts if gg == g])
    bb = {g: (B(g).min(0), B(g).max(0)) for g in ('frame', 'bed', 'gantry_z', 'carriage', 'toolhead')}
    # nozzle tip: lowest toolhead point
    tv = np.vstack([m.vertices for g, _, m, *_ in parts if g == 'toolhead'])
    lo = tv[:, 2].min()
    tip = tv[tv[:, 2] < lo + 0.3].mean(0)
    plate = [m for g, _, m, *_ in parts if g == 'bed' and m.extents[0] > 200]
    pb = np.vstack([m.bounds for m in plate])
    # frame box: the extrusion cube (profiles only)
    prof = np.vstack([m.bounds for g, c, m, *_ in parts if g == 'frame' and c == DARK_GREY and max(m.extents) > 280])
    # toolhead mount: E plate (X carriage plate) front face centre
    ep = np.vstack([m.bounds for g, c, m, *_ in parts if g == 'carriage' and m.extents[0] > 50])
    bed_c = (pb.min(0) + pb.max(0)) / 2
    meta = {
        'source': 'Creality3DPrinting/Ender-3 Ender-3 Mechanical/STP/01 MAIN ASM Ender_3.STEP (GPL-3.0)',
        'units': 'mm, Z up, front -Y (like voron24); bed top z = 0, bed plate from x 0 / y 0',
        'build_volume': [220, 220, 250],
        'kinematics': {'bed': 'y', 'gantry_z': 'z', 'carriage': 'x', 'toolhead': 'x'},
        'frame_box': [r1(prof.min(0)), r1(prof.max(0))],
        'bed_top': {'z': 0.0, 'rect': [r1(pb.min(0)[:2]), r1(pb.max(0)[:2])], 'centre': r1(bed_c[:2]),
                    'y_travel': 220, 'note': 'bed as drawn; it moves along Y by +-110 round this centre'},
        'gantry': {'bbox': [r1(bb['gantry_z'][0]), r1(bb['gantry_z'][1])], 'z_travel': 250,
                   'note': 'gantry_z + carriage + toolhead rise with the nozzle; as drawn the nozzle is at z %.1f' % tip[2]},
        'carriage_bbox': [r1(bb['carriage'][0]), r1(bb['carriage'][1])],
        'x_travel': 220,
        'toolhead': {'bbox': [r1(bb['toolhead'][0]), r1(bb['toolhead'][1])],
                     'nozzle_tip': r1(tip),
                     'mount': r1([(ep[:, 0].min() + ep[:, 0].max()) / 2, ep[:, 1].min(), (ep[:, 2].min() + ep[:, 2].max()) / 2]),
                     'mount_note': 'front face centre of the X carriage plate (E plate); a swapped toolhead hangs here'},
    }
    build('bedslinger', parts, {'frame': 95000, 'bed': 12000, 'gantry_z': 26000, 'carriage': 5000,
                                'toolhead': 12000}, extra_meta=meta)


if __name__ == '__main__':
    main()
