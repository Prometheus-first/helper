# 微信机器人框架

📚 [在线文档](https://prometheus-first.github.io/helper/) · [安装与启动](https://prometheus-first.github.io/helper/guide/quick-start.html) · [插件开发](https://prometheus-first.github.io/helper/development/plugins.html)

项目已在linux.do公开声明[https://linux.do/](https://linux.do/t/topic/3029497/51)

交流群
<img width="630" height="952" alt="image" src="https://github.com/user-attachments/assets/f6125f82-b369-4106-90c4-052c624fa400" />


# 已完成
- 账号识别与绑定
- 文本收发与消息类型区分
- 群聊真实艾特
- 图片收发
- 文件附件收发
- 本地表情导出、动态表情发送
- 语音条发送
- Fish Audio 语音回复、中文公开音色浏览与按群切换
- 进群欢迎、退群提醒和群成员昵称查询


面向 Windows x64、微信 4.1.15.13 的 Python 插件框架。统一入口为 `main.py`，原生桥接负责文本、图片、文件附件收发和表情、语音发送，功能以插件加载。


# 本项目仅供学习，请勿用于违法行为

## 功能

- 文件传输助手、好友和群聊文本收发，以及真正的群艾特。
- 原生语音条发送：插件和控制台可发送 SILK 语音，安装可选编码依赖后支持 PCM WAV。
- Fish Audio 语音回复：艾特机器人后，将模型回复合成为语音；通过 `/查看音色`、`/音色 名称` 按群选择音色。
- 图片消息自动下载保存，插件可取得本地图片路径；支持发送本地图片。
- 普通文件附件收发：支持控制台和插件发送，接收后按账号隔离保存，单个文件最多 20 MiB。
- 好友申请实时接收与自动通过：默认关闭，可按验证文字关键词匹配，通过控制台启停。
- 控制台邀请好友进群与创建群聊：接口和本地检查已完成，尚未进行真实群管理操作验证。
- 可插拔的新人进群欢迎和成员退群提醒；支持查询完整群成员名单及群昵称。
- 导出当前账号的本地缓存表情，转换 WXGF 动画，并通过原生表情通道发送 GIF。
- 成语接龙：独立玩家、合并抢答和退出模式。
- 看图猜成语：默认从飞飞 API 获取题图和答案，失败时回退 26 道本地题；支持群友抢答、提示、换题和揭晓，无需新增 API 密钥。启用与指令见[插件说明](plugins/picture_idiom/README.md)。
- `/绑定 昵称`：只设置当前群的称呼；不同群各自保存。未绑定时优先使用本群缓存昵称，再使用微信名。
- `/总结 昵称`、`/总结群聊`：通过配置的大模型总结当前群当天的文字记录。
- `/切换人格 嘴臭|卑微|夸夸|取消`：为当前群的对话、总结和接龙点评设置表达风格。
- 真正艾特机器人后对话，携带该成员在当前群当天此前最多十条有效发言。
- 每天北京时间 0 点清理旧日文本；`/清空` 手动清理当前群文本。
- 按文本长度随机等待后发送。
- 插件启停、重载与开发模板。

## 运行环境

Windows 10/11 x64、64 位 Python（已验证 Python 3.13）、`requirements.txt` 中的依赖，以及 Microsoft Visual C++ x64 运行库。

## 首次配置

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python tools/init_config.py
```

1. `bot_config.json`：首次使用将 `wechat_account` 保持为 `null`，不需要提前知道自己的 wxid。运行 `python main.py` 后，程序会等待电脑微信登录；只有一个已登录账号时自动绑定，多个账号时显示昵称和 ID，输入序号选择机器人账号。绑定会保存到本地配置，重新登录后 PID 改变不影响选择；后续多开只会等待绑定的账号。
2. `members_config.json`：在 `groups` 中添加自己要启用的群 ID，成员列表可以为空：

```json
{
  "database_file": ".runtime/member_data.sqlite3",
  "history_enabled": true,
  "daily_clear": true,
  "groups": {
    "12345678@chatroom": []
  }
}
```

3. `llm_config.json`：填写自己的 `api_key`、`base_url` 和 `model`。模板保留 DeepSeek 接口格式，密钥为空。
4. 模板默认关闭需要模型的 `idiom_chain`、`chat_summary`、`mention_chat` 插件。填写模型配置后，把它们的 `enabled` 改为 `true`。
5. 需要艾特后的语音回复时，安装 `requirements-voice.txt`，填写自己的 `tts_config.json`，再启用 `voice_reply` 和 `mention_chat`。语音配置模板的密钥、默认音色均为空，使用 `/查看音色` 和 `/音色 名称` 选择自己的音色，详见[语音回复](docs/messages/voice.md#fish-audio-文字转语音)。

旧版配置里的 `wxid_example_robot` 和空字符串也会进入首次绑定流程；需要换机器人账号时，将本地 `wechat_account` 改回 `null` 并重启。首次选择只读取登录账号的 ID 和微信名，绑定完成后才启动机器人。群 ID 以 `@chatroom` 结尾，收到群文本时可以直接从控制台日志复制。`test` 消息会由 `id_responder` 插件回复当前会话 ID，`chats` 控制台命令可以列出本次连接收到消息的会话。所有示例 ID 和测试用名称均为虚构数据。

## 启动与验证

```powershell
python main.py --check
python main.py
```

`--check` 只检查配置、插件清单与语法，不识别或绑定账号，不注入微信，也不调用模型。直接运行 `python main.py` 时会自动补齐缺失的默认本地配置，已经填写的内容不会被覆盖；之后在微信中完成登录即可继续。

默认 `INFO` 日志级别下，启动后会在控制台实时显示当前连接的微信账号、收到私聊的发送者 ID 和正文，以及收到群消息的群 ID；这些日志也会保存到 `logs/bot.log`：

```text
当前登录微信ID=wxid_example_robot；好友和群聊文本已就绪
私聊消息 | 发送者微信ID=wxid_example_friend | 内容="你好"
群消息 | 群ID=12345678@chatroom
```

消息显示在会话白名单过滤之前，因此新群尚未加入白名单时，也可以让群里的人发一条文本，从控制台复制群 ID。自动消息日志不打印群正文或群成员 ID；总结功能使用的群聊天缓存按原有配置保存。私聊中的换行会显示为 `\n`，一条消息占一行。`logs/` 已被 Git 忽略。

也可以运行 `start_bot.ps1`：脚本优先使用项目的 `.venv`，其次使用 PATH 中的 Python；可通过 `-Python` 或环境变量 `WXBOT_PYTHON` 指定解释器，不依赖某台电脑的安装路径。

控制台命令：

```text
status
chats
friend-requests
accept-friend 申请ID
invite-members 群ID 好友ID [好友ID...]
create-group 好友ID 好友ID [好友ID...]
send-image filehelper "E:\图片\示例.png"
export-emoticons
send-emoticon filehelper "E:\表情\示例.gif"
send-voice filehelper "E:\语音\示例.wav"
send-file filehelper "E:\文件\示例.txt"
members [群ID]
history 称呼 [条数]
plugins
enable 插件名
disable 插件名
reload [插件名]
quit
```

增加群、修改账号或成员配置后重启。停止时使用 `quit`，等待正常卸载原生桥接；微信可以保持打开。

普通文件附件支持通过 `send-file`、`ctx.send_file()`、`ctx.reply_file()` 发送，接收事件类型为 `49`（`appmsg/type=6`），下载成功时 `message.file_path` 为本地路径。单个文件限定非空且不超过 20 MiB，默认按账号保存到 `.runtime/files/`；下载范围遵循 `chat_allowlist`，不会自动打开文件，也不会把附件 XML 交给大模型。详见[文件收发文档](docs/messages/files.md)。

2026-10-19 已验证：文件传输助手收到 TXT 后可在手机打开且内容正确；好友附件下载后与源文件 SHA-256 一致。好友、群聊发送及群附件接收尚未分别实机验证。

`quit`、Ctrl+C、Ctrl+Break 和关闭控制台窗口都会请求清理。退出时先停止接收、卸载桥接 DLL，再等待插件退出；运行中会有一个隐藏的清理守护进程，主程序被强制结束时会尝试卸载本项目残留的桥接，完成后自行退出。下次启动也会检查并恢复本项目的残留；原生任务未结束时保留模块并重试，不强行卸载。

同一微信进程只能由一个项目副本连接，自用版与公开版也共用此限制。其他副本正在运行或加载了其他目录的桥接时，会提示先退出该副本。`.runtime/bot.lock` 是可重复使用的锁文件，存在本身不表示重复运行，不需要手工删除；账号、群配置和聊天记录会保留。守护清理日志位于 `logs/bridge_watchdog.log`。

首次自动绑定完成后才初始化成员目录和群记录，使用保存的机器人账号隔离数据。无交互终端且同时登录多个账号时，程序会提示在交互终端选择账号或只保留机器人账号登录，不会自动挑选其中一个。

## 好友申请与群管理

进群欢迎和退群提醒使用 `group_notice` 插件，在控制台输入 `enable group_notice` 开启。首次读取群名单只建立基线，后续确认成员变化才通知；昵称按群隔离。可用 `group-members 群ID` 查询微信成员名单，`group-nickname 群ID 微信ID` 查询某位群友的昵称。说明与配置见[好友申请与群管理](docs/guide/friends-and-groups.md#进群欢迎退群提醒和群友昵称)。

在程序控制台输入 `enable friend_auto_accept` 开启自动通过，`disable friend_auto_accept` 关闭。默认模板和没有该插件条目的旧配置都保持关闭；验证文字关键词通过 `plugins.friend_auto_accept.verification_keywords` 配置。申请凭据只保存在当前账号、当前连接的内存中，不写日志或配置。

群邀请和创建只接受程序控制台命令，不由微信聊天消息触发。每次邀请 1–8 位好友，创建群聊需指定 2–8 位好友；这是当前框架接口限制。邀请成功不代表成员已经确认入群。详细命令及范围见[好友申请与群管理](docs/guide/friends-and-groups.md)。

2026-10-19 已实测：通过独立好友验证通道接收实时申请，自动通过后对方确认可以正常聊天，测试结束后桥接正常卸载。群邀请与创建只完成本地编译、ABI 和模拟检查，尚未进行真实操作测试。

## 图片收发

使用 `native_text` 后端时，收到允许会话的普通图片会通过当前微信的 CDN 下载任务保存到本地。`bot_config.json` 可设置 `"image_directory": ".runtime/images"`；相对路径以项目目录为起点，目录下再按微信账号隔离。未配置时使用该默认值。`chat_allowlist` 同时控制图片下载和插件分发，空列表允许所有会话。

收到图片后日志显示状态和文件路径。插件中 `message.type == 3` 表示图片，`message.text` 为空；`message.data["image_status"]` 为 `ready` 时，`message.image_path` 指向已保存的图片。下载失败时状态为 `failed`，路径为空，原因在 `message.data["image_error"]` 中。`message.data["image_quality"]` 区分 `standard`（普通尺寸）、`original`（消息仅提供原图引用）和 `thumbnail`（消息仅提供缩略图引用）。普通文本的类型为 `1`。图片不会进入文字聊天记录或大模型的文本上下文。

已有插件默认只接收文本。需要处理图片的插件在 `plugin.json` 中声明消息类型和能力，例如：

```json
{
  "api": "v1",
  "description": "处理图片消息",
  "message_types": [3],
  "requires": ["image.receive", "image.send"]
}
```

```python
class Plugin:
    def on_message(self, message, ctx):
        if message.type == 3 and message.image_path:
            ctx.logger.info("图片已保存：%s", message.image_path)
            # 在自己的业务条件满足时发送或回复图片：
            # ctx.reply_image(message, message.image_path)
            # ctx.send_image("filehelper", message.image_path)
```

同时处理文字和图片时，将 `message_types` 设置为 `[1, 3]`。图片收发支持 JPEG、PNG、GIF、WebP、BMP 文件，单张最多 20 MiB；图片类型按文件内容识别。发送需要本机可读取的文件，路径的 UTF-8 编码最多 1023 字节。在控制台使用 `send-image 会话ID "完整图片路径"`，或者在插件调用 `ctx.send_image(会话ID, 路径)`、`ctx.reply_image(message, 路径)`。

接收仅处理启动后实时到达的普通图片，不补抓旧聊天历史；默认下载微信提供的普通尺寸图片。表情包、视频和文件附件不属于本次图片接口。原生桥接只允许已验证的微信版本与 DLL 指纹，升级微信后需重新适配。

2026-10-16 已实测：向文件传输助手发送本地 PNG，好友图片接收并下载为可解码的 828×1792 JPEG。**手机同账号发给自己的文件传输助手图片走另一条同步路径，当前尚未接入收图**；接收验证请使用另一个微信号发来的普通图片。

## 本地表情与动态发送

`native_text` 后端支持 `emoticon.extract`、`emoticon.send`。发送 GIF 走微信原生表情消息（类型 47）；在文件传输助手上已验证收到后能够播放动画。`send-image` 仍是普通图片接口。

发送已有 GIF 不需要配置数据目录：

```text
send-emoticon filehelper "E:\表情\示例.gif"
```

GIF 按实际内容完整校验，最多 1 MiB，宽高各不超过 1024，路径 UTF-8 编码最多 1023 字节。本地处理还限制最多 300 帧、累计 6400 万像素。原生端会再次核对文件及当前登录账号，并要求微信将文件识别为可发送的类型 47 表情；超限或不支持的文件会报错。

要提取微信已缓存在本机的表情，在本地 `bot_config.json` 中设置 `wechat_data_directory`，指向**当前绑定账号**的数据目录，例如 `E:/WeChat/xwechat_files/wxid_example_robot_abcd`；不要填到其他账号或上层 `xwechat_files` 目录。默认 `null` 时不会读取缓存。设置后重启，在机器人控制台运行：

```text
export-emoticons
export-emoticons raw
```

第一条导出原件、已有缩略图，并尝试生成可发送的 GIF；`raw` 仅导出原件和缩略图。结果中 `gif_path` 是发送路径，`sendable` 表示该 GIF 是否通过本地发送条件校验，`error` 为单项失败原因。导出默认保存在 `.runtime/emoticons/`，按账号哈希隔离；可用 `emoticon_directory` 更改输出目录。

提取只读取所配置账号的 `business/emoticon`，支持根目录文件以及 `Persist`、`Thumb` 的分片布局，不修改微信原文件。缓存密钥只在导出期间保留于内存，不写入结果、日志或配置。仅在执行导出命令或调用导出 API 时读取，不会在启动时自动导出。本地缓存可能不全，也不能证明每个文件仍属于爱心收藏列表。

WXGF 转换通过 FFmpeg 解码，按 30 fps 生成 GIF，并在必要时减少颜色或尺寸以满足发送限制；原件会保留。转换后的播放时长不保证与微信原件一致。解码失败或无法满足限制时，结果保留原件路径并报告失败，不会把不可发送的文件标为可发送。尚未接入实时接收类型 47 消息和完整收藏排序。

插件可声明 `"requires": ["emoticon.send"]` 或 `"requires": ["emoticon.extract"]`，并调用：

```python
ctx.send_emoticon("filehelper", gif_path)
ctx.reply_emoticon(message, gif_path)
items = ctx.export_emoticons(convert=True)
for item in items:
    if item.sendable:
        ctx.logger.info("可发送表情：%s", item.gif_path)
```

运行产生的表情、聊天记录、日志和本地配置均是个人数据，请勿提交。发布检查会拒绝图片、GIF、WXGF、数据库及非空账号数据目录配置；测试只使用程序生成的样本。

## 语音发送

已在微信 4.1.15.13 的文件传输助手完成 2 秒合成测试音发送，手机端确认收到并可正常播放。好友、群聊共用目标 ID 接口，尚未分别实机验证。

控制台输入 `send-voice 会话ID "完整语音路径"`；会话 ID 可为 `filehelper`、好友 ID 或群 ID。

```powershell
python -m pip install -r requirements-voice.txt
python main.py
```

安装可选依赖后，可以发送单声道、16-bit PCM WAV，采样率支持 8/12/16/24 kHz。WAV 会转成 Tencent SILK，并补齐最后不足 20 毫秒的一帧；源文件保留。直接发送已经编码好的微信 SILK 不需要编码依赖；标准 SILK 的完整头尾会在发送快照中规范化。只支持 20 ms/包的 SILK v3 封装，时长按包计数，不保证任意第三方 SILK 载荷都可解码。暂不支持 MP3、AAC、立体声或其他 WAV 编码；单条最多 60 秒，输入文件最多 20 MiB。

插件声明 `"requires": ["voice.send"]` 后调用：

```python
ctx.send_voice("filehelper", voice_path)
ctx.reply_voice(message, voice_path)
```

原生发送按连接和账号核对，不使用文本随机等待。文件快照放在本项目 `.runtime/voices/` 的当前账号子目录，调用结束后清理；原生入口在排队前把音频字节复制到任务持有的内存，避免超时或退出时删除快照影响已排队任务。底层仍锁定微信 4.1.15.13，发送结果为微信原生接口返回的接受状态，不能单独证明对方已经收到或成功播放。暂未接入语音接收或语音识别。

### Fish Audio 语音回复

填写本地 `tts_config.json` 的 `api_key`，然后启用 `mention_chat` 与 `voice_reply` 插件，并安装 `requirements-voice.txt` 中的依赖。群内发送 `/查看音色` 查询个人音色和中文公开音色第一页，`/查看音色 2` 翻页，`/查看音色 温柔` 搜索，`/查看音色 温柔 2` 查看搜索结果下一页。`/查看音色 全部` 可浏览各国语言的公开音色，后面也可加关键词和页码。发送 `/音色 完整名称` 或 `/音色 ID` 选择当前群音色；个人音色同名时优先使用，公开重名音色使用菜单中的 ID 区分。普通条目只显示名称，重名或名称被缩略时才附 ID。此后真正艾特机器人提问，会按当前人格生成文字、合成语音，再只发送微信语音条。`/音色 关闭` 恢复当前群的文字回复。

音色选择按账号和群保存，重启后保留；`default_voice` 可设置默认名称或 ID，留空时先使用群指令选择。菜单、总结、成语接龙仍用文字。合成失败会回退文字；临时 WAV、SILK 在发送结束后清理。模型生成的回复正文会提交给 Fish Audio，密钥配置和音频缓存已被 Git 忽略。详细配置见[语音文档](docs/messages/voice.md#fish-audio-文字转语音)。

## 目录

```text
main.py                  统一入口
wxbot_framework/         框架、成员目录、消息缓存、大模型客户端
plugins/                 各功能插件和开发模板
native/                  原生桥接源码、两个必要 DLL、版本指纹和 MinHook
tests/                  本地测试
tools/                   本地配置初始化和发布前检查
```

