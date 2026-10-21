"""撤回解析、精确关联和原生映射测试；不连接微信。"""
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

from wxbot_framework.recall import message_id, parse_recall
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message

ROOM = '12345678@chatroom'
ACCOUNT = 'wxid_recall_robot'
FRIEND = 'wxid_recall_friend'
IDENTIFIER = '18446744073709551600'


def xml(chat=ROOM, identifier=IDENTIFIER):
    return (f'<sysmsg type="revokemsg"><revokemsg><session>{chat}</session>'
            f'<msgid>42</msgid><newmsgid>{identifier}</newmsgid>'
            '<replacemsg><![CDATA[群友撤回了一条消息]]></replacemsg></revokemsg></sysmsg>')


def original(identifier=IDENTIFIER, chat=ROOM, kind=1, **extra):
    return Message('原始正文', chat, FRIEND, type=kind, is_group=chat.endswith('@chatroom'),
                   sequence=1, session_id='receive', data={'native_message_id':identifier, **extra})


def recall(identifier=IDENTIFIER, chat=ROOM):
    return Message('', chat, type=10002, is_group=chat.endswith('@chatroom'),
                   data={'recall':parse_recall(xml(chat, identifier), chat_id=chat), 'monitor_only':True})


class ParserTests(unittest.TestCase):
    def test_exact_64_bit_identifier_is_string(self):
        result = parse_recall(xml(), chat_id=ROOM)
        self.assertEqual(result['message_id'], IDENTIFIER)
        self.assertEqual(parse_recall(FRIEND+':\n'+xml(), chat_id=ROOM), result)
        self.assertEqual(message_id(int(IDENTIFIER)), IDENTIFIER)
        for invalid in (True, 0, -1, 1.5, '01', '0', '1e19', ' 12 ', str(2**64), ''):
            self.assertEqual(message_id(invalid), '')

    def test_untrusted_xml_duplicate_fields_and_other_sessions_cannot_match(self):
        bad = [xml().replace('type="revokemsg"', 'type="sysmsgtemplate"'),
               xml().replace('<session>', '<session>another@chatroom</session><session>'),
               xml().replace('<newmsgid>', '<newmsgid>2</newmsgid><newmsgid>'),
               xml().replace(f'<newmsgid>{IDENTIFIER}</newmsgid>', ''),
               xml().replace(IDENTIFIER, str(2**64)), xml(chat=FRIEND),
               '<!DOCTYPE a [<!ENTITY x "x">]>'+xml(),
               xml().replace('<msgid>', '<x>'*18+'</x>'*18+'<msgid>'),
               xml().replace('<msgid>', '<x/>'*1024+'<msgid>'),
               xml()+'文'*16384, '某人撤回了一条消息', '<broken']
        for value in bad:
            with self.subTest(value=value[:90]):
                self.assertIsNone(parse_recall(value, chat_id=ROOM))


