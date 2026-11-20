"""Dragon Burner (chirpy2605, github.com/chirpy2605/voron V0/Dragon_Burner, GPL-3.0) -> web/models/dragonburner.glb/.json

The STEP (CAD/Dragon_Burner_v0.2_v8.step, the V0.2 version) is a catalogue of printed parts only:
every cowl, duct, hotend mount and extruder mount is drawn in place, and there are no fans, no
hotend and no X carriage. One configuration is picked: MainBody cowl, the two 'Standard' ducts,
RevoVoron hotend mount, bowden mount, NeoPixel nozzle-LED mount, Neopixel logo carrier + Voron
diffuser. The Revo Voron hotend is taken from the official Stealthburner STEP and placed on the
measured mount: axis = filament bore of RevoVoron_Mount (x 147.73, y -130.76), heatsink top on the
mount's lower face (z 94), as in the DownDraft/Stealthburner CAD. No fans are added (their place is
not fixed by the CAD).

    python -I tools/models/build_dragonburner.py
"""
import os
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cad_common import (RAW, TOOLHEAD_ROT, iter_parts, build, r1, BLACK, DARK_GREY, RAIL_STEEL,
                        DIFFUSER, BRASS)

SRC = RAW + r"\dragonburner_raw.glb"
SB = RAW + r"\stealthburner_raw.glb"
KEEP = {'MainBody', 'Standard', 'Standard (1)', 'Bowden_Mount (1)', 'NeoPixel_Mount',
        '[o]_Carrier_Neopixel', '[c]_Voron_Diffuser'}
AXIS = np.array([147.73, -130.76])   # bore centre of Hotend_Mounts/RevoVoron_Mount (CAD mm)
SEAT = 94.0                           # lower face of that mount = Revo heatsink top
SB_REVO = {'V7_wo_fanV7_Fitment_CAD_wo_fan_1.3', 'Heatsink', 'PTFE Tube Revo Voron 29.8'}


def main():
    parts, diff, npm = [], None, None
    for names, part, c, m in iter_parts(SRC):
        anc = ' '.join(names[1:3])
        if part == 'RevoVoron_Mount' and 'Hotend_Mounts' in anc:
            parts.append(['toolhead', BLACK, m])
            continue
        if part not in KEEP:
            continue
        if part == '[c]_Voron_Diffuser':
            diff = m.bounds.copy()
        if part == 'NeoPixel_Mount':
            npm = m.bounds.copy()
        parts.append(['toolhead', DIFFUSER if part.startswith('[c]') else BLACK, m])
    # Revo Voron from the Stealthburner CAD: its heatsink top is at z 49, axis at x/y 0
    revo, hs_top = [], None
    for names, part, c, m in iter_parts(SB):
        if part in SB_REVO:
            if part == 'Heatsink':
                hs_top = m.bounds[1][2]
            col = (BRASS if (c[0] > 140 and 60 < c[1] < 100) else RAIL_STEEL if max(c) > 200 else DARK_GREY)
            revo.append([part, col, m])
    for part, col, m in revo:
        m.apply_translation((AXIS[0], AXIS[1], SEAT - hs_top))
        parts.append(['toolhead', col, m])
    for p in parts:
        p[2].apply_transform(TOOLHEAD_ROT)
    allv = np.vstack([m.vertices for _, _, m in parts])
    lo = allv[:, 1].min()
    tipv = allv[allv[:, 1] < lo + 0.3]
    tip = np.array([tipv[:, 0].mean(), lo, tipv[:, 2].mean()])
    for p in parts:
        p[2].apply_translation(-tip)
    T = lambda b: (np.c_[b, np.ones(2)] @ TOOLHEAD_ROT.T)[:, :3] - tip
    d = T(diff)
    n = T(npm)
    lo3, hi3 = d.min(0), d.max(0)
    nlo, nhi = n.min(0), n.max(0)
    body = np.vstack([m.bounds for _, _, m in parts])
    meta = {
        'source': 'chirpy2605/voron V0/Dragon_Burner/CAD/Dragon_Burner_v0.2_v8.step (GPL-3.0); Revo Voron '
                  'hotend from VoronDesign/Voron-Stealthburner Stealthburner_CW2.5_Assembly.step (GPL-3.0)',
        'config': 'V0.2 Dragon Burner: MainBody cowl, Standard ducts, RevoVoron mount, bowden mount, '
                  'NeoPixel nozzle LED mount, Neopixel logo carrier + Voron diffuser',
        'missing_in_cad': ['part-cooling fans (2x 4010 blower)', 'hotend fan', 'X carriage (V0 MGN9)'],
        'borrowed_parts': sorted(SB_REVO),
        'nozzle_tip_derived': True,
        'units': 'mm, Y up, front +Z (same frame as rx.glb); nozzle tip at origin',
        'nozzle_tip': [0.0, 0.0, 0.0],
        'logo_led': [r1([(lo3[0] + hi3[0]) / 2, (lo3[1] + hi3[1]) / 2, hi3[2] + 0.5])],
        'logo_led_note': 'front face of the logo diffuser (one WS2812B behind it)',
        'nozzle_leds': [r1([(nlo[0] + nhi[0]) / 2, (nlo[1] + nhi[1]) / 2, nhi[2] + 0.5])],
        'nozzle_leds_note': 'front face centre of the NeoPixel nozzle LED mount (LED count not in the CAD)',
        'mount': {'kind': 'Voron V0 X carriage (not in the CAD); rear face of the cowl',
                  'rear_face_centre': r1([(body[:, 0].min() + body[:, 0].max()) / 2,
                                          (body[:, 1].min() + body[:, 1].max()) / 2, body[:, 2].min()])},
    }
    build('dragonburner', parts, {'toolhead': 60000}, extra_meta=meta)


if __name__ == '__main__':
    main()
