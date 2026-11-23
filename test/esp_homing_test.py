"""Check the ESP producer at the point where a homing override silences it."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import smoke_klippy as smoke


class EspGuardTests(unittest.TestCase):
    def setUp(self):
        self.strip = Mock(brightness=1.)
        self.light = SimpleNamespace(enabled=True, brightness=1., state='idle',
                                     current_specs={self.strip: ('solid', {'color': '00005a'})})
        self.output = smoke.LS.EspBridgeOutput(self.light, 'led')
        self.output.strips = [self.strip]
        self.bridge = smoke.Bridge()
        self.bridge.publish = Mock(return_value=True)
        self.output.bridge = self.bridge
        self.output.push(100., {}, False)

    def enter_homing(self):
        self.light.state = 'homing'
        self.light.current_specs[self.strip] = ('chase', {'color': '00ff00'})
        self.output.push(100.01, {}, False)

    def test_entry_publishes_before_suspension_without_full_telemetry(self):
        self.bridge.publish.assert_not_called()
        self.enter_homing()
        calls = dict(call.args for call in self.bridge.publish.call_args_list)
        self.assertEqual((calls['led.st'], calls['led.fx'], calls['led.rgb']),
                         ('homing', 'chase', '0,255,0'))
        self.assertEqual(self.bridge.passes, [])
        self.bridge.publish.reset_mock()
        self.output.push(100.26, {}, False)
        self.bridge.publish.assert_not_called()

    def test_disabled_or_quiet_bridge_only_prepares_producer(self):
        for enabled, quiet in ((False, False), (True, True)):
            with self.subTest(enabled=enabled, quiet=quiet):
                self.light.state = 'idle'
                self.output.push(100., {}, False)
                self.bridge._enabled = enabled
                self.bridge._silenced = lambda: quiet
                self.enter_homing()
                self.bridge.publish.assert_not_called()
                self.assertEqual(self.bridge.passes, [])
                self.assertEqual(dict(self.output._produce(100.02))['led.rgb'], '0,255,0')

    def test_legacy_bridge_flushes_once_without_publishing_while_quiet(self):
        del self.bridge.publish
        self.enter_homing()
        self.assertEqual(self.bridge.passes, [100.01])
        self.bridge._silenced = lambda: True
        self.light.state = 'probing'
        self.output.push(101., {}, False)
        self.assertEqual(self.bridge.passes, [100.01])
        self.assertEqual(dict(self.output._produce(101.))['led.st'], 'probing')


if __name__ == '__main__':
    unittest.main()
