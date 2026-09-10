import json
import tempfile
import unittest
from pathlib import Path

from supervisor import (
    Child,
    HUMAN_TRIGGER_MAX_AGE_SECONDS,
    PRACTICAL_MONITOR_MAX_AGE_SECONDS,
    REPORT_MAX_AGE_SECONDS,
    V36_WORKER_MAX_AGE_SECONDS,
    V37_WORKER_MAX_AGE_SECONDS,
    V39_WORKER_MAX_AGE_SECONDS,
    _report_health_error,
)


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

    def test_stale_practical_monitor_is_unhealthy_when_position_is_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'generated_at_ms': int((now - 60) * 1000),
                'signal_research': {
                    'practical_open': 1,
                    'practical_monitor_attempted_ms': int(
                        (now - PRACTICAL_MONITOR_MAX_AGE_SECONDS - 1) * 1000
                    ),
                },
            }))
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIn('winstmonitor stilgevallen', _report_health_error(child, now=now) or '')

    def test_fresh_practical_monitor_is_healthy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'generated_at_ms': int((now - 60) * 1000),
                'signal_research': {
                    'practical_open': 1,
                    'practical_monitor_attempted_ms': int((now - 30) * 1000),
                },
            }))
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

    def test_stale_human_decision_layer_is_unhealthy_in_v35(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'version': '3.5',
                'generated_at_ms': int((now - 60) * 1000),
                'human_research': {
                    'human_entry_attempted_ms': int(
                        (now - HUMAN_TRIGGER_MAX_AGE_SECONDS - 1) * 1000
                    ),
                },
            }))
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIn('beslislaag stilgevallen', _report_health_error(child, now=now) or '')

    def test_fresh_human_layers_are_healthy_with_open_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'report.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'version': '3.5',
                'generated_at_ms': int((now - 60) * 1000),
                'human_research': {
                    'human_entry_attempted_ms': int((now - 30) * 1000),
                    'human_open': 1,
                    'human_monitor_attempted_ms': int((now - 30) * 1000),
                },
            }))
            child = Child(['python3', '-u', 'worker.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

    def test_v36_requires_all_three_independent_heartbeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v36.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'version': '3.6',
                'component': 'AUTONOMOUS_PAPERBOT',
                'generated_at_ms': int((now - 10) * 1000),
                'modes': {'live_orders': 'UIT / TECHNISCH ONMOGELIJK'},
                'heartbeat': {
                    'cycle_attempted_ms': int((now - 20) * 1000),
                    'candidate_recheck_attempted_ms': int((now - 20) * 1000),
                    'position_monitor_attempted_ms': int((now - 20) * 1000),
                },
            }))
            child = Child(['python3', '-u', 'autonomous_v36.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

            report = json.loads(path.read_text())
            report['heartbeat']['cycle_attempted_ms'] = int(
                (now - V36_WORKER_MAX_AGE_SECONDS - 1) * 1000
            )
            path.write_text(json.dumps(report))
            self.assertIn('universumscan stilgevallen', _report_health_error(child, now=now) or '')

    def test_v37_requires_observe_only_and_two_independent_heartbeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v37.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'version': '3.7',
                'component': 'HUMAN_OBSERVER_V37',
                'generated_at_ms': int((now - 10) * 1000),
                'modes': {
                    'paper_execution': 'UIT',
                    'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
                },
                'safety': {'execution_enabled': False},
                'heartbeat': {
                    'cycle_attempted_ms': int((now - 20) * 1000),
                    'candidate_recheck_attempted_ms': int((now - 20) * 1000),
                },
            }))
            child = Child(['python3', '-u', 'autonomous_v37.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

            report = json.loads(path.read_text())
            report['heartbeat']['candidate_recheck_attempted_ms'] = int(
                (now - V37_WORKER_MAX_AGE_SECONDS - 1) * 1000
            )
            path.write_text(json.dumps(report))
            self.assertIn('L2-meetvenster stilgevallen', _report_health_error(child, now=now) or '')

            report['heartbeat']['candidate_recheck_attempted_ms'] = int((now - 20) * 1000)
            report['safety']['execution_enabled'] = True
            path.write_text(json.dumps(report))
            self.assertIn('uitvoering staat niet aantoonbaar uit', _report_health_error(child, now=now) or '')

    def test_v39_requires_complete_observe_only_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'v39.json'
            now = 10_000.0
            path.write_text(json.dumps({
                'version': '3.9',
                'component': 'FULL_EUR_HUMAN_PIPELINE_V39',
                'generated_at_ms': int((now - 10) * 1000),
                'modes': {
                    'paper_execution': 'UIT',
                    'live_orders': 'UIT / TECHNISCH ONMOGELIJK',
                },
                'safety': {'execution_enabled': False},
                'heartbeat': {
                    'discovery_attempted_ms': int((now - 20) * 1000),
                    'candidate_recheck_attempted_ms': int((now - 20) * 1000),
                    'outcome_attempted_ms': int((now - 20) * 1000),
                },
            }))
            child = Child(['python3', '-u', 'autonomous_v39.py'], False, str(path))
            child.started_at = 1_000.0
            self.assertIsNone(_report_health_error(child, now=now))

            report = json.loads(path.read_text())
            report['heartbeat']['outcome_attempted_ms'] = int(
                (now - V39_WORKER_MAX_AGE_SECONDS - 1) * 1000
            )
            path.write_text(json.dumps(report))
            self.assertIn(
                'uitkomstmeting stilgevallen', _report_health_error(child, now=now) or ''
            )


if __name__ == '__main__':
    unittest.main()
