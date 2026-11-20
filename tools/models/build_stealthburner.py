"""Voron Stealthburner + Clockwork 2.5 + Revo Voron + X carriage -> web/models/stealthburner.glb/.json

Source: github.com/VoronDesign/Voron-Stealthburner CAD/Stealthburner_CW2.5_Assembly.step (GPLv3).
File coords like rx.glb: mm, Y up, front +Z; nozzle tip at the origin.

    python -I tools/models/build_stealthburner.py
"""
import os
import re
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cad_common import (RAW, TOOLHEAD_ROT, iter_parts, build, r1, BLACK, DARK_GREY, RAIL_STEEL,
                        FAN_GREY, RED, DIFFUSER, BRASS)

SRC = RAW + r"\stealthburner_raw.glb"
DROP = re.compile(r"(SHCS|BHCS|FHCS|BHSC|Screw|screw|Nut|Insert|Washer|ISO 7089|Magnet|"
                  r"NH-SB|Toolboard|ADXL|adxl|asxl|HFSB5|^SOLID$|rgbw_led_pcb|AH3364|Collet|"
                  r"hall_effect_retained|Grab Ring|Seal Retainer|Plunger|Bowden_Clip|PTFE|Spring|Dowel|Pin|Bearing|Shaft)")


def colour(part, names, c):
    r, g, b = c
    if 'Fan' in part or part.startswith(('bottom', 'turbine')):
        return FAN_GREY
    if part in ('LED_Diffuser',):
        return DIFFUSER
    if part.startswith('V7_wo_fan'):   # Revo heater core / nozzle
        return BRASS if (r > 140 and 60 < g < 100) else RAIL_STEEL if max(c) > 200 else DARK_GREY
    if part == 'Heatsink':
        return DARK_GREY
    if part == 'PRODUCT_NAME':          # MGN12 block
        return RAIL_STEEL
    if r > 120 and g < 40 and b < 40:   # accent parts ([a] main body, CW2 latch/guidler/...)
        return RED
    if max(c) < 70:
        return BLACK
    return DARK_GREY


def main():
    parts, pcb, diff, blk = [], [], None, []
    for names, part, c, m in iter_parts(SRC, TOOLHEAD_ROT):
        if part == 'rgbw_led_pcb':
            pcb.append(m.bounds)
        if part == 'LED_Diffuser':
            diff = m.bounds
        if part == 'PRODUCT_NAME':
            blk.append(m.bounds)
        if DROP.search(part) or any(DROP.search(n) for n in names[1:3]):
            continue
        if np.linalg.norm(m.extents) < 3.0:
            continue
        # the page finds the nozzle tip as the lowest vertices: keep the Revo nozzle/heater exact
        parts.append(['toolhead', colour(part, names, c), m, part.startswith('V7_wo_fan')])
    # nozzle tip = lowest point of the hotend (file Y)
    allv = np.vstack([m.vertices for _, _, m, *_ in parts])
    lo = allv[:, 1].min()
    tipv = allv[allv[:, 1] < lo + 0.3]
    tip = np.array([tipv[:, 0].mean(), lo, tipv[:, 2].mean()])
    for p in parts:
        p[2].apply_translation(-tip)
    pcb = [b - tip for b in pcb]
    diff = diff - tip
    pcb.sort(key=lambda b: b[0][1])
    noz = [[(b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2, b[1][2] + 0.5] for b in pcb[:2]]
    logo = [(diff[0][0] + diff[1][0]) / 2, (diff[0][1] + diff[1][1]) / 2, diff[1][2] + 0.5]
    blk = np.vstack(blk) - np.tile(tip, (len(blk) * 2, 1)).reshape(-1, 3)
    meta = {
        'source': 'VoronDesign/Voron-Stealthburner CAD/Stealthburner_CW2.5_Assembly.step (GPL-3.0)',
        'units': 'mm, Y up, front +Z (same frame as rx.glb); nozzle tip at origin',
        'nozzle_tip': [0.0, 0.0, 0.0],
        'logo_led': [r1(logo)],
        'logo_led_note': 'front face of the LED diffuser (one RGBW LED behind it)',
        'nozzle_leds': [r1(p) for p in noz],
        'nozzle_leds_note': 'front faces of the two RGBW LED PCBs either side of the nozzle',
        'mount': {'kind': 'Voron X carriage (MGN12 block)',
                  'rail_block_centre': r1((blk.min(0) + blk.max(0)) / 2),
                  'rail_block_bbox': [r1(blk.min(0)), r1(blk.max(0))]},
    }
    build('stealthburner', parts, 70000, extra_meta=meta)


def trimesh_centre(ms):
    b = np.vstack([m.bounds for m in ms])
    return (b.min(0) + b.max(0)) / 2


if __name__ == '__main__':
    main()
