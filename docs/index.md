---
layout: home
title: 微信机器人 Python 插件框架
hero:
  name: helper
  text: 微信机器人的 Python 插件框架
  tagline: 一个 main.py 持续运行。把文本、图片、文件收发和表情、语音发送接进自己的插件。
  image:
    src: /logo.svg
    alt: helper 标志
  actions:
    - theme: brand
      text: 安装与启动
      link: /guide/quick-start
    - theme: alt
      text: 编写第一个插件
      link: /development/plugins
    - theme: alt
      text: GitHub 仓库
      link: https://github.com/Prometheus-first/helper
features:
  - icon: 💬
    title: 文本与真实艾特
    details: 文件传输助手、好友和群聊使用统一接口，群回复支持真正的成员艾特。
    link: /messages/text
  - icon: 🖼️
    title: 图片与动态表情
    details: 下载普通图片、发送本地图片，提取本地缓存表情并发送动态 GIF。
    link: /messages/images
  - icon: 🎙️
    title: 原生语音条
    details: 发送 SILK 或将 PCM WAV 转成语音条，文件传输助手已验证手机端播放。
    link: /messages/voice
  - icon: 📎
    title: 普通文件附件
    details: 发送本地文件，下载实时到达的附件并按账号隔离保存，单个文件最多 20 MiB。
    link: /messages/files
  - icon: 🧩
    title: 功能按插件加载
    details: 每个插件独立处理消息，可在控制台启停、重载，也能扩展自己的业务。
    link: /development/plugins
  - icon: ✨
    title: 大模型群聊功能
    details: 成语接龙、当天聊天总结、人格切换与艾特对话，模型配置从本地文件读取。
    link: /guide/features
  - icon: 🔗
    title: 绑定机器人账号
    details: 首次启动自动识别或选择账号，多开时按绑定身份连接，各群称呼独立保存。
    link: /guide/account-and-groups
---

## 先了解运行条件

helper 目前适配 **Windows x64、微信 4.1.15.13**，使用 Python 作为插件入口、原生桥接负责消息能力。版本和核心 DLL 指纹均有检查，其他微信版本需要重新适配。

```powershell
python -m pip install -r requirements.txt
python main.py --check
python main.py
```

首次启动无需提前知道自己的 wxid。按照终端提示完成账号选择后，即可用文件传输助手开始验证。

## 从你要做的事情开始

| 需求 | 对应说明 |
| --- | --- |
| 第一次安装并运行 | [安装与启动](./guide/quick-start.md) |
| 找到账号 ID、加入新的群 | [账号与群聊](./guide/account-and-groups.md) |
| 发送文本、图片、文件、GIF 或语音 | [文本](./messages/text.md) · [图片](./messages/images.md) · [文件](./messages/files.md) · [表情](./messages/emoticons.md) · [语音](./messages/voice.md) |
| 开启接龙和聊天总结 | [群聊功能](./guide/features.md) |
| 编写自己的插件 | [插件开发](./development/plugins.md) |
| 处理启动或清理问题 | [常见问题](./guide/faq.md) |

本项目仅供学习，请勿用于违法行为。功能范围与已验证边界在对应章节中说明。
