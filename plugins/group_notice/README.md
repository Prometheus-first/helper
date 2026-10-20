# 进群欢迎与退群提醒

在运行 `main.py` 的控制台输入 `enable group_notice` 开启，`disable group_notice` 关闭。

仅处理框架确认的群成员变化，普通聊天不会触发。第一次读取成员列表只建立基线，不会欢迎全部老成员。

`bot_config.json` 的 `plugins.group_notice` 可设置 `welcome`、`leave_notice`，以及只支持 `{nickname}` 占位符的 `welcome_template`、`leave_template`。修改文案后输入 `reload group_notice`。

群名单每 15 秒核对一次，名单变化连续两次确认后才发提醒；缓存未就绪、读取失败或名单不完整时不推断退群。带明确成员 ID 的受支持系统事件须由完整实时名单核对后，才提前通知。只读缓存，不主动请求服务器刷新，微信未同步名单时可能延迟。重连后重新建立基线，不补发离线变化。
