# Lightspeed - Klipper module
#
# Printer-state driven LED effects that never get in the way of printing:
#  * effects are pure functions of time (lightspeed_engine), so frames can
#    be dropped at any moment without anything going out of step
#  * MCU-driven chains (neopixel, dotstar) are rate limited to a small
#    share of MCU time, never have two updates in flight, and freeze while
#    the printer homes, probes or calibrates
#  * WLED (UDP over Wi-Fi) and esp_bridge outputs put no load on the
#    printer's MCUs and keep animating during homing
#  * any error in an output disables that output, never the printer
#
# Config: see docs/CONTRACT.md.
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging
import math
import re
import socket
import threading

try:
    from . import lightspeed_engine as engine
except ImportError:          # tests import it outside Klipper's package
    import lightspeed_engine as engine

__version__ = engine.__version__

# Calibration commands during which MCU LED traffic stays frozen, and the
# printer state shown meanwhile (None = keep the current scene).
GUARDED_COMMANDS = {
    'BED_MESH_CALIBRATE': 'probing', 'QUAD_GANTRY_LEVEL': 'probing',
    'Z_TILT_ADJUST': 'probing', 'SCREWS_TILT_CALCULATE': 'probing',
    'PROBE': 'probing', 'PROBE_ACCURACY': 'probing',
    'PROBE_CALIBRATE': 'probing', 'PROBE_EDDY_CURRENT_CALIBRATE': 'probing',
    'LDC_CALIBRATE_DRIVE_CURRENT': 'probing', 'Z_ENDSTOP_CALIBRATE': 'probing',
    'DELTA_CALIBRATE': 'probing', 'BED_TILT_CALIBRATE': 'probing',
    'AXIS_TWIST_COMPENSATION_CALIBRATE': 'probing',
    'SHAPER_CALIBRATE': None, 'TEST_RESONANCES': None,
}
GUARD_HOLD = 1.0          # s the guard stays on after the last guarded move
SNAPSHOT_MAX_BYTES = 48   # chains up to 16 RGB LEDs show the guard scene once
COMPLETE_HOLD = 60.0      # s the 'complete' scene stays after a print
CROSSFADE = 0.6           # s between scenes
HEAT_MARGIN = 2.0         # degrees below target that still counts as heating
OUTPUT_ERROR_LIMIT = 5    # consecutive errors before an output is disabled
TICK_ERROR_LIMIT = 10     # consecutive errors before Lightspeed stops
LATE_LIMIT = 0.15         # s a tick may run late before the MCU rate drops
WLED_PORT = 21324
WLED_KEEPALIVE = 1.0
WLED_TIMEOUT = 2          # s WLED waits before returning to its own effect
WLED_MAX_PER_PACKET = 489

NAME_RE = re.compile(r'^[a-z0-9_]+$')
# Words scene sections use themselves: no strip / group / scene may be named so
RESERVED = ('all', 'trigger', 'priority', 'hold')
MOTION_OFF_DELAY = 0.3    # s extruding / retracting stays on after the last
                          # sample (motion_report refreshes ~4x a second)

# esp_bridge LED nodes animate these effects themselves
# The esp_bridge LED node (statusled firmware) draws these itself, at its own
# fixed speed: solid, pulse, breathe, blink, bar, chase, comet, scan, theater,
# wipe, rainbow, twinkle, fire, off. Blink is driven from here (solid, colour
# switched at each edge) so it blinks in step with the other strips.
ESP_FX = {
    'off': 'off', 'solid': 'solid', 'breathe': 'breathe', 'blink': 'solid',
    'rainbow': 'rainbow', 'chase': 'chase', 'comet': 'comet', 'scan': 'scan',
    'twinkle': 'twinkle', 'fire': 'fire', 'progress': 'bar', 'heat': 'bar',
    'follow': 'solid',
}


def _clamp01(v):
    return 0.0 if v < 0.0 else 1.0 if v > 1.0 else v


######################################################################
# Outputs
######################################################################

class OutputError(Exception):
    pass


class Output(object):
    """Base: something that takes frames for one or more strips."""
    kind = '?'
    uses_mcu = False

    def __init__(self, light, target):
        self.light = light
        self.target = target
        self.strips = []
        self.errors = 0
        self.disabled = False
        self.fps = 0.0
        self.sent_bytes = 0
        self.dropped = 0
        self.next_due = 0.0
        self.last_error = ''

    def describe(self):
        return '%s %s' % (self.kind, self.target)

    def connect(self):
        pass

    def interval(self):
        return 1.0 / max(1.0, self.fps)

    def push(self, eventtime, frames, guard):
        """frames: {strip: list of (r,g,b)}. Returns nothing; raise on error."""
        raise NotImplementedError

    def fail(self, exc):
        self.errors += 1
        self.last_error = str(exc)
        logging.exception("lightspeed: output %s failed", self.describe())
        if self.errors >= OUTPUT_ERROR_LIMIT and not self.disabled:
            self.disabled = True
            self.light.notify("Lightspeed: output '%s' stopped after %d "
                               "errors (%s). The printer is not affected."
                               % (self.describe(), self.errors,
                                  self.last_error))

    def ok(self):
        self.errors = 0

    def close(self):
        pass


class ChainOutput(Output):
    """A Klipper neopixel/dotstar chain, updated through its update_leds()."""
    uses_mcu = True

    def __init__(self, light, kind, target):
        Output.__init__(self, light, target)
        self.kind = kind
        self.chain = None
        self.helper = None
        self.led_count = 0
        self.busy = False
        self.last_bytes = None
        self.state = None
        self.snapshot_done = None

    def connect(self):
        printer = self.light.printer
        self.chain = printer.lookup_object('%s %s' % (self.kind, self.target),
                                           None)
        if self.chain is None:
            raise printer.config_error(
                "lightspeed: strip output '%s %s' does not exist (no [%s %s] "
                "section)" % (self.kind, self.target, self.kind, self.target))
        if not hasattr(self.chain, 'update_leds'):
            raise printer.config_error(
                "lightspeed: '%s %s' has no update_leds(); this Klipper is "
                "not supported" % (self.kind, self.target))
        self.helper = getattr(self.chain, 'led_helper', None)
        status = self.chain.get_status(None)
        self.state = [tuple(c) + (0.0,) * (4 - len(c))
                      for c in status['color_data']]
        self.led_count = len(self.state)
        used = {}
        for strip in self.strips:
            if strip.leds is None:
                strip.leds = list(range(self.led_count))
            for i in strip.leds:
                if i >= self.led_count:
                    raise printer.config_error(
                        "lightspeed: strip '%s' uses LED %d but %s %s has "
                        "only %d" % (strip.name, i + 1, self.kind,
                                     self.target, self.led_count))
                if i in used:
                    raise printer.config_error(
                        "lightspeed: LED %d of %s %s is in strips '%s' and "
                        "'%s'" % (i + 1, self.kind, self.target, used[i],
                                  strip.name))
                used[i] = strip.name
            strip.count = len(strip.leds)
        self.fps = engine.mcu_fps(self.led_count * 3)

    def push(self, eventtime, frames, guard):
        if guard:
            # Frozen while homing/probing. Small chains show the guard
            # scene once per state (a few hundred microseconds).
            if (self.snapshot_done == self.light.state
                    or self.led_count * 3 > SNAPSHOT_MAX_BYTES):
                return
        else:
            self.snapshot_done = None
        if self.busy:
            self.dropped += 1
            return
        gamma = engine.GAMMA
        state = list(self.state)
        for strip in self.strips:
            frame = frames.get(strip)
            if frame is None:
                continue
            raw = engine.to_bytes(frame,
                                  self.light.brightness * strip.brightness)
            for j, i in enumerate(strip.leds):
                old_w = state[i][3]
                state[i] = (gamma[raw[j * 3]] / 255.0,
                            gamma[raw[j * 3 + 1]] / 255.0,
                            gamma[raw[j * 3 + 2]] / 255.0, old_w)
        key = tuple(state)
        if key == self.last_bytes:
            return
        self.last_bytes = key
        self.state = state
        self.busy = True
        self.snapshot_done = self.light.state if guard else None
        nbytes = self.led_count * 3
        self.light.reactor.register_callback(
            lambda et, s=state, n=nbytes: self._send(s, n))

    def _send(self, state, nbytes):
        try:
            if self.light.shutdown or self.disabled:
                return
            mutex = getattr(self.helper, 'mutex', None)
            if mutex is not None:
                with mutex:
                    self._update(state)
            else:
                self._update(state)
            self.sent_bytes += nbytes
            self.ok()
        except Exception as e:
            self.fail(e)
        finally:
            self.busy = False

    def _update(self, state):
        self.chain.update_leds(state, None)
        # Keep Klipper's own view (Mainsail colour chips, SET_LED) in step
        if self.helper is not None and hasattr(self.helper, 'led_state'):
            self.helper.led_state = state


