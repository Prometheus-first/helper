"""消息监控使用内存账号、模拟服务与本机测试 HTTP；不连接或启动微信。"""
import http.client
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock

from test_robot_hub import FakeWorker
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.robot_hub import RobotHub
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, ServiceFacade
from wxbot_framework.web_panel import PanelServer, redact

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'wxid_monitor_robot'
FRIEND = 'wxid_monitor_friend'
ROOM = '12345678@chatroom'


class BufferTests(unittest.TestCase):
    def test_text_keeps_conversation_sender_and_multiline_content(self):
        buffer = MessageBuffer(clock=lambda: 1792540800)
        buffer.append(Message('第一行\n第二行', ROOM, FRIEND, is_group=True), account=ACCOUNT, sender_name='群友甲')
        row = buffer.tail()['rows'][0]
        self.assertEqual((row['account'], row['chat_id'], row['sender_id'], row['sender_name']), (ACCOUNT, ROOM, FRIEND, '群友甲'))
        self.assertEqual(row['content'], '第一行\n第二行')
        self.assertTrue(row['is_group'])
        self.assertRegex(row['time'], r'^\d\d:\d\d:\d\d$')

    def test_long_text_is_complete_in_monitor_and_secrets_are_redacted(self):
        from wxbot_framework.panel_common import redact_full
        buffer = MessageBuffer(sanitize=redact_full)
        body = '消息🌸\n' * 700
        buffer.append(Message(body, ROOM, FRIEND, is_group=True))
        self.assertEqual(buffer.tail()['rows'][0]['content'], body)
        longer = '中' * 10000 + 'sk-' + 'example_redaction_not_a_real_key_123456' + '末尾'
        buffer.append(Message(longer, ROOM, FRIEND, is_group=True))
        shown = buffer.tail()['rows'][-1]['content']
        self.assertEqual(shown, '中' * 10000 + '[密钥已隐藏]末尾')

    def test_full_monitor_batch_fits_worker_transport_with_supplementary_characters(self):
        buffer = MessageBuffer()
        for _ in range(200):
            buffer.append(Message('🌸' * 5000, ROOM, FRIEND, is_group=True), account=ACCOUNT)
        payload = json.dumps(buffer.tail(), ensure_ascii=False).encode('utf-8')
        self.assertLess(len(payload), 4 * 1024 * 1024)

    def test_long_message_batches_preserve_full_text_and_cursor_order(self):
        buffer = MessageBuffer()
        body = '🌸' * 30000 + '末尾'
        buffer.append(Message('起点', ROOM, FRIEND, is_group=True))
        cursor = buffer.tail()['cursor']
        for number in range(50):
            buffer.append(Message(f'{number}:' + body, ROOM, FRIEND, is_group=True))
        received = []
        while cursor < buffer.sequence:
            batch = buffer.tail(cursor)
            self.assertLess(len(json.dumps(batch, ensure_ascii=False).encode('utf-8')), 4 * 1024 * 1024)
            self.assertTrue(batch['rows'])
            received.extend(row['content'] for row in batch['rows'])
            cursor = batch['cursor']
        self.assertEqual(received, [f'{number}:' + body for number in range(50)])
        initial = buffer.tail()
        self.assertLess(len(json.dumps(initial, ensure_ascii=False).encode('utf-8')), 4 * 1024 * 1024)
        initial_received = [row['content'] for row in initial['rows']]
        cursor = initial['cursor']
        while cursor < buffer.sequence:
            batch = buffer.tail(cursor)
            initial_received.extend(row['content'] for row in batch['rows'])
            cursor = batch['cursor']
        self.assertEqual(initial_received, ['起点'] + [f'{number}:' + body for number in range(50)])

    def test_media_never_exposes_raw_xml_paths_or_download_credentials(self):
        buffer = MessageBuffer(sanitize=redact)
        secret = '<msg aeskey="private-download-token">C:/private/media</msg>'
        for kind, label in ((3, '[图片]'), (34, '[语音]'), (43, '[视频]'), (47, '[表情]'), (62, '[视频]')):
            message = Message.from_native(11046, {'wx_type': kind, 'chat_id': FRIEND, 'sender_id': FRIEND,
                'msg': secret, 'image_path': 'C:/private/image.png', 'video_url': 'https://private.test'}, 'test')
            self.assertEqual(message.text, '')
            buffer.append(message)
            self.assertEqual(buffer.tail()['rows'][-1]['content'], label)
        buffer.append(Message('', FRIEND, FRIEND, type=49, data={'file_name': 'C:\\private\\example.txt'}))
        self.assertEqual(buffer.tail()['rows'][-1]['content'], '[文件] example.txt')
        serialized = json.dumps(buffer.tail(), ensure_ascii=False)
        for forbidden in ('private-download-token', 'C:/private', 'private.test', '<msg', 'image_path'):
            self.assertNotIn(forbidden, serialized)
        buffer.append(Message('sk-' + 'example_redaction_not_a_real_key_123456', FRIEND))
        self.assertNotIn('sk-example', buffer.tail()['rows'][-1]['content'])

    def test_media_updates_and_bot_native_echo_do_not_duplicate(self):
        buffer = MessageBuffer()
        for status in ('pending', 'ready', 'failed'):
            buffer.append(Message('', ROOM, FRIEND, type=3, is_group=True, sequence=3, session_id='session', data={'image_status': status}))
        buffer.append(Message('回复', ROOM, ACCOUNT, is_self=True, data={'bot_generated': True}))
        buffer.append(Message('回复', ROOM, is_self=True, data={'bot_generated': True, 'monitor_outgoing': True}), account=ACCOUNT)
        rows = buffer.tail()['rows']
        self.assertEqual(len(rows), 2)
        self.assertEqual((rows[1]['sender_id'], rows[1]['sender_name'], rows[1]['direction']), (ACCOUNT, '机器人', 'outgoing'))

    def test_cursor_pages_in_order_and_bounded_cache_reports_eviction(self):
        buffer = MessageBuffer()
        for number in range(300): buffer.append(Message(str(number), FRIEND))
        first = buffer.tail()
        self.assertEqual([row['id'] for row in first['rows']], list(range(101, 301)))
        for number in range(450): buffer.append(Message(str(number), FRIEND))
        second = buffer.tail(first['cursor'])
        self.assertEqual([row['id'] for row in second['rows']], list(range(301, 501)))
        self.assertEqual(buffer.tail(second['cursor'])['rows'][0]['id'], 501)
        self.assertEqual(buffer.tail(9999)['cursor'], 750)
        self.assertTrue(buffer.tail(9999)['reset'])
        small = MessageBuffer(capacity=3)
        for number in range(8): small.append(Message(str(number), FRIEND))
        self.assertTrue(small.tail(1)['dropped'])
        self.assertEqual(len(small.rows), 3)
        for value in (-1, True, '1'):
            with self.assertRaises(ValueError): small.tail(value)

    def test_threaded_callbacks_keep_unique_ordered_ids(self):
        buffer = MessageBuffer()
        threads = [threading.Thread(target=lambda:[buffer.append(Message('消息', FRIEND)) for _ in range(100)]) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
        self.assertEqual([row['id'] for row in buffer.rows], list(range(1, 501)))

    def test_reset_drops_previous_account_and_rotates_stream(self):
        buffer = MessageBuffer()
        buffer.append(Message('旧账号内容', FRIEND, sequence=1, session_id='old'))
        stream = buffer.tail()['stream_id']
        buffer.reset()
        self.assertEqual(buffer.tail()['rows'], [])
        self.assertNotEqual(buffer.tail()['stream_id'], stream)
        buffer.append(Message('新账号内容', FRIEND, sequence=1, session_id='new'))
        self.assertEqual(buffer.tail()['rows'][0]['content'], '新账号内容')


class RuntimeMonitorTests(unittest.TestCase):
    def app(self):
        app = BotApplication.__new__(BotApplication)
        app.config = {'chat_allowlist': ['filehelper']}
        app.members, app.plugins = Mock(), Mock()
        app.plugins.is_active.return_value = False
        return app

    def test_monitor_sees_new_group_before_allowlist_without_changing_dispatch_or_logs(self):
        app, buffer = self.app(), MessageBuffer()
        app.message_observer = buffer.append
        with self.assertLogs('Bot', 'INFO') as logs:
            app._emit(Message('仅在监控显示的正文', ROOM, FRIEND, is_group=True))
        self.assertEqual(buffer.tail()['rows'][0]['content'], '仅在监控显示的正文')
        self.assertNotIn('仅在监控显示的正文', '\n'.join(logs.output))
        app.plugins.dispatch.assert_not_called()
        app.members.record.assert_not_called()

    def test_metadata_never_reaches_plugins_and_observer_failure_cannot_break_chat(self):
        app, buffer = self.app(), MessageBuffer()
        app.message_observer = buffer.append
        app._emit(Message('', ROOM, FRIEND, type=43, is_group=True, data={'monitor_only': True}))
        self.assertEqual(buffer.tail()['rows'][0]['content'], '[视频]')
        app.plugins.dispatch.assert_not_called()
        app.message_observer = Mock(side_effect=RuntimeError('observer failed'))
        app._emit(Message('正常消息', 'filehelper'))
        app.plugins.dispatch.assert_called_once()

    def test_successful_sends_are_observed_failed_sends_are_not(self):
        service = ServiceFacade(text_delay={'enabled': False})
        backend = Mock(session_id='outgoing-test')
        backend.send_text.return_value = True
        service.bind(backend)
        buffer = MessageBuffer()
        service.message_observer = lambda message: buffer.append(message, account=ACCOUNT)
        self.assertTrue(service.send_text(FRIEND, '发送内容'))
        self.assertEqual(buffer.tail()['rows'][0]['direction'], 'outgoing')
        backend.send_text.side_effect = RuntimeError('send failed')
        with self.assertRaises(RuntimeError): service.send_text(FRIEND, '不能记录为成功')
        backend.send_text.side_effect = None
        backend.send_text.return_value = False
        self.assertFalse(service.send_text(FRIEND, '也不能记录'))
        self.assertEqual(len(buffer.tail()['rows']), 1)
        service.message_observer = Mock(side_effect=RuntimeError('observer failure'))
        backend.send_text.return_value = True
        self.assertTrue(service.send_text(FRIEND, '发送仍成功'))


@unittest.skipUnless(sys.platform == 'win32', 'Windows 原生映射，仅使用模拟消息')
class NativeMetadataTests(unittest.TestCase):
    def test_video_voice_and_media_before_allowlist_are_metadata_only(self):
        sys.path.insert(0, str(ROOT / 'native'))
        from filehelper_service import ReceivedMediaMessage
        from text_service import NativeTextService
        service = NativeTextService(pid=123, account=ACCOUNT, image_allowlist=('filehelper',))
        service.router.set_account(ACCOUNT)
        service._sender_wechat_name = Mock(return_value='测试群友')
        service._image_receiver, service._file_receiver, service._run = Mock(), Mock(), Mock()
        captured = []
        service.monitor_handler = captured.append
        for kind in (3, 49, 34, 43, 47, 62):
            raw = ReceivedMediaMessage(type=kind, sequence=kind, sender=ROOM.encode(), receiver=ACCOUNT.encode(),
                image_xml=(FRIEND + ':\n<msg>private media metadata</msg>').encode())
            result = service._map_message(raw)
            data = captured[-1] if kind in (3, 49) else result
            self.assertTrue(data['monitor_only'])
            self.assertEqual((data['chat_id'], data['sender_id']), (ROOM, FRIEND))
            self.assertEqual(Message.from_native(11046, data, 'metadata').text, '')
        service._image_receiver.submit.assert_not_called()
        service._file_receiver.submit.assert_not_called()
        service._run.assert_not_called()


class AccountHTTPTests(unittest.TestCase):
    def test_account_streams_are_isolated_authenticated_and_survive_robot_stop(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as folder:
            root = Path(folder)
            for source in ROOT.glob('*.example.json'): shutil.copy2(source, root / source.name)
            shutil.copytree(ROOT / 'plugins/echo', root / 'plugins/echo', ignore=shutil.ignore_patterns('__pycache__'))
            accounts = [{'wxid': ACCOUNT, 'pid': 1234, 'name': '测试账号甲'},
                        {'wxid': 'wxid_monitor_other', 'pid': 4321, 'name': '测试账号乙'}]
            hub = RobotHub(root, worker_factory=FakeWorker, scan=lambda root: accounts)
            server = PanelServer(hub, 0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            def get(profile, *, token=True, after=0, origin=None):
                connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=4)
                headers = {'X-Helper-Token': server.token} if token else {}
                if origin: headers['Origin'] = origin
                connection.request('GET', f'/api/messages?robot={profile}&after={after}', headers=headers)
                response = connection.getresponse()
                result = response.status, json.loads(response.read())
                connection.close()
                return result
            try:
                result = hub.start_selected({'accounts': accounts})
                first, second = [row['robot_id'] for row in result['results']]
                for identifier, content in ((first, '甲的消息'), (second, '乙的消息')):
                    app = hub.workers[identifier].controller.app
                    app.message_observer(Message(content, ROOM, FRIEND, is_group=True))
                self.assertEqual(get(first)[1]['rows'][0]['content'], '甲的消息')
                self.assertEqual(get(second)[1]['rows'][0]['content'], '乙的消息')
                self.assertNotEqual(get(first)[1]['stream_id'], get(second)[1]['stream_id'])
                self.assertEqual(get(first, token=False)[0], 403)
                self.assertEqual(get(first, origin='https://example.invalid')[0], 403)
                self.assertEqual(get(first, after=-1)[0], 400)
                self.assertEqual(get('unknown')[0], 404)
                controller = hub.workers[first].controller
                controller.stop(); controller.worker.join(2)
                self.assertEqual(get(first)[1]['rows'][0]['content'], '甲的消息')
                before = get(first)[1]['stream_id']
                controller.scan = lambda root: [{'wxid': 'wxid_monitor_rebound', 'pid': 5678}]
                controller.bind_account({'wxid': 'wxid_monitor_rebound', 'pid': 5678})
                self.assertEqual(get(first)[1]['rows'], [])
                self.assertNotEqual(get(first)[1]['stream_id'], before)
            finally:
                server.shutdown(); server.server_close(); thread.join(2); hub.close()
