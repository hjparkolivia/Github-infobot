"""Fail-closed cost controls for THIS bot, not an account-wide billing cap.

No cache/file counter reset: all repository workflow runs in the KST month
consume a conservative slot, including failed/test runs. Re-runs cannot use AI.
No paid call is made if history, official prices or FX cannot be verified.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from decimal import Decimal
import json
import os
from pathlib import Path
import re

import requests

MODEL = 'gemini-3.5-flash-lite'
MONTHLY_RESERVE_KRW = 5000
REQUEST_RESERVE_KRW = 100
MAX_MONTHLY_RUNS = MONTHLY_RESERVE_KRW // REQUEST_RESERVE_KRW
MAX_INPUT_BYTES = 24000
INPUT_TOKEN_CEILING = 32000  # conservative allowance above the entire bounded UTF-8 request
MAX_OUTPUT_TOKENS = 6144  # includes thinking tokens; no extra candidates/tools/cache
MAX_FX = Decimal('2000')
FEE_BUFFER = Decimal('1.25')
MAX_INPUT_USD_PER_M = Decimal('0.30')
MAX_OUTPUT_USD_PER_M = Decimal('2.50')
PRICING_URL = 'https://ai.google.dev/gemini-api/docs/pricing.md'
FX_URL = 'https://api.frankfurter.dev/v1/latest'
KST = timezone(timedelta(hours=9))

@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    slots: int = 0
    run_id: str = ''

    @property
    def note(self):
        return (f'AI 보호예산: {self.slots * REQUEST_RESERVE_KRW:,}/{MONTHLY_RESERVE_KRW:,}원 '
                f'(월 실행 이력에 따른 보수적 예약액, 실제 청구액 아님) · {self.reason}')

def month_window(now):
    local = now.astimezone(KST)
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    next_month = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    end = next_month - timedelta(seconds=1)
    fmt = lambda value: value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return fmt(start) + '..' + fmt(end)

def standard_prices(text):
    match = re.search(r'^## Gemini 3\.5 Flash-Lite\s+.*$', text, re.M)
    if not match:
        raise ValueError('모델 가격표 없음')
    section = text[match.end():].split('\n## ', 1)[0]
    standard = section.split('{% tab title="Standard" %}', 1)[1].split('{% /tab %}', 1)[0]
    def amount(label):
        found = re.search(r'\* ' + label + r'[^\n]*\n\s*\* [^\n]*\n\s*\* \$(\d+(?:\.\d+)?)', standard)
        if not found:
            raise ValueError('표준 텍스트 가격 확인 불가')
        return Decimal(found.group(1))
    return amount('Input price'), amount('Output price')

def check_budget(model, *, now=None, session_factory=requests.Session, external_get=requests.get):
    now = now or datetime.now(timezone.utc)
    if model != MODEL:
        return Decision(False, '허용하지 않은 모델: AI 호출 차단')
    repo, run_id, attempt = (os.getenv(k, '') for k in ('GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT'))
    token = os.getenv('GITHUB_TOKEN', '').strip()
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not run_id.isdigit() or not token:
        return Decision(False, '월 실행 이력을 검증할 수 없는 환경: 무료 후보 안내만 사용')
    if attempt != '1':
        return Decision(False, '워크플로 재실행은 AI 호출 금지: 무료 후보 안내만 사용', run_id=run_id)
    stage, count = 'GitHub 실행 이력', 0
    try:
        # This authenticated session is NEVER used for Google/FX requests.
        with session_factory() as session:
            session.headers.update({'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
                                    'User-Agent': 'app-marketing-budget-guard'})
            response = session.get(f'https://api.github.com/repos/{repo}/actions/runs/{run_id}', timeout=(10, 20))
            response.raise_for_status();current = response.json()
            created = datetime.fromisoformat(current['created_at'].replace('Z', '+00:00'))
            if (current['id'] != int(run_id) or current['run_attempt'] != 1
                    or current['head_branch'] != 'main'
                    or current['event'] not in ('schedule', 'workflow_dispatch')
                    or created.astimezone(KST).strftime('%Y-%m') != now.astimezone(KST).strftime('%Y-%m')):
                return Decision(False, '현재 실행 식별·월 경계 대조 실패', run_id=run_id)
            response = session.get(f'https://api.github.com/repos/{repo}/actions/runs',
                params={'created': month_window(now), 'per_page': 1}, timeout=(10, 20))
            response.raise_for_status();count = response.json().get('total_count')
        if type(count) is not int or count < 1:
            return Decision(False, '이번 달 실행 이력 누락: AI 호출 차단', run_id=run_id)
        if count > MAX_MONTHLY_RUNS:
            return Decision(False, f'월 {MAX_MONTHLY_RUNS}회 보호 한도 도달: 추가 AI 호출 차단', min(count, MAX_MONTHLY_RUNS), run_id)
        stage = 'Google 공식 요금표'
        response = external_get(PRICING_URL, timeout=(10, 20))
        response.raise_for_status();input_price, output_price = standard_prices(response.text)
        if input_price > MAX_INPUT_USD_PER_M or output_price > MAX_OUTPUT_USD_PER_M:
            return Decision(False, '공식 모델 요금 인상 감지: AI 호출 차단', count, run_id)
        stage = 'USD/KRW 환율'
        response = external_get(FX_URL, params={'base': 'USD', 'symbols': 'KRW'}, timeout=(10, 20))
        response.raise_for_status();fx = response.json()
        age = (now.date() - date.fromisoformat(fx['date'])).days
        rate = Decimal(str(fx['rates']['KRW']))
        if fx['base'] != 'USD' or not 0 <= age <= 7 or not Decimal('0') < rate <= MAX_FX:
            return Decision(False, '환율·데이터 유효기간 보호 기준 초과: AI 호출 차단', count, run_id)
        worst = ((INPUT_TOKEN_CEILING * input_price + MAX_OUTPUT_TOKENS * output_price)
                 / Decimal('1000000') * MAX_FX * FEE_BUFFER)
        if worst > REQUEST_RESERVE_KRW:
            return Decision(False, '입력·출력 최대 비용이 1회 보호액 초과: AI 호출 차단', count, run_id)
        return Decision(True, f'1회 최대 환산 {worst:.2f}원 이내 · 재시도 없음', count, run_id)
    except Exception as exc:
        # URLs/credentials from HTTP exception bodies are never returned.
        status = getattr(getattr(exc, 'response', None), 'status_code', None)
        error = f'{type(exc).__name__}' + (f' HTTP {status}' if status else '')
        return Decision(False, f'{stage} 확인 실패({error}): AI 호출 차단',
                        min(count, MAX_MONTHLY_RUNS) if type(count) is int else 0, run_id)

def claim_once(decision, root=None):
    if not decision.allowed or not decision.run_id.isdigit():
        raise RuntimeError('유효한 비용 승인이 없습니다.')
    folder = Path(root) if root else Path(__file__).resolve().parent / '.budget-runtime'
    folder.mkdir(exist_ok=True, parents=True)
    # A second process/call in the SAME runner also cannot bill again.
    path = folder / ('claimed-' + decision.run_id + '.json')
    with path.open('x', encoding='utf-8') as file:
        json.dump({'run_id': decision.run_id, 'reserved_krw': REQUEST_RESERVE_KRW,
                   'model': MODEL, 'monthly_slots': decision.slots}, file)

def trim_utf8(text, length):
    return text.encode('utf-8')[:length].decode('utf-8', errors='ignore')

def payload_for(repos):
    payload = [{'id': r['id'], 'repository': r['full_name'],
                'description': trim_utf8(r.get('description') or '', 500),
                'topics': [trim_utf8(str(t), 50) for t in r.get('topics', [])[:8]],
                'readme_excerpt': trim_utf8(r.get('excerpt', ''), 2000)} for r in repos]
    def encode():return json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    data = encode()
    while len(data.encode('utf-8')) > 16000:
        largest = max(payload, key=lambda p: len(p['readme_excerpt'].encode('utf-8')))
        old = len(largest['readme_excerpt'].encode('utf-8'))
        if old == 0:
            raise ValueError('AI 입력 메타데이터가 비용 보호 한도를 넘었습니다.')
        largest['readme_excerpt'] = trim_utf8(largest['readme_excerpt'], max(0, old - 256))
        data = encode()
    return data
