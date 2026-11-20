"""Voron DownDraft (official, github.com/VoronDesign/voron-downdraft) -> web/models/downdraft.glb/.json

The DownDraft STEP (CAD/Downdraft_Toolheads.zip, GPL-3.0) is a catalogue of printed parts: every
option is drawn in place (3 front-mount heights x 3 cutters, 4010/5015, 14 hotend mounts, 20 PCB
mounts) and it holds no hotend and no X carriage. One configuration is picked here:
dual 5015 blowers, low front mount + its 2510 hotend fan and spacer, Revo Voron mount, collet bowden
mount, generic umbilical PCB36 mount, x2 nozzle LED mount, Neopixel logo carrier + diffuser.
The Revo Voron hotend and the X carriage are taken from the official Stealthburner STEP (same
GPL-3.0 project, same datum: nozzle at the origin, Revo mount seat at z 49 = Revo heatsink top).

    python -I tools/models/build_downdraft.py
"""
import os
import re
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cad_common import (RAW, TOOLHEAD_ROT, iter_parts, build, r1, BLACK, DARK_GREY, RAIL_STEEL,
                        FAN_GREY, RED, DIFFUSER, BRASS)

SRC = RAW + r"\downdraft_raw.glb"
SB = RAW + r"\stealthburner_raw.glb"
KEEP = {'[a]_5015_Front_Mount_Low', '5015_Fan_Mount_Right', '5015_Fan_Mount_Left',
        '5015_2510_Fan_Spacer', 'Revo_Voron_Mount', 'Bowden_Mount_Collet', 'Umbilical_PCB36_Mount',
        'Nozzle_LEDx2_Mount', '[o]_Neopixel_Carrier', '[o]_LED_Diffuser_Mask', '[c]_LED_Diffuser'}
SB_KEEP = {'V7_wo_fanV7_Fitment_CAD_wo_fan_1.3', 'Heatsink', 'PTFE Tube Revo Voron 29.8',
           'SW_Carriage_Left', 'SW_Carriage_Right', 'x_carriage_frame_left', 'x_carriage_frame_right',
           'PRODUCT_NAME', 'Part38'}


def colour(part, c):
    r, g, b = c
    if 'Fan' in part and 'Mount' not in part and 'Spacer' not in part:
        return FAN_GREY
    if part == '[c]_LED_Diffuser':
        return DIFFUSER
    if part.startswith('V7_wo_fan'):
        return BRASS if (r > 140 and 60 < g < 100) else RAIL_STEEL if max(c) > 200 else DARK_GREY
    if part == 'Heatsink':
        return DARK_GREY
    if part == 'PRODUCT_NAME':
        return RAIL_STEEL
    if part.startswith('Neopixel Mini Button'):
        return DIFFUSER if max(c) > 200 else BLACK
    if part.startswith('[a]') or (r > 120 and g < 40 and b < 40):
        return RED
    if max(c) < 70:
        return BLACK
    return DARK_GREY


def main():
    parts, noz, logo_pcb, diff, blk, borrowed = [], [], None, None, [], set()
    for names, part, c, m in iter_parts(SRC, TOOLHEAD_ROT):
        anc = ' '.join(names[1:4])
        if part.startswith('Neopixel Mini Button') and '5015_Hardware' in anc:
            noz.append(m.bounds)
            parts.append(['toolhead', colour(part, c), m])
            continue
        if part == 'rgbw_led_pcb (1)':
            logo_pcb = m.bounds if logo_pcb is None else np.vstack([logo_pcb, m.bounds])
        if part == '[c]_LED_Diffuser':
            diff = m.bounds
        if part.startswith('5015 Blower Fan'):
            parts.append(['toolhead', FAN_GREY, m])
            continue
        if part.startswith('2510 Axial Fan (3)') and abs(m.bounds[0][1] - 23.0) < 1.5:  # file Y = CAD z
            parts.append(['toolhead', FAN_GREY, m])                                  # the low-mount fan
            continue
        if part in KEEP:
            parts.append(['toolhead', colour(part, c), m])
    for names, part, c, m in iter_parts(SB, TOOLHEAD_ROT):
        if part in SB_KEEP:
            if part == 'PRODUCT_NAME':
                blk.append(m.bounds)
            borrowed.add(part)
            parts.append(['toolhead', colour(part, c), m])
    allv = np.vstack([m.vertices for _, _, m in parts])
    lo = allv[:, 1].min()
    tipv = allv[allv[:, 1] < lo + 0.3]
    tip = np.array([tipv[:, 0].mean(), lo, tipv[:, 2].mean()])
    for p in parts:
        p[2].apply_translation(-tip)
    # nozzle LEDs: the two Neopixel mini buttons (4 colour pieces each) -> one point per board
    nb = np.vstack(noz) - tip
    left = nb[nb[:, 0] < 0]
    right = nb[nb[:, 0] > 0]
    nl = [[(q[:, 0].min() + q[:, 0].max()) / 2, (q[:, 1].min() + q[:, 1].max()) / 2, q[:, 2].max() + 0.5]
          for q in (left, right)]
    d = diff - tip
    logo = [(d[0][0] + d[1][0]) / 2, (d[0][1] + d[1][1]) / 2, d[1][2] + 0.5]
    bk = np.vstack(blk) - tip
    meta = {
        'source': 'VoronDesign/voron-downdraft CAD/Downdraft_Toolheads.zip (GPL-3.0); hotend + X carriage '
                  'from VoronDesign/Voron-Stealthburner Stealthburner_CW2.5_Assembly.step (GPL-3.0)',
        'config': 'dual 5015, low front mount, 2510 hotend fan, Revo Voron, collet bowden mount, '
                  'umbilical PCB36 mount, x2 nozzle LEDs, Neopixel logo carrier',
        'borrowed_parts': sorted(borrowed),
        'units': 'mm, Y up, front +Z (same frame as rx.glb); nozzle tip at origin',
        'nozzle_tip': [0.0, 0.0, 0.0],
        'logo_led': [r1(logo)],
        'logo_led_note': 'front face of the logo LED diffuser (RGBW LED behind it)',
        'nozzle_leds': [r1(p) for p in nl],
        'nozzle_leds_note': 'front faces of the two Neopixel mini button boards in the x2 nozzle LED mount',
        'mount': {'kind': 'Voron X carriage (MGN12 block), carriage from the Stealthburner CAD',
                  'rail_block_centre': r1((bk.min(0) + bk.max(0)) / 2),
                  'rail_block_bbox': [r1(bk.min(0)), r1(bk.max(0))]},
    }
    build('downdraft', parts, {'toolhead': 65000}, extra_meta=meta)


if __name__ == '__main__':
    main()
