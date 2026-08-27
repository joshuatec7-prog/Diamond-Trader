from __future__ import annotations

import argparse
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

from auto_research_controller import research_report_path
from config import Settings

logger = logging.getLogger('cryptobot_research_report_publisher')

PUBLISH_INTERVAL_SECONDS = 3600
DEFAULT_REPOSITORY = 'joshuatec7-prog/Diamond-Trader'
DEFAULT_ISSUE_NUMBER = 3
TOKEN_ENV = 'RESEARCH_GITHUB_TOKEN'


def _repository_parts(repository: str) -> tuple[str, str]:
    clean = repository.strip()
    parts = clean.split('/')
    if len(parts) != 2 or not all(parts):
        raise ValueError('RESEARCH_GITHUB_REPOSITORY moet owner/repo zijn')
    return parts[0], parts[1]


def _issue_number(value: int | str | None) -> int:
    raw = os.getenv('RESEARCH_GITHUB_ISSUE', str(DEFAULT_ISSUE_NUMBER)) if value is None else value
    number = int(raw)
    if number <= 0:
        raise ValueError('RESEARCH_GITHUB_ISSUE moet positief zijn')
    return number


def publish_once(
    report_path: str,
    *,
    token: str | None = None,
    repository: str | None = None,
    issue_number: int | str | None = None,
    session: requests.Session | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    token = (os.getenv(TOKEN_ENV, '') if token is None else token).strip()
    repository = (
        os.getenv('RESEARCH_GITHUB_REPOSITORY', DEFAULT_REPOSITORY)
        if repository is None
        else repository
    ).strip()

    try:
        issue = _issue_number(issue_number)
    except (TypeError, ValueError) as exc:
        return {
            'status': 'ERROR',
            'detail': f'{type(exc).__name__}: {exc}',
            'repository': repository,
            'issue': issue_number,
        }

    if not token:
        return {
            'status': 'DISABLED',
            'detail': f'{TOKEN_ENV} ontbreekt',
            'repository': repository,
            'issue': issue,
        }

    p = Path(report_path)
    if not p.exists():
        return {
            'status': 'WAIT',
            'detail': 'lokaal researchrapport bestaat nog niet',
            'repository': repository,
            'issue': issue,
        }

    try:
        owner, repo = _repository_parts(repository)
        report = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(report, dict):
            raise ValueError('researchrapport is geen JSON-object')
        body = json.dumps(report, indent=2, sort_keys=True) + '\n'

        client = session or requests.Session()
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'cryptobot-cleanroom-research-publisher',
        }
        url = f'https://api.github.com/repos/{owner}/{repo}/issues/{issue}'

        current = client.get(url, headers=headers, timeout=timeout_seconds)
        if current.status_code != 200:
            return {
                'status': 'ERROR',
                'detail': f'GitHub issue read HTTP {current.status_code}',
                'repository': repository,
                'issue': issue,
            }

        payload = current.json()
        current_body = payload.get('body') if isinstance(payload, dict) else None
        if isinstance(current_body, str) and current_body.strip() == body.strip():
            return {
                'status': 'UNCHANGED',
                'detail': 'GitHub Research Feed bevat al hetzelfde rapport',
                'repository': repository,
                'issue': issue,
            }

        saved = client.patch(
            url,
            headers=headers,
            json={'body': body},
            timeout=timeout_seconds,
        )
        if saved.status_code != 200:
            return {
                'status': 'ERROR',
                'detail': f'GitHub issue write HTTP {saved.status_code}',
                'repository': repository,
                'issue': issue,
            }

        return {
            'status': 'OK',
            'detail': 'nieuwste researchrapport gepubliceerd naar Research Feed',
            'repository': repository,
            'issue': issue,
        }
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        return {
            'status': 'ERROR',
            'detail': f'{type(exc).__name__}: {exc}',
            'repository': repository,
            'issue': issue,
        }


def print_status(result: dict[str, Any]) -> None:
    print('=== RESEARCH REPORT PUBLISHER ===')
    print(f"STATUS          : {result['status']}")
    print(f"DETAIL          : {result['detail']}")
    print(f"REPOSITORY      : {result['repository']}")
    print(f"RESEARCH ISSUE  : #{result['issue']}")
    print(f"TOKEN           : {'INGESTELD' if os.getenv(TOKEN_ENV, '').strip() else 'ONTBREEKT'}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Publiceer het nieuwste PAPER researchrapport naar GitHub Research Feed'
    )
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()

    settings = Settings()
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    local_report = research_report_path(settings.db_path)

    if args.once or args.status:
        result = publish_once(local_report)
        print_status(result)
        return 0 if result['status'] != 'ERROR' else 2

    logger.info(
        'gestart | Research Feed publisher | interval=%ss | issue=%s',
        PUBLISH_INTERVAL_SECONDS,
        os.getenv('RESEARCH_GITHUB_ISSUE', str(DEFAULT_ISSUE_NUMBER)),
    )

    while True:
        started = time.monotonic()
        result = publish_once(local_report)
        logger.info(
            'publish | status=%s | detail=%s | issue=%s',
            result['status'],
            result['detail'],
            result['issue'],
        )
        elapsed = time.monotonic() - started
        time.sleep(max(60.0, PUBLISH_INTERVAL_SECONDS - elapsed))


if __name__ == '__main__':
    raise SystemExit(main())
