import base64
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
    def __init__(self, current_payload):
        self.current_payload = current_payload
        self.put_calls = []
        self.get_calls = []

    def get(self, url, *, params, headers, timeout):
        self.get_calls.append((url, params, headers, timeout))
        return FakeResponse(200, self.current_payload)

    def put(self, url, *, headers, json, timeout):
        self.put_calls.append((url, headers, json, timeout))
        return FakeResponse(200, {'commit': {'sha': 'abc123'}})


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
            result = publish_once(self._report(tmp), token='')
            self.assertEqual(result['status'], 'DISABLED')
            self.assertIn('RESEARCH_GITHUB_TOKEN', result['detail'])

    def test_updates_only_research_data_branch_with_existing_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._report(tmp)
            session = FakeSession(
                {
                    'sha': 'oldsha',
                    'content': base64.b64encode(b'{"old":true}').decode('ascii'),
                }
            )
            result = publish_once(
                report_path,
                token='secret-for-test',
                repository='joshuatec7-prog/Diamond-Trader',
                branch='research-data',
                remote_path='latest.json',
                session=session,
            )

            self.assertEqual(result['status'], 'OK')
            self.assertEqual(result['commit'], 'abc123')
            self.assertEqual(len(session.put_calls), 1)
            payload = session.put_calls[0][2]
            self.assertEqual(payload['branch'], 'research-data')
            self.assertEqual(payload['sha'], 'oldsha')
            self.assertEqual(
                payload['message'],
                'Research data: update latest hourly report',
            )
            decoded = json.loads(base64.b64decode(payload['content']).decode('utf-8'))
            self.assertEqual(decoded['mode'], 'OBSERVE_ANALYSE_ONLY')

    def test_identical_remote_report_does_not_create_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_path = self._report(tmp)
            body = (
                json.dumps(
                    {'generated_at_ms': 123, 'mode': 'OBSERVE_ANALYSE_ONLY'},
                    indent=2,
                    sort_keys=True,
                )
                + '\n'
            ).encode('utf-8')
            session = FakeSession(
                {
                    'sha': 'same',
                    'content': base64.b64encode(body).decode('ascii'),
                }
            )
            result = publish_once(
                report_path,
                token='secret-for-test',
                branch='research-data',
                session=session,
            )
            self.assertEqual(result['status'], 'UNCHANGED')
            self.assertEqual(session.put_calls, [])


if __name__ == '__main__':
    unittest.main()
