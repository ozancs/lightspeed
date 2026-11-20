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

# esp_bridge LED nodes animate these effects themselves
# The esp_bridge LED node renders only these: solid, pulse, bar, chase,
# blink, off (see esp_led.py); its speed and size are fixed on the node.
# blink and rainbow are driven from here (solid, colour switched by us).
ESP_FX = {
    'off': 'off', 'solid': 'solid', 'breathe': 'pulse', 'blink': 'solid',
    'rainbow': 'solid', 'chase': 'chase', 'comet': 'chase', 'scan': 'chase',
    'twinkle': 'pulse', 'fire': 'pulse', 'progress': 'bar', 'heat': 'bar',
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
        if blink or name == 'rainbow':
            # the node's own blink runs on its own clock: drive it from ours
            # (solid, colour switched at each edge) so it blinks with the
            # other strips. Rainbow: the whole node walks the colour wheel.
            c = engine.render(name, params, 1, self.light.ctx)[0]
            rgb = '%d,%d,%d' % tuple(engine.to_byte(x) for x in c)
        elif name == 'fire':
            rgb = '255,70,0'
        else:
            c = engine.parse_color(params.get('color', 'ffffff'))
            if name == 'heat':
                c = engine.mix(engine.parse_color(params['color2']), c,
                               _clamp01(self.light.ctx['heat']))
            elif name == 'progress' and params.get('gradient'):
                v = self.light.ctx['heat' if params.get('source') == 'heat'
                                    else 'progress']
                c = engine.mix(c, engine.parse_color(params['color2']),
                               _clamp01(v))
            rgb = '%d,%d,%d' % tuple(engine.to_byte(x) for x in c)
        if name == 'progress':
            p = self.light.ctx['heat' if params.get('source') == 'heat'
                                else 'progress']
        elif name == 'heat':
            p = self.light.ctx['heat']
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
        self.outputs = []
        self.enabled = True
        self.shutdown = False
        self.state = 'idle'
        self.auto_state = 'idle'
        self.override = None      # (state, until)
        self.preview = {}         # strip -> (spec, until)
        self.current_specs = {}   # strip -> (name, params)
        self.scene_since = 0.0
        self.prev_scene_frames = {}
        self.ctx = {'t': 0.0, 'progress': 0.0, 'heat': 0.0,
                    'pos': (0.0, 0.0, 0.0)}
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

    # ------------------------------------------------------------ config

    def add_strip(self, strip):
        if any(s.name == strip.name for s in self.strips):
            raise self.printer.config_error("lightspeed: strip '%s' defined "
                                            "twice" % (strip.name,))
        self.strips.append(strip)

    def add_scene(self, config):
        state = config.get_name().split()[-1].lower()
        if state not in engine.STATES:
            raise config.error("lightspeed: unknown state '%s' (one of %s)"
                               % (state, ', '.join(engine.STATES)))
        items = {}
        for opt in config.get_prefix_options(''):
            raw = config.get(opt)
            try:
                items[opt.lower()] = engine.parse_spec(raw)
            except engine.SpecError as e:
                raise config.error("lightspeed scene %s, %s: %s"
                                   % (state, opt, e))
        self.scenes[state] = items

    def _connect(self):
        names = set(s.name for s in self.strips)
        for state, items in self.scenes.items():
            for key in items:
                if key != 'all' and key not in names:
                    raise self.printer.config_error(
                        "lightspeed scene %s: no strip named '%s' (strips: "
                        "%s)" % (state, key, ', '.join(sorted(names)) or
                                 'none'))
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

    def _ready(self):
        self.shutdown = False
        self._wrap_guarded_commands()
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
        obj = self.printer.lookup_object(name, None)
        if obj is None:
            return {}
        try:
            return obj.get_status(eventtime)
        except Exception:
            return {}

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

    def _spec_for(self, strip, state, eventtime):
        pv = self.preview.get(strip.name) or self.preview.get('all')
        if pv is not None and eventtime < pv[1]:
            return pv[0]
        scene = engine.resolve_scene(state, self.scenes)
        if scene is None:
            return ('off', {'reverse': 0})
        items = self.scenes[scene]
        return items.get(strip.name) or items.get('all') or \
            ('off', {'reverse': 0})

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

        guard, guard_state = self._guard(eventtime)
        auto = self._decide(eventtime, guard_state)
        self.auto_state = auto
        state = auto
        if self.override is not None:
            if eventtime < self.override[1]:
                state = self.override[0]
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
            self.prev_scene_frames = dict(
                (s, s.last_frame) for s in self.strips)
            self.scene_since = eventtime
            self.state = state
        fade = _clamp01((eventtime - self.scene_since) / CROSSFADE)
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
                spec = self._spec_for(strip, state, eventtime)
                self.current_specs[strip] = spec
                if out.kind == 'esp_bridge':
                    continue
                frame = engine.render(spec[0], spec[1], strip.count, self.ctx)
                if fade < 1.0 and not (guard and out.uses_mcu):
                    frame = engine.crossfade(
                        self.prev_scene_frames.get(strip), frame, fade)
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
        return {
            'version': __version__, 'enabled': self.enabled,
            'state': self.state, 'auto_state': self.auto_state,
            'override': self.override is not None, 'guard': guard,
            'brightness': self.brightness, 'model': self.model,
            'size': self.size, 'mcu_rate': self.mcu_slow, 'strips': strips,
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
        raise self.ActionError("no strip '%s'" % (name,))

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
        if state not in engine.STATES:
            raise self.ActionError("unknown state '%s' (one of %s)"
                                   % (state, ', '.join(engine.STATES)))
        self._strip_named(name)
        items = self.scenes.setdefault(state, {})
        if not text:
            items.pop(name, None)
            if not items:
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
        if name != 'all' and not any(s.name == name for s in self.strips):
            raise gcmd.error("Lightspeed: no strip '%s'" % (name,))
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
        state = gcmd.get('STATE').lower()
        if state not in engine.STATES:
            raise gcmd.error("Lightspeed: unknown state '%s' (one of %s)"
                             % (state, ', '.join(engine.STATES)))
        name = gcmd.get('STRIP').lower()
        if name != 'all' and not any(s.name == name for s in self.strips):
            raise gcmd.error("Lightspeed: no strip '%s'" % (name,))
        text = gcmd.get('EFFECT', '').strip()
        items = self.scenes.setdefault(state, {})
        if not text:
            items.pop(name, None)
            if not items:
                del self.scenes[state]
        else:
            try:
                items[name] = engine.parse_spec(text)
            except engine.SpecError as e:
                raise gcmd.error("Lightspeed: %s" % (e,))
        self._kick()

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
    if len(parts) != 3 or parts[1] not in ('strip', 'scene'):
        raise config.error("lightspeed: section must be [lightspeed strip "
                           "<name>] or [lightspeed scene <state>], got [%s]"
                           % (config.get_name(),))
    if parts[1] == 'strip':
        light.add_strip(Strip(light, config))
    else:
        light.add_scene(config)
    # A plain object: Moonraker lists only the main 'lightspeed' status
    return SectionMarker()


class SectionMarker(object):
    pass
