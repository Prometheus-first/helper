# 合并转发聊天记录

`native_text` 后端会解析启动后实时收到的合并转发（微信类型 `49`，`appmsg/type` 为 `19`）。不需要额外命令或 API 密钥，好友私聊、群聊和文件传输助手使用同一接收路径。

## 文字与图片

每条内部记录保留原发言者名称、原消息时间和文字。图片通过现有原生下载接口获取，核对文件大小和 MD5 后，沿用 `image_directory` 按账号隔离保存。真实格式以文件头为准，不信任转发记录中的扩展名。

下载范围沿用 `chat_allowlist`：列表为空时不限制，非空时只下载其中会话的图片。其他会话仍可在消息监控中展开文字和图片标记，但图片显示“未下载”。单张图片失败不会丢弃同一转发里的文字及其他图片。

语音、视频、文件、链接和嵌套合并转发显示类型标记，本功能不下载或递归展开它们。若消息只有引用、未携带内层记录，或内容不可解析，会显示无法解析状态。

## 消息监控

收到合并转发后，先显示合集标题，再逐条显示内层记录。会话 ID 是接收这条转发的群 ID 或私聊 wxid；每条记录标出原作者、原时间以及转发者，便于区别现场发言与转发历史。

转发中的作者名称属于消息自带信息，不等于已验证的微信账号。没有可靠 wxid 时保留为空，不把名称或哈希值当作 wxid。监控不展示图片预览、本地文件路径、原始 XML 或下载凭据。图片状态会从“等待下载”更新为“已保存”或“下载失败”，同一条记录不会重复新增。

内层文字不会作为新收到的普通文本重新执行，因此其中的 `/清空`、`/切换人格` 等文字不会触发指令，也不作为原作者今天的新发言写入聊天总结历史。

## 插件读取

插件需要声明类型 `49` 和能力 `forwarded.receive`：

```json
{
  "api": "v1",
  "message_types": [49],
  "requires": ["forwarded.receive"]
}
```

下载处理结束后，插件收到一个合并转发事件；即使部分图片失败，仍会收到已解析的文字及图片结果。

```python
def on_message(self, message, ctx):
    if message.data.get("app_type") != 19:
        return
    for record in message.data.get("forwarded_records", ()):
        name = record.get("sender_name", "")
        original_time = record.get("source_time", "")
        if record["kind"] == "text":
            text = record["text"]
            # 在这里处理转发文字，保留它来自转发的语义。
        elif record["kind"] == "image" and record.get("image_status") == "ready":
            path = record["image_path"]
            # path 是当前机器人账号目录中的本地图片。
```

`forwarded_title` 保存合集标题，`forwarded_status` 为 `ready` 或 `partial`，`forwarded_truncated` 表示仅保留了前面部分记录。每张图片包含 `image_status`、`image_path` 和不含凭据的 `image_error`。事件不向插件提供 CDN 地址或密钥。

## 限制与升级

每条转发 XML 小于 256 KiB，最多展开前 200 条记录；每条最多下载 16 张图片、合计声明大小不超过 64 MiB，单张不超过 20 MiB。超过图片额度时保留图片标记和文字。超过接收大小上限的分享消息显示“内容过长或不可读”。监控每条正文仍最多显示 2000 字符，插件文字最多保留 32768 字符。

本次升级包含新的桥接 DLL 和 V4 接收接口，并保留旧版 V1–V3 导出的缓冲区边界。更新前应正常停止机器人并关闭管理台，再替换代码和 DLL，之后自行重新启动。不会自动补读升级前的微信历史；需要查看旧合集时，请在启动后重新转发。
