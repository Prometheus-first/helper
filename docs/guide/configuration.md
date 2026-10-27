# 配置参考

框架读取项目目录里的 JSON 配置。仓库中的 `*.example.json` 是通用模板，本地生成的配置已被 Git 忽略。

| 文件 | 用途 |
| --- | --- |
| `bot_config.json` | 账号、会话范围、插件开关、发送节奏 |
| `members_config.json` | 支持的群、群内称呼、消息记录 |
| `llm_config.json` | 大模型地址、密钥、模型与超时 |
| `tts_config.json` | Fish Audio 密钥、模型、默认音色与合成参数 |

## 机器人配置

常用字段如下，完整默认值以 `bot_config.example.json` 为准。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `backend` | `native_text` | 文本、图片、文件、表情和语音能力使用此后端 |
| `wechat_account` | `null` | 网页选择账号后自动对应配置；控制台首次启动时识别或选择 wxid |
| `wechat_pid` | `null` | 通常保持默认，程序按绑定账号寻找进程 |
| `chat_allowlist` | `[]` | 空列表允许全部会话；非空时只分发指定会话 |
| `image_directory` | `.runtime/images` | 收到图片的保存目录 |
| `file_directory` | `.runtime/files` | 收到文件的保存目录，按微信账号隔离 |
| `emoticon_directory` | `.runtime/emoticons` | 表情缓存导出目录 |
| `wechat_data_directory` | `null` | 仅导出表情时设置当前账号的数据目录 |
| `reconnect_seconds` | `5` | 连接重试间隔，单位秒 |
| `console` | `true` | 启用本地控制台命令 |
| `log_level` | `INFO` | 日志级别 |
| `plugin_queue_size` | `128` | 每个插件的消息队列容量 |

相对文件路径以项目配置目录为基准。修改账号、群范围或配置路径后，重新启动对应机器人。

通过[网页管理台](./web-panel.md)运行多个账号时，已有账号沿用原配置，新账号使用独立空模板。顶部“当前账号”决定正在编辑哪一份配置；保存群聊、模型等设置前，先停止该账号，再启动生效。附加账号配置和数据位于 `.runtime/robots/<实例ID>/`，不要提交该目录或将其他账号的配置复制进去。

### 文本发送节奏

```json
{
  "text_send_delay": {
    "enabled": true,
    "characters_per_group": 10,
    "min_seconds": 0.1,
    "max_seconds": 0.3
  }
}
```

此设置按文本长度增加随机等待，只调整文本发送节奏，不保证账号安全。图片、表情和语音不使用这段文本等待。

### 插件开关

在 `plugins` 中设置对应名称。例如启用聊天总结：

```json
{
  "plugins": {
    "chat_summary": { "enabled": true }
  }
}
```

示例只展示相关字段，不要用这一小段覆盖整个配置。也可以在运行窗口使用 `enable chat_summary`、`disable chat_summary`、`reload chat_summary`。

### 看图猜成语

`picture_idiom` 默认关闭，可在 `plugins` 下设置：

```json
{
  "plugins": {
    "picture_idiom": {
      "enabled": true,
      "source": "ffapi",
      "timeout_seconds": 8,
      "wrong_reply_cooldown_seconds": 3
    }
  }
}
```

`source` 支持 `ffapi`（默认在线优先，失败回退本地）和 `local`（只用本地 26 题）。`timeout_seconds` 是每次 HTTP 请求超时，支持 1–30 秒；取题和下载图分别请求，重复题最多再取一次，整轮等待可能高于这个值。接口或图片失败后一分钟内不重试在线源。

