"""
赔率历史落盘（SQLite，零依赖）。

变动驱动：只在某 (范围, 对象, 平台) 的价格相对上次落盘变动 ≥ MIN_DELTA
（或首次出现）时写一行——赔率不动不占空间，动了一笔不漏。
曲线还原用阶梯插值（上一笔的值一直有效到下一笔）。

表结构：snap(ts, scope, key, platform, price)
  scope: "champion" / "group" / "match"
  key:   球队规范名；match 范围下为 "队A|队B|结果"
"""
from __future__ import annotations

import os
import hashlib
import bisect
import re
import sqlite3
import threading
import time
from pathlib import Path

MIN_DELTA = 0.001     # 0.1pt（价格为 0~1）
DB_PATH = Path(os.environ.get("PMW_DB", "history.db"))   # 托管时设为持久卷路径，如 /data/history.db

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
_last: dict[tuple, float] = {}    # (scope,key,platform) -> 上次落盘价


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.execute("""CREATE TABLE IF NOT EXISTS snap(
            ts INTEGER NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
            platform TEXT NOT NULL, price REAL NOT NULL)""")
        _conn.execute("CREATE INDEX IF NOT EXISTS i_skp ON snap(scope, key, ts)")
        # 结果表：与 snap 完全独立、按 match_id 幂等 upsert。
        # 收盘锚点存在 snap，这里只存「最终结果 + 开赛时刻」，绝不回写 snap。
        # outcome 用赢家规范名或 "Draw"——与 snap 的 match label 同词表，join 不用转换。
        _conn.execute("""CREATE TABLE IF NOT EXISTS result(
            match_id   INTEGER PRIMARY KEY,
            home       TEXT NOT NULL, away TEXT NOT NULL,
            home_score INTEGER, away_score INTEGER,
            outcome    TEXT NOT NULL,
            kickoff_ts INTEGER NOT NULL, status TEXT NOT NULL,
            fetched_at INTEGER NOT NULL, grp TEXT)""")
        # 旧库迁移：result 表已存在但没有 grp 列时补上
        _rcols = [r[1] for r in _conn.execute("PRAGMA table_info(result)")]
        if "grp" not in _rcols:
            _conn.execute("ALTER TABLE result ADD COLUMN grp TEXT")
        # 回填溯源：记录哪些 snap 行是从历史接口补的，便于审计/回退
        _conn.execute("""CREATE TABLE IF NOT EXISTS backfill(
            scope TEXT, key TEXT, platform TEXT, price REAL,
            ts INTEGER, src TEXT, fetched_at INTEGER)""")
        # 访问统计：每日总点击 hits + 当日去重独立访客 uniq
        _conn.execute("""CREATE TABLE IF NOT EXISTS visit_day(
            day TEXT PRIMARY KEY, hits INTEGER NOT NULL DEFAULT 0, uniq INTEGER NOT NULL DEFAULT 0)""")
        _conn.execute("""CREATE TABLE IF NOT EXISTS visit_uniq(
            day TEXT NOT NULL, iph TEXT NOT NULL, PRIMARY KEY(day, iph))""")
        _conn.commit()
        # 预热变动检测缓存：取每个序列的最后一笔
        for s, k, p, v in _conn.execute(
                """SELECT scope, key, platform, price FROM snap s1
                   WHERE ts=(SELECT MAX(ts) FROM snap s2 WHERE s2.scope=s1.scope
                             AND s2.key=s1.key AND s2.platform=s1.platform)"""):
            _last[(s, k, p)] = v
    return _conn


def record(scope: str, key: str, platform: str, price: float,
           ts: int | None = None) -> bool:
    """变动 ≥ MIN_DELTA 才落盘。返回是否写入。"""
    if price is None:
        return False
    with _lock:
        c = _db()
        lk = (scope, key, platform)
        prev = _last.get(lk)
        if prev is not None and abs(price - prev) < MIN_DELTA:
            return False
        c.execute("INSERT INTO snap VALUES(?,?,?,?,?)",
                  (ts or int(time.time()), scope, key, platform, float(price)))
        c.commit()
        _last[lk] = float(price)
        return True


def record_board(scope: str, board_rows: list[dict]) -> int:
    """落盘 serve._rows() 形状的榜单（p 为 0~100 的百分数）。返回写入行数。
    champion 榜只收真正的 48 强——从源头拦掉射手榜球员、板球队、'Other' 等污染项。"""
    n = 0
    ts = int(time.time())
    wc_only = (scope == "champion")
    if wc_only:
        from .names import canonical_country, is_wc_team
    for r in board_rows:
        team = r["team"]
        if wc_only and not is_wc_team(canonical_country(team)):
            continue
        for plat, v in (r.get("p") or {}).items():
            if record(scope, team, plat, v / 100.0, ts):
                n += 1
    return n


def record_matches(matchboard: list[dict]) -> int:
    n = 0
    ts = int(time.time())
    for m in matchboard:
        a, b = m["teams"]
        for plat, od in (m.get("odds") or {}).items():
            for label, v in od.items():
                if record("match", f"{a}|{b}|{label}", plat, v / 100.0, ts):
                    n += 1
    return n


