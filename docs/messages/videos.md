# 视频消息

微信 4.2.0.12 的 Windows 原生后端支持接收 type 43 视频后，通过微信 CDN 下载并按账号保存。默认目录是 `.runtime/videos`；多账号使用各自的账号目录。`chat_allowlist` 控制可下载的会话，空列表允许所有会话。已实测好友私聊自动下载，不需打开或播放视频；群视频的路由已离线验证，尚未单独实机验证。

有完整的原始视频引用时优先下载原始视频，否则使用普通视频。选择的引用必须同时包含 CDN 参数、声明大小和 MD5；下载结果经大小、MD5 和 MP4 结构校验，失败不会悄悄切换到其他版本。原始视频引用指微信提供的版本，不保证与发送前文件逐字节一致。

需要接收视频的插件在 `plugin.json` 中声明：

```json
{"requires": ["video.receive"], "message_types": [43]}
```

插件在 `on_message(self, message, ctx)` 中读取 `message.data["video_status"]`：`ready` 表示视频已保存，`failed` 表示下载或校验失败。成功时 `message.video_path` 是本机文件路径，`message.data["video_quality"]` 为 `original` 或 `standard`；下载失败的原因在 `video_error`。插件事件不包含 CDN 密钥或原始 XML。正在下载的消息只进入监控，完成后才交给普通插件。

接收只处理运行期间的新视频，不会补抓历史；短视频 type 62、合并转发中的视频和视频防撤回尚未适配。其他微信版本不会启用视频 CDN 下载。下载受微信可用性、账号登录状态、原消息提供的参数和本机存储空间影响。
