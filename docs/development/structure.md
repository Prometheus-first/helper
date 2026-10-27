# 代码结构与验证

## 入口与模块边界

`main.py` 启动单个机器人；`web.py` 启动本机管理台，由 `robot_hub.py` 管理各账号的独立工作进程。两种入口共用框架、插件和原生后端。

| 模块 | 职责 |
| --- | --- |
| `runtime.py` / `backend.py` | 微信连接、消息分发与后端能力 |
| `plugins.py` / `sdk.py` | 插件加载、隔离与业务 API |
| `members.py` / `message_monitor.py` | 群称呼、当天聊天缓存与管理台消息缓冲 |
| `llm.py` / `personality.py` / `speech.py` | 模型调用、人格与语音回复 |
| `plugin_business.py` / `plugin_sources.py` | 业务函数编辑、版本校验与默认恢复 |
| `web_panel.py` | 管理台配置、插件事务和机器人控制 |
| `panel_http.py` / `panel_common.py` | HTTP 路由、本机访问校验、共用错误与日志脱敏 |
| `robot_hub.py` | 账号发现与工作进程管理 |
| `moments_reader.py` | 只读解析微信已加载的朋友圈缓存 |
| `moments.py` | 朋友圈草稿校验、图片临时文件、发布任务与结果查询 |

表内 Python 模块位于 `wxbot_framework/`。`webui/` 只负责页面交互，通过 HTTP API 调用管理台；`plugins/` 负责具体群聊业务。

原生层见 [原生适配说明](https://github.com/Prometheus-first/helper/blob/main/native/README.md)。`native/run_selftest.py` 名称虽然包含 selftest，但承担生产运行所需的进程、远程调用和桥接清理，不能按测试残留删除。两个桥接 DLL、对应源码、版本指纹和 MinHook 也需要保留。

## 本地数据与开发资料

- `*.example.json` 是可分发模板，根目录的实际配置为本地设置。
- `.runtime/robots/` 保存账号工作区；各工作区的数据库、插件覆盖代码和媒体缓存需要保留。
- `.runtime/` 其余运行缓存和 `logs/` 不属于源码，也不应进入仓库。不要在运行中清空它们。
- 逆向工具、旧适配资料、历史快照和一次性探针不属于运行目录；需要留存时放到项目外。
- `node_modules/` 用于文档构建，可由 `npm ci` 重建；不属于机器人运行依赖。

## 离线回归

安装项目依赖后，在根目录执行：

```powershell
python tools/run_tests.py
python tools/run_tests.py --pattern test_web_panel.py --verbose
```

测试使用临时文件、模拟后端和本机测试 HTTP 服务，不启动真实机器人、不注入微信，也不发送真实消息。它们不能替代新微信版本的实机验证。

前端语法与文档构建：

```powershell
node --check webui/app.js
node --check webui/moments.js
npm ci
npm run docs:build
```

## 维护约定

新业务优先放在独立插件里，通过 SDK 使用框架能力；已有模块负责共享的账号、连接和消息状态。原生地址统一维护在现有版本映射中。

README 只保留功能概览、启动与文档入口；配置字段、详细命令、接口限制放在对应专题文档，避免两处说明逐渐不一致。移除代码前核对导入、插件清单、网页调用、构建脚本和测试，不能只凭文件名判断用途。

发布前需要检查实际配置、密钥、账号标记和媒体文件。`tools/check_publication.py` 检查的是待发布目录；开发目录包含被忽略的本地数据和依赖，检查时应先将 Git 跟踪文件及计划新增文件复制到干净目录，避免直接递归扫描运行缓存。
