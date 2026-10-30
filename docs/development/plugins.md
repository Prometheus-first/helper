# 插件开发

插件通过 Python 接口处理消息，不需要接触 DLL 或原生地址。

## 新建插件

网页“插件管理 → 配置”只展示可修改的业务函数和 JSON 选项，保存后自动重载；支持按账号隔离与恢复默认，见[网页修改与重置插件](../guide/web-panel.md#网页修改与重置插件)。项目的 `plugins/` 目录作为默认代码，网页修改保存在当前机器人的 `.runtime/plugin_code/` 副本中。插件资源相对 `__file__` 所在目录读取；需要项目根目录时可使用 `ctx.project_root`。

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
  "requires": ["text.receive", "text.send"],
  "business": [{"function": "Plugin.on_message", "label": "回复消息"}]
}
```

在 `__init__.py` 实现 `Plugin`：

`business` 声明网页可编辑的函数（最多 8 个）。默认文件是 `__init__.py`；函数位于别的模块时，增加 `"file": "business.py"`，并填写模块内的函数名。未声明的旧插件默认展示 `Plugin.on_message`。推荐将初始化、消息过滤、缓存和连接检查留在固定代码里，把回复文案、业务判断或处理结果拆成一个清晰的函数并声明在这里。网页只读取这些函数，保存时校验签名并局部替换，不让用户逐个浏览源文件。

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

## 注册定时动作

插件可以在 `plugin.json` 中声明 `scheduled_actions`，管理台会自动列出动作并按字段生成表单。例如：

```json
{
  "scheduled_actions": [{
    "id": "greeting",
    "label": "发送问候",
    "method": "scheduled_greeting",
    "fields": [
      {"name": "chat_id", "label": "会话 ID", "type": "chat", "required": true},
      {"name": "text", "label": "内容", "type": "textarea", "required": true}
    ]
  }]
}
```

将该字段加到原有清单中，并在 `Plugin` 中实现方法：

```python
def scheduled_greeting(self, params, ctx):
    # params 按声明校验；ctx 使用触发时的微信连接。
    if ctx.send_text(params['chat_id'], params['text']) is False:
        raise RuntimeError('发送失败')
```

字段类型支持 `text`、`textarea`、`chat`（微信或群 ID）、`integer`、`boolean`；`required` 可省略，默认为 `false`。每个插件最多声明 16 个动作，每个动作最多 16 个字段。动作 ID 和字段名使用小写字母、数字及下划线，方法名由清单指定。网页不能指定任意 Python 函数。模板 `plugins/_template` 包含可复制示例。

对应插件必须已加载，触发时使用插件当前实例和当前配置；`ctx` 的微信操作绑定触发时的连接，发送还受会话白名单限制。回调与该插件的消息回调串行，异常计入任务失败状态，不自动重试；不要在回调里创建无法停止的后台工作。插件代码按信任代码运行，定时接口不是安全沙箱；插件若自行创建后台工作或操作外部服务，必须自行处理生命周期、失败与不确定结果。

管理台可在插件停止时配置已声明动作，但必须启动机器人并启用该插件才能执行；插件已删除或移除动作时，旧任务仍可停用或删除。长任务不阻塞调度器，其重叠周期会跳过。已有定时文字任务无需迁移。默认 `chat_summary` 插件提供“总结群聊”动作；自行定制过插件源码的账号，需要在自己使用的版本中声明并实现该动作。

## 常用发送接口

| 接口 | 用途 |
| --- | --- |
| `ctx.reply(message, text)` | 回复文本 |
| `ctx.reply_at(message, text)` | 真正艾特群消息作者并回复 |
| `ctx.send_text(chat_id, text)` | 向明确的会话发送文本 |
| `ctx.send_at_text(room, text, members, expected_session=message.session_id, display_names=names)` | 向指定群发送真实艾特，支持 1–8 位成员 |
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
| `ctx.contacts(expected_session=message.session_id)` | 扫描当前账号的好友及资料，需要 `contact.read` 能力 |
| `ctx.group_member_name(room, wxid, expected_session=message.session_id)` | 查询群昵称，未设置时返回微信名 |
| `ctx.group_member_role(room, wxid, expected_session=message.session_id)` | 查询群主、群管理员或普通群友身份，未确认时返回 `unknown` |

每类文件有独立限制，见对应的消息章节。`ctx.send_voice` 发送已有音频；可选的 `voice_reply` 插件通过 Fish Audio 合成艾特对话的语音，见[语音回复](../messages/voice.md#fish-audio-文字转语音)。

## 处理图片

合并转发需要单独声明 `forwarded.receive` 并接收类型 `49`，通过 `message.data["forwarded_records"]` 访问文字和图片结果，见[合并转发解析](../messages/forwarded.md)。普通图片消息仍使用下述类型 `3`。

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
contact.read
message.recall
emoticon.extract / emoticon.send
voice.send / friend.voice.send / group.voice.send
```

