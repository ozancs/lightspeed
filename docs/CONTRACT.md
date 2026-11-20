# Lightspeed — contract (v0.1)

One Klipper module (`lightspeed.py` + `lightspeed_engine.py`)
and one page (`web/lightspeed.html`). The page writes the config described here; the
module reads it. Both use the same effect engine (`lightspeed_engine.py` /
`web/lightspeed_engine.js`, checked by `test/parity.py`).

## Config (`lightspeed.cfg`, included from printer.cfg)

```ini
[lightspeed]
brightness: 0.6              # 0..1, applied to every output
guard_commands: MY_SCAN      # extra commands (comma list) during which MCU LEDs freeze,
                             # e.g. commands that stream sensor data over the same bus
model: voron24               # page only: printer model drawn in the simulator
size: 350,350,340            # page only: X,Y,Z build volume in mm

[lightspeed strip hotend]
output: neopixel hotend_rgb  # neopixel <name> | dotstar <name> | wled <ip>[:port] | esp_bridge <prefix>
leds: 1-3                    # neopixel/dotstar only: which LEDs of the chain, 1-based (default: all)
count: 30                    # wled: LED count (required); esp_bridge: LED count shown on the page
start: 0                     # wled only: first LED on the WLED device (default 0)
place: toolhead_logo         # page only: where the strip sits on the model

[lightspeed scene idle]
hotend: breathe color=1a3cff color2=000814 period=4
all: rainbow period=8        # every strip this scene does not name
```

* Section names: `lightspeed`, `lightspeed strip <name>`, `lightspeed scene <state>`.
  Strip names: lowercase letters, digits, `_`.
* Colours are `rrggbb` hex **without `#`** (`#` starts a comment in G-code).
* A state with no scene falls back: probing→homing→idle, paused→printing→idle,
  shutdown→error→idle, heating/printing/complete→idle. With no scene sections at all the
  built-in `DEFAULT_SCENES` are used.

### States
`shutdown, error, homing, probing, paused, heating, printing, complete, idle`
(priority in that order). `homing` = G28, `probing` = any probe move or a wrapped
calibration command (BED_MESH_CALIBRATE, QUAD_GANTRY_LEVEL, Z_TILT_ADJUST, ...).
`complete` lasts 60 s after a print ends.

### Effect spec
`<effect> key=value ...` — effects and defaults are `EFFECTS` in the engine:
`off, solid, breathe, blink, rainbow, chase, comet, scan, twinkle, fire, progress, heat, follow`.
Parameters: `color color2 period size density source(progress|heat) axis(x|y|z) reverse(0|1)`.

## Outputs
| output | how | load |
|---|---|---|
| `neopixel`/`dotstar` | Klipper's own `update_leds()` of that chain | MCU: fps limited so updates use ≤3 % of MCU time (`mcu_fps`), max 25 fps, frozen during homing/probing (chains ≤16 LEDs send one frame on entry) |
| `wled` | UDP realtime DNRGB to port 21324, non-blocking, keep-alive 1 s | none on the printer MCUs, 40 fps, keeps running during homing |
| `esp_bridge` | producer on the existing `[esp_bridge]` (keys `<prefix>.fx/.rgb/.p/.b/.st`); the node animates locally | a few packets per state change |

## G-code
| command | |
|---|---|
| `LIGHTSPEED_STATUS` | state, guard, every strip with its effect, fps and errors |
| `LIGHTSPEED_STATE STATE=<state\|auto> [HOLD=10]` | show a state's scene (HOLD=0 → back to auto) |
| `LIGHTSPEED_PREVIEW STRIP=<name\|all> EFFECT="<spec>" [HOLD=30]` | try an effect live (page live mode uses this) |
| `LIGHTSPEED_BRIGHTNESS VALUE=<0..1>` | brightness until restart |
| `LIGHTSPEED_ENABLE VALUE=<0\|1>` | stop / start all Lightspeed output |

## Status object `lightspeed`
`version, enabled, state, auto_state, override, guard, brightness, model, size,
mcu_rate, strips: {name: {output, kind, count, place, effect, fps, bytes_sent, dropped,
errors, disabled, last_error}}`

## Page
* Served by Mainsail/Fluidd from the printer (`/lightspeed/lightspeed.html`) → talks to
  Moonraker with relative URLs. Opened from disk → offline designer.
* Live mode: `GET /printer/objects/query?lightspeed`, `POST /printer/gcode/script`,
  save = upload `lightspeed.cfg` to the config root (`POST /server/files/upload`, root=config).
