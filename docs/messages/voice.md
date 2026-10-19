# 语音发送

已在微信 4.1.15.13 的文件传输助手完成 2 秒合成测试音发送，手机端确认收到并可正常播放。好友、群聊共用目标 ID 接口，尚未分别实机验证。

控制台输入 `send-voice 会话ID "完整语音路径"`；会话 ID 可为 `filehelper`、好友 ID 或群 ID。

```powershell
python -m pip install -r requirements-voice.txt
python main.py
```

安装可选依赖后，可以发送单声道、16-bit PCM WAV，采样率支持 8/12/16/24 kHz。WAV 会转成 Tencent SILK，并补齐最后不足 20 毫秒的一帧；源文件保留。直接发送已经编码好的微信 SILK 不需要编码依赖；标准 SILK 的完整头尾会在发送快照中规范化。只支持 20 ms/包的 SILK v3 封装，时长按包计数，不保证任意第三方 SILK 载荷都可解码。暂不支持 MP3、AAC、立体声或其他 WAV 编码；单条最多 60 秒，输入文件最多 20 MiB。

插件声明 `"requires": ["voice.send"]` 后调用：

```python
ctx.send_voice("filehelper", voice_path)
ctx.reply_voice(message, voice_path)
```

发送按连接和账号核对，不使用文本随机等待，也不调用大模型。文件快照放在本项目 `.runtime/voices/` 的当前账号子目录，调用结束后清理；原生入口在排队前把音频字节复制到任务持有的内存，避免超时或退出时删除快照影响已排队任务。底层仍锁定微信 4.1.15.13，发送结果为微信原生接口返回的接受状态，不能单独证明对方已经收到或成功播放。本功能尚未接入语音接收、语音识别或文字转语音。