旧的 `native_filehelper` 后端只提供基础文本能力。缺少所需能力时，框架会报告该插件无法加载。

好友申请插件订阅 `message_types: [37]`，通过 `message.data["friend_request"]` 取得公开申请 ID、昵称、验证文字及状态，不会取得验证票据或原始 XML。使用 `ctx.accept_friend(request_id, expected_session=message.session_id)` 通过申请。群管理仅提供程序控制台入口。

需要明确启用的插件可在 `plugin.json` 设置 `"default_enabled": false`。当旧配置尚无该插件条目时，框架也会保持关闭；控制台 `enable 插件名` 才会启用并保存设置。

`message.recall` 表示后端可提供微信原始消息编号和撤回通知。框架把撤回事件及媒体监控元数据定向交给内置防撤回插件，不广播为普通文本；参考 `anti_recall` 的精确编号匹配、会话缓存和发送连接检查。它同时补发文字、内置表情和 emoji；后端提供 `emoticon.send` 时，还会异步缓存类型 47 的原件并用 `ctx.reply_emoticon` 补发。原始 XML、CDN 地址及凭据不应进入日志或大模型。补发插件默认开启，网页监控标记独立保留。

## 联系人

`ctx.contacts(expected_session=message.session_id)` 返回含 `account`、`session_id`、`scanned_at`、`contacts` 和标签目录 `tags` 的快照。每位好友含 `wxid`、`nickname`、`remark`、`alias`、`description`、`sex`、`country`、`province`、`city`、`signature`、`tag_ids`、`tags` 和 `tags_status`。`tags_status == "ready"` 且 `tags` 为空才代表已确认没有标签；`unavailable` 表示字段或标签名称尚未就绪。未读取到的个人资料为空，性别为 `unknown`。`complete` 当前为 `false`，仅表示已加载缓存，不保证完整好友名单。连接不可用或读期间切换账号会失败，不返回旧账号结果。当前仅验证微信 4.1.15.56，使用说明见[扫描联系人](../guide/web-panel.md#扫描联系人)。

## 群成员与昵称

`ctx.group_members(room, expected_session=message.session_id)` 返回完整成员快照的元组；缓存不可用时返回 `None`，不能把它当成空群。每项含 `wxid`（成员 ID）、`group_nickname`（本群原始昵称）、`wechat_name`（微信名）、`nickname`（群昵称优先、再回退到微信名）。微信 4.2.0.11 的微信名也会查询已加载的非好友成员缓存；未读取到的名字为空字符串。

`ctx.group_member_name(room, wxid, expected_session=message.session_id)` 返回该群的昵称或微信名，找不到时返回 `None`。这些接口读取当前微信缓存；框架 `/绑定` 设置的称呼可用 `ctx.members.display_name(wxid, room)` 查询，两者分别保留，不相互覆盖。

成员项还提供 `role`（`owner` / `admin` / `member` / `unknown`）、`is_owner`、`is_admin` 和 `role_source`。群主与管理员分别标记；未知时两个布尔字段为 `None`。`ctx.group_member_role()` 返回同样的角色字符串，找不到成员时返回 `unknown`。实时群消息的 `message.data["sender_role"]` 只使用本连接最近 30 秒内的缓存；合并转发的原作者不继承转发者身份。身份缺失不代表普通群友，也不应据此授予权限。

需要群成员查询时声明 `group.members.read`，主动艾特还需声明 `text.send`、`group.text.mention`；显示昵称不会改变真实通知目标。可见标签、数量和字节限制见[文本与真实艾特](../messages/text.md#群成员艾特)。

订阅成员变化时在清单声明 `"message_types": [10002]` 和 `"requires": ["group.events", "text.send"]`，参考 `plugins/group_notice`。首次名单只建立基线，确认变化后才分发；始终用 `ctx.reply(message, text)` 保留连接隔离。

## 成员目录与模型

`ctx.members` 访问框架共用的成员目录和当天记录。查询时始终传当前群的 `chat_id`，不要按昵称猜测真实身份或跨群混用记录。

`ctx.llm_for(message)` 将当前群的人格作为模型表达层；直接使用 `ctx.llm` 则访问基础模型客户端。涉及清空、跨天或切换人格的长任务，参考仓库中 `chat_summary`、`mention_chat` 的记录快照和发送保护实现。

## 生命周期和异常

每个插件有独立队列和工作线程，同一插件按顺序处理消息。单条回调异常会记入日志，框架继续处理后续消息；队列满时会计数并记录丢弃情况。

`on_unload` 中应停止自己创建的线程、定时器并关闭资源。耗时回调执行中重载可能被拒绝，可以等任务完成后重试。

不要在本地调试插件时重复启动多个 `main.py`；使用 `plugins` 查看状态、`reload 名称` 加载改动。
