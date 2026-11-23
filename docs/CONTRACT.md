# Lightspeed — contract (v0.1)

One Klipper module (`lightspeed.py` + `lightspeed_engine.py`)
and one page (`web/lightspeed.html`). The page writes the config described here; the
module reads it. Both use the same effect engine (`lightspeed_engine.py` /
`web/lightspeed_engine.js`, checked by `test/parity.py`), including the scene
resolution (`plan_strips`) and trigger parsing (`parse_trigger`).

## Config (`lightspeed.cfg`, included from printer.cfg)

```ini
[lightspeed]
brightness: 0.6              # 0..1, applied to every output
guard_commands: MY_SCAN      # extra commands (comma list) during which MCU LEDs freeze,
                             # e.g. commands that stream sensor data over the same bus
model: voron24               # page only: printer model drawn in the simulator
size: 350,350,340            # page only: X,Y,Z build volume in mm

[lightspeed strip hotend]
output: neopixel hotend_rgb  # neopixel <name> | dotstar <name> | wled <ip>[:port] | esp_bridge <prefix> | pin <output_pin>
leds: 1-3                    # neopixel/dotstar only: which LEDs of the chain, 1-based (default: all)
count: 30                    # wled: LED count (required); esp_bridge: LED count shown on the page
start: 0                     # wled only: first LED on the WLED device (default 0)
place: toolhead_logo         # page only: where the strip sits on the model

[lightspeed group front]     # strips drawn as one: an effect runs across all of them
strips: left, right          # in this order (may span several chains / outputs)

[lightspeed scene idle]
hotend: breathe color=1a3cff color2=000814 period=4
all: rainbow period=8        # every strip this scene does not name
#priority: 0                 # optional: replaces the state's built-in priority

[lightspeed scene clean]     # a custom scene: any name that is not a state
trigger: macro CLEAN_NOZZLE  # when it is on (see Triggers)
priority: 70                 # default 50; command scenes default 90
hold: 2                      # s it stays on after the trigger goes off (optional)
hotend: solid color=00ffff   # <strip | group | all>: <effect spec>
```

* Section names: `lightspeed`, `lightspeed strip <name>`, `lightspeed group <name>`,
  `lightspeed scene <state | name>`. Strip, group and scene names: lowercase letters,
  digits, `_`; `all`, `trigger`, `priority`, `hold` are reserved; a group may not share
  a strip's name. A group lists existing strips, each once.
* Colours are `rrggbb` hex **without `#`** (`#` starts a comment in G-code).
* A state with no scene falls back: probing→homing→idle, paused→printing→idle,
  shutdown→error→idle, heating/printing/complete→idle. The state keeps **its own**
  priority while it shows the fallback scene's lines. With no *state* scene sections
  at all the built-in `DEFAULT_SCENES` are used (custom scenes do not count).

### States
`shutdown, error, homing, probing, paused, heating, printing, complete, idle`
(chosen in that order). `homing` = G28, `probing` = any probe move or a wrapped
calibration command (BED_MESH_CALIBRATE, QUAD_GANTRY_LEVEL, Z_TILT_ADJUST, ...).
`G28` selects the homing scene before a homing override starts its preparation
moves. Nested axis commands share that scene until the outer command finishes.
An available ESP bridge receives its scene before the override can suspend it.
Small MCU chains queue one guard frame. Large chains stay frozen.
`complete` lasts 60 s after a print ends.

### Custom scenes, priorities, resolution
Built-in state priorities (`BUILTIN_PRIORITY`): shutdown/error **100**, homing/probing
**80**, heating/printing/paused/complete **20**, idle **0**. A state section's own
`priority:` replaces it. Custom scenes: `priority:` default **50**; a `command` scene
defaults to **90** (above every state except error/shutdown, so macros written for
led_effect win as before).

Per strip, every tick: the candidates are the active built-in state scene (always)
and every active custom scene. The strip shows the **highest-priority candidate that
names it** — by its own line, by a group it is in, or by `all`. Within one scene its
own line beats a group line, which beats `all`. Ties: the most recently activated
wins (switching a scene on again counts as activating it). A strip nothing names is
off. A scene change crossfades (0.6 s) per strip.

On top of that, unchanged: `LIGHTSPEED_STATE` (state test) shows only that state's
scene, custom scenes are ignored while it holds; `LIGHTSPEED_PREVIEW` (manual /
page previews, strip, group or `all`) beats everything.

A group line is rendered **once** over the group's total LED count and cut into the
member strips in the order of `strips:`, so a comet runs from `left` into `right`
even across two chains.

