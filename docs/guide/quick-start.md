# 安装与启动

先跑通文件传输助手，再配置群聊和大模型功能。

## 运行条件

| 项目 | 要求 |
| --- | --- |
| 系统 | Windows 10/11 x64 |
| 微信 | 4.1.15.13，核心 DLL 指纹需匹配 |
| Python | 64 位，已验证 Python 3.13 |
| 运行库 | Microsoft Visual C++ x64 运行库 |
| 开发环境 | 日常使用已有 DLL，无需安装 Visual Studio |

::: warning 微信版本需要匹配
框架会检查客户端版本与核心 DLL 指纹。升级微信后如果检查失败，需要适配新版本，不能直接复用旧地址。
:::

## 1. 获取项目

```powershell
git clone https://github.com/Prometheus-first/helper.git
cd helper
```

也可以下载仓库 ZIP 并解压，在包含 `main.py` 的目录打开终端。

## 2. 安装依赖

```powershell
python -m pip install -r requirements.txt
python tools/init_config.py
```

配置初始化会生成本地配置文件，已填写的内容不会覆盖。需要发送 WAV 语音时额外安装：

```powershell
python -m pip install -r requirements-voice.txt
```

## 3. 检查配置

```powershell
python main.py --check
```

这一步检查配置结构和启用插件的语法，不识别或绑定微信账号、不注入微信，也不调用模型。需要模型的插件启用后，还会检查模型配置是否完整。

## 4. 启动机器人

```powershell
python main.py
```

在电脑微信正常登录。首次配置中的 `wechat_account` 保持 `null` 即可：

- 一个账号已登录：自动识别并保存绑定。
- 多个账号已登录：终端显示昵称和 ID，输入序号选择机器人账号。
- 尚未登录：框架保持运行并等待。

以后重启会按保存的账号 ID 连接，不需要给文件传输助手发送 `init`。也可以使用仓库里的 `start_bot.ps1` 启动同一个入口。

## 5. 用文件传输助手验证

用手机给机器人账号的文件传输助手发送：

```text
echo 你好
```

默认 `echo` 插件应回复“你好”。发送 `test`，默认 `id_responder` 插件会回复会话 ID。在电脑运行窗口中输入 `status` 查看当前连接状态。

接下来可以[添加需要支持的群](./account-and-groups.md)，或[配置大模型](./configuration.md#大模型配置)。

## 正常停止

在机器人运行窗口输入：

```text
quit
```

等待“框架已停止”后再启动另一个实例，微信可以保持打开。Ctrl+C 也会请求清理。关于异常退出后的 DLL 恢复，见[常见问题](./faq.md#提示重复运行或桥接-dll-已加载)。
