---
name: helper 本地管理台
description: 以往来目录组织身份、真实状态与日常操作的本地管理界面
colors:
  canvas: '#f7f8f5'
  surface: '#ffffff'
  shell: '#e8ede5'
  shell-hover: '#dce5d9'
  ink: '#25332b'
  muted: '#56645a'
  quiet: '#66726a'
  rule: '#dce2d9'
  accent: '#345d46'
  accent-hover: '#264b36'
  success: '#28603e'
  warning: '#825b16'
  warning-bg: '#fcf4e4'
  danger: '#9b3832'
  danger-bg: '#fff0ed'
  field-border: '#82917b'
  secondary-border: '#cbd5c7'
  secondary-hover: '#edf2e9'
  secondary-hover-border: '#b5c4af'
  text-hover: '#234a32'
  text-hover-bg: '#eaf0e6'
  disabled-text: '#7b857e'
  disabled-bg: '#edf0eb'
  disabled-border: '#d9dfd5'
  selected-row: '#edf4eb'
  table-heading: '#eff3ec'
  table-heading-text: '#4c5e47'
  table-rule: '#e4e8e0'
  nav-ink: '#3e513e'
  nav-active: '#254d32'
  on-accent: '#ffffff'
  dark-canvas: '#212121'
  dark-surface: '#282828'
  dark-shell: '#171717'
  dark-ink: '#ececec'
  dark-muted: '#bfbfbf'
  dark-quiet: '#acacac'
  dark-accent: '#f4f4f4'
  dark-on-accent: '#171717'
  dark-success: '#9fd3ac'
  dark-warning: '#e0be79'
  dark-danger: '#f1a49e'
  dark-selected-row: '#333333'
typography:
  headline:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 26px
    fontWeight: 650
    lineHeight: 1.35
    letterSpacing: -.015em
  title:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 18px
    fontWeight: 650
    lineHeight: 1.5
    letterSpacing: -.01em
  body:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 14px
    fontWeight: 400
    lineHeight: 1.6
  label:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.6
  control:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 13px
    fontWeight: 500
    lineHeight: 1.5
  field:
    fontFamily: '"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif'
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.6
  data:
    fontFamily: '"Cascadia Code", Consolas, "Microsoft YaHei UI", monospace'
    fontSize: 13px
    fontWeight: 400
    lineHeight: 1.6
  data-small:
    fontFamily: '"Cascadia Code", Consolas, "Microsoft YaHei UI", monospace'
    fontSize: 12px
    fontWeight: 400
    lineHeight: 1.6
rounded:
  control: 6px
  panel: 12px
spacing:
  '1': 4px
  '2': 8px
  '3': 12px
  '4': 16px
  '5': 20px
  '6': 24px
  '8': 32px
  '10': 40px
components:
  button-primary:
    backgroundColor: '{colors.accent}'
    textColor: '{colors.on-accent}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 8px 16px
  button-primary-hover:
    backgroundColor: '{colors.accent-hover}'
  button-secondary:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 8px 16px
  button-secondary-hover:
    backgroundColor: '{colors.secondary-hover}'
  button-text:
    backgroundColor: transparent
    textColor: '{colors.accent}'
    typography: '{typography.control}'
    rounded: '{rounded.control}'
    padding: 8px 16px
  button-text-hover:
    backgroundColor: '{colors.text-hover-bg}'
    textColor: '{colors.text-hover}'
  field-text:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    typography: '{typography.field}'
    rounded: '{rounded.control}'
    padding: 9px 12px
  search-field:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    typography: '{typography.field}'
    rounded: '{rounded.control}'
    padding: 0 12px
  navigation-item:
    backgroundColor: transparent
    textColor: '{colors.nav-ink}'
    rounded: '{rounded.control}'
    padding: 12px 16px
  navigation-item-active:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.nav-active}'
  settings-panel:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.panel}'
    padding: 24px
  directory-table:
    backgroundColor: '{colors.surface}'
    textColor: '{colors.ink}'
    rounded: '{rounded.panel}'
    typography: '{typography.body}'
  directory-row-selected:
    backgroundColor: '{colors.selected-row}'
  status-live:
    textColor: '{colors.success}'
    typography: '{typography.label}'
  status-waiting:
    textColor: '{colors.warning}'
    typography: '{typography.label}'
  status-failed:
    textColor: '{colors.danger}'
    typography: '{typography.label}'
  log-row:
    textColor: '{colors.ink}'
    typography: '{typography.data-small}'
    padding: 10px 0
  button-primary-dark:
    backgroundColor: '{colors.dark-accent}'
    textColor: '{colors.dark-on-accent}'
  surface-dark:
    backgroundColor: '{colors.dark-surface}'
    textColor: '{colors.dark-ink}'