### Triggers (`trigger:` of a custom scene, default `command`)
| trigger | on while | hold |
|---|---|---|
| `command` | switched on by `LIGHTSPEED_SCENE_ON NAME=<name> [HOLD=s]` or `SET_LED_EFFECT EFFECT=<name>`, until `LIGHTSPEED_SCENE_OFF` / `STOP_LED_EFFECTS` / `SET_LED_EFFECT ... STOP=1` | with `hold:` and no `HOLD=`, a switch-on lasts `hold` s (an event) |
| `macro <NAME>` | the G-code command / macro `NAME` runs (wrapped like guard_commands; the command always runs, its errors pass through, the scene ends even if it raises) | after it ends |
| `print_cancelled` | an event: `print_stats.state` became `cancelled` (the state stays `cancelled` until the next print, so it is not a level) | length of the event, default **30 s** |
| `extruding` / `retracting` | `motion_report.live_extruder_velocity` > +0.5 / < −0.5 mm/s; stays on 0.3 s after the last such sample (motion_report refreshes ~4×/s) | added to the 0.3 s |
| `<object>[.<field>...] <op> <number>` | the status value compares true; `op` is one of `> >= < <= == !=` | after it goes false |

Conditions read `lookup_object(object).get_status()` once per tick: any Klipper
status object (`heater_bed`, `extruder`, `fan`, `temperature_sensor chamber`,
`output_pin x`, `print_stats.info.current_layer`, ...). The object is the longest
prefix that exists (the name may contain one space), the rest is the field path; no
field → the first of `temperature, speed, value, progress`. `print_stats.progress`
reads `display_status.progress`. Booleans count as 1/0. A missing object → the
trigger stays off and one warning is printed at connect; a field that is missing or not
a number → off and one warning. Any custom scene, whatever its trigger, can be forced
on with `LIGHTSPEED_SCENE_ON` (the page previews with it). Triggers are evaluated in
the normal update tick: no timers of their own, no MCU traffic.

### Effect spec
`<effect> key=value ...` — effects and defaults are `EFFECTS` in the engine:
`off, solid, breathe, blink, rainbow, chase, comet, scan, twinkle, fire, progress, heat, follow`.
Parameters: `color color2 period size density source axis(x|y|z) reverse(0|1)
gradient(0|1) min max colors`.

Value sources (`progress`, `heat`):
* `source=progress | heat | <object>.<field>` — e.g. `heater_bed.temperature`,
  `extruder.temperature`, `fan.speed`, `motion_report.live_extruder_velocity`,
  `temperature_sensor chamber.temperature` (the object's second word simply follows:
  `source=temperature_sensor chamber.temperature min=20`). `progress` defaults to
  `source=progress`, `heat` to `source=heat`.
* `min= max=` map the raw value to 0..1 (may be negative; `auto` = default). Defaults:
  progress/heat 0..1; `*.temperature`/`*.target`: 20..`max_temp` of that heater on the
  printer (the page assumes extruder 300, heater_bed 120, others 80);
  `live_extruder_velocity` 0..10; anything else 0..1. `min > max` inverts.
* `colors=rrggbb,rrggbb,...` evenly spaced colour stops. `heat`: the whole strip takes
  the stop colour of the value (first = min, last = max). `progress`: the lit part runs
  through the stops along the strip; with `gradient=1` the lit part takes the colour of
  the value. Without `colors`, heat mixes color2 (cold) → color (hot) as before.
* The engine is pure: the module puts `ctx['vars'] = {source: raw}` and
  `ctx['ranges'] = {source: [lo, hi]}`; the engine maps.

### Example: front bar
One 24-LED chain behind the front door split into two strips: left shows the bed
temperature, right the hotend temperature, both blue→yellow→orange→red→purple; a
cancelled print blinks the whole bar red; a nozzle wipe macro and the extruder show on
the toolhead.

```ini
[lightspeed strip front_left]
output: neopixel front_bar
leds: 1-12
place: door_top

[lightspeed strip front_right]
output: neopixel front_bar
leds: 13-24
place: door_top

[lightspeed strip nozzle]
output: neopixel sb_leds
leds: 2-3
place: nozzle

[lightspeed group front]
strips: front_left, front_right

[lightspeed scene idle]
front_left: heat source=heater_bed.temperature colors=0030ff,ffe000,ff8000,ff0000,a000ff
front_right: heat source=extruder.temperature colors=0030ff,ffe000,ff8000,ff0000,a000ff
nozzle: solid color=202020

[lightspeed scene printing]
front: progress colors=0030ff,ffe000,ff8000,ff0000,a000ff
nozzle: solid color=ffffff

[lightspeed scene cancelled]
trigger: print_cancelled
priority: 60
hold: 20
front: blink color=ff0000 period=0.6

[lightspeed scene wipe]
trigger: macro CLEAN_NOZZLE
priority: 70
hold: 1
nozzle: chase color=00ffff period=0.5

[lightspeed scene push]
trigger: extruding
priority: 55
nozzle: progress source=motion_report.live_extruder_velocity min=0 max=8 color=00ff40

[lightspeed scene pull]
trigger: retracting
priority: 56
nozzle: solid color=ff2000

[lightspeed scene chamber_hot]
trigger: temperature_sensor chamber.temperature > 45
priority: 30
front_right: breathe color=ff8000 color2=200800 period=2

[lightspeed scene party]      # trigger: command (priority 90)
all: rainbow period=3
```
`SET_LED_EFFECT EFFECT=party` / `STOP_LED_EFFECTS` from any macro switch `party`.

