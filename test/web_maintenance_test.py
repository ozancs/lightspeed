"""Exercise the page's migration and guarded-restart endpoints without hardware."""
from contextlib import contextmanager, nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import smoke_klippy as K


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.printer, self.gcode = K.build('[lightspeed]\n', 0)
        self.endpoints = self.printer.objects['webhooks'].endpoints

    def request(self, endpoint, **params):
        req = K.WebRequest(params)
        self.endpoints['lightspeed/' + endpoint](req)
        return req.result

    def test_restart_checks_state_under_gcode_lock(self):
        events = []

        @contextmanager
        def lock():
            events.append('lock')
            try:
                yield
            finally:
                events.append('release')

        mutex = lock()
        mutex.test = lambda: False
        self.gcode.get_mutex = lambda: mutex
        self.gcode.run_script_from_command = events.append
        self.assertEqual(self.request('restart_if_idle'), {'ok': True})
        self.assertEqual(events, ['lock', 'FIRMWARE_RESTART', 'release'])

        # A print begins just as the current request takes the lock.
        @contextmanager
        def start_print():
            self.printer.objects['print_stats'].st['state'] = 'printing'
            yield

        mutex = start_print()
        mutex.test = lambda: False
        with self.assertRaises(K.GCmdError):
            self.request('restart_if_idle')
        self.assertEqual(events.count('FIRMWARE_RESTART'), 1)

    def test_uncertain_or_active_states_never_restart(self):
        self.gcode.run_script_from_command = Mock()
        busy = Mock()
        busy.test.return_value = True
        self.gcode.get_mutex = lambda: busy
        with self.assertRaises(K.GCmdError):
            self.request('restart_if_idle')
        for obj, status in [('print_stats', {'state': 'paused'}),
                            ('print_stats', {'state': 'unknown'}),
                            ('idle_timeout', {'state': 'Printing'}),
                            ('virtual_sdcard', {'is_active': True}),
                            ('pause_resume', {'is_paused': True})]:
            with self.subTest(obj=obj), patch.dict(self.printer.objects,
                                                    {obj: K.Obj(status)}):
                mutex = nullcontext()
                mutex.test = lambda: False
                self.gcode.get_mutex = lambda: mutex
                with self.assertRaises(K.GCmdError):
                    self.request('restart_if_idle')
        self.gcode.run_script_from_command.assert_not_called()

    def test_unavailable_status_returns_web_error(self):
        mutex = nullcontext()
        mutex.test = lambda: False
        self.gcode.get_mutex = lambda: mutex
        self.gcode.run_script_from_command = Mock()
        self.printer.objects['print_stats'].get_status = Mock(side_effect=RuntimeError('offline'))
        with self.assertLogs(level='ERROR'), self.assertRaisesRegex(K.GCmdError, 'cannot restart safely'):
            self.request('restart_if_idle')
        self.gcode.run_script_from_command.assert_not_called()

    def test_migration_uses_the_running_instance_and_verified_result(self):
        legacy = ('[neopixel case]\npin: PA1\nchain_count: 4\n'
                  '[led_effect warm]\nleds: neopixel:case\n'
                  'layers: static 1 0 top (1,0.5,0)\n')
        with TemporaryDirectory() as folder:
            root = Path(folder)
            other = root / 'printer.cfg'
            active = root / 'printer-2.cfg'
            other.write_text('# a different instance\n')
            active.write_text(legacy)
            existing = root / 'lightspeed.cfg'
            existing.write_text('[lightspeed]\n[lightspeed strip custom]\n'
                                'output: wled example.local\ncount: 6\n')
            self.printer.get_start_args = lambda: {'config_file': str(active)}

            # Run the actual CLI synchronously in the temporary directory.
            def thread(target, args):
                return SimpleNamespace(start=lambda: target(*args))
            with patch.object(K.LS, 'threading', SimpleNamespace(Thread=thread)):
                result = self.request('migrate', start=1)
                self.assertEqual(result['state'], 'merge_required')
                self.assertFalse(result['ok'])
                self.assertTrue((root / result['candidate']).is_file())
                self.assertEqual(active.read_text(), legacy)
                existing.unlink()
                result = self.request('migrate', start=1)
                self.assertTrue(result['ok'], result['output'])
                self.assertIn('[include lightspeed.cfg]', active.read_text())
                self.assertIn('[lightspeed scene warm]', existing.read_text())
                self.assertEqual(other.read_text(), '# a different instance\n')

    def test_successful_exit_requires_clean_active_config(self):
        ls = self.printer.objects['lightspeed']
        ok = SimpleNamespace(returncode=0, stdout=b'')
        with patch.object(K.LS.subprocess, 'run', side_effect=[ok, ok]):
            ls._migrate_run('migrate.py', '/printers/second.cfg')
        self.assertFalse(ls.migrate['ok'])
        self.assertIn('second.cfg', ls.migrate['output'])


if __name__ == '__main__':
    unittest.main()