---

# Design System: helper 本地管理台

## Overview

**Creative North Star: "往来目录"**

以“往来目录”为视觉隐喻：像一本随手可查的通讯录，先让操作者找到对象，再看清状态与可用操作。白色任务面、柔和的浅绿导航底和深色文字形成稳定的阅读关系；森林绿只承担导航、操作与链接等功能。

这是一个面向日常重复操作的本地管理台。中文界面保持紧凑而不拥挤，技术 ID、进程信息和日志使用等宽字体，长内容允许换行并保留可选择性。视觉表达依靠字重、对齐、分隔线与真实运行信息，不依赖装饰插图、外部字体或虚构活动。

**Key Characteristics:**
- 白任务面与浅绿导航，建立安静、可辨认的操作环境。
- 中文无衬线负责阅读，等宽字负责 ID 与日志。
- 身份、状态和直接操作构成可重复使用的目录签名。
- 平面分层为主，只有模态对话框使用投影。
- 桌面以对齐表格提高扫描效率，手机将记录重排而保留状态与操作。

本文件从已实现的 `webui/style.css`、`webui/index.html` 与 `webui/app.js` 提取；前置 YAML 是规范值。色阶预览与可独立渲染的组件在 `.impeccable/design.json` 中补充。色阶是由现有颜色生成的面板预览，不是新增的产品配色。

## Colors

支持亮色与暗色。首次跟随系统；手动选择后按浏览器记忆，右上角按钮切换。暗色使用中性黑灰：黑色侧栏、深灰画布与任务面、灰色选中行、近白文字及主按钮。绿色仅用于成功状态，不用于暗色背景、品牌或普通操作。完整变量覆盖记录在 sidecar 的 `extensions.themeModes`。

绿色家族偏灰、克制；白与近白承担阅读，深绿黑承担信息。色值以 frontmatter 为准，以下名称说明用途。

### Primary

- **森林绿**（accent）：主操作、链接、焦点环与勾选框；accent-hover 是主按钮的悬停反馈。
- **导航墨绿**（nav-ink / nav-active）：导航常态与当前页文字；当前页通过白底与字重共同区分。

### Neutral

- **雾白画布**（canvas）与**白纸任务面**（surface）：分别是页面底与表单、表格、状态区等内容面。
- **浅鼠尾草导航**（shell / shell-hover）：让导航与主工作区分离，悬停使用略深的同族底色。
- **绿黑正文**（ink）、**中度说明**（muted）、**低位元信息**（quiet）：构成正文、说明与辅助数据三个阅读层级。
- **细分隔线**（rule / table-rule）：承担区域与记录的边界。**字段描边**（field-border）比内容分隔线更明确。
- **表头浅绿**（table-heading / table-heading-text）与**目录选中浅绿**（selected-row）：帮助扫描结构和辨认当前管理对象。
- **次按钮边界**（secondary-border / secondary-hover-border）、**次按钮浅底**（secondary-hover）、**文字按钮反馈**（text-hover / text-hover-bg）：只用于各自控件状态。
- **禁用文字、底与描边**（disabled-text / disabled-bg / disabled-border）：同时说明控件当前不可操作；文字按钮禁用时仍保持透明底。

### Semantic States

- **连接绿**（success）：已经确认的连接或运行状态，与森林绿主操作分工。
- **待确认赭色**（warning / warning-bg）：等待、状态待更新与服务提示。
- **故障红**（danger / danger-bg）：错误状态、表单错误与破坏性密钥清除提示。
- 这些颜色承担状态语义，不构成独立的 Secondary 或 Tertiary 品牌色。

**The Meaningful Green Rule.** 森林绿用于操作与导航，实时健康使用 success 色并附带文字；“当前选中”和“运行正常”不得共用含糊的视觉暗示。

## Typography

**Body / Heading Font:** `"Segoe UI Variable Text", "Microsoft YaHei UI", "PingFang SC", sans-serif`。

**Data / Mono Font:** `"Cascadia Code", Consolas, "Microsoft YaHei UI", monospace`。

中文 UI 字体贯穿标题、正文、导航与表单，保持熟悉和稳定；本地可用字库按栈回退，不下载外部字体。没有单独的 Display 字体或装饰性大标题。等宽字把账号 ID、进程、配置文本与日志从说明文字中区分出来，中文仍保留字体回退。

### Hierarchy

