import json
import tempfile
import unittest
from pathlib import Path

from supervisor import Child, REPORT_MAX_AGE_SECONDS, _report_health_error


class SupervisorHealthTests(unittest.TestCase):
    def test_missing_report_is_unhealthy_after_startup_grace(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = Child(['python3', '-u', 'worker.py'], False, str(Path(tmp) / 'missing.json'))
            child.started_at = 1_000.0
            self.assertIn('ontbreekt', _report_health_error(child, now=1_400.0) or '')

    def test_fresh_report_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(json.dumps({'generated_at_ms': int((now - 60) * 1000)}))
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

    def test_stale_report_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(
                json.dumps({'generated_at_ms': int((now - REPORT_MAX_AGE_SECONDS - 1) * 1000)})
            )
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIn('verouderd', _report_health_error(child, now=now) or '')


if __name__ == '__main__':
    unittest.main()
