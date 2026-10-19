# 本地表情与动态发送

`native_text` 后端支持 `emoticon.extract`、`emoticon.send`。发送 GIF 走微信原生表情消息（类型 47）；在文件传输助手上已验证收到后能够播放动画。`send-image` 仍是普通图片接口。

发送已有 GIF 不需要配置数据目录：

```text
send-emoticon filehelper "E:\表情\示例.gif"
```

GIF 按实际内容完整校验，最多 1 MiB，宽高各不超过 1024，路径 UTF-8 编码最多 1023 字节。本地处理还限制最多 300 帧、累计 6400 万像素。原生端会再次核对文件及当前登录账号，并要求微信将文件识别为可发送的类型 47 表情；超限或不支持的文件会报错。

要提取微信已缓存在本机的表情，在本地 `bot_config.json` 中设置 `wechat_data_directory`，指向**当前绑定账号**的数据目录，例如 `E:/WeChat/xwechat_files/wxid_example_robot_abcd`；不要填到其他账号或上层 `xwechat_files` 目录。默认 `null` 时不会读取缓存。设置后重启，在机器人控制台运行：

```text
export-emoticons
export-emoticons raw
```

第一条导出原件、已有缩略图，并尝试生成可发送的 GIF；`raw` 仅导出原件和缩略图。结果中 `gif_path` 是发送路径，`sendable` 表示该 GIF 是否通过本地发送条件校验，`error` 为单项失败原因。导出默认保存在 `.runtime/emoticons/`，按账号哈希隔离；可用 `emoticon_directory` 更改输出目录。

提取只读取所配置账号的 `business/emoticon`，支持根目录文件以及 `Persist`、`Thumb` 的分片布局，不修改微信原文件。缓存密钥只在导出期间保留于内存，不写入结果、日志或配置。仅在执行导出命令或调用导出 API 时读取，不会在启动时自动导出。本地缓存可能不全，也不能证明每个文件仍属于爱心收藏列表。

WXGF 转换通过 FFmpeg 解码，按 30 fps 生成 GIF，并在必要时减少颜色或尺寸以满足发送限制；原件会保留。转换后的播放时长不保证与微信原件一致。解码失败或无法满足限制时，结果保留原件路径并报告失败，不会把不可发送的文件标为可发送。尚未接入实时接收类型 47 消息和完整收藏排序。

插件可声明 `"requires": ["emoticon.send"]` 或 `"requires": ["emoticon.extract"]`，并调用：

```python
ctx.send_emoticon("filehelper", gif_path)
ctx.reply_emoticon(message, gif_path)
items = ctx.export_emoticons(convert=True)
for item in items:
    if item.sendable:
        ctx.logger.info("可发送表情：%s", item.gif_path)
```

运行产生的表情、聊天记录、日志和本地配置均是个人数据，请勿提交。发布检查会拒绝图片、GIF、WXGF、数据库及非空账号数据目录配置；测试只使用程序生成的样本。
