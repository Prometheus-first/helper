"""撤回通知解析。仅供系统消息使用，不根据正文或显示名称猜测原消息。"""
import re
from xml.etree import ElementTree as ET

from .text import WXID


def message_id(value):
    """JSON 使用十进制字符串，避免浏览器损失 64 位编号精度。"""
    if type(value) is int:
        return str(value) if 0 < value < 2**64 else ''
    if isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value):
        return value if int(value) < 2**64 else ''
    return ''


def parse_recall(content, *, chat_id=None):
    if not isinstance(content, str) or len(content) > 16384:
        return None
    if len(content.encode('utf-8')) > 16384 or re.search(r'<!\s*(DOCTYPE|ENTITY)', content, re.I):
        return None
    value = content.strip()
    prefix, separator, body = value.partition(':\n')
    if separator and WXID.fullmatch(prefix):
        value = body.strip()
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError):
        return None
    stack, count = [(root, 0)], 0
    while stack:
        element, depth = stack.pop()
        count += 1
        if count > 1024 or depth > 16:
            return None
        stack.extend((child, depth+1) for child in element)
    if root.tag != 'sysmsg' or root.get('type') != 'revokemsg':
        return None
    nodes = root.findall('revokemsg')
    if len(nodes) != 1:
        return None
    node = nodes[0]
    def scalar(name):
        fields = node.findall(name)
        return fields[0].text or '' if len(fields) == 1 and not len(fields[0]) else ''
    session = scalar('session').strip()
    # msgid 是另一种旧编号；缺少 newmsgid 时不拿它去匹配 64 位服务器编号。
    identifier = message_id(scalar('newmsgid').strip())
    if (chat_id is not None and session != chat_id) or not WXID.fullmatch(session) or not identifier:
        return None
    return {'message_id': identifier, 'chat_id': session,
            'notice': scalar('replacemsg').strip()[:256]}
