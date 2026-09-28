/*
 * pm-watcher 历史回放数据层（archive.js）
 * ------------------------------------------------------------
 * 原站是一个实时服务：前端每隔几秒请求 /api/board、/api/recap、/api/news 等接口。
 * 世界杯结束后，本文件在浏览器里接管这些请求：读取 data/archive.json（全部定价变动记录、
 * 赛果、访问量）与 data/news.json，按用户选定的时刻 T，用与 serve.py / history.py
 * 完全相同的算法，现场算出「T 时刻服务器会返回什么」。前端渲染逻辑一行不改。
 *
 * 时间轴：顶部控制栏可选日期时间、拖动、跳到赛程节点、倍速播放。URL ?t= 可分享某一时刻。
 */
(function () {
  "use strict";

  // ───────── 嵌入在看板里的平行宇宙 iframe：直接借用父页面的回放状态 ─────────
  try {
    if (window.parent !== window && window.parent.PMW) {
      var P = window.parent.PMW;
      window.PMW = P;
      window.PMW_NOW = function () { return P.T * 1000; };
      var of0 = window.fetch ? window.fetch.bind(window) : function () { return Promise.reject(new Error("no fetch")); };
      window.fetch = function (input, init) {
        var h = P.route(input);
        return h ? h : of0(input, init);
      };
      return;
    }
  } catch (e) { /* 跨域等情况：退回独立模式 */ }

  var PLATS = ["polymarket", "kalshi", "42", "manifold", "predict"];
  var MOM_WIN = 90;          // 秒：最近一次刷新在此窗口内，才显示 ▲▼ 动量（原站每次刷新间隔约 30s）
  var HIST_N = 90;
  var NEWS_WIN = 72 * 3600;  // 新闻窗口：原站 RSS 源通常只保留最近一两天的条目           // 与 serve.py 相同：history 保留最近 90 次刷新
  var KO = ["R32", "R16", "QF", "SF", "3P", "FIN"];
  var KO_ORDER = { R32: 0, R16: 1, QF: 2, SF: 3, "3P": 4, FIN: 5 };

  var D = null;              // 解码后的数据
  var NEWS = [];
  var readyResolve;
  var ready = new Promise(function (r) { readyResolve = r; });

  var PMW = {
    T: 0, t0: 0, t1: 0, ready: ready, playing: false, speed: 600,
  };
  window.PMW = PMW;
  window.PMW_NOW = function () { return PMW.T * 1000; };

  // ───────── 工具 ─────────
  // 与 Python round(x, n) 逐位一致：按 double 的精确值做「四舍六入五成双」。
  // 远离进位临界点时走快速路径；接近 .5 时用 BigInt 精确计算。
  var _dv = new DataView(new ArrayBuffer(8));
  function pyRound(x, n) {
    if (x === 0 || !isFinite(x)) return x;
    var p = Math.pow(10, n), y = Math.abs(x) * p, f = y - Math.floor(y);
    if (Math.abs(f - 0.5) > 1e-7) return (x < 0 ? -1 : 1) * Math.floor(y + 0.5) / p;
    _dv.setFloat64(0, x);
    var hi = _dv.getUint32(0), lo = _dv.getUint32(4);
    var E = (hi >>> 20) & 0x7ff;
    var m = (BigInt(hi & 0xfffff) << 32n) | BigInt(lo);
    if (E) m |= (1n << 52n); else E = 1;
    var k = E - 1075, num = m * (10n ** BigInt(n)), q;
    if (k >= 0) q = num << BigInt(k);
    else {
      var den = 1n << BigInt(-k); q = num / den; var r2 = (num % den) * 2n;
      if (r2 > den || (r2 === den && (q & 1n))) q += 1n;
    }
    return (x < 0 ? -1 : 1) * Number(q) / p;
  }
  function dv(x) { return Number.isInteger(x) ? x / 10000 : x; }   // 数据包里的价格 → 0~1
  function idxLE(ts, T) {             // 最后一个 ts<=T 的下标，没有则 -1
    var lo = 0, hi = ts.length - 1, ans = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (ts[mid] <= T) { ans = mid; lo = mid + 1; } else hi = mid - 1;
    }
    return ans;
  }
  function idxLT(ts, T) { return idxLE(ts, T - 1); }   // 时间戳为整数秒
  function mean(a) { var s = 0; for (var i = 0; i < a.length; i++) s += a[i]; return s / a.length; }

  // ───────── 解码 ─────────
  function decode(A) {
    var d = { t0: A.t0, t1: A.t1, lag: A.finishLag, wc48: {}, scopes: {}, games: A.games, visits: A.visits };
    A.wc48.forEach(function (t) { d.wc48[t] = 1; });
    ["champion", "group", "match"].forEach(function (sc) {
      var S = A.series[sc], list = [], byKey = {};
      S.series.forEach(function (row) {
        var k = S.keys[row[0]], p = S.plats[row[1]], tsd = row[2], vals = row[3];
        var ts = new Array(tsd.length), acc = A.t0;
        for (var i = 0; i < tsd.length; i++) { acc += tsd[i]; ts[i] = acc; }
        var s = { k: k, p: p, ts: ts, v: vals };
        list.push(s);
        (byKey[k] = byKey[k] || []).push(s);
      });
      d.scopes[sc] = { list: list, byKey: byKey };
    });
    // 刷新时钟：champion 范围所有写入时刻（每次刷新整批同一 ts）
    ["champion", "group"].forEach(function (sc) {
      var set = {};
      d.scopes[sc].list.forEach(function (s) { s.ts.forEach(function (t) { set[t] = 1; }); });
      d.scopes[sc].polls = Object.keys(set).map(Number).sort(function (a, b) { return a - b; });
    });
    // 单场：按「排序后的对阵」归组，队伍顺序取最早出现的 key
    var games = {};
    d.games.forEach(function (g) {
      if (g.home && g.away) games[[g.home, g.away].sort().join("|")] = g;
    });
    var pairs = {};
    d.scopes.match.list.forEach(function (s) {
      var parts = s.k.split("|");
      if (parts.length !== 3) return;
      var pk = [parts[0], parts[1]].sort().join("|");
      var pr = pairs[pk];
      if (!pr) pr = pairs[pk] = { teams: [parts[0], parts[1]], first: s.ts[0], series: [] };
      if (s.ts[0] < pr.first) { pr.first = s.ts[0]; pr.teams = [parts[0], parts[1]]; }
      pr.series.push({ s: s, a: parts[0], b: parts[1], label: parts[2] });
    });
    d.pairs = [];
    Object.keys(pairs).forEach(function (pk) {
      var g = games[pk];
      if (!g) return;                               // 官方赛程白名单（history.fixture_pairs）
      pairs[pk].game = g;
      d.pairs.push(pairs[pk]);
    });
    d.pairByKey = {};
    d.pairs.forEach(function (p) { d.pairByKey[[p.teams[0], p.teams[1]].sort().join("|")] = p; });
    // 冠军异动事件（|Δ|≥1pt，仅 48 强），供新闻时间线
    var ev = [];
    d.scopes.champion.list.forEach(function (s) {
      if (!d.wc48[s.k]) return;
      for (var i = 1; i < s.ts.length; i++) {
        var dd = (dv(s.v[i]) - dv(s.v[i - 1])) * 100;
        if (Math.abs(dd) >= 1.0) ev.push({ ts: s.ts[i], team: s.k, delta: pyRound(dd, 2), plat: s.p, key: s.k + "|" + s.p + "|" + s.ts[i] });
      }
    });
    ev.sort(function (a, b) { return a.ts - b.ts; });
    d.events = ev;
    d.evTs = ev.map(function (e) { return e.ts; });
    // 冠军序列（history.champ_series 口径：只含 48 强，价格 0~1）
    d.champ = {};
    d.scopes.champion.list.filter(function (s) { return d.wc48[s.k]; })
      .sort(function (a, b) { return a.ts[0] - b.ts[0]; })
      .forEach(function (s) {
        (d.champ[s.k] = d.champ[s.k] || {})[s.p] = { ts: s.ts, pr: s.v.map(dv) };
      });
    var n = 0; ["champion", "group", "match"].forEach(function (sc) { d.scopes[sc].list.forEach(function (s) { n += s.ts.length; }); });
    d.nrows = n;
    return d;
  }

  // ───────── 榜单（serve._rows 口径） ─────────
  function boardAt(sc, T) {
    var b = {};
    D.scopes[sc].list.forEach(function (s) {
      var i = idxLE(s.ts, T);
      if (i < 0) return;
      (b[s.k] = b[s.k] || {})[s.p] = dv(s.v[i]);
    });
    return b;
  }
  function momAt(sc, T) {
    var polls = D.scopes[sc].polls, pi = idxLE(polls, T), m = {};
    if (pi < 0) return m;
    var last = polls[pi];
    if (T - last > MOM_WIN) return m;
    D.scopes[sc].list.forEach(function (s) {
      var i = idxLE(s.ts, T);
      if (i < 1 || s.ts[i] !== last) return;
      var d = dv(s.v[i]) - dv(s.v[i - 1]);
      if (Math.abs(d) >= 0.0005) (m[s.k] = m[s.k] || {})[s.p] = pyRound(d * 100, 2);
    });
    return m;
  }
  function rows(board, mom) {
    var out = [];
    Object.keys(board).forEach(function (team) {
      var by = board[team], present = {}, vals = [];
      PLATS.forEach(function (p) { if (by[p] != null) { present[p] = by[p]; vals.push(by[p]); } });
      if (!vals.length) return;
      var cons = mean(vals);
      var div = vals.length >= 2 ? Math.max.apply(null, vals) - Math.min.apply(null, vals) : 0;
      var low = null;
      // 平局时取平台列表里靠前者（与 Python min(dict) 一致；注意 JS 会把 "42" 这种数字键排到对象最前，不能用 Object.keys）
      if (vals.length >= 2) { var mn = Infinity; PLATS.forEach(function (p) { if (present[p] != null && present[p] < mn) { mn = present[p]; low = p; } }); }
      var pp = {}; Object.keys(present).forEach(function (p) { pp[p] = pyRound(present[p] * 100, 1); });
      out.push({ team: team, p: pp, consensus: pyRound(cons * 100, 1), divergence: pyRound(div * 100, 1), low: low, mom: (mom && mom[team]) || {} });
    });
    out.sort(function (a, b) { return b.consensus - a.consensus; });
    return out;
  }
  function consensusMap(board) {
    var c = {};
    Object.keys(board).forEach(function (k) {
      var vals = PLATS.map(function (p) { return board[k][p]; }).filter(function (v) { return v != null; });
      c[k] = pyRound((vals.length ? mean(vals) : 0) * 100, 1);
    });
    return c;
  }

  // ───────── 单场赛程板（build_matchboard + merge_knockout_fixtures 口径） ─────────
  function matchboardAt(T) {
    var out = [], seen = {};
    D.pairs.forEach(function (pr) {
      var g = pr.game;
      if (pr.first > T) return;                       // 盘口尚未出现
      if (T > g.kickoff + 4 * 3600) return;            // 早已结算下架
      var odds = {};
      pr.series.forEach(function (x) {
        var i = idxLE(x.s.ts, T);
        if (i < 0) return;
        if (x.label !== pr.teams[0] && x.label !== pr.teams[1] && x.label !== "Draw") return;
        var o = odds[x.s.p] = odds[x.s.p] || {};
        if (o[x.label] == null) o[x.label] = pyRound(dv(x.s.v[i]) * 100, 1);
      });
      if (!Object.keys(odds).length) return;
      var row = { teams: pr.teams.slice(), title: pr.teams[0] + " vs " + pr.teams[1], kickoff: g.kickoff, vol: 0.0, odds: odds };
      if (KO.indexOf(g.grp) >= 0) row.grp = g.grp;
      out.push(row);
      seen[[pr.teams[0], pr.teams[1]].sort().join("|")] = 1;
    });
    D.games.forEach(function (g) {                    // 对阵已定、尚未开盘的淘汰赛 → 待开盘
      if (KO.indexOf(g.grp) < 0) return;
      if (!(g.known_home <= T && g.known_away <= T)) return;
      if (T >= g.kickoff + D.lag) return;
      var k = [g.home, g.away].sort().join("|");
      if (seen[k]) return;
      out.push({ teams: [g.home, g.away], title: g.home + " vs " + g.away, kickoff: g.kickoff, vol: 0.0, odds: {}, pending: true, grp: g.grp });
    });
    out.sort(function (a, b) { return (a.kickoff || 9e18) - (b.kickoff || 9e18); });
    return out;
  }

  // ───────── 复盘（/api/recap 口径） ─────────
  function closingLine(h, a, kickoff, T) {
    var pr = D.pairByKey[[h, a].sort().join("|")], out = {};
    if (!pr) return out;
    var lim = Math.min(kickoff - 1, T);              // ts < kickoff 且不晚于回放时刻
    pr.series.forEach(function (x) {
      if (x.label !== h && x.label !== a && x.label !== "Draw") return;
      var i = idxLE(x.s.ts, lim);
      if (i < 0) return;
      (out[x.s.p] = out[x.s.p] || {})[x.label] = pyRound(dv(x.s.v[i]) * 100, 2);
    });
    return out;
  }
  function oddsArr(cl, h, a) {
    var o = {};
    Object.keys(cl).forEach(function (p) { var d = cl[p]; o[p] = [d[h] != null ? d[h] : null, d.Draw != null ? d.Draw : null, d[a] != null ? d[a] : null]; });
    return o;
  }
  function champMovers(home, away, kickoff, T) {
    var pre_h = 8 * 3600, post_lo = 2 * 3600, post_hi = 14 * 3600, min_pp = 0.5, out = [];
    [home, away].forEach(function (team) {
      var plats = D.champ[team];
      if (!plats) return;
      var ds = [], bs = [], as = [];
      Object.keys(plats).forEach(function (p) {
        var tsl = plats[p].ts, prl = plats[p].pr;
        var i = idxLE(tsl, Math.min(kickoff, T));
        if (i < 0 || tsl[i] < kickoff - pre_h) return;
        var j = idxLE(tsl, Math.min(kickoff + post_hi, T));
        if (j < 0 || tsl[j] < kickoff + post_lo) return;
        ds.push(prl[j] - prl[i]); bs.push(prl[i]); as.push(prl[j]);
      });
      if (!ds.length) return;
      var d = mean(ds) * 100;
      if (Math.abs(d) >= min_pp) out.push({ team: team, before: pyRound(mean(bs) * 100, 1), after: pyRound(mean(as) * 100, 1), delta: pyRound(d, 1) });
    });
    return out;
  }
  function recapAt(T) {
    var matches = [], ko = [];
    D.games.forEach(function (g) {
      if (T < g.kickoff + D.lag) return;             // 还没打完 / 未入库
      var cl = closingLine(g.home, g.away, g.kickoff, T);
      matches.push({ home: g.home, away: g.away, sa: g.sa, sb: g.sb, result: g.result, kickoff: g.kickoff,
        grp: g.grp, odds: oddsArr(cl, g.home, g.away), movers: champMovers(g.home, g.away, g.kickoff, T) });
    });
    matches.sort(function (a, b) { return a.kickoff - b.kickoff; });
    D.games.forEach(function (g) {
      if (KO.indexOf(g.grp) < 0) return;
      var hk = g.known_home <= T, ak = g.known_away <= T;
      if (!hk && !ak) return;                          // 两边都未定 → 当时尚未入库
      var h2 = hk ? g.home : "", a2 = ak ? g.away : "";
      var fin = T >= g.kickoff + D.lag;
      var cl = (h2 && a2) ? closingLine(h2, a2, g.kickoff, T) : {};
      ko.push({ home: h2, away: a2, sa: fin ? g.sa : null, sb: fin ? g.sb : null, result: fin ? g.result : null,
        kickoff: g.kickoff, round: g.grp, status: fin ? "FINISHED" : (T >= g.kickoff ? "IN_PLAY" : "TIMED"),
        odds: oddsArr(cl, h2, a2) });
    });
    ko.sort(function (a, b) { return (KO_ORDER[a.round] - KO_ORDER[b.round]) || (a.kickoff - b.kickoff); });
    return { matches: matches, ko: ko };
  }

  // ───────── 历史序列（/api/history 口径） ─────────
  function seriesAt(sc, key, since, T) {
    var out = {};
    (D.scopes[sc].byKey[key] || []).forEach(function (s) {
      var end = idxLE(s.ts, T), arr = [];
      if (end < 0) return;
      var start = 0;
      if (since) start = idxLE(s.ts, since - 1) + 1;
      for (var i = start; i <= end; i++) arr.push([s.ts[i], pyRound(dv(s.v[i]) * 100, 2)]);
      if (since) {
        var a = idxLE(s.ts, since - 1);
        if (a >= 0 && (!arr.length || arr[0][0] > since)) arr.unshift([since, pyRound(dv(s.v[a]) * 100, 2)]);
      }
      if (arr.length) out[s.p] = arr;
    });
    return out;
  }
  var statsCache = { T: null, v: null };
  function histStats(T) {
    if (statsCache.T === T) return statsCache.v;
    var n = 0;
    ["champion", "group", "match"].forEach(function (sc) { D.scopes[sc].list.forEach(function (s) { n += idxLE(s.ts, T) + 1; }); });
    var nr = D.games.filter(function (g) { return T >= g.kickoff + D.lag; }).length;
    statsCache = { T: T, v: { rows: n, since: D.t0, results: nr } };
    return statsCache.v;
  }

  // ───────── 访问量 / 新闻 ─────────
  function dayStr(T) { return new Date(T * 1000).toISOString().slice(0, 10); }
  function visitsAt() {                 // 访问量不随回放时刻变化：始终显示原站最终的累计值
    var total = 0, last = D.visits[D.visits.length - 1] || [null, 0, 0];
    D.visits.forEach(function (v) { total += v[1]; });
    return { total: total, today_hits: last[1], today_unique: last[2] };
  }
  function newsAt(T) {
    var out = [];
    for (var i = NEWS.length - 1; i >= 0 && out.length < 50; i--) {
      var x = NEWS[i];
      if (x[0] > T) continue;
      if (x[0] < T - NEWS_WIN) break;
      out.push({ source: x[1], title: x[2], url: x[3], ts: x[0], teams: x[4] || [], wc: !!x[5] });
    }
    return out;
  }

  // ───────── /api/board ─────────
  var boardCache = { T: null, v: null };
  function boardPayload(T) {
    if (boardCache.T === T) return boardCache.v;
    var champ = boardAt("champion", T), group = boardAt("group", T);
    var over = {};
    PLATS.forEach(function (p) {
      // 只算 48 强：早期混入的板球/球员盘结算后早已下架，但其最后一笔会一直留在记录里，不能计入
      var s = 0; Object.keys(champ).forEach(function (k) { if (D.wc48[k] && champ[k][p] != null) s += champ[k][p]; });
      over[p] = s ? pyRound(s * 100, 1) : null;
    });
    var polls = D.scopes.champion.polls, pi = idxLE(polls, T), hist = [];
    for (var i = Math.max(0, pi - HIST_N + 1); i <= pi; i++) hist.push({ ts: polls[i], c: consensusMap(boardAt("champion", polls[i])) });
    var v = {
      updated: pi >= 0 ? polls[pi] : T, platforms: PLATS.slice(), live: true, interval: 30,
      champion: rows(champ, momAt("champion", T)), group: rows(group, momAt("group", T)),
      matches: matchboardAt(T), overround: over, errors: {}, history: hist,
    };
    boardCache = { T: T, v: v };
    return v;
  }

  // ───────── 请求路由 ─────────
  function json(obj) {
    return new Response(JSON.stringify(obj), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
  }
  function handle(path, q) {
    var T = PMW.T;
    if (path === "/api/board") return boardPayload(T);
    if (path === "/api/recap") return recapAt(T);
    if (path === "/api/news") return { items: newsAt(T) };
    if (path === "/api/stats") return visitsAt(T);
    if (path === "/api/history") {
      var scope = q.get("scope") || "champion", teams = (q.get("teams") || "").split(",").filter(Boolean);
      var since = parseInt(q.get("since") || "0", 10) || null, ser = {};
      teams.forEach(function (t) { ser[t] = seriesAt(scope, t, since, T); });
      return { scope: scope, stats: histStats(T), series: ser };
    }
    return null;
  }
  PMW.route = function (input) {
    var url = typeof input === "string" ? input : (input && input.url) || "";
    var u;
    try { u = new URL(url, location.href); } catch (e) { return null; }
    if (u.origin !== location.origin) return null;
    var path = u.pathname.replace(/\/+$/, "");
    var m = path.match(/\/api\/(board|recap|news|stats|history)$/);
    if (!m) return null;
    return ready.then(function () {
      var r = handle("/api/" + m[1], u.searchParams);
      return r ? json(r) : new Response("not found", { status: 404 });
    });
  };
  var of = window.fetch.bind(window);
  window.fetch = function (input, init) { var h = PMW.route(input); return h ? h : of(input, init); };

  // 供看板 logMoves 使用：T 之前最近 60 条异动
  PMW.events = function () {
    if (!D) return [];
    var i = idxLE(D.evTs, PMW.T);
    return D.events.slice(Math.max(0, i - 59), i + 1);
  };
  PMW.metaText = function () {
    var zh = (typeof LANG !== "undefined" && LANG === "zh");
    var t = new Date(PMW.T * 1000).toLocaleString(zh ? "zh-CN" : "en-GB", { year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
    return (zh ? "历史回放 · " : "REPLAY · ") + t + (zh ? " · 当时的实时数据" : " · as it was live");
  };
  PMW.recapAt = recapAt; PMW.boardAt = boardPayload; PMW.seriesAt = seriesAt;   // 供分析页 / 测试复用

  // ───────── 时间轴控制栏（仅在看板页） ─────────
  var UI = {};
  function isZh() { return typeof LANG !== "undefined" && LANG === "zh"; }
  function tx(zh, en) { return isZh() ? zh : en; }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function toLocalInput(T) {
    var d = new Date(T * 1000);
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
  function fromLocalInput(s) { var d = new Date(s); return isNaN(d) ? null : Math.floor(d.getTime() / 1000); }
  function isoUTC(T) { var d = new Date(T * 1000).toISOString(); return d.slice(0, 13) + d.slice(14, 16) + "Z"; }   // 2026-06-25T2000Z：UTC，任何时区打开都是同一时刻

  function stages() {
    var G = D.games, first = function (r) { var x = G.filter(function (g) { return g.grp === r; }).map(function (g) { return g.kickoff; }); return x.length ? Math.min.apply(null, x) : null; };
    var grp = G.filter(function (g) { return KO.indexOf(g.grp) < 0; }).map(function (g) { return g.kickoff; });
    var list = [
      { id: "gend", zh: "小组赛收官", en: "Groups end", t: Math.max.apply(null, grp) + D.lag + 600 },
      { id: "R32", zh: "32强", en: "R32", t: first("R32") - 1800 },
      { id: "R16", zh: "16强", en: "R16", t: first("R16") - 1800 },
      { id: "QF", zh: "1/4决赛", en: "QF", t: first("QF") - 1800 },
      { id: "SF", zh: "半决赛", en: "SF", t: first("SF") - 1800 },
      { id: "FIN", zh: "决赛", en: "Final", t: first("FIN") - 1800 },
      { id: "end", zh: "终场后", en: "Full time", t: PMW.t1 },
    ];
    return list.filter(function (s) { return s.t; });
  }
  function stageNow(T) {
    var live = D.games.filter(function (g) { return T >= g.kickoff && T < g.kickoff + D.lag; });
    if (live.length) {
      var g = live[0];
      return tx("进行中 · ", "Live · ") + (typeof rcName === "function" ? rcName(g.home) + " vs " + rcName(g.away) : g.home + " vs " + g.away);
    }
    var names = { R32: ["32强", "Round of 32"], R16: ["16强", "Round of 16"], QF: ["1/4决赛", "Quarter-finals"], SF: ["半决赛", "Semi-finals"], "3P": ["季军赛", "3rd place"], FIN: ["决赛", "Final"] };
    var next = D.games.filter(function (g) { return g.kickoff > T; }).sort(function (a, b) { return a.kickoff - b.kickoff; })[0];
    if (!next) return tx("赛事已结束", "Tournament over");
    var st = KO.indexOf(next.grp) >= 0 ? names[next.grp] : ["小组赛", "Group stage"];
    return tx(st[0], st[1]);
  }

  function buildBar() {
    var bar = document.createElement("div");
    bar.id = "pmwbar";
    bar.innerHTML =
      '<div class="pb-row pb-main">' +
        '<span class="pb-badge" id="pbBadge"><i class="pb-rec"></i><span id="pbBadgeT"></span></span>' +
        '<div class="pb-clock" id="pbClock"><div class="pb-time" id="pbTime"></div><div class="pb-stage" id="pbStage"></div></div>' +
        '<div class="pb-nav" id="pbNav">' +
          '<button class="pb-btn" id="pbBack" title="-1h">−1h</button>' +
          '<input type="datetime-local" id="pbInput">' +
          '<button class="pb-btn" id="pbFwd" title="+1h">+1h</button>' +
        '</div>' +
        '<button class="pb-play" id="pbPlay"><span class="pb-ico" aria-hidden="true"></span><span class="pb-lbl" id="pbPlayL"></span></button>' +
        '<select id="pbSpeed"></select>' +
        '<span class="pb-flex"></span>' +
        '<button class="pb-help" id="pbHelp"></button>' +
        '<a class="pb-ana" id="pbAna" href="analysis.html"></a>' +
        '<a class="pb-nut" href="https://buymeacoffee.com/yixuuuan" target="_blank" rel="noopener" title="Buy me a chestnut">🌰</a>' +
      '</div>' +
      '<div class="pb-row pb-row2"><div class="pb-track" id="pbTrack"><div class="pb-ticks" id="pbTicks"></div><input type="range" id="pbRange"></div></div>' +
      '<div class="pb-row pb-chips" id="pbChips"></div>';
    document.body.insertBefore(bar, document.body.firstChild);
    UI.input = bar.querySelector("#pbInput");
    UI.range = bar.querySelector("#pbRange");
    UI.play = bar.querySelector("#pbPlay");
    UI.speed = bar.querySelector("#pbSpeed");
    UI.range.min = PMW.t0; UI.range.max = PMW.t1; UI.range.step = 60;
    UI.input.min = toLocalInput(PMW.t0); UI.input.max = toLocalInput(PMW.t1);

    UI.input.addEventListener("change", function () { var t = fromLocalInput(UI.input.value); if (t) setT(t, "jump"); });
    UI.range.addEventListener("input", function () { setT(+UI.range.value, "drag"); });
    UI.range.addEventListener("change", function () { setT(+UI.range.value, "jump"); });
    bar.querySelector("#pbBack").addEventListener("click", function () { setT(PMW.T - 3600, "jump"); });
    bar.querySelector("#pbFwd").addEventListener("click", function () { setT(PMW.T + 3600, "jump"); });
    UI.play.addEventListener("click", function () { PMW.playing ? stop() : play(); try { localStorage.setItem("pmw_played", "1"); } catch (e) {} });
    bar.querySelector("#pbHelp").addEventListener("click", function () { tour(0); });
    UI.speed.addEventListener("change", function () { PMW.speed = +UI.speed.value; });
    bar.querySelector("#pbChips").addEventListener("click", function (e) {
      var b = e.target.closest("[data-t]"); if (b) { stop(); setT(+b.dataset.t, "jump"); }
    });
    // 语言切换后刷新控制栏文字
    document.addEventListener("click", function (e) { if (e.target.closest("[data-lang]")) setTimeout(labels, 0); });
    // 「全屏打开」平行宇宙：带上当前回放时刻
    document.addEventListener("click", function (e) {
      var a = e.target.closest('a[href*="parallel-universe.html"]');
      if (!a) return;
      var u = new URL(a.getAttribute("href"), location.href);
      u.searchParams.set("t", String(PMW.T)); a.href = u.pathname.replace(/^\//, "") + u.search;
    }, true);
    labels(); sync();
    var place = function () {                     // 记录控制栏高度，供弹幕开关/飘屏区避让
      var mobile = window.matchMedia && window.matchMedia("(max-width:640px)").matches;
      document.documentElement.style.setProperty("--pmwbar-h", (mobile ? 52 : bar.offsetHeight) + "px");
    };
    place(); window.addEventListener("resize", place);
    UI.place = place;
  }
  function labels() {
    if (!UI.play) return;
    document.getElementById("pbBadgeT").textContent = tx("历史回放", "REPLAY");
    document.getElementById("pbPlayL").textContent = PMW.playing ? tx("暂停", "Pause") : tx("播放", "Play");
    UI.play.classList.toggle("on", !!PMW.playing);
    document.getElementById("pbHelp").innerHTML = '<b>?</b><span>' + tx("使用指引", "Guide") + "</span>";
    var sp = [[60, "1分钟/秒", "1 min/s"], [600, "10分钟/秒", "10 min/s"], [3600, "1小时/秒", "1 h/s"], [21600, "6小时/秒", "6 h/s"]];
    UI.speed.innerHTML = sp.map(function (s) { return '<option value="' + s[0] + '"' + (s[0] === PMW.speed ? " selected" : "") + ">" + tx(s[1], s[2]) + "</option>"; }).join("");
    document.getElementById("pbAna").textContent = tx("📊 全程数据分析 →", "📊 Tournament analysis →");
    var st = stages(), span = PMW.t1 - PMW.t0;
    document.getElementById("pbChips").innerHTML = st.map(function (s) {
      return '<button class="pb-chip" data-t="' + s.t + '" data-sid="' + s.id + '">' + tx(s.zh, s.en) + "</button>";
    }).join("");
    document.getElementById("pbTicks").innerHTML = st.map(function (s) {
      return '<i style="left:' + ((s.t - PMW.t0) / span * 100).toFixed(2) + '%" title="' + tx(s.zh, s.en) + '"></i>';
    }).join("");
    sync();
    if (UI.place) UI.place();
  }
  function sync() {
    if (!UI.input) return;
    if (document.activeElement !== UI.input) UI.input.value = toLocalInput(PMW.T);
    if (document.activeElement !== UI.range || PMW.playing) UI.range.value = PMW.T;
    document.getElementById("pbStage").textContent = stageNow(PMW.T);
    var d = new Date(PMW.T * 1000);
    document.getElementById("pbTime").innerHTML = '<span>' + d.getFullYear() + "." + pad(d.getMonth() + 1) + "." + pad(d.getDate()) + "</span> " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    UI.range.style.setProperty("--p", ((PMW.T - PMW.t0) / (PMW.t1 - PMW.t0) * 100).toFixed(2) + "%");
    var chips = document.querySelectorAll(".pb-chip"), cur = null;
    chips.forEach(function (c) { if (+c.dataset.t <= PMW.T + 1) cur = c; });
    chips.forEach(function (c) { c.classList.toggle("on", c === cur); });
  }

  var lastSig = null, lastUrl = 0, lastHist = 0, lastPu = 0, refreshTimer = null;
  function recapSigAt(T) {
    var n = 0, k = 0;
    D.games.forEach(function (g) { if (T >= g.kickoff + D.lag) n++; if (g.known_home <= T) k++; if (g.known_away <= T) k++; if (T >= g.kickoff) k += 100; });
    return n + ":" + k;
  }
  function refresh(kind) {
    var jump = kind === "jump";
    try { if (typeof tick === "function") tick(); } catch (e) {}
    var now = Date.now();
    if (jump || now - lastHist > 4000) { lastHist = now; try { if (typeof fetchHist === "function") fetchHist(); } catch (e) {} }
    var sig = recapSigAt(PMW.T);
    if (sig !== lastSig) {
      lastSig = sig;
      try { if (typeof refreshRecap === "function") refreshRecap(); } catch (e) {}
      if (jump || now - lastPu > 3000) {
        lastPu = now;
        var f = document.getElementById("puFrame");
        if (f) try { f.contentWindow.location.reload(); } catch (e) {}
      }
    } else if (jump) {
      var f2 = document.getElementById("puFrame");
      if (f2) try { f2.contentWindow.location.reload(); } catch (e) {}
    }
    if (jump) {                                        // 跳转：清空弹幕进度，避免把"未来"的新闻当成新消息
      try { SEEN_NEWS = new Set(); dmQueue = []; dmFirst = true; dmCycle = 0; } catch (e) {}
    }
    var sv = document.getElementById("sfViews");
    if (sv) { var v = visitsAt(PMW.T); sv.innerHTML = "👀 <b>" + (v.total || 0).toLocaleString("en-US") + "</b> " + tx("累计访问", "total views"); }
    if (jump || now - lastUrl > 1500) {
      lastUrl = now;
      var u = new URL(location.href); u.searchParams.set("t", isoUTC(PMW.T)); u.searchParams.delete("m");
      try { history.replaceState(null, "", u.pathname + u.search + u.hash); } catch (e) {}
    }
  }
  function setT(t, kind) {
    t = Math.round(Math.min(Math.max(t, PMW.t0), PMW.t1));
    if (kind !== "play" && PMW.playing) stop();
    PMW.T = t;
    sync();
    if (kind === "drag") {                           // 拖动中：节流刷新
      clearTimeout(refreshTimer);
      refreshTimer = setTimeout(function () { refresh("drag"); }, 120);
      return;
    }
    refresh(kind);
  }
  var playTimer = null;
  function play() {
    if (PMW.T >= PMW.t1 - 60) {                    // 在终点按播放：从开幕前重播，并默认用 1 小时/秒（10 分钟/秒要看两个小时）
      var k0 = Math.min.apply(null, D.games.map(function (g) { return g.kickoff; }));
      setT(Math.max(PMW.t0, k0 - 3 * 3600), "jump");
      if (PMW.speed === 600) PMW.speed = 3600;
    }
    PMW.playing = true; labels();
    var last = Date.now();
    playTimer = setInterval(function () {
      var now = Date.now(), dt = (now - last) / 1000; last = now;
      var t = PMW.T + PMW.speed * dt;
      if (t >= PMW.t1) { setT(PMW.t1, "play"); stop(); return; }
      setT(t, "play");
    }, 500);
  }
  function stop() { PMW.playing = false; clearInterval(playTimer); playTimer = null; labels(); }
  PMW.setT = setT;


  // ───────── 直接定位到某场比赛的复盘小卡（?m=主队|客队） ─────────
  function focusMatch(mk) {
    var tries = 0;
    (function attempt() {
      tries++;
      try { if (typeof TAB !== "undefined" && TAB !== "recap") { var tb = document.querySelector('[data-tab="recap"]'); if (tb) tb.click(); } } catch (e) {}
      var btn = null;
      document.querySelectorAll("#recap [data-rcexport]").forEach(function (b) {
        var v = b.getAttribute("data-rcexport");
        if (v === mk || v === mk.split("|").reverse().join("|")) btn = b;
      });
      if (!btn) { if (tries < 40) setTimeout(attempt, 250); return; }
      var card = btn.closest(".node") || btn.parentElement;
      var sec = document.getElementById("rcmatch");
      if (sec && sec.tagName === "DETAILS") sec.open = true;
      var panel = card.closest("[data-rcdaypanel]");
      if (panel) {
        var i = panel.getAttribute("data-rcdaypanel");
        var tab = (sec || document).querySelector('[data-rcdaytab="' + i + '"]');
        if (tab && !tab.classList.contains("on")) tab.click();
      }
      setTimeout(function () {
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("pmw-focus");
        setTimeout(function () { card.classList.remove("pmw-focus"); }, 4200);
      }, 120);
    })();
  }
  PMW.focusMatch = focusMatch;


  // ───────── 使用指引：首次进入自动播放，之后可点「？使用指引」重看 ─────────
  var TOUR = null;
  function tourSteps() {
    return [
      { sel: null,
        t: tx("欢迎来到 pm-watcher 时光机", "Welcome to the pm-watcher time machine"),
        b: tx("2026 世界杯期间，这个看板实时追踪了 <b>5 个预测市场</b>对每支球队、每场比赛的定价，前后记录了 <b>20 万次</b>价格变动。<br>赛事已经结束，它变成了一份可回放的历史档案——你可以回到任意时刻，看到当时屏幕上的真实数据。",
              "During the 2026 World Cup this dashboard tracked how <b>5 prediction markets</b> priced every team and match — over <b>200,000</b> price changes.<br>The tournament is over; now it is a replayable archive. Jump to any moment and see exactly what the live board showed.") },
      { sel: "#pbClock,#pbNav",
        t: tx("① 选一个时刻", "① Pick a moment"),
        b: tx("这里显示你正在回看的时间。点日期可以直接选到分钟，或用 −1h / +1h 逐小时前后翻。", "This is the moment you are viewing. Pick any date and time, or step an hour back / forward.") },
      { sel: "#pbTrack,#pbChips",
        t: tx("② 拖动时间轴", "② Scrub the timeline"),
        b: tx("拖动滑块穿越整届赛事；下面的按钮一键跳到关键节点：小组赛收官、32 强……直到决赛终场。", "Drag across the whole tournament, or jump straight to key moments — end of groups, each knockout round, the final whistle.") },
      { sel: "#pbPlay,#pbSpeed", play: 1,
        t: tx("③ 按下播放", "③ Press play"),
        b: tx("赔率、榜单和新闻弹幕会像直播一样随时间流动。右边可以调速度：1 小时/秒约 24 秒看完一个比赛日。<br><em>现在就点一下试试 ▶</em>", "Odds, rankings and news start flowing as if live. Change the speed on the right — at 1 h/s a matchday takes about 24 seconds.<br><em>Try clicking it now ▶</em>") },
      { sel: ".tabs.row",
        t: tx("④ 切换视角", "④ Switch views"),
        b: tx("复盘看每场比赛前后市场怎么变；冠军榜看夺冠概率；赛程与淘汰赛看对阵与各平台赔率。它们都会跟着上面的时刻变化。", "Recap shows how markets moved around each match; Champion shows title odds; Matches and Knockout show fixtures and prices. All follow the moment you picked.") },
      { sel: "#pbAna",
        t: tx("⑤ 想看整届总结？", "⑤ Want the big picture?"),
        b: tx("这里是全程数据复盘：冠军概率赛跑、最大冷门、哪个平台最准……", "The tournament analysis: the title race, the biggest upsets, which platform was most accurate…") },
    ];
  }
  function tourEl() {
    if (TOUR) return TOUR;
    var m = document.createElement("div"); m.className = "pmw-tour";
    m.innerHTML = '<div class="pmw-spot"></div><div class="pmw-tip" role="dialog" aria-live="polite"></div>';
    document.body.appendChild(m);
    TOUR = { m: m, spot: m.firstChild, tip: m.lastChild, i: 0 };
    TOUR.tip.addEventListener("click", function (e) {
      var b = e.target.closest("[data-go]"); if (!b) return;
      var g = b.getAttribute("data-go");
      if (g === "end") tourEnd(); else tour(TOUR.i + (+g));
    });
    document.addEventListener("keydown", function (e) {
      if (!TOUR || !TOUR.m.classList.contains("on")) return;
      if (e.key === "Escape") tourEnd();
      if (e.key === "ArrowRight") tour(TOUR.i + 1);
      if (e.key === "ArrowLeft" && TOUR.i > 0) tour(TOUR.i - 1);
    });
    var re = function () { if (TOUR && TOUR.m.classList.contains("on")) tourPlace(); };
    window.addEventListener("resize", re); window.addEventListener("scroll", re, { passive: true });
    UI.play && UI.play.addEventListener("click", function () {   // 在第 ③ 步真的点了播放 → 自动进入下一步
      if (TOUR && TOUR.m.classList.contains("on") && tourSteps()[TOUR.i].play) setTimeout(function () { tour(TOUR.i + 1); }, 700);
    });
    return TOUR;
  }
  function tourRect(sel) {
    var r = null;
    sel.split(",").forEach(function (q) {
      var el = document.querySelector(q); if (!el || !el.offsetParent && el.tagName !== "BODY") return;
      var b = el.getBoundingClientRect(); if (!b.width) return;
      r = r ? { l: Math.min(r.l, b.left), t: Math.min(r.t, b.top), r: Math.max(r.r, b.right), b: Math.max(r.b, b.bottom) } : { l: b.left, t: b.top, r: b.right, b: b.bottom };
    });
    return r;
  }
  function tourPlace() {
    var st = tourSteps()[TOUR.i], spot = TOUR.spot, tip = TOUR.tip, vw = innerWidth, vh = innerHeight;
    var r = st.sel ? tourRect(st.sel) : null;
    TOUR.m.classList.toggle("center", !r);
    if (!r) { spot.style.cssText = "left:50%;top:50%;width:0;height:0"; tip.style.left = ""; tip.style.top = ""; return; }
    var pad = 8;
    spot.style.cssText = "left:" + (r.l - pad) + "px;top:" + (r.t - pad) + "px;width:" + (r.r - r.l + pad * 2) + "px;height:" + (r.b - r.t + pad * 2) + "px";
    var tw = tip.offsetWidth, th = tip.offsetHeight, x = Math.min(Math.max(12, (r.l + r.r) / 2 - tw / 2), vw - tw - 12);
    var y = r.b + pad + 14; if (y + th > vh - 12) y = Math.max(12, r.t - pad - 14 - th);
    tip.style.left = x + "px"; tip.style.top = y + "px";
    tip.style.setProperty("--ax", Math.min(Math.max(20, (r.l + r.r) / 2 - x), tw - 20) + "px");
    tip.classList.toggle("up", y < r.t);
  }
  function tour(i) {
    var S = tourSteps(); if (i < 0) i = 0; if (i >= S.length) return tourEnd();
    var T = tourEl(); T.i = i; var st = S[i], last = i === S.length - 1;
    var dots = S.map(function (_, k) { return '<i class="' + (k === i ? "on" : "") + '"></i>'; }).join("");
    T.tip.innerHTML =
      (i === 0 ? '<div class="pmw-hello"><span>Polymarket</span><span>Kalshi</span><span>42</span><span>Manifold</span><span>Predict</span></div>' : "") +
      '<div class="pmw-h">' + st.t + "</div><div class=\"pmw-b\">" + st.b + "</div>" +
      '<div class="pmw-f"><div class="pmw-dots">' + dots + "</div>" +
      '<button class="pmw-skip" data-go="end">' + (last ? "" : tx("跳过", "Skip")) + "</button>" +
      (i > 0 ? '<button class="pmw-prev" data-go="-1">' + tx("上一步", "Back") + "</button>" : "") +
      '<button class="pmw-next" data-go="' + (last ? "end" : "1") + '">' + (i === 0 ? tx("带我看看 →", "Show me →") : last ? tx("开始探索 ✓", "Start exploring ✓") : tx("下一步 →", "Next →")) + "</button></div>";
    T.m.classList.add("on");
    document.documentElement.classList.toggle("pmw-touring", true);
    if (st.sel) {                               // 目标不在视野里就先滚过去
      var el = document.querySelector(st.sel.split(",")[0]), b = el && el.getBoundingClientRect();
      if (b && (b.top < 0 || b.bottom > innerHeight - 180)) el.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    tourPlace(); setTimeout(tourPlace, 350); setTimeout(tourPlace, 700);
  }
  function tourEnd() {
    if (!TOUR) return;
    TOUR.m.classList.remove("on");
    document.documentElement.classList.remove("pmw-touring");
    try { localStorage.setItem("pmw_tour_v1", "1"); } catch (e) {}
  }
  PMW.tour = tour;

  function initBar() {
    if (!document.getElementById("meta")) return;    // 只有看板页才有控制栏
    lastSig = recapSigAt(PMW.T);
    buildBar();
    var mk = new URLSearchParams(location.search).get("m");
    if (mk) focusMatch(mk);
    if (!mk) setTimeout(function () { tour(0); }, 900);      // 每次进入看板都弹出使用指引（从分析页深链进来的不打扰）
  }
  ready.then(function () {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initBar);
    else initBar();
  });

  // ───────── 载入数据 ─────────
  function parseT(s) {                 // 支持：epoch 秒 / 2026-06-25T2000Z / 2026-06-25T20:00Z / 20260625T2000（无 Z 视为本地时间）
    if (!s) return null;
    s = String(s).trim();
    if (/^\d{9,11}$/.test(s)) return parseInt(s, 10);
    var m = s.match(/^(\d{4})-?(\d{2})-?(\d{2})[T ]?(\d{2}):?(\d{2})(?::?(\d{2}))?(Z)?$/i);
    if (m) {
      var a = [+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)];
      var ms = m[7] ? Date.UTC.apply(null, a) : new Date(a[0], a[1], a[2], a[3], a[4], a[5]).getTime();
      return Math.floor(ms / 1000);
    }
    var d = new Date(s);
    return isNaN(d) ? null : Math.floor(d.getTime() / 1000);
  }
  PMW.load = function (A, N) {
    D = decode(A); NEWS = N || [];
    var fin = D.games.filter(function (g) { return g.grp === "FIN"; })[0];
    var champLast = D.scopes.champion.polls[D.scopes.champion.polls.length - 1];
    PMW.t0 = D.t0;
    PMW.t1 = Math.max(champLast, fin ? fin.kickoff + D.lag : champLast) + 600;
    PMW.games = D.games; PMW.D = D;
    var qt = parseT(new URLSearchParams(location.search).get("t"));
    PMW.T = qt ? Math.min(Math.max(qt, PMW.t0), PMW.t1) : PMW.t1;
    readyResolve();
  };
  if (!window.PMW_NO_AUTOLOAD) {
    Promise.all([
      of("data/archive.json").then(function (r) { return r.json(); }),
      of("data/news.json").then(function (r) { return r.ok ? r.json() : []; }).catch(function () { return []; }),
    ]).then(function (x) { PMW.load(x[0], x[1]); }).catch(function (e) { console.error("回放数据载入失败", e); });
  }
})();