def series(scope: str, key: str, since: int | None = None) -> dict[str, list]:
    """返回 {platform: [[ts, price%], ...]}（按时间升序，price 已转 0~100）。
    带 since 时，会把 since 之前的最后一笔作为锚点放在序列开头（ts=since），
    这样窗口内即使没有变动，也能画出基线。"""
    with _lock:
        c = _db()
        q = "SELECT platform, ts, price FROM snap WHERE scope=? AND key=?"
        args: list = [scope, key]
        if since:
            q += " AND ts>=?"
            args.append(since)
        q += " ORDER BY ts"
        out: dict[str, list] = {}
        for plat, ts, price in c.execute(q, args):
            out.setdefault(plat, []).append([ts, round(price * 100, 2)])
        if since:
            for plat, price in c.execute(
                    """SELECT platform, price FROM snap s1
                       WHERE scope=? AND key=? AND ts<? AND ts=(
                         SELECT MAX(ts) FROM snap s2 WHERE s2.scope=s1.scope
                         AND s2.key=s1.key AND s2.platform=s1.platform AND s2.ts<?)""",
                    (scope, key, since, since)):
                lst = out.setdefault(plat, [])
                if not lst or lst[0][0] > since:
                    lst.insert(0, [since, round(price * 100, 2)])
    return out


def stats() -> dict:
    with _lock:
        c = _db()
        n = c.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
        t0 = c.execute("SELECT MIN(ts) FROM snap").fetchone()[0]
        nr = c.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    return {"rows": n, "since": t0, "results": nr}


# ─────────────────────────── 结果 / 复盘 ───────────────────────────

def record_result(match_id: int, home: str, away: str,
                  home_score: int | None, away_score: int | None,
                  outcome: str, kickoff_ts: int, status: str,
                  grp: str | None = None) -> None:
    """按 match_id 幂等写入一场结果。重复调用只是覆盖为同样的终值。"""
    with _lock:
        c = _db()
        c.execute("""INSERT INTO result
            (match_id, home, away, home_score, away_score, outcome,
             kickoff_ts, status, fetched_at, grp)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id) DO UPDATE SET
              home=excluded.home, away=excluded.away,
              home_score=excluded.home_score, away_score=excluded.away_score,
              outcome=excluded.outcome, kickoff_ts=excluded.kickoff_ts,
              status=excluded.status, fetched_at=excluded.fetched_at,
              grp=excluded.grp""",
            (match_id, home, away, home_score, away_score, outcome,
             kickoff_ts, status, int(time.time()), grp))
        c.commit()


def results() -> list[dict]:
    """所有已结束的比赛，按开赛时间升序。"""
    with _lock:
        c = _db()
        rows = c.execute(
            """SELECT match_id, home, away, home_score, away_score,
                      outcome, kickoff_ts, status, grp FROM result
               WHERE status='FINISHED' ORDER BY kickoff_ts""").fetchall()
    cols = ["match_id", "home", "away", "home_score", "away_score",
            "outcome", "kickoff_ts", "status", "grp"]
    return [dict(zip(cols, r)) for r in rows]


def record_backfill(scope: str, key: str, platform: str, price: float,
                    ts: int, src: str = "polymarket-history") -> bool:
    """把一条真实历史价写进 snap(price 为 0~1，与 live 快照同口径)，并记溯源。
    幂等：同 (scope,key,platform,ts) 已存在则跳过，绝不覆盖 live 采集的快照。"""
    with _lock:
        c = _db()
        ex = c.execute("SELECT 1 FROM snap WHERE scope=? AND key=? AND platform=? AND ts=?",
                       (scope, key, platform, ts)).fetchone()
        if ex:
            return False
        c.execute("INSERT INTO snap(ts, scope, key, platform, price) VALUES(?,?,?,?,?)",
                  (ts, scope, key, platform, price))
        c.execute("""INSERT INTO backfill(scope, key, platform, price, ts, src, fetched_at)
                     VALUES(?,?,?,?,?,?,?)""",
                  (scope, key, platform, price, ts, src, int(time.time())))
        c.commit()
        return True


def closing_line(home: str, away: str, kickoff_ts: int) -> dict[str, dict[str, float]]:
    """某场比赛的收盘定价：每个平台、每个结果 label 在开赛前最后一笔。

    返回 {platform: {label: price%}}，label 为赢家规范名或 "Draw"。
    主客顺序无关——snap 里这场可能存成 home|away 也可能 away|home，两向都收。
    label 直接取自 key 第三段（队名或 Draw），所以与谁主谁客无关。
    """
    keys = [f"{a}|{b}|{lbl}"
            for a, b in ((home, away), (away, home))
            for lbl in (home, away, "Draw")]
    qmarks = ",".join("?" * len(keys))
    with _lock:
        c = _db()
        rows = c.execute(
            f"""SELECT platform, key, price FROM snap s1
                WHERE scope='match' AND key IN ({qmarks}) AND ts<?
                  AND ts=(SELECT MAX(ts) FROM snap s2
                          WHERE s2.scope='match' AND s2.key=s1.key
                            AND s2.platform=s1.platform AND s2.ts<?)""",
            (*keys, kickoff_ts, kickoff_ts)).fetchall()
    out: dict[str, dict[str, float]] = {}
    for plat, key, price in rows:
        label = key.split("|")[2]
        out.setdefault(plat, {})[label] = round(price * 100, 2)
    return out


