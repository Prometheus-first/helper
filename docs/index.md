---
layout: home
title: 微信机器人 Python 插件框架
hero:
  name: helper
  text: 微信机器人的 Python 插件框架
  tagline: 网页管理多个账号，实时查看消息，按需加载插件。也可使用 main.py 控制台入口。
  image:
    src: /logo.svg
    alt: helper 标志
  actions:
    - theme: brand
      text: 安装与启动
      link: /guide/quick-start
    - theme: alt
      text: 网页管理台
      link: /guide/web-panel
    - theme: alt
      text: 编写第一个插件
      link: /development/plugins
    - theme: alt
      text: GitHub 仓库
      link: https://github.com/Prometheus-first/helper
features:
  - icon: 📷
    title: 朋友圈
    details: 发布文字和图文，读取已加载记录及互动，手动点赞或评论。
    link: /guide/web-panel#朋友圈
  - icon: 💬
    title: 文本与真实艾特
    details: 文件传输助手、好友和群聊使用统一接口，群回复支持真正的成员艾特。
    link: /messages/text
  - icon: 🖼️
    title: 图片与动态表情
    details: 下载普通图片、发送本地图片或从免费图库取图，提取本地缓存表情并发送动态 GIF。
    link: /messages/images
  - icon: 🎙️
    title: 语音合成与发送
    details: 将模型回复合成为微信语音条，浏览中文公开音色，按群保存音色选择。
    link: /messages/voice
  - icon: 🧩
    title: 看图猜成语
    details: 在线获取题图和答案，失败回退本地题库；群友抢答、真实艾特、提示和自动换题。
    link: /guide/features#看图猜成语
  - icon: 📎
    title: 普通文件附件
    details: 发送本地文件，下载实时到达的附件并按账号隔离保存，单个文件最多 20 MiB。
    link: /messages/files
  - icon: 🧩
    title: 功能按插件加载
    details: 每个插件独立处理消息，可在网页或控制台启停、重载，也能扩展自己的业务。
    link: /development/plugins
  - icon: ✨
    title: 大模型群聊功能
    details: 成语接龙、当天聊天总结、人格切换与艾特对话，模型配置从本地文件读取。
    link: /guide/features
  - icon: 🔗
    title: 账号选择与隔离
    details: 网页勾选已登录微信并批量启动，各账号配置和数据独立，各群称呼分别保存。
    link: /guide/account-and-groups
  - icon: 🖥️
    title: 网页管理与消息监控
    details: 一键打开本地管理台，逐个或批量启动账号，查看时间、会话 ID、发送者和消息内容。
    link: /guide/web-panel
  - icon: 👋
    title: 群成员与欢迎提醒
    details: 查询成员 ID 和群昵称，按群监控新人加入与成员退出，自定义欢迎和提醒文案。
    link: /guide/friends-and-groups#进群欢迎退群提醒和群友昵称
---

**使用许可：允许个人自用，禁止商业使用。** 完整范围及第三方组件说明见[使用许可](./guide/license.md)。

## 先了解运行条件

helper 目前适配 **Windows x64、微信 4.1.15.13 / 4.1.15.56**，使用 Python 作为插件入口、原生桥接负责消息能力。版本和核心 DLL 指纹均有检查，按构建选择对应地址；其他微信版本需要重新适配。

```powershell
python -m pip install -r requirements.txt
python main.py --check
python web.py
```

也可以双击 `start_web.bat`。首次启动无需提前知道自己的 wxid，在网页选择已登录账号并启动后，即可用文件传输助手开始验证。需要控制台时使用 `python main.py`。

## 从你要做的事情开始

| 需求 | 对应说明 |
| --- | --- |
| 第一次安装并运行 | [安装与启动](./guide/quick-start.md) |
| 网页管理多个账号、实时查看消息 | [网页管理台](./guide/web-panel.md) |
| 找到账号 ID、加入新的群 | [账号与群聊](./guide/account-and-groups.md) |
| 发送文本、图片、文件、GIF 或语音 | [文本](./messages/text.md) · [图片](./messages/images.md) · [文件](./messages/files.md) · [表情](./messages/emoticons.md) · [语音](./messages/voice.md) |
| 展开合并转发的文字、原作者和图片记录 | [合并转发解析](./messages/forwarded.md) |
| 开启接龙和聊天总结 | [群聊功能](./guide/features.md) |
| 艾特后用语音回复、查看和切换音色 | [Fish Audio 语音回复](./messages/voice.md#fish-audio-文字转语音) |
| 自动通过好友申请、使用控制台群管理 | [好友申请与群管理](./guide/friends-and-groups.md) |
| 进群欢迎、退群提醒、查询群昵称 | [群成员与欢迎提醒](./guide/friends-and-groups.md#进群欢迎退群提醒和群友昵称) |
| 编写自己的插件 | [插件开发](./development/plugins.md) |
| 处理启动或清理问题 | [常见问题](./guide/faq.md) |

本项目仅供个人非商业使用，禁止商用，请勿用于违法行为。功能范围与已验证边界在对应章节中说明。
