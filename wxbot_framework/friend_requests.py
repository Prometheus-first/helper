"""好友申请 XML 与内存目录；验证凭据不写入磁盘或公开元数据。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import math
import re
import secrets
import threading
import time
from typing import Callable
import xml.etree.ElementTree as ET

from .account import validate_account

MAX_FRIEND_REQUEST_XML_BYTES = 16_384
_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_DECIMAL = re.compile(r"[0-9]{1,10}")
_REQUEST_ID = re.compile(r"fr_[0-9a-f]{32}")


class FriendRequestError(ValueError):
    """错误文字不会携带原始 XML、验证票据或对方的验证消息。"""


@dataclass(frozen=True, slots=True)
class FriendRequest:
    sender_id: str
    nickname: str
    verification: str
    scene: int
    opcode: int
    encrypted_username: str = field(repr=False)
    ticket: str = field(repr=False)
    raw_xml: str = field(repr=False, compare=False)


def _attribute(root, name, maximum=8192, *, multiline=False):
    value = root.get(name, "")
    if (len(value.encode("utf-8")) > maximum or
            any((ord(char) < 32 and not (multiline and char in "\n\r\t")) or
                ord(char) == 127 for char in value)):
        raise FriendRequestError("好友申请 XML 字段过长或包含控制字符")
    return value


def parse_friend_request(content: str) -> FriendRequest:
    """解析 type=37 的 msg 根属性；调用方须先检查原生消息类型与接收账号。

    这里只把 encryptusername/ticket 当作临时验证凭据，绝不将 v3/v4 字符串
    当成可用于聊天的微信 ID。未知属性保留在隐藏的原 XML 中，不公开展示。
    """
    if not isinstance(content, str) or not content.strip():
        raise FriendRequestError("好友申请 XML 必须是非空字符串")
    if len(content) > MAX_FRIEND_REQUEST_XML_BYTES or "\0" in content:
        raise FriendRequestError("好友申请 XML 过长或包含 NUL")
    try:
        if len(content.encode("utf-8")) > MAX_FRIEND_REQUEST_XML_BYTES:
            raise FriendRequestError("好友申请 XML 超过 16384 字节上限")
    except UnicodeError:
        raise FriendRequestError("好友申请 XML 必须包含有效 UTF-8 字符") from None
    if _FORBIDDEN_DECLARATION.search(content):
        raise FriendRequestError("好友申请 XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise FriendRequestError("好友申请 XML 格式不合法") from None
    pending, count = [(root, 1)], 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 256 or depth > 16 or len(element.attrib) > 64:
            raise FriendRequestError("好友申请 XML 结构过于复杂")
        pending.extend((child, depth + 1) for child in element)
    if root.tag != "msg":
        raise FriendRequestError("好友申请 XML 必须具有 msg 根元素")
    encrypted_username = _attribute(root, "encryptusername")
    ticket = _attribute(root, "ticket")
    for value, prefix in ((encrypted_username, "v3_"), (ticket, "v4_")):
        if (not value.startswith(prefix) or len(value) <= len(prefix) or
                not value.isascii() or any(char.isspace() for char in value)):
            raise FriendRequestError("好友申请缺少有效的临时验证凭据")
    scene = _attribute(root, "scene", 10)
    if not _DECIMAL.fullmatch(scene) or int(scene) > (1 << 32) - 1:
        raise FriendRequestError("好友申请 scene 必须是有效的无符号整数")
    opcode = _attribute(root, "opcode", 1)
    if not _DECIMAL.fullmatch(opcode) or not 0 <= int(opcode) <= 6:
        raise FriendRequestError("好友申请必须包含 0 到 6 的明确 opcode")
    sender = _attribute(root, "fromusername")
    if sender.startswith(("v3_", "v4_")):
        sender = ""
    if sender:
        try:
            validate_account(sender)
        except ValueError:
            raise FriendRequestError("好友申请中的微信 ID 不合法") from None
    nickname = _attribute(root, "fromnickname", 1024) or _attribute(root, "nickname", 1024)
    return FriendRequest(sender, nickname, _attribute(root, "content", 4096, multiline=True),
                         int(scene), int(opcode),
                         encrypted_username, ticket, content)


@dataclass(frozen=True, slots=True)
class PendingFriendRequest:
    request_id: str
    request: FriendRequest = field(repr=False)
    status: str
    created_at: float
    expires_at: float
    error: str = ""

    def public(self) -> dict:
        """为控制台/插件提供新字典；不含原 XML 或验证凭据。"""
        return {"request_id": self.request_id, "sender_id": self.request.sender_id,
                "nickname": self.request.nickname, "verification": self.request.verification,
                "scene": self.request.scene, "opcode": self.request.opcode,
                "status": self.status, "error": self.error}


class PendingFriendRequests:
    """单账号、单连接的有界内存目录；重连应创建新目录并关闭旧目录。

    默认保留 1 小时、最多 512 条（包括已处理记录），相同 v3/v4/scene 的申请
    在保留期间只生成一个随机 ID。TTL 是内存保留策略，并非微信票据有效期。
    超量时删除最早记录；过期或关闭后凭据不再可从此目录获取。失败和本地忽略
    状态也参与去重，收到重复 XML 不会自动重新尝试。原生操作互斥由服务承担。
    """

    def __init__(self, account: str, session_id: str, *, ttl_seconds: float = 3600,
                 max_requests: int = 512, clock: Callable[[], float] = time.monotonic):
        validate_account(account)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError("好友申请目录需要有效的连接 ID")
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float)) or
                not math.isfinite(ttl_seconds) or ttl_seconds <= 0):
            raise ValueError("好友申请保留时间必须是有限正数")
        if type(max_requests) is not int or not 1 <= max_requests <= 10_000:
            raise ValueError("好友申请内存上限必须为 1 到 10000 条")
        if not callable(clock):
            raise ValueError("好友申请时钟必须可调用")
        self._account = account
        self._session_id = session_id
        self.ttl_seconds = float(ttl_seconds)
        self.max_requests = max_requests
        self._clock = clock
        self._records: OrderedDict[str, PendingFriendRequest] = OrderedDict()
        self._lock = threading.RLock()
        self._closed = False

    @property
    def account(self):
        return self._account

    @property
    def session_id(self):
        return self._session_id

    def _prune(self):
        if self._closed:
            raise RuntimeError("好友申请目录所属连接已结束")
        now = self._clock()
        for key, record in tuple(self._records.items()):
            if record.expires_at <= now:
                del self._records[key]
        return now

    def _check_owner(self, account, session_id):
        if account != self.account or session_id != self.session_id:
            raise RuntimeError("好友申请不属于当前账号或连接")

    def add(self, request: FriendRequest) -> dict:
        if not isinstance(request, FriendRequest):
            raise TypeError("好友申请目录只接收已解析的 FriendRequest")
        # 不能通过手工构造数据类绕过凭据和 XML 校验。等价空白 XML 可正常去重。
        checked = parse_friend_request(request.raw_xml)
        if checked != request:
            raise FriendRequestError("好友申请字段与原 XML 不一致")
        # type=37 同时承载申请与状态通知；只允许明确入站申请进入待通过目录。
        # 非 2 的消息不入目录，也就不会覆盖同一凭据已有的 accepted 记录。
        if request.opcode != 2:
            raise FriendRequestError("只有 opcode=2 的入站好友申请可进入待处理目录")
        with self._lock:
            now = self._prune()
            identity = (request.encrypted_username, request.ticket, request.scene)
            for record in self._records.values():
                previous = record.request
                if (previous.encrypted_username, previous.ticket, previous.scene) == identity:
                    return record.public()
            while len(self._records) >= self.max_requests:
                self._records.popitem(last=False)
            request_id = "fr_" + secrets.token_hex(16)
            while request_id in self._records:
                request_id = "fr_" + secrets.token_hex(16)
            record = PendingFriendRequest(request_id, request, "pending", now,
                                          now + self.ttl_seconds)
            self._records[request_id] = record
            return record.public()

    def list(self) -> list[dict]:
        with self._lock:
            self._prune()
            return [record.public() for record in self._records.values()]

    def resolve(self, request_id: str, *, account: str, session_id: str) -> PendingFriendRequest:
        """仅供服务内部读取临时凭据；操作前仍须核对实际微信登录账号。"""
        with self._lock:
            self._check_owner(account, session_id)
            self._prune()
            if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
                raise ValueError("好友申请 ID 不合法")
            try:
                record = self._records[request_id]
            except KeyError:
                raise KeyError("好友申请不存在、已过期或已从内存清除") from None
            if record.request.opcode != 2:
                raise FriendRequestError("这条消息不是可通过的入站好友申请")
            return record

    def _mark(self, request_id, status, account, session_id):
        with self._lock:
            record = self.resolve(request_id, account=account, session_id=session_id)
            if record.status == status:
                return record.public()
            if record.status != "pending":
                raise RuntimeError("好友申请已处理，不能重复操作")
            record = replace(record, status=status,
                             error="friend_request_failed" if status == "failed" else "")
            self._records[request_id] = record
            return record.public()

    def mark_accepted(self, request_id: str, *, account: str, session_id: str) -> dict:
        return self._mark(request_id, "accepted", account, session_id)

    def mark_failed(self, request_id: str, *, account: str, session_id: str, error=None) -> dict:
        # 不调用 str(error)，原生异常可能携带票据；仅保留固定公开错误码。
        return self._mark(request_id, "failed", account, session_id)

    def mark_rejected(self, request_id: str, *, account: str, session_id: str) -> dict:
        """只在本地忽略此申请；不表示已向微信发送拒绝操作。"""
        return self._mark(request_id, "rejected", account, session_id)

    def close(self):
        with self._lock:
            self._records.clear()
            self._closed = True
