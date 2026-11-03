"""只读检查 Linux 微信进程；不启动、登录或注入微信。"""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wxbot_framework.linux_wechat import discover_wechat

if sys.platform != "linux":
    raise SystemExit("该检查工具只支持 Linux")
print(json.dumps({"processes": discover_wechat(),
                  "message_bridge": "未连接；实验桥接需通过验证入口显式加载"}, ensure_ascii=False, indent=2))
