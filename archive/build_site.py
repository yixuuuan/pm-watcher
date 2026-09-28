#!/usr/bin/env python3
"""
把 pm-watcher 构建成可永久托管的静态「历史回放」站点。

  python3 archive/build_site.py [--db history_railway.db] [--out site]

输出目录（默认 ./site）可直接部署到 Cloudflare Pages / 任意静态托管：
  index.html              原看板（仅做最小改动：接入回放数据层 + 时间轴）
  parallel-universe.html  平行宇宙冠军机（同样接入回放）
  analysis.html           2026 世界杯全程数据分析
  archive.js              回放数据层：在浏览器里按任意时刻复现原 /api/* 接口
  data/archive.json       全部定价变动序列 + 赛果/收盘线 + 访问量
  data/news.json          新闻存档（archive/news_archive.json，若存在）
  data/analysis.json      分析页用的预计算结果
  flags/ fonts/ fonts-subset/ qrcode.js

设计原则：数据口径全部复用 pm_watcher/history.py 的原函数（收盘线、冠军序列等），
不重新实现；前端逻辑不改，只把"现在"换成回放时刻。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
PKG = ROOT / "pm_watcher"

ICON = ('<link rel="icon" type="image/svg+xml" href="favicon.svg?v=3">\n'
        '<link rel="icon" type="image/png" sizes="32x32" href="favicon-32.png?v=3">\n'
        '<link rel="shortcut icon" href="favicon.ico?v=3">\n'
        '<link rel="apple-touch-icon" href="apple-touch-icon.png?v=3">\n')
PLATFORMS = ["polymarket", "kalshi", "42", "manifold", "predict"]   # 与 ALL_PLATFORMS 同序
FINISH_LAG = 2 * 3600 + 15 * 60     # 开球后多久视为「已结束并入库」（赛果源每 5 分钟同步）
KO_ROUNDS = ("R32", "R16", "QF", "SF", "3P", "FIN")


def load_history(db_path: Path):
    """以指定数据库载入原项目的 history / names 模块（绕开包 __init__ 的平台 client 导入）。"""
    os.environ["PMW_DB"] = str(db_path)
    pkg = types.ModuleType("pm_watcher")
    pkg.__path__ = [str(PKG)]
    sys.modules["pm_watcher"] = pkg
    import pm_watcher.history as H   # noqa: E402
    import pm_watcher.names as N     # noqa: E402
    return H, N


# ─────────────────────────── 定价序列 ───────────────────────────
def export_series(H, N):
    c = H._db()
    t_min = c.execute("SELECT MIN(ts) FROM snap").fetchone()[0]
    t_max = c.execute("SELECT MAX(ts) FROM snap").fetchone()[0]
    out = {}
    for scope in ("champion", "group", "match"):
        merged: dict[tuple[str, str], list] = {}
        for key, plat, ts, price in c.execute(
                "SELECT key, platform, ts, price FROM snap WHERE scope=? ORDER BY ts", (scope,)):
            if scope == "match":
                k = key
            else:
                k = N.canonical_country(H._ckey(key)) or key
            merged.setdefault((k, plat), []).append((ts, price))
        keys = sorted({k for k, _ in merged})
        kidx = {k: i for i, k in enumerate(keys)}
        plats = PLATFORMS + sorted({p for _, p in merged if p not in PLATFORMS})
        pidx = {p: i for i, p in enumerate(plats)}
        ser = []
        for (k, p), pts in sorted(merged.items(), key=lambda x: (kidx[x[0][0]], pidx[x[0][1]])):
            pts.sort()
            tsd, vals, prev_t, prev_v = [], [], t_min, None
            for ts, price in pts:
                # 价格本身只有 3~4 位小数，但库里的 double 可能带末位噪声（如 0.24600000000000002）。
                # 原代码的四舍五入会受这点噪声影响，所以：恰好等于 n/10000 的存整数，其余原样存浮点，保证逐位一致。
                vi = int(round(price * 10000))
                v = vi if vi / 10000 == price else price
                if ts == prev_t and tsd:            # 同一时刻重复 → 以后写为准
                    vals[-1] = v
                    continue
                tsd.append(ts - prev_t)
                vals.append(v)
                prev_t, prev_v = ts, v
            ser.append([kidx[k], pidx[p], tsd, vals])
        out[scope] = {"keys": keys, "plats": plats, "series": ser}
    return out, t_min, t_max


# ─────────────────────────── 赛果 / 收盘线 / 淘汰赛对阵 ───────────────────────────
def export_results(H, N):
    cano = N.canonical_country
    rows = H._db().execute(
        """SELECT match_id, home, away, home_score, away_score, outcome,
                  kickoff_ts, status, grp FROM result ORDER BY kickoff_ts""").fetchall()
    games = []
    for mid, home, away, hs, as_, outcome, ko, status, grp in rows:
        h, a = cano(home or ""), cano(away or "")
        o = outcome
        if o and o != "Draw":
            o = cano(o)
        res = None
        if hs is not None and o:
            res = "D" if o == "Draw" else ("A" if o == h else "B")
        cl = H.closing_line(h, a, ko) if (h and a) else {}
        odds = {p: [d.get(h), d.get("Draw"), d.get(a)] for p, d in cl.items()}
        games.append({"id": mid, "home": h, "away": a, "sa": hs, "sb": as_,
                      "result": res, "kickoff": ko, "grp": grp, "status": status,
                      "odds": odds})
    # 淘汰赛每一边「对阵确定」的时刻：该队上一场比赛结束入库之时
    for g in games:
        if g["grp"] not in KO_ROUNDS:
            continue
        for side in ("home", "away"):
            team = g[side]
            prev = [x["kickoff"] for x in games
                    if x["kickoff"] < g["kickoff"] and team in (x["home"], x["away"])]
            g["known_" + side] = (max(prev) + FINISH_LAG) if prev else g["kickoff"] - 86400
    return games


def export_visits(H):
    rows = H._db().execute("SELECT day, hits, uniq FROM visit_day ORDER BY day").fetchall()
    out = []
    for day, hits, uniq in rows:
        out.append([day, hits, uniq])
    return out


def export_news():
    f = HERE / "news_archive.json"
    if not f.exists():
        return []
    items = json.loads(f.read_text(encoding="utf-8"))
    out, seen = [], set()
    for x in sorted(items, key=lambda x: int(x.get("ts") or x.get("first_seen") or 0)):
        if not x.get("title") or not x.get("url"):
            continue
        u = x["url"].lower()
        # BBC 综合体育频道会混进网球/板球等（「France」「England」被当成球队）→ 只留足球
        if "bbc." in u and "/sport/" in u and "/sport/football" not in u:
            continue
        if x["title"].startswith("Copy of "):       # ESPN 源里的草稿标题
            continue
        key = re.sub(r"^https?://(www\.)?", "", x["url"].split("?")[0].split("#")[0]).rstrip("/").lower()
        if key in seen:                    # 同一篇报道出现在多个频道/带不同跟踪参数 → 只留一条
            continue
        seen.add(key)
        out.append([int(x.get("ts") or x.get("first_seen") or 0), x.get("source", ""),
                    x["title"], x["url"], x.get("teams") or [], bool(x.get("wc"))])
    out.sort(key=lambda r: r[0])
    return out


# ─────────────────────────── 前端补丁 ───────────────────────────
def patch_dashboard(html: str, site_url: str) -> str:
    n0 = html.count("Date.now()")
    host = site_url.split("//", 1)[-1].rstrip("/")
    old = 'SITE_URL="https://pm-watchers.up.railway.app/", SITE_HOST="pm-watchers.up.railway.app"'
    assert old in html, "SITE_URL 结构变了"
    html = html.replace(old, f'SITE_URL="{site_url}", SITE_HOST="{host}"')
    html = html.replace("Date.now()", "PMW_NOW()")
    # 回放模式下，异动事件由历史数据推算，而不是浏览器本地累计
    html = html.replace("function logMoves(){",
                        "function logMoves(){ if(window.PMW){ EVENTS=PMW.events(); return; }", 1)
    # 顶部状态行：LIVE · 更新于 … → 回放 · 具体时刻
    old_meta = '`${DATA.live?"LIVE":"MOCK"} · ${L("updated")} ${upd} · ${L("every")} ${DATA.interval}s ${L("refresh")}`'
    assert old_meta in html, "meta 行结构变了"
    html = html.replace(old_meta, "(window.PMW?PMW.metaText():" + old_meta + ")")
    # 页脚：加「请我喝杯咖啡」
    old_foot = '<a href="mailto:isabel.yx.wang@gmail.com">isabel.yx.wang@gmail.com</a></div>`;'
    assert old_foot in html, "页脚结构变了"
    html = html.replace(old_foot, '<a href="mailto:isabel.yx.wang@gmail.com">isabel.yx.wang@gmail.com</a>'
                        ' · <a href="https://buymeacoffee.com/yixuuuan" target="_blank" rel="noopener">🌰 ${LANG==="zh"?"请我喝杯咖啡":"Buy me a coffee"}</a></div>`;', 1)
    # 尽早载入回放层（须在主脚本之前）
    inj = '<link rel="stylesheet" href="archive.css">\n<script src="archive.js"></script>\n' + ICON
    html = html.replace("</head>", inj + "</head>", 1)
    assert n0 == 3, f"Date.now() 出现次数={n0}，与预期不符，请复核"
    return html


def patch_pu(html: str, site_url: str) -> str:
    html = html.replace("Date.now()", "PMW_NOW()")
    html = html.replace("https://pm-watchers.up.railway.app/parallel-universe.html", site_url + "parallel-universe.html")
    inj = '<script src="archive.js"></script>\n' + ICON
    if "</head>" in html:
        html = html.replace("</head>", inj + "</head>", 1)
    else:
        html = inj + html
    return html


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(ROOT / "site"))
    ap.add_argument("--site-url", default="https://pmwatcher.wy-x.com/")
    args = ap.parse_args()
    db = Path(args.db) if args.db else next(
        (p for p in (ROOT / "history_railway.db", ROOT / "history.db") if p.exists()), None)
    if not db or not db.exists():
        sys.exit("找不到数据库，请用 --db 指定")
    out = Path(args.out)
    print(f"数据库: {db}  →  输出: {out}")

    H, N = load_history(db)
    series, t_min, t_max = export_series(H, N)
    games = export_results(H, N)
    visits = export_visits(H)
    news = export_news()
    c = H._db()
    wc48 = sorted({N.canonical_country(H._ckey(k)) for (k,) in c.execute(
        "SELECT DISTINCT key FROM snap WHERE scope='champion'")
        if N.is_wc_team(N.canonical_country(H._ckey(k)))})

    if out.exists():
        shutil.rmtree(out)
    (out / "data").mkdir(parents=True)

    archive = {"v": 1, "built": int(time.time()), "t0": t_min, "t1": t_max,
               "platforms": PLATFORMS, "finishLag": FINISH_LAG, "wc48": wc48,
               "series": series, "games": games, "visits": visits}
    (out / "data" / "archive.json").write_text(
        json.dumps(archive, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    (out / "data" / "news.json").write_text(
        json.dumps(news, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # 分析页数据
    try:
        sys.path.insert(0, str(HERE))
        import analyze   # noqa: E402
        ana = analyze.build(H, N, series, games, archive)
        (out / "data" / "analysis.json").write_text(
            json.dumps(ana, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    except ImportError:
        print("（未找到 analyze.py，跳过分析页数据）")

    # 页面
    (out / "index.html").write_text(
        patch_dashboard((PKG / "dashboard.html").read_text(encoding="utf-8"), args.site_url), encoding="utf-8")
    (out / "parallel-universe.html").write_text(
        patch_pu((PKG / "parallel-universe.html").read_text(encoding="utf-8"), args.site_url), encoding="utf-8")
    for f in ("archive.js", "archive.css", "analysis.html", "favicon.svg", "favicon-32.png", "favicon.ico", "apple-touch-icon.png"):
        src = HERE / "web" / f
        if src.exists():
            shutil.copy(src, out / f)
    # 进球地图的世界底图：优先用仓库里的本地副本（永久可用），没有就尝试下载一份存进仓库；都失败时页面会退回 CDN
    vend = HERE / "web" / "vendor" / "countries-110m.json"
    if not vend.exists():
        import subprocess
        vend.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["curl", "-sSfL", "--max-time", "30", "-o", str(vend),
                        "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json"], check=False)
        if vend.exists() and vend.stat().st_size < 50000:
            vend.unlink()
    if vend.exists():
        (out / "vendor").mkdir(exist_ok=True)
        shutil.copy(vend, out / "vendor" / vend.name)
    else:
        print("（未能下载世界底图，分析页将从 CDN 加载）")
    for d in ("flags", "fonts", "fonts-subset"):
        if (PKG / d).exists():
            shutil.copytree(PKG / d, out / d)
    if (PKG / "qrcode.js").exists():
        shutil.copy(PKG / "qrcode.js", out / "qrcode.js")
    (out / "_headers").write_text(
        "/data/*\n  Cache-Control: public, max-age=3600\n"
        "/flags/*\n  Cache-Control: public, max-age=604800\n"
        "/fonts/*\n  Cache-Control: public, max-age=604800\n"
        "/fonts-subset/*\n  Cache-Control: public, max-age=604800\n", encoding="utf-8")

    sz = lambda p: f"{p.stat().st_size / 1048576:.2f} MB"
    print("完成：")
    for p in sorted((out / "data").iterdir()):
        print(f"  data/{p.name:16s} {sz(p)}")
    print(f"  序列: champion {len(series['champion']['series'])} · group {len(series['group']['series'])}"
          f" · match {len(series['match']['series'])}；赛果 {len(games)} 场；新闻 {len(news)} 条")


if __name__ == "__main__":
    main()
