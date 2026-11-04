# Linux 实验版

Linux 版正在适配中，尚不能替代 Windows 版。已接入本地网页管理台，可创建多个 Linux 微信会话并分别管理机器人。也保留受控测试命令；`main.py` 仍是 Windows 入口。

## 支持范围

已验证环境：Ubuntu 24.04 x86_64、Python 3.12、官方 Linux 微信 **4.1.13.26**。桥接检查可执行文件 SHA-256、Build ID 和入口字节，其他版本会拒绝加载。

| 能力 | 当前状态 |
| --- | --- |
| 私聊、群聊文本接收 | 实机通过，包含发送者和会话字段 |
| 私聊、群聊文本回复 | 实机通过，经过框架插件 `ctx.reply()` |
| 真实群艾特接收 | 实机识别微信提供的成员名单和当前账号，插件可读取 `message.data["at_members"]` 与 `is_at_self` |
| 真实群艾特发送与群成员昵称扫描 | 尚未完成验证；相关插件不会因仅有接收能力而被标记为可用 |
| 本地图片、文件发送 | 实机通过，手机确认内容正确 |
| 好友私聊图片自动下载 | 实机通过；无需进入私聊或点开图片，完整 MD5 校验后可回发 |
| 已落盘文件的校验与回发 | 实机通过，校验文件名、大小和 MD5；已验证类型 74 附件在后台自动落盘后回发，手机确认一致 |
| 私聊语音消息识别 | 元数据及原生消息上下文实机通过；音频提取和语音条发送尚未通过 |
| 群图片自动下载、未落盘文件主动下载 | 尚未验证或适配 |
| 网页管理台 | 已接入启停、消息监控、插件配置和业务函数编辑、群备注、模型设置、文字及本地图片/文件发送；已完成离线与模拟浏览器验证 |
| 登录账号确认 | 微信登录后读取原生账号及昵称，无需发送初始化消息；单账号识别和机器人启停已实机通过 |
| 多账号 | 独立微信进程与账号配置已接入，模拟隔离测试通过；两个真实微信同时登录仍待验证 |
| 网页显示登录二维码 | 无桌面模式使用独立虚拟显示，提取二维码后以 PNG Base64 交给网页；真实微信二维码已在网页显示，并完成单账号扫码登录 |
| 语音条发送、撤回、联系人、群事件、朋友圈、公众号 | 尚未适配 |

Windows 版的功能列表不代表 Linux 版具备相同能力。Linux 使用独立 ELF 桥接，不能加载 Windows DLL，也不能复用 Windows 偏移。

## 安装与编译

请先安装对应版本的官方 Linux 微信。仓库不包含微信安装包、预编译 Linux 桥接、账号配置或测试媒体。

在项目根目录执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
mkdir -p native/linux/build
gcc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared -pthread native/linux/text_bridge.c -ldl -o native/linux/build/text_bridge.so
gcc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared -pthread native/linux/send_bridge.c -ldl -o native/linux/build/send_bridge.so
```

需要 GCC 和 Python venv 支持。生成的 `.so` 只用于本机运行，已被 Git 忽略。修改桥接后，在下次启动测试微信时加载新文件。

以下命令默认微信安装在 `/opt/wechat/wechat`。其他位置用 `--wechat /path/to/wechat` 指定；桥接也可以通过各命令的参数指定。请在图形桌面或支持图形界面的 WSL 中运行。

## 网页管理台

首次安装 Python 依赖后，在 Linux 图形桌面或 WSLg 中执行：

```bash
./start_web.sh --wechat /opt/wechat/wechat
```

脚本优先使用项目的 `.venv`，自动编译本机桥接，再启动管理服务。也可以先执行 `python -B -m tools.build_linux_bridges`，然后运行 `python -B web.py --wechat /path/to/wechat --no-browser`。端口默认 8765，可用 `--port` 修改。

1. 打开 `http://127.0.0.1:8765`，点击“登录微信”，在 Linux 微信窗口扫码并用手机确认。
2. 页面自动显示已识别的昵称和微信 ID。再次点击“登录微信”可创建另一个独立会话。
3. 勾选需要运行的账号并启动，也可以在账号行单独启动或停止。切换顶部账号选择器可查看各自配置和消息。
4. 发送图片或文件时，填写 Linux 本机的绝对路径。当前真实媒体送达验证覆盖文件传输助手，群媒体仍需单独实测。

停止机器人后，微信登录窗口保持运行。使用账号行的“退出微信”关闭该会话。每个微信进程使用独立 HOME 和缓存，机器人配置、群备注、插件覆盖及聊天数据按账号保存在 `.runtime/linux/accounts/`。之前单账号配置仍位于 `.runtime/linux/`，不会自动复制到其他账号。

单账号自动读取、机器人启停和文本收发已实机验证；跨账号隔离已通过模拟测试，真实的双账号同时登录和收发还需要实机验证。尚未适配的功能在网页中隐藏或标记为不可用。


### 无桌面服务器

安装 `xvfb`、`xauth` 和 `ffmpeg`，并安装本项目 Python 依赖后，启动管理台。若需自动从已识别的快捷登录页面切换到扫码页面，还需 `xwininfo` 和 XTest 运行库（通常来自 `x11-utils`、`libxtst6`）：

