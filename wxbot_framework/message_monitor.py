"""管理台的有界消息缓存；仅保存显示字段，不保存媒体、原始 XML 或下载凭证。"""
from collections import deque
import threading
import time
import uuid


class MessageBuffer:
    def __init__(self, *, capacity=1000, sanitize=str, clock=time.time):
        self.rows = deque(maxlen=capacity)
        self.seen = deque(maxlen=capacity * 2)
        self.seen_keys = set()
        self.sequence = 0
        self.stream_id = uuid.uuid4().hex
        self.lock = threading.Lock()
        self.sanitize, self.clock = sanitize, clock

    def _text(self, value, limit=2000):
        return self.sanitize(value)[:limit] if isinstance(value, str) else ''

    def reset(self):
        with self.lock:
            self.rows.clear()
            self.seen.clear()
            self.seen_keys.clear()
            self.sequence = 0
            self.stream_id = uuid.uuid4().hex

    def append(self, message, *, account='', sender_name=''):
        data = message.data
        # 机器人原生回显与发送入口只记一次；媒体下载状态更新也不重复。
        if message.is_self and data.get('bot_generated') and not data.get('monitor_outgoing'):
            return
        group = bool(message.is_group or message.chat_id.endswith('@chatroom'))
        sender = message.sender_id or (account if message.is_self else '' if group else message.chat_id)
        if message.type == 1:
            content = self._text(message.text)
        elif message.type == 49:
            name = self._text(data.get('file_name'), 256).replace('\\', '/').rsplit('/', 1)[-1]
            content = '[文件] ' + name if name else '[文件/分享]'
        else:
            content = {3: '[图片]', 34: '[语音]', 37: '[好友申请]', 43: '[视频]',
                       47: '[表情]', 62: '[视频]', 10000: '[系统消息]', 10002: '[系统消息]'}.get(
                           message.type, f'[消息类型 {message.type}]')
        now = self.clock()
        row = {'timestamp': now, 'time': time.strftime('%H:%M:%S', time.localtime(now)),
               'date': time.strftime('%Y-%m-%d', time.localtime(now)), 'account': self._text(account, 128),
               'chat_id': self._text(message.chat_id, 128), 'is_group': group,
               'sender_id': self._text(sender, 128),
               'sender_name': '机器人' if data.get('monitor_outgoing') else self._text(sender_name, 128),
               'direction': 'outgoing' if data.get('monitor_outgoing') or message.is_self else 'incoming',
               'type': message.type, 'content': content}
        key = (message.session_id, message.sequence, message.type, message.chat_id, message.is_self)
        deduplicate = bool(message.session_id and message.sequence)
        with self.lock:
            if deduplicate:
                if key in self.seen_keys:
                    return
                if len(self.seen) == self.seen.maxlen:
                    self.seen_keys.discard(self.seen[0])
                self.seen.append(key)
                self.seen_keys.add(key)
            self.sequence += 1
            self.rows.append(dict(row, id=self.sequence))

    def tail(self, after=0):
        if type(after) is not int or after < 0:
            raise ValueError('消息游标必须是非负整数')
        with self.lock:
            reset = after > self.sequence
            available = list(self.rows)
            # 新页面先显示最近 200 条；已有页面按顺序补齐，不跳过未读批次。
            rows = available[-200:] if not after or reset else [row for row in available if row['id'] > after][:200]
            cursor = rows[-1]['id'] if rows else self.sequence
            return {'rows': [dict(row) for row in rows], 'cursor': cursor, 'latest': self.sequence,
                    'stream_id': self.stream_id, 'reset': reset,
                    'dropped': bool(after and available and after < available[0]['id'] - 1)}
