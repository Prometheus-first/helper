# Linux 群成员与群事件依据

目标与 `MENTIONS.md` 相同：Linux 微信 4.1.13.26 x86_64，精确 ELF 指纹由 `VerifiedLinuxProcess` 检查。
数据库使用当前受管进程实际打开的 `contact.db`，校验进程账号、目录、文件归属与 inode。
临时密钥仅在内存中用于 SQLCipher 只读连接，不写密钥文件或解密副本。

## 实机数据库结构

- `chat_room(id, username, owner, ext_buffer)`：目标群、群主、protobuf 扩展。
- `chatroom_member(room_id, member_id)`：独立成员索引。
- `contact(id, username, nick_name, delete_flag, is_in_chat_room, ...)`：成员 ID 与微信名。

一次事务同时读取群记录、扩展完整成员与索引，要求两份 ID 集合严格相同、无重复、包含当前账号。
未缓存、已经离群、损坏扩展、缺失联系人或索引不一致均返回 `None`，不得生成退群事件。
群主来自 `chat_room.owner`；其他人的管理员身份未核对，不从成员 flag 猜测。
只读连接每次开启新事务；每次查询前后重新检查进程、账号、数据库路径、inode 和 salt。
失败时关闭连接并短暂退避；不返回上一次成员结果。

2026-11-04 在 WSL Ubuntu 26.04 上，测试群三位成员均能读取，与真实群及账号匹配；网页显示成员、微信名和群主。
一次测试首次准备耗时约 4.4 秒，同连接下一次读取约 3 毫秒；这只是该测试群的观察，不是性能保证。

## protobuf 静态证据与边界

地址为模块相对 VA。

| 消息 | 字段 | 已核对类型/位置 | 依据 |
| --- | --- | --- | --- |
| `ChatroomExtData` | 1 | repeated `ChatroomMemberLocalData`，对象 `+0x08` | parser `0x7b50d50`，成员解析 `0x7b50edc` |
| `ChatroomMemberLocalData` | 1 | string，`+0x08`；实机为成员 wxid | parser `0x7b50436`，实机与 SQL 索引对应 |
| 同上 | 2 | string，`+0x10`；群内显示名候选 | parser `0x7b504de`，serializer `0x7b5065e` |
| 同上 | 3 | varint，`+0x20` | parser store `0x7b50578`，不用于猜身份 |
| 同上 | 4 | string，`+0x18` | parser `0x7b505a0`，不用于猜身份 |
| `ChatroomExtData` | 5 | repeated string，`+0x30` | tag `0x2a` 分支 `0x7b510d1`、读取 `0x7b51138`；尚不能据此认定管理员 |

类型名分别由 `0x7b50870` 和 `0x7b516d0` 返回。
业务函数 `0x78b2570` 按群和成员 ID 取出字段 2，`0x78b260b` 返回该字符串；
同步更新 `0x78acb3d` 从服务器成员对象 `+0x18` 复制至本地字段 2。
字段 2 的群昵称语义仍需修改已知测试成员群昵称作实机闭环；仅凭生成代码不能证明语义。

## 群事件

原生接收桥现在传递 `10000` 和 `10002` 系统帧，正文上限 16 KiB。
Linux 适配器只把严格解析通过的群系统内容交给共用 `GroupMembershipTracker`，普通聊天不能冒充系统事件。
撤回继续走原有监控路径。系统 XML 不进入公开消息监控，仅确认事件投影为入群/退群摘要。

首次完整名单只建基线；差分至少两次完整快照确认，失败打断确认；
结构化系统事件须由当前完整名单佐证；无 ID 的名字不能独立产生事件。
连接改变后丢弃旧基线、待确认变化和去重状态。退群/被移出统一使用 `leave`，不推测原因。

离线验证覆盖真实 C 接收路径、系统消息类型、长度限制、名单交叉检查、SQLCipher 只读事务、
连接复用但结果更新、账号改变、旧会话拒绝、基线静默、失败打断、系统/差分去重，以及网页成员选择。
真实进退群变化仍待测试成员操作后核对；不把 fixture 成功描述为实机成功。
