"""Offline regression tests: no real Telegram, GitHub or Gemini traffic."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests
import bot
from budget import Decision

ENV = {'TELEGRAM_TOKEN': 'fixture-secret-token', 'TELEGRAM_CHAT_ID': 'fixture-chat', 'GEMINI_KEY': 'fixture-ai-key'}

def repo(i, name=None, stars=100):
    return {'id': i, 'full_name': name or f'owner/tool-{i}', 'stargazers_count': stars,
            'html_url': f'https://github.com/owner/tool-{i}', 'description': 'social media scheduling tool'}

def assessment(i, useful=False):
    return {'id': i, 'useful': useful, 'reason': 'SNS 게시 자동화 가능' if useful else '정보 부족: 활용 경로 확인 불가',
            'summary': 'SNS 게시\n릴스 운영\n비용 확인 필요' if useful else ''}

class Tests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.logs = contextlib.redirect_stdout(self.output);self.logs.__enter__()
        self.err = io.StringIO()
        self.errors = contextlib.redirect_stderr(self.err);self.errors.__enter__()
        budget_patch = patch.object(bot, 'check_budget', return_value=Decision(True, 'fixture', 1, '123'))
        budget_patch.start();self.addCleanup(budget_patch.stop)
        claim_patch = patch.object(bot, 'claim_once')
        claim_patch.start();self.addCleanup(claim_patch.stop)

    def tearDown(self):
        self.logs.__exit__(None, None, None);self.errors.__exit__(None, None, None)

    def test_search_scope_sort_balance_and_job_exclusions(self):
        groups = [
            {'items': [repo(1, 'jobright-ai/2026-Engineering-New-Grad', 9999), repo(2, 'gitroomhq/postiz-app', 800)]},
            {'items': [repo(2, 'gitroomhq/postiz-app', 800), repo(3, 'remotion-dev/remotion', 700)]},
        ]
        with patch.object(bot, 'QUERIES', bot.QUERIES[:2]), patch.object(bot, 'github_get') as get, patch.object(bot.time, 'sleep'):
            get.side_effect = [Mock(json=Mock(return_value=g)) for g in groups]
            result = bot.search_repos(Mock())
            self.assertEqual([r['id'] for r in result], [2, 3])
            for call in get.call_args_list:
                self.assertEqual(call.kwargs['params']['sort'], 'stars')
                self.assertEqual(call.kwargs['params']['per_page'], 10)
                self.assertNotIn('readme', call.kwargs['params']['q'])
        self.assertIn('채용·인턴 공고 목록', self.output.getvalue())
        self.assertIsNone(bot.exclusion_reason(repo(1, 'org/job-scheduler')))

    def test_all_search_errors_fail_instead_of_silent_zero(self):
        with patch.object(bot, 'QUERIES', ['one']), patch.object(bot, 'github_get', side_effect=bot.BotError('GitHub API 오류: HTTP 403')):
            with self.assertRaises(bot.BotError):bot.search_repos(Mock())

    def test_partial_search_failure_keeps_results_and_error(self):
        state = bot.RunState()
        with patch.object(bot, 'QUERIES', ['one', 'two']), patch.object(bot.time, 'sleep'), patch.object(bot, 'github_get') as get:
            get.side_effect = [bot.BotError('HTTP 403'), Mock(json=Mock(return_value={'items': [repo(1)]}))]
            self.assertEqual(len(bot.search_repos(Mock(), state)), 1)
        self.assertEqual(len(state.errors), 1)

    def test_no_candidates_still_sends_start_and_zero_summary(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=[]), patch.object(bot, 'curate') as ai:
            self.assertEqual(bot.entrypoint([]), 0)
            self.assertEqual(send.call_count, 2)
            self.assertIn('추천 0개', send.call_args.args[2]);ai.assert_not_called()

    def test_all_rejected_logs_specific_reason_and_sends_zero_summary(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=[repo(1)]), patch.object(bot, 'github_get', return_value=None), patch.object(bot, 'curate', return_value={1: assessment(1)}):
            self.assertEqual(bot.entrypoint([]), 0)
            self.assertIn('정보 부족: 활용 경로 확인 불가', self.output.getvalue())
            self.assertIn('추천 0개', send.call_args.args[2])

    def test_recommendations_capped_at_three_and_batch_called_once(self):
        candidates = [repo(i) for i in range(4)]
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=candidates), patch.object(bot, 'github_get', return_value=None), patch.object(bot, 'curate', return_value={i: assessment(i, True) for i in range(4)}) as ai:
            self.assertEqual(bot.entrypoint([]), 0)
            self.assertEqual(send.call_count, 5)  # start + 3 recommendations + summary
            self.assertIn('선정 이유', send.call_args_list[1].args[2]);ai.assert_called_once()

    def test_missing_gemini_key_is_notified_without_ai(self):
        with patch.dict(os.environ, {k: v for k, v in ENV.items() if k != 'GEMINI_KEY'}, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=[repo(1)]), patch.object(bot, 'github_get', return_value=None):
            self.assertEqual(bot.entrypoint([]), 1)
            self.assertIn('GEMINI_KEY', send.call_args.args[2])
            self.assertIn('실행 실패', send.call_args.args[2])

    def test_budget_block_sends_unassessed_candidates_without_key_or_ai(self):
        with patch.dict(os.environ, {k: v for k, v in ENV.items() if k != 'GEMINI_KEY'}, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=[repo(1)]), patch.object(bot, 'check_budget', return_value=Decision(False, '월 보호 한도 도달', 50)), patch.object(bot, 'curate') as ai, patch.object(bot, 'github_get') as readme:
            self.assertEqual(bot.entrypoint([]), 0)
            messages='\n'.join(c.args[2] for c in send.call_args_list)
            self.assertIn('AI 미평가 검색 후보', messages)
            self.assertIn('5,000/5,000', messages)
            ai.assert_not_called();readme.assert_not_called()

    def test_sdk_request_has_one_attempt_bounded_output_and_no_paid_tools(self):
        google, genai = ModuleType('google'), ModuleType('google.genai')
        google.genai = genai
        genai.errors = SimpleNamespace(APIError=type('FixtureAPIError', (Exception,), {}))
        genai.types = SimpleNamespace(**{name: Mock(side_effect=lambda **values: values)
            for name in ('HttpOptions','HttpRetryOptions','GenerateContentConfig','ThinkingConfig')})
        client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
        client.models.generate_content.return_value=SimpleNamespace(text=json.dumps([assessment(1,True)]),usage_metadata=None)
        genai.Client=Mock(return_value=client)
        with patch.dict(sys.modules,{'google':google,'google.genai':genai}):
            bot.curate('fixture-key',bot.MODEL,[{**repo(1),'excerpt':'長い説明'*10000}])
        client.models.generate_content.assert_called_once()
        request=client.models.generate_content.call_args.kwargs
        self.assertEqual(request['config']['max_output_tokens'],6144)
        self.assertNotIn('tools',request['config'])
        self.assertLessEqual(len(request['contents'].encode('utf-8')),16000)
        options=genai.Client.call_args.kwargs
        self.assertEqual(options['http_options']['retry_options']['attempts'],1)
        self.assertIs(options['vertexai'],False)

    def test_ai_quota_error_notifies_and_keeps_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            output = str(Path(temp)/'actions-output')
            with patch.dict(os.environ, {**ENV, 'GITHUB_OUTPUT': output}, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'search_repos', return_value=[repo(1)]), patch.object(bot, 'github_get', return_value=None), patch.object(bot, 'curate', side_effect=bot.BotError('Gemini API 오류: 429')):
                self.assertEqual(bot.entrypoint([]), 1)
                self.assertIn('429', send.call_args.args[2])
                self.assertIn('error_notified=true', Path(output).read_text())

    def test_reject_missing_duplicate_or_foreign_ai_ids_and_missing_reason(self):
        for data in ([], [assessment(1), assessment(1)], [assessment(3)], [{**assessment(1), 'reason': ''}]):
            with self.subTest(data=data), self.assertRaises(bot.BotError):bot.validate_assessments(data, [repo(1)])
        self.assertEqual(bot.validate_assessments([assessment(1)], [repo(1)])[1]['useful'], False)

    def test_telegram_failures_have_actionable_safe_messages(self):
        for code, expected in ((400, 'CHAT_ID'), (401, 'TOKEN'), (403, '차단')):
            response = Mock(status_code=code, json=Mock(return_value={'ok': False, 'error_code': code}))
            with patch.object(bot.requests, 'post', return_value=response), self.assertRaisesRegex(bot.BotError, expected):
                bot.send_telegram('secret', 'chat', 'test')

    def test_telegram_transient_retry_is_bounded(self):
        busy = Mock(status_code=429, json=Mock(return_value={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 999}}))
        ok = Mock(status_code=200, json=Mock(return_value={'ok': True}))
        with patch.object(bot.requests, 'post', side_effect=[busy, ok]) as post, patch.object(bot.time, 'sleep') as sleep:
            bot.send_telegram('secret', 'chat', 'test')
            self.assertEqual(post.call_count, 2);sleep.assert_called_once_with(20)

    def test_token_bearing_exceptions_never_enter_logs_or_alerts(self):
        error = requests.ConnectionError('https://api.telegram.org/botTOP_SECRET/sendMessage')
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot.requests, 'post', side_effect=error):
            self.assertEqual(bot.entrypoint(['--test-telegram']), 1)
        self.assertNotIn('TOP_SECRET', self.output.getvalue()+self.err.getvalue())
        self.assertIn('오류 알림도 전송 실패', self.err.getvalue())

    def test_telegram_only_test_never_calls_search_or_ai(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'curate') as ai, patch.object(bot, 'search_repos') as search:
            self.assertEqual(bot.entrypoint(['--test-telegram']), 0)
            send.assert_called_once();ai.assert_not_called();search.assert_not_called()

    def test_intentional_failure_test_notifies_without_paid_calls(self):
        with patch.dict(os.environ, ENV, clear=True), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'curate') as ai:
            self.assertEqual(bot.entrypoint(['--test-error']), 1)
            self.assertIn('의도적 오류', send.call_args.args[2]);ai.assert_not_called()

    def test_no_telegram_credentials_logs_why_notification_is_impossible(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(bot, 'send_telegram') as send:
            self.assertEqual(bot.entrypoint([]), 1);send.assert_not_called()
            self.assertIn('오류 알림 불가', self.err.getvalue())

    def test_utf16_message_bound_for_emoji(self):
        with patch.object(bot.requests, 'post', return_value=Mock(status_code=200, json=Mock(return_value={'ok': True}))) as post:
            bot.send_telegram('secret', 'chat', '🪐'*4000)
            self.assertLessEqual(len(post.call_args.kwargs['json']['text'].encode('utf-16-le')), 7000)

if __name__ == '__main__':
    unittest.main()
