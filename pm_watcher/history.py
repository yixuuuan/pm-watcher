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
    """落盘 serve._rows() 形状的榜单（p 为 0~100 的百分数）。返回写入行数。"""
    n = 0
    ts = int(time.time())
    for r in board_rows:
        for plat, v in (r.get("p") or {}).items():
            if record(scope, r["team"], plat, v / 100.0, ts):
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
