"""
国名归一：把各平台对同一支球队的不同写法合并成一个规范名。
否则 "Türkiye"(Polymarket) 和 "Turkey"(Kalshi) 会算成两支队，价差失真。

用法：canonical_country("Türkiye") -> "Turkey"
"""
from __future__ import annotations

import unicodedata


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def _key(name: str) -> str:
    """归一到比较键：去口音、小写、& → and、去标点、连字符转空格、压空格。"""
    s = _strip_accents(name).lower()
    s = s.replace("&", " and ").replace("-", " ").replace(".", " ").replace("'", "")
    return " ".join(s.split())


# 比较键 → 规范显示名。键必须用 _key() 的归一形式书写。
_ALIAS: dict[str, str] = {}


def _reg(canonical: str, *variants: str) -> None:
    for v in (canonical, *variants):
        _ALIAS[_key(v)] = canonical


_reg("Turkey", "Türkiye", "Turkiye")
_reg("USA", "United States", "United States of America", "US", "U.S.A.")
_reg("South Korea", "Korea Republic", "Republic of Korea", "Korea South", "Korea, Republic of")
_reg("North Korea", "Korea DPR", "DPR Korea", "Korea, DPR")
_reg("DR Congo", "Congo DR", "Democratic Republic of the Congo",
     "Democratic Republic of Congo", "Congo Kinshasa", "Congo-Kinshasa")
_reg("Congo", "Congo Republic", "Republic of the Congo", "Congo-Brazzaville")
_reg("Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire")
_reg("Czechia", "Czech Republic")
_reg("Bosnia & Herzegovina", "Bosnia and Herzegovina", "Bosnia-Herzegovina", "Bosnia")
_reg("Cape Verde", "Cabo Verde", "Cape Verde Islands")
_reg("Curaçao", "Curacao")
_reg("Iran", "Iran Islamic Republic", "IR Iran")


def canonical_country(name: str) -> str:
    raw = " ".join((name or "").split())
    k = _key(raw)
    if k in _ALIAS:
        return _ALIAS[k]
    # 未登记：智能 title-case（已正确的缩写不强改）
    if raw.isupper() and len(raw) <= 3:
        return raw
    return raw.title()


# 2026 世界杯 48 强（来自真实榜单数据，规范名）。用于识别"单场比赛"市场。
WC_TEAMS: frozenset[str] = frozenset({
    "Spain", "France", "Portugal", "England", "Argentina", "Brazil",
    "Germany", "Netherlands", "Norway", "Belgium", "Colombia", "Japan",
    "Morocco", "Mexico", "USA", "Turkey", "Switzerland", "Uruguay",
    "Czechia", "Bosnia & Herzegovina", "Croatia", "DR Congo", "Iraq",
    "Ecuador", "Iran", "Canada", "Algeria", "Haiti", "Egypt", "Austria",
    "Ghana", "Ivory Coast", "Australia", "Curaçao", "Panama", "Senegal",
    "South Korea", "Cape Verde", "New Zealand", "Scotland", "South Africa",
    "Uzbekistan", "Saudi Arabia", "Jordan", "Qatar", "Tunisia",
    "Paraguay", "Sweden",
})


def is_wc_team(name: str) -> bool:
    return canonical_country(name) in WC_TEAMS
