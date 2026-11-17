# AI 生图

在私聊或已支持的群聊发送 `/生图 提示词`，例如 `/生图 一只在草地上晒太阳的橘猫`。

插件默认关闭。先复制项目根目录的 `image_generation.example.json` 到 `.runtime/image_generation.json`，填写自己的图片生成接口地址、密钥和模型，再在网页“插件管理”启用“AI 生图”，或在控制台执行 `enable image_generation`。群聊需要先加入成员配置，私聊及群聊还受框架会话白名单限制。

```powershell
New-Item -ItemType Directory -Force .runtime
Copy-Item image_generation.example.json .runtime/image_generation.json
```

配置包括 `base_urls`、`api_key`、`model` 和 `timeout_seconds`。`base_urls` 是 1 至 8 个 HTTPS 接口地址的列表，多个地址共用一个密钥和模型；也兼容旧版单个 `base_url`。渠道需支持 OpenAI 兼容的 `/v1/images/generations`，返回 `b64_json` 或 HTTPS 图片地址。密钥保存在本机私有配置中，不要上传或分享；修改配置后下一次请求即生效。该配置由当前项目工作区内的机器人共用。

返回的图片下载地址必须解析到公网地址；使用 fake-IP 代理时，若域名解析为保留地址，需要调整代理的 DNS 模式或使用返回 `b64_json` 的服务。

提示词会同时发送给配置的 API 渠道，先完成的图片会发回会话。每个失败的渠道最多重试 5 次，所有渠道均失败时回复“生图失败”；群内会提示开始生图。首张图片成功后，其他渠道停止后续重试，但已发出的请求不一定能中断，可能继续生成并产生费用。生成过程可能需要几分钟；停止机器人或切换微信连接后不会发送过期结果。

生图最多同时执行 5 个任务，另可排队 8 个；任务过多时会提示稍后再试。渠道请求最多同时运行 20 个，正在结束的旧请求可能暂时占用名额。各任务完成后分别回复原会话，完成顺序取决于接口返回时间。接口服务商可能另有限流或并发限制。

正常发送后临时图片会删除；如果发送结果不确定，图片会暂存在 `.runtime/generated_images/`，以便微信完成读取。
