"""从 Helper 已有的消息监控与群文字库导出会话。"""
from contextlib import closing, contextmanager
import csv
from datetime import date, datetime, time, timezone
import heapq
import io
import json
import sqlite3
import tempfile

from .chat_archive import find_native, merge_replay, native_key
from .panel_common import PanelError, redact_full
from .text import validate_target


@contextmanager
def group_history(path):
    if path is None or not path.is_file():
        yield None
        return
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()
            yield db if exists else None
    except sqlite3.Error:
        raise PanelError('无法读取 Helper 的本地群聊记录', 503) from None


def conversations(rows, db, archive, account, names):
    chats = {row['chat_id']: row['is_group'] for row in rows if row['chat_id']}
    if archive is not None:
        for (chat,) in archive.execute('SELECT DISTINCT chat_id FROM messages WHERE account=?', (account,)):
            chats.setdefault(chat, chat.endswith('@chatroom'))
    if db is not None:
        for (room,) in db.execute('SELECT DISTINCT room FROM messages WHERE account=?', (account,)):
            chats[room] = True
    return [{'chat_id': chat, 'is_group': group, 'name': names.get(chat, '')}
            for chat, group in sorted(chats.items(), key=lambda item:
                (not item[1], names.get(item[0], item[0]).casefold(), item[0]))]


def _date(value):
    if value == '':
        return None
    try:
        day = date.fromisoformat(value)
        if day.isoformat() == value:
            return day
    except ValueError:
        pass
    raise ValueError('日期应为 YYYY-MM-DD')


def _utc_bound(day, clock):
    try:
        return datetime.combine(day, clock).astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        raise ValueError('日期超出系统支持范围') from None


def _group_rows(db, archive, account, chat, start, end, duplicates):
    if db is None:
        return
    query = '''SELECT sender_id, text, received_at, session_id, sequence, is_self
               FROM messages WHERE account=? AND room=?'''
    values = [account, chat]
    if start:
        query += ' AND received_at>=?'
        values.append(_utc_bound(start, time.min))
    if end:
        query += ' AND received_at<=?'
        values.append(_utc_bound(end, time.max))
    for sender, text, utc, session, sequence, is_self in db.execute(query + ' ORDER BY received_at, id', values):
        if (session, sequence) in duplicates or (archive is not None and sequence > 0 and archive.execute(
            '''SELECT 1 FROM messages WHERE account=? AND chat_id=? AND capture_session=? AND capture_sequence=?
                UNION ALL
                SELECT 1 FROM message_capture_aliases WHERE account=? AND chat_id=? AND session=? AND sequence=?
                LIMIT 1''', (account, chat, session, sequence) * 2).fetchone()):
            continue
        when = datetime.fromisoformat(utc).astimezone()
        yield {'timestamp': when.timestamp(), 'date': when.date().isoformat(),
               'time': when.strftime('%H:%M:%S'), 'chat_id': chat, 'sender_id': sender,
               'direction': 'outgoing' if is_self else 'incoming', 'type': 1, 'content': redact_full(text)}


def _archive_rows(db, account, chat, first, last, stream, ids, recent, replaced):
    if db is None:
        return
    query = 'SELECT stream, id, native_id, forwarded_part, payload FROM messages WHERE account=? AND chat_id=?'
    values = [account, chat]
    if first:
        query += ' AND day>=?'
        values.append(first)
    if last:
        query += ' AND day<=?'
        values.append(last)
    for source, identifier, native_id, part, payload in db.execute(query + ' ORDER BY captured_at, stream, id', values):
        key = native_id, part
        if key in replaced or (native_id is None and source == stream and identifier in ids):
            continue
        row = json.loads(payload)
        yield merge_replay(row, recent[key], same_row=source == stream and identifier == recent[key]['id']) \
            if key in recent else row


def _visible_rows(rows, archive, account, chat):
    visible, by_native = [], {}
    for row in rows:
        key = native_key(row)
        if key[0] is None:
            visible.append(row)
        else:
            previous = by_native.get(key)
            by_native[key] = merge_replay(previous, row) if previous else row
    recent, replaced = {}, set()
    for key, row in by_native.items():
        saved = find_native(archive, account, chat, *key) if archive is not None else None
        if not saved:
            visible.append(row)
            continue
        archived = json.loads(saved[-1])
        if archived.get('recall_missing') and not row.get('recall_missing'):
            replaced.add(key)
            visible.append(merge_replay(archived, row))
        else:
            recent[key] = row
    return visible, recent, replaced


def _csv_cell(value):
    if isinstance(value, str) and (value.startswith(('\t', '\r', '\n')) or
                                   value.lstrip().startswith(('=', '+', '-', '@', '＝', '＋', '－', '＠'))):
        return "'" + value
    return value


@contextmanager
def export_csv(stream, rows, db, archive, account, names, filters):
    chat = validate_target(filters.get('chat_id'))
    start, end = _date(filters.get('start', '')), _date(filters.get('end', ''))
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    group = chat.endswith('@chatroom')
    first, last = start.isoformat() if start else '', end.isoformat() if end else ''
    selected = [row for row in rows if row['chat_id'] == chat]
    visible, recent, replaced = _visible_rows(selected, archive, account, chat)
    visible = [row for row in visible if
               (not first or first <= row['date']) and (not last or row['date'] <= last)]
    visible.sort(key=lambda row: row['timestamp'])
    archived = _archive_rows(archive, account, chat, first, last, stream, {row['id'] for row in selected},
                             recent, replaced)
    sources = [visible, archived]
    if group:
        duplicates = {row['_capture'] for row in selected if row.get('_capture')}
        sources.append(_group_rows(db, archive, account, chat, start, end, duplicates))
    records = heapq.merge(*sources, key=lambda row: row['timestamp'])

    with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode='w+b') as output, \
            io.TextIOWrapper(output, encoding='utf-8-sig', newline='') as text:
        writer = csv.writer(text)
        writer.writerow(('记录时间', '会话类型', '会话名称', '会话 ID', '发送者', '发送者 ID', '方向',
                         '消息类型', '正文', '已撤回', '微信消息 ID', '转发原消息时间'))
        has_rows = False
        name = redact_full(names.get(chat, ''))
        for row in records:
            has_rows = True
            fields = (f"{row['date']} {row['time']}", '群聊' if group else '私聊', name, chat,
                      row.get('sender_name') or ('' if row.get('forwarded') else row['sender_id']),
                      row['sender_id'], '发出' if row['direction'] == 'outgoing' else '收到', row['type'],
                      row['content'], '是' if row.get('recalled') else '', row.get('message_id', ''),
                      row.get('source_time', ''))
            writer.writerow(map(_csv_cell, fields))
        if not has_rows:
            raise PanelError('所选日期内没有已记录的消息', 404)
        text.flush()
        size = output.tell()
        output.seek(0)
        yield output, size
