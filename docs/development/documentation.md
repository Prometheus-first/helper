# 维护这份文档

文档源文件在仓库的 `docs/` 中，使用 VitePress 生成静态页面，GitHub Pages 发布。

## 本地预览

在项目根目录运行，使用 Node.js 22 或更新版本：

```powershell
npm ci
npm run docs:dev
```

打开终端显示的本地地址，修改 Markdown 即可看到更新。文档工具与 Python 机器人入口独立，不需要启动微信。

## 修改内容

| 位置 | 内容 |
| --- | --- |
| `docs/index.md` | 首页介绍和功能入口 |
| `docs/guide/` | 安装、配置、群功能、常见问题 |
| `docs/messages/` | 文本、图片、表情、语音接口 |
| `docs/development/` | 插件开发和文档维护 |
| `docs/.vitepress/config.mts` | 导航、侧栏、搜索、发布路径 |
| `docs/.vitepress/theme/` | 页面主题和颜色 |

新增页面后，在 `config.mts` 中补上侧栏入口。内部链接使用 Markdown 相对链接，例如 `[配置](../guide/configuration.md)`。

## 检查正式构建

```powershell
npm run docs:build
npm run docs:preview
```

构建会检查失效的文档链接；预览使用正式构建产物。`docs/.vitepress/dist` 和 `node_modules` 都不会提交。

## 推送后自动发布

仓库的 `.github/workflows/docs.yml` 会在 `main` 上的文档或构建配置更新时运行：

1. 安装锁定的 npm 依赖。
2. 构建 VitePress 页面。
3. 将生成的静态文件部署到 GitHub Pages。

也可在仓库 **Actions → Deploy documentation → Run workflow** 手动触发。Pull Request 会构建检查，不发布正式站点。

仓库 **Settings → Pages → Source** 使用 **GitHub Actions**。默认发布路径为 `/helper/`；仓库改名或改用自定义域名时，需同步修改 VitePress 的 `base` 和图标路径。

文档只使用通用示例，不把本地账号、群 ID、密钥、聊天日志或真实媒体放入 `docs/`。
