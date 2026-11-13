"""小程序业务接口的离线契约测试；不打开微信、不操作系统鼠标。"""
from dataclasses import asdict
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from wxbot_framework.miniprograms import MiniProgramClient, MiniProgramPage, parse_components
from wxbot_framework.sdk import CapabilityError, PluginContext, ServiceFacade


APP = 'wx0123456789abcdef'
PAGE = MiniProgramPage(APP, 'pages/home/index', 7)
OTHER_PAGE = MiniProgramPage(APP, 'pages/detail/index', 8)


def evaluated(page=PAGE):
    return {'result': {'success': True, 'data': {'route': page.route, 'page_id': page.page_id}}}


def cdp(value):
    return {'cdp_response': json.dumps({'result': value})}


class ComponentTests(unittest.TestCase):
    def test_textarea_contents_and_rendered_input_values_are_not_component_labels(self):
        items = parse_components('<wx-view><textarea placeholder="备注">private-initial-value</textarea>'
            '<wx-input><span>private-rendered-value</span></wx-input></wx-view>', PAGE)
        public = json.dumps([asdict(item) for item in items])
        self.assertNotIn('private-initial-value', public)
        self.assertNotIn('private-rendered-value', public)
        self.assertEqual(items[1].name, '备注')

    def test_public_component_data_skips_scripts_styles_and_private_attributes(self):
        html = '''<html><body><wx-view id="root" class="card" data-private="private-token">
          <script>script-secret<wx-button>script-button-secret</wx-button></script>
          <style>style-secret</style><template><wx-text>template-secret</wx-text></template>
          <wx-button aria-label="购买" role="button" onclick="handler-secret"> 买 <span>两件</span> </wx-button>
          <input name="收件人" placeholder="姓名" value="input-secret" data-key="key-secret" />
          <wx-image src="https://private.example/image-secret" />
          <button disabled>稍后</button><textarea aria-disabled="true" placeholder="备注"></textarea>
        </wx-view></body></html>'''
        items = parse_components(html, PAGE)
        self.assertEqual([item.tag for item in items],
                         ['wx-view', 'wx-button', 'input', 'wx-image', 'button', 'textarea'])
        self.assertEqual(items[1].name, '购买')
        self.assertEqual(items[1].text, '买 两件')
        self.assertEqual(items[1].role, 'button')
        self.assertEqual(items[2].name, '收件人')
        self.assertEqual(items[2].placeholder, '姓名')
        self.assertEqual(items[0].classes, 'card')
        self.assertEqual(items[1].xpath, '/html[1]/body[1]/wx-view[1]/wx-button[1]')
        self.assertTrue(items[-1].disabled)
        self.assertTrue(items[-2].disabled)
        self.assertTrue(all(item.page == PAGE for item in items))
        public = json.dumps([asdict(item) for item in items], ensure_ascii=False)
        for private in ('private-token', 'script-secret', 'script-button-secret', 'style-secret',
                        'template-secret', 'handler-secret', 'input-secret', 'key-secret',
                        'https://private.example', 'image-secret'):
            with self.subTest(private=private):
                self.assertNotIn(private, public)
        self.assertFalse({'value', 'src', 'data-private', 'onclick'} & set(asdict(items[0])))

    def test_xpath_counts_same_tag_siblings_and_decodes_visible_entities(self):
        items = parse_components('<wx-view><wx-text>A &amp; B</wx-text><div></div>'
                                 '<wx-text>C</wx-text><input/><input/></wx-view>', PAGE)
        self.assertEqual(items[1].text, 'A & B')
        self.assertEqual(items[2].xpath, '/wx-view[1]/wx-text[2]')
        self.assertEqual(items[-1].xpath, '/wx-view[1]/input[2]')

    def test_invalid_component_arguments_are_rejected(self):
        for html, page in ((b'<button/>', PAGE), (None, PAGE), ('', {})):
            with self.subTest(html=html, page=page), self.assertRaises(ValueError):
                parse_components(html, page)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.command = Mock(return_value=[{'app_id': APP}])
        self.client = MiniProgramClient(APP, self.command)
        self.client.open('pages/home/index')
        self.page = MiniProgramPage(APP, PAGE.route, PAGE.page_id, self.client._session)
        self.command.reset_mock()

    def test_open_confirms_target_and_uses_stable_private_session(self):
        command = Mock(return_value=[{'app_id': APP}])
        client = MiniProgramClient(APP, command)
        with self.assertRaises(RuntimeError):
            client.click(1, 2)
        command.assert_not_called()
        client.open('pages/home/index')
        client.page_info()
        launch, read = command.call_args_list
        self.assertEqual(launch.args[0], APP)
        self.assertRegex(launch.args[1], r'^helper_mp_[0-9a-f]{32}$')
        self.assertEqual(read.args[1], launch.args[1])
        self.assertEqual(launch.args[2], [{'method': 'XWeb.LaunchApplet', 'params': {
            'app_id': APP, 'prefer_hot_launch': True, 'entry_url': 'pages/home/index'}}])
        self.assertFalse(launch.kwargs['check_app'])
        self.assertTrue(read.kwargs['check_app'])
        command.return_value = [{'app_id': 'wxffffffffffffffff'}]
        rejected = MiniProgramClient(APP, command)
        with self.assertRaises(RuntimeError):
            rejected.open()
        command.reset_mock()
        with self.assertRaises(RuntimeError):
            rejected.click(1, 2)
        command.assert_not_called()

    def test_click_dispatches_one_touch_batch_without_desktop_input(self):
        self.assertEqual(self.client.click(10, 20.5), {'status': 'dispatched'})
        self.command.assert_called_once()
        self.assertEqual(self.command.call_args.args[2], [
            {'method': 'Input.dispatchTouchEvent', 'params': {
                'type': 'touchStart', 'touchPoints': [{'x': 10, 'y': 20.5, 'id': 1}]}},
            {'method': 'Input.dispatchTouchEvent', 'params': {'type': 'touchEnd', 'touchPoints': []}}])

    def test_insert_and_replace_preserve_unicode_and_do_not_submit(self):
        self.client.input_text('中文😀\n第二行')
        self.assertEqual(self.command.call_args.args[2], [
            {'method': 'Input.insertText', 'params': {'text': '中文😀\n第二行'}}])
        self.command.reset_mock()
        self.client.input_text('', replace=True)
        self.command.assert_called_once()
        requests = self.command.call_args.args[2]
        self.assertEqual(requests, [
            {'method': 'Input.dispatchKeyEvent', 'params': {'type': 'rawKeyDown', 'key': 'a',
                'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2}},
            {'method': 'Input.dispatchKeyEvent', 'params': {'type': 'keyUp', 'key': 'a',
                'code': 'KeyA', 'windowsVirtualKeyCode': 65, 'modifiers': 2}},
            {'method': 'Input.insertText', 'params': {'text': ''}}])

    def test_invalid_inputs_never_reach_command_channel(self):
        for x, y in ((True, 1), (1, False), (-1, 0), (0, -0.1), (float('nan'), 1),
                     (1, float('inf')), ('1', 2), (None, 0)):
            with self.subTest(x=x, y=y), self.assertRaises(ValueError):
                self.client.click(x, y)
        for value, replace in ((None, False), (123, False), ('x\0y', False),
                               ('x', 1), ('x', 'yes'), ('\ud800', False)):
            with self.subTest(value=repr(value), replace=replace):
                with self.assertRaises((ValueError, UnicodeError)):
                    self.client.input_text(value, replace=replace)
        for script in ('', ' ', None, 'x\0y'):
            with self.subTest(script=script), self.assertRaises(ValueError):
                self.client.evaluate(script)
        self.command.assert_not_called()

    def test_invalid_app_and_launch_path_are_rejected(self):
        for app in ('', 'wx123', 'wx0123456789abcdeg', 'WX0123456789abcdef', 1):
            with self.subTest(app=app), self.assertRaises(ValueError):
                MiniProgramClient(app, self.command)
        for entry in (None, 1, 'page\0secret'):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.client.open(entry)
        self.command.assert_not_called()

    def test_expected_page_blocks_stale_click_and_text_before_action(self):
        for action in (lambda: self.client.click(1, 2, expected_page=self.page),
                       lambda: self.client.input_text('不可发送', replace=True, expected_page=self.page)):
            self.command.reset_mock()
            self.command.return_value = [evaluated(OTHER_PAGE)]
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, '页面已切换'):
                action()
            self.command.assert_called_once()
            self.assertEqual(self.command.call_args.args[2][0]['method'], 'XWeb.EvaluateJavaScript')

    def test_current_page_allows_actions_but_another_client_page_is_rejected(self):
        self.command.return_value = [evaluated()]
        self.client.click(1, 2, expected_page=self.page)
        self.assertEqual(self.command.call_count, 2)
        self.assertEqual(self.command.call_args.args[2][0]['method'], 'Input.dispatchTouchEvent')
        foreign = MiniProgramPage(APP, PAGE.route, PAGE.page_id, 'helper_mp_' + 'f' * 32)
        for action in (lambda: self.client.click(1, 2, expected_page=foreign),
                       lambda: self.client.input_text('不可发送', expected_page=foreign)):
            self.command.reset_mock()
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                action()
            self.command.assert_called_once()
            self.assertEqual(self.command.call_args.args[2][0]['method'], 'XWeb.EvaluateJavaScript')

    def test_components_select_current_frame_and_verify_page_after_snapshot(self):
        # 当前 frame 故意放在兄弟的前面；不能按 DFS 第一项或固定 frame 取页。
        root = {'nodeId': 1, 'children': [
            {'attributes': ['name', 'page-frame_7'], 'contentDocument': {'nodeId': 70}},
            {'attributes': ['name', 'page-frame_8'], 'contentDocument': {'nodeId': 80}}]}
        self.command.side_effect = [[evaluated()], [cdp({'root': root})],
                                   [cdp({'outerHTML': '<wx-button>当前页面</wx-button>'})], [evaluated()]]
        items = self.client.components()
        self.assertEqual(items[0].text, '当前页面')
        self.assertEqual(items[0].page, self.page)
        self.assertEqual(self.command.call_args_list[2].args[2],
                         [{'method': 'DOM.getOuterHTML', 'params': {'nodeId': 70}}])
        self.command.side_effect = [[evaluated()], [cdp({'root': root})],
                                   [cdp({'outerHTML': '<wx-button>旧页面</wx-button>'})],
                                   [evaluated(OTHER_PAGE)]]
        with self.assertRaisesRegex(RuntimeError, '页面已切换'):
            self.client.components()

    def test_missing_current_frame_never_reads_a_different_page(self):
        self.command.side_effect = [[evaluated()], [cdp({'root': {'children': [
            {'attributes': ['name', 'page-frame_8'], 'contentDocument': {'nodeId': 80}}]}})]]
        with self.assertRaisesRegex(RuntimeError, '当前小程序页面'):
            self.client.components()
        self.assertEqual(self.command.call_count, 2)

    def test_failed_evaluation_does_not_expose_private_error_body(self):
        for value in ({'error': 'private-script-stack', 'result': {'success': False}},
                      {'result': None}, {'result': []}, {'result': 'private-script-stack'}):
            self.command.return_value = [value]
            with self.subTest(value=value), self.assertRaises(RuntimeError) as caught:
                self.client.evaluate('getCurrentPages()')
            self.assertNotIn('private-script-stack', str(caught.exception))

    def test_malformed_document_response_is_rejected_without_private_diagnostics(self):
        for value in (None, [], {'cdp_response': 'private-bad-json'},
                      {'cdp_response': '[]'}, {'cdp_response': '{"result":null}'},
                      {'cdp_response': '{"error":"private-cdp-stack"}'}):
            self.command.side_effect = [[evaluated()], [value]]
            with self.subTest(value=value), self.assertRaises(RuntimeError) as caught:
                self.client.components()
            self.assertNotIn('private', str(caught.exception))


class FacadeTests(unittest.TestCase):
    def setUp(self):
        self.service = ServiceFacade()
        self.backend = SimpleNamespace(session_id='offline_session', stop=Mock(),
            miniprogram_commands=Mock(return_value=[{'app_id': APP}]))

    def test_disconnected_and_unsupported_backends_fail_before_native_call(self):
        with self.assertRaises(RuntimeError):
            self.service.miniprogram(APP)
        self.service.bind(SimpleNamespace(session_id='unsupported'))
        with self.assertRaises(CapabilityError):
            self.service.miniprogram(APP)

    def test_current_client_forwards_account_session_and_cancellation_callback(self):
        self.service.bind(self.backend)
        client = self.service.miniprogram(APP, expected_session='offline_session')
        client.open()
        call = self.backend.miniprogram_commands.call_args
        self.assertEqual(call.args[0], APP)
        self.assertRegex(call.args[1], r'^helper_mp_[0-9a-f]{32}$')
        self.assertFalse(call.kwargs['check_app'])
        self.assertFalse(call.kwargs['cancelled']())
        self.service.cancel_pending()
        self.assertTrue(call.kwargs['cancelled']())
        self.backend.miniprogram_commands.reset_mock()
        with self.assertRaises(RuntimeError):
            client.click(1, 2)
        self.backend.miniprogram_commands.assert_not_called()

    def test_mismatched_expected_session_rejected_before_client_creation(self):
        self.service.bind(self.backend)
        with self.assertRaises(RuntimeError):
            self.service.miniprogram(APP, expected_session='old_session')
        self.backend.miniprogram_commands.assert_not_called()

    def test_old_client_rejected_after_rebind_even_when_session_string_reused(self):
        self.service.bind(self.backend)
        old = self.service.miniprogram(APP)
        old.open()
        self.service.close()
        new_backend = SimpleNamespace(session_id='offline_session', stop=Mock(),
            miniprogram_commands=Mock(return_value=[{'app_id': APP}]))
        self.service.bind(new_backend)
        self.backend.miniprogram_commands.reset_mock()
        with self.assertRaises(RuntimeError):
            old.input_text('旧连接不可输入')
        self.backend.miniprogram_commands.assert_not_called()
        new_backend.miniprogram_commands.assert_not_called()

    def test_session_change_during_call_discards_ack_without_repeating_action(self):
        self.service.bind(self.backend)
        client = self.service.miniprogram(APP)
        def changed(*args, **kwargs):
            self.backend.session_id = 'replacement_session'
            return [{'app_id': APP}]
        self.backend.miniprogram_commands.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, '连接已结束'):
            client.open()
        self.backend.miniprogram_commands.assert_called_once()


class PluginContextTests(unittest.TestCase):
    def test_miniprogram_requires_capability_and_preserves_expected_connection(self):
        service = Mock()
        context = PluginContext('offline', {}, service, [])
        with self.assertRaises(CapabilityError):
            context.miniprogram(APP, expected_session='offline_session')
        service.miniprogram.assert_not_called()
        allowed = PluginContext('offline', {}, service, ['miniprogram.control'])
        self.assertIs(allowed.miniprogram(APP, expected_session='offline_session'),
                      service.miniprogram.return_value)
        service.miniprogram.assert_called_once_with(APP, expected_session='offline_session')


if __name__ == '__main__':
    unittest.main()
