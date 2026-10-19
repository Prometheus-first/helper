# 图片收发

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
