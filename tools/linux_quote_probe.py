"""受控实测 Linux 私聊引用；仅向发送测试口令的账号回复一次。"""
from tools import linux_media_test as media
from wxbot_framework.account import validate_account
from wxbot_framework.quoted import validate_quote_id

_execute = media.MediaTest.execute


def on_event(self, event):
    if event.get('type') == 49:
        original = getattr(self, 'quote_event', None)
        if not original or (event.get('sender'), event.get('receiver')) != (original['sender'], original['receiver']):
            return
        from wxbot_framework.quoted import QuoteError, parse_quote
        try:
            quote = parse_quote(event.get('text', ''))
        except QuoteError:
            print('测试引用解析失败', flush=True)
            return
        if quote is not None:
            print('收到原生引用：True；引用内容匹配：',
                  quote.reference.text == 'WX_LINUX_BRIDGE_07', flush=True)
        return
    if event.get('type') != 1 or event.get('text') != 'WX_LINUX_BRIDGE_07':
        return
    if getattr(self, 'quote_event', None):
        return
    try:
        profile = self.sender.current_profile()
        self.quote_event = dict(event)
        self.quote_event['valid_account'] = (profile.get('logged_in') is True and
                                             profile.get('wxid') == event.get('receiver'))
        print('已收到测试文字；账号匹配：', self.quote_event['valid_account'],
              '原消息编号可用：', bool(event.get('native_message_id')), flush=True)
    except Exception as error:
        print('测试文字检查失败：', type(error).__name__, flush=True)


def execute(self, operation, *, repeat=False):
    if operation == 'arm_quote':
        profile = self.sender.current_profile()
        if profile.get('logged_in') is not True:
            raise RuntimeError('请先确认微信登录')
        self.sender.bind_account(validate_account(profile['wxid']))
        self.quote_scope = self.sender.bind_all_image_scopes(profile['wxid'])
        self.quote_event = None
        self.attempted.discard('probe_quote')
        return {'success': True}
    if operation != 'probe_quote':
        return _execute(self, operation, repeat=repeat)
    if 'probe_quote' in self.attempted:
        raise RuntimeError('本轮已尝试发送，不自动重试')
    event = getattr(self, 'quote_event', None)
    if not event or not event['valid_account'] or not event.get('native_message_id'):
        raise RuntimeError('尚未收到有效测试文字')
    peer = validate_account(event['sender'])
    account = validate_account(event['receiver'])
    if peer == account or self.sender.current_profile().get('wxid') != account:
        raise RuntimeError('当前账号或测试会话不匹配')
    server_id = validate_quote_id(event['native_message_id'])
    self.sender.allowed_targets = frozenset({peer})
    self.attempted.add('probe_quote')
    self.sender.send_quote_text(peer, server_id, '引用回复测试', scope_generation=self.quote_scope)
    return {'success': True}


if __name__ == '__main__':
    media.MediaTest.on_event = on_event
    media.MediaTest.execute = execute
    media.main()
