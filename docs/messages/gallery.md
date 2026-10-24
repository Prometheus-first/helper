# 图库图片

在已支持的群里发送 `/来张图` 或 `/美女`，机器人从图库 API 获取一张图片并发送。插件名为 `image_gallery`，默认启用，不需要大模型或图库密钥。

可在管理台“插件管理”启用、停用或重载“图库图片”；控制台也可执行 `reload image_gallery`。不会自动启动微信或机器人。

## 图源与配置

默认使用[小小 API](https://xxapi.cn/doc/meinvpic)，可将 `source` 改为 `wzapi` 使用[万站 API](https://wzapi.com/doc/sjmntp.html)。2026-10-24 已实测两者免费请求均可取得有效 JPEG 图片。

配置位于当前机器人 `bot_config.json` 的 `plugins.image_gallery`：

```json
{
  "enabled": true,
  "source": "xxapi",
  "commands": ["/来张图", "/美女"],
  "cooldown_seconds": 10,
  "timeout_seconds": 12,
  "max_image_bytes": 10485760
}
```

每群独立计时，默认 10 秒内响应一次；不处理私聊、机器人自身消息、艾特对话或超过 30 秒的排队指令。成功时只发送图片，失败时给出简短提示。

自定义 API 使用 `source: "custom"`，设置 `api_url`、`response_type`（`json` 或 `image`）、`json_path`（例如 `data.images.0.url`）以及 `image_hosts`（图片域名列表）。需要鉴权时在 `headers` 中设置请求头，密钥仅保存在本地配置；鉴权头不会跟随图片下载或跨域重定向发送。接口只收到固定请求，不包含微信账号、群 ID、成员昵称和聊天正文。

## 文件与连接

下载前限制响应大小，下载后校验图片格式和像素数，移除原始元数据并转换为 JPEG；动态图使用首帧。临时目录位于 `.runtime/image_gallery/` 的账号散列子目录，成功发送或发送前取消后清理本次文件。

连接变化、插件停用或群移出支持列表时，不向新会话补发旧请求。原生发送结果未确认时保留本次图片，不自动重发，以免影响仍在排队的任务；此类缓存应在停止机器人并关闭对应微信后清理。
