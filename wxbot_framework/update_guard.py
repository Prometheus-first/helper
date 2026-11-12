"""在启动微信或连接机器人前执行已启用的更新保护。"""
import logging
from pathlib import Path


def check_update_protection(root, config=None):
    root = Path(root)
    if config is None:
        path = root / 'bot_config.json'
        if not path.is_file():
            return None
        from .config import ConfigStore
        config = ConfigStore(path).load()
    options = config.get('plugins', {}).get('wechat_update_guard', {})
    if options.get('enabled') is not True:
        return None
    from plugins.wechat_update_guard.guard import protect
    result = protect(root)
    logging.getLogger('UpdateGuard').info('启动前更新保护：%s', result)
    return result