## Outputs
| output | how | load |
|---|---|---|
| `neopixel`/`dotstar` | Klipper's own `update_leds()` of that chain | `mcu_fps` targets a 3% MCU time budget with a maximum of 25 fps. During homing/probing, chains of up to 48 bytes can show one frame on entry. Larger chains stay frozen. |
| `wled` | UDP realtime DNRGB to port 21324, non-blocking, keep-alive 1 s | none on the printer MCUs, 40 fps, keeps running during homing |
| `esp_bridge` | producer on the existing `[esp_bridge]` (keys `<prefix>.fx/.rgb/.p/.b/.st`); the node animates locally. A value-driven `progress`/`heat` sends its current single colour (`value_color`) and the mapped value as `.p` | a few packets per state change |
| `pin` | `[output_pin]`, on/off or PWM brightness | a change only |

## G-code
| command | |
|---|---|
| `LIGHTSPEED_STATUS` | state, guard, every strip with its effect, fps and errors |
| `LIGHTSPEED_STATE STATE=<state\|auto> [HOLD=10]` | show a state's scene (HOLD=0 → back to auto) |
| `LIGHTSPEED_PREVIEW STRIP=<strip\|group\|all> EFFECT="<spec>" [HOLD=30]` | try an effect live (page live mode uses this) |
| `LIGHTSPEED_SCENE STATE=<state\|custom> STRIP=<strip\|group\|all> EFFECT="<spec>"` | change a scene line until restart (empty EFFECT removes the line; a custom scene stays) |
| `LIGHTSPEED_SCENE_ON NAME=<custom> [HOLD=s]` | switch a custom scene on (any trigger type), for HOLD s or until off |
| `LIGHTSPEED_SCENE_OFF NAME=<custom\|all>` | switch it off (trigger-driven scenes stay driven by their trigger) |
| `LIGHTSPEED_BRIGHTNESS VALUE=<0..1>` | brightness until restart |
| `LIGHTSPEED_STRIP STRIP=<name> BRIGHTNESS=<0..1>` | strip brightness until restart |
| `LIGHTSPEED_ENABLE VALUE=<0\|1>` | stop / start all Lightspeed output |
| `SET_LED_EFFECT EFFECT=<scene> [STOP=1]` | **only when no `led_effect` module is loaded** (no `led_effect` / `led_effect <name>` object, command not taken): scene on (`STOP=1` off). Name case-insensitive; `FADETIME= REPLACE= RESTART= LEDS=` accepted and ignored; an unknown name prints a message, never an error |
| `STOP_LED_EFFECTS [EFFECT=<scene>]` | same condition: every forced-on scene off, or just that one; strips then show what wins below (e.g. the state scene). `LEDS=` ignored |

## Web endpoint `lightspeed/cmd` (Moonraker: `POST /printer/lightspeed/cmd`)
Silent (nothing in the console). `{"action": ...}`:
`state {state, hold}`, `preview {strip, effect, hold}`, `scene {state, strip, effect}`,
`strip {strip, brightness}`, `brightness {value}`, `scene_on {name, hold}` (hold 0 =
until off), `scene_off {name}` (`all` allowed), `batch {actions: [...]}`.

## Printer maintenance

* `POST /printer/lightspeed/migrate` with `{"start":1}` starts migration of the running
  Klipper instance's config while the printer is idle. Poll with `{"start":0}`.
  A verified switch returns `state: done, ok: true`. A manual merge returns
  `state: merge_required, ok: false` and the draft filename in `candidate`.
  Other failures return `ok: false` with a diagnostic in `output`.
* `POST /printer/lightspeed/restart_if_idle` checks printer activity under the G-code
  mutex before issuing `FIRMWARE_RESTART`. A busy G-code queue, an active or paused
  print, or unavailable idle status blocks the restart.

## Web endpoint `lightspeed/scan` (Moonraker: `POST /printer/lightspeed/scan`)
Finds other printers on the LAN that run Lightspeed. Returns at once; the scan runs in
background threads (never in the reactor) and only *reads* other hosts.
Body (all optional): `start` (default 1; 0 = only read the current state, never start),
`refresh` (default 0; 1 = start a new scan even if a cached result exists).
* A scan runs at most once at a time; a finished result is answered from cache for
  10 minutes, after that the next `start` call scans again.
