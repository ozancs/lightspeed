"""G28 timing against the fake Klipper dispatcher and reactor."""
import unittest
from unittest.mock import patch

import smoke_klippy as smoke


CFG = """
[lightspeed]
brightness: 1
[lightspeed strip hotend]
output: neopixel hotend_rgb
[lightspeed strip case]
output: neopixel case
[lightspeed strip front]
output: esp_bridge led
count: 30
[lightspeed scene idle]
all: solid color=0000ff
[lightspeed scene homing]
all: solid color=00ff00
front: chase color=00ff00
[lightspeed scene priority_scene]
trigger: command
priority: 95
hotend: solid color=ff0000
"""
BLUE = (0., 0., 1., 0.)
GREEN = (0., 1., 0., 0.)
RED = (1., 0., 0., 0.)


class HomingTimingTest(unittest.TestCase):
    def setUp(self):
        self.reactor = smoke.Reactor()
        patcher = patch.object(smoke, 'REACTOR', self.reactor)
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, command):
        def register(printer, gcode):
            gcode.register_command('G28', command, desc='Home axes')
        self.printer, self.gcode = smoke.build(CFG, 0, register)
        self.light = self.printer.objects['lightspeed']
        self.hotend = self.printer.objects['neopixel hotend_rgb']
        self.case = self.printer.objects['neopixel case']
        self.advance(1.)
        self.assertEqual(self.hotend.updates[-1][1][0], BLUE)

    def advance(self, seconds):
        self.reactor.run_until(self.reactor.t + seconds)

    def test_scene_starts_before_first_rail_and_lasts_through_override(self):
        received = []

        def original(gcmd):
            received.append(gcmd)
            # A homing override can do substantial preparation before the
            # first Klipper rail event. Allow only one reactor iteration.
            self.advance(.001)
            self.assertEqual(self.light.state, 'homing')
            self.assertEqual(self.hotend.updates[-1][1][0], GREEN)
            self.printer.send_event('homing:home_rails_begin', None, [])
            self.printer.send_event('homing:homing_move_begin', None)
            self.advance(.25)
            self.printer.send_event('homing:homing_move_end', None)
            self.printer.send_event('homing:home_rails_end', None, [])
            # A long gap after homing one axis is still part of G28.
            self.advance(smoke.LS.GUARD_HOLD + .5)
            self.assertEqual(self.light.state, 'homing')
            self.assertEqual(self.hotend.led_helper.led_state[0], GREEN)
            self.assertEqual(len(self.case.updates), case_before)

        self.build(original)
        case_before = len(self.case.updates)
        # Model a scheduled future frame on an otherwise idle printer.
        next_due = self.reactor.t + .25
        self.reactor.update_timer(self.light.timer, next_due)
        for out in self.light.outputs:
            out.next_due = next_due
        gcmd = smoke.GCmd({'X': '0'})
        self.gcode.handlers['G28'](gcmd)
        self.assertEqual(received, [gcmd])
        self.advance(smoke.LS.GUARD_HOLD / 2)
        self.assertEqual(self.light.state, 'homing')
        self.advance(smoke.LS.GUARD_HOLD + smoke.LS.CROSSFADE + .2)
        self.assertEqual(self.light.state, 'idle')
        self.assertEqual(self.hotend.led_helper.led_state[0], BLUE)

    def test_failure_before_first_rail_releases_guard(self):
        failed = smoke.GCmdError('homing override failed')

        def original(gcmd):
            self.advance(.01)
            self.assertEqual(self.hotend.led_helper.led_state[0], GREEN)
            raise failed

        self.build(original)
        with self.assertRaises(smoke.GCmdError) as raised:
            self.gcode.run('G28')
        self.assertIs(raised.exception, failed)
        self.advance(smoke.LS.GUARD_HOLD + smoke.LS.CROSSFADE + .2)
        self.assertFalse(self.light.get_status(self.reactor.t)['guard'])
        self.assertEqual(self.hotend.led_helper.led_state[0], BLUE)

    def test_esp_handoff_precedes_suspend_and_nested_g28(self):
        calls = []
        def original(gcmd):
            # First line of homing_override, before the reactor can run.
            bridge = self.printer.objects['esp_bridge']
            keys = dict(bridge.producers[0](self.reactor.t))
            self.assertEqual((keys['led.st'], keys['led.fx'], keys['led.rgb']),
                             ('homing', 'chase', '0,255,0'))
            calls.append(gcmd)
            if len(calls) == 1:
                self.assertEqual(bridge.passes, [self.reactor.t])
                bridge._silenced = lambda: True
                self.gcode.run('G28 Y=0')
                self.advance(smoke.LS.GUARD_HOLD + .25)
                self.assertEqual(self.light.state, 'homing')
        self.build(original)
        self.gcode.run('G28')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.printer.objects['esp_bridge'].passes), 1)

    def test_higher_priority_scene_still_wins_on_entry(self):
        def original(gcmd):
            self.advance(.01)
            self.assertEqual(self.light.state, 'homing')
            self.assertEqual(self.hotend.led_helper.led_state[0], RED)

        self.build(original)
        self.gcode.run('LIGHTSPEED_SCENE_ON NAME=priority_scene')
        self.advance(smoke.LS.CROSSFADE + .2)
        self.gcode.run('G28')


if __name__ == '__main__':
    unittest.main()
