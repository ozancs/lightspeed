"""Offline output regressions using the same Klipper fixtures as smoke_klippy."""
import socket
import threading
import unittest
from unittest import mock

import smoke_klippy as smoke

LS = smoke.LS

CFG = """
[lightspeed]
brightness: 1
[lightspeed strip first]
output: neopixel case
leds: 1
[lightspeed scene idle]
first: solid color=ff0000
"""


class OutputTest(unittest.TestCase):
    def setUp(self):
        reactor_patch = mock.patch.object(smoke, 'REACTOR', smoke.Reactor())
        self.reactor = reactor_patch.start()
        self.addCleanup(reactor_patch.stop)
        self.printer, self.gcode = smoke.build(CFG, 0)
        self.light = self.printer.objects['lightspeed']
        self.chain = self.printer.objects['neopixel case']
        self.out = self.light.outputs[0]
        self.strip = self.light.strips[0]

    def test_merge_only_managed_rgb_when_queued_frame_runs(self):
        self.out.push(100, {self.strip: [(1, 0, 0)]}, False)
        # SET_LED runs after push and before the queued MCU write.
        self.chain.led_helper.led_state[0] = (0, 0, 0, .8)
        self.chain.led_helper.led_state[1] = (0, 1, 0, .5)
        callback, _ = self.reactor.callbacks.pop()
        callback(100)
        self.assertEqual(self.chain.updates[-1][1][:2],
                         [(1, 0, 0, .8), (0, 1, 0, .5)])

    def test_guard_snapshot_retries_after_transient_send_failure(self):
        # Use the three-LED fixture so guard snapshots are eligible.
        self.strip.leds = [0]
        self.out.target = 'hotend_rgb'
        self.out.connect()
        self.light._rails_begin(None, [])
        self.light.state = 'homing'
        chain = self.out.chain
        chain.fail = True
        frame = {self.strip: [(1., 0., 0.)]}
        self.out.push(100, frame, True)
        callback, _ = self.reactor.callbacks.pop()
        with self.assertLogs(level='ERROR'):
            callback(100)
        self.assertEqual(self.out.errors, 1)
        chain.fail = False
        self.out.push(101, frame, True)
        callback, _ = self.reactor.callbacks.pop()
        callback(101)
        self.out.push(102, frame, True)
        self.assertFalse(self.reactor.callbacks)
        self.assertEqual(len(chain.updates), 1)
        self.assertEqual(self.out.errors, 0)

    def test_late_guard_and_new_command_invalidate_queued_frames(self):
        frame = {self.strip: [(1, 0, 0)]}
        self.out.push(100, frame, False)
        callback, _ = self.reactor.callbacks.pop()
        self.light._rails_begin(None, [])
        callback(100)
        self.assertFalse(self.chain.updates)
        self.light._rails_end(None, [])
        self.reactor.t = 102
        self.out.push(102, frame, False)
        callback, _ = self.reactor.callbacks.pop()
        self.gcode.run('LIGHTSPEED_ENABLE VALUE=0')
        callback(102)
        self.assertFalse(self.chain.updates)
        self.assertFalse(self.out.busy)

    def test_rgbw_chain_uses_wire_bytes_for_rate_and_guard(self):
        cfg = CFG + '\n[neopixel case]\ncolor_order: GRBW\n'
        def set_chain(p, g):
            p.objects['neopixel case'] = smoke.Chain(14)
        p, _ = smoke.build(cfg, 0, set_chain)
        ls = p.objects['lightspeed']
        out = ls.outputs[0]
        self.assertEqual(out.nbytes, 56)
        self.assertEqual(out.fps, LS.engine.mcu_fps(56))
        out.push(100, {ls.strips[0]: [(1, 0, 0)]}, True)
        self.assertFalse(out.busy)
        out.push(100, {ls.strips[0]: [(1, 0, 0)]}, False)
        callback, _ = self.reactor.callbacks.pop()
        callback(100)
        self.assertEqual(out.sent_bytes, 56)
        status = ls.get_status(100)['strips']['first']
        self.assertEqual((status['count'], status['chain_count'],
                          status['frame_bytes']), (1, 14, 56))

    def test_neopixel_wire_buffer_is_preferred_when_available(self):
        self.chain.color_data = bytearray(350)  # 50 RGB + 50 RGBW LEDs
        self.out.connect()
        self.assertEqual(self.out.nbytes, 350)
        self.assertEqual(self.out.fps, LS.engine.mcu_fps(350))

    def test_on_cancels_blackout_even_with_an_active_strip_preview(self):
        self.light._do_preview('first', 'solid color=00ff00', 10)
        self.gcode.run('LIGHTSPEED_ENABLE VALUE=0')
        self.reactor.run_until(100.3)
        self.assertEqual(self.out.chain.led_helper.led_state[0], (0, 0, 0, 0))
        self.gcode.run('LIGHTSPEED_ENABLE VALUE=1')
        self.reactor.run_until(102)
        self.assertTrue(self.light.enabled)
        self.assertEqual(self.out.chain.led_helper.led_state[0], (0, 1, 0, 0))
        self.gcode.run('LIGHTSPEED_ENABLE VALUE=0')
        self.reactor.run_until(104)
        self.assertFalse(self.light.enabled)
        self.assertEqual(self.out.chain.led_helper.led_state[0], (0, 0, 0, 0))

    def test_blink_respects_entire_chain_budget_and_host_slowdown(self):
        self.light._do_preview('first', 'blink period=0.1', 10)
        # One controlled LED still clocks the full 100-LED chain.
        for speed in (1., .5):
            self.light.mcu_slow = speed
            self.light.mcu_slow_until = 200
            self.out.next_due = 0
            self.light._tick_inner(100)
            self.assertAlmostEqual(self.out.next_due - 100,
                                   self.out.interval() / speed)

    def test_dns_retries_with_backoff_and_recovers(self):
        out = LS.WledOutput(self.light, 'case.local')
        stop = mock.Mock(wraps=threading.Event())
        stop.wait.side_effect = lambda delay: False
        with mock.patch.object(socket, 'gethostbyname', side_effect=[
                socket.gaierror('unavailable'), socket.gaierror('unavailable'),
                '192.0.2.2']):
            out._resolve(stop)
        self.assertEqual(stop.wait.call_args_list, [mock.call(1), mock.call(2)])
        self.assertEqual(out.addr, ('192.0.2.2', LS.WLED_PORT))
        self.assertEqual(out.last_error, '')

    def test_disconnect_discards_inflight_dns_and_cancels_retry(self):
        out = LS.WledOutput(self.light, 'case.local')
        stop = out.resolve_stop = threading.Event()
        def finish_after_close(host):
            out.close()
            return '192.0.2.2'
        with mock.patch.object(socket, 'gethostbyname', finish_after_close):
            out._resolve(stop)
        self.assertTrue(stop.is_set())
        self.assertIsNone(out.addr)


if __name__ == '__main__':
    unittest.main()