def record_visit(ip: str) -> None:
    """每次页面加载 +1 次点击；同一 IP 当天只计一次独立访客。绝不抛错。"""
    try:
        day = time.strftime("%Y-%m-%d")
        iph = hashlib.sha256(((ip or "?") + "|" + day).encode("utf-8")).hexdigest()[:16]
        with _lock:
            c = _db()
            cur = c.execute("INSERT OR IGNORE INTO visit_uniq(day, iph) VALUES(?,?)", (day, iph))
            new_uniq = 1 if cur.rowcount > 0 else 0
            c.execute("INSERT INTO visit_day(day, hits, uniq) VALUES(?,1,?) "
                      "ON CONFLICT(day) DO UPDATE SET hits=hits+1, uniq=uniq+?",
                      (day, new_uniq, new_uniq))
            c.commit()
    except Exception:
        pass


def visit_stats() -> dict:
    """返回累计总点击、今日点击、今日独立访客。"""
    try:
        with _lock:
            c = _db()
            total = c.execute("SELECT COALESCE(SUM(hits),0) FROM visit_day").fetchone()[0]
            day = time.strftime("%Y-%m-%d")
            row = c.execute("SELECT hits, uniq FROM visit_day WHERE day=?", (day,)).fetchone()
        return {"total": int(total or 0),
                "today_hits": int(row[0]) if row else 0,
                "today_unique": int(row[1]) if row else 0}
    except Exception:
        return {"total": 0, "today_hits": 0, "today_unique": 0}


# —— 冠军概率增量（赛后影响）——
_CKEY_JUNK = re.compile("[\u200d\ufe0f\U0001F000-\U0001FAFF]")  # 去 ZWJ/变体选择符/emoji（含旗）


def _ckey(k: str) -> str:
    k2 = _CKEY_JUNK.sub("", k).strip()
    return "USA" if k2.lower() == "usa" else k2


def champ_series() -> dict:
    """载入 champion 快照 → {team: {platform: ([ts],[price])}}。
    只保留真正的 48 强；丢掉射手榜球员、板球队、'Other'/'No Winner' 等污染项。"""
    from .names import canonical_country, is_wc_team
    with _lock:
        c = _db()
        rows = c.execute("SELECT key, platform, ts, price FROM snap WHERE scope='champion' ORDER BY ts").fetchall()
    series: dict = {}
    for k, p, ts, pr in rows:
        team = canonical_country(_ckey(k))
        if not is_wc_team(team):          # 白名单：非 48 强一律丢弃
            continue
        tsl, prl = series.setdefault(team, {}).setdefault(p, ([], []))
        tsl.append(ts); prl.append(pr)
    for plats in series.values():         # 合并别名后可能乱序，按 ts 重排
        for p, (tsl, prl) in plats.items():
            if any(tsl[i] > tsl[i + 1] for i in range(len(tsl) - 1)):
                z = sorted(zip(tsl, prl)); plats[p] = ([t for t, _ in z], [v for _, v in z])
    return series


def champ_movers(series: dict, home: str, away: str, kickoff_ts: int,
                 pre_h: int = 8 * 3600, post_lo: int = 2 * 3600,
                 post_hi: int = 14 * 3600, min_pp: float = 0.5) -> list:
    """本场两支球队的赛前→赛后冠军概率变化。按平台配对：只用“同一平台在赛前(开赛前 pre_h 内)
    与赛后(开赛后 post_lo~post_hi 内)都有就近快照”的那些平台，算各平台自身变化再平均——
    避免不同平台子集混算导致的失真。无配对快照则不显示（不跨场、不编造）。"""
    from .names import canonical_country
    out = []
    for raw in (home, away):
        team = canonical_country(raw)
        plats = series.get(team)
        if not plats:
            continue
        ds, bs, as_ = [], [], []
        for tsl, prl in plats.values():
            i = bisect.bisect_right(tsl, kickoff_ts) - 1
            if i < 0 or tsl[i] < kickoff_ts - pre_h:
                continue
            j = bisect.bisect_right(tsl, kickoff_ts + post_hi) - 1
            if j < 0 or tsl[j] < kickoff_ts + post_lo:
                continue
            ds.append(prl[j] - prl[i]); bs.append(prl[i]); as_.append(prl[j])
        if not ds:
            continue
        d = (sum(ds) / len(ds)) * 100
        if abs(d) >= min_pp:
            out.append({"team": team,
                        "before": round((sum(bs) / len(bs)) * 100, 1),
                        "after": round((sum(as_) / len(as_)) * 100, 1),
                        "delta": round(d, 1)})
    return out
