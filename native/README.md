# 原生消息适配

当前适配 Windows x64 微信 4.1.15.13 / 4.1.15.56；使用前核对 `compatibility.json` 中的核心文件指纹。加载前按文件大小与 SHA-256 匹配构建，原生端还核对已加载映像的 PE 标识；未知构建会拒绝加载。

运行需要的组件：

- `text_service.py`：文件传输助手、好友、群聊的原生文本、图片、文件附件服务及表情、语音发送。
- `filehelper_service.py`：接收通道和服务公共实现。
- `run_selftest.py`：进程、版本、远程调用与桥接生命周期封装。
- `core_version.h`：按精确构建解析原生地址，调用、虚表校验和 Hook 卸载共用同一份映射；`wxbot_framework/native_profiles.py` 提供对应的只读账号、缓存地址和表情解码参数。
- `bridge_watchdog.py`：主程序异常退出后的隐藏清理守护入口，运行时需要一起保留。
- `build_text/text_bridge.dll`：当前消息收发和真实艾特桥接。
- `emoticon_send.h`：本地 GIF 的原生类型 47 发送，含账号核对、文件限制和共享引用清理。
- `voice_send.h`：语音对象构造与原生类型 34 发送，入口为 `WxSendVoice` / `WxInspectVoicePath`；排队前复制音频字节，沿用任务存活和安全卸载检查。
- `file_download.h`：普通文件附件的 CDN 下载，入口为 `WxDownloadFile`；按声明大小和完成大小校验，原始 CDN 参数不写入报告。
- `file_send.h`：普通文件附件发送，入口为 `WxSendFile` / `WxInspectFilePath`；排队前复制字节，使用微信自己的 InputTemp 路径供上传任务读取。
- `friend_receive.h`：独立好友验证接收通道，仅复制实时申请，跳过历史初始化记录；沿用 V3 内存队列。
- `friend_request.h`：`WxInspectFriendRequest` / `WxAcceptFriendRequest`，核对账号并等待微信原生验证结果；申请凭据不写报告。
- `group_management.h`：原生邀请好友进群及创建群聊，创建成功时读取服务器返回的群 ID；未进行真实群管理操作测试。
- 群系统消息 `10000/10002` 不作为普通聊天正文；完整群成员和真实群昵称由 `wxbot_framework/group_members.py` 只读获取，`group_events.py` 负责变动确认与去重。
- 当前接收通道为 `WxPollMediaMessageV6`，在 V4 的元数据缓冲区后追加 64 位微信原始消息编号，以十进制字符串传给 Python，V6 在 V5 后追加音频偏移与长度，保留 V1–V5 调用方的缓冲区边界。普通消息与独立系统 XML 入口共同提供撤回关联；新增 Hook 沿用回调计数和正常卸载流程。管理台保留原记录，`anti_recall` 插件控制自动补发。
- `build/selftest_bridge.dll`：兼容文件传输助手后端。
- `moments_send.h` / `moments_transport.py`：微信 4.1.15.56 原生朋友圈发布，已实测仅自己可见的纯文字与双图发布，并由其他账号确认公开纯文字可见；4.1.15.13 暂不支持。图片先复制到微信自身的 InputTemp 缓存，完成回执含有效 SNS ID 才确认成功。取消订阅后沿用原生事件清理函数释放回调引用，活动回调内由原生 Emit 收尾，不绕过 DLL 卸载保护；结果不确定时不自动重发。
- 好友目录由 `wxbot_framework/contacts.py` 只读获取，包含资料及好友标签关联，当前仅验证 4.1.15.56；结果限于已加载的本地缓存，不发起联网同步。

源码 `text_bridge.cpp` 复用 `selftest_bridge.cpp` 的公共结构、任务调度和清理逻辑，因此两个源文件都要保留。`vendor/` 中只保留编译所需的 MinHook 源码、头文件和许可。

原始调试器、逆向数据库、跟踪日志、探针可执行文件、旧版本 DLL、编译中间产物和历史版本均不属于本目录。

重新编译使用 `build_bridges.ps1`；正常使用已提供的两个 DLL 时无需编译。账号和群聊从根目录的本地配置读取，没有预置真实用户。

4.1.15.56 已实测账号发现、文本发送、其他账号发来的群文本、撤回通知与原消息 ID 关联，以及桥接正常加载和卸载。媒体、好友验证和群管理入口已完成静态地址及布局核对，本轮未逐项重做真实网络操作。同账号从手机发送的同步文本未被当前来信入口捕获，测试请使用另一账号。
