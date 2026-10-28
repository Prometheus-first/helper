# 文件收发

`native_text` 后端将普通文件附件单独处理，类型为 `49`、`appmsg/type` 为 `6`。分享链接、转发合集和小程序等类型 `49` 消息不作为文件下载。

合并转发的文字与图片由独立的[合并转发解析](./forwarded.md)处理。

## 发送文件

在 `python main.py` 运行后的控制台输入：

```text
send-file filehelper "C:\测试文件\hello.txt"
```

将 `filehelper` 换成好友微信 ID 或以 `@chatroom` 结尾的群 ID，可以使用同一发送接口。文件以附件形式发送；不会按图片、表情或语音消息发送。

插件中调用：

```python
ctx.send_file(chat_id, r"C:\测试文件\hello.txt")
ctx.reply_file(message, r"C:\测试文件\hello.txt")
```

当前版本要求非空本地普通文件，取消框架原有的 20 MiB 文件大小上限；路径仍最多 1023 个 UTF-8 字节。发送按块创建受控快照，源文件不修改；实际大小受系统、磁盘空间、网络、微信和超时影响。原生接口接受请求不等于收件方已经下载完成。

## 接收文件

接收启动后实时到达的普通文件附件，下载范围沿用 `chat_allowlist`。默认按账号隔离保存到 `.runtime/files/` 下；可以通过 `file_directory` 修改保存根目录。下载完成后，控制台会显示私聊文件的名称、大小、状态和路径；群消息日志只显示群 ID。

```json
{
  "api": "v1",
  "entry": "plugin.py",
  "message_types": [49],
  "requires": ["file.receive"]
}
```

```python
class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx

    def on_message(self, message):
        if message.data.get("file_status") == "ready":
            # 路径指向已下载的本地附件；由插件决定如何使用。
            self.ctx.logger.info("文件已保存：%s", message.file_path)
```

事件中 `message.text` 为空，附件 XML 不进入文本聊天记录或模型上下文。文件状态为 `ready` 或 `failed`，对应字段为 `file_name`、`file_size`、`file_path`、`file_error`。文件名仅用于展示和安全保存，消息不能指定目录；相同名称的文件不会覆盖已有文件。

接收器要求消息声明非零且在系统范围内的大小，下载后分块核对实际大小及 MD5（如果消息提供）；这是下载前后校验，不是下载期间的磁盘配额。不会自动打开或执行附件。暂不补下载旧消息，也不处理仅提供 `attachid`、缺少可用 CDN 下载信息或文件大小的附件。

## 验证方法

先发一个只有固定测试文字的 TXT 到文件传输助手，确认手机端能打开并看到相同内容。接收测试请用另一个微信号发一个普通文件给机器人，检查终端的 `ready` 状态和实际保存内容。手机同账号向文件传输助手发送的同步消息可能走不同路径，不能替代好友来信测试。

底层适配 Windows x64 微信 4.1.15.13 / 4.1.15.56，并核对核心 DLL 精确指纹；其他构建需要重新适配。

2026-10-19 已实测：向文件传输助手发送 TXT，手机端确认可以打开且中英文内容正确；好友发来的 TXT 下载后与源文件的 SHA-256 完全一致。2026-10-28 使用自用版向文件传输助手发送约 21 MiB 文件，手机端确认可下载打开；超过旧上限的文件接收使用离线模拟验证。测试结束后原生桥接正常卸载。更大文件及不同会话的行为仍受微信和运行环境影响。
