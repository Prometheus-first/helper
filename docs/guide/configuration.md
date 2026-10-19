# 配置参考

框架读取项目目录里的 JSON 配置。仓库中的 `*.example.json` 是通用模板，本地生成的配置已被 Git 忽略。

| 文件 | 用途 |
| --- | --- |
| `bot_config.json` | 账号、会话范围、插件开关、发送节奏 |
| `members_config.json` | 支持的群、群内称呼、消息记录 |
| `llm_config.json` | 大模型地址、密钥、模型与超时 |

## 机器人配置

常用字段如下，完整默认值以 `bot_config.example.json` 为准。

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `backend` | `native_text` | 文本、图片、文件、表情和语音能力使用此后端 |
| `wechat_account` | `null` | 首次启动自动识别或选择，之后保存机器人 wxid |
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

相对文件路径以项目配置目录为基准。修改账号、群范围或配置路径后重启 `main.py`。

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
  "max_tokens": 512
}
```

以上是项目模板的接口和模型配置。密钥填写自己的值，模型名称以服务商实际提供的模型为准。配置读取入口由 `bot_config.json` 的 `llm_config_file` 指定。

模板默认关闭 `idiom_chain`、`chat_summary`、`mention_chat`。填写有效模型配置后，开启需要的插件，再执行 `--check`。

修改模型配置后重载对应插件或重启。聊天总结、艾特对话会将对应文本交给你配置的模型服务；记录范围见[群聊功能](./features.md)。

## 更新项目时保留本地配置

账号、模型密钥、群绑定及聊天记录都保存在本地配置和 `.runtime/` 中。不要把这些文件加入 Git 或放进 `docs/`；文档示例均使用虚构 ID 和占位密钥。
