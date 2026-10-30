"""复制此目录为一个不以下划线开头的新目录，即可添加功能。"""


class Plugin:
    def on_load(self, ctx):
        # 可从 ctx.config 读取此插件的配置。
        ctx.logger.info("示例插件已加载")

    def on_message(self, message, ctx):
        # 修改指令和回复内容，即可实现你自己的功能。
        if message.text.strip() == "/hello":
            ctx.reply(message, "你好，插件正在运行 🌸")

    def scheduled_greeting(self, params, ctx):
        # 在网页创建定时任务后，按设置的时间执行这个业务函数。
        if ctx.send_text(params['chat_id'], params['text']) is False:
            raise RuntimeError('发送未完成')

    def on_unload(self, ctx):
        # 在此停止自己创建的线程、定时器并关闭文件或网络连接。
        ctx.logger.info("示例插件已卸载")