class WledOutput(Output):
    """WLED realtime UDP (DNRGB). Never blocks the reactor."""
    kind = 'wled'

    def __init__(self, light, target):
        Output.__init__(self, light, target)
        host, _, port = target.partition(':')
        self.host = host
        self.port = int(port) if port else WLED_PORT
        self.addr = None
        self.sock = None
        self.last_frame = None
        self.last_send = 0.0
        self.fps = engine.NET_FPS

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        if re.match(r'^\d+\.\d+\.\d+\.\d+$', self.host):
            self.addr = (self.host, self.port)
        else:
            # Name lookup can block: do it off the reactor thread
            t = threading.Thread(target=self._resolve)
            t.daemon = True
            t.start()

    def _resolve(self):
        try:
            self.addr = (socket.gethostbyname(self.host), self.port)
        except Exception as e:
            self.last_error = 'cannot resolve %s: %s' % (self.host, e)
            logging.info("lightspeed: %s", self.last_error)

    def push(self, eventtime, frames, guard):
        if self.addr is None:
            return
        gamma = engine.GAMMA
        for strip in self.strips:
            frame = frames.get(strip)
            if frame is None:
                continue
            raw = engine.to_bytes(frame,
                                  self.light.brightness * strip.brightness)
            data = bytearray(gamma[b] for b in raw)
            same = data == strip.wled_last
            if same and eventtime - strip.wled_sent < WLED_KEEPALIVE:
                continue
            strip.wled_last = data
            strip.wled_sent = eventtime
            for off in range(0, strip.count, WLED_MAX_PER_PACKET):
                n = min(WLED_MAX_PER_PACKET, strip.count - off)
                first = strip.start + off
                pkt = bytearray((4, WLED_TIMEOUT, (first >> 8) & 255,
                                 first & 255))
                pkt += data[off * 3:(off + n) * 3]
                try:
                    self.sock.sendto(bytes(pkt), self.addr)
                    self.sent_bytes += len(pkt)
                except (BlockingIOError, InterruptedError):
                    self.dropped += 1
                except socket.error as e:
                    # Wi-Fi down / node off: stay silent, retry later
                    self.last_error = str(e)
                    self.dropped += 1
        self.ok()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass


class EspBridgeOutput(Output):
    """Feeds an [esp_bridge] LED node. The node animates by itself;
    Lightspeed only says which effect, colour and progress."""
    kind = 'esp_bridge'

    def __init__(self, light, target):
        Output.__init__(self, light, target)
        self.prefix = target
        self.bridge = None
        self.keys = []
        self.fps = 4.0

    def connect(self):
        printer = self.light.printer
        bridge = printer.lookup_object('esp_bridge', None)
        if bridge is None or not hasattr(bridge, 'register_producer'):
            raise printer.config_error(
                "lightspeed: strip output 'esp_bridge %s' needs an "
                "[esp_bridge] section" % (self.prefix,))
        other = printer.lookup_object('esp_led', None)
        if other is not None and getattr(other, 'prefix', None) == self.prefix:
            raise printer.config_error(
                "lightspeed: [esp_led] already drives the '%s' node. Remove "
                "[esp_led] (Lightspeed replaces it) or use another prefix."
                % (self.prefix,))
        self.bridge = bridge
        bridge.register_producer(self._produce)

    def push(self, eventtime, frames, guard):
        strip = self.strips[0]
        spec = self.light.current_specs.get(strip)
        if spec is None:
            return
        name, params = spec
        fx = ESP_FX.get(name, 'solid')
        blink = name == 'blink'
        if blink:
            # the node's own blink runs on its own clock: drive it from ours
            # (solid, colour switched at each edge) so it blinks with the
            # other strips
            c = engine.render(name, params, 1, self.light.ctx)[0]
            rgb = '%d,%d,%d' % tuple(engine.to_byte(x) for x in c)
        elif name == 'fire':
            rgb = '255,70,0'
        elif name in ('heat', 'progress'):
            # value driven (progress, heat or any <object>.<field>): the
            # colour it stands for right now
            c = engine.value_color(name, params, self.light.ctx)
            rgb = '%d,%d,%d' % tuple(engine.to_byte(x) for x in c)
        else:
            c = engine.parse_color(params.get('color', 'ffffff'))
            rgb = '%d,%d,%d' % tuple(engine.to_byte(x) for x in c)
        if name in ('progress', 'heat'):
            p = engine.source_value(params, self.light.ctx)
        else:
            p = 1.0
        b = int(round(_clamp01(self.light.brightness * strip.brightness)
                      * 255))
        p = round(_clamp01(p), 2)
        old = self.keys
        self.keys = [
            ('%s.fx' % self.prefix, fx),
            ('%s.rgb' % self.prefix, rgb),
            ('%s.p' % self.prefix, '%.2f' % p),
            ('%s.b' % self.prefix, '%d' % b),
            ('%s.st' % self.prefix, self.light.state),
        ]
        self.ok()
        if blink and old != self.keys:
            # the bridge publishes a few times a second; a blink edge goes
            # out now so the node does not lag the others
            pub = getattr(self.bridge, '_publish_pass', None)
            quiet = getattr(self.bridge, '_silenced', lambda: False)
            if pub is not None and not quiet():
                try:
                    pub(eventtime)
                except Exception:
                    logging.exception("lightspeed: esp_bridge publish failed")

    def _produce(self, eventtime):
        if self.disabled or not self.light.enabled:
            return []
        return list(self.keys)


class PinOutput(Output):
    """A single-colour light on an [output_pin] (e.g. a white case light on
    a fan/mosfet output). PWM pins dim, plain pins switch on/off. Only a
    change of the wanted value is sent, so a light switched by hand stays
    that way until the next scene change."""
    kind = 'pin'
    uses_mcu = True

    def __init__(self, light, target):
        Output.__init__(self, light, target)
        self.pin = None
        self.is_pwm = False
        self.scale = 1.
        self.last_value = None
        self.snapshot_done = None

    def connect(self):
        printer = self.light.printer
        self.pin = printer.lookup_object('output_pin %s' % (self.target,),
                                         None)
        if self.pin is None:
            raise printer.config_error(
                "lightspeed: strip output 'pin %s' does not exist (no "
                "[output_pin %s] section)" % (self.target, self.target))
        self.is_pwm = bool(getattr(self.pin, 'is_pwm', False))
        self.scale = float(getattr(self.pin, 'scale', 1.) or 1.)
        self.fps = 8.0 if self.is_pwm else 2.0

    def push(self, eventtime, frames, guard):
        if guard:
            if self.snapshot_done == self.light.state:
                return
        strip = self.strips[0]
        frame = frames.get(strip)
        if not frame:
            return
        lum = max(frame[0]) * self.light.brightness * strip.brightness
        if self.is_pwm:
            value = round(engine.GAMMA[engine.to_byte(lum)] / 255. * 20.) / 20.
        else:
            value = 1. if lum >= 0.25 else 0.
        if value == self.last_value:
            return
        self.last_value = value
        self.snapshot_done = self.light.state if guard else None
        gcrq = getattr(self.pin, 'gcrq', None)
        if gcrq is not None and hasattr(gcrq, 'send_async_request'):
            gcrq.send_async_request(value)
        else:
            script = "SET_PIN PIN=%s VALUE=%.3f" % (self.target,
                                                     value * self.scale)
            self.light.reactor.register_callback(
                lambda et: self.light.gcode.run_script(script))
        self.sent_bytes += 1
        self.ok()


######################################################################
# Strips and scenes (config sections)
######################################################################

