"""
足球新闻源（只读，零新依赖）：BBC Sport Football + The Guardian Football 的公开 RSS。
按世界杯/球队关键词过滤，带时间戳，供看板与赔率异动并排对照。

诚实边界：我们不做"新闻→赔率变动"的自动因果归因；只把两路信息
都打上时间戳并排展示，由使用者自行对照。
"""
from __future__ import annotations

import asyncio
import email.utils
import time
import xml.etree.ElementTree as ET

import httpx

FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Guardian", "https://www.theguardian.com/football/rss"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
    ("Sky", "https://www.skysports.com/rss/12040"),
]

# 过滤词：世界杯总词 + 球队名/常见别称（小写匹配）
WC_WORDS = ["world cup", "fifa"]
TEAM_WORDS = {
    "Spain": ["spain", "spanish"], "France": ["france", "french"],
    "England": ["england"], "Portugal": ["portugal", "portuguese"],
    "Brazil": ["brazil", "brazilian"], "Argentina": ["argentina", "argentine"],
    "Germany": ["germany", "german"], "Netherlands": ["netherlands", "dutch"],
    "Norway": ["norway"], "Belgium": ["belgium"], "Japan": ["japan"],
    "Morocco": ["morocco"], "Mexico": ["mexico"], "USA": ["usmnt", "united states"],
    "Croatia": ["croatia"], "Uruguay": ["uruguay"], "Colombia": ["colombia"],
    "Switzerland": ["switzerland"], "Turkey": ["turkey", "türkiye"],
    "South Korea": ["south korea"], "Canada": ["canada"], "Australia": ["australia"],
    "Senegal": ["senegal"], "Ecuador": ["ecuador"], "Egypt": ["egypt"],
    "Scotland": ["scotland"], "Austria": ["austria"], "Sweden": ["sweden"],
}


def _teams_in(text: str) -> list[str]:
    t = text.lower()
    return [team for team, kws in TEAM_WORDS.items() if any(k in t for k in kws)]


def _is_wc(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in WC_WORDS)


def _parse_rss(xml_text: str, source: str) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        desc = (item.findtext("description") or "").strip()
        pub = (item.findtext("pubDate") or "").strip()
        ts = 0
        if pub:
            try:
                ts = int(email.utils.parsedate_to_datetime(pub).timestamp())
            except Exception:
                ts = 0
        blob = f"{title} {desc}"
        teams = _teams_in(blob)
        if not (_is_wc(blob) or teams):
            continue   # 与世界杯/球队无关的足球新闻，跳过
        out.append({"source": source, "title": title, "url": link,
                    "ts": ts, "teams": teams, "wc": _is_wc(blob)})
    return out


async def fetch_news(limit: int = 40) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0.0.0 Safari/537.36"}
    items: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0, headers=headers,
                                 follow_redirects=True) as c:
        async def one(name, url):
            try:
                r = await c.get(url)
                r.raise_for_status()
                return _parse_rss(r.text, name)
            except Exception:
                return []
        batches = await asyncio.gather(*[one(n, u) for n, u in FEEDS])
    for b in batches:
        items.extend(b)
    # 去重（按链接）、按时间倒序
    seen, dedup = set(), []
    for it in sorted(items, key=lambda x: -x["ts"]):
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        dedup.append(it)
    return dedup[:limit]


# 给 serve.py 用的同步缓存包装
_cache = {"ts": 0, "items": []}


def get_news_cached(max_age: int = 120) -> list[dict]:
    if time.time() - _cache["ts"] > max_age:
        try:
            _cache["items"] = asyncio.run(fetch_news())
            _cache["ts"] = time.time()
        except Exception:
            pass
    return _cache["items"]
