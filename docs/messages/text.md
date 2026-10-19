# 文本与真实艾特

`native_text` 后端开放文件传输助手、好友、群聊的文本收发，以及真正的群成员艾特。

## 文本发送

在插件里调用：

```python
ctx.send_text("filehelper", "你好，helper")
ctx.reply(message, "已收到你的消息")
```

`ctx.reply` 回复消息所属会话，并核对原消息的连接会话，避免断线重连后把旧回复发到新账号。主动发送使用会话 ID，不能使用显示昵称。

文本应为 1–1023 个 UTF-8 字节，且不能包含 NUL。中文通常占多个字节；文本发送还会应用本地配置中的长度随机等待。

## 群成员艾特

```python
if message.is_group:
    ctx.reply_at(message, "收到，这是一条真正的艾特回复")
```

真实艾特的通知目标使用成员 ID，可见标签优先使用成员在本群的 `/绑定` 称呼，其次使用微信联系人缓存的名字。不能通过只在普通文本里拼接 `@昵称` 获得真实通知。

需要的能力声明：

```json
{
  "api": "v1",
  "description": "文本和群艾特示例",
  "message_types": [1],
  "requires": ["text.receive", "text.send", "group.text.mention"]
}
```

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

插件默认只收到文本消息。框架只处理实际收到的实时消息，不补拉停机时的旧消息。

## 已有示例插件

- `echo`：发送 `echo 你好`，回复“你好”。
- `id_responder`：发送 `test`，回复当前会话 ID。

开始扩展自己的逻辑，见[插件开发](../development/plugins.md)。
