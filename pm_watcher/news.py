"""
足球新闻源（只读，零新依赖）。
英文：BBC / Guardian / ESPN / Sky 的公开 RSS。
中文：懂球帝头条接口（api.dongqiudi.com，公开只读 JSON）——对大陆用户最贴合。

按世界杯/球队关键词过滤，带时间戳，供看板与赔率异动并排对照。
诚实边界：不做"新闻→赔率变动"的自动因果归因；只把两路信息打时间戳并排，由使用者对照。
"""
from __future__ import annotations

import asyncio
import datetime
import email.utils
import time
import xml.etree.ElementTree as ET

import httpx

# ---- 英文 RSS 源 ----
FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/sport/football/rss.xml"),
    ("Guardian", "https://www.theguardian.com/football/rss"),
    ("ESPN", "https://www.espn.com/espn/rss/soccer/news"),
    ("Sky", "https://www.skysports.com/rss/12040"),
]

# ---- 懂球帝中文源（头条频道；公开只读）----
DQD_API = "https://api.dongqiudi.com/app/tabs/web/1.json"
DQD_ARTICLE = "https://www.dongqiudi.com/articles/{id}.html"

WC_WORDS = ["world cup", "fifa"]          # 英文世界杯总词
WC_WORDS_CN = ["世界杯", "世预赛", "国家队", "美洲杯", "欧洲杯", "国际比赛日", "大名单", "热身赛"]   # 中文世界杯/国家队总词

# 英文关键词（小写匹配）
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

# 中文关键词 → 英文规范名（与看板里球队名一致，便于联动与显示国旗）
# 含球星名：头条常用球星而非国家名（写"姆巴佩"不写"法国"），加上可显著提升世界杯内容命中率。
TEAM_WORDS_CN = {
    "Spain": ["西班牙", "亚马尔", "佩德里", "莫拉塔"],
    "France": ["法国", "法兰西", "姆巴佩", "楚阿梅尼", "格列兹曼", "登贝莱"],
    "England": ["英格兰", "凯恩", "贝林厄姆", "福登", "萨卡"],
    "Portugal": ["葡萄牙", "C罗", "罗纳尔多", "B费", "B席", "莱奥"],
    "Brazil": ["巴西", "维尼修斯", "罗德里戈", "拉菲尼亚", "内马尔"],
    "Argentina": ["阿根廷", "梅西", "劳塔罗", "阿尔瓦雷斯"],
    "Germany": ["德国", "穆西亚拉", "维尔茨", "基米希"],
    "Netherlands": ["荷兰", "范戴克", "加克波"],
    "Belgium": ["比利时", "德布劳内", "卢卡库"],
    "Norway": ["挪威", "哈兰德", "厄德高"],
    "Croatia": ["克罗地亚", "莫德里奇"], "Uruguay": ["乌拉圭"], "Colombia": ["哥伦比亚"],
    "Switzerland": ["瑞士"], "Morocco": ["摩洛哥"], "Japan": ["日本", "森保一"],
    "Mexico": ["墨西哥"], "USA": ["美国队", "美国男足"], "South Korea": ["韩国", "孙兴慜"],
    "Senegal": ["塞内加尔"], "Australia": ["澳大利亚"],
    "Sweden": ["瑞典"], "Austria": ["奥地利"], "Turkey": ["土耳其"],
    "Ecuador": ["厄瓜多尔"], "Egypt": ["埃及", "萨拉赫"], "Ghana": ["加纳"],
    "Ivory Coast": ["科特迪瓦", "象牙海岸"], "Tunisia": ["突尼斯"],
    "Algeria": ["阿尔及利亚"], "Iran": ["伊朗"], "Saudi Arabia": ["沙特"],
    "Qatar": ["卡塔尔"], "Canada": ["加拿大"], "Scotland": ["苏格兰"],
    "Paraguay": ["巴拉圭"], "Uzbekistan": ["乌兹别克斯坦"], "Jordan": ["约旦"],
    "Iraq": ["伊拉克"], "Cape Verde": ["佛得角"], "Curaçao": ["库拉索"],
    "Haiti": ["海地"], "New Zealand": ["新西兰"], "South Africa": ["南非"],
    "Panama": ["巴拿马"], "DR Congo": ["刚果"], "Bosnia & Herzegovina": ["波黑"],
    "Czechia": ["捷克"], "Italy": ["意大利"],
}


