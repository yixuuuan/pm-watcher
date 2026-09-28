"""
2026 世界杯全程数据分析：由 build_site.py 调用，产出 data/analysis.json，供 analysis.html 渲染。

口径说明
  · 单场：一律用「收盘线」（开球前各平台最后一笔），三档都有报价的平台才参与；
    先把三档归一化再比较（与看板 rcNorm 一致）。Brier = 三档平方误差之和（与看板 rcBrierOne 一致）。
  · 共识 = 各可比平台归一化概率的平均。共识热门 = 共识概率最高的结果。
  · 冠军概率走势做了清洗（仅用于分析，不影响回放页）：
      - 串盘污染：某平台价格偏离其他平台中位数 >20pt（多为其他运动「World Cup」盘同名队）→ 剔除
      - 过期报价：超过 5 天未更新 → 视为下架
      - 被淘汰后夺冠概率记 0，决赛结束后冠军记 100%（事实，而非市场报价）
"""
from __future__ import annotations

import bisect
import json
import re
import statistics
from pathlib import Path

KO = ("R32", "R16", "QF", "SF", "3P", "FIN")
PL = ["polymarket", "kalshi", "42", "manifold", "predict"]
STALE = 5 * 86400
OUTLIER = 0.20


def _norm(o):
    if not o or len(o) != 3 or any(x is None for x in o):
        return None
    s = sum(o)
    return [x / s for x in o] if s > 0 else None


def _brier(p, r):
    return sum((p[i] - (1.0 if i == r else 0.0)) ** 2 for i in range(3))


def _ri(res):
    return {"A": 0, "D": 1, "B": 2}[res]


def _stage(grp):
    return "group" if grp not in KO else grp


def _dec_champ(archive):
    S, t0, out = archive["series"]["champion"], archive["t0"], {}
    for k, p, tsd, vals in S["series"]:
        ts, acc = [], t0
        for d in tsd:
            acc += d
            ts.append(acc)
        pr = [v / 10000 if isinstance(v, int) else v for v in vals]
        out.setdefault(S["keys"][k], {})[S["plats"][p]] = (ts, pr)
    return out


