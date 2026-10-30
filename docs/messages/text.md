# 文本与真实艾特

`native_text` 后端开放文件传输助手、好友、群聊的文本收发，以及真正的群成员艾特。

## 文本发送

在插件里调用：

```python
ctx.send_text("filehelper", "你好，helper")
ctx.reply(message, "已收到你的消息")
```

`ctx.reply` 回复消息所属会话，并核对原消息的连接会话，避免断线重连后把旧回复发到新账号。主动发送使用会话 ID，不能使用显示昵称。

文本必须非空且不能包含 NUL。超出桥接单条 1023 个 UTF-8 字节时，框架按字符边界拆成多条顺序发送；真实艾特只出现在第一条。每条沿用配置的随机等待；途中失败或连接中断时，之前的部分可能已经发出，不会自动重发。

## 群成员艾特

```python
if message.is_group:
    ctx.reply_at(message, "收到，这是一条真正的艾特回复")
```

真实艾特的通知目标使用成员 ID，可见标签优先使用成员在本群的 `/绑定` 称呼，其次使用微信联系人缓存的名字。不能通过只在普通文本里拼接 `@昵称` 获得真实通知。

主动发送时可指定一位或多位群成员；显示昵称和真实通知目标分别传入：

```python
if message.is_group:
    rows = ctx.group_members(message.chat_id, expected_session=message.session_id)
    member = next((row for row in rows or () if row["wxid"] == message.sender_id), None)
    if member is not None:
        ctx.send_at_text(message.chat_id, "请查看这条消息", [member["wxid"]],
                         expected_session=message.session_id,
                         display_names=[member["nickname"] or member["wxid"]])
```

`members` 与 `display_names` 按顺序对应，一次支持 1–8 位成员；昵称每个最多 64 字符。艾特标签须能装入第一条消息，超长正文自动分段；使用消息的 `session_id` 可防止重连后发送旧任务。未提供 `display_names` 时以 wxid 作为可见标签。

网页也支持真实艾特：进入“群聊与会话 → 发送消息”，刷新会话、选择群并勾选成员，再填写正文发送。网页会在发送前重新核对成员名单并使用当前群昵称；普通好友会话不提供艾特选项。完整流程见[网页管理台](../guide/web-panel.md#发送文字和艾特成员)。

需要的能力声明：

```json
{
  "api": "v1",
  "description": "文本和群艾特示例",
  "message_types": [1],
  "requires": ["text.receive", "text.send", "group.text.mention"]
}
```

## 文本接收

普通文本接收支持最多 262143 个 UTF-8 字节（群消息包含微信附带的成员 ID 前缀）。完整正文交给插件和聊天记录，不按发送时的 1023 字节分段。超过接收上限或原生内容不可读取时会记录警告并跳过，不将截断正文当作完整消息处理。

## 收到的消息字段

| 字段 | 含义 |
| --- | --- |
| `message.type` | 文本为 `1`，普通图片为 `3` |
| `message.text` | 文本正文 |
| `message.chat_id` | 当前会话 ID，群以 `@chatroom` 结尾 |
| `message.sender_id` | 实际发言者 ID |
| `message.is_group` | 是否来自群聊 |
| `message.is_self` | 是否是当前账号同步来的消息 |
| `message.session_id` | 消息所属框架连接 |
| `message.data` | 消息附加字段快照 |
| `message.data["sender_role"]` | 实时群消息作者身份：`owner`、`admin`、`member` 或 `unknown` |

插件默认只收到文本消息。框架只处理实际收到的实时消息，不补拉停机时的旧消息。

群内身份取自当前连接最近 30 秒内读取的群缓存，约每 15 秒检查；无法确认、缓存过期或连接变化时为 `unknown`。主动查询可使用 `ctx.group_member_role(room, wxid, expected_session=message.session_id)`。这只是身份信息，不自动赋予插件管理权限，详见[群成员与身份](../guide/friends-and-groups.md#群内身份和真实艾特)。

## 已有示例插件

- `echo`：发送 `echo 你好`，回复“你好”。
- `id_responder`：发送 `test`，回复当前会话 ID。

开始扩展自己的逻辑，见[插件开发](../development/plugins.md)。
