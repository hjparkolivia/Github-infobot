"""KST daily delivery lock and a separate, AI-free missed-delivery watchdog."""
import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
import re
import sys

import requests

KST = timezone(timedelta(hours=9))


class DailyError(RuntimeError):
    pass


class Ledger:
    def __init__(self, session, day):
        repo = os.getenv('GITHUB_REPOSITORY', '')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
            raise DailyError('일별 발송 이력 저장소 설정 없음')
        self.session, self.day = session, day
        self.url = f'https://api.github.com/repos/{repo}/contents/state/daily/{day}.json'
        self.data, self.sha = {}, None
        response = self.request('get', allowed=(200, 404), params={'ref': 'main'})
        if response.status_code == 404:
            return  # A new date is the only supported missing-file case.
        try:
            item = response.json()
            self.sha = item['sha']
            self.data = json.loads(base64.b64decode(item['content']))
            if self.data.get('day') != day or self.data.get('version') != 1:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise DailyError('일별 이력 손상: 초기화·재전송 중단') from None

    def request(self, method, allowed=(200, 201), **kwargs):
        try:
            response = getattr(self.session, method)(self.url, timeout=(10, 25), **kwargs)
        except requests.RequestException:
            raise DailyError('일별 이력 통신 실패: 중복 방지를 위해 중단') from None
        if response.status_code not in allowed:
            raise DailyError(f'일별 이력 HTTP {response.status_code}: 중복 방지를 위해 중단')
        return response

    def save(self, **changes):
        data = {**self.data, 'version': 1, 'day': self.day, **changes}
        payload = {'message': f'Update daily delivery {self.day} [skip ci]', 'branch': 'main',
                   'content': base64.b64encode(json.dumps(data, ensure_ascii=False, indent=2).encode()).decode()}
        if self.sha:
            payload['sha'] = self.sha
        response = self.request('put', json=payload)
        try:
            self.sha = response.json()['content']['sha']
        except (KeyError, ValueError, TypeError):
            raise DailyError('일별 이력 저장 응답 불명확: 재전송 중단') from None
        self.data = data

    def claim(self, run_id, at):
        if self.data.get('status') == 'completed' or self.data.get('run_id'):
            return False  # Never retry ambiguous/failed deliveries automatically.
        self.save(status='running', run_id=run_id, started_at=at)
        return True


def session():
    token = os.getenv('GITHUB_TOKEN', '').strip()
    if not token:
        raise DailyError('GitHub 인증 없음')
    s = requests.Session()
    s.headers.update({'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                      'User-Agent': 'app-marketing-daily-guard'})
    return s


def delivery(s, now=None):
    import bot
    now = now or datetime.now(KST)
    day = now.astimezone(KST).date().isoformat()
    ledger = Ledger(s, day)
    run_id = os.environ['GITHUB_RUN_ID']
    if not ledger.claim(run_id, now.isoformat()):
        print(f'{day}: 오늘 발송 완료 또는 실행권 확보됨 · 중복 검색/AI/전송 생략')
        return 0
    if os.getenv('DAILY_SCHEDULED') == 'true':
        os.environ['BOT_SEND_AT'] = now.astimezone(KST).replace(hour=9, minute=0, second=0, microsecond=0).isoformat()
    result = bot.entrypoint([])
    ledger.save(status='completed' if result == 0 else 'failed',
                completed_at=datetime.now(KST).isoformat(), exit_code=result)
    return result


def watchdog(s, now=None, dry_run=False):
    import bot
    now = now or datetime.now(KST)
    local = now.astimezone(KST)
    day = local.date().isoformat()
    if (local.hour, local.minute) < (9, 5):
        print('09:05 KST 이전: 누락 판정 보류')
        return 0
    ledger = Ledger(s, day)
    if ledger.data.get('status') == 'completed':
        print(f'{day}: 오늘 발송 완료 확인 · 알림/AI/재실행 없음')
        return 0
    if ledger.data.get('alerted_at'):
        print(f'{day}: 누락 알림 이미 시도됨 · 중복 알림 없음')
        return 0
    missing = not ledger.data.get('run_id')
    detail = '오늘 발송 실행이 시작되지 않았습니다.' if missing else '오늘 발송이 시작됐지만 완료되지 않았습니다. 중복 전송 방지를 위해 자동 재시도하지 않습니다.'
    print(detail)
    if dry_run:
        return 0
    # Reserve BEFORE messaging. Unknown delivery results are not retried.
    ledger.save(alerted_at=local.isoformat(), status=ledger.data.get('status', 'missing'))
    bot.send_telegram(bot.required_env('TELEGRAM_TOKEN'), bot.required_env('TELEGRAM_CHAT_ID'),
                      f'⚠️ 오전 9시 추천 발송 누락·지연 감지\n{day} {local:%H:%M} KST\n{detail}'
                      + ('\n대체 실행을 한 번 요청합니다.' if missing else '') + bot.workflow_link())
    bot.mark_error_notified()
    if missing:
        # Single recovery request. The shared atomic daily claim stops duplicates.
        ledger.save(recovery_requested_at=local.isoformat())
        repo = os.environ['GITHUB_REPOSITORY']
        try:
            r = s.post(f'https://api.github.com/repos/{repo}/actions/workflows/main.yml/dispatches',
                       json={'ref': 'main', 'inputs': {'mode': 'scheduled'}}, timeout=(10, 25))
        except requests.RequestException:
            raise DailyError('대체 실행 요청 결과 불명확: 자동 재시도하지 않음') from None
        if r.status_code != 204:
            raise DailyError(f'대체 실행 요청 실패 HTTP {r.status_code}')
        print('대체 실행 요청 완료 · 오늘 실행권은 한 번만 확보 가능')
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('run', 'watchdog'))
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    try:
        with session() as s:
            return delivery(s) if args.mode == 'run' else watchdog(s, dry_run=args.dry_run)
    except Exception as exc:
        print(str(exc) if isinstance(exc, DailyError) else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
