"""管理台的有界消息缓存；仅保存显示字段，不保存媒体、原始 XML 或下载凭证。"""
from collections import deque, OrderedDict
import json
import threading
import time
import uuid
from .recall import message_id


class MessageBuffer:
    def __init__(self, *, capacity=1000, sanitize=str, clock=time.time):
        self.rows = deque(maxlen=capacity)
        self.seen = deque(maxlen=capacity * 2)
        self.seen_keys = set()
        self.forwarded_rows = {}
        self.recalls = OrderedDict()
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
            self.forwarded_rows.clear()
            self.recalls.clear()
            self.sequence = 0
            self.stream_id = uuid.uuid4().hex

    def append(self, message, *, account='', sender_name=''):
        data = message.data
        account = account or data.get('self_wxid', '')
        if message.type in (10000, 10002) and isinstance(data.get('recall'), dict):
            self._recall(message, account)
            return
        # 机器人原生回显与发送入口只记一次；媒体下载状态更新也不重复。
        if message.is_self and data.get('bot_generated') and not data.get('monitor_outgoing'):
            return
        group = bool(message.is_group or message.chat_id.endswith('@chatroom'))
        sender = message.sender_id or (account if message.is_self else '' if group else message.chat_id)
        if message.type == 1:
            content = self.sanitize(message.text) if isinstance(message.text, str) else ''
        elif message.type == 49:
            name = self._text(data.get('file_name'), 256).replace('\\', '/').rsplit('/', 1)[-1]
            if data.get('app_type') == 19:
                content = '[合并转发] ' + self._text(data.get('forwarded_title'), 256)
                content += '（无法解析）' if data.get('forwarded_status') == 'failed' else (
                    f"（{len(data.get('forwarded_records', ()))} 条" +
                    ('，已截取' if data.get('forwarded_truncated') else '') + '）')
            elif data.get('metadata_unavailable'):
                content = '[文件/分享：内容过长或不可读]'
            else:
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
               'type': message.type, 'content': content,
               'message_id': message_id(data.get('native_message_id'))}
        if group:
            role = data.get('sender_role', 'unknown')
            row['sender_role'] = role if role in ('owner', 'admin', 'member') else 'unknown'
        projected = [row]
        records = data.get('forwarded_records')
        if data.get('app_type') == 19 and isinstance(records, (list, tuple)):
            for index, item in enumerate(records[:200]):
                if not isinstance(item, dict):
                    continue
                kind = item.get('kind')
                if kind == 'text':
                    body = self._text(item.get('text'))
                elif kind == 'image':
                    body = '[图片]' + {'ready':' 已保存', 'pending':' 等待下载', 'failed':' 下载失败',
                                      'unavailable':' 不可下载', 'skipped':' 未下载'}.get(item.get('image_status'), '')
                else:
                    body = {'voice':'[语音]', 'video':'[视频]', 'file':'[文件]', 'link':'[链接]',
                            'forwarded':'[嵌套合并转发]'}.get(kind, '[其他消息]')
                projected.append(dict(row, type=3 if kind == 'image' else 1 if kind == 'text' else 49,
                    content=body, sender_id='', sender_name=self._text(item.get('sender_name'),128),
                    sender_role='unknown',
                    forwarded=True, forwarded_index=index+1,
                    source_time=self._text(item.get('source_time'),64),
                    forwarded_by=self._text(sender_name or sender,128),
                    forwarded_by_id=self._text(sender,128)))
        key = (row['account'], message.session_id, message.sequence, message.type,
               message.chat_id, message.is_self, row['message_id'])
        deduplicate = bool(message.session_id and message.sequence)
        with self.lock:
            if deduplicate:
                if key in self.seen_keys:
                    previous = self.forwarded_rows.get(key)
                    if previous is not None and len(previous) == len(projected):
                        positions = {item['id']: i for i, item in enumerate(self.rows)}
                        for identifier, updated in zip(previous, projected):
                            position = positions.get(identifier)
                            if position is None:
                                continue
                            old = self.rows[position]
                            # 到达时间保持不变；revision 驱动增量刷新，id 保持稳定。
                            comparable = dict(updated, timestamp=old['timestamp'], time=old['time'], date=old['date'])
                            for field in ('recalled', 'recalled_at', 'recall_notice'):
                                if field in old:
                                    comparable[field] = old[field]
                            if comparable == {k:v for k,v in old.items() if k not in ('id','revision')}:
                                continue
                            self.sequence += 1
                            self.rows[position] = dict(comparable, id=identifier, revision=self.sequence)
                    return
                if len(self.seen) == self.seen.maxlen:
                    oldest = self.seen[0]
                    self.seen_keys.discard(oldest)
                    self.forwarded_rows.pop(oldest, None)
                self.seen.append(key)
                self.seen_keys.add(key)
            identifiers = []
            recall_key = (row['account'], row['chat_id'], row['message_id'])
            recall = self.recalls.get(recall_key) if row['message_id'] else None
            placeholder = next((i for i, old in enumerate(self.rows) if recall and
                old.get('recall_missing') and old.get('message_id') == row['message_id'] and
                old['account'] == row['account'] and old['chat_id'] == row['chat_id']), None)
            for index, projected_row in enumerate(projected):
                self.sequence += 1
                if recall:
                    projected_row.update(recalled=True, recalled_at=recall[0], recall_notice=recall[1])
                if index == 0 and placeholder is not None:
                    identifier = self.rows[placeholder]['id']
                    self.rows[placeholder] = dict(projected_row, id=identifier, revision=self.sequence)
                else:
                    identifier = self.sequence
                    self.rows.append(dict(projected_row, id=identifier, revision=self.sequence))
                identifiers.append(identifier)
            if deduplicate and records is not None:
                self.forwarded_rows[key] = identifiers

    def _recall(self, message, account):
        recall = message.data['recall']
        identifier = message_id(recall.get('message_id'))
        if not identifier or recall.get('chat_id') != message.chat_id:
            return
        account = self._text(account, 128)
        chat = self._text(message.chat_id, 128)
        key = (account, chat, identifier)
        now = self.clock()
        notice = self._text(recall.get('notice'), 256)
        with self.lock:
            if key in self.recalls:
                return
            self.recalls[key] = (now, notice)
            if len(self.recalls) > self.rows.maxlen:
                self.recalls.popitem(last=False)
            matched = False
            for index, old in enumerate(self.rows):
                if (old['account'], old['chat_id'], old.get('message_id')) != key:
                    continue
                self.sequence += 1
                self.rows[index] = dict(old, recalled=True, recalled_at=now,
                    recall_notice=notice, revision=self.sequence)
                matched = True
            if not matched:
                self.sequence += 1
                self.rows.append({'id':self.sequence, 'revision':self.sequence, 'timestamp':now,
                    'time':time.strftime('%H:%M:%S',time.localtime(now)),
                    'date':time.strftime('%Y-%m-%d',time.localtime(now)), 'account':account,
                    'chat_id':chat, 'is_group':message.is_group, 'sender_id':'', 'sender_name':'',
                    'direction':'incoming', 'type':message.type,
                    'content':'[撤回消息] 原消息未捕获', 'message_id':identifier,
                    'recalled':True, 'recalled_at':now, 'recall_notice':notice, 'recall_missing':True})

    def tail(self, after=0):
        if type(after) is not int or after < 0:
            raise ValueError('消息游标必须是非负整数')
        with self.lock:
            reset = after > self.sequence
            available = list(self.rows)
            # 新页面先显示最近 200 条；已有页面按顺序补齐，不跳过未读批次。
            initial = not after or reset
            candidates = sorted(available[-200:], key=lambda row:row['revision']) if initial else sorted(
                (row for row in available if row['revision'] > after), key=lambda row:row['revision'])[:200]
            # 消息正文不截断；按实际 JSON 字节分页，避免超过子进程 4 MiB 响应限制。
            budget = 3 * 1024 * 1024
            rows, used = [], 0
            for row in candidates:
                size = len(json.dumps(row, ensure_ascii=False).encode('utf-8')) + 1
                if rows and used + size > budget:
                    break
                rows.append(row)
                used += size
            cursor = rows[-1]['revision'] if rows else self.sequence
            return {'rows': [dict(row) for row in rows], 'cursor': cursor, 'latest': self.sequence,
                    'stream_id': self.stream_id, 'reset': reset,
                    'dropped': bool(after and available and after < available[0]['id'] - 1)}
