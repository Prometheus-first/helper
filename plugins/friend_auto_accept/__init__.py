"""仅处理好友申请事件；凭据由后端保存在当前账号内存中。"""


class Plugin:
    def on_load(self, ctx):
        self.ctx = ctx
        keywords = ctx.config.get("verification_keywords", [])
        if (not isinstance(keywords, list) or len(keywords) > 32 or
                any(not isinstance(value, str) or not value.strip() or len(value) > 128
                    for value in keywords)):
            raise ValueError("verification_keywords 应为最多 32 个非空关键词的列表")
        self.keywords = tuple(value.strip() for value in keywords)

    def on_message(self, message):
        if message.type != 37:
            return
        request = message.data.get("friend_request", {})
        request_id = request.get("request_id", "")
        if not request_id or request.get("status") != "pending":
            return
        verification = request.get("verification", "")
        if self.keywords and not any(word in verification for word in self.keywords):
            return
        try:
            accepted = self.ctx.accept_friend(request_id, expected_session=message.session_id)
        except Exception:
            # 原生错误或申请对象可能包含凭据；不把原始异常写入日志。
            self.ctx.logger.warning("好友申请自动通过失败：申请ID=%s", request_id)
            return
        if accepted:
            self.ctx.logger.info("已自动通过好友申请：申请ID=%s", request_id)
        else:
            self.ctx.logger.warning("好友申请未通过：申请ID=%s", request_id)
