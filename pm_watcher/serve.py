"""
本地网页看板服务（只读）。后台按 interval 拉取冠军榜 + 小组榜，算好
共识 / 分歧 / 动量 / 溢价，通过两个端点提供：
  /            交互式单页看板（dashboard.html）
  /api/board   当前数据 + 历史快照（JSON）

跑：
  python -m pm_watcher.serve --live --interval 30
  然后浏览器打开 http://127.0.0.1:8765
（代理需在同一终端 export HTTPS_PROXY=... ，Kalshi/42 才连得上）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import make_client, ALL_PLATFORMS
from .aggregator import fetch_all, build_board, fetch_matches, build_matchboard

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

_state: dict = {"champion": {}, "group": {}, "overround": {}, "errors": {},
                "prev": {}, "ts": 0, "history": []}
_lock = threading.Lock()
_cfg = {"platforms": list(ALL_PLATFORMS), "live": False, "interval": 30}


def _consensus(by_plat: dict, plats: list) -> float | None:
    vals = [by_plat[p] for p in plats if by_plat.get(p) is not None]
    return sum(vals) / len(vals) if vals else None


async def _collect(platforms, live):
    clients = [make_client(p, live=live) for p in platforms]
    try:
        cr, cerr = await fetch_all(clients, "World Cup", limit=64)
        champ = build_board(cr)
        gr, gerr = await fetch_all(clients, "World Cup Group", limit=64)
        group = build_board(gr)
        mr = await fetch_matches(clients)
        matchboard = build_matchboard(mr)
        over = {}
        for p in platforms:
            s = sum(v[p] for v in champ.values() if v.get(p) is not None)
            over[p] = round(s * 100, 1) if s else None
        return champ, group, matchboard, over, {**cerr, **gerr}
    finally:
        for c in clients:
            await c.close()


def _refresh(force: bool = False):
    with _lock:
        fresh = _state["ts"] and (time.time() - _state["ts"] < _cfg["interval"])
        if fresh and not force:
            return
        champ, group, matchboard, over, errs = asyncio.run(_collect(_cfg["platforms"], _cfg["live"]))
        _state["prev"] = _state.get("champion") or {}
        _state["champion"], _state["group"] = champ, group
        _state["matches"] = matchboard
        _state["overround"], _state["errors"] = over, errs
        _state["ts"] = time.time()
        # 历史落盘（变动驱动，价没动不写）
        try:
            from . import history
            history.record_board("champion", _rows(champ))
            history.record_board("group", _rows(group))
            history.record_matches(matchboard)
        except Exception:
            pass
        snap = {"ts": int(_state["ts"]),
                "c": {k: round((_consensus(v, _cfg["platforms"]) or 0) * 100, 1)
                      for k, v in champ.items()}}
        _state["history"].append(snap)
        _state["history"] = _state["history"][-90:]


def _rows(board: dict):
    plats = _cfg["platforms"]
    prev = _state.get("prev") or {}
    out = []
    for team, by in board.items():
        present = {p: by[p] for p in plats if by.get(p) is not None}
        if not present:
            continue
        cons = sum(present.values()) / len(present)
        div = (max(present.values()) - min(present.values())) if len(present) >= 2 else 0.0
        low = min(present, key=present.get) if len(present) >= 2 else None
        mom = {}
        for p, v in present.items():
            pv = (prev.get(team) or {}).get(p)
            if pv is not None and abs(v - pv) >= 0.0005:
                mom[p] = round((v - pv) * 100, 2)
        out.append({"team": team,
                    "p": {k: round(v * 100, 1) for k, v in present.items()},
                    "consensus": round(cons * 100, 1),
                    "divergence": round(div * 100, 1),
                    "low": low, "mom": mom})
    out.sort(key=lambda r: -r["consensus"])
    return out


def _payload():
    return {"updated": int(_state["ts"]), "platforms": _cfg["platforms"],
            "live": _cfg["live"], "interval": _cfg["interval"],
            "champion": _rows(_state["champion"]),
            "group": _rows(_state["group"]),
            "matches": _state.get("matches", []),
            "overround": _state["overround"],
            "errors": {k: str(v)[:80] for k, v in _state["errors"].items()},
            "history": _state["history"]}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body: bytes, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, _HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/history"):
            try:
                from urllib.parse import urlparse, parse_qs
                from . import history
                qs = parse_qs(urlparse(self.path).query)
                scope = (qs.get("scope") or ["champion"])[0]
                teams = (qs.get("teams") or [""])[0]
                since = int((qs.get("since") or [0])[0]) or None
                payload = {"scope": scope, "stats": history.stats(),
                           "series": {t: history.series(scope, t, since)
                                      for t in teams.split(",") if t}}
                self._send(200, json.dumps(payload).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        elif self.path.startswith("/api/news"):
            try:
                from .news import get_news_cached
                self._send(200, json.dumps({"items": get_news_cached()}).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        elif self.path.startswith("/api/board"):
            try:
                _refresh()
                self._send(200, json.dumps(_payload()).encode("utf-8"),
                           "application/json; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}).encode(),
                           "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description="pm-watcher 本地网页看板")
    ap.add_argument("--platforms", default=",".join(ALL_PLATFORMS))
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    _cfg["platforms"] = [p.strip() for p in args.platforms.split(",") if p.strip()]
    _cfg["live"] = args.live
    _cfg["interval"] = max(5, args.interval)

    mode = "LIVE(只读)" if args.live else "MOCK"
    print(f"平台: {', '.join(_cfg['platforms'])} | 模式: {mode} | 刷新: {_cfg['interval']}s")
    print(f"看板地址 → http://127.0.0.1:{args.port}    (Ctrl+C 停止)")
    threading.Thread(target=lambda: _refresh(force=True), daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止。")


if __name__ == "__main__":
    main()
