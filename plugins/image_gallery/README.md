# 图库图片

群里发送 `/来张图` 或 `/美女`，机器人从图库 API 获取一张图片并回复。只发图片，不额外发送说明文字。默认每个群 10 秒内响应一次，群之间分别计时；不处理私聊、自身消息或已过期的排队指令。

默认启用。运行中的管理台可在“插件管理”找到“图库图片”，启用或重载；控制台可执行 `reload image_gallery`。只在成员配置支持的群中工作，同时沿用框架会话白名单。

## 免费图源

| `source` | 图源 | 返回格式 |
| --- | --- | --- |
| `xxapi`（默认） | [小小 API：随机小姐姐图片](https://xxapi.cn/doc/meinvpic) | JSON 的 `data` 字段 |
| `wzapi` | [万站 API：随机美女图片](https://wzapi.com/doc/sjmntp.html) | JSON 的 `data.wallpaper` 字段 |
| `custom` | 自己配置的 HTTPS 图库 API | JSON 地址或直接返回图片 |

2026-10-24 两家免费图源均已在不传密钥的情况下实测取得 JSON 和有效 JPEG 图片。图片来自图源自身的图库，插件不生成图片。接口只收到固定取图请求，不会收到微信账号、群号、成员名称或聊天正文。

## 配置

配置放在当前机器人 `bot_config.json` 的 `plugins.image_gallery` 中；网页插件设置也可编辑这些字段：

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

将 `source` 改为 `wzapi` 即可换图源；保存后重载本插件。单张下载默认最多 10 MiB，可调至 20 MiB；API 请求和图片下载各自受超时限制。

自定义 JSON 接口示例：

```json
{
  "enabled": true,
  "source": "custom",
  "api_url": "https://example.com/api/random",
  "response_type": "json",
  "json_path": "data.images.0.url",
  "image_hosts": ["images.example.com"],
  "headers": {"Authorization": "Bearer YOUR_API_KEY"}
}
```

`json_path` 支持对象字段和数组下标。接口直接返回图片时，改为 `"response_type": "image"`，不再解析 JSON。密钥只放在本地配置中；`headers` 只用于 API 的同源请求，不随图片下载或跨域重定向发送。图源更换 CDN 时，可通过 `image_hosts` 配置允许的图片域名或域名后缀。

## 缓存与退出

图片会校验格式、大小和像素数，移除原始元数据，并转换为普通 JPEG；动态图使用首帧。缓存位于 `.runtime/image_gallery/` 的账号散列目录，成功发送或发送前取消后立即清理该次文件。

微信连接改变、插件停用或群移出支持列表后，未发送的旧请求会取消。原生发送返回失败或抛出异常时可能仍有排队任务，因此保留该次缓存，不自动重试发送；需要清理这类文件时，先停止机器人并关闭对应微信。该目录沿用 `.runtime/` 的忽略规则，不应提交到公开仓库。
