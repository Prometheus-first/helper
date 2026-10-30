"""管理台共用的错误类型与日志脱敏，不依赖业务控制器。"""
import re

SECRET = re.compile(r'(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')


def redact_full(text):
    return SECRET.sub('[密钥已隐藏]', str(text))


def redact(text):
    return redact_full(text)[:8000]


class PanelError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status
