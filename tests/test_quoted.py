"""原生引用回复和消息监控的离线回归测试。"""
import json
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
from xml.sax.saxutils import escape

from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.quoted import MAX_XML_BYTES, QuoteError, display_quote, parse_quote, reference
from wxbot_framework.sdk import CapabilityError, Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
ROOM, ROBOT, FRIEND, OTHER = '123@chatroom', 'wxid_example_robot', 'wxid_example_friend', 'wxid_example_other'


def quote_xml(question='@机器人\u2005你怎么看？', content='明天上午九点开会。', *, kind=1, author='被引用者'):
    return ('<msg><appmsg><title>' + escape(question) + '</title><type>57</type><refermsg><type>' +
            str(kind) + '</type><svrid>1234567890123456789</svrid><fromusr>' + ROOM + '</fromusr>' +
            '<chatusr>' + OTHER + '</chatusr><displayname>' + escape(author) + '</displayname><content>' +
            escape(content) + '</content></refermsg></appmsg></msg>')


def quoted_message(question='@机器人\u2005你怎么看？', content='明天上午九点开会。', *, sequence=100, **options):
    quote = parse_quote(quote_xml(question, content, **options))
    return Message.from_native(11046, dict(quote.public(), msg=question, wx_type=49,
        chat_id=ROOM, room_wxid=ROOM, sender_id=FRIEND, native_receiver=ROBOT, self_wxid=ROBOT,
        sequence=sequence, at_members=(ROBOT,), is_at_self=True), 'test')


class QuoteParserTests(unittest.TestCase):
    def test_question_and_reference_are_separate_and_standard_entities_decode_once(self):
        value = parse_quote(quote_xml('问：a < b & c？', '1 < 2 & 原样 &lt;tag&gt;\n第二行', author='甲 & 乙'))
        self.assertEqual(value.text, '问：a < b & c？')
        self.assertEqual(value.reference.author, '甲 & 乙')
        self.assertEqual(value.reference.text, '1 < 2 & 原样 &lt;tag&gt;\n第二行')
        self.assertTrue(value.reference.available)
        self.assertEqual(value.reference.kind, 'text')
        self.assertNotIn('svrid', json.dumps(value.public()))

    def test_cdata_and_missing_reference(self):
        xml = quote_xml().replace('明天上午九点开会。', '<![CDATA[原话 <3 & 内容]]>')
        self.assertEqual(parse_quote(xml).reference.text, '原话 <3 & 内容')
        value = parse_quote('<msg><appmsg><type>57</type><title>问一下</title></appmsg></msg>')
        self.assertEqual(value.text, '问一下')
        self.assertFalse(value.reference.available)
        code = '<!DOCTYPE html><html>代码示例</html>'
        xml = quote_xml().replace('明天上午九点开会。', '<![CDATA[' + code + ']]>')
        self.assertEqual(parse_quote(xml).reference.text, code)

    def test_media_only_produces_type_markers_never_cdn_keys_or_media_xml(self):
        secret = '<msg><img aeskey="fixture-private-key" cdnmidimgurl="https://private.invalid/image"/></msg>'
        for kind, expected in ((3, '[图片]'), (34, '[语音]'), (43, '[视频]'), (47, '[表情]'), (62, '[视频]')):
            with self.subTest(kind=kind):
                value = parse_quote(quote_xml(content=secret, kind=kind))
                self.assertEqual(value.reference.text, expected)
                self.assertTrue(value.reference.available)
                self.assertNotIn('fixture-private-key', json.dumps(value.public()))
                self.assertNotIn('private.invalid', json.dumps(value.public()))

    def test_file_link_and_nested_quote_use_labels_and_titles_without_urls(self):
        for subtype, kind in ((5, 'link'), (6, 'file')):
            raw = f'<msg><appmsg><type>{subtype}</type><title>原始标题</title><url>https://private.invalid/</url></appmsg></msg>'
            value = parse_quote(quote_xml(content=raw, kind=49))
            self.assertEqual(value.reference.kind, kind)
            self.assertIn('原始标题', value.reference.text)
            self.assertNotIn('https://', value.reference.text)
        nested = quote_xml('转述一下', '最初的原话', author='最初作者')
        value = parse_quote(quote_xml(content=nested, kind=49))
        self.assertEqual(value.reference.kind, 'quote')
        self.assertIn('最初作者：最初的原话', value.reference.text)

    def test_unknown_type_and_invalid_nested_content_are_explicitly_unavailable(self):
        for kind, content in ((2001, '<secret>do not leak</secret>'), (49, '<broken>')):
            value = parse_quote(quote_xml(content=content, kind=kind))
            self.assertFalse(value.reference.available)
            self.assertNotIn('secret', value.reference.text)

    def test_limits_declarations_duplicate_fields_and_malformed_payload(self):
        samples = [None, '', '<msg>', 'x' * (MAX_XML_BYTES + 1),
            '<!DOCTYPE x [<!ENTITY y "test">]>' + quote_xml(),
            '<!ENTITY x "test">' + quote_xml(),
            quote_xml().replace('<displayname>', '<displayname>重复</displayname><displayname>'),
            quote_xml().replace('</appmsg>', '<refermsg/></appmsg>'),
            quote_xml().replace('</appmsg>', '<x>' * 25 + '</x>' * 25 + '</appmsg>')]
        for sample in samples:
            with self.subTest(sample=str(sample)[:30]), self.assertRaises(QuoteError):
                parse_quote(sample)
        self.assertIsNone(parse_quote('<msg><appmsg><type>6</type></appmsg></msg>'))
        self.assertIsNone(parse_quote('<msgsource><atuserlist>bot</atuserlist></msgsource>'))

    def test_long_original_is_complete_and_author_has_no_controls(self):
        original = '字' * 9000 + '结尾'
        value = parse_quote(quote_xml(content=original, author='昵称\n换行'))
        self.assertEqual(value.reference.text, original)
        self.assertFalse(value.reference.truncated)
        self.assertEqual(value.reference.author, '昵称换行')

    def test_long_question_and_reference_are_preserved_in_monitor(self):
        original = ('长文本与&符号\n' * 1400) + '原文结尾'
        question = '当前问题' * 2300 + '问题结尾'
        message = quoted_message(question=question, content=original)
        self.assertEqual(message.text, question)
        self.assertEqual(reference(message).text, original)
        buffer = MessageBuffer()
        buffer.append(message)
        displayed = buffer.tail()['rows'][0]['content']
        self.assertEqual(displayed, question + '\n引用 被引用者：' + original)

    def test_only_valid_quote_metadata_is_considered_a_reference(self):
        self.assertIsNone(reference(Message('可见引用', ROOM)))
        self.assertIsNone(reference(Message('', ROOM, type=49, data={'app_type':57})))
        self.assertIsNone(reference(Message('', ROOM, type=49, data={'app_type':57,
            'quoted_message':{'kind':'text', 'text':'正文', 'author':'名字', 'available':'false', 'truncated':False}})))


class QuoteSendTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id='test', capabilities={'text.quote.send'})
        self.backend.send_quote_text.return_value = True
        self.service = ServiceFacade(text_delay={'enabled': False})
        self.service.bind(self.backend)
        self.ctx = PluginContext('quote', {}, self.service, self.backend.capabilities)
        self.buffer = MessageBuffer()
        self.service.message_observer = self.buffer.append
        self.message = Message.from_native(11046, {'msg': '原文', 'room_wxid': ROOM,
            'sender_id': FRIEND, 'sender_wechat_name': '甲', 'native_message_id': '1234567890123456789'},
            'test')

    def test_reply_uses_the_exact_server_id_and_shows_as_quote_in_monitor(self):
        self.assertTrue(self.ctx.reply_quote(self.message, '收到 & <OK>'))
        self.backend.send_quote_text.assert_called_once_with(ROOM, 1234567890123456789, '收到 & <OK>')
        self.backend.send_text.assert_not_called()
        row, = self.buffer.tail()['rows']
        self.assertEqual((row['direction'], row['type'], row['content']),
                         ('outgoing', 49, '收到 & <OK>\n引用 甲：原文'))

    def test_stale_unsupported_or_invalid_original_never_sends(self):
        cases = ((replace(self.message, session_id='stale'), '收到', RuntimeError),
                 (replace(self.message, type=3), '收到', ValueError),
                 (replace(self.message, data={}, sequence=1234), '收到', QuoteError),
                 (self.message, '字' * 342, ValueError),
                 (self.message, 'a\0b', ValueError))
        for message, content, expected in cases:
            with self.subTest(kind=expected.__name__), self.assertRaises(expected):
                self.ctx.reply_quote(message, content)
        self.backend.capabilities = {'text.send'}
        with self.assertRaises(CapabilityError):
            self.ctx.reply_quote(self.message, '收到')
        self.backend.send_quote_text.assert_not_called()
        self.backend.send_text.assert_not_called()

    def test_unsent_reply_is_not_shown_and_never_falls_back_to_plain_text(self):
        self.backend.send_quote_text.return_value = False
        self.assertFalse(self.ctx.reply_quote(self.message, '收到'))
        self.assertEqual(self.buffer.tail()['rows'], [])
        self.backend.send_text.assert_not_called()


