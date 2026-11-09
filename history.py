"""Durable at-most-once recommendations. Missing/corrupt history fails closed.

Reserve before Telegram: an ambiguous send is never automatically resent.
Only public repo names, status and timestamps are stored; no chat IDs or keys.
"""
import base64
from datetime import datetime, timezone
import json
import os
import re

import requests


class HistoryError(RuntimeError):
    pass


class History:
    def __init__(self, session):
        repo = os.getenv('GITHUB_REPOSITORY', '')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not os.getenv('GITHUB_TOKEN'):
            raise HistoryError('전송 이력 인증 환경 없음: 중복 방지를 위해 전송 중단')
        self.session = session
        self.url = f'https://api.github.com/repos/{repo}/contents/state/sent.json'
        response = self.request('get', params={'ref': 'main'})
        try:
            record = response.json()
            self.sha = record['sha']
            self.data = json.loads(base64.b64decode(record['content'], validate=False))
            if self.data['version'] != 1 or not isinstance(self.data['repos'], dict):
                raise ValueError()
            if any(not isinstance(k, str) or not isinstance(v, dict) for k, v in self.data['repos'].items()):
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise HistoryError('전송 이력 손상: 이력을 초기화하거나 재전송하지 않습니다') from None

    def request(self, method, **kwargs):
        try:
            response = getattr(self.session, method)(self.url, timeout=(10, 30), **kwargs)
        except requests.RequestException:
            raise HistoryError('전송 이력 통신 실패: 전송 중단') from None
        if response.status_code not in (200, 201):
            raise HistoryError(f'전송 이력 읽기/저장 실패 HTTP {response.status_code}: 전송 중단')
        return response

    def seen(self, repo):
        return repo['full_name'].casefold() in self.data['repos']

    def record(self, repo, status):
        self.data['repos'][repo['full_name'].casefold()] = {
            'status': status, 'at': datetime.now(timezone.utc).isoformat(),
        }
        content = base64.b64encode(json.dumps(self.data, ensure_ascii=False, indent=2).encode()).decode()
        response = self.request('put', json={'message': f'Update digest delivery state [skip ci]',
            'branch': 'main', 'sha': self.sha, 'content': content})
        try:
            self.sha = response.json()['content']['sha']
        except (KeyError, ValueError, TypeError):
            raise HistoryError('전송 이력 저장 응답 불명확: 전송 중단') from None

    def reserve(self, repo):
        if self.seen(repo):
            raise HistoryError('이미 전송 또는 전송 시도한 후보입니다')
        self.record(repo, 'reserved')

    def confirm(self, repo):
        self.record(repo, 'sent')
