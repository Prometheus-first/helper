# 自动通过好友申请

默认关闭。控制台执行 `enable friend_auto_accept` 开启，`disable friend_auto_accept` 关闭。

`verification_keywords` 是验证文字的关键词列表，空列表允许所有当前支持的实时申请，非空时匹配任意一个关键词。设置保存在 `bot_config.json` 的 `plugins.friend_auto_accept` 下，修改后使用 `reload friend_auto_accept`。

仅处理当前机器人连接收到的类型37、opcode2事件，通过公开申请ID调用后端；验证票据只在后端内存中，不交给插件、不写磁盘。旧申请不会从历史中补抓，失败不会自动重复提交。

完整用法见[好友申请与群管理](../../docs/guide/friends-and-groups.md)。
