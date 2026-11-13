"""目录快照的重启恢复、更新语义、账号隔离和写入失败保护；仅使用合成数据。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.directory_cache import DirectoryCache

ACCOUNT = 'wxid_cache_demo'
OTHER = 'wxid_cache_other'


def snapshot():
    return {'account': ACCOUNT, 'session_id': 'connection-not-for-disk',
            'source': 'wechat_cache', 'scanned_at': '2026-01-01T12:00:00+08:00', 'complete': False,
            'contacts': [{'wxid': 'wxid_cache_peer', 'nickname': '测试好友', 'remark': '测试备注',
                          'alias': 'demo_alias', 'description': '描述', 'sex': 'female',
                          'signature': '签名', 'country': 'CN', 'province': '测试省', 'city': '测试市',
                          'tag_ids': ['7'], 'tags': ['演示标签'], 'tags_status': 'ready',
                          'raw_xml': 'not-for-disk', 'token': 'not-for-disk'}],
            'tags': [{'id': '7', 'name': '演示标签'}]}


class DirectoryCacheTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = DirectoryCache(self.root)

    def test_restart_restores_fields_without_connection_or_raw_payloads(self):
        original = snapshot()
        self.cache.save_contacts(ACCOUNT, original)
        restored = DirectoryCache(self.root).contacts(ACCOUNT)
        for key in ('nickname', 'remark', 'alias', 'description', 'sex', 'signature',
                    'country', 'province', 'city', 'tag_ids', 'tags', 'tags_status'):
            self.assertEqual(restored['contacts'][0][key], original['contacts'][0][key])
        self.assertEqual(restored['tags'], original['tags'])
        self.assertEqual(restored['session_id'], '')
        self.assertTrue(restored['cached'])
        raw = self.cache.path(ACCOUNT, 'contacts').read_text(encoding='utf-8')
        self.assertNotIn('connection-not-for-disk', raw)
        self.assertNotIn('not-for-disk', raw)
        self.assertNotIn('session_id', raw)
        self.assertEqual(original, snapshot())

    def test_successful_scan_replaces_contacts_and_tags_including_removals(self):
        self.cache.save_contacts(ACCOUNT, snapshot())
        replacement = dict(snapshot(), contacts=[{'wxid': 'wxid_new_peer', 'nickname': '新好友'}], tags=[])
        self.cache.save_contacts(ACCOUNT, replacement)
        restored = self.cache.contacts(ACCOUNT)
        self.assertEqual([row['wxid'] for row in restored['contacts']], ['wxid_new_peer'])
        self.assertEqual(restored['tags'], [])
        self.cache.save_contacts(ACCOUNT, dict(snapshot(), contacts=[], tags=[]))
        self.assertEqual(self.cache.contacts(ACCOUNT)['contacts'], [])

    def test_accounts_have_independent_files_and_unknown_account_stays_empty(self):
        self.cache.save_contacts(ACCOUNT, snapshot())
        self.cache.remember_groups(ACCOUNT, [{'chat_id': '101@chatroom', 'name': '演示群'}])
        self.assertIsNone(self.cache.contacts(OTHER))
        self.assertEqual(self.cache.groups(OTHER), [])
        self.cache.save_contacts(OTHER, dict(snapshot(), account=OTHER, contacts=[], tags=[]))
        self.assertNotEqual(self.cache.path(ACCOUNT, 'contacts'), self.cache.path(OTHER, 'contacts'))
        self.assertEqual(len(self.cache.contacts(ACCOUNT)['contacts']), 1)
        self.assertEqual(self.cache.contacts(OTHER)['contacts'], [])

    def test_invalid_snapshots_cannot_replace_valid_file(self):
        self.cache.save_contacts(ACCOUNT, snapshot())
        path = self.cache.path(ACCOUNT, 'contacts')
        before = path.read_bytes()
        duplicate = snapshot()
        duplicate['contacts'] *= 2
        for data in (dict(snapshot(), account=OTHER), duplicate, dict(snapshot(), scanned_at='no-time'),
                     dict(snapshot(), complete='yes'), dict(snapshot(), tags=None)):
            with self.subTest(data=data), self.assertRaises((ValueError, TypeError)):
                self.cache.save_contacts(ACCOUNT, data)
            self.assertEqual(path.read_bytes(), before)

    def test_interrupted_replace_retains_old_snapshot_and_removes_temporary_file(self):
        self.cache.save_contacts(ACCOUNT, snapshot())
        path = self.cache.path(ACCOUNT, 'contacts')
        before = path.read_bytes()
        with patch('wxbot_framework.directory_cache.os.replace', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                self.cache.save_contacts(ACCOUNT, dict(snapshot(), contacts=[], tags=[]))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.glob('.directory-*.tmp')), [])

    def test_corrupt_or_wrong_account_files_are_ignored(self):
        path = self.cache.path(ACCOUNT, 'contacts')
        path.parent.mkdir(parents=True)
        for raw in ('{', '[]', json.dumps({'version': 1, 'account': OTHER, 'snapshot': snapshot()}),
                    json.dumps({'version': True, 'account': ACCOUNT, 'snapshot': snapshot()}),
                    json.dumps({'version': 1, 'account': ACCOUNT, 'snapshot': None}),
                    json.dumps({'version': 1, 'account': ACCOUNT, 'snapshot': []}),
                    json.dumps({'version': 1, 'account': ACCOUNT, 'snapshot': {'contacts': []}})):
            path.write_text(raw, encoding='utf-8')
            self.assertIsNone(self.cache.contacts(ACCOUNT))
        self.cache.save_contacts(ACCOUNT, snapshot())
        self.assertIsNotNone(self.cache.contacts(ACCOUNT))

    def test_discovered_groups_merge_by_id_and_preserve_name_on_partial_scan(self):
        self.cache.remember_groups(ACCOUNT, [{'chat_id': '101@chatroom', 'name': '旧名称', 'last_text': 'not-for-disk'},
                                             {'chat_id': '102@chatroom', 'name': '保留群'}])
        rows = self.cache.remember_groups(ACCOUNT, [{'chat_id': '101@chatroom', 'name': '新名称'},
                                                   {'chat_id': '103@chatroom', 'name': '新增群'},
                                                   {'chat_id': 'wxid_cache_peer', 'name': '不是群'}])
        self.assertEqual([row['chat_id'] for row in rows], ['101@chatroom', '102@chatroom', '103@chatroom'])
        result = self.cache.remember_groups(ACCOUNT, [{'chat_id': '101@chatroom'}])
        self.assertEqual(result[0]['name'], '新名称')
        self.assertEqual(DirectoryCache(self.root).groups(ACCOUNT), result)
        self.assertNotIn('not-for-disk', self.cache.path(ACCOUNT, 'groups').read_text(encoding='utf-8'))

    def test_unchanged_scan_does_not_rewrite_groups(self):
        rows = [{'chat_id': '101@chatroom', 'name': '演示群'}]
        self.cache.remember_groups(ACCOUNT, rows)
        with patch.object(self.cache, '_save') as save:
            self.assertEqual(self.cache.remember_groups(ACCOUNT, rows), self.cache.groups(ACCOUNT))
        save.assert_not_called()

    def test_cache_cannot_change_account_into_path_or_read_another_kind(self):
        for account in ('../escape', '', '101@chatroom', None):
            with self.subTest(account=account), self.assertRaises(ValueError):
                self.cache.contacts(account)
        with self.assertRaises(ValueError):
            self.cache.path(ACCOUNT, '../contacts')

    def test_invalid_group_file_falls_back_to_empty_list(self):
        path = self.cache.path(ACCOUNT, 'groups')
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({'version': 1, 'account': ACCOUNT,
                                    'groups': [{'chat_id': '../escape'}]}), encoding='utf-8')
        self.assertEqual(self.cache.groups(ACCOUNT), [])
        self.cache.remember_groups(ACCOUNT, [{'chat_id': '101@chatroom', 'name': '修复后的目录'}])
        self.assertEqual(len(self.cache.groups(ACCOUNT)), 1)
