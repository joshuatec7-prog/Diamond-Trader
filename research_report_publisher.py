from __future__ import annotations

import argparse
import base64
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
DEFAULT_BRANCH = 'research-data'
DEFAULT_REMOTE_PATH = 'latest.json'
TOKEN_ENV = 'RESEARCH_GITHUB_TOKEN'


def _clean_remote_path(value: str) -> str:
    path = value.strip().lstrip('/')
    if not path or path.endswith('/') or '..' in Path(path).parts:
        raise ValueError('ongeldig research publish pad')
    return path


def _repository_parts(repository: str) -> tuple[str, str]:
    clean = repository.strip()
    parts = clean.split('/')
    if len(parts) != 2 or not all(parts):
        raise ValueError('RESEARCH_GITHUB_REPOSITORY moet owner/repo zijn')
    return parts[0], parts[1]


def publish_once(
    report_path: str,
    *,
    token: str | None = None,
    repository: str | None = None,
    branch: str | None = None,
    remote_path: str | None = None,
    session: requests.Session | None = None,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    token = (os.getenv(TOKEN_ENV, '') if token is None else token).strip()
    repository = (
        os.getenv('RESEARCH_GITHUB_REPOSITORY', DEFAULT_REPOSITORY)
        if repository is None
        else repository
    ).strip()
    branch = (
        os.getenv('RESEARCH_GITHUB_BRANCH', DEFAULT_BRANCH)
        if branch is None
        else branch
    ).strip()
    remote_path = _clean_remote_path(
        os.getenv('RESEARCH_GITHUB_PATH', DEFAULT_REMOTE_PATH)
        if remote_path is None
        else remote_path
    )

    if not token:
        return {
            'status': 'DISABLED',
            'detail': f'{TOKEN_ENV} ontbreekt',
            'repository': repository,
            'branch': branch,
            'path': remote_path,
        }

    if not branch:
        return {
            'status': 'ERROR',
            'detail': 'RESEARCH_GITHUB_BRANCH is leeg',
            'repository': repository,
            'branch': branch,
            'path': remote_path,
        }

    p = Path(report_path)
    if not p.exists():
        return {
            'status': 'WAIT',
            'detail': 'lokaal researchrapport bestaat nog niet',
            'repository': repository,
            'branch': branch,
            'path': remote_path,
        }

    try:
        owner, repo = _repository_parts(repository)
        report = json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(report, dict):
            raise ValueError('researchrapport is geen JSON-object')
        body = (json.dumps(report, indent=2, sort_keys=True) + '\n').encode('utf-8')

        client = session or requests.Session()
        headers = {
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {token}',
            'X-GitHub-Api-Version': '2022-11-28',
            'User-Agent': 'cryptobot-cleanroom-research-publisher',
        }
        url = f'https://api.github.com/repos/{owner}/{repo}/contents/{remote_path}'

        current = client.get(
            url,
            params={'ref': branch},
            headers=headers,
            timeout=timeout_seconds,
        )
        sha: str | None = None
        if current.status_code == 200:
            payload = current.json()
            sha_value = payload.get('sha') if isinstance(payload, dict) else None
            sha = str(sha_value or '').strip() or None

            encoded = payload.get('content') if isinstance(payload, dict) else None
            if isinstance(encoded, str):
                try:
                    existing = base64.b64decode(encoded.replace('\n', '')).strip()
                except (ValueError, TypeError):
                    existing = b''
                if existing == body.strip():
                    return {
                        'status': 'UNCHANGED',
                        'detail': 'GitHub bevat al hetzelfde rapport',
                        'repository': repository,
                        'branch': branch,
                        'path': remote_path,
                    }
        elif current.status_code != 404:
            return {
                'status': 'ERROR',
                'detail': f'GitHub read HTTP {current.status_code}',
                'repository': repository,
                'branch': branch,
                'path': remote_path,
            }

        update: dict[str, Any] = {
            'message': 'Research data: update latest hourly report',
            'content': base64.b64encode(body).decode('ascii'),
            'branch': branch,
        }
        if sha:
            update['sha'] = sha

        saved = client.put(
            url,
            headers=headers,
            json=update,
            timeout=timeout_seconds,
        )
        if saved.status_code not in {200, 201}:
            return {
                'status': 'ERROR',
                'detail': f'GitHub write HTTP {saved.status_code}',
                'repository': repository,
                'branch': branch,
                'path': remote_path,
            }

        result = saved.json()
        commit = result.get('commit') if isinstance(result, dict) else None
        commit_sha = commit.get('sha') if isinstance(commit, dict) else None
        return {
            'status': 'OK',
            'detail': 'nieuwste researchrapport gepubliceerd',
            'repository': repository,
            'branch': branch,
            'path': remote_path,
            'commit': commit_sha,
        }
    except (OSError, ValueError, json.JSONDecodeError, requests.RequestException) as exc:
        return {
            'status': 'ERROR',
            'detail': f'{type(exc).__name__}: {exc}',
            'repository': repository,
            'branch': branch,
            'path': remote_path,
        }


def print_status(result: dict[str, Any]) -> None:
    print('=== RESEARCH REPORT PUBLISHER ===')
    print(f"STATUS          : {result['status']}")
    print(f"DETAIL          : {result['detail']}")
    print(f"REPOSITORY      : {result['repository']}")
    print(f"BRANCH          : {result['branch']}")
    print(f"PAD             : {result['path']}")
    print(f"TOKEN           : {'INGESTELD' if os.getenv(TOKEN_ENV, '').strip() else 'ONTBREEKT'}")
    if result.get('commit'):
        print(f"LAATSTE COMMIT  : {result['commit']}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Publiceer het nieuwste PAPER researchrapport naar GitHub research-data'
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
        'gestart | research-data publisher | interval=%ss | branch=%s | pad=%s',
        PUBLISH_INTERVAL_SECONDS,
        os.getenv('RESEARCH_GITHUB_BRANCH', DEFAULT_BRANCH),
        os.getenv('RESEARCH_GITHUB_PATH', DEFAULT_REMOTE_PATH),
    )

    while True:
        started = time.monotonic()
        result = publish_once(local_report)
        logger.info(
            'publish | status=%s | detail=%s | branch=%s | path=%s',
            result['status'],
            result['detail'],
            result['branch'],
            result['path'],
        )
        elapsed = time.monotonic() - started
        time.sleep(max(60.0, PUBLISH_INTERVAL_SECONDS - elapsed))


if __name__ == '__main__':
    raise SystemExit(main())
