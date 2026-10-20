# 插件开发

插件通过 Python 接口处理消息，不需要接触 DLL 或原生地址。

## 新建插件

复制 `plugins/_template`，改成不以下划线开头的新目录，例如 `plugins/hello`：

```text
plugins/
└── hello/
    ├── __init__.py
    └── plugin.json
```

在 `plugin.json` 声明接口和能力：

```json
{
  "api": "v1",
  "description": "收到 /hello 后回复",
  "message_types": [1],
  "requires": ["text.receive", "text.send"]
}
```

在 `__init__.py` 实现 `Plugin`：

```python
class Plugin:
    def on_load(self, ctx):
        # 本插件配置来自 bot_config.json 的 plugins.hello。
        self.greeting = ctx.config.get("greeting", "你好，插件已运行")

    def on_message(self, message, ctx):
        if message.type == 1 and message.text.strip() == "/hello":
            # 回复原消息所属会话，保留连接隔离检查。
            ctx.reply(message, self.greeting)

    def on_unload(self, ctx):
        # 如创建了线程、定时器或文件连接，在这里释放。
        pass
```

在本地 `bot_config.json` 的 `plugins` 中增加：

```json
{
  "hello": {
    "enabled": true,
    "greeting": "你好，helper"
  }
}
```

以上是 `plugins` 内的条目，保留其他配置不变。执行 `python main.py --check` 后，在运行中的终端输入 `reload hello`，或重启主程序。

## 常用发送接口

| 接口 | 用途 |
| --- | --- |
| `ctx.reply(message, text)` | 回复文本 |
| `ctx.reply_at(message, text)` | 真正艾特群消息作者并回复 |
| `ctx.send_text(chat_id, text)` | 向明确的会话发送文本 |
| `ctx.reply_image(message, path)` | 回复本地图片 |
| `ctx.send_image(chat_id, path)` | 发送本地图片 |
| `ctx.reply_file(message, path)` | 回复本地文件附件 |
| `ctx.send_file(chat_id, path)` | 发送本地文件附件 |
| `ctx.reply_emoticon(message, path)` | 回复 GIF 表情 |
| `ctx.send_emoticon(chat_id, path)` | 发送 GIF 表情 |
| `ctx.reply_voice(message, path)` | 回复语音条 |
| `ctx.send_voice(chat_id, path)` | 发送语音条 |
| `ctx.export_emoticons(convert=True)` | 显式导出当前账号本地缓存表情 |
| `ctx.group_members(room, expected_session=message.session_id)` | 读取完整群成员快照，缓存不可用时返回 `None` |
| `ctx.group_member_name(room, wxid, expected_session=message.session_id)` | 查询群昵称，未设置时返回微信名 |

每类文件有独立限制，见对应的消息章节。`ctx.send_voice` 发送已有音频；可选的 `voice_reply` 插件通过 Fish Audio 合成艾特对话的语音，见[语音回复](../messages/voice.md#fish-audio-文字转语音)。

## 处理图片

把 `message_types` 改为 `[1, 3]`，并在 `requires` 中加入 `image.receive`。收到类型 `3` 后，查看 `message.image_path` 与 `message.data["image_status"]`。图片正文为空，不会自动作为文字交给模型。

## 处理文件附件

在 `plugin.json` 中声明 `"message_types": [49]` 和 `"requires": ["file.receive"]`。文件接收事件的 `message.text` 为空；仅在 `message.data["file_status"] == "ready"` 时使用 `message.file_path`。失败原因位于 `file_error`，不包含原始 CDN 参数。完整示例和限制见[文件收发](../messages/files.md)。

## 能力声明

群成员变动通过 `message.type == 10002`、`message.data["group_event"]` 传给声明了 `group.events` 的插件，字段为 `kind`（`join` / `leave`）、`room_id`、`member_id`、`nickname`、`source`。`message.text` 为空，不会作为普通聊天交给大模型。只读群成员接口的能力名称为 `group.members.read`。

只声明实际需要的能力。当前 `native_text` 后端提供：

```text
text.receive / text.send
friend.text.receive / friend.text.send
group.text.receive / group.text.send / group.text.mention
image.receive / image.send
file.receive / file.send
friend.file.receive / friend.file.send
group.file.receive / group.file.send
friend.request.receive / friend.request.accept
group.member.invite / group.create
group.members.read / group.events
emoticon.extract / emoticon.send
voice.send / friend.voice.send / group.voice.send
```

旧的 `native_filehelper` 后端只提供基础文本能力。缺少所需能力时，框架会报告该插件无法加载。

好友申请插件订阅 `message_types: [37]`，通过 `message.data["friend_request"]` 取得公开申请 ID、昵称、验证文字及状态，不会取得验证票据或原始 XML。使用 `ctx.accept_friend(request_id, expected_session=message.session_id)` 通过申请。群管理仅提供程序控制台入口。

需要明确启用的插件可在 `plugin.json` 设置 `"default_enabled": false`。当旧配置尚无该插件条目时，框架也会保持关闭；控制台 `enable 插件名` 才会启用并保存设置。

## 群成员与昵称

`ctx.group_members(room, expected_session=message.session_id)` 返回完整成员快照的元组；缓存不可用时返回 `None`，不能把它当成空群。每项含 `wxid`（成员 ID）、`group_nickname`（本群原始昵称）、`wechat_name`（微信名）、`nickname`（群昵称优先、再回退到微信名）。未读取到的名字为空字符串。

`ctx.group_member_name(room, wxid, expected_session=message.session_id)` 返回该群的昵称或微信名，找不到时返回 `None`。这些接口读取当前微信缓存；框架 `/绑定` 设置的称呼可用 `ctx.members.display_name(wxid, room)` 查询，两者分别保留，不相互覆盖。

订阅成员变化时在清单声明 `"message_types": [10002]` 和 `"requires": ["group.events", "text.send"]`，参考 `plugins/group_notice`。首次名单只建立基线，确认变化后才分发；始终用 `ctx.reply(message, text)` 保留连接隔离。

## 成员目录与模型

`ctx.members` 访问框架共用的成员目录和当天记录。查询时始终传当前群的 `chat_id`，不要按昵称猜测真实身份或跨群混用记录。

`ctx.llm_for(message)` 将当前群的人格作为模型表达层；直接使用 `ctx.llm` 则访问基础模型客户端。涉及清空、跨天或切换人格的长任务，参考仓库中 `chat_summary`、`mention_chat` 的记录快照和发送保护实现。

## 生命周期和异常

每个插件有独立队列和工作线程，同一插件按顺序处理消息。单条回调异常会记入日志，框架继续处理后续消息；队列满时会计数并记录丢弃情况。

`on_unload` 中应停止自己创建的线程、定时器并关闭资源。耗时回调执行中重载可能被拒绝，可以等任务完成后重试。

不要在本地调试插件时重复启动多个 `main.py`；使用 `plugins` 查看状态、`reload 名称` 加载改动。
