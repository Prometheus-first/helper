"""群系统消息和完整成员快照差分；不依赖 Windows、SDK 或网络。"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
import threading
import time
import xml.etree.ElementTree as ET

from .account import validate_account
from .text import validate_target

MAX_GROUP_SYSTEM_BYTES = 16_384
_SYSTEM_TYPES = frozenset({10000, 10002})
_NAME_HINT_SECONDS = 60
_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_PLACEHOLDER = r"\$(?P<target>[A-Za-z0-9_]{1,64})\$"
_ANY_PLACEHOLDER = r"\$[A-Za-z0-9_]{1,64}\$"
_ACTOR = rf"(?:{_ANY_PLACEHOLDER}|你|群主|管理员)"
_JOIN = r"加入(?:了)?群聊"
_REMOVE = r"移出(?:了)?群聊"
_PATTERNS = (
    ("join", re.compile(rf"{_ACTOR}邀请{_PLACEHOLDER}{_JOIN}")),
    ("join", re.compile(rf"{_PLACEHOLDER}通过扫描{_ACTOR}(?:分享的)?二维码{_JOIN}")),
    ("join", re.compile(rf"{_PLACEHOLDER}(?:通过扫描二维码|通过扫描群二维码|通过群聊二维码|通过群二维码|通过二维码){_JOIN}")),
    ("join", re.compile(rf"{_PLACEHOLDER}{_JOIN}")),
    ("leave", re.compile(rf"{_PLACEHOLDER}(?:主动)?退出(?:了)?群聊")),
    ("leave", re.compile(rf"{_PLACEHOLDER}被{_ACTOR}{_REMOVE}")),
    ("leave", re.compile(rf"{_ACTOR}将{_PLACEHOLDER}{_REMOVE}")),
)
_TEMPLATE_QUOTES = str.maketrans("", "", '\"\'“”「」『』 \t\r\n')
_QUOTED_NAME = re.compile(r'["“「『]([^"”」』\r\n]{1,256})["”」』]')
_PRIVACY_NAME_HINT = re.compile(rf"{_PLACEHOLDER}与群里其他人都不是朋友关系[，,]请注意隐私安全")


class GroupEventError(ValueError):
    """消息或快照不满足可信群事件的结构要求；错误不回显消息正文。"""


@dataclass(frozen=True, slots=True)
class GroupMemberEvent:
    kind: str
    room_id: str
    member_id: str
    nickname: str
    source: str


def _room(value):
    try:
        validate_target(value)
    except ValueError:
        raise GroupEventError("群事件需要有效的群 ID") from None
    if not value.endswith("@chatroom"):
        raise GroupEventError("群事件目标必须是群 ID")
    return value


def _name(value):
    if not isinstance(value, str) or len(value) > 256:
        raise GroupEventError("群成员昵称必须是最多 256 字符的单行文本")
    try:
        if (len(value.encode("utf-8")) > 1024 or
                any(ord(char) < 32 or ord(char) == 127 for char in value)):
            raise GroupEventError("群成员昵称包含控制字符或过长")
    except UnicodeError:
        raise GroupEventError("群成员昵称必须包含有效 UTF-8 字符") from None
    return value.strip()


def _member_id(value):
    try:
        validate_account(value)
    except ValueError:
        raise GroupEventError("群成员必须具有有效的个人微信 ID") from None
    if value.startswith(("v3_", "v4_")):
        raise GroupEventError("临时验证凭据不能当作群成员 ID")
    return value


def _single(parent, tag, *, required=True):
    values = parent.findall(tag)
    if len(values) > 1 or (required and not values):
        raise GroupEventError("群系统 XML 缺少唯一的必要字段")
    return values[0] if values else None


def _leaf(parent, tag, *, required=True):
    value = _single(parent, tag, required=required)
    if value is None:
        return ""
    if len(value):
        raise GroupEventError("群系统 XML 文本字段不能包含子元素")
    return value.text or ""


def _classify(template):
    normalized = template.translate(_TEMPLATE_QUOTES)
    for kind, pattern in _PATTERNS:
        match = pattern.fullmatch(normalized)
        if match:
            return kind, match.group("target")
    return None


def _text_events(content, room_id, self_id):
    # 普通系统文本没有可核对的成员 ID：只保留明确引号里的名字，不猜 wxid。
    names = []
    def replace(match):
        names.append(_name(match.group(1)))
        return f"$quoted_{len(names) - 1}$"
    template = _QUOTED_NAME.sub(replace, content)
    # 非好友加入时，隐私提示可能先于成员资料到达。它仅提供名字，
    # 不能独立触发通知，仍须通过连续名单快照确认唯一的成员变化。
    privacy_hint = _PRIVACY_NAME_HINT.fullmatch(template.translate(_TEMPLATE_QUOTES))
    classified = ("join", privacy_hint.group("target")) if privacy_hint else _classify(template)
    if not classified:
        return ()
    kind, role = classified
    if not role.startswith("quoted_"):
        return ()
    index = int(role.removeprefix("quoted_"))
    if index >= len(names):
        return ()
    name = names[index]
    if not name or name == "你":
        return ()
    return (GroupMemberEvent(kind, room_id, "", name, "system_text"),)


def parse_group_system_message(content: str, *, msg_type: int, room_id: str,
                               self_id: str = "") -> tuple[GroupMemberEvent, ...]:
    """只解析原生 10000/10002 系统消息；聊天正文即使同文也不会成为事件。

    结构化 sysmsgtemplate 从模板中选取动作的目标占位符，再读取同名 link
    的 memberlist/member/username、nickname。邀请者与被邀请者不会混淆。
    名字文本没有 ID，本身不能触发欢迎；只可补充之后由完整名单确认的唯一变化。
    """
    if type(msg_type) is not int or msg_type not in _SYSTEM_TYPES:
        return ()
    _room(room_id)
    if self_id:
        _member_id(self_id)
    if not isinstance(content, str) or not content.strip() or "\0" in content:
        raise GroupEventError("群系统消息必须是非空文本且不能包含 NUL")
    try:
        if len(content) > MAX_GROUP_SYSTEM_BYTES or len(content.encode("utf-8")) > MAX_GROUP_SYSTEM_BYTES:
            raise GroupEventError("群系统消息超过 16384 字节上限")
    except UnicodeError:
        raise GroupEventError("群系统消息必须包含有效 UTF-8 字符") from None
    content = content.strip()
    if content.startswith(room_id + ":\n"):
        content = content[len(room_id) + 2:].lstrip()
    elif content.startswith(room_id + ":\r\n"):
        content = content[len(room_id) + 3:].lstrip()
    elif re.match(r"[^\r\n]+@chatroom:\r?\n", content):
        raise GroupEventError("群系统消息前缀不属于当前群")
    if not content.startswith("<"):
        return _text_events(content, room_id, self_id)
    if _DECLARATION.search(content):
        raise GroupEventError("群系统 XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise GroupEventError("群系统 XML 格式不合法") from None
    pending, count = [(root, 1)], 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 1024 or depth > 16 or len(element.attrib) > 64:
            raise GroupEventError("群系统 XML 结构过于复杂")
        pending.extend((child, depth + 1) for child in element)
    if root.tag != "sysmsg" or root.get("type") != "sysmsgtemplate":
        return ()
    wrapper = _single(root, "sysmsgtemplate")
    template = _single(wrapper, "content_template")
    classified = _classify(_leaf(template, "template"))
    if not classified:
        return ()
    kind, role = classified
    links = _single(template, "link_list")
    selected = [link for link in links.findall("link") if link.get("name") == role]
    if len(selected) != 1:
        raise GroupEventError("群系统 XML 缺少唯一的目标成员列表")
    members = _single(selected[0], "memberlist")
    result, seen = [], set()
    for member in members.findall("member"):
        member_id = _leaf(member, "username").strip()
        nickname = _name(_leaf(member, "nickname", required=False))
        if member_id:
            _member_id(member_id)
        if (self_id and member_id == self_id) or (not member_id and (not nickname or nickname == "你")):
            continue
        key = member_id or ("name:" + nickname)
        if key in seen:
            continue
        seen.add(key)
        result.append(GroupMemberEvent(kind, room_id, member_id, nickname, "system_structured"))
    return tuple(result)


@dataclass(slots=True)
class _RoomState:
    members: dict[str, str] | None = None
    pending: dict[str, tuple[str, int]] = field(default_factory=dict)
    expected: dict[str, tuple[bool, float]] = field(default_factory=dict)
    sequences: OrderedDict = field(default_factory=OrderedDict)
    name_hints: OrderedDict = field(default_factory=OrderedDict)


class GroupMembershipTracker:
    """一账号、一连接的内存成员状态；首次完整快照只建立基线。

    差分须至少连续两次完整快照确认；失败/空/不含机器人自己的快照打断确认。
    系统事件与快照共享基线，且短暂等待缓存跟上系统事件，避免重复欢迎或把
    缓存延迟误报成反向变更。连接变化须 close 后创建新实例，不补发旧事件。
    """

    def __init__(self, account: str, session_id: str, *, confirmations: int = 2,
                 max_groups: int = 128, max_members: int = 10_000,
                 system_grace_seconds: float = 120, clock=time.monotonic):
        self._account = _member_id(account)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("群成员跟踪需要有效的连接 ID")
        for value, minimum, maximum, label in ((confirmations, 2, 10, "确认次数"),
                (max_groups, 1, 1024, "群数量上限"), (max_members, 1, 100_000, "成员上限")):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"群成员{label}不在支持范围内")
        if (isinstance(system_grace_seconds, bool) or
                not isinstance(system_grace_seconds, (int, float)) or
                not math.isfinite(system_grace_seconds) or system_grace_seconds < 0):
            raise ValueError("系统事件缓存等待时间必须是有限的非负数")
        if not callable(clock):
            raise ValueError("群成员时钟必须可调用")
        self._session_id = session_id
        self.confirmations = confirmations
        self.max_groups = max_groups
        self.max_members = max_members
        self.system_grace_seconds = float(system_grace_seconds)
        self._clock = clock
        self._rooms: dict[str, _RoomState] = {}
        self._lock = threading.RLock()
        self._closed = False

    @property
    def account(self):
        return self._account

    @property
    def session_id(self):
        return self._session_id

    def _check(self, session_id):
        if self._closed:
            raise RuntimeError("群成员跟踪所属连接已结束")
        if session_id is not None and session_id != self.session_id:
            raise RuntimeError("群成员事件不属于当前连接")

    def _state(self, room_id):
        if room_id not in self._rooms:
            if len(self._rooms) >= self.max_groups:
                raise GroupEventError("群成员跟踪已达到群数量上限")
            self._rooms[room_id] = _RoomState()
        return self._rooms[room_id]

    def _snapshot(self, snapshot):
        if not isinstance(snapshot, Mapping) or not 1 <= len(snapshot) <= self.max_members:
            return None
        try:
            values = dict(snapshot)
            checked = {_member_id(key): _name(name) for key, name in values.items()}
        except (GroupEventError, ValueError, TypeError, RuntimeError):
            return None
        return checked if self.account in checked else None

    def members(self, room_id: str) -> dict[str, str]:
        """返回确认后的成员名字副本；离开者的旧名保留在对应 leave 事件内。"""
        _room(room_id)
        with self._lock:
            self._check(None)
            state = self._rooms.get(room_id)
            return dict(state.members) if state and state.members is not None else {}

    @staticmethod
    def _expire_name_hints(state, now):
        known_names = set(state.members.values())
        for key, deadline in list(state.name_hints.items()):
            if now >= deadline or (key[0] == "join" and key[1] in known_names):
                state.name_hints.pop(key)

    @staticmethod
    def _take_name_hint(state, kind, candidates):
        # Names never establish identity or membership. Use only one recent name
        # for one independently confirmed change; ambiguous batches are discarded.
        keys = [key for key in state.name_hints if key[0] == kind]
        for key in keys:
            state.name_hints.pop(key)
        return keys[0][1] if candidates == 1 and len(keys) == 1 else ""

    def update(self, room_id: str, snapshot: Mapping[str, str] | None, *,
               session_id: str | None = None) -> tuple[GroupMemberEvent, ...]:
        _room(room_id)
        with self._lock:
            self._check(session_id)
            checked = self._snapshot(snapshot)
            if checked is None:
                state = self._rooms.get(room_id)
                if state:
                    state.pending.clear()
                return ()
            state = self._state(room_id)
            if state.members is None:
                state.members = checked
                return ()
            result, now = [], self._clock()
            self._expire_name_hints(state, now)
            changes = {
                "join": len(checked.keys() - state.members.keys() - {self.account}),
                "leave": len(state.members.keys() - checked.keys() - {self.account}),
            }
            for member_id in sorted(state.members.keys() | checked.keys() |
                                    state.pending.keys() | state.expected.keys()):
                before, after = member_id in state.members, member_id in checked
                expected = state.expected.get(member_id)
                if expected:
                    if after == expected[0] or now >= expected[1]:
                        state.expected.pop(member_id)
                    else:
                        state.pending.pop(member_id, None)
                        continue
                if before == after:
                    state.pending.pop(member_id, None)
                    if after and checked[member_id]:
                        state.members[member_id] = checked[member_id]
                    continue
                kind = "join" if after else "leave"
                previous_kind, count = state.pending.get(member_id, (kind, 0))
                count = count + 1 if previous_kind == kind else 1
                state.pending[member_id] = (kind, count)
                if count < self.confirmations:
                    continue
                nickname = checked[member_id] if after else state.members[member_id]
                hint = self._take_name_hint(state, kind, changes[kind])
                nickname = nickname or hint
                if after:
                    state.members[member_id] = nickname
                else:
                    state.members.pop(member_id)
                state.pending.pop(member_id, None)
                if member_id != self.account:
                    result.append(GroupMemberEvent(kind, room_id, member_id, nickname, "membership"))
            return tuple(result)

    def observe_system(self, room_id: str, content: str, msg_type: int, *, sequence: int = 0,
                       session_id: str | None = None) -> tuple[GroupMemberEvent, ...]:
        _room(room_id)
        with self._lock:
            self._check(session_id)
            if type(sequence) is not int or not 0 <= sequence <= (1 << 64) - 1:
                raise ValueError("群系统消息序号必须是有效的无符号整数")
            events = parse_group_system_message(content, msg_type=msg_type, room_id=room_id,
                                                self_id=self.account)
            if not events:
                return ()
            state = self._state(room_id)
            if sequence:
                key = (msg_type, sequence)
                if key in state.sequences:
                    return ()
                state.sequences[key] = None
                while len(state.sequences) > 256:
                    state.sequences.popitem(last=False)
            # 启动时同步的旧系统消息不能给既有群成员补发欢迎。
            if state.members is None:
                return ()
            self._expire_name_hints(state, self._clock())
            result = []
            for event in events:
                if not event.member_id:
                    if event.nickname:
                        key = (event.kind, event.nickname)
                        if key not in state.name_hints:
                            state.name_hints[key] = self._clock() + _NAME_HINT_SECONDS
                        while len(state.name_hints) > 64:
                            state.name_hints.popitem(last=False)
                    continue
                if event.member_id == self.account:
                    continue
                present = event.member_id in state.members
                if (event.kind == "join") == present:
                    continue
                if event.kind == "join":
                    if len(state.members) >= self.max_members:
                        raise GroupEventError("群成员跟踪已达到成员数量上限")
                    nickname = event.nickname
                    state.members[event.member_id] = nickname
                else:
                    nickname = state.members.pop(event.member_id) or event.nickname
                state.pending.pop(event.member_id, None)
                state.expected[event.member_id] = (event.kind == "join",
                    self._clock() + self.system_grace_seconds)
                while len(state.expected) > self.max_members:
                    state.expected.pop(next(iter(state.expected)))
                result.append(GroupMemberEvent(event.kind, room_id, event.member_id, nickname,
                                               event.source))
            return tuple(result)

    def close(self):
        with self._lock:
            self._rooms.clear()
            self._closed = True
