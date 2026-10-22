import { defineConfig } from 'vitepress'

export default defineConfig({
  lang: 'zh-CN',
  title: 'helper',
  description: '微信机器人 Python 插件框架：安装配置、消息收发与插件开发文档。',
  base: '/helper/',
  head: [['link', { rel: 'icon', type: 'image/svg+xml', href: '/helper/logo.svg' }]],
  lastUpdated: true,
  themeConfig: {
    logo: '/logo.svg',
    siteTitle: 'helper',
    nav: [
      { text: '开始使用', link: '/guide/quick-start' },
      { text: '消息收发', link: '/messages/text' },
      { text: '插件开发', link: '/development/plugins' },
      { text: '常见问题', link: '/guide/faq' }
    ],
    sidebar: [
      { text: '开始使用', items: [
        { text: '项目介绍', link: '/' },
        { text: '安装与启动', link: '/guide/quick-start' },
        { text: '配置参考', link: '/guide/configuration' },
        { text: '账号与群聊', link: '/guide/account-and-groups' },
        { text: '使用许可', link: '/guide/license' }
      ] },
      { text: '使用框架', items: [
        { text: '网页管理台', link: '/guide/web-panel' },
        { text: '控制台命令', link: '/guide/commands' },
        { text: '群聊功能', link: '/guide/features' },
        { text: '好友申请与群管理', link: '/guide/friends-and-groups' },
        { text: '文本与真实艾特', link: '/messages/text' },
        { text: '图片收发', link: '/messages/images' },
        { text: '文件收发', link: '/messages/files' },
        { text: '合并转发解析', link: '/messages/forwarded' },
        { text: '防撤回与补发', link: '/messages/recall' },
        { text: '表情提取与发送', link: '/messages/emoticons' },
        { text: '语音合成与发送', link: '/messages/voice' }
      ] },
      { text: '开发与维护', items: [
        { text: '插件开发', link: '/development/plugins' },
        { text: '常见问题', link: '/guide/faq' },
        { text: '维护这份文档', link: '/development/documentation' }
      ] }
    ],
    socialLinks: [{ icon: 'github', link: 'https://github.com/Prometheus-first/helper' }],
    search: {
      provider: 'local',
      options: { locales: { root: { translations: {
        button: { buttonText: '搜索文档', buttonAriaLabel: '搜索文档' },
        modal: {
          noResultsText: '没有找到相关内容', resetButtonTitle: '清空搜索',
          displayDetails: '显示详细内容',
          footer: { selectText: '选择', navigateText: '切换', closeText: '关闭' }
        }
      } } } }
    },
    outline: { level: [2, 3], label: '本页目录' },
    docFooter: { prev: '上一页', next: '下一页' },
    sidebarMenuLabel: '文档目录',
    returnToTopLabel: '回到顶部',
    darkModeSwitchLabel: '外观',
    lightModeSwitchTitle: '切换为浅色',
    darkModeSwitchTitle: '切换为深色',
    lastUpdated: { text: '最后更新', formatOptions: { dateStyle: 'medium' } },
    editLink: { pattern: 'https://github.com/Prometheus-first/helper/edit/main/docs/:path', text: '在 GitHub 上编辑此页' },
    footer: { message: 'Windows x64 · 微信 4.1.15.13', copyright: 'helper 项目文档' }
  }
})
