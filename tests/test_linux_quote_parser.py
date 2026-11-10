import unittest
import socket
from dataclasses import replace
from unittest.mock import Mock, patch

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate
from wxbot_framework.quoted import QuoteError, parse_quote
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade


class LinuxQuoteParserTests(unittest.TestCase):
    def test_text_reference_has_no_internal_identifier(self):
        value = parse_quote('<msg><appmsg><type>57</type><title>reply</title>'
                            '<refermsg><type>1</type><svrid>12345</svrid>'
                            '<displayname>friend</displayname><content>original</content>'
                            '</refermsg></appmsg></msg>')
        self.assertEqual(value.text, 'reply')
        self.assertEqual(value.reference.text, 'original')
        self.assertNotIn('12345', str(value.public()))

    def test_other_share_and_unsafe_xml(self):
        self.assertIsNone(parse_quote('<msg><appmsg><type>6</type></appmsg></msg>'))
        with self.assertRaises(QuoteError):
            parse_quote('<!DOCTYPE msg [<!ENTITY x "test">]><msg>&x;</msg>')

    def test_media_reference_does_not_expose_xml(self):
        value = parse_quote('<msg><appmsg><type>57</type><title>reply</title>'
                            '<refermsg><type>3</type><content>secret</content></refermsg>'
                            '</appmsg></msg>')
        self.assertEqual(value.reference.text, '[图片]')
        self.assertNotIn('secret', str(value.public()))


class QuoteProbeTests(unittest.TestCase):
    def test_probe_quote_only_accepts_bound_peer(self):
        from tools.linux_quote_probe import on_event
        test = Mock(quote_event={'sender': 'wxid_peer', 'receiver': 'wxid_self'})
        xml = ('<msg><appmsg><type>57</type><title>reply</title><refermsg>'
               '<type>1</type><content>WX_LINUX_BRIDGE_07</content>'
               '</refermsg></appmsg></msg>')
        with patch('builtins.print') as output:
            on_event(test, {'type': 49, 'sender': 'wxid_other', 'receiver': 'wxid_self', 'text': xml})
            output.assert_not_called()
            on_event(test, {'type': 49, 'sender': 'wxid_peer', 'receiver': 'wxid_self', 'text': xml})
            output.assert_called_once()
            self.assertNotIn('WX_LINUX_BRIDGE_07', str(output.call_args))

    def test_probe_is_bound_to_captured_account_and_runs_once(self):
        from tools.linux_quote_probe import execute
        sender = Mock()
        sender.current_profile.return_value = {'wxid': 'wxid_self'}
        test = Mock(sender=sender, attempted=set(), quote_event={
            'valid_account': True, 'sender': 'wxid_peer', 'receiver': 'wxid_self',
            'native_message_id': '12345'}, quote_scope=7)
        self.assertTrue(execute(test, 'probe_quote')['success'])
        sender.send_quote_text.assert_called_once_with('wxid_peer', 12345, '引用回复测试',
                                                       scope_generation=7)
        with self.assertRaises(RuntimeError):
            execute(test, 'probe_quote')
        self.assertEqual(sender.send_quote_text.call_count, 1)

    def test_probe_rejects_changed_account_before_send(self):
        from tools.linux_quote_probe import execute
        sender = Mock()
        sender.current_profile.return_value = {'wxid': 'wxid_other'}
        test = Mock(sender=sender, attempted=set(), quote_event={
            'valid_account': True, 'sender': 'wxid_peer', 'receiver': 'wxid_self',
            'native_message_id': '12345'})
        with self.assertRaises(RuntimeError):
            execute(test, 'probe_quote')
        sender.send_quote_text.assert_not_called()


@unittest.skipUnless(hasattr(socket, 'SO_PEERCRED'), 'requires Linux peer credentials')
class LinuxQuoteSendTests(unittest.TestCase):
    def test_plugin_quote_uses_current_scope_and_full_server_id(self):
        sender = LinuxNativeSender(allowed_targets=('wxid_peer',))
        self.addCleanup(sender.close)
        sender.features = frozenset({'native_quote'})
        sender._bound_account = sender.image_account = 'wxid_self'
        sender.image_scope_generation = 7
        sender._context_ready = True
        service = ServiceFacade(text_delay={'enabled': False})
        backend = Mock(session_id='current', capabilities={'text.quote.send'})
        backend.send_quote_text.side_effect = lambda peer, original_id, text: sender.send_quote_text(
            peer, original_id, text, scope_generation=7)
        service.bind(backend)
        ctx = PluginContext('fixture', {}, service, [])
        message = Message('original', 'wxid_peer', session_id='current',
                          data={'native_message_id': '18446744073709551615'})
        with patch.object(sender, '_request', return_value={'callback': True, 'result': '{"success":true}'}) as call:
            self.assertTrue(ctx.reply_quote(message, '收到'))
            with self.assertRaises(RuntimeError):
                ctx.reply_quote(replace(message, session_id='stale'), '收到')
            sender.image_scope_generation = 8
            with self.assertRaises(LinuxSendError):
                ctx.reply_quote(message, '收到')
            call.assert_called_once_with(b'Y', '7\nwxid_peer\n18446744073709551615\n收到'.encode())

        sender.image_scope_generation = 7
        with patch.object(sender, '_request', return_value={'pending': True}) as call:
            with self.assertRaises(LinuxSendIndeterminate):
                ctx.reply_quote(message, '收到')
            call.assert_called_once()