`wrong_reply_cooldown_seconds` 支持 0–60 秒，控制本群答错提醒频率。可选 `font_file` 指定中文字体，相对路径以项目目录为起点；未指定时使用本机楷体、微软雅黑或黑体。在线模式也需要中文字体，供本地备用题使用。不需要模型或额外 API 密钥。玩法与缓存说明见[看图猜成语](./features.md#看图猜成语)。

### 进群欢迎和退群提醒

`group_notice` 默认关闭，在 `plugins` 中加入以下配置或使用控制台 `enable group_notice` 开启：

```json
{
  "plugins": {
    "group_notice": {
      "enabled": true,
      "welcome": true,
      "leave_notice": true,
      "welcome_template": "欢迎 {nickname} 加入群聊！",
      "leave_template": "{nickname} 离开了群聊。"
    }
  }
}
```

文案只支持 `{nickname}` 占位符，最多 700 个 UTF-8 字节；`welcome` 与 `leave_notice` 可以独立关闭。改完文案后执行 `reload group_notice`。监控群范围和缓存更新限制见[群成员与欢迎提醒](./friends-and-groups.md#进群欢迎退群提醒和群友昵称)。

## 群聊和记录配置

```json
{
  "database_file": ".runtime/member_data.sqlite3",
  "history_enabled": true,
  "daily_clear": true,
  "groups": {
    "12345678@chatroom": []
  }
}
```

将示例群 ID 换成自己的实际群 ID，成员列表可以为空。`history_enabled` 启用当天文本记录，`daily_clear` 在北京时间每天 0 点清理旧日文本；停机跨天时，下次启动会补清理。绑定称呼保留。

如果 `chat_allowlist` 非空，还需要把该群 ID 加入会话白名单。两者的操作见[账号与群聊](./account-and-groups.md)。

## 大模型配置

```json
{
  "base_url": "https://api.deepseek.com",
  "api_key": "YOUR_API_KEY",
  "model": "deepseek-flash",
  "timeout_seconds": 20,
  "max_tokens": 512,
  "json_mode": true,
  "token_limit_parameter": "max_tokens",
  "extra_body": {}
}
```

支持 DeepSeek 和其他提供 OpenAI 兼容 **Chat Completions** 接口的服务。以上保留项目的 DeepSeek 默认地址和模型；密钥与模型名称填写服务商实际提供的值。配置读取入口由 `bot_config.json` 的 `llm_config_file` 指定，也可以在网页“模型与语音”中修改。

### 换用其他兼容服务

```json
{
  "base_url": "https://gateway.example.com/v1",
  "api_key": "YOUR_API_KEY",
  "model": "YOUR_MODEL_NAME",
  "timeout_seconds": 30,
  "max_tokens": 2048,
  "json_mode": true,
  "token_limit_parameter": "max_tokens",
  "extra_body": {}
}
```

`gateway.example.com` 是占位地址，需要替换。`base_url` 保留服务商要求的路径前缀，例如 `/v1`；程序在后面追加 `/chat/completions`。也可以直接填写完整的 `/chat/completions` 地址，不会重复追加。仅提供 Responses 或其他原生协议的服务不能直接使用。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `json_mode` | `true` | 发送 `response_format: {"type":"json_object"}`；接口不支持时改为 `false` |
| `token_limit_parameter` | `max_tokens` | 可改为 `max_completion_tokens`，请求只发送选中的一种令牌参数 |
| `max_tokens` | `512` | 上述令牌参数的数值，支持 128–4096 |
| `extra_body` | `{}` | 可选扩展请求参数；其他兼容服务默认不发送 DeepSeek 的 `thinking` 等专属字段 |

关闭 JSON 模式后，程序会通过提示词要求模型返回 JSON，并允许完整包在代码围栏中的 JSON 对象；成语接龙、总结和对话仍需要模型正确输出 JSON，普通文字不能作为结构化结果使用。网页中的这三项设置位于“大模型 → 高级选项”。

官方 `https://api.deepseek.com` 的 `deepseek-flash`、`deepseek-v4-pro` 和 `deepseek-chat` 默认关闭思考，保留旧版的快速回复行为；旧的 `deepseek-reasoner` 不强制关闭。可在 `extra_body` 中显式设置 `thinking` 或 `reasoning_effort` 覆盖默认行为，例如 `{"thinking":{"type":"enabled"}}`。使用其他域名的兼容网关时不会自动添加这些字段；如有需要，按服务商文档填写。切换服务时请检查或清空扩展参数。

若接口正常结束却返回空正文，最多重试一次。官方 DeepSeek 的 JSON 模式可能[偶发空正文](https://api-docs.deepseek.com/guides/json_mode/)，重试时改用提示词 JSON 输出，结果仍须通过严格 JSON 校验；不会把思考过程当成回复。HTTP 错误、超时、内容过滤、拒绝、工具调用或令牌截断不会自动重试。若持续出现空正文，可在网页高级选项中关闭 JSON 模式。

扩展对象最多 16 KiB，不能覆盖 `model`、`messages`、`stream`、`response_format`、令牌上限或认证字段，密钥只填 `api_key`。

远程接口使用 HTTPS；本机服务也支持 `http://localhost:端口/v1`、`http://127.0.0.1:端口/v1` 和 IPv6 回环地址。本地服务不校验密钥时，可在 `api_key` 填非空占位值。不同模型支持的参数以服务商文档为准；OpenAI 的参数定义可参阅 [Chat Completions 官方文档](https://developers.openai.com/api/reference/resources/chat/subresources/completions/methods/create)。

旧配置无需补齐新增字段，缺省值与上表一致。

模板默认关闭 `idiom_chain`、`chat_summary`、`mention_chat`。填写有效模型配置后，开启需要的插件，再执行 `--check`。

修改模型配置后重载对应插件或重启。聊天总结、艾特对话会将对应文本交给你配置的模型服务；记录范围见[群聊功能](./features.md)。

## 防撤回补发

`anti_recall`（文字、内置表情、emoji 和已缓存表情包）、`anti_recall_image`（图片）、`anti_recall_file`（文件）、`anti_recall_voice`（语音）默认开启，旧配置缺少条目时也会启用。可在网页插件管理分别关闭，或在对应 `plugins.<插件名>` 设置 `"enabled": false`。四个插件独立工作；会话白名单非空时只在其中的会话补发，不需要模型配置。表情包缓存不需要配置微信数据目录或导出收藏；附件必须已下载成功，详细限制和缓存边界见[防撤回与自动补发](../messages/recall.md)。

## 语音合成配置

`voice_reply` 的配置和按群音色切换见[语音回复](../messages/voice.md#fish-audio-文字转语音)。模板默认关闭，`tts_config.json` 的密钥与默认音色为空；填好后再启用。`tts_config_file` 默认指向本地 `tts_config.json`。

## 更新项目时保留本地配置

账号、模型密钥、群绑定及聊天记录都保存在本地配置和 `.runtime/` 中。不要把这些文件加入 Git 或放进 `docs/`；文档示例均使用虚构 ID 和占位密钥。