class Strip(object):
    def __init__(self, light, config):
        self.name = config.get_name().split()[-1]
        if not NAME_RE.match(self.name):
            raise config.error("lightspeed: strip name '%s' may only use "
                               "a-z, 0-9 and _" % (self.name,))
        out = config.get('output').split()
        if len(out) != 2:
            raise config.error(
                "lightspeed strip %s: output must be '<type> <name>', e.g. "
                "'neopixel hotend_rgb', 'wled 192.168.1.60' or "
                "'esp_bridge led'" % (self.name,))
        self.out_kind, self.out_target = out[0].lower(), out[1]
        if self.out_kind not in ('neopixel', 'dotstar', 'wled', 'esp_bridge',
                                 'pin'):
            raise config.error("lightspeed strip %s: unknown output type '%s'"
                               % (self.name, self.out_kind))
        # page only: where the strip is drawn (one place or a comma list,
        # e.g. 'top_left, top_right') and the LED pitch in mm (0 = spread
        # over the whole place, otherwise centred on it)
        self.place = config.get('place', '')
        self.spacing = config.getfloat('spacing', 0., minval=0., maxval=1000.)
        # Per-strip brightness on top of [lightspeed] brightness: strips
        # differ (a bright case light next to a small status ring)
        self.brightness = config.getfloat('brightness', 1., minval=0.,
                                          maxval=1.)
        self.leds = None
        self.count = 0
        self.start = 0
        if self.out_kind in ('neopixel', 'dotstar'):
            spec = config.get('leds', None)
            count = config.getint('count', None, minval=1, maxval=4096)
            if spec is not None:
                self.leds = self._parse_leds(config, spec)
                if count is not None and count != len(self.leds):
                    raise config.error(
                        "lightspeed strip %s: count %d but leds lists %d"
                        % (self.name, count, len(self.leds)))
            elif count is not None:
                self.leds = list(range(count))
        elif self.out_kind == 'wled':
            host, _, port = self.out_target.partition(':')
            if not host or (port and not (port.isdigit()
                                          and 0 < int(port) < 65536)):
                raise config.error("lightspeed strip %s: wled output must be "
                                   "'wled <ip>' or 'wled <ip>:<port>'"
                                   % (self.name,))
            self.count = config.getint('count', minval=1, maxval=4096)
            self.start = config.getint('start', 0, minval=0, maxval=65535)
        else:
            # esp_bridge, pin: how many LEDs the page draws
            self.count = config.getint('count', 1, minval=1, maxval=4096)
        # pin: the light's own fixed colour, only used to draw it on the page
        self.color = ''
        if self.out_kind == 'pin':
            self.color = config.get('color', 'ffffff')
            try:
                engine.parse_color(self.color)
            except engine.SpecError as e:
                raise config.error("lightspeed strip %s: %s" % (self.name, e))
        self.wled_last = None
        self.wled_sent = 0.0
        self.last_frame = None
        self.output = None
        # crossfade when the scene this strip shows changes
        self.plan_key = None
        self.fade_from = None
        self.fade_since = -1e9

    def _parse_leds(self, config, spec):
        out = []
        for part in spec.split(','):
            part = part.strip()
            if not part:
                continue
            try:
                if '-' in part:
                    a, b = [int(x) for x in part.split('-', 1)]
                    step = 1 if b >= a else -1
                    out.extend(range(a - 1, b - 1 + step, step))
                else:
                    out.append(int(part) - 1)
            except ValueError:
                raise config.error("lightspeed strip %s: bad leds '%s' (use "
                                   "e.g. 1-8 or 1,3,5)" % (self.name, spec))
        if not out or min(out) < 0:
            raise config.error("lightspeed strip %s: leds are numbered from 1"
                               % (self.name,))
        return out


class CustomScene(object):
    """[lightspeed scene <name>] that is not a printer state: shown while
    its trigger is on (or forced on by LIGHTSPEED_SCENE_ON /
    SET_LED_EFFECT), on the strips it names, when no higher-priority
    active scene names them."""

    def __init__(self, name, trigger, priority, hold, items):
        self.name = name
        self.kind, self.arg = engine.parse_trigger(trigger)
        self.trigger = engine.format_trigger(self.kind, self.arg)
        self.priority = priority
        self.hold = hold              # None = not set
        self.items = items
        self.forced = False           # LIGHTSPEED_SCENE_ON / SET_LED_EFFECT
        self.forced_until = None      # None = until switched off
        self.depth = 0                # macro running (nesting)
        self.last_true = None         # trigger last seen on
        self.event_at = None          # print_cancelled: when it happened
        self.prev_level = None        # None: not sampled yet (no event at start)
        self.active = False
        self.since = 0.0
        self.ref = None               # resolved (object, [fields]) or False

    def linger(self):
        h = self.hold or 0.0
        if self.kind in ('extruding', 'retracting'):
            h += MOTION_OFF_DELAY
        return h

    def update(self, now, level):
        """level: the trigger right now (True/False). Sets self.active."""
        if self.kind == 'print_cancelled':
            # an event: the state stays 'cancelled' until the next print
            if level and self.prev_level is False:
                self.event_at = now
            hold = engine.CANCEL_HOLD if self.hold is None else self.hold
            on = bool(level and self.event_at is not None
                      and now - self.event_at < hold)
        else:
            if level:
                self.last_true = now
            on = bool(level) or (self.last_true is not None
                                 and now - self.last_true < self.linger())
        self.prev_level = bool(level)
        if self.forced and self.forced_until is not None \
                and now >= self.forced_until:
            self.forced = False
        on = on or self.forced
        if on and not self.active:
            self.since = now
        self.active = on
        return on


######################################################################
# Main object
######################################################################

