"""
读取 Telegram 凭据。优先用 python-dotenv（若已安装），否则用内置的极简 .env 解析——
不依赖任何第三方包，避免"没装 dotenv 就静默读不到 .env"的坑。
在当前目录及其上两层查找 .env。
"""
from __future__ import annotations

import os
from pathlib import Path


def _load_env_file() -> str | None:
    here = Path.cwd()
    for d in [here, *list(here.parents)[:2]]:
        f = d / ".env"
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8")
            except Exception:
                continue
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if " #" in v:                       # 去掉行内注释（前有空格的 #）
                    v = v.split(" #", 1)[0].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":  # 去掉成对引号
                    v = v[1:-1]
                os.environ.setdefault(k, v)          # 真实环境变量优先，不覆盖
            return str(f)
    return None


try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

ENV_PATH = _load_env_file()  # 找到的 .env 路径（没找到为 None），便于排错

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
