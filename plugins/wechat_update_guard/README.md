# 微信更新保护

此插件默认关闭，只针对已核对核心指纹及更新器哈希的 Windows 微信 4.2.0.12。启用时从微信注册表或已运行的微信定位安装目录，将更新器改名为 `WeixinUpdate.exe.helper-disabled`，把检测到的 4.2.0.13 更新缓存移到当前用户本地备份目录；不会删除聊天数据。

插件需要机器人启动后才能运行，可能赶不上微信启动时已触发的更新。需要在微信启动前处理时，在项目目录执行：

```powershell
python tools/wechat_update_guard.py --protect
```

要恢复微信的更新程序，执行：

```powershell
python tools/wechat_update_guard.py --restore
```

恢复时不会自动把之前备份的更新缓存放回，避免意外立即升级。缓存备份保存在 `%LOCALAPPDATA%\helper-wechat-update-backup`。每次检查保护时，只保留最新一份已完成的缓存备份，自动清理旧版积累的缓存备份；没有新下载时也会清理。关闭插件不会自动恢复更新器。

不启动微信或机器人也可以单独清理旧缓存备份：

```powershell
python tools/wechat_update_guard.py --cleanup-backups
```

清理仍保留最新一份缓存。安装目录中的 `WeixinUpdate.exe.helper-disabled` 是恢复更新功能所需的更新器，始终保留。只清理本插件已知命名和内容结构的缓存目录；其他文件、含链接的目录及未搬完的 `pending-*` 备份不会被当作旧缓存删除。

此措施可能失效，例如微信重建更新器或采用其他更新途径；它不是微信官方支持的更新开关。不同版本、安装布局、用户权限或不同的更新器文件可能无法使用，遇到不匹配时会停止并报告，不自动提权。停用更新意味着不会按原方式获取后续安全更新。
