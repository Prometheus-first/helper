# 小程序通用接口

Windows 微信 4.2.0.12 可以通过原生小程序 IPC 唤起小程序、读取组件、点击、输入文字，以及获取截图。接口不使用系统鼠标、键盘模拟或 UIA，也不开放远程调试端口。

当前已在美团小程序验证唤起、读取页面和组件、点击进入搜索页、中文输入。其他小程序可复用同一套接口，但画布、自绘组件和小程序自身业务权限仍可能限制可读取的信息。

## 使用

从当前机器人连接取得客户端，不填写账号或固定进程编号：

```python
# 在已经连接微信的插件业务函数中使用。
mini = ctx.miniprogram("wxde8ac0a21135c07d", expected_session=message.session_id)
mini.open()

# 获取当前页面的组件描述，包含类型、名称、文字、ID、类名和 placeholder。
components = mini.components()
for component in components:
    print(component.tag, component.name, component.element_id, component.classes)

# 可选：取得小程序原生截图，用它确认页面布局。
image_bytes = mini.screenshot()

# 坐标仅为示例，需要根据当前原生截图定位；不是桌面上的坐标。
mini.click(300, 150)

# 向当前小程序中已聚焦的输入框输入，不会自动点击搜索或提交。
mini.input_text("示例文字")
mini.input_text("替换输入框内容", replace=True)
```

插件需要在 `plugin.json` 的 `requires` 中声明 `miniprogram.control`。客户端只属于创建它时的机器人连接。断线或切换账号后需要重新获取客户端并 `open()`；同一客户端可以继续操作多个页面。不要在每条消息中反复创建客户端。

## 接口

| 方法 | 行为 |
| --- | --- |
| `open(entry_url="")` | 原生唤起目标 AppID，可选择入口路径 |
| `page_info()` | 读取当前小程序名称、AppID 和页面路径 |
| `components()` | 读取当前页面的组件类型、名称、文字、ID、类名、角色、占位文字、禁用属性及 frame 内 XPath |
| `click(x, y, expected_page=None)` | 在原生截图对应的页面坐标派发一次点击 |
| `input_text(text, replace=False, expected_page=None)` | 输入文字，可选择全选替换；要求输入框已经聚焦 |
| `screenshot()` | 返回原生 JPEG 字节，不自动保存 |
| `evaluate(script)` | 在小程序 AppService 执行 JS，可访问 `getCurrentPages()`、`getApp()`、`wx` |

`evaluate()` 不是浏览器页面执行环境，没有 `window` 和 `document`。组件列表通过原生 DOM 通道读取，能够继续读取当前页面 iframe 内的组件。列表里的 XPath 相对于该 frame；本版 `click()` 接受坐标，不把跨 frame XPath 冒充为已经支持的组件点击。

组件描述保留渲染名称和属性，不包含输入框当前值、图片 URL、页面脚本或任意 `data-*` 数据。`disabled` 来自 DOM 属性，不代表完整可见性和可点击性判断。组件树无法读取画布内部单独绘制的按钮。

可以把任意组件的 `page` 传给 `expected_page`，避免使用旧页面位置操作新页面：

```python
page = components[0].page
mini.click(300, 150, expected_page=page)
```

点击和输入返回 `{"status": "dispatched"}` 表示引擎接收了事件，不代表登录、查询、下单等业务已经完成；按需求使用页面信息、组件或截图核对结果。超时和取消后不会自动重发操作。

## 运行要求

- 微信核心和小程序宿主运行库均做指纹校验，目前锁定已验证的 4.2.0.12 组合。运行库可以独立升级；不匹配时接口拒绝调用。
- 首次使用前，需要在目标账号的微信打开过任意小程序，让运行库加载。此后可以原生唤起其他 AppID。
- 使用随项目提供的 `native/build_miniprogram/miniprogram_bridge.dll`，普通使用不需要 C++ 开发环境。
- 开发者重编译：在 MSVC x64 开发者终端运行 `powershell -File native/build_bridges.ps1 -Target miniprograms`。
- 同一个微信 PID 的原生命令串行执行，不同机器人账号各自独立。服务停止和异常退出守护会一并清理小程序观察 Hook。
- 页面和输入只返回给调用方，不写入消息监控、运行日志或默认配置。截图也由调用方自行决定是否保存。

目前没有美团专属下单、登录或手机号授权流程，也没有网页小程序操作面板。
