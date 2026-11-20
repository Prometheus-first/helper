"""New-post eligibility, durable claims, reconnects and cancellation; no WeChat."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.moments_auto_comment import MomentsAutoComment, validate
from wxbot_framework.panel_common import PanelError
from wxbot_framework.web_panel import atomic_json, read_json


def row(identifier, timestamp, **overrides):
    return dict(sns_id=str(identifier), timestamp=timestamp, author='wxid_fixture_friend',
                is_self=False, private=False, comments=[]) | overrides


class AutoCommentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1700000000.25
        self.session = {'session_id': 'offline_session', 'account': 'wxid_fixture_robot'}
        self.rows, self.sent, self.outcome = [], [], 'completed'
        self.controller = SimpleNamespace(workspace=Path(self.directory.name),
            _moments_session=lambda: deepcopy(self.session),
            moments_connection=lambda: {'comment_supported': True},
            read_moments=lambda body: {'rows': deepcopy(self.rows)},
            interact_moments=self.send)
        self.auto = MomentsAutoComment(self.controller, atomic_json, read_json, clock=lambda: self.now)
        self.auto.start = Mock()  # All scans use the deterministic production run_once path.
        self.addCleanup(self.auto.close)

    def configure(self, **overrides):
        return self.auto.configure(dict(enabled=True, content='固定评论 👍', interval_seconds=60,
            authors=[], session_id='offline_session') | overrides)

    def send(self, payload, *, _guard):
        if not _guard():
            return {'status': 'failed'}
        path, posts = self.auto._history(self.session['account'])
        self.assertEqual(posts[payload['sns_id']]['status'], 'claimed')
        self.sent.append(deepcopy(payload))
        return {'status': self.outcome}

    def baseline(self):
        self.rows = [row(1, int(self.now)-10)]
        self.configure()
        self.auto.run_once()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.auto.view()['phase'], 'baseline')

    def advance(self, seconds=61):
        self.now += seconds
        self.auto.run_once()

    def test_disabled_default_never_reads_or_sends(self):
        self.controller.read_moments = Mock()
        self.auto.run_once()
        self.controller.read_moments.assert_not_called()
        self.assertFalse(self.auto.view()['enabled'])

    def test_initial_feed_skipped_new_post_sent_once_with_durable_claim(self):
        self.baseline()
        self.rows.insert(0, row(2, int(self.now)+1))
        self.advance()
        self.advance()
        self.assertEqual([item['sns_id'] for item in self.sent], ['2'])
        self.assertEqual(self.sent[0]['content'], '固定评论 👍')
        self.assertEqual(self.auto.view()['completed'], 1)

    def test_self_old_private_and_already_commented_posts_are_skipped(self):
        self.baseline()
        at = int(self.now)+1
        self.rows = [row(2, at, is_self=True), row(3, at, private=True), row(4, at-4000),
                     row(5, at, comments=[{'username': self.session['account']}]), row(6, at)]
        self.advance()
        self.assertEqual([item['sns_id'] for item in self.sent], ['6'])

    def test_author_allowlist_applies_only_to_new_posts(self):
        self.configure(authors=['wxid_allowed_fixture'])
        self.auto.run_once()
        at = int(self.now)+1
        self.rows = [row(1, at), row(2, at, author='wxid_allowed_fixture')]
        self.advance()
        self.assertEqual([item['sns_id'] for item in self.sent], ['2'])

    def test_same_second_after_baseline_is_not_lost(self):
        self.baseline()
        self.rows.insert(0, row(2, int(self.now)))
        self.advance()
        self.assertEqual(len(self.sent), 1)

    def test_reconnect_and_restart_do_not_replay_current_feed_or_old_claim(self):
        self.baseline()
        self.rows.insert(0, row(2, int(self.now)+1))
        self.advance()
        self.session['session_id'] = 'another_session'
        self.rows.insert(0, row(3, int(self.now)+1))
        self.advance()
        self.assertEqual(len(self.sent), 1)
        replacement = MomentsAutoComment(self.controller, atomic_json, read_json, clock=lambda: self.now)
        self.addCleanup(replacement.close)
        replacement.run_once()
        self.assertEqual(replacement.view()['phase'], 'baseline')
        self.assertEqual(len(self.sent), 1)

    def test_pending_posts_are_batched_and_do_not_get_lost(self):
        self.baseline()
        self.rows = [row(i, int(self.now)+1) for i in range(2, 7)]
        self.advance()
        self.assertEqual(len(self.sent), 3)
        self.advance()
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(len({item['sns_id'] for item in self.sent}), 5)

    def test_unknown_result_pauses_and_is_not_retried(self):
        self.baseline()
        self.outcome = 'unknown'
        self.rows.insert(0, row(2, int(self.now)+1))
        self.advance()
        self.advance()
        self.assertEqual(len(self.sent), 1)
        self.assertFalse(self.auto.view()['enabled'])
        self.assertEqual(self.auto.view()['phase'], 'paused')
        self.configure()
        self.auto.run_once()
        self.advance()
        self.assertEqual(len(self.sent), 1)

    def test_account_switch_cannot_send_using_previous_account_settings(self):
        self.baseline()
        self.session['account'] = 'wxid_different_fixture'
        self.rows.insert(0, row(2, int(self.now)+1))
        self.advance()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.auto.view()['phase'], 'waiting_connection')

    def test_disabling_while_reading_discards_result(self):
        self.baseline()
        def read(_):
            self.configure(enabled=False)
            return {'rows': [row(2, int(self.now))]}
        self.controller.read_moments = read
        self.advance()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.auto.view()['enabled'])

    def test_storage_failure_prevents_any_submission(self):
        self.baseline()
        self.rows.insert(0, row(2, int(self.now)+1))
        self.auto.write = Mock(side_effect=OSError('offline storage fixture'))
        self.advance()
        self.assertEqual(self.sent, [])

    def test_corrupt_history_blocks_send_instead_of_resetting_it(self):
        self.baseline()
        path, _ = self.auto._history(self.session['account'])
        path.write_text('{bad fixture', encoding='utf-8')
        self.rows.insert(0, row(2, int(self.now)+1))
        self.advance()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.auto.view()['phase'], 'error')

    def test_final_dispatch_guard_blocks_disable_after_read(self):
        self.baseline()
        self.rows.insert(0, row(2, int(self.now)+1))
        real_send = self.controller.interact_moments
        def send(payload, **kwargs):
            self.configure(enabled=False)
            return real_send(payload, **kwargs)
        self.controller.interact_moments = send
        self.advance()
        self.assertEqual(self.sent, [])

    def test_invalid_configuration_and_unsupported_backend(self):
        for bad in ({'enabled': 1}, {'enabled': True, 'content': ''}, {'interval_seconds': 1},
                    {'content': '\ud800'}, {'content': '字'*342}, {'authors': ['invalid room@chatroom']},
                    {'content': '\0'}, {'authors': 'all'}, {'unknown': True}):
            with self.subTest(bad=repr(bad)), self.assertRaises(PanelError):
                validate(bad)
        self.controller.moments_connection = lambda: {'comment_supported': False}
        with self.assertRaises(PanelError):
            self.configure()


if __name__ == '__main__':
    unittest.main()
