"""Dependency-free failure notification; never log token-bearing URLs."""
import json
import os
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def main():
    token, chat = (os.getenv(k, '').strip() for k in ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID'))
    if not token or not chat:
        print('Telegram 알림 인증정보 없음', file=sys.stderr)
        return 1
    link = f"https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    body = json.dumps({'chat_id': chat, 'text': '🚨 추천 발송 누락 감시기 실행 실패\n의존성·GitHub 이력·감시 단계 확인이 필요합니다.\n' + link,
                       'link_preview_options': {'is_disabled': True}}).encode()
    try:
        req = Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body,
                      headers={'Content-Type': 'application/json'}, method='POST')
        with urlopen(req, timeout=25) as r:
            if not json.load(r).get('ok'):
                raise ValueError()
    except HTTPError as exc:
        print(f'Telegram 알림 실패 HTTP {exc.code}', file=sys.stderr)
        return 1
    except Exception as exc:
        print('Telegram 알림 실패: ' + type(exc).__name__, file=sys.stderr)
        return 1
    print('감시기 오류 알림 전송 완료')
    return 0


if __name__ == '__main__':
    sys.exit(main())
