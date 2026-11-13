# 代码结构与验证

## 入口与模块边界

`main.py` 启动单个机器人；`web.py` 启动本机管理台，由 `robot_hub.py` 管理各账号的独立工作进程。两种入口共用框架、插件和原生后端。

| 模块 | 职责 |
| --- | --- |
| `runtime.py` / `backend.py` | 微信连接、消息分发与后端能力 |
| `plugins.py` / `sdk.py` | 插件加载、隔离与业务 API |
| `miniprograms.py` | 账号隔离的小程序客户端、组件描述、引擎点击及输入 |
| `members.py` / `message_monitor.py` | 群称呼、当天聊天缓存与管理台消息缓冲 |
| `llm.py` / `personality.py` / `speech.py` | 模型调用、人格与语音回复 |
| `plugin_business.py` / `plugin_sources.py` | 业务函数编辑、版本校验与默认恢复 |
| `web_panel.py` | 管理台配置、插件事务和机器人控制 |
| `contacts.py` / `directory_cache.py` | 只读联系人扫描，按账号保存联系人快照和已发现群 |
| `panel_http.py` / `panel_common.py` | HTTP 路由、本机访问校验、共用错误与日志脱敏 |
| `robot_hub.py` | 账号发现与工作进程管理 |
| `articles.py` | 按账号保存公众号文章、转换只读 Markdown、缓存和校验图片 |
| `official_accounts.py` / `official_account_parser.py` | 读取关注公众号及已加载文章缓存，校验文章公开身份 |
| `official_account_sync.py` | 主动分页、临时续页标识、账号和会话核对、一次加载正文 |
| `moments_reader.py` | 只读解析微信已加载的朋友圈缓存 |
| `moments.py` | 朋友圈草稿校验、图片临时文件、发布任务与结果查询 |

表内 Python 模块位于 `wxbot_framework/`。`webui/` 只负责页面交互，通过 HTTP API 调用管理台；`plugins/` 负责具体群聊业务。

原生层见 [原生适配说明](https://github.com/Prometheus-first/helper/blob/main/native/README.md)。`native/run_selftest.py` 名称虽然包含 selftest，但承担生产运行所需的进程、远程调用和桥接清理，不能按测试残留删除。消息桥接、公众号桥接及其对应源码、版本指纹和 MinHook 也需要保留。

小程序使用独立的 `miniprogram_bridge.cpp`、`miniprogram_transport.py` 和 `build_miniprogram/miniprogram_bridge.dll`。调用结束后关闭观察 Hook 并卸载；停止与退出守护同时检查残留桥接。构建只需使用 `-Target miniprograms`，不需要替换消息桥接。

## 本地数据与开发资料

- `*.example.json` 是可分发模板，根目录的实际配置为本地设置。
- `.runtime/robots/` 保存账号工作区；各工作区的数据库、插件覆盖代码和媒体缓存需要保留。
- 各工作区的 `.runtime/directory_cache/` 保存联系人、备注、标签和已发现群，属于本地资料；重新扫描时更新，重启时读取，不作为临时测试文件清理。
- `.runtime/` 其余运行缓存和 `logs/` 不属于源码，也不应进入仓库。不要在运行中清空它们。
- `.runtime/articles/` 包含已同步的公众号目录、正文和图片，属于账号的本地数据。关注列表和微信的原始分页游标仅保留在内存，接口只返回当前账号的公开公众号字段与本地续页标识。
- 逆向工具、旧适配资料、历史快照和一次性探针不属于运行目录；需要留存时放到项目外。
- `node_modules/` 用于文档构建，可由 `npm ci` 重建；不属于机器人运行依赖。

## 离线回归

安装项目依赖后，在根目录执行：

```powershell
python tools/run_tests.py
python tools/run_tests.py --pattern test_web_panel.py --verbose
```

测试使用临时文件、模拟后端和本机测试 HTTP 服务，不启动真实机器人、不注入微信，也不发送真实消息。它们不能替代新微信版本的实机验证。

在 MSVC x64 开发者环境中运行 `native/build_bridges.ps1 -Target text` 时，还会编译并执行本机防撤回的 C++ 边界测试，检查提示编号分离、原消息与排序字段保留，以及无效输入不被修改。

前端语法与文档构建：

```powershell
node --check webui/app.js
node --check webui/moments.js
node --check webui/articles.js
node --test tests/test_*_ui.cjs
npm ci
npm run docs:build
```

## 维护约定

新业务优先放在独立插件里，通过 SDK 使用框架能力；已有模块负责共享的账号、连接和消息状态。原生地址统一维护在现有版本映射中。

README 只保留功能概览、启动与文档入口；配置字段、详细命令、接口限制放在对应专题文档，避免两处说明逐渐不一致。移除代码前核对导入、插件清单、网页调用、构建脚本和测试，不能只凭文件名判断用途。

发布前需要检查实际配置、密钥、账号标记和媒体文件。`tools/check_publication.py` 检查的是待发布目录；开发目录包含被忽略的本地数据和依赖，检查时应先将 Git 跟踪文件及计划新增文件复制到干净目录，避免直接递归扫描运行缓存。

清理个人数据时先正常退出管理台和机器人，再移除运行目录、数据库、媒体、文章、日志和测试导出，并将四份本地配置恢复为对应的示例。仅使用 Git 忽略规则不会清除磁盘上的数据；发布目录检查也不能替代 Git 历史中的个人邮箱和旧文件检查。清理后保持服务停止，下次启动时重新选择账号、填写配置。
