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

import sqlite3
import threading
import time
from pathlib import Path

MIN_DELTA = 0.001     # 0.1pt（价格为 0~1）
DB_PATH = Path("history.db")

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
    return {"rows": n, "since": t0}
