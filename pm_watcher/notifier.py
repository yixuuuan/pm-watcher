"""
Telegram 推送：只发消息，不需要整套 bot 框架——直接打 Bot API 的 sendMessage。
token 和 chat_id 从 .env 读，绝不写进代码。
"""
from __future__ import annotations

import httpx


async def send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    if not token or not chat_id:
        return False, "缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(url, json={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            })
        if r.status_code < 300:
            return True, "ok"
        # 常见错误：401 token 错；400 chat not found（你还没给 bot 发过 /start）
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, str(e)