* Range: this printer's own IPv4 /24 subnet(s), at most 2 interfaces, private ranges only
  (10/8, 172.16/12, 192.168/16); loopback, link-local and docker / bridge / veth
  interfaces are skipped, and so are this printer's own addresses.
* Per host: TCP connect to 7125 (Moonraker), then 80 (Mainsail / Fluidd proxy), 0.4 s
  timeout, 32 hosts at once; an open port gets
  `GET /printer/objects/query?lightspeed=version` (1.5 s) and, on a hit,
  `GET /printer/info` for the hostname. Moonraker without a `lightspeed` object (or with
  Klippy not ready) is not listed. Stdlib only; errors are caught and logged once.

Answer: `{state: idle|running|done, found: [{host (IP), port (the port that answered:
Moonraker's, not the page's), hostname, version}], started, finished (Unix time or null),
subnets: ['a.b.c.0/24'], addresses: [this printer's IPs], done, total, error,
self: {hostname}}`. `found` is sorted by IP; hostname / version from other devices are
checked (`[A-Za-z0-9._-]`, short) and dropped when odd. `klippy:disconnect` stops a scan.

## Status object `lightspeed`
`version, host (this printer's hostname, '' if unknown), enabled, state, auto_state, override, guard, brightness, model, size,
mcu_rate, strips: {name: {output, kind, count, place, color, spacing, effect, fps,
bytes_sent, dropped, errors, disabled, last_error, manual}},
scenes_active: [custom scene names, highest priority first],
custom_scenes: [{name, trigger, priority, hold, active, forced}],
groups: {name: [strips]}, ranges: {source: [lo, hi]}, compat: [registered
SET_LED_EFFECT / STOP_LED_EFFECTS]`. Each NeoPixel or DotStar strip also reports
`chain_count` (LEDs on the entire physical chain) and `frame_bytes` (the byte count
used for its MCU budget). `get_status` never raises.

## Page
* Served by Mainsail/Fluidd from the printer (`/lightspeed/lightspeed.html`) → talks to
  Moonraker with relative URLs. Opened from disk → offline designer.
* Live mode: `GET /printer/objects/query?lightspeed`, `POST /printer/lightspeed/cmd`
  (G-code fallback for old modules), save = upload `lightspeed.cfg` to the config root
  (`POST /server/files/upload`, root=config).
* Scene line edits (built-in and custom) go live with `scene`; new / removed custom
  scenes, triggers, priorities, holds and groups need Save & apply (restart).
* A custom scene chip previews with `scene_on {name, hold}` and `scene_off` at the end.

### Several printers
* A browser cannot write to another printer's Moonraker from this page (CORS; Moonraker
  normally does not trust IP origins), so the switcher **navigates** (same tab) to that
  printer's own page, `http://<host>[:port]/lightspeed/` (the web port; never 7125).
  Picking this page's own host does nothing. The switcher and the scan never send
  commands or uploads to another host; the scan runs on this printer and is asked through
  this page's own Moonraker. (The older *Printer address* field in Settings > Live still
  points the page's Moonraker requests at the address typed there; the printer named in
  the header is then that address.)
* Header: a printer button (left of party mode) with the configured printer's name:
  `lightspeed.host`, else `/printer/info` hostname, else the host; `Demo` when no
  Moonraker answers (opened from disk, static host / 404). Its menu (keyboard + aria like
  the language menu, closes on outside click / Escape) lists this printer (checked),
  saved printers (removable: × or Delete), the printers found by the last scan with their
  Lightspeed version, *Scan network* (`lightspeed/scan`, progress while it runs; only while
  connected and Klipper is ready, otherwise disabled with a hint; a 404 from an older
  module shows an update hint) and *Add printer…* (host or IPv4, optional `:port`,
  validated). Saved printers: `UI.printers`, last scan: `UI.scan` (localStorage of this
  page's origin, checked on load).
* The configured printer is named wherever settings are written: under the Settings
  tabs, in the Save & apply box, on the floating Save & apply button (tooltip) and in the
  Save & apply / Restart firmware confirm dialogs ("Printer: <name> (<host>)").
* Limits: only the scanning printer's own /24 subnet(s); another printer is found only
  when its Moonraker answers this printer without authorization (trusted clients) and
  Klippy is ready; any Lightspeed version is found, but scanning needs this module
  version on the printer whose page is open, and the target printer needs this page
  version for its own switcher. Saved lists are per origin (each printer's page has its
  own). Other Moonraker instances on the scanning host itself are not listed (its own
  addresses are skipped); a found printer always opens on port 80 (another web port:
  *Add printer…* with `:port`); VPN interfaces (tun / tap / wg / zt / tailscale) are
  skipped; IPv4 only.
