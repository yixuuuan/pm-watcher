"""
命令行 watcher：跨平台赔率并排 + 价差。
- --interval 轮询；单平台价格异动在控制台打 ⚡
- --notify 推 Telegram：启动快照 + 【价差告警】(两家真实价差越过 --spread-alert 时，边沿触发，不刷屏)

用法：
  python -m pm_watcher.watch --query "World Cup" --board --live
  python -m pm_watcher.watch --query "World Cup" --board --live --interval 60 --notify --spread-alert 3
  # 只看你信的两家、价差更干净：
  python -m pm_watcher.watch --query "World Cup" --board --live --platforms polymarket,kalshi --interval 60 --notify --spread-alert 1.5
"""
from __future__ import annotations

import argparse
import asyncio

from . import make_client, ALL_PLATFORMS
from .aggregator import fetch_all, compare_outcome, spread, build_board
from . import config
from .notifier import send_telegram


def _fmt(p) -> str:
    return f"{p*100:5.1f}%" if isinstance(p, float) else "  —  "


async def run_board(clients, query, platforms, last: dict, threshold: float,
                    spread_alert: float, alert_state: set, first: bool):
    """
    打印榜单；返回 (last, summary_lines, spread_alert_lines)。
    spread_alert_lines：本轮【新越过】spread_alert 的行（边沿触发；first 轮只填充状态不告警）。
    """
    results, errors = await fetch_all(clients, query, limit=64)
    board = build_board(results)

    print(f"\n=== {query} · 跨平台赔率榜 ===")
    print("  " + "结果".ljust(20) + "".join(p[:9].ljust(10) for p in platforms) + "价差")

    rows = sorted(board.items(), key=lambda kv: max(kv[1].values()), reverse=True)
    summary: list[str] = []
    spread_alerts: list[str] = []

    for label, by_plat in rows:
        cells = ""
        for p in platforms:
            v = by_plat.get(p)
            tag = ""
            if v is not None:
                key = f"{label}@{p}"
                if key in last and abs(v - last[key]) >= threshold:
                    tag = "⚡"
                last[key] = v
            cells += (f"{_fmt(v)}{tag}").ljust(10)

        present = {p: by_plat[p] for p in platforms if by_plat.get(p) is not None}
        sp_pt = None
        if len(present) >= 2:
            hi = max(present, key=present.get)
            lo = min(present, key=present.get)
            sp_pt = (present[hi] - present[lo]) * 100
        sp_str = f"{sp_pt:.1f}pt" if sp_pt is not None else "—"
        print("  " + label[:20].ljust(20) + cells + sp_str)

        if len(summary) < 8 and present:
            cols = " | ".join(f"{p}:{present[p]*100:.1f}%" for p in present)
            summary.append(f"{label}  {cols}  价差{sp_str}")

        # 价差告警：边沿触发
        if spread_alert > 0 and sp_pt is not None:
            crossed = sp_pt >= spread_alert
            was = label in alert_state
            if crossed and not was:
                alert_state.add(label)
                if not first:  # 启动轮只记录现状，不刷屏
                    spread_alerts.append(
                        f"{label}  价差 {sp_pt:.1f}pt  ↑高 {hi} {present[hi]*100:.1f}%  ↓低 {lo} {present[lo]*100:.1f}%")
            elif not crossed and was:
                alert_state.discard(label)  # 回落，下次可再触发

    for p, e in errors.items():
        print(f"  ⚠️ {p}: {e[:60]}")
    return last, summary, spread_alerts


async def run_once(clients, query, outcome, last: dict, threshold: float):
    results, errors = await fetch_all(clients, query)
    quotes = compare_outcome(results, outcome)
    print(f"\n=== {query} → “{outcome}” ===")
    for q in quotes:
        delta = ""
        if q.price is not None and q.platform in last and last[q.platform] is not None:
            d = q.price - last[q.platform]
            if abs(d) >= threshold:
                delta = f"  ⚡{'+' if d>=0 else ''}{d*100:.1f}pt"
        title = (q.market_title[:42] + "…") if len(q.market_title) > 43 else q.market_title
        print(f"  {q.platform:11} {_fmt(q.price)}  {title}{delta}")
        last[q.platform] = q.price if q.price is not None else last.get(q.platform)
    sp = spread(quotes)
    if sp is not None:
        print(f"  ── 平台间最大价差: {sp*100:.1f} 个百分点")
    return last


async def main_async(args):
    platforms = [p.strip() for p in args.platforms.split(",") if p.strip()]
    clients = [make_client(p, live=args.live) for p in platforms]
    mode = "LIVE(只读)" if args.live else "MOCK"
    extras = []
    if args.notify:
        extras.append("📨Telegram")
    if args.notify and args.spread_alert > 0:
        extras.append(f"价差告警≥{args.spread_alert}pt")
    print(f"平台: {', '.join(platforms)} | 模式: {mode}" + (" | " + " ".join(extras) if extras else ""))

    notify_ok = args.notify and config.TG_TOKEN and config.TG_CHAT_ID
    if args.notify and not notify_ok:
        print("⚠️ 未在 .env 配置 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，推送不会发出。")

    last: dict = {}
    alert_state: set = set()
    first = True
    try:
        while True:
            if args.board:
                last, summary, spread_alerts = await run_board(
                    clients, args.query, platforms, last, args.threshold,
                    args.spread_alert, alert_state, first)
                if notify_ok and first:
                    ok, msg = await send_telegram(
                        config.TG_TOKEN, config.TG_CHAT_ID,
                        f"📊 {args.query} 监控已启动（{mode}）\n" + "\n".join(summary))
                    print("  📨 启动推送:", "已发" if ok else msg)
                elif notify_ok and spread_alerts:
                    ok, msg = await send_telegram(
                        config.TG_TOKEN, config.TG_CHAT_ID,
                        f"📐 {args.query} 跨平台价差告警（≥{args.spread_alert}pt）\n" + "\n".join(spread_alerts))
                    print("  📨 价差告警:", "已发" if ok else msg)
            else:
                last = await run_once(clients, args.query, args.outcome, last, args.threshold)
            first = False
            if args.interval <= 0:
                break
            await asyncio.sleep(args.interval)
    finally:
        for c in clients:
            await c.close()


def main():
    ap = argparse.ArgumentParser(description="多平台预测市场只读比价 watcher")
    ap.add_argument("--query", default="World Cup", help="市场关键词")
    ap.add_argument("--outcome", default="Spain", help="要比较的结果名（单结果模式）")
    ap.add_argument("--board", action="store_true", help="排行榜模式（忽略 --outcome）")
    ap.add_argument("--platforms", default=",".join(ALL_PLATFORMS),
                    help=f"逗号分隔，默认全部: {','.join(ALL_PLATFORMS)}")
    ap.add_argument("--live", action="store_true", help="用真实公开只读端点（否则 mock）")
    ap.add_argument("--interval", type=int, default=0, help="轮询秒数；0=只跑一次")
    ap.add_argument("--threshold", type=float, default=0.02, help="单平台异动 ⚡ 阈值（0~1）")
    ap.add_argument("--spread-alert", dest="spread_alert", type=float, default=3.0,
                    help="跨平台价差告警阈值（百分点）；0=关闭。默认 3")
    ap.add_argument("--notify", action="store_true", help="把启动快照与价差告警推到 Telegram")
    args = ap.parse_args()
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()
