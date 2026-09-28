#!/usr/bin/env python3
"""
补回 2026 世界杯期间的新闻（线上版只把新闻放在内存里，没有落盘）。

三条来源（都会用 pm_watcher/news.py 里原来的"是否世界杯新闻 / 涉及哪些球队"规则过滤）：
  1) 互联网档案馆 Wayback Machine 存下的 RSS 快照
     —— 原站同源（BBC / Guardian / ESPN / Sky / 懂球帝），并扩展到这些媒体旗下的子频道 RSS。
  2) GDELT 全球新闻库（免费、无需 key）：按 12 小时窗口检索主流中英文媒体的世界杯报道。
     注意：GDELT 只保留最近约 3 个月，太早的时段会是空的。
  3) The Guardian 开放内容接口（需要免费 key，export GUARDIAN_KEY=...）：
     覆盖整个赛程，是补齐小组赛阶段最可靠的来源。

输出：archive/news_archive.json    每条 {source,title,url,ts,teams,wc,first_seen}

用法（仓库根目录，Mac 终端）：
  export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
  export GUARDIAN_KEY=你的key            # 可选但强烈建议
  python3 archive/backfill_news.py --check   # 先自检
  python3 archive/backfill_news.py

可反复运行：抓过的内容缓存在 archive/.news_cache/，中断后重跑会跳过已完成部分。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
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

START, END = "20260601", "20260806"                   # 档案馆检索窗口（UTC）
WIN_FROM, WIN_TO = dt.date(2026, 6, 8), dt.date(2026, 7, 21)   # 按天检索窗口
KEEP_FROM = int(dt.datetime(2026, 6, 1, tzinfo=dt.timezone.utc).timestamp())
KEEP_TO = int(dt.datetime(2026, 8, 6, tzinfo=dt.timezone.utc).timestamp())

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# 原站同源的精确 RSS
RSS_FEEDS = [
    ("BBC", "feeds.bbci.co.uk/sport/football/rss.xml"),
    ("BBC", "feeds.bbci.co.uk/sport/football/world-cup/rss.xml"),
    ("BBC", "feeds.bbci.co.uk/sport/rss.xml"),
    ("Guardian", "www.theguardian.com/football/rss"),
    ("Guardian", "www.theguardian.com/football/world-cup-2026/rss"),
    ("Guardian", "www.theguardian.com/football/world-cup-football/rss"),
    ("ESPN", "www.espn.com/espn/rss/soccer/news"),
    ("Sky", "www.skysports.com/rss/12040"),
]
# 同媒体旗下的子频道（国家队频道等）：按前缀在档案馆里找所有 RSS 快照
RSS_PREFIXES = [
    ("BBC", "feeds.bbci.co.uk/sport/football/"),
    ("Guardian", "www.theguardian.com/football/"),
    ("ESPN", "www.espn.com/espn/rss/"),
    ("Sky", "www.skysports.com/rss/"),
]
DQD_FEEDS = ["api.dongqiudi.com/app/tabs/web/1.json"]
MAX_SNAPS_PER_PREFIX = 400

# GDELT：只收主流媒体，域名 → 显示名
GDELT_EN = {"bbc.co.uk": "BBC", "bbc.com": "BBC", "theguardian.com": "Guardian", "espn.com": "ESPN",
            "skysports.com": "Sky", "reuters.com": "Reuters", "apnews.com": "AP",
            "nytimes.com": "NYT", "cnn.com": "CNN", "foxsports.com": "FOX Sports",
            "theathletic.com": "The Athletic", "independent.co.uk": "Independent", "goal.com": "Goal"}
GDELT_ZH = {"dongqiudi.com": "懂球帝", "sina.com.cn": "新浪体育", "qq.com": "腾讯体育", "sohu.com": "搜狐体育",
            "163.com": "网易体育", "thepaper.cn": "澎湃新闻", "cctv.com": "央视", "xinhuanet.com": "新华社",
            "chinanews.com.cn": "中新网", "ifeng.com": "凤凰网", "hupu.com": "虎扑", "people.com.cn": "人民网"}


# ---------- 载入原项目的 news.py（绕开包 __init__；没装 httpx 时放空壳） ----------
def load_news_module():
    pkg = types.ModuleType("pm_watcher")
    pkg.__path__ = [str(ROOT / "pm_watcher")]
    sys.modules.setdefault("pm_watcher", pkg)
    try:
        import httpx  # noqa: F401
    except ImportError:
        sys.modules["httpx"] = types.ModuleType("httpx")
    spec = importlib.util.spec_from_file_location("pm_watcher.news", ROOT / "pm_watcher" / "news.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------- 网络（优先系统 curl：避开 macOS Python 缺 SSL 证书的问题；自动走代理环境变量） ----------
CURL = shutil.which("curl")
ERRORS: dict[str, str] = {}


def _note(url: str, msg: str):
    host = urllib.parse.urlparse(url).netloc
    if host not in ERRORS:
        ERRORS[host] = msg
        print(f"   ⚠ {host}: {msg}")


def _get_once(url: str, timeout: int):
    if CURL:
        r = subprocess.run([CURL, "-sS", "-L", "--max-time", str(timeout), "-A", UA,
                            "-o", "-", "-w", "\n__HTTP__%{http_code}", url], capture_output=True)
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


def _cache_file(url: str) -> Path:
    CACHE.mkdir(exist_ok=True)
    return CACHE / (hashlib.sha1(url.encode()).hexdigest() + ".bin")


def cached_get(url: str, valid=None) -> bytes | None:
    """带缓存的 GET。valid(body)->bool 用于拒绝缓存限流页之类的无效响应。"""
    f = _cache_file(url)
    if f.exists():
        return f.read_bytes()
    body = http_get(url)
    if body is not None and (valid is None or valid(body)):
        f.write_bytes(body)
        return body
    return None if valid else body


def _is_json(b: bytes) -> bool:
    try:
        json.loads(b.decode("utf-8", "replace"))
        return True
    except Exception:
        return False


def check():
    tests = [
        ("档案馆 CDX", "https://web.archive.org/cdx/search/cdx?url=feeds.bbci.co.uk/sport/football/rss.xml&from=20260615&to=20260616&output=json&limit=3"),
        ("GDELT", "https://api.gdeltproject.org/api/v2/doc/doc?query=%22world%20cup%22%20domainis:bbc.co.uk&mode=ArtList&maxrecords=3&format=json&timespan=7d"),
        ("Guardian API", "https://content.guardianapis.com/search?section=football&from-date=2026-06-20&to-date=2026-06-20&page-size=3&api-key=" + os.environ.get("GUARDIAN_KEY", "test")),
    ]
    print("传输方式:", "curl" if CURL else "python urllib",
          "| 代理:", os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY") or "未设置",
          "| GUARDIAN_KEY:", "已设置" if os.environ.get("GUARDIAN_KEY") else "未设置")
    for name, url in tests:
        code, body = _get_once(url, 30)
        if code == 200:
            print(f"  ✅ {name}: 通 （{len(body)} 字节） {body[:120]!r}")
        else:
            print(f"  ❌ {name}: " + (f"HTTP {code}" if code else str(body)[:300]))


def wb_ts_to_epoch(ts14: str) -> int:
    return int(dt.datetime.strptime(ts14, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc).timestamp())


def cdx(params: dict) -> list:
    q = urllib.parse.urlencode({"from": START, "to": END, "output": "json",
                                "filter": "statuscode:200", "collapse": "digest", **params})
    body = cached_get("https://web.archive.org/cdx/search/cdx?" + q, valid=_is_json)
    if not body:
        return []
    try:
        rows = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return []
    return [(r[1], r[2]) for r in rows[1:] if len(r) > 2]


# ---------- 汇总（按规范化网址去重：去掉 ?at_medium=RSS 这类跟踪参数） ----------
ITEMS: dict[str, dict] = {}


def norm_url(u: str) -> str:
    p = urllib.parse.urlsplit((u or "").strip())
    if not p.netloc:
        return (u or "").strip()
    host = p.netloc.lower().replace("www.", "", 1) if p.netloc.lower().startswith("www.") else p.netloc.lower()
    return urllib.parse.urlunsplit(("https", host, p.path.rstrip("/"), "", ""))


def add(item: dict, seen_at: int):
    url = norm_url(item.get("url") or "")
    if not url or not item.get("title"):
        return
    cur = ITEMS.get(url)
    if cur is None:
        it = dict(item)
        it["url"] = (item.get("url") or "").split("?")[0]
        it["first_seen"] = seen_at
        it["teams"] = list(it.get("teams") or [])
        if not it.get("ts"):
            it["ts"] = seen_at
        ITEMS[url] = it
    else:
        cur["first_seen"] = min(cur["first_seen"], seen_at)
        if item.get("ts") and (not cur.get("ts") or item["ts"] < cur["ts"]):
            cur["ts"] = item["ts"]
        for t in item.get("teams") or []:
            if t not in cur["teams"]:
                cur["teams"].append(t)
        cur["wc"] = cur.get("wc") or item.get("wc")


# ---------- 1) 档案馆 ----------
def _parse_snapshot(news, source, ts14, orig):
    body = cached_get(f"https://web.archive.org/web/{ts14}id_/{orig}")
    if not body:
        return 0
    n0 = len(ITEMS)
    for it in news._parse_rss(body.decode("utf-8", "replace"), source):
        add(it, wb_ts_to_epoch(ts14))
    return len(ITEMS) - n0


def run_wayback(news):
    seen_snaps = set()
    for source, feed in RSS_FEEDS:
        snaps = cdx({"url": feed})
        print(f"[档案馆] {source:8s} {feed}  快照 {len(snaps)} 个")
        for ts14, orig in snaps:
            seen_snaps.add((ts14, orig))
            _parse_snapshot(news, source, ts14, orig)
    for source, prefix in RSS_PREFIXES:
        snaps = [s for s in cdx({"url": prefix, "matchType": "prefix", "filter": "statuscode:200",
                                 "limit": "20000"})
                 if ("rss" in s[1].lower() or s[1].lower().endswith(".xml")) and s not in seen_snaps]
        snaps = snaps[:MAX_SNAPS_PER_PREFIX]
        feeds = len({s[1] for s in snaps})
        print(f"[档案馆] {source:8s} {prefix}*  子频道 {feeds} 个 · 快照 {len(snaps)} 个")
        for n, (ts14, orig) in enumerate(snaps, 1):
            _parse_snapshot(news, source, ts14, orig)
            if n % 50 == 0:
                print(f"          …{n}/{len(snaps)}  累计新闻 {len(ITEMS)}")
    for feed in DQD_FEEDS:
        snaps = cdx({"url": feed})
        print(f"[档案馆] 懂球帝   {feed}  快照 {len(snaps)} 个")
        for ts14, orig in snaps:
            body = cached_get(f"https://web.archive.org/web/{ts14}id_/{orig}", valid=_is_json)
            if not body:
                continue
            seen = wb_ts_to_epoch(ts14)
            for it in news._parse_dongqiudi(json.loads(body.decode("utf-8", "replace"))):
                if it.get("ts", 0) > seen:
                    it["ts"] = seen
                add(it, seen)


# ---------- 2) GDELT ----------
def _gdelt_valid(b: bytes) -> bool:
    try:
        j = json.loads(b.decode("utf-8", "replace"))
        return isinstance(j, dict)
    except Exception:
        return False


def run_gdelt(news):
    total_hits = 0
    for lang, doms, words in (("english", GDELT_EN, '"world cup"'), ("chinese", GDELT_ZH, '"world cup"')):
        dq = " OR ".join(f"domainis:{d}" for d in doms)
        day, got = WIN_FROM, 0
        while day <= WIN_TO:
            for h in (0, 12):
                s = dt.datetime(day.year, day.month, day.day, h, tzinfo=dt.timezone.utc)
                e = s + dt.timedelta(hours=12)
                q = urllib.parse.urlencode({
                    "query": f"{words} ({dq}) sourcelang:{lang}", "mode": "ArtList", "maxrecords": 250,
                    "format": "json", "sort": "DateAsc",
                    "startdatetime": s.strftime("%Y%m%d%H%M%S"), "enddatetime": e.strftime("%Y%m%d%H%M%S")})
                url = "https://api.gdeltproject.org/api/v2/doc/doc?" + q
                cached = _cache_file(url).exists()
                body = cached_get(url, valid=_gdelt_valid)
                if not cached:
                    time.sleep(5.5)                     # GDELT 要求约 5 秒一次
                if not body:
                    continue
                arts = json.loads(body.decode("utf-8", "replace")).get("articles") or []
                for a in arts:
                    dom = (a.get("domain") or "").lower()
                    src = next((v for k, v in doms.items() if dom == k or dom.endswith("." + k)), None)
                    title = (a.get("title") or "").strip()
                    if not src or not title:
                        continue
                    if lang == "english":
                        teams, wc = news._teams_in(title), news._is_wc(title)
                    else:
                        teams, wc = news._teams_in_cn(title), news._is_wc_cn(title)
                        wc = wc or ("world cup" in title.lower())
                    if not (wc or teams):
                        continue
                    try:
                        ts = int(dt.datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ")
                                 .replace(tzinfo=dt.timezone.utc).timestamp())
                    except Exception:
                        continue
                    add({"source": src, "title": title, "url": a.get("url"), "ts": ts,
                         "teams": teams, "wc": wc}, ts)
                    got += 1
            if day.day in (1, 10, 20) or day == WIN_TO:
                print(f"[GDELT] {lang:7s} 到 {day}  累计收录 {got} 条")
            day += dt.timedelta(days=1)
        total_hits += got
    if total_hits == 0:
        print("[GDELT] 没有取到结果（GDELT 只保留约最近 3 个月，或网络/限流问题）")


# ---------- 3) Guardian ----------
def run_guardian(news):
    key = os.environ.get("GUARDIAN_KEY")
    if not key:
        print("[Guardian] 未设置 GUARDIAN_KEY，跳过。免费申请：https://open-platform.theguardian.com/access/")
        return
    day = WIN_FROM
    while day <= WIN_TO:
        page, pages, got = 1, 1, 0
        while page <= pages:
            q = urllib.parse.urlencode({
                "section": "football", "from-date": day.isoformat(), "to-date": day.isoformat(),
                "page-size": 200, "page": page, "order-by": "oldest",
                "show-fields": "trailText", "api-key": key})
            body = cached_get("https://content.guardianapis.com/search?" + q, valid=_is_json)
            if not body:
                break
            resp = json.loads(body.decode("utf-8", "replace")).get("response") or {}
            pages = resp.get("pages", 1) or 1
            for r in resp.get("results", []):
                title = (r.get("webTitle") or "").strip()
                blob = f"{title} {(r.get('fields') or {}).get('trailText') or ''}"
                teams = news._teams_in(blob)
                if not (news._is_wc(blob) or teams):
                    continue
                try:
                    ts = int(dt.datetime.fromisoformat(r["webPublicationDate"].replace("Z", "+00:00")).timestamp())
                except Exception:
                    continue
                add({"source": "Guardian", "title": title, "url": r.get("webUrl"), "ts": ts,
                     "teams": teams, "wc": news._is_wc(blob)}, ts)
                got += 1
            page += 1
        print(f"[Guardian] {day}  世界杯相关 {got} 条")
        day += dt.timedelta(days=1)


def main():
    news = load_news_module()
    t0 = time.time()
    skip = set(a.split("=", 1)[1] for a in sys.argv if a.startswith("--skip="))
    if "wayback" not in skip:
        run_wayback(news)
    if "gdelt" not in skip:
        run_gdelt(news)
    if "guardian" not in skip:
        run_guardian(news)
    items = sorted((x for x in ITEMS.values() if KEEP_FROM <= x["ts"] <= KEEP_TO), key=lambda x: x["ts"])
    OUT.write_text(json.dumps(items, ensure_ascii=False, indent=0), encoding="utf-8")

    by_src, by_day = {}, {}
    for x in items:
        by_src[x["source"]] = by_src.get(x["source"], 0) + 1
        d = dt.datetime.utcfromtimestamp(x["ts"]).strftime("%m-%d")
        by_day[d] = by_day.get(d, 0) + 1
    print("\n==== 完成 ====")
    print(f"共 {len(items)} 条，用时 {int(time.time() - t0)} 秒 → {OUT}")
    print("按来源：", dict(sorted(by_src.items(), key=lambda kv: -kv[1])))
    print("按日期：", " ".join(f"{d}:{n}" for d, n in sorted(by_day.items())))
    gaps = [d for d in (WIN_FROM + dt.timedelta(days=i) for i in range((WIN_TO - WIN_FROM).days + 1))
            if d.strftime("%m-%d") not in by_day and d >= dt.date(2026, 6, 11)]
    if gaps:
        print("仍无新闻的赛期日期：", " ".join(d.strftime("%m-%d") for d in gaps))


if __name__ == "__main__":
    if "--check" in sys.argv:
        check()
    else:
        main()