- **Headline**：页面标题使用 headline 角色；小屏（≤600px）收至 24px，保留原字重、行高与字距。
- **Title**：区域标题使用 title 角色；对话框标题为 20px，沿用同一标题字重与行高。
- **Body**：普通文字使用 body；说明与字段常见 field 角色。正文说明限制在 72ch 以内，配置与技术字符串可在任意位置折行。
- **Label**：状态、计数、表头与辅助说明使用 label 量级；表头加至 500 字重。机器人的名称与插件名称为 14px、600 字重。
- **Control**：按钮使用 control 角色。小按钮为 12px，保持同一 UI 字体与 1.5 行高。
- **Data**：连接信息使用 data；账号标识、插件代码与日志使用 data-small。数字计数、时间与连接信息采用等宽数字；日志行高为 1.8。

**The Reading First Rule.** 标题使用同一中文 UI 字体栈，通过字号与字重建立层级；等宽字体只用于技术标识与日志，不扩展为展示字体。

## Layout

基础间距来自源代码的八档 `space-1 / 2 / 3 / 4 / 5 / 6 / 8 / 10`，对应 frontmatter 中同名数字键。控件内部与图文间距使用小档，字段组和记录使用中档，章节间与并列任务区使用大档。使用这些档位建立节奏，不把每个元素都包进独立卡片。

主工作区采用稳定导航与流式内容；页面内容上限为 1440px。桌面导航宽 224px、主区侧内边距 40px；≤1250px 导航收至 204px、主区内边距 24px；≥1800px 主区内边距扩为 64px。这些是当前管理台外壳的实现尺寸，新表面需先检验内容密度。

- **≤1000px**：模型和设置双栏折为单栏，隐藏重复的工具栏标签，保留可访问名称。
- **≤800px**：导航成为 240px 抽屉；主区取消左偏移、侧边距为 20px。遮罩、菜单按钮与 Escape 关闭配合，打开时锁定背景滚动。
- **≤600px**：主区侧边距为 16px；工具栏换行，按钮（含小按钮）最小高度统一为 40px。目录与插件表格重排为记录行，身份、状态与操作仍在同一记录内；日志消息另起一行。表单脚注与保存按钮分行，三字段组改为单列。

目录桌面列宽、概览页上下区块顺序与各页双栏比例服务于当前任务，不是全局构图规则。扩展界面优先复用对齐、层级与重排行为。

## Elevation & Depth

默认依靠浅绿外壳、白任务面、淡色选中行与细描边表现层级。常规面板、按钮、导航和表格没有投影。原生模态对话框是唯一投影层；遮罩同时用于模态与手机导航，清楚表示背景暂不可操作。

### Shadow Vocabulary

- **模态投影**（`0 12px 48px #1c342a36`）：只用于对话框。
- **遮罩**（`#172b2359`）：覆盖非当前操作内容，不作为卡片底色。

**The Flat Task Surface Rule.** 任务面、列表与导航默认平面分层；不要给常规卡片、按钮或选中行增加投影。模态层的投影用于说明遮挡关系。

## Shapes

控件采用轻微圆角（control，6px），白任务面、表格容器和对话框采用较宽圆角（panel，12px）。主内容区仍以直线与稳定列对齐为骨架；圆角仅帮助识别可操作或独立的边界。

常规描边为 1px；状态使用 6px 圆点，和文字同色，不做胶囊标签。勾选框为 16px 原生控件。图标来自内联线性 SVG，基本笔画为 1.65，常用尺寸为 18–20px；按钮内缩小到 15px。圆点与账号首字标记是有意义的识别元素，不延伸为装饰字形方块。

## Components

### Buttons

主题按钮与次按钮保持相同边界与焦点环，图标和可访问名称说明目标主题；手机仅显示图标，保留完整可访问名称。

按钮直接表达动作，分为主、次、文字三个层级。

- **Primary**：森林绿底、白字、透明描边；用于启动、创建与保存等主动作，悬停加深。
- **Secondary**：白底、正文色、明确的浅绿描边；悬停同时改变底色与描边。
- **Text**：透明底、森林绿字；悬停出现淡绿反馈，适合重启、重载、配置等次级操作。
- 共同采用 control 字体与圆角，内边距为 8px 16px，桌面最小高度为 38px。小按钮内边距为 6px 12px、最小高度为 34px；手机按 Layout 提高触控高度。禁用态使用原生 `disabled`，既减弱视觉，也阻止操作。
- 背景色与文字色过渡为 160ms，缓动为 `cubic-bezier(.16, 1, .3, 1)`。不增加悬浮位移。

### Inputs / Fields

字段是白色、单像素绿灰描边与 control 圆角；标签在字段上方。标准输入内边距为 9px 12px、最小高度为 40px。输入、文本域与选择框共享这套边界，技术文本域使用等宽字体并允许竖向缩放。

键盘焦点为森林绿 2px 轮廓，普通可聚焦元素偏移 3px，字段偏移 2px。搜索框把图标与无独立边框的输入包在一个整体描边中，以 `focus-within` 显示同一焦点环。错误信息在当前对话框或表单上下文内使用红字、淡红底与细描边。

