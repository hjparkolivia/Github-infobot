import base64
from datetime import datetime
import json
import os
import unittest
from unittest.mock import Mock, patch

import bot
import daily

NOW = datetime(2026, 11, 10, 9, 7, tzinfo=daily.KST)
ENV = {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_RUN_ID': '123',
       'TELEGRAM_TOKEN': 'fixture', 'TELEGRAM_CHAT_ID': 'fixture'}


class DailyTests(unittest.TestCase):
    def test_atomic_new_day_claim_and_ambiguous_claim_never_retries(self):
        s = Mock()
        s.get.return_value.status_code = 404
        s.put.return_value = Mock(status_code=201, json=Mock(return_value={'content': {'sha': 'a'}}))
        with patch.dict(os.environ, ENV, clear=True):
            ledger = daily.Ledger(s, '2026-11-10')
            self.assertTrue(ledger.claim('123', NOW.isoformat()))
            self.assertFalse(ledger.claim('456', NOW.isoformat()))
            self.assertEqual(s.put.call_count, 1)

    def test_conflict_or_corruption_blocks_execution(self):
        for response in (Mock(status_code=403), Mock(status_code=200, json=Mock(return_value={'sha':'a','content':'!'}))):
            s = Mock();s.get.return_value = response
            with patch.dict(os.environ, ENV, clear=True), self.assertRaises(daily.DailyError):
                daily.Ledger(s, '2026-11-10')
        s=Mock();s.get.return_value.status_code=404;s.put.return_value.status_code=409
        with patch.dict(os.environ, ENV, clear=True), self.assertRaises(daily.DailyError):
            daily.Ledger(s, '2026-11-10').claim('123', NOW.isoformat())

    def test_completed_or_claimed_day_never_calls_bot_again(self):
        ledger = Mock();ledger.claim.return_value=False
        with patch.dict(os.environ, ENV, clear=True), patch.object(daily, 'Ledger', return_value=ledger), patch.object(bot, 'entrypoint') as run:
            self.assertEqual(daily.delivery(Mock(), NOW), 0)
            run.assert_not_called()

    def test_scheduled_preparation_sets_09_target_and_completes_ledger(self):
        ledger=Mock();ledger.claim.return_value=True;ledger.data={'run_id':'123'}
        early=NOW.replace(hour=8, minute=44)
        with patch.dict(os.environ, {**ENV,'DAILY_SCHEDULED':'true'}, clear=True), patch.object(daily, 'Ledger', return_value=ledger), patch.object(bot, 'entrypoint', return_value=0) as run:
            daily.delivery(Mock(), early)
            self.assertEqual(os.environ['BOT_SEND_AT'], '2026-11-10T09:00:00+09:00')
            run.assert_called_once_with([])
            self.assertEqual(ledger.save.call_args.kwargs['status'], 'completed')

    def test_watchdog_race_retries_only_completion_not_ai_or_delivery(self):
        initial=Mock();initial.claim.return_value=True
        stale=Mock();stale.data={'run_id':'123'};stale.save.side_effect=daily.StateConflict('race')
        fresh=Mock();fresh.data={'run_id':'123','alerted_at':'earlier'}
        with patch.dict(os.environ,ENV,clear=True), patch.object(daily,'Ledger',side_effect=[initial,stale,fresh]), patch.object(bot,'entrypoint',return_value=0) as run:
            self.assertEqual(daily.delivery(Mock(),NOW),0)
            run.assert_called_once_with([])
            fresh.claim.assert_not_called()
            self.assertEqual(fresh.save.call_args.kwargs['status'],'completed')

    def check(self, record, dry_run=False, now=NOW):
        ledger=Mock();ledger.data=record;s=Mock();s.post.return_value.status_code=204
        with patch.dict(os.environ, ENV, clear=True), patch.object(daily, 'Ledger', return_value=ledger), patch.object(bot, 'send_telegram') as send, patch.object(bot, 'mark_error_notified'):
            daily.watchdog(s, now, dry_run=dry_run)
            return ledger,s,send

    def test_missing_run_notifies_and_requests_single_recovery(self):
        ledger,s,send=self.check({})
        send.assert_called_once();self.assertIn('시작되지',send.call_args.args[2]);s.post.assert_called_once()
        self.assertEqual(ledger.save.call_count,2)

    def test_running_or_failed_notifies_but_never_retries(self):
        for status in ('running','failed'):
            _,s,send=self.check({'status':status,'run_id':'123'})
            send.assert_called_once();s.post.assert_not_called()

    def test_completed_already_alerted_and_early_checks_are_quiet(self):
        for data,now in (({'status':'completed'},NOW),({'alerted_at':'earlier'},NOW),({},NOW.replace(hour=8))):
            ledger,s,send=self.check(data,now=now)
            ledger.save.assert_not_called();send.assert_not_called();s.post.assert_not_called()

    def test_dry_run_never_writes_sends_or_dispatches(self):
        ledger,s,send=self.check({},dry_run=True)
        ledger.save.assert_not_called();send.assert_not_called();s.post.assert_not_called()

    def test_send_at_waits_after_preparation_and_rejects_unbounded_wait(self):
        with patch.dict(os.environ, {'BOT_SEND_AT': '2099-01-01T09:00:00+09:00'}, clear=True), patch.object(bot.time,'sleep') as sleep:
            with self.assertRaises(bot.BotError):bot.wait_for_delivery()
            sleep.assert_not_called()
        with patch.dict(os.environ, {'BOT_SEND_AT': '2020-01-01T09:00:00+09:00'}, clear=True), patch.object(bot.time,'sleep') as sleep:
            bot.wait_for_delivery();sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
