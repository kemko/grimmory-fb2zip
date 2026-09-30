import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('smoke', Path(__file__).with_name('smoke.py'))
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class ScanWaitTest(unittest.TestCase):
    def test_waits_for_new_completion_after_old_scan(self):
        old = 'Parsing task completed!\n'
        with patch.object(smoke, 'application_logs', side_effect=[old, old + 'scan running\n',
                          old + 'scan running\nParsing task completed!\n']) as logs, \
             patch.object(smoke.time, 'sleep'):
            smoke.wait_for_scan(old)
        self.assertEqual(logs.call_count, 3)

    def test_scan_error_or_skipped_request_fails_even_with_completion(self):
        for message in (' ERROR Error while parsing library books',
                        'InvalidDataAccessApiUsageException - Library id: 1', 'skipping duplicate rescan request'):
            with self.subTest(message=message), \
                 patch.object(smoke, 'application_logs', return_value=message + '\nParsing task completed!'):
                with self.assertRaises(AssertionError):
                    smoke.wait_for_scan('')

    def test_old_completion_does_not_hide_timeout(self):
        old = 'Parsing task completed!\n'
        with patch.object(smoke, 'application_logs', return_value=old), \
             patch.object(smoke.time, 'monotonic', side_effect=[0, 0, 121]), \
             patch.object(smoke.time, 'sleep'):
            with self.assertRaisesRegex(AssertionError, 'Timed out'):
                smoke.wait_for_scan(old)


if __name__ == '__main__':
    unittest.main()