### Cards / Containers

容器承载完整任务：白底、panel 圆角、细分隔线，不加常驻阴影。标准设置面板内边距为 24px，手机改为 20px 16px；表格根据自身内容采用独立的内边距，不把它视为统一卡片模板。原生对话框复用白底与 panel 圆角，并使用唯一模态投影。

### Navigation

浅绿导航底上，链接以图标与中文名称并排。默认导航文字为 nav-ink，当前页改用白底、nav-active 与 600 字重；悬停使用 shell-hover。每项最小高度为 44px、内边距为 12px 16px、项间距为 4px。当前页同时提供 `aria-current="page"`。手机导航按 Layout 转换为抽屉，保留相同链接语言。

### Status

状态是“色点＋文字”的短行，不是彩色胶囊。中性、成功、等待与错误分别由静态文字和颜色共同表达；账号目录与插件行使用同一状态词义。配置启用与实际运行分开，管理服务断连后，缓存信息标注为上次读取、状态改为待更新，依赖服务的控件禁用，成功轮询后恢复。

### Directory Signature

“身份—状态—直接操作”是目录签名。已登录微信列表直接承载状态与启停操作，不再单独展示当前账号状态面板，原生复选框用于批量选择，微信名使用明确字重，wxid 与 PID 采用等宽字，启动／停止动作在记录末端。当前管理对象使用 selected-row 底色；复选框独立表达待启动选择，不能把当前管理误当成已勾选或正在运行。顶部选择账号只准备配置，行内按钮或批量按钮明确触发启动；行内启停不改变当前配置对象。

桌面用表格与对齐列扫描，手机重排为复选框、身份和操作三列；长名称、账号与错误信息可换行。刷新保留仍有效的选择和键盘焦点，页面重载不记忆勾选。账号消失、进程变化或已在运行时禁用旧选择；批量提交反馈逐项显示失败，不掩盖其他账号的结果。

插件沿用目录语言，额外展示插件代码、功能说明与启停／重载／配置；不改造成等权装饰卡片。

### Message Monitor

概览页账号列表下方显示消息监控，替代常用设置快捷入口。时间、会话 ID、发送者与正文形成四列；身份与 ID 保持可选择，正文保留换行。搜索字段和暂停滚动、清空视图按钮直接服务于查看记录，不增加介绍文案。媒体只显示文字类型标记，不放预览、缩略图或媒体播放器。

列表有固定最大可视高度并自动滚至新消息，暂停滚动时保留当前位置。手机将每条记录重排：作者与时间同排，会话 ID 和内容随后；保留全部字段，不横向滚动页面。顶部切换账号时立即清除旧视图，并丢弃旧账号尚未返回的请求。接口暂不可用时只在消息区域反馈，不能阻止账号启停。

### Logs & Feedback

日志使用白底、等宽小字与逐行分隔，时间、级别与消息分列；警告和错误只改变级别文字语义色，消息允许换行。手机把消息移至下一行。提示条用赭色浅底表达服务状态；即时反馈用深绿底白字，错误即时反馈用故障红。保留空状态说明真实前提，不补造记录或指标。

动效仅用于状态切换和导航抽屉：控件反馈为 160ms，抽屉位移为 180ms，均使用现有缓动。`prefers-reduced-motion: reduce` 下禁用过渡与动画并使用自动滚动行为。刷新数据与焦点保留不使用入场动画。

## Do's and Don'ts

界面只保留标题、操作、字段标签和必要反馈；删除重复说明、版本角标与访问范围标语，详细使用说明放在文档。

### Do:

- Do 将机器人或插件的身份、状态与可用操作放在可关联的同一记录中；长名称与 ID 允许换行。
- Do 使用已提取的浅绿导航、白任务面、细线与森林绿控件，按内容层级选择间距。
- Do 让状态同时具备文字与色点；服务断连时显示“待更新”或“状态待更新”，并禁用依赖服务的操作。
- Do 保留键盘焦点环、明确字段标签、原生控件语义与移动端触控高度。
- Do 让新页面沿用字体、控件与目录签名，再根据实际任务安排布局。

### Don't:

- Don't 用装饰性字形方块、英文眉题、柔光主视觉或虚构图表代替产品事实。
- Don't 将“已启用”“运行中”“已连接”混为同一状态，或把上次读取的数据呈现为实时健康。
- Don't 为普通任务面增加悬浮阴影、渐变或无功能的彩色卡片。
- Don't 把概览页的状态区顺序、列宽或双栏比例提升为所有页面的固定模板。
- Don't 在界面或设计样例中放入真实账号、密钥或私有运行数据。