@unittest.skipUnless(sys.platform == 'win32', 'Windows 原生映射，模拟接收')
class NativeQuoteTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / 'native'))
        from filehelper_service import ReceivedMediaMessageV5
        from text_service import NativeTextService
        self.raw_type = ReceivedMediaMessageV5
        self.service = NativeTextService(pid=123, account=ROBOT)
        self.service.router.set_account(ROBOT)
        self.service._sender_wechat_name = Mock(return_value='当前提问者')
        self.service._file_receiver, self.service._forwarded_receiver = Mock(), Mock()
        self.service.monitor_handler = Mock()

    def test_retry_only_when_wechat_has_not_indexed_the_original(self):
        self.service._owns_bridge = True
        self.service._check_account = Mock()
        not_indexed = {'code': 0, 'cleanup_code': 0, 'success': False,
                       'report': {'stage': 84, 'exception': 0}}
        self.service._run = Mock(side_effect=[not_indexed, {'success': True}])
        self.assertTrue(self.service.send_quote_text(ROOM, 12345, '回复'))
        self.assertEqual(self.service._run.call_count, 2)

        self.service._run = Mock(return_value={'code': 0, 'cleanup_code': 258, 'success': False})
        self.assertFalse(self.service.send_quote_text(ROOM, 12345, '可能已经提交的回复'))
        self.service._run.assert_called_once()

    def receive(self, *, source=None, content=None, group=True):
        if source is None:
            source = '<msgsource><atuserlist>' + ROBOT + '</atuserlist></msgsource>'
        body = (FRIEND + ':\n' if group else '') + (content or quote_xml())
        raw = self.raw_type(type=49, sequence=100, sender=(ROOM if group else FRIEND).encode(),
            receiver=ROBOT.encode(), image_xml=body.encode(), mention_source=source.encode(), native_message_id=12345)
        return self.service._map_message(raw)

    def test_group_prefix_is_removed_quote_bypasses_attachment_and_forward_downloads(self):
        data = self.receive()
        self.assertEqual(data['app_type'], 57)
        self.assertTrue(data['is_at_self'])
        self.assertEqual(data['at_members'], (ROBOT,))
        self.assertEqual(data['raw_content'], '')
        message = Message.from_native(11046, data, 'test')
        self.assertEqual(message.text, '@机器人\u2005你怎么看？')
        self.assertEqual(message.type, 49)
        self.assertEqual(message.sender_id, FRIEND)
        self.assertEqual(reference(message).author, '被引用者')
        self.service._file_receiver.submit.assert_not_called()
        self.service._forwarded_receiver.submit.assert_not_called()
        self.service.monitor_handler.assert_not_called()  # 正常事件在框架中只观察一次。

    def test_private_quote_is_recognized_but_visible_at_or_embedded_at_is_not_real_at(self):
        data = self.receive(group=False)
        self.assertEqual(data['app_type'], 57)
        self.assertFalse(data['is_at_self'])
        xml = quote_xml(content='<msgsource><atuserlist>' + ROBOT + '</atuserlist></msgsource>')
        data = self.receive(source='', content=xml)
        self.assertFalse(data['is_at_self'])
        self.assertEqual(data['at_members'], ())

    def test_long_reference_reaches_group_monitor_unchanged(self):
        original = '长消息🌸\n' * 2200 + '原文结尾'
        question = '@机器人\u2005请概括这条消息'
        data = self.receive(content=quote_xml(question=question, content=original))
        self.assertIsNotNone(data)
        message = Message.from_native(11046, data, 'test')
        self.assertEqual((message.text, reference(message).text), (question, original))
        self.assertEqual(data['native_message_id'], '12345')
        self.assertTrue(data['is_at_self'])
        buffer = MessageBuffer()
        buffer.append(message)
        self.assertEqual(buffer.tail()['rows'][0]['content'], question + '\n引用 被引用者：' + original)
        self.service._file_receiver.submit.assert_not_called()

    def test_regular_file_still_uses_the_attachment_receiver(self):
        data = self.receive(content='<msg><appmsg><type>6</type></appmsg></msg>')
        self.assertIsNone(data)
        self.service._file_receiver.submit.assert_called_once()
        self.assertNotEqual(self.service.monitor_handler.call_args.args[0].get('app_type'), 57)
