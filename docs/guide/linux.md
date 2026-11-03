# Linux 实验版

Linux 版正在适配中，尚不能替代 Windows 版。当前通过受控命令验证收发，**还没有接入日常 `main.py` 或网页管理台**。

## 支持范围

已验证环境：Ubuntu 24.04 x86_64、Python 3.12、官方 Linux 微信 **4.1.13.26**。桥接检查可执行文件 SHA-256、Build ID 和入口字节，其他版本会拒绝加载。

| 能力 | 当前状态 |
| --- | --- |
| 私聊、群聊文本接收 | 实机通过，包含发送者和会话字段 |
| 私聊、群聊文本回复 | 实机通过，经过框架插件 `ctx.reply()` |
| 本地图片、文件发送 | 实机通过，手机确认内容正确 |
| 好友私聊图片自动下载 | 实机通过；无需进入私聊或点开图片，完整 MD5 校验后可回发 |
| 已下载文件的缓存读取 | 实机通过，校验文件名、大小和 MD5；兼容附件类型 6、74 |
| 群图片自动下载、未落盘文件主动下载 | 尚未验证或适配 |
| 登录账号自动识别、多账号管理台 | 尚未接入；进程发现不代表账号已登录 |
| 语音、撤回、联系人、群事件、朋友圈、公众号 | 尚未适配 |

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

此入口仍是验证工具，不会接管所有会话的日常媒体收发。临时图片写入当前用户的私有临时目录；`.runtime/` 仅保存测试状态，不保存微信 ID、群 ID、聊天正文、原始 XML、CDN 凭据或缓存密钥。使用结束后可清理这些测试输出，不要提交到仓库。

## 开发接口

- `LinuxNativeTextBackend` 是实验性文本后端，复用框架消息转换、插件分发和回复链路。发送需要显式提供 `send_bridge_path`，通过 `send_targets` 允许指定会话；默认仅文件传输助手。
- `LinuxNativeSender` 提供 `send_text()`、`send_image()`、`send_file()`，以及受控的 `bind_image_scope()`、`image_context_available()`、`download_message_image()`。
- `LinuxImageDownloader` 符合现有 `ImageReceiver` 下载器接口。图片下载与正式收发后端、网页管理台的集成仍需后续完成。
- 原生图片上下文最多缓存 16 条，仅绑定当前测试双方账号。未知版本、账号不匹配、未完成下载或校验失败都会拒绝返回图片。
- 本地通道核对 PID 和 UID。原生调用在微信自己的协程中执行；超时或连接中断时报告结果未知，不自动重试，避免重复发送。
- Linux 进程锁使用 `flock`，Windows 仍使用 `msvcrt`。共用运行时新增的能力列表参数可选，Windows 默认行为保留。

## 离线检查

```bash
python -B -m unittest discover -s tests -p 'test_linux_*.py'
```

包含可重复编译的合成 fixture，覆盖文本与媒体协议、断线去重、进程身份、原生调度、图片上下文范围、缓存解密、MD5 和路径检查。测试不会连接真实微信、调用模型或读取个人聊天记录。

发布前还应运行 `python -B -m unittest tests.test_publication_checks`，并对实际待发布目录执行 `tools.check_publication.inspect()`。检查目标是源码和示例配置；本地 `.runtime/`、日志、`.so`、崩溃转储和测试媒体不得进入提交。
