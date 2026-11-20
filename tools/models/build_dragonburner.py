"""Dragon Burner (chirpy2605, github.com/chirpy2605/voron V0/Dragon_Burner, GPL-3.0) -> web/models/dragonburner.glb/.json

The STEP (CAD/Dragon_Burner_v0.2_v8.step, the V0.2 version) is a catalogue of printed parts only:
every cowl, duct, hotend mount and extruder mount is drawn in place, and there are no fans, no
hotend and no X carriage. One configuration is picked: MainBody cowl, the two 'Standard' ducts,
RevoVoron hotend mount, bowden mount, NeoPixel nozzle-LED mount, Neopixel logo carrier + Voron
diffuser. The Revo Voron hotend is taken from the official Stealthburner STEP and placed on the
measured mount: axis = filament bore of RevoVoron_Mount (x 147.73, y -130.76), heatsink top on the
mount's lower face (z 94), as in the DownDraft/Stealthburner CAD.
The STEP has no fans. The Dragon Burner README (BOM) lists 1x 3010 axial hotend fan (slides up into the
front of the cowl, behind the logo; the middle part that was missing) and 2x 4010 blowers for part
cooling in the slots at the left/right rear of the cowl, blowing down into the ducts. They are added
from real fan CAD: the 3010 axial from the Voron 0.2 Mini Stealthburner STEP (VoronDesign/Voron-0,
GPLv3), the 4010 blowers from the DownDraft STEP (VoronDesign/voron-downdraft, GPLv3). Their boxes
are fitted to the measured cowl pockets (cowl slices, STEP mm): hotend fan x 132.7..162.7,
z 68.4..98.4, y -153.4..-143.4 (the seat of the 2510 spacer); blowers x 120.2..130.2 (left) and
165.2..175.2 (right), y -154.4..-114.4, z 64.7..104.7 (on top of the duct inlets, z 64.7).

    python -I tools/models/build_dragonburner.py
"""
import os
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cad_common import (RAW, TOOLHEAD_ROT, iter_parts, build, r1, BLACK, DARK_GREY, RAIL_STEEL,
                        DIFFUSER, BRASS, FAN_GREY)
import trimesh  # noqa: E402

SRC = RAW + r"\dragonburner_raw.glb"
SB = RAW + r"\stealthburner_raw.glb"
KEEP = {'MainBody', 'Standard', 'Standard (1)', 'Bowden_Mount (1)', 'NeoPixel_Mount',
        '[o]_Carrier_Neopixel', '[c]_Voron_Diffuser'}
AXIS = np.array([147.73, -130.76])   # bore centre of Hotend_Mounts/RevoVoron_Mount (CAD mm)
SEAT = 94.0                           # lower face of that mount = Revo heatsink top
V0 = os.path.join(RAW, "voron0_raw.glb")
DD = os.path.join(RAW, "downdraft_raw.glb")
# target boxes (STEP mm) measured from the MainBody cowl and the Standard ducts
FAN_BOX = {
    'hotend_3010': ((132.7, -153.4, 68.4), (162.7, -143.4, 98.4)),
    'blower_left': ((120.2, -154.4, 64.7), (130.2, -114.4, 104.7)),
    'blower_right': ((165.2, -154.4, 64.7), (175.2, -114.4, 104.7)),
}
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
        parts.append(['toolhead', col, m, part.startswith('V7_wo_fan')])
    fans, fan_fit = fan_parts()
    for p in parts:
        p[2].apply_transform(TOOLHEAD_ROT)
    allv = np.vstack([m.vertices for _, _, m, *_ in parts])
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
    body = np.vstack([m.bounds for _, _, m, *_ in parts])
    meta = {
        'source': 'chirpy2605/voron V0/Dragon_Burner/CAD/Dragon_Burner_v0.2_v8.step (GPL-3.0); Revo Voron '
                  'hotend from VoronDesign/Voron-Stealthburner Stealthburner_CW2.5_Assembly.step (GPL-3.0); '
                  'fans (3010 axial, 2x 4010 blower) from the Voron 0.2 and DownDraft STEP files (GPL-3.0)',
        'fans_note': 'fan types and places from the Dragon Burner README/BOM (3010 hotend fan in the front of '
                     'the cowl, 4010 blowers in the rear side slots); boxes fitted to the measured cowl pockets',
        'config': 'V0.2 Dragon Burner: MainBody cowl, Standard ducts, RevoVoron mount, bowden mount, '
                  'NeoPixel nozzle LED mount, Neopixel logo carrier + Voron diffuser, 3010 hotend fan, 2x 4010 blowers',
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
    for col, m in fans:
        m.apply_transform(TOOLHEAD_ROT)
        m.apply_translation(-tip)
        parts.append(['toolhead', col, m])
    lowest = min(m.bounds[0][1] for _, m in fans)
    assert lowest > 0.0, 'a fan reaches below the nozzle tip'
    meta['added_parts'] = fan_fit
    meta['missing_in_cad'] = ['X carriage (V0 MGN9)']
    build('dragonburner', parts, 75000, extra_meta=meta)


def fan_parts():
    """the three fans from real fan CAD, each moved so its box sits in the measured pocket"""
    src = {'hotend_3010': (V0, lambda part, names: part == '3010 DC Axial Fan'),
           'blower_left': (DD, lambda part, names: names[0].startswith('4010 Blower Fan:1')),
           'blower_right': (DD, lambda part, names: names[0].startswith('4010 Blower Fan (1):1'))}
    out, fit = [], {}
    for key, (path, pick) in src.items():
        ms = [m for names, part, c, m in iter_parts(path) if pick(part, names)]
        fan = trimesh.util.concatenate(ms)
        lo, hi = np.array(FAN_BOX[key][0]), np.array(FAN_BOX[key][1])
        size = fan.extents
        fan.apply_translation((lo + hi) / 2 - fan.bounds.mean(0))
        fit[key] = {'source': ('VoronDesign/Voron-0 V0.2 Mini Stealthburner STEP, "3010 DC Axial Fan"' if path == V0 else
                               'VoronDesign/voron-downdraft Downdraft_Toolheads STEP, "4010 Blower Fan"'),
                    'size_mm': r1(size), 'pocket_mm': r1(hi - lo),
                    'fit_residual_mm': round(float(np.abs(size - (hi - lo)).max()), 2)}
        out.append((FAN_GREY, fan))
    return out, fit


if __name__ == '__main__':
    main()
