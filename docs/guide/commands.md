# 控制台命令

以下命令输入到运行 `python main.py` 的终端窗口中。群里的聊天指令见[群聊功能](./features.md)。

## 状态与会话

| 命令 | 说明 |
| --- | --- |
| `help` | 显示本地控制台帮助 |
| `status` | 查看连接、当前账号与插件状态 |
| `chats` | 查看本次连接收到过的会话 ID |
| `members` | 查看成员目录 |
| `members 群ID` | 查看指定群成员 |
| `history 昵称 10` | 查询该称呼的最近文字记录 |

## 发送媒体

```text
send-image filehelper "E:\图片\示例.png"
send-emoticon filehelper "E:\表情\示例.gif"
send-voice filehelper "E:\语音\示例.wav"
send-file filehelper "E:\文件\示例.txt"
```

把 `filehelper` 换成好友或群的实际 ID，即可向该会话提交发送。路径有空格时保留双引号。格式、大小和时长限制分别见[图片](../messages/images.md)、[文件](../messages/files.md)、[表情](../messages/emoticons.md)、[语音](../messages/voice.md)。

## 导出本地表情

```text
export-emoticons
export-emoticons raw
```

需要先配置当前账号的 `wechat_data_directory`。第一条导出并尝试转成可发送的 GIF；`raw` 只导出原件和缩略图。

## 管理插件

```text
plugins
enable echo
disable echo
reload echo
reload
```

`plugins` 显示处理数、异常和队列丢弃情况。`reload 名称` 只重载一个插件，`reload` 重载插件集合。正在处理长回调时，重载可能提示稍后再试。

修改账号、群配置、原生 DLL 后应停止并重启主程序，插件重载不会替换这些内容。

## 退出

```text
quit
```

`exit`、`stop` 也可以退出。等待框架完成连接清理后再重启；微信无需退出。
