# helper

Windows 微信机器人 Python 插件框架，提供本地网页管理台和控制台入口。

## 交流
<summary>交流群二维码</summary>
<img width="630" height="952" alt="image" src="https://github.com/user-attachments/assets/b149253c-9684-447d-af17-f83030bdf1ba" />

[在线文档](https://prometheus-first.github.io/helper/) · [快速开始](docs/guide/quick-start.md) · [插件开发](docs/development/plugins.md)

**使用许可：允许个人自用，禁止商业使用。** 允许个人学习研究及非商业修改、免费分享，分享时须保留协议和版权声明。完整条款见 [LICENSE](LICENSE)，第三方组件继续适用各自的许可证。

## 功能

- 多账号管理：发现已登录微信，按账号启停，独立保存插件配置与数据。
- 微信版本保护：可选的更新保护工具，针对已验证的 4.2.0.12 构建保留可恢复的更新程序备份。
- 联系人：扫描当前账号的好友，搜索及按标签筛选，查看昵称、备注、微信号、地区和签名。
- 朋友圈：发布文字和图片，读取已加载记录、点赞人和评论，手动点赞或发送评论。
- 公众号文章：自动获取当前账号的关注公众号，点击公众号自动分页同步它的文章，点击文章自动加载正文和图片，按账号保存并导出 Markdown；目前支持已核对的微信 4.2.0.12
- 消息收发：文本自动分段，图片、文件、表情包、语音条和视频，支持真实群艾特与群成员身份查询；接收图片时优先下载可用原图，微信 4.2.0.12 支持视频 CDN 下载，普通文件按块处理。
- 消息监控：显示时间、会话 ID、发送者及正文，展开合并转发记录，保留撤回标记。
- 定时任务：按间隔或每天指定时间执行文字发送和插件提供的动作，支持多账号独立管理。
- 群聊管理：群聊备注、进退群通知、成员昵称绑定、聊天总结、人格切换、成语游戏和图库取图。
- 模型与语音：支持 OpenAI 兼容的 Chat Completions API、可选的图片生成插件、Fish Audio 合成和按群切换音色。
- 插件二开：在网页修改业务函数与配置，保存生效，支持恢复默认代码和配置。

自用版另有原生红包自动领取功能，不包含在此公开仓库；需要可联系交流群群主。

防撤回分为文本（含表情包）、图片、文件、语音四个独立插件，均默认开启。好友自动通过默认关闭。完整指令见[群聊功能](docs/guide/features.md)和[控制台命令](docs/guide/commands.md)。

## 快速开始

运行条件：Windows 10/11 x64、64 位 Python（已验证 3.13）、Microsoft Visual C++ x64 运行库。

当前适配微信 4.2.0.12 的指定构建。

4.2.0.12 已实测账号与二维码识别、文本及群艾特、图片、文件和语音收发、撤回补发、好友申请接收、通过及从共同群主动发起、进退群事件、朋友圈读取与仅自己可见的发布，以及公众号目录、分页和正文读取。MP4 视频发送已在文件传输助手和群聊验证，好友私聊尚未单独实测；公众号图片显示尚未单独实测。

各版本的详细范围见 [native/README.md](native/README.md)。

```powershell
python -m pip install -r requirements.txt
```

登录电脑微信后，双击 `start_web.bat`，打开 `http://127.0.0.1:8765`，点击账号行的“启动”；也可以勾选多个账号批量启动。首次使用无需手填 wxid。顶部账号选择用于切换正在编辑的配置。

需要控制台操作时运行：

```powershell
python main.py --check
python main.py
```

缺失的本地配置会自动补齐，已有配置保留。`--check` 检查配置与插件语法，不连接微信或调用模型。发送 WAV 语音和使用语音合成时，还需安装 `requirements-voice.txt`。

正常退出：网页先停止机器人，再在管理台窗口按 Ctrl+C；控制台入口输入 `quit`。关闭浏览器不会停止机器人。同一微信进程只能由一个项目副本连接。

## 配置与数据

| 文件 | 用途 |
| --- | --- |
| `bot_config.json` | 后端、会话范围、插件开关与选项 |
| `members_config.json` | 各群成员称呼与功能范围 |
| `llm_config.json` | 模型地址、密钥及模型名称 |
| `tts_config.json` | Fish Audio 密钥及语音选项 |
| `.runtime/image_generation.json` | 可选图片生成插件的渠道、密钥和模型 |

网页可编辑主要配置。图片生成插件使用本地私有配置，设置方法见[AI 生图](docs/guide/features.md#ai-生图)。多账号工作区位于 `.runtime/robots/`；聊天记录、媒体缓存和日志均为本地数据，已被 Git 忽略，请勿提交。具体字段见[配置参考](docs/guide/configuration.md)。

## 文档

| 需要做什么 | 文档 |
| --- | --- |
| 安装、选择账号、添加群 | [快速开始](docs/guide/quick-start.md) · [账号与群聊](docs/guide/account-and-groups.md) |
| 启停账号、监控消息、修改插件 | [网页管理台](docs/guide/web-panel.md) |
| 扫描好友、查看资料和标签 | [扫描联系人](docs/guide/web-panel.md#扫描联系人) |
| 关注公众号、自动同步文章和站内阅读 | [公众号文章](docs/guide/web-panel.md#公众号文章) |
| 图片、文件、视频、表情和语音 | [图片](docs/messages/images.md) · [文件](docs/messages/files.md) · [视频](docs/messages/videos.md) · [表情](docs/messages/emoticons.md) · [语音](docs/messages/voice.md) |
| 合并转发与防撤回 | [合并转发](docs/messages/forwarded.md) · [防撤回](docs/messages/recall.md) |
| 好友申请、群成员与群管理 | [好友申请与群管理](docs/guide/friends-and-groups.md) |
| 开发和维护 | [插件开发](docs/development/plugins.md) · [代码结构与验证](docs/development/structure.md) |

## 目录

```text
main.py / web.py      控制台 / 网页入口
wxbot_framework/      连接生命周期、消息、插件、模型和管理台
plugins/              独立功能与业务函数
native/               原生桥接、版本指纹与必要 DLL
webui/                管理台静态页面
docs/                 使用与开发文档
tests/                离线回归测试
tools/                配置初始化、测试与发布检查
```






</details>

[linux.do 项目讨论](https://linux.do/t/topic/3029497/51)
