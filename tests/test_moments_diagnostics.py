"""失败探针保留定位信息，同时不泄漏账号、内容及凭据。"""
import unittest

from native.moments_transport import _result, _emit_diagnostics


class MomentsDiagnosticTests(unittest.TestCase):
    def inspect_result(self, report, *, execute=False):
        return _result(report, execute=execute, account='wxid_offline_robot', content_bytes=10,
                       image_count=0, visibility='private', dispatch_code=0, cleanup_ok=False)

    def test_cleanup_failure_retains_only_bounded_diagnostic_fields(self):
        result = self.inspect_result({'stage': 169, 'status': 31, 'exception': 0xc0000005,
                                      'outcome': 'cleanup_unconfirmed', 'account': 'wxid_private',
                                      'client_id': 'secret', 'content': 'private content',
                                      'path': 'C:/private/report.json'})
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unload_blocked'])
        self.assertEqual(result['diagnostics'], {'stage': 169, 'status': 31, 'exception': 0xc0000005,
                                                 'outcome': 'cleanup_unconfirmed'})
        self.assertNotIn('wxid_private', repr(result))
        self.assertNotIn('secret', repr(result))
        self.assertNotIn('private content', repr(result))
        self.assertNotIn('report.json', repr(result))

    def test_invalid_values_do_not_become_diagnostic_text(self):
        for report in (None, {}, {'stage': True, 'status': -1, 'exception': 2**40,
                                  'outcome': 'C:/private/report.json'}):
            self.assertNotIn('diagnostics', self.inspect_result(report))

    def test_publishing_does_not_expose_probe_diagnostics(self):
        result = self.inspect_result({'stage': 169, 'status': 31, 'exception': 0}, execute=True)
        self.assertNotIn('diagnostics', result)

    def test_explicit_diagnostic_sink_has_server_code_but_no_private_fields(self):
        rows = []
        _emit_diagnostics(rows.append, {'stage': 166, 'ret_code': -5, 'accepted': True,
                                       'outcome': 'failed', 'image_count': 2, 'media_ready': 2,
                                       'account': 'wxid_private', 'client_id': 'secret',
                                       'path': 'C:/private/data', 'content': 'private body'},
                          cleanup_ok=True, dispatch_code=0)
        self.assertEqual(rows[0]['ret_code'], -5)
        self.assertTrue(rows[0]['cleanup_confirmed'])
        self.assertNotIn('private', repr(rows))
        self.assertNotIn('secret', repr(rows))

    def test_failing_diagnostic_sink_does_not_interrupt_cleanup(self):
        def failed(_):
            raise RuntimeError('diagnostic failure')
        _emit_diagnostics(failed, None, cleanup_ok=True, dispatch_code=0)


if __name__ == '__main__':
    unittest.main()
