import base64
import json
import os
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone, timedelta

from history import History, HistoryError


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo', 'GITHUB_TOKEN': 'fixture'})
        self.env.start();self.addCleanup(self.env.stop)
        self.session = Mock()
        content = base64.b64encode(json.dumps({'version': 1, 'repos': {'owner/old': {'status': 'sent'}}}).encode()).decode()
        self.session.get.return_value = Mock(status_code=200, json=Mock(return_value={'sha': 'first', 'content': content}))
        self.session.put.side_effect = [Mock(status_code=200, json=Mock(return_value={'content': {'sha': sha}})) for sha in ('second', 'third')]

    def test_persistent_reserve_confirm_and_case_insensitive_duplicate(self):
        history = History(self.session)
        self.assertTrue(history.seen({'full_name': 'OWNER/OLD'}))
        repo = {'full_name': 'Owner/New'}
        history.reserve(repo)
        self.assertTrue(history.seen(repo))
        self.assertEqual(history.data['repos']['owner/new']['status'], 'reserved')
        history.confirm(repo)
        self.assertEqual(history.data['repos']['owner/new']['status'], 'sent')
        self.assertEqual(self.session.put.call_args_list[1].kwargs['json']['sha'], 'second')
        with self.assertRaises(HistoryError):
            history.reserve(repo)
        self.assertEqual(self.session.put.call_count, 2)

    def test_missing_corrupt_or_conflicted_history_never_resets(self):
        self.session.get.return_value.status_code = 404
        with self.assertRaises(HistoryError):History(self.session)
        self.session.get.return_value = Mock(status_code=200, json=Mock(return_value={'sha': 'x', 'content': '!!!!'}))
        with self.assertRaises(HistoryError):History(self.session)
        self.session.put.assert_not_called()

    def test_conflict_prevents_delivery_reservation(self):
        history = History(self.session)
        self.session.put.side_effect = None
        self.session.put.return_value = Mock(status_code=409)
        with self.assertRaises(HistoryError):history.reserve({'full_name': 'owner/new'})

    def test_reminders_wait_fourteen_days_and_never_retry_ambiguous_sends(self):
        history = History(self.session)
        repo = {'full_name': 'owner/old', 'repeat_candidate': True}
        now = datetime.now(timezone.utc)
        for days, status, expected in ((13, 'sent', False), (14, 'sent', True), (90, 'reserved', False)):
            history.data['repos']['owner/old'] = {'status': status, 'at': (now - timedelta(days=days)).isoformat()}
            self.assertIs(history.repeat_due(repo, now=now), expected)
        history.data['repos']['owner/old'] = {'status': 'sent', 'at': (now - timedelta(days=15)).isoformat()}
        history.reserve(repo)
        self.assertFalse(history.repeat_due(repo))
        with self.assertRaises(HistoryError):history.reserve(repo)


if __name__ == '__main__':
    unittest.main()
