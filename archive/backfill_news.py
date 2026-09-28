#!/usr/bin/env python3
"""
补回 2026 世界杯期间的新闻（线上版只把新闻放在内存里，没有落盘）。

两条来源：
  1) 互联网档案馆 Wayback Machine 存下的 RSS / 懂球帝接口快照
     —— 和线上完全同源（BBC / Guardian / ESPN / Sky / 懂球帝），
        用 pm_watcher/news.py 里原来的解析与过滤函数重新解析。
  2) The Guardian 开放内容接口（按天检索历史文章），给 Guardian 兜底补全。

输出：archive/news_archive.json
  每条 {source,title,url,ts,teams,wc,first_seen}
  ts         = 新闻发布时间（取不到时用 first_seen）
  first_seen = 最早被抓到的快照时间

用法（在仓库根目录，Mac 终端）：
  export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897   # 走 Clash
  python3 archive/backfill_news.py

可反复运行：抓过的快照缓存在 archive/.news_cache/，中断后重跑会跳过已完成部分。
可选：export GUARDIAN_KEY=你的key（默认用公开的 test key，速度较慢但可用）
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".news_cache"
OUT = HERE / "news_archive.json"

START = "20260601"          # 抓取窗口（UTC 日期）
END = "20260806"
G_FROM = dt.date(2026, 6, 1)
G_TO = dt.date(2026, 7, 21)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 与 news.py 相同的源，外加几个同媒体的世界杯专题源（提高档案馆命中率）
RSS_FEEDS = [
    ("BBC", "feeds.bbci.co.uk/sport/football/rss.xml"),
    ("BBC", "feeds.bbci.co.uk/sport/football/world-cup/rss.xml"),
    ("Guardian", "www.theguardian.com/football/rss"),
    ("Guardian", "www.theguardian.com/football/world-cup-2026/rss"),
    ("Guardian", "www.theguardian.com/football/world-cup-football/rss"),
    ("ESPN", "www.espn.com/espn/rss/soccer/news"),
    ("Sky", "www.skysports.com/rss/12040"),
]
DQD_FEEDS = ["api.dongqiudi.com/app/tabs/web/1.json"]


# ---------- 载入原项目的 news.py（绕开包 __init__，避免导入各平台 client） ----------
def load_news_module():
    pkg = types.ModuleType("pm_watcher")
    pkg.__path__ = [str(ROOT / "pm_watcher")]
    sys.modules.setdefault("pm_watcher", pkg)
    try:                                   # news.py 顶部 import httpx；本脚本只用其解析函数，
        import httpx  # noqa: F401         # 没装 httpx 时放一个空壳，免得要求额外安装
    except ImportError:
        sys.modules["httpx"] = types.ModuleType("httpx")
    spec = importlib.util.spec_from_file_location("pm_watcher.news", ROOT / "pm_watcher" / "news.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 网络 ----------
# 优先用系统 curl：macOS 上 python.org 版 Python 常缺 SSL 证书（CERTIFICATE_VERIFY_FAILED），
# curl 用系统证书；两者都会自动使用 https_proxy / http_proxy 环境变量。
import shutil
import subprocess

CURL = shutil.which("curl")
ERRORS: dict[str, str] = {}          # host -> 首个错误，用于打印原因


def _note(url: str, msg: str):
    host = urllib.parse.urlparse(url).netloc
    if host not in ERRORS:
        ERRORS[host] = msg
        print(f"   ⚠ {host}: {msg}")


def _get_once(url: str, timeout: int):
    """返回 (状态码, 内容)；网络层失败时状态码为 0。"""
    if CURL:
        r = subprocess.run([CURL, "-sS", "-L", "--max-time", str(timeout), "-A", UA,
                            "-o", "-", "-w", "\n__HTTP__%{http_code}", url],
                           capture_output=True)
        out = r.stdout
        k = out.rfind(b"\n__HTTP__")
        if r.returncode != 0 or k < 0:
            return 0, (r.stderr.decode("utf-8", "replace").strip() or f"curl 退出码 {r.returncode}")
        return int(out[k + 9:] or 0), out[:k]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def http_get(url: str, tries: int = 4, timeout: int = 40) -> bytes | None:
    for i in range(tries):
        code, body = _get_once(url, timeout)
        if code == 200:
            return body
        if code in (404, 403, 400, 401):
            _note(url, f"HTTP {code}")
            return None
        _note(url, f"HTTP {code}" if code else str(body)[:160])
        time.sleep(6 * (i + 1) if code in (429, 503, 0) else 2 * (i + 1))
    return None


def check():
    """自检：几秒钟测出网络/代理/证书是否可用。"""
    tests = [
        ("档案馆 CDX", "https://web.archive.org/cdx/search/cdx?url=feeds.bbci.co.uk/sport/football/rss.xml&from=20260615&to=20260616&output=json&limit=3"),
        ("Guardian API", "https://content.guardianapis.com/search?section=football&from-date=2026-06-20&to-date=2026-06-20&page-size=3&api-key=" + os.environ.get("GUARDIAN_KEY", "test")),
    ]
    print("传输方式:", "curl" if CURL else "python urllib",
          "| 代理:", os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "未设置")
    for name, url in tests:
        code, body = _get_once(url, 30)
        if code == 200:
            print(f"  ✅ {name}: 通 （返回 {len(body)} 字节） 预览: {body[:160]!r}")
        else:
            print(f"  ❌ {name}: " + (f"HTTP {code}" if code else str(body)[:300]))


def cached_get(url: str) -> bytes | None:
    CACHE.mkdir(exist_ok=True)
    f = CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".bin")
    if f.exists():
        return f.read_bytes()
    body = http_get(url)
    if body is not None:
        f.write_bytes(body)
    return body


def wb_ts_to_epoch(ts14: str) -> int:
    return int(dt.datetime.strptime(ts14, "%Y%m%d%H%M%S")
               .replace(tzinfo=dt.timezone.utc).timestamp())


def cdx_snapshots(url: str) -> list[tuple[str, str]]:
    """返回 [(timestamp14, original_url)]，已去掉内容相同的相邻快照。"""
    q = urllib.parse.urlencode({
        "url": url, "from": START, "to": END, "output": "json",
        "filter": "statuscode:200", "collapse": "digest"})
    body = cached_get("https://web.archive.org/cdx/search/cdx?" + q)
    if not body:
        return []
    try:
        rows = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return []
    return [(r[1], r[2]) for r in rows[1:] if len(r) > 2]


# ---------- 汇总 ----------
ITEMS: dict[str, dict] = {}


def add(item: dict, seen_at: int):
    url = (item.get("url") or "").strip()
    if not url or not item.get("title"):
        return
    cur = ITEMS.get(url)
    if cur is None:
        it = dict(item)
        it["first_seen"] = seen_at
        if not it.get("ts"):
            it["ts"] = seen_at
        ITEMS[url] = it
    else:
        cur["first_seen"] = min(cur["first_seen"], seen_at)
        if not cur.get("ts") and item.get("ts"):
            cur["ts"] = item["ts"]
        for t in item.get("teams") or []:
            if t not in cur["teams"]:
                cur["teams"].append(t)
        cur["wc"] = cur.get("wc") or item.get("wc")


def run_wayback(news):
    for source, feed in RSS_FEEDS:
        snaps = cdx_snapshots(feed)
        print(f"[wayback] {source:8s} {feed}  快照 {len(snaps)} 个")
        for n, (ts14, orig) in enumerate(snaps, 1):
            body = cached_get(f"https://web.archive.org/web/{ts14}id_/{orig}")
            if not body:
                continue
            seen = wb_ts_to_epoch(ts14)
            for it in news._parse_rss(body.decode("utf-8", "replace"), source):
                add(it, seen)
            if n % 25 == 0:
                print(f"           …{n}/{len(snaps)}  累计新闻 {len(ITEMS)}")
    for feed in DQD_FEEDS:
        snaps = cdx_snapshots(feed)
        print(f"[wayback] 懂球帝   {feed}  快照 {len(snaps)} 个")
        for ts14, orig in snaps:
            body = cached_get(f"https://web.archive.org/web/{ts14}id_/{orig}")
            if not body:
                continue
            try:
                data = json.loads(body.decode("utf-8", "replace"))
            except Exception:
                continue
            seen = wb_ts_to_epoch(ts14)
            for it in news._parse_dongqiudi(data):
                if it.get("ts", 0) > seen:      # 解析器会把未来时间夹到"现在"，这里改夹到快照时刻
                    it["ts"] = seen
                add(it, seen)


def run_guardian(news):
    key = os.environ.get("GUARDIAN_KEY", "test")
    day = G_FROM
    while day <= G_TO:
        page, pages, got = 1, 1, 0
        while page <= pages:
            q = urllib.parse.urlencode({
                "section": "football", "from-date": day.isoformat(),
                "to-date": day.isoformat(), "page-size": 200, "page": page,
                "order-by": "oldest", "show-fields": "trailText", "api-key": key})
            body = cached_get("https://content.guardianapis.com/search?" + q)
            if not body:
                break
            try:
                resp = json.loads(body.decode("utf-8", "replace"))["response"]
            except Exception:
                break
            pages = resp.get("pages", 1) or 1
            for r in resp.get("results", []):
                title = (r.get("webTitle") or "").strip()
                trail = ((r.get("fields") or {}).get("trailText") or "")
                blob = f"{title} {trail}"
                teams = news._teams_in(blob)
                if not (news._is_wc(blob) or teams):
                    continue
                try:
                    ts = int(dt.datetime.fromisoformat(
                        r["webPublicationDate"].replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
                add({"source": "Guardian", "title": title, "url": r.get("webUrl"),
                     "ts": ts, "teams": teams, "wc": news._is_wc(blob)}, ts)
                got += 1
            page += 1
            if key == "test":
                time.sleep(1.1)          # test key 有频率限制
        print(f"[guardian] {day}  世界杯相关 {got} 条")
        day += dt.timedelta(days=1)


def main():
    news = load_news_module()
    t0 = time.time()
    run_wayback(news)
    run_guardian(news)
    items = sorted(ITEMS.values(), key=lambda x: x["ts"])
    lo = wb_ts_to_epoch("20260601000000")
    hi = wb_ts_to_epoch("20260806000000")
    items = [x for x in items if lo <= x["ts"] <= hi]
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=0), encoding="utf-8")

    by_src: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for x in items:
        by_src[x["source"]] = by_src.get(x["source"], 0) + 1
        d = dt.datetime.utcfromtimestamp(x["ts"]).strftime("%m-%d")
        by_day[d] = by_day.get(d, 0) + 1
    print("\n==== 完成 ====")
    print(f"共 {len(items)} 条，用时 {int(time.time() - t0)} 秒 → {OUT}")
    print("按来源：", by_src)
    print("按日期：", " ".join(f"{d}:{n}" for d, n in sorted(by_day.items())))


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        main()
