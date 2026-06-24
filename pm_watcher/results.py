"""
比赛结果源（复盘板块用）。

数据来自 football-data.org 免费档：世界杯在免费竞赛之列、干净的 REST JSON。
读取端只需一个免费 token（注册后邮件发放），放进 .env 的 FOOTBALL_DATA_TOKEN。
我们只取「已结束」的比赛，按 match_id 幂等写入 history.result 表——不是轮询，
一天跑几次或打开复盘视图时取一次即可（免费档 10 次/分钟，一次调用返回整届）。

队名一律过 names.canonical_country()，与 snap 同一套规范名；结果 outcome 由比分推导，
取赢家规范名或 "Draw"，与 snap 的 match label 同词表，所以算分时是平凡等值 join。

用法：
    python -m pm_watcher.results              # 拉取并写入已结束的比赛
    python -m pm_watcher.results --selfcheck  # 只核对 48 强队名能否对上 names.py
"""
from __future__ import annotations

import sys
import time

import httpx

from . import config, history
from .model import iso_to_ts
from .names import WC_TEAMS, canonical_country

BASE = "https://api.football-data.org/v4"
COMP = "WC"   # FIFA World Cup


def _client() -> httpx.Client:
    token = config.FOOTBALL_DATA_TOKEN
    if not token:
        raise RuntimeError(
            "没读到 FOOTBALL_DATA_TOKEN。在项目根目录 .env 里加一行 "
            "FOOTBALL_DATA_TOKEN=你的token（参见 .env.example）。")
    return httpx.Client(base_url=BASE, timeout=20.0,
                        headers={"X-Auth-Token": token,
                                 "User-Agent": "pm-watcher/0.1"})


def _outcome(home: str, away: str, hs: int | None, as_: int | None) -> str | None:
    """由比分推导结果 label（赢家规范名 / "Draw"）。比分缺失返回 None。"""
    if hs is None or as_ is None:
        return None
    if hs > as_:
        return home
    if as_ > hs:
        return away
    return "Draw"


def fetch_finished() -> list[dict]:
    """返回已结束比赛的规范化记录列表（不写库）。"""
    with _client() as http:
        r = http.get(f"/competitions/{COMP}/matches", params={"status": "FINISHED"})
        r.raise_for_status()
        data = r.json()

    out: list[dict] = []
    for m in data.get("matches", []):
        home = canonical_country((m.get("homeTeam") or {}).get("name") or "")
        away = canonical_country((m.get("awayTeam") or {}).get("name") or "")
        ft = ((m.get("score") or {}).get("fullTime") or {})
        hs, as_ = ft.get("home"), ft.get("away")
        outcome = _outcome(home, away, hs, as_)
        kickoff = iso_to_ts(m.get("utcDate"))
        if outcome is None or kickoff is None:
            continue   # 没比分或没开赛时间的，跳过，下次再补
        grp = m.get("group")            # 例 "GROUP_F" → 存 "F"；非小组赛阶段为 None
        if grp and grp.upper().startswith("GROUP_"):
            grp = grp.split("_", 1)[1]
        out.append({
            "match_id": int(m["id"]), "home": home, "away": away,
            "home_score": hs, "away_score": as_, "outcome": outcome,
            "kickoff_ts": int(kickoff), "status": m.get("status", "FINISHED"),
            "grp": grp,
        })
    return out


def ingest() -> int:
    """拉取已结束比赛并幂等写入 history.result。返回写入条数。"""
    rows = fetch_finished()
    for r in rows:
        history.record_result(
            r["match_id"], r["home"], r["away"],
            r["home_score"], r["away_score"], r["outcome"],
            r["kickoff_ts"], r["status"], r.get("grp"))
    return len(rows)


def selfcheck() -> list[str]:
    """核对 football-data 的 48 强队名能否经 canonical_country() 落进 WC_TEAMS。
    返回未对上的（football-data 原始名, 归一后名）说明列表，并打印可直接粘贴的 _reg 建议。"""
    with _client() as http:
        r = http.get(f"/competitions/{COMP}/teams")
        r.raise_for_status()
        teams = r.json().get("teams", [])

    misses: list[str] = []
    for t in teams:
        raw = t.get("name") or ""
        canon = canonical_country(raw)
        if canon not in WC_TEAMS:
            misses.append(f'{raw!r} → {canon!r}（未落进 WC_TEAMS）')

    print(f"football-data 返回 {len(teams)} 支队；未对上 {len(misses)} 支。")
    for line in misses:
        print("  ✗", line)
    if misses:
        print("\n在 names.py 里给每支补一行（规范名按 WC_TEAMS 里的写法）：")
        for t in teams:
            raw = t.get("name") or ""
            if canonical_country(raw) not in WC_TEAMS:
                print(f'    _reg("规范名", {raw!r})')
    else:
        print("✓ 全部对上，names.py 不用改。")
    return misses


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        selfcheck()
    else:
        t0 = time.time()
        n = ingest()
        print(f"写入 {n} 场结果，用时 {time.time() - t0:.1f}s。"
              f"库内已结束比赛共 {history.stats()['results']} 场。")