def build(H, N, series, games, archive):
    lag = archive["finishLag"]
    G = [g for g in games if g["result"]]
    wc = set(archive["wc48"])
    champ = _dec_champ({"series": series, "t0": archive["t0"]})

    # ───────── 名次 / 淘汰时刻 ─────────
    fin = next(g for g in G if g["grp"] == "FIN")
    p3 = next((g for g in G if g["grp"] == "3P"), None)
    win = lambda g: g["home"] if g["result"] == "A" else (g["away"] if g["result"] == "B" else None)
    lose = lambda g: g["away"] if g["result"] == "A" else (g["home"] if g["result"] == "B" else None)

    NEXT = {"R32": "R16", "R16": "QF", "QF": "SF", "SF": "FIN"}

    def ko_winner(g):                     # 淘汰赛平局（点球决胜）→ 看谁出现在「下一轮」
        w = win(g)
        if w:
            return w
        nxt = NEXT.get(g["grp"])
        if nxt:
            for t in (g["home"], g["away"]):
                if any(t in (x["home"], x["away"]) for x in G if x["grp"] == nxt):
                    return t
        return None                       # 季军赛/决赛点球：比分无法判定，下方另行处理

    champion = ko_winner(fin)
    runner = fin["away"] if champion == fin["home"] else fin["home"]
    third = ko_winner(p3) if p3 else None
    fourth = (p3["away"] if third == p3["home"] else p3["home"]) if p3 else None
    fin_end = fin["kickoff"] + lag

    groups: dict[str, dict] = {}
    for g in G:
        if g["grp"] in KO:
            continue
        for side, gf, ga in ((g["home"], g["sa"], g["sb"]), (g["away"], g["sb"], g["sa"])):
            t = groups.setdefault(g["grp"], {}).setdefault(side, {"team": side, "p": 0, "gf": 0, "ga": 0, "w": 0, "d": 0, "l": 0})
            t["gf"] += gf; t["ga"] += ga
            if gf > ga: t["p"] += 3; t["w"] += 1
            elif gf == ga: t["p"] += 1; t["d"] += 1
            else: t["l"] += 1
    r32 = {t for g in G if g["grp"] == "R32" for t in (g["home"], g["away"])}
    group_end_all = max(g["kickoff"] for g in G if g["grp"] not in KO) + lag
    standings, elim = {}, {}
    for grp, tm in sorted(groups.items()):
        rows = sorted(tm.values(), key=lambda r: (-r["p"], -(r["gf"] - r["ga"]), -r["gf"], r["team"]))
        gend = max(g["kickoff"] for g in G if g["grp"] == grp) + lag
        for pos, r in enumerate(rows, 1):
            r["pos"] = pos; r["q"] = r["team"] in r32
            if not r["q"]:
                elim[r["team"]] = gend if pos == 4 else group_end_all
        standings[grp] = rows
    reached = {}
    for g in sorted(G, key=lambda x: x["kickoff"]):
        if g["grp"] not in KO:
            continue
        for t in (g["home"], g["away"]):
            reached[t] = g["grp"] if g["grp"] != "3P" else "SF"
        if g["grp"] in ("R32", "R16", "QF"):
            w = ko_winner(g)
            l = g["away"] if w == g["home"] else g["home"]
            elim[l] = g["kickoff"] + lag
        if g["grp"] == "SF":
            w = ko_winner(g)
            elim[g["away"] if w == g["home"] else g["home"]] = g["kickoff"] + lag   # 失去夺冠可能
    elim[runner] = fin_end

    # ───────── 清洗后的冠军共识（30 分钟网格，带连续性） ─────────
    #  ≥3 个报价：剔除偏离中位数 >20pt 者；
    #  恰好 2 个且相差 >20pt：无法靠中位数判断，保留与上一时刻共识更接近者（真实概率不会凭空 0.1%→100%）。
    STEP = 1800
    t_first = min(ts[0] for plats in champ.values() for ts, _ in plats.values())
    g0 = t_first - t_first % STEP
    g1 = fin_end + 86400
    CLEAN: dict[str, list] = {}
    for team in wc:
        plats = champ.get(team, {})
        prev, out = None, []
        for t in range(g0, g1 + 1, STEP):
            vals = []
            for p, (ts, pr) in plats.items():
                i = bisect.bisect_right(ts, t) - 1
                if i >= 0 and t - ts[i] <= STALE:
                    vals.append((p, pr[i]))
            if not vals:
                out.append((None, [], vals)); continue
            kept = vals
            if len(vals) >= 3:
                med = statistics.median(v for _, v in vals)
                kept = [(p, v) for p, v in vals if abs(v - med) <= OUTLIER] or vals
            elif len(vals) == 2 and abs(vals[0][1] - vals[1][1]) > OUTLIER:
                ref = prev if prev is not None else min(v for _, v in vals)
                kept = [min(vals, key=lambda x: abs(x[1] - ref))]
            cv = sum(v for _, v in kept) / len(kept)
            prev = cv
            out.append((cv, kept, vals))
        CLEAN[team] = out

    def cons_raw(team, t):
        i = (t - g0) // STEP
        seq = CLEAN.get(team)
        if not seq or i < 0:
            return None, []
        i = min(i, len(seq) - 1)
        return seq[i][0], seq[i][1]

    def all_vals(team, t):
        i = min(max((t - g0) // STEP, 0), len(CLEAN[team]) - 1)
        return CLEAN[team][i][2]

    def cons(team, t):
        if team in elim and t >= elim[team]:
            return 0.0
        if team == champion and t >= fin_end:
            return 1.0
        return cons_raw(team, t)[0]

    t_start = (t_first // 3600 + 1) * 3600
    grid = list(range(t_start, fin_end, 3 * 3600))
    grid += [fin["kickoff"] - 1800, fin_end, fin_end + 3 * 3600]    # 决赛前、终场、赛后：保证画出最终结果
    grid = sorted(set(grid))
    race, bar = {}, {}
    for team in wc:
        pts = [cons(team, t) for t in grid]
        mx = max((x or 0) for x in pts)
        vals = [None if x is None else round(x * 100, 2) for x in pts]
        if mx >= 0.05 or team == champion:
            race[team] = vals
        if mx >= 0.01:                      # 动态条形图：曾经 ≥1% 的球队都参与排名
            bar[team] = vals
    start_rank = sorted(((cons(t, t_start) or 0, t) for t in wc), reverse=True)
    start_top = [{"team": t, "p": round(v * 100, 1)} for v, t in start_rank[:10]]
    champ_start_rank = next(i for i, (_, t) in enumerate(start_rank, 1) if t == champion)

    # ───────── 单场准确度 ─────────
    acc = {"all": [0, 0], "group": [0, 0], "R32": [0, 0], "R16": [0, 0], "QF+": [0, 0]}
    br = {}                 # platform -> stage -> [sum, n]
    rel = {p: [[0, 0.0, 0] for _ in range(10)] for p in PL + ["consensus"]}
    rel_out = [[[0, 0.0, 0] for _ in range(10)] for _ in range(3)]
    draw_p, draw_n, n_priced = 0.0, 0, 0
    upsets, sure, per_match = [], [], []
    plat_bias = {p: [0.0, 0] for p in PL}
    outlier_cnt = {p: 0 for p in PL}
    overround = {p: [0.0, 0] for p in PL}
    for g in G:
        odds = g.get("odds") or {}
        cmp_ = {p: _norm(o) for p, o in odds.items()}
        cmp_ = {p: v for p, v in cmp_.items() if v}
        if not cmp_:
            continue
        n_priced += 1
        r = _ri(g["result"])
        c = [sum(v[i] for v in cmp_.values()) / len(cmp_) for i in range(3)]
        fav = c.index(max(c))
        hit = fav == r
        st = _stage(g["grp"])
        for key in ("all", "group" if st == "group" else ("QF+" if st in ("QF", "SF", "3P", "FIN") else st)):
            acc[key][0] += hit; acc[key][1] += 1
        stg = "group" if st == "group" else "ko"
        for p, v in list(cmp_.items()) + [("consensus", c)]:
            b = _brier(v, r)
            d = br.setdefault(p, {"all": [0, 0], "group": [0, 0], "ko": [0, 0]})
            for k in ("all", stg):
                d[k][0] += b; d[k][1] += 1
            if p in rel:
                for i in range(3):
                    bi = min(9, int(v[i] * 10))
                    rel[p][bi][0] += 1; rel[p][bi][1] += v[i]; rel[p][bi][2] += (1 if i == r else 0)
        for i in range(3):
            bi = min(9, int(c[i] * 10))
            rel_out[i][bi][0] += 1; rel_out[i][bi][1] += c[i]; rel_out[i][bi][2] += (1 if i == r else 0)
        for p, v in cmp_.items():
            plat_bias[p][0] += v[fav] - c[fav]; plat_bias[p][1] += 1
            o = odds[p]
            overround[p][0] += sum(o); overround[p][1] += 1
        if len(cmp_) >= 3:
            far = max(cmp_, key=lambda p: abs(cmp_[p][fav] - c[fav]))
            outlier_cnt[far] += 1
        draw_p += c[1]; draw_n += 1
        rec = {"home": g["home"], "away": g["away"], "sa": g["sa"], "sb": g["sb"], "grp": g["grp"],
               "kickoff": g["kickoff"], "result": g["result"], "p_actual": round(c[r] * 100, 1),
               "fav": ["A", "D", "B"][fav], "p_fav": round(c[fav] * 100, 1), "hit": hit,
               "n_plat": len(cmp_), "cons": [round(x * 100, 1) for x in c]}
        per_match.append(rec)
    upsets = sorted([m for m in per_match if not m["hit"]], key=lambda m: m["p_actual"])[:12]
    sure = sorted([m for m in per_match if m["hit"]], key=lambda m: -m["p_actual"])[:8]
    draws_actual = sum(1 for g in G if g["result"] == "D")
    # 平局实际发生率只算有定价的场次，与 draw_p 同样本
    priced_draws = sum(1 for m in per_match if m["result"] == "D")

    brier = []
    for p, d in br.items():
        if d["all"][1] < 5:
            continue
        brier.append({"p": p, "all": round(d["all"][0] / d["all"][1], 4), "n": d["all"][1],
                      "group": round(d["group"][0] / d["group"][1], 4) if d["group"][1] else None,
                      "ko": round(d["ko"][0] / d["ko"][1], 4) if d["ko"][1] else None,
                      "n_ko": d["ko"][1]})
    brier.sort(key=lambda x: x["all"])

    def rel_pack(bins):
        return [[round(b[1] / b[0] * 100, 1), round(b[2] / b[0] * 100, 1), b[0]] for b in bins if b[0]]

    # ───────── 球队：市场预期 vs 实际（与看板 tcAgg 同公式） ─────────
    teams = {}
    for m in per_match:
        g = next(x for x in G if x["home"] == m["home"] and x["away"] == m["away"] and x["kickoff"] == m["kickoff"])
        c = [x / 100 for x in m["cons"]]
        fav = c.index(max(c))
        for name, opp, w_, gf, ga, pts, my in ((g["home"], g["away"], c[0], g["sa"], g["sb"], 3 if g["result"] == "A" else (1 if g["result"] == "D" else 0), 0),
                                              (g["away"], g["home"], c[2], g["sb"], g["sa"], 3 if g["result"] == "B" else (1 if g["result"] == "D" else 0), 2)):
            t = teams.setdefault(name, {"team": name, "n": 0, "mkt": 0.0, "pts": 0, "gf": 0, "ga": 0, "w": 0, "d": 0, "l": 0, "ups": 0, "big": None, "miss": None})
            t["n"] += 1; t["mkt"] += w_; t["pts"] += pts; t["gf"] += gf; t["ga"] += ga
            t["w" if pts == 3 else ("d" if pts == 1 else "l")] += 1
            favored = fav == my
            if not favored and pts > 0:
                t["ups"] += 1
                if not t["big"] or w_ < t["big"]["win"]:
                    t["big"] = {"win": round(w_ * 100, 1), "opp": opp, "score": f"{gf}–{ga}"}
            if favored and pts < 3 and (not t["miss"] or w_ * 100 > t["miss"]["win"]):
                t["miss"] = {"win": round(w_ * 100, 1), "opp": opp, "score": f"{gf}–{ga}"}
    team_rows = []
    for t in teams.values():
        mk = round(t["mkt"] / t["n"] * 100); ac = round(t["pts"] / (t["n"] * 3) * 100); gap = ac - mk
        sur = round(min(99, 38 + abs(gap) * 1.2 + t["ups"] * 6))
        place = {champion: 1, runner: 2, third: 3, fourth: 4}.get(t["team"])
        team_rows.append({**t, "mkt": mk, "act": ac, "gap": gap, "surprise": sur,
                          "tier": "S" if sur >= 85 else "A" if sur >= 72 else "B" if sur >= 58 else "C",
                          "reached": reached.get(t["team"], "group"), "place": place,
                          "start_p": round((cons(t["team"], t_start) or 0) * 100, 1)})
    team_rows.sort(key=lambda r: -r["gap"])

    # ───────── 单场对夺冠概率的冲击 ─────────
    swings = []
    for g in G:
        for tm in (g["home"], g["away"]):
            b, a = cons(tm, g["kickoff"] - 3600), cons(tm, g["kickoff"] + lag + 3 * 3600)
            if b is None or a is None:
                continue
            d = (a - b) * 100
            if abs(d) >= 1:
                swings.append({"team": tm, "home": g["home"], "away": g["away"], "sa": g["sa"], "sb": g["sb"],
                               "grp": g["grp"], "kickoff": g["kickoff"], "before": round(b * 100, 1),
                               "after": round(a * 100, 1), "delta": round(d, 1)})
    swings.sort(key=lambda s: -abs(s["delta"]))

    # ───────── 平台画像 ─────────
    rows_by_plat = dict(H._db().execute("SELECT platform, COUNT(*) FROM snap GROUP BY platform").fetchall())
    div_acc = {p: [0.0, 0] for p in PL}
    pollution = {p: 0 for p in PL}
    for t in range(t_start, fin_end, 86400 // 2):
        for team in wc:
            cv, kept = cons_raw(team, t)
            if cv is None or (team in elim and t >= elim[team]):
                continue
            allv = all_vals(team, t)
            keptp = {p for p, _ in kept}
            for p, v in allv:
                if p not in keptp:                    # 串盘/离群报价：对所有球队计数（被串盘的多是冷门队）
                    pollution[p] = pollution.get(p, 0) + 1
                elif p in div_acc and cv >= 0.03:     # 偏离度只看有意义的争冠球队
                    div_acc[p][0] += abs(v - cv) * 100; div_acc[p][1] += 1
    platforms = []
    for p in PL:
        b = next((x for x in brier if x["p"] == p), None)
        platforms.append({
            "p": p, "changes": rows_by_plat.get(p, 0),
            "matches": b["n"] if b else 0, "brier": b["all"] if b else None,
            "overround": round(overround[p][0] / overround[p][1], 1) if overround[p][1] else None,
            "fav_bias": round(plat_bias[p][0] / plat_bias[p][1] * 100, 2) if plat_bias[p][1] else None,
            "outlier": outlier_cnt[p],
            "champ_dev": round(div_acc[p][0] / div_acc[p][1], 2) if div_acc[p][1] else None,
            "pollution": pollution.get(p, 0),
        })

    # ───────── 比赛本身 ─────────
    goals = sum(g["sa"] + g["sb"] for g in G)
    grp_g = [g for g in G if g["grp"] not in KO]
    ko_g = [g for g in G if g["grp"] in KO]
    big = sorted(G, key=lambda g: (-abs(g["sa"] - g["sb"]), -(g["sa"] + g["sb"])))[:5]
    high = sorted(G, key=lambda g: -(g["sa"] + g["sb"]))[:5]
    slim = lambda g: {"home": g["home"], "away": g["away"], "sa": g["sa"], "sb": g["sb"], "grp": g["grp"], "kickoff": g["kickoff"]}
    ko_list = [dict(slim(g), result=g["result"], winner=ko_winner(g)) for g in sorted(ko_g, key=lambda x: (KO.index(x["grp"]), x["kickoff"]))]

    visits = archive["visits"]
    peak = max(visits, key=lambda v: v[1]) if visits else None

    # 中文名 / 国旗（取自看板，保证与原站一致）
    dash = (Path(__file__).resolve().parent.parent / "pm_watcher" / "dashboard.html").read_text(encoding="utf-8")
    cn = json.loads(re.search(r"const TC_CN=(\{.*?\});", dash).group(1))
    fl = json.loads(re.search(r"const TC_FLAG=(\{.*?\});", dash).group(1))
    all_teams = sorted({g["home"] for g in G} | {g["away"] for g in G})

    return {
        "overview": {
            "first": min(g["kickoff"] for g in G), "last": fin["kickoff"], "matches": len(G),
            "group_matches": len(grp_g), "ko_matches": len(ko_g), "teams": len(all_teams),
            "goals": goals, "gpm": round(goals / len(G), 2),
            "gpm_group": round(sum(g["sa"] + g["sb"] for g in grp_g) / len(grp_g), 2),
            "gpm_ko": round(sum(g["sa"] + g["sb"] for g in ko_g) / len(ko_g), 2),
            "draws": draws_actual,
            "champion": champion, "runner": runner, "third": third, "fourth": fourth,
            "final": slim(fin), "third_match": slim(p3) if p3 else None,
            "snap_rows": sum(rows_by_plat.values()), "platforms": len(PL),
            "tracking_from": archive["t0"], "tracking_to": archive["t1"],
            "priced_matches": n_priced,
            "visits": sum(v[1] for v in visits), "peak_day": peak,
        },
        "race": {"t": grid, "series": race, "bar": bar, "champion": champion, "start_top": start_top,
                 "champ_start_rank": champ_start_rank, "elim": {k: v for k, v in elim.items() if k in bar or k in race}},
        "accuracy": {
            "hit": {k: {"hit": v[0], "n": v[1], "rate": round(v[0] / v[1] * 100, 1) if v[1] else None} for k, v in acc.items()},
            "brier": brier,
            "reliability": {p: rel_pack(b) for p, b in rel.items()},
            "reliability_outcome": [rel_pack(b) for b in rel_out],
            "draw_implied": round(draw_p / draw_n * 100, 1) if draw_n else None,
            "draw_actual": round(priced_draws / draw_n * 100, 1) if draw_n else None,
        },
        "upsets": upsets, "sure": sure, "per_match": per_match,
        "teams": team_rows, "standings": standings, "swings": swings[:14],
        "platforms": platforms,
        "biggest_wins": [slim(g) for g in big], "highest_scoring": [slim(g) for g in high],
        "knockout": ko_list,
        "visits": visits,
        "names": {"cn": {t: cn.get(t, t) for t in all_teams}, "flag": {t: fl.get(t) for t in all_teams}},
    }