def _teams_in(text: str) -> list[str]:
    t = text.lower()
    return [team for team, kws in TEAM_WORDS.items() if any(k in t for k in kws)]


def _teams_in_cn(text: str) -> list[str]:
    return [team for team, kws in TEAM_WORDS_CN.items() if any(k in text for k in kws)]


def _is_wc(text: str) -> bool:
    return any(w in text.lower() for w in WC_WORDS)


def _is_wc_cn(text: str) -> bool:
    return any(w in text for w in WC_WORDS_CN)


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
            continue
        out.append({"source": source, "title": title, "url": link,
                    "ts": ts, "teams": teams, "wc": _is_wc(blob)})
    return out


def _dqd_articles(data) -> list:
    """从懂球帝返回里稳健地找出文章列表，兼容结构漂移。"""
    if isinstance(data, dict):
        for key in ("articles", "data", "list", "items", "result"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
            if isinstance(v, dict):                       # 再下钻一层
                got = _dqd_articles(v)
                if got:
                    return got
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data
    return []


def _dqd_field(a: dict, *names):
    for n in names:
        v = a.get(n)
        if v not in (None, ""):
            return v
    return None


def _parse_dongqiudi(data: dict) -> list[dict]:
    """懂球帝头条 → 统一新闻条目；中文队名映射回英文规范名。容忍字段/结构变化。"""
    out = []
    now = int(time.time())
    cn_tz = datetime.timezone(datetime.timedelta(hours=8))   # 北京时间
    for a in _dqd_articles(data):
        if not isinstance(a, dict):
            continue
        title = str(_dqd_field(a, "title", "news_title", "name", "share_title") or "").strip()
        aid = _dqd_field(a, "id", "aid", "article_id", "news_id")
        if not title or aid is None:
            continue
        blob = title + " " + str(_dqd_field(a, "description", "share_desc", "summary") or "")
        teams = _teams_in_cn(blob)
        wc = _is_wc_cn(blob)
        # 只留世界杯/球队相关；滤掉头条里混入的 CBA / 篮协 / 国内转会等噪音
        if not (wc or teams):
            continue
        ts = 0
        pub = _dqd_field(a, "published_at", "created_at", "publish_at")
        if pub:
            try:
                dt = datetime.datetime.strptime(str(pub), "%Y-%m-%d %H:%M:%S").replace(tzinfo=cn_tz)
                ts = int(dt.timestamp())
            except Exception:
                ts = 0
        if not ts:                                    # 退而取 epoch 时间戳字段
            epoch = _dqd_field(a, "sort_timestamp", "publish_timestamp", "timestamp")
            try:
                epoch = int(epoch)
                ts = epoch // 1000 if epoch > 10**12 else epoch   # 毫秒→秒
            except Exception:
                ts = 0
        if ts > now:          # 置顶文章会用未来时间戳，夹到"现在"
            ts = now
        out.append({"source": "懂球帝", "title": title,
                    "url": DQD_ARTICLE.format(id=aid),
                    "ts": ts, "teams": teams, "wc": wc})
    return out


async def fetch_news(limit: int = 50) -> list[dict]:
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0.0.0 Safari/537.36",
               "Accept": "application/json, text/xml, */*"}
    items: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0, headers=headers,
                                 follow_redirects=True) as c:
        async def rss(name, url):
            try:
                r = await c.get(url)
                r.raise_for_status()
                return _parse_rss(r.text, name)
            except Exception:
                return []

        async def dqd():
            try:
                r = await c.get(DQD_API)
                r.raise_for_status()
                return _parse_dongqiudi(r.json())
            except Exception:
                return []

        tasks = [rss(n, u) for n, u in FEEDS] + [dqd()]
        batches = await asyncio.gather(*tasks)
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