class Lightspeed(object):
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self.brightness = config.getfloat('brightness', 0.6, minval=0.,
                                          maxval=1.)
        self.model = config.get('model', '')
        self.size = config.get('size', '')
        # Commands of the user's own that stream sensor data (eddy, IMU...)
        # and must not share the bus with LED updates
        self.guard_commands = [c.strip().upper() for c in
                               config.getlist('guard_commands', [])
                               if c.strip()]
        self.strips = []
        self.scenes = {}          # state -> {strip name | 'all': spec}
        self.scene_priority = {}  # state -> priority: from its section
        self.custom = {}          # name -> CustomScene (config order)
        self.groups = []          # [(name, [strip names])]
        self.compat = []          # SET_LED_EFFECT / STOP_LED_EFFECTS if ours
        self.ranges = {}          # value source -> (lo, hi) from max_temp
        self._src_ref = {}        # value source -> (object, fields) | False
        self._stc = {}            # status objects read in this tick
        self._gframes = {}        # group frames rendered in this tick
        self.state_since = 0.0
        self.pstate = 'standby'
        self.outputs = []
        self.enabled = True
        self.shutdown = False
        self.state = 'idle'
        self.auto_state = 'idle'
        self.override = None      # (state, until)
        self.preview = {}         # strip | group -> (spec, until)
        self.current_specs = {}   # strip -> (name, params)
        self.plan = {}            # strip name -> (scene, key, spec, off, n)
        self.ctx = {'t': 0.0, 'progress': 0.0, 'heat': 0.0,
                    'pos': (0.0, 0.0, 0.0), 'vars': {}, 'ranges': {}}
        self.timer = None
        self.tick_errors = 0
        self.late_strikes = []
        self.mcu_slow = 1.0
        self.mcu_slow_until = 0.0
        # guard
        self.homing_depth = 0
        self.rails_depth = 0
        self.guard_until = 0.0
        self.guard_kind = None
        self.cmd_depth = 0
        self.cmd_kind = None
        # print tracking
        self.last_print_state = None
        self.complete_since = None
        self.heat_start = {}
        self.heaters = []
        self.toolhead = None
        self._notified = set()

        self.printer.register_event_handler('klippy:connect', self._connect)
        # The page talks to this instead of G-code, so testing scenes does
        # not fill the console (Moonraker serves it as /printer/lightspeed/cmd)
        webhooks = self.printer.lookup_object('webhooks', None)
        if webhooks is not None:
            webhooks.register_endpoint('lightspeed/cmd', self._web_cmd)
        self.printer.register_event_handler('klippy:ready', self._ready)
        self.printer.register_event_handler('klippy:shutdown', self._shut)
        self.printer.register_event_handler('klippy:disconnect',
                                            self._disconnect)
        self.printer.register_event_handler('homing:homing_move_begin',
                                            self._hmove_begin)
        self.printer.register_event_handler('homing:homing_move_end',
                                            self._hmove_end)
        self.printer.register_event_handler('homing:home_rails_begin',
                                            self._rails_begin)
        self.printer.register_event_handler('homing:home_rails_end',
                                            self._rails_end)
        g = self.gcode
        g.register_command('LIGHTSPEED_STATUS', self.cmd_STATUS,
                           desc="Show what Lightspeed is showing and why")
        g.register_command('LIGHTSPEED_STATE', self.cmd_STATE,
                           desc="LIGHTSPEED_STATE STATE=<state|auto> "
                                "[HOLD=10] - show a state's scene")
        g.register_command('LIGHTSPEED_PREVIEW', self.cmd_PREVIEW,
                           desc='LIGHTSPEED_PREVIEW STRIP=<name|all> '
                                'EFFECT="<effect key=value ...>" [HOLD=30]')
        g.register_command('LIGHTSPEED_BRIGHTNESS', self.cmd_BRIGHTNESS,
                           desc="LIGHTSPEED_BRIGHTNESS VALUE=<0..1>")
        g.register_command('LIGHTSPEED_SCENE', self.cmd_SCENE,
                           desc='LIGHTSPEED_SCENE STATE=<state> STRIP=<name|all> '
                                'EFFECT="<effect key=value ...>" - change a '
                                'scene until restart (EFFECT= empty removes it)')
        g.register_command('LIGHTSPEED_STRIP', self.cmd_STRIP,
                           desc="LIGHTSPEED_STRIP STRIP=<name> BRIGHTNESS=<0..1>")
        g.register_command('LIGHTSPEED_ENABLE', self.cmd_ENABLE,
                           desc="LIGHTSPEED_ENABLE VALUE=<0|1> - stop or "
                                "start all Lightspeed output")
        g.register_command('LIGHTSPEED_SCENE_ON', self.cmd_SCENE_ON,
                           desc="LIGHTSPEED_SCENE_ON NAME=<scene> [HOLD=s] - "
                                "switch a custom scene on")
        g.register_command('LIGHTSPEED_SCENE_OFF', self.cmd_SCENE_OFF,
                           desc="LIGHTSPEED_SCENE_OFF NAME=<scene|all> - "
                                "switch a custom scene off")

    # ------------------------------------------------------------ config

    def add_strip(self, strip):
        if strip.name in RESERVED:
            raise self.printer.config_error(
                "lightspeed: a strip may not be called '%s'" % (strip.name,))
        if any(s.name == strip.name for s in self.strips):
            raise self.printer.config_error("lightspeed: strip '%s' defined "
                                            "twice" % (strip.name,))
        self.strips.append(strip)

    def add_scene(self, config):
        state = config.get_name().split()[-1].lower()
        builtin = state in engine.STATES
        if not builtin and (not NAME_RE.match(state) or state in RESERVED):
            raise config.error(
                "lightspeed: scene name '%s' may only use a-z, 0-9 and _ "
                "(printer states: %s)" % (state, ', '.join(engine.STATES)))
        opts = [o.lower() for o in config.get_prefix_options('')]
        if builtin and ('trigger' in opts or 'hold' in opts):
            raise config.error(
                "lightspeed scene %s: trigger / hold are for custom scenes "
                "only; a printer state has its own trigger" % (state,))
        if 'priority' in opts:
            pri = config.getint('priority', engine.DEFAULT_PRIORITY)
        else:
            pri = None
        items = {}
        for opt in opts:
            if opt in ('trigger', 'priority', 'hold'):
                continue
            raw = config.get(opt)
            try:
                items[opt] = engine.parse_spec(raw)
            except engine.SpecError as e:
                raise config.error("lightspeed scene %s, %s: %s"
                                   % (state, opt, e))
        if builtin:
            self.scenes[state] = items
            if pri is not None:
                self.scene_priority[state] = pri
            return
        trigger = config.get('trigger', 'command')
        hold = config.getfloat('hold', None, minval=0.) \
            if 'hold' in opts else None
        try:
            sc = CustomScene(state, trigger, engine.DEFAULT_PRIORITY
                             if pri is None else pri, hold, items)
            if pri is None and sc.kind == 'command':
                sc.priority = engine.COMMAND_PRIORITY
            self.custom[state] = sc
        except engine.SpecError as e:
            raise config.error("lightspeed scene %s, trigger: %s"
                               % (state, e))

    def add_group(self, config):
        name = config.get_name().split()[-1].lower()
        if not NAME_RE.match(name) or name in RESERVED:
            raise config.error("lightspeed: group name '%s' may only use "
                               "a-z, 0-9 and _" % (name,))
        if any(g[0] == name for g in self.groups):
            raise config.error("lightspeed: group '%s' defined twice"
                               % (name,))
        members = [m.strip().lower() for m in config.getlist('strips')
                   if m.strip()]
        if not members or len(set(members)) != len(members):
            raise config.error("lightspeed group %s: strips must list "
                               "strips, each once" % (name,))
        self.groups.append((name, members))

    def _connect(self):
        names = set(s.name for s in self.strips)
        gnames = set(g[0] for g in self.groups)
        for g, members in self.groups:
            if g in names:
                raise self.printer.config_error(
                    "lightspeed group %s: a strip has that name too" % (g,))
            for m in members:
                if m not in names:
                    raise self.printer.config_error(
                        "lightspeed group %s: no strip named '%s'" % (g, m))
        every = list(self.scenes.items()) + [
            (c.name, c.items) for c in self.custom.values()]
        for state, items in every:
            for key in items:
                if key != 'all' and key not in names and key not in gnames:
                    raise self.printer.config_error(
                        "lightspeed scene %s: no strip or group named '%s' "
                        "(strips: %s)" % (state, key, ', '.join(sorted(names))
                                          or 'none'))
        # only the printer-state scenes decide whether the defaults apply
        if not self.scenes:
            self.scenes = dict(
                (st, {'all': engine.parse_spec(spec)})
                for st, spec in engine.DEFAULT_SCENES.items())
        by_target = {}
        for strip in self.strips:
            key = (strip.out_kind, strip.out_target)
            out = by_target.get(key)
            if out is None:
                if strip.out_kind in ('neopixel', 'dotstar'):
                    out = ChainOutput(self, strip.out_kind, strip.out_target)
                elif strip.out_kind == 'wled':
                    out = WledOutput(self, strip.out_target)
                elif strip.out_kind == 'pin':
                    out = PinOutput(self, strip.out_target)
                else:
                    out = EspBridgeOutput(self, strip.out_target)
                by_target[key] = out
                self.outputs.append(out)
            elif strip.out_kind in ('esp_bridge', 'pin'):
                raise self.printer.config_error(
                    "lightspeed: only one strip can use '%s %s'"
                    % (strip.out_kind, strip.out_target))
            out.strips.append(strip)
            strip.output = out
        for out in self.outputs:
            out.connect()
        try:
            self.toolhead = self.printer.lookup_object('toolhead')
        except Exception:
            self.toolhead = None
        pheaters = self.printer.lookup_object('heaters', None)
        if pheaters is not None:
            try:
                names = pheaters.get_status(None)['available_heaters']
            except Exception:
                names = []
            self.heaters = [n for n in names
                            if n == 'heater_bed' or n.startswith('extruder')]
        self._register_compat()
        for sc in self.custom.values():
            if sc.kind == 'condition':
                ref = self._resolve_ref(sc.arg[0])
                if ref is False:
                    self.notify("Lightspeed: scene '%s': no printer object "
                                "for '%s'; its trigger stays off"
                                % (sc.name, sc.arg[0]))

    # ------------------------------------------------- led_effect compat

    def _led_effect_loaded(self):
        p = self.printer
        lo = getattr(p, 'lookup_objects', None)
        if lo is not None:
            try:
                if lo('led_effect'):
                    return True
            except Exception:
                pass
        for name in ('led_effect', 'ledFrameHandler'):
            if p.lookup_object(name, None) is not None:
                return True
        objs = getattr(p, 'objects', None)
        if isinstance(objs, dict):
            return any(k == 'led_effect' or k.startswith('led_effect ')
                       for k in objs)
        return False

    def _register_compat(self):
        """Without a led_effect module, its two commands switch Lightspeed
        scenes, so existing macros keep working unchanged."""
        if self.compat or self._led_effect_loaded():
            return
        for name, fn, desc in (
                ('SET_LED_EFFECT', self.cmd_SET_LED_EFFECT,
                 "SET_LED_EFFECT EFFECT=<scene> [STOP=1] - Lightspeed: "
                 "switch a custom scene on (led_effect compatible)"),
                ('STOP_LED_EFFECTS', self.cmd_STOP_LED_EFFECTS,
                 "STOP_LED_EFFECTS [EFFECT=<scene>] - Lightspeed: switch "
                 "custom scenes off")):
            try:
                self.gcode.register_command(name, fn, desc=desc)
                self.compat.append(name)
            except Exception:
                logging.info("lightspeed: %s is taken, not registered", name)

    def _ready(self):
        self.shutdown = False
        self._wrap_guarded_commands()
        self._wrap_scene_macros()
        if self.timer is None:
            self.timer = self.reactor.register_timer(self._tick,
                                                     self.reactor.NOW)

    def _wrap_guarded_commands(self):
        g = self.gcode
        if getattr(g, '_lightspeed_wrapped', None) is None:
            g._lightspeed_wrapped = set()
        commands = dict(GUARDED_COMMANDS)
        for name in self.guard_commands:
            commands.setdefault(name, None)
        for name, kind in commands.items():
            if name in g._lightspeed_wrapped:
                continue
            old = None
            try:
                old = g.register_command(name, None)
                if old is None:
                    continue
                desc = getattr(g, 'gcode_help', {}).get(name)

                def wrapper(gcmd, old=old, kind=kind):
                    self._cmd_begin(kind)
                    try:
                        old(gcmd)
                    finally:
                        self._cmd_end()
                g.register_command(name, wrapper, desc=desc)
                g._lightspeed_wrapped.add(name)
            except Exception:
                # Never lose the command: put the original back
                logging.exception("lightspeed: could not guard %s", name)
                if old is not None:
                    try:
                        g.register_command(name, None)
                        g.register_command(name, old, desc=desc)
                    except Exception:
                        logging.exception("lightspeed: restoring %s", name)

    def _wrap_scene_macros(self):
        """'trigger: macro NAME': the scene is on while NAME runs. Wrapped
        once (its own set, so a guarded command can be a trigger too); the
        command always runs and always ends the scene, even if it fails."""
        g = self.gcode
        if getattr(g, '_lightspeed_scene_wrapped', None) is None:
            g._lightspeed_scene_wrapped = set()
        by_macro = {}
        for sc in self.custom.values():
            if sc.kind == 'macro':
                by_macro.setdefault(sc.arg, []).append(sc)
        for name, scs in by_macro.items():
            if name in g._lightspeed_scene_wrapped:
                continue
            old = None
            desc = None
            try:
                old = g.register_command(name, None)
                if old is None:
                    self.notify("Lightspeed: scene '%s': no G-code command "
                                "'%s'; its trigger stays off"
                                % (scs[0].name, name))
                    continue
                desc = getattr(g, 'gcode_help', {}).get(name)

                def wrapper(gcmd, old=old, scs=scs):
                    self._macro_begin(scs)
                    try:
                        old(gcmd)
                    finally:
                        self._macro_end(scs)
                g.register_command(name, wrapper, desc=desc)
                g._lightspeed_scene_wrapped.add(name)
            except Exception:
                logging.exception("lightspeed: could not watch %s", name)
                if old is not None:
                    try:
                        g.register_command(name, None)
                        g.register_command(name, old, desc=desc)
                    except Exception:
                        logging.exception("lightspeed: restoring %s", name)

    def _macro_begin(self, scs):
        try:
            for sc in scs:
                sc.depth += 1
            self._kick()
        except Exception:
            logging.exception("lightspeed: macro scene begin")

    def _macro_end(self, scs):
        try:
            now = self.reactor.monotonic()
            for sc in scs:
                sc.depth = max(0, sc.depth - 1)
                sc.last_true = now
            self._kick()
        except Exception:
            logging.exception("lightspeed: macro scene end")

    def _shut(self):
        self.shutdown = True
        for out in self.outputs:
            if out.uses_mcu:
                out.busy = False

    def _disconnect(self):
        for out in self.outputs:
            out.close()

    def notify(self, msg):
        logging.info(msg)
        if msg in self._notified:
            return
        self._notified.add(msg)
        try:
            self.gcode.respond_info(msg)
        except Exception:
            pass

    # ------------------------------------------------------------ guard

    def _hmove_begin(self, hmove):
        self.homing_depth += 1

    def _hmove_end(self, hmove):
        self.homing_depth = max(0, self.homing_depth - 1)
        if not self.rails_depth and not self.cmd_depth:
            self.guard_kind = 'probing'
        self.guard_until = self.reactor.monotonic() + GUARD_HOLD

    def _rails_begin(self, homing_state, rails):
        self.rails_depth += 1

    def _rails_end(self, homing_state, rails):
        self.rails_depth = max(0, self.rails_depth - 1)
        self.guard_kind = 'homing'
        self.guard_until = self.reactor.monotonic() + GUARD_HOLD

    def _cmd_begin(self, kind):
        self.cmd_depth += 1
        if self.cmd_depth == 1:
            self.cmd_kind = kind

    def _cmd_end(self):
        self.cmd_depth = max(0, self.cmd_depth - 1)
        self.guard_kind = self.cmd_kind
        self.guard_until = self.reactor.monotonic() + GUARD_HOLD

    def _guard(self, now):
        """(active, state to show or None)"""
        if self.rails_depth:
            return True, 'homing'
        if self.cmd_depth:
            return True, self.cmd_kind
        if self.homing_depth:
            return True, 'probing'
        if now < self.guard_until:
            return True, self.guard_kind
        return False, None

    # ------------------------------------------------------------ state

    def _status(self, name, eventtime):
        st = self._status_or_none(name, eventtime)
        return {} if st is None else st

    def _status_or_none(self, name, eventtime):
        """An object's status, read once per tick; None if no object."""
        if name in self._stc:
            return self._stc[name]
        obj = self.printer.lookup_object(name, None)
        st = None
        if obj is not None:
            try:
                st = obj.get_status(eventtime)
                if not isinstance(st, dict):
                    st = {}
            except Exception:
                st = {}
        self._stc[name] = st
        return st

    def _resolve_ref(self, ref):
        """'temperature_sensor chamber.temperature' -> (object, [fields]);
        the longest prefix that is an object wins (nested fields allowed).
        False when no object matches."""
        if ref in self._src_ref:
            return self._src_ref[ref]
        if ref == 'print_stats.progress':
            res = ('display_status', ['progress'])
        else:
            parts = ref.split('.')
            res = False
            for i in range(len(parts), 0, -1):
                name = '.'.join(parts[:i])
                if self.printer.lookup_object(name, None) is not None:
                    res = (name, parts[i:])
                    break
        self._src_ref[ref] = res
        if res and res[1] and res[1][-1] in ('temperature', 'target'):
            top = self._max_temp(res[0])
            if top is not None and top > 20.:
                self.ranges[ref] = (20.0, float(top))
        return res

    def _max_temp(self, name):
        obj = self.printer.lookup_object(name, None)
        for get in (lambda o: o.max_temp, lambda o: o.heater.max_temp,
                    lambda o: o.get_heater().max_temp):
            try:
                v = get(obj)
                if isinstance(v, (int, float)):
                    return v
            except Exception:
                pass
        return None

    def _read(self, ref, eventtime):
        """Number behind '<object>[.<field>...]', or None."""
        res = self._resolve_ref(ref)
        if not res:
            return None
        v = self._status_or_none(res[0], eventtime)
        if v is None:
            return None
        fields = res[1]
        if not fields:
            fields = [f for f in ('temperature', 'speed', 'value', 'progress')
                      if f in v][:1]
        for f in fields:
            if isinstance(v, dict):
                v = v.get(f)
            elif isinstance(v, (list, tuple)) and f.isdigit() \
                    and int(f) < len(v):
                v = v[int(f)]
            else:
                return None
        return engine._num(v)

    def _heat(self, eventtime):
        """(heating?, 0..1 progress towards target)"""
        heating = False
        fracs = []
        for name in self.heaters:
            st = self._status(name, eventtime)
            target = st.get('target', 0.0) or 0.0
            temp = st.get('temperature', 0.0) or 0.0
            if target <= 0.0:
                self.heat_start.pop(name, None)
                continue
            start = self.heat_start.get(name)
            if start is None or start[1] != target:
                start = (min(temp, target), target)
                self.heat_start[name] = start
            span = target - start[0]
            f = 1.0 if span <= HEAT_MARGIN else (temp - start[0]) / span
            fracs.append(_clamp01(f))
            if temp < target - HEAT_MARGIN:
                heating = True
        return heating, (min(fracs) if fracs else 0.0)

    def _decide(self, eventtime, guard_state):
        if self.shutdown:
            return 'shutdown'
        klippy = self._status('webhooks', eventtime).get('state', 'ready')
        ps = self._status('print_stats', eventtime)
        pstate = ps.get('state', 'standby')
        self.pstate = pstate
        if pstate == 'complete' and self.last_print_state != 'complete':
            self.complete_since = eventtime
        elif pstate != 'complete':
            self.complete_since = None
        self.last_print_state = pstate
        heating, heat = self._heat(eventtime)
        self.ctx['heat'] = heat
        p = self._status('display_status', eventtime).get('progress')
        if p is None:
            p = self._status('virtual_sdcard', eventtime).get('progress', 0.)
        self.ctx['progress'] = _clamp01(float(p or 0.0))
        if self.toolhead is not None:
            th = self.toolhead.get_status(eventtime)
            pos = th.get('position')
            amin, amax = th.get('axis_minimum'), th.get('axis_maximum')
            if pos is not None and amin is not None and amax is not None:
                self.ctx['pos'] = tuple(
                    _clamp01((pos[i] - amin[i]) / max(1e-6, amax[i] - amin[i]))
                    for i in range(3))
        if klippy == 'shutdown':
            return 'shutdown'
        if klippy != 'ready' or pstate == 'error':
            return 'error'
        if guard_state:
            return guard_state
        if pstate == 'paused':
            return 'paused'
        if heating:
            return 'heating'
        if pstate == 'printing':
            return 'printing'
        if self.complete_since is not None and \
                eventtime - self.complete_since < COMPLETE_HOLD:
            return 'complete'
        return 'idle'

    def _trigger_level(self, sc, eventtime):
        k = sc.kind
        if k == 'command':
            return False
        if k == 'macro':
            return sc.depth > 0
        if k == 'print_cancelled':
            return self.pstate == 'cancelled'
        if k in ('extruding', 'retracting'):
            v = self._read('motion_report.live_extruder_velocity', eventtime)
            if v is None:
                return False
            return v > engine.MOTION_SPEED if k == 'extruding' \
                else v < -engine.MOTION_SPEED
        ref, op, num = sc.arg
        v = self._read(ref, eventtime)
        if v is None:
            if self._resolve_ref(ref):
                self.notify("Lightspeed: scene '%s': '%s' is not a number on "
                            "this printer; its trigger stays off"
                            % (sc.name, ref))
            return False
        return engine.compare(v, op, num)

    def _update_triggers(self, eventtime):
        for sc in self.custom.values():
            try:
                level = self._trigger_level(sc, eventtime)
            except Exception:
                logging.exception("lightspeed: trigger of scene %s",
                                  sc.name)
                level = False
            sc.update(eventtime, level)

    def _make_plan(self, state, eventtime, overridden):
        scene = engine.resolve_scene(state, self.scenes)
        items = self.scenes.get(scene, {}) if scene else {}
        pri = self.scene_priority.get(state,
                                      engine.BUILTIN_PRIORITY.get(state, 0))
        cands = [(state, pri, self.state_since, items)]
        if not overridden:   # a state test (LIGHTSPEED_STATE) shows only it
            for sc in self.custom.values():
                if sc.active:
                    cands.append((sc.name, sc.priority, sc.since, sc.items))
        strips = [(s.name, s.count) for s in self.strips]
        plan = engine.plan_strips(strips, self.groups, cands)
        # previews on top (a strip, a group or all), as before
        pv = dict((k, v[0]) for k, v in self.preview.items()
                  if eventtime < v[1])
        if pv:
            top = engine.plan_strips(strips, self.groups,
                                     [('__preview__', 0, 0., pv)])
            for name, hit in top.items():
                if hit[0] is not None:
                    plan[name] = hit
        return plan

    def _frame(self, strip, hit):
        scene, key, spec, off, total = hit
        if spec is None:
            spec = ('off', {'reverse': 0})
        if off == 0 and total == strip.count:
            return engine.render(spec[0], spec[1], strip.count, self.ctx)
        gk = (scene, key)
        whole = self._gframes.get(gk)
        if whole is None:
            whole = engine.render(spec[0], spec[1], total, self.ctx)
            self._gframes[gk] = whole
        return whole[off:off + strip.count]

    def _gather_vars(self, eventtime):
        vs = {}
        for hit in self.plan.values():
            spec = hit[2]
            if spec is None or spec[0] not in ('progress', 'heat'):
                continue
            src = spec[1].get('source', 'progress')
            if src not in ('progress', 'heat') and src not in vs:
                vs[src] = self._read(src, eventtime)
        self.ctx['vars'] = vs
        self.ctx['ranges'] = self.ranges

    # ------------------------------------------------------------ tick

    def _tick(self, eventtime):
        try:
            nxt = self._tick_inner(eventtime)
            self.tick_errors = 0
            return nxt
        except Exception:
            self.tick_errors += 1
            logging.exception("lightspeed: frame failed")
            if self.tick_errors >= TICK_ERROR_LIMIT:
                self.enabled = False
                self.notify("Lightspeed stopped after repeated errors (see "
                            "klippy.log). The printer is not affected; "
                            "LIGHTSPEED_ENABLE VALUE=1 tries again.")
                return self.reactor.NEVER
            return eventtime + 1.0

    def _tick_inner(self, eventtime):
        # Running late means the host is busy: slow MCU outputs down
        if getattr(self, '_expected', None) is not None:
            late = eventtime - self._expected
            if late > LATE_LIMIT:
                self.late_strikes = [t for t in self.late_strikes
                                     if eventtime - t < 10.0] + [eventtime]
                if len(self.late_strikes) >= 3:
                    self.mcu_slow = max(0.125, self.mcu_slow * 0.5)
                    self.mcu_slow_until = eventtime + 60.0
                    self.late_strikes = []
                    logging.info("lightspeed: host busy, MCU LED rate x%.3f",
                                 self.mcu_slow)
        if self.mcu_slow < 1.0 and eventtime > self.mcu_slow_until:
            self.mcu_slow = 1.0

        if not self.enabled:
            self._expected = None
            return eventtime + 0.5

        self._stc = {}
        self._gframes = {}
        guard, guard_state = self._guard(eventtime)
        auto = self._decide(eventtime, guard_state)
        self.auto_state = auto
        state = auto
        self._update_triggers(eventtime)
        overridden = False
        if self.override is not None:
            if eventtime < self.override[1]:
                state = self.override[0]
                overridden = True
                if len(self.override) > 3 and self.override[3] > 0.:
                    f = (eventtime - self.override[2]) / self.override[3]
                    if state == 'heating':
                        self.ctx['heat'] = _clamp01(f / 0.8)
                    elif state == 'printing':
                        self.ctx['progress'] = _clamp01(f)
                    elif state == 'complete':
                        self.ctx['progress'] = 1.0
            else:
                self.override = None
        for k in [k for k, v in self.preview.items() if eventtime >= v[1]]:
            del self.preview[k]
        if state != self.state:
            self.state_since = eventtime
            self.state = state
        self.plan = self._make_plan(state, eventtime, overridden)
        for strip in self.strips:
            hit = self.plan.get(strip.name)
            if hit is None or hit[0] == '__preview__':
                continue      # a preview never starts a crossfade (as before)
            key = (hit[0], hit[1])
            if key != strip.plan_key:
                strip.fade_from = strip.last_frame
                strip.fade_since = eventtime
                strip.plan_key = key
        self._gather_vars(eventtime)
        self.ctx['t'] = eventtime

        soonest = eventtime + 0.5
        for out in self.outputs:
            if out.disabled:
                continue
            if out.uses_mcu and self.shutdown:
                continue
            if eventtime < out.next_due:
                soonest = min(soonest, out.next_due)
                continue
            frames = {}
            for strip in out.strips:
                hit = self.plan.get(strip.name) or (None, None, None, 0,
                                                    strip.count)
                spec = hit[2] or ('off', {'reverse': 0})
                self.current_specs[strip] = spec
                if out.kind == 'esp_bridge':
                    continue
                frame = self._frame(strip, hit)
                fade = _clamp01((eventtime - strip.fade_since) / CROSSFADE)
                if fade < 1.0 and not (guard and out.uses_mcu):
                    frame = engine.crossfade(strip.fade_from, frame, fade)
                strip.last_frame = frame
                frames[strip] = frame
            try:
                out.push(eventtime, frames, guard and out.uses_mcu)
            except Exception as e:
                out.fail(e)
            interval = out.interval()
            if out.uses_mcu:
                interval /= self.mcu_slow
            out.next_due = eventtime + interval
            # A blink must switch on time even on slow outputs (a pin runs
            # at 2 fps): also wake at the strip's next on/off edge. Effects
            # are pure functions of time, so that frame is exactly right.
            for strip in out.strips:
                spec = self.current_specs.get(strip)
                if spec is None or spec[0] != 'blink':
                    continue
                half = max(0.05, spec[1].get('period', 1.0)) / 2.0
                edge = (math.floor(eventtime / half) + 1.0) * half + 0.002
                out.next_due = min(out.next_due, edge)
            soonest = min(soonest, out.next_due)
        self._expected = soonest
        return soonest

    # ------------------------------------------------------------ status

    def get_status(self, eventtime):
        # Moonraker polls this while Klipper is still starting; an exception
        # here shuts the printer down, so it must never raise.
        try:
            return self._status_dict(eventtime)
        except Exception:
            logging.exception("lightspeed: status failed")
            return {'version': __version__, 'enabled': self.enabled,
                    'state': self.state, 'auto_state': self.auto_state,
                    'strips': {}}

    def _status_dict(self, eventtime):
        strips = {}
        for s in self.strips:
            out = getattr(s, 'output', None)
            if out is None:       # before klippy:connect wired the outputs
                continue
            spec = self.current_specs.get(s)
            strips[s.name] = {
                'output': out.describe(), 'kind': out.kind,
                'count': s.count, 'place': s.place, 'color': s.color,
                'spacing': s.spacing,
                'effect': engine.format_spec(*spec) if spec else '',
                'fps': round(out.fps * (self.mcu_slow if out.uses_mcu
                                        else 1.0), 1),
                'bytes_sent': out.sent_bytes, 'dropped': out.dropped,
                'errors': out.errors, 'disabled': out.disabled,
                'last_error': out.last_error,
                # seconds a LIGHTSPEED_PREVIEW still holds this strip
                'manual': int(max(0., (self.preview.get(s.name) or
                                       (None, 0.))[1] - eventtime)),
            }
        guard, _ = self._guard(eventtime)
        active = sorted((c for c in self.custom.values() if c.active),
                        key=lambda c: (-c.priority, -c.since))
        return {
            'version': __version__, 'enabled': self.enabled,
            'state': self.state, 'auto_state': self.auto_state,
            'override': self.override is not None, 'guard': guard,
            'brightness': self.brightness, 'model': self.model,
            'size': self.size, 'mcu_rate': self.mcu_slow, 'strips': strips,
            'scenes_active': [c.name for c in active],
            'custom_scenes': [
                {'name': c.name, 'trigger': c.trigger,
                 'priority': c.priority, 'hold': c.hold,
                 'active': c.active, 'forced': c.forced}
                for c in self.custom.values()],
            'groups': dict((g, list(m)) for g, m in self.groups),
            'ranges': dict((k, list(v)) for k, v in self.ranges.items()),
            'compat': list(self.compat),
        }

    # ------------------------------------------------------------ gcode

    def cmd_STATUS(self, gcmd):
        et = self.reactor.monotonic()
        st = self.get_status(et)
        lines = ["Lightspeed %s: %s, state %s%s%s"
                 % (__version__, 'on' if self.enabled else 'OFF', self.state,
                    ' (held)' if st['override'] else '',
                    ', MCU LEDs frozen (homing/probing)' if st['guard']
                    else '')]
        if self.mcu_slow < 1.0:
            lines.append("  host busy: MCU LED rate x%.3f" % self.mcu_slow)
        for name, s in sorted(st['strips'].items()):
            extra = ''
            if s['disabled']:
                extra = '  STOPPED: %s' % s['last_error']
            elif s['errors']:
                extra = '  errors: %d' % s['errors']
            lines.append("  %s (%s, %d LEDs, %.0f fps): %s%s"
                         % (name, s['output'], s['count'], s['fps'],
                            s['effect'], extra))
        gcmd.respond_info('\n'.join(lines))

    class ActionError(Exception):
        pass

    def _strip_named(self, name, allow_all=True):
        if allow_all and name == 'all':
            return None
        for s in self.strips:
            if s.name == name:
                return s
        if allow_all and any(g[0] == name for g in self.groups):
            return None
        raise self.ActionError("no strip '%s'" % (name,))

    def _scene_items(self, state):
        """The editable items of a printer-state or custom scene."""
        if state in self.custom:
            return self.custom[state].items
        if state not in engine.STATES:
            raise self.ActionError(
                "unknown scene '%s' (a state: %s, or a custom scene: %s)"
                % (state, ', '.join(engine.STATES),
                   ', '.join(self.custom) or 'none'))
        return self.scenes.setdefault(state, {})

    def _do_scene_on(self, name, hold):
        if name in engine.STATES:
            raise self.ActionError("'%s' is a printer state: use "
                                   "LIGHTSPEED_STATE" % (name,))
        sc = self.custom.get(name)
        if sc is None:
            raise self.ActionError("no custom scene '%s' (scenes: %s)"
                                   % (name, ', '.join(self.custom) or 'none'))
        now = self.reactor.monotonic()
        if (hold is None or hold <= 0.) and sc.kind == 'command' and sc.hold:
            hold = sc.hold      # a command scene with hold: is an event
        sc.forced = True
        sc.forced_until = now + min(hold, 86400.) if hold and hold > 0. \
            else None
        sc.since = now          # switched on again: newest of equals
        sc.active = True
        self._kick()
        return sc

    def _do_scene_off(self, name):
        if name == 'all':
            for sc in self.custom.values():
                sc.forced = False
        elif name in self.custom:
            self.custom[name].forced = False
        else:
            raise self.ActionError("no custom scene '%s'" % (name,))
        self._kick()

    def _do_state(self, state, hold):
        if state == 'auto' or hold <= 0.:
            self.override = None
            self._kick()
            return None
        if state not in engine.STATES:
            raise self.ActionError("unknown state '%s' (one of %s, auto)"
                                   % (state, ', '.join(engine.STATES)))
        now = self.reactor.monotonic()
        # (state, until, started, length): a test of heating/printing ramps
        # heat/progress over the test so progress strips visibly fill
        self.override = (state, now + min(hold, 86400.), now, min(hold, 86400.))
        self._kick()
        return state

    def _do_preview(self, name, text, hold):
        self._strip_named(name)
        if hold <= 0.:
            self.preview.pop(name, None)
            self._kick()
            return
        try:
            spec = engine.parse_spec(text)
        except engine.SpecError as e:
            raise self.ActionError(str(e))
        self.preview[name] = (spec, self.reactor.monotonic() + min(hold, 86400.))
        self._kick()

    def _do_scene(self, state, name, text):
        items = self._scene_items(state)
        self._strip_named(name)
        if not text:
            items.pop(name, None)
            if not items and state in self.scenes:
                del self.scenes[state]
        else:
            try:
                items[name] = engine.parse_spec(text)
            except engine.SpecError as e:
                raise self.ActionError(str(e))
        self._kick()

    def _do_strip(self, name, brightness):
        strip = self._strip_named(name, allow_all=False)
        strip.brightness = max(0., min(1., brightness))
        self._kick()

    def _do_brightness(self, value):
        self.brightness = max(0., min(1., value))
        self._kick()

    def _web_cmd(self, web_request):
        """POST /printer/lightspeed/cmd {"action": ..., ...} - silent."""
        action = web_request.get_str('action')
        try:
            if action == 'state':
                self._do_state(web_request.get_str('state').lower(),
                               web_request.get_float('hold', 10.))
            elif action == 'preview':
                self._do_preview(web_request.get_str('strip').lower(),
                                 web_request.get_str('effect', ''),
                                 web_request.get_float('hold', 30.))
            elif action == 'scene':
                self._do_scene(web_request.get_str('state').lower(),
                               web_request.get_str('strip').lower(),
                               web_request.get_str('effect', '').strip())
            elif action == 'strip':
                self._do_strip(web_request.get_str('strip').lower(),
                               web_request.get_float('brightness'))
            elif action == 'brightness':
                self._do_brightness(web_request.get_float('value'))
            elif action == 'scene_on':
                self._do_scene_on(web_request.get_str('name').lower(),
                                  web_request.get_float('hold', 0.))
            elif action == 'scene_off':
                self._do_scene_off(web_request.get_str('name').lower())
            elif action == 'batch':
                # several actions in one request (a state test touches every
                # strip). One bad item (e.g. a strip the page has but this
                # config not yet) must not stop the rest, or the end of a
                # test would leave strips stuck: run all, report the first
                first = None
                for item in web_request.get('actions', []):
                    try:
                        self._web_item(item)
                    except self.ActionError as e:
                        if first is None:
                            first = e
                if first is not None:
                    raise first
            else:
                raise self.ActionError("unknown action '%s'" % (action,))
        except self.ActionError as e:
            raise web_request.error("Lightspeed: %s" % (e,))
        except self.printer.command_error:
            raise
        except Exception as e:
            # anything else in a web endpoint would shut Klipper down
            logging.exception("lightspeed: web command failed")
            raise web_request.error("Lightspeed: %s" % (e,))
        web_request.send({'ok': True})

    def _web_item(self, item):
        a = str(item.get('action', ''))
        g = lambda k, d=None: item.get(k, d)
        if a == 'state':
            self._do_state(str(g('state', 'auto')).lower(), float(g('hold', 10.)))
        elif a == 'preview':
            self._do_preview(str(g('strip', '')).lower(), str(g('effect', '')),
                             float(g('hold', 30.)))
        elif a == 'scene':
            self._do_scene(str(g('state', '')).lower(), str(g('strip', '')).lower(),
                           str(g('effect', '')).strip())
        elif a == 'strip':
            self._do_strip(str(g('strip', '')).lower(), float(g('brightness', 1.)))
        elif a == 'brightness':
            self._do_brightness(float(g('value', 1.)))
        elif a == 'scene_on':
            self._do_scene_on(str(g('name', '')).lower(), float(g('hold', 0.)))
        elif a == 'scene_off':
            self._do_scene_off(str(g('name', '')).lower())
        else:
            raise self.ActionError("unknown action '%s'" % (a,))

    def cmd_STATE(self, gcmd):
        state = gcmd.get('STATE').lower()
        hold = gcmd.get_float('HOLD', 10., minval=0., maxval=86400.)
        try:
            shown = self._do_state(state, hold)
        except self.ActionError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))
        if shown is None:
            gcmd.respond_info("Lightspeed: back to automatic")
        else:
            gcmd.respond_info("Lightspeed: showing '%s' for %.0f s"
                              % (shown, hold))

    def cmd_PREVIEW(self, gcmd):
        name = gcmd.get('STRIP').lower()
        try:
            self._strip_named(name)
        except self.ActionError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))
        # Up to a day: the page's light buttons hold a colour "until Auto"
        hold = gcmd.get_float('HOLD', 30., minval=0., maxval=86400.)
        if hold == 0.:
            self.preview.pop(name, None)
            self._kick()
            return
        try:
            spec = engine.parse_spec(gcmd.get('EFFECT'))
        except engine.SpecError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))
        self.preview[name] = (spec, self.reactor.monotonic() + hold)
        self._kick()

    def cmd_SCENE(self, gcmd):
        # The page saves the same change into lightspeed.cfg, so it also
        # survives a restart; this makes it visible right away.
        try:
            self._do_scene(gcmd.get('STATE').lower(), gcmd.get('STRIP').lower(),
                           gcmd.get('EFFECT', '').strip())
        except self.ActionError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))

    def cmd_SCENE_ON(self, gcmd):
        hold = gcmd.get_float('HOLD', 0., minval=0., maxval=86400.)
        try:
            sc = self._do_scene_on(gcmd.get('NAME').lower(), hold)
        except self.ActionError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))
        gcmd.respond_info("Lightspeed: scene '%s' on%s" % (
            sc.name, ' for %.0f s' % (sc.forced_until - self.reactor.monotonic())
            if sc.forced_until else ''))

    def cmd_SCENE_OFF(self, gcmd):
        try:
            self._do_scene_off(gcmd.get('NAME', 'all').lower())
        except self.ActionError as e:
            raise gcmd.error("Lightspeed: %s" % (e,))

    def cmd_SET_LED_EFFECT(self, gcmd):
        # led_effect compatible: EFFECT=<scene> [STOP=1]; FADETIME, REPLACE,
        # RESTART, LEDS... are accepted and ignored. Never an error: a
        # macro written for led_effect must keep running.
        name = str(gcmd.get('EFFECT', '')).strip().lower()
        try:
            stop = int(float(gcmd.get('STOP', '0') or 0))
        except (TypeError, ValueError):
            stop = 0
        if name not in self.custom:
            gcmd.respond_info("Lightspeed: no scene '%s' for SET_LED_EFFECT "
                              "(custom scenes: %s)"
                              % (name, ', '.join(self.custom) or 'none'))
            return
        if stop:
            self._do_scene_off(name)
        else:
            self._do_scene_on(name, None)

    def cmd_STOP_LED_EFFECTS(self, gcmd):
        name = str(gcmd.get('EFFECT', '') or '').strip().lower()
        if name and name not in self.custom:
            gcmd.respond_info("Lightspeed: no scene '%s' for STOP_LED_EFFECTS"
                              % (name,))
            return
        self._do_scene_off(name or 'all')

    def cmd_STRIP(self, gcmd):
        name = gcmd.get('STRIP').lower()
        strip = None
        for s in self.strips:
            if s.name == name:
                strip = s
        if strip is None:
            raise gcmd.error("Lightspeed: no strip '%s'" % (name,))
        strip.brightness = gcmd.get_float('BRIGHTNESS', strip.brightness,
                                          minval=0., maxval=1.)
        self._kick()

    def cmd_BRIGHTNESS(self, gcmd):
        self.brightness = gcmd.get_float('VALUE', minval=0., maxval=1.)
        self._kick()

    def cmd_ENABLE(self, gcmd):
        on = gcmd.get_int('VALUE', 1, minval=0, maxval=1)
        if on:
            self.enabled = True
            self.tick_errors = 0
            for out in self.outputs:
                out.disabled = False
                out.errors = 0
            self._notified.clear()
            if self.timer is not None:
                self._kick()
            gcmd.respond_info("Lightspeed: on")
        else:
            # Show black once, then go quiet
            self.preview['all'] = (('off', {'reverse': 0}),
                                   self.reactor.monotonic() + 2.0)
            self._kick()
            self.reactor.register_callback(
                lambda et: setattr(self, 'enabled', False),
                self.reactor.monotonic() + 1.0)
            gcmd.respond_info("Lightspeed: off")

    def _kick(self):
        for out in self.outputs:
            out.next_due = 0.0
            if isinstance(out, ChainOutput):
                out.last_bytes = None
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NOW)


def load_config(config):
    return Lightspeed(config)


def load_config_prefix(config):
    light = config.get_printer().load_object(config, 'lightspeed')
    parts = config.get_name().split()
    if len(parts) != 3 or parts[1] not in ('strip', 'scene', 'group'):
        raise config.error("lightspeed: section must be [lightspeed strip "
                           "<name>], [lightspeed scene <state|name>] or "
                           "[lightspeed group <name>], got [%s]"
                           % (config.get_name(),))
    if parts[1] == 'strip':
        light.add_strip(Strip(light, config))
    elif parts[1] == 'group':
        light.add_group(config)
    else:
        light.add_scene(config)
    # A plain object: Moonraker lists only the main 'lightspeed' status
    return SectionMarker()


class SectionMarker(object):
    pass