class BufferTests(unittest.TestCase):
    def test_recall_updates_original_in_place_and_incremental_cursor(self):
        buffer = MessageBuffer(clock=lambda:10)
        buffer.append(original(), account=ACCOUNT, sender_name='群友甲')
        first = buffer.tail(); row = first['rows'][0]
        buffer.append(recall(), account=ACCOUNT)
        update = buffer.tail(first['cursor'])['rows']
        self.assertEqual(len(buffer.rows), 1)
        self.assertEqual(len(update), 1)
        self.assertEqual(update[0]['id'], row['id'])
        self.assertEqual(update[0]['content'], '原始正文')
        self.assertEqual(update[0]['sender_id'], FRIEND)
        self.assertTrue(update[0]['recalled'])
        self.assertGreater(update[0]['revision'], row['revision'])
        buffer.append(recall(), account=ACCOUNT)
        self.assertEqual(buffer.tail()['latest'], update[0]['revision'])

    def test_matching_is_scoped_to_account_conversation_and_native_id(self):
        buffer = MessageBuffer()
        for account, chat, identifier in ((ACCOUNT, ROOM, IDENTIFIER),
                ('wxid_other', ROOM, IDENTIFIER), (ACCOUNT, FRIEND, IDENTIFIER),
                (ACCOUNT, ROOM, '100')):
            buffer.append(original(identifier, chat), account=account)
        buffer.append(recall(), account=ACCOUNT)
        self.assertEqual([row.get('recalled',False) for row in buffer.rows], [True,False,False,False])
        # 普通文本中的伪造系统 XML 或伪造 recall 字段均不能触发标记。
        buffer.append(Message(xml(), ROOM, type=1, data={'recall':recall().data['recall']}), account=ACCOUNT)
        self.assertFalse(buffer.rows[-1].get('recalled',False))

    def test_out_of_order_notification_becomes_retained_original(self):
        buffer = MessageBuffer()
        buffer.append(recall(), account=ACCOUNT)
        first = buffer.tail()['rows'][0]
        self.assertTrue(first['recall_missing'])
        buffer.append(original(), account=ACCOUNT)
        self.assertEqual(len(buffer.rows),1)
        row = buffer.tail()['rows'][0]
        self.assertEqual(row['id'],first['id'])
        self.assertEqual(row['content'],'原始正文')
        self.assertTrue(row['recalled'])
        self.assertNotIn('recall_missing',row)

    def test_recalled_media_and_forwarded_updates_preserve_original(self):
        buffer = MessageBuffer()
        buffer.append(original(kind=3), account=ACCOUNT)
        buffer.append(recall(), account=ACCOUNT)
        buffer.append(original(kind=3, image_status='ready'), account=ACCOUNT)
        self.assertEqual(len(buffer.rows),1)
        self.assertEqual(buffer.rows[0]['content'],'[图片]')
        buffer.reset()
        def forwarded(status):
            return original(kind=49,app_type=19,forwarded_records=[
                {'kind':'text','sender_name':'原作者','text':'转发正文'},
                {'kind':'image','sender_name':'原作者','image_status':status}])
        buffer.append(forwarded('pending'), account=ACCOUNT)
        buffer.append(recall(), account=ACCOUNT)
        buffer.append(forwarded('ready'), account=ACCOUNT)
        self.assertEqual(len(buffer.rows),3)
        self.assertTrue(all(row['recalled'] for row in buffer.rows))
        self.assertEqual(buffer.rows[-1]['content'],'[图片] 已保存')
        self.assertEqual(buffer.rows[1]['content'],'转发正文')

    def test_eviction_and_reset_are_bounded_and_never_invent_original(self):
        buffer = MessageBuffer(capacity=2)
        for identifier in ('1','2','3','4'):
            buffer.append(recall(identifier), account=ACCOUNT)
        self.assertEqual(len(buffer.recalls),2)
        self.assertEqual(len(buffer.rows),2)
        self.assertTrue(all(row['recall_missing'] for row in buffer.rows))
        buffer.reset()
        self.assertFalse(buffer.recalls)
        buffer.append(original(), account=ACCOUNT)
        self.assertFalse(buffer.rows[0].get('recalled',False))


@unittest.skipUnless(sys.platform=='win32','Windows 原生消息映射')
class NativeTests(unittest.TestCase):
    def service(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'native'))
        from text_service import NativeTextService
        service = NativeTextService(pid=123,account=ACCOUNT)
        service.router.set_account(ACCOUNT)
        service._sender_wechat_name = Mock(return_value=None)
        return service

    def test_group_and_private_recall_are_monitor_only(self):
        service = self.service()
        from filehelper_service import ReceivedMediaMessageV5
        for chat in (ROOM,FRIEND):
            raw = ReceivedMediaMessageV5(type=10002,sender=chat.encode(),receiver=ACCOUNT.encode(),
                native_message_id=33,image_xml=xml(chat).encode())
            data = service._map_message(raw)
            self.assertTrue(data['monitor_only'])
            self.assertEqual(data['recall']['message_id'],IDENTIFIER)
            self.assertEqual(data['chat_id'],chat)
            self.assertEqual(Message.from_native(11046,data,'x').text,'')
            raw.receiver=b'wxid_different'
            self.assertIsNone(service._map_message(raw))

    def test_original_native_id_survives_mapping_without_precision_loss(self):
        service = self.service()
        from filehelper_service import ReceivedMediaMessageV5
        raw = ReceivedMediaMessageV5(type=1,sequence=1,sender=FRIEND.encode(),receiver=ACCOUNT.encode(),
            native_message_id=int(IDENTIFIER),text='正文'.encode())
        data = service._map_message(raw)
        self.assertEqual(data['native_message_id'],IDENTIFIER)
        buffer = MessageBuffer()
        buffer.append(Message.from_native(11046,data,'receive'),account=ACCOUNT)
        buffer.append(recall(chat=FRIEND),account=ACCOUNT)
        self.assertTrue(buffer.rows[0]['recalled'])

    def test_independent_system_entry_uses_session_from_validated_xml(self):
        service = self.service()
        from filehelper_service import ReceivedMediaMessageV5
        for chat in (ROOM,FRIEND):
            raw = ReceivedMediaMessageV5(type=10002,reserved=2,sender=b'weixin',receiver=b'weixin',
                image_xml=xml(chat).encode())
            data = service._map_message(raw)
            self.assertEqual(data['chat_id'],chat)
            self.assertEqual(data['recall']['message_id'],IDENTIFIER)
            self.assertTrue(data['monitor_only'])
            raw.reserved=0
            self.assertIsNone(service._map_message(raw))
            raw.reserved=2;raw.image_xml=b'<broken>'
            self.assertIsNone(service._map_message(raw))