```bash
./start_web.sh --wechat /opt/wechat/wechat --headless --no-browser
```

通过 SSH 转发本机端口后，在浏览器打开 `http://127.0.0.1:8765`：

```bash
ssh -L 8765:127.0.0.1:8765 user@server
```

点击“登录微信”，等待页面显示二维码并扫码。每个微信会话使用独立的虚拟显示和认证文件，登录成功后页面清除二维码。`GET /api/linux-login-qr?robot=会话ID` 返回该会话的状态和 PNG Base64，需带页面会话令牌；它只用于已由管理台启动的微信。虚拟显示属于管理台进程，停止会话时一并关闭。自动切换只针对已识别的快捷登录页面；其他页面不会被自动点击。二维码长时间加载时，请检查 Linux 系统能否访问微信服务。

## 受控验证入口

每个命令会启动一个加载桥接的 Linux 微信。先退出已运行的同一 Linux 微信，运行命令后扫码登录。停止测试会停用桥接采集和发送接口，微信保持打开；后续重新加载桥接时才需要退出并重新启动。

### 文件传输助手发文字

```bash
python -B -m tools.linux_text_send_test
```

登录后在终端输入 `send` 发送 `WX_LINUX_NATIVE_SEND_01`；输入 `quit` 停用接口。启动命令本身不会自动发消息。

### 私聊和群聊文本回复

```bash
python -B -m tools.linux_text_roundtrip_test --timeout 1800
```

桥接就绪后，用另一个测试账号：

1. 私聊发送 `WX_LINUX_BRIDGE_07_SEND`，预期回复 `WX_LINUX_FRIEND_REPLY_01`。
2. 在测试群发送 `WX_LINUX_BRIDGE_08_SEND`，预期在同一群回复 `WX_LINUX_GROUP_REPLY_01`。

每类只回复一次，其他文字不会触发回复。会话范围来自本轮收到的测试消息，不需要填写个人 wxid 或群 ID。

### 后台收到图片后自动下载

```bash
python -B -m tools.linux_media_test --auto-download-images --timeout 1800
```

登录后让电脑微信停留在文件传输助手。用另一个测试账号先私聊发送 `WX_LINUX_MEDIA_ARM_11`，再发一张全新图片；电脑端不要进入私聊或点开图片。

工具只处理该测试账号的一张图片：保留原生消息上下文，在微信协程中下载原图，读取当前账号缓存并解密 V2 格式，完整 MD5 校验通过后再回发到文件传输助手。已通过后台实机验证，手机确认回发图片内容一致。

此入口仍是验证工具，不会接管所有会话的日常媒体收发。临时图片写入当前用户的私有临时目录；该验证工具不将原始 XML、CDN 凭据或缓存密钥写入测试输出。管理台的 `.runtime/` 可能包含账号信息、配置和聊天数据，属于本地私有数据，不应提交到仓库。

### 附件后台落盘与语音识别

```bash
python -B -m tools.linux_media_test --receive-voice --watch-files --file-name WX_LINUX_FILE_RECV_02.txt --timeout 1800
```

附件由微信后台落盘后可校验并回发。语音元数据已识别，自动提取音频尚未通过实机验证。

## 开发接口

- `LinuxNativeTextBackend` 是实验性文本后端，复用框架消息转换、插件分发和回复链路。发送需要显式提供 `send_bridge_path`，通过 `send_targets` 允许指定会话；默认仅文件传输助手。
- `LinuxNativeSender` 提供 `send_text()`、`send_image()`、`send_file()`，以及受控的 `bind_image_scope()`、`image_context_available()`、`download_message_image()`。
- `LinuxImageDownloader` 符合现有 `ImageReceiver` 下载器接口。图片自动下载尚未接入网页管理台，网页监控仅显示媒体类型。
- 原生图片上下文最多缓存 16 条，仅绑定当前测试双方账号。未知版本、账号不匹配、未完成下载或校验失败都会拒绝返回图片。
- 下载或发送等待超时后，迟到回调结束会允许后续请求继续执行；原请求仍保持结果未知，不会自动重发。
- 本地通道核对 PID 和 UID。原生调用在微信自己的协程中执行；超时或连接中断时报告结果未知，不自动重试，避免重复发送。
- Linux 进程锁使用 `flock`，Windows 仍使用 `msvcrt`。共用运行时新增的能力列表参数可选，Windows 默认行为保留。

## 离线检查

```bash
python -B -m unittest discover -s tests -p 'test_linux_*.py'
```

包含可重复编译的合成 fixture，覆盖文本与媒体协议、断线去重、进程身份、原生调度、图片上下文范围、缓存解密、MD5 和路径检查。测试不会连接真实微信、调用模型或读取个人聊天记录。

发布前还应运行 `python -B -m unittest tests.test_publication_checks`，并对实际待发布目录执行 `tools.check_publication.inspect()`。检查目标是源码和示例配置；本地 `.runtime/`、日志、`.so`、崩溃转储和测试媒体不得进入提交。
