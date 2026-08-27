import json
import tempfile
import unittest
from pathlib import Path

from research_report_publisher import publish_once


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, current_body):
        self.current_body = current_body
        self.patch_calls = []
        self.get_calls = []

    def get(self, url, *, headers, timeout):
        self.get_calls.append((url, headers, timeout))
        return FakeResponse(200, {'body': self.current_body})

    def patch(self, url, *, headers, json, timeout):
        self.patch_calls.append((url, headers, json, timeout))
        return FakeResponse(200, {'number': 3})


class ResearchReportPublisherTests(unittest.TestCase):
    def _report(self, tmp: str) -> str:
        path = Path(tmp) / 'report.json'
        path.write_text(
            json.dumps({'mode': 'OBSERVE_ANALYSE_ONLY', 'generated_at_ms': 123}),
            encoding='utf-8',
        )
        return str(path)

    def test_without_token_is_disabled_and_never_needs_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = publish_once(self._report(tmp), token='', issue_number=3)
            self.assertEqual(result['status'], 'DISABLED')
            self.assertIn('RESEARCH_GITHUB_TOKEN', result['detail'])

    def test_updates_only_research_feed_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._report(tmp)
            session = FakeSession('{"old":true}')
            result = publish_once(
                report_path,
                token='secret-for-test',
                repository='joshuatec7-prog/Diamond-Trader',
                issue_number=3,
                session=session,
            )

            self.assertEqual(result['status'], 'OK')
            self.assertEqual(result['issue'], 3)
            self.assertEqual(len(session.patch_calls), 1)
            url, _headers, payload, _timeout = session.patch_calls[0]
            self.assertTrue(url.endswith('/issues/3'))
            decoded = json.loads(payload['body'])
            self.assertEqual(decoded['mode'], 'OBSERVE_ANALYSE_ONLY')

    def test_identical_issue_body_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._report(tmp)
            current = json.dumps(
                {'generated_at_ms': 123, 'mode': 'OBSERVE_ANALYSE_ONLY'},
                indent=2,
                sort_keys=True,
            )
            session = FakeSession(current)
            result = publish_once(
                report_path,
                token='secret-for-test',
                issue_number=3,
                session=session,
            )
            self.assertEqual(result['status'], 'UNCHANGED')
            self.assertEqual(session.patch_calls, [])


if __name__ == '__main__':
    unittest.main()
