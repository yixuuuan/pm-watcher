# pm-watcher · World Cup Cross-Platform Pricing Monitor

*by Yixuan · [中文版](./README.zh-CN.md)*

One World Cup, five prediction markets, five different answers. pm-watcher puts Polymarket, Kalshi, 42, Manifold and Predict.fun side by side as they price the 2026 World Cup in real time — so you can see where they agree, and where they diverge.

> A **read-only analysis tool.** It places no orders, connects no wallet, and needs no platform account. It answers *"what does the market think,"* not *"how do I bet."*

![Champion board with divergence heatmap](docs/board_en.png)

## What it shows

- **Champion board** — title odds for all 48 teams, five platforms side by side, plus a consensus price and a divergence heatmap (darker = the platforms disagree more)
- **Group winner** — qualifying odds for all 12 groups (live data from Kalshi and 42)
- **Fixtures** — a schedule of ~80 group-stage matches, auto-generated from the platforms' own markets; tap any match for a cross-platform Win / Draw / Loss comparison and the spread
- **Live news × odds** — a BBC / Guardian / ESPN / Sky football feed plus Dongqiudi (Chinese), filtered by team; **tap a story to see that team's per-platform odds for ±3 hours around it** (the dashed red line marks the news timestamp)
- **News danmaku** — recent and newly arrived headlines drift across the top as bullet-screen pills; hover to pause and read, click to open, and a top-right button toggles the stream off
- **Persisted history** — every odds change is written to a local SQLite file (change-driven: nothing is stored while a price holds steady). When the tournament ends, `history.db` is a complete record of how five markets priced 104 matches
- **Telegram alerts** — optional push when a cross-platform spread crosses your threshold
- **Bilingual UI** — English / 中文 toggle, top right

**A fixture expanded into a five-platform Win / Draw / Loss comparison:**

![Fixture detail across five platforms](docs/match_en.png)

**Tap a news story to see the two teams' odds around it; ⚡ marks consensus moves the monitor caught:**

![Live news and odds-move timeline](docs/news_en.png)

**News danmaku — headlines drift across the top like a bullet-screen; hovering a pill pauses it so you can read or click through, and the top-right button turns the stream off:**

![News danmaku bullet-screen](docs/danmaku_en.png)

**News × odds in one picture — a story's timestamp dropped onto each platform's price line for the hours around it, so the relationship between a headline and a price move is visible at a glance (a temporal relationship, not a causal claim):**

![News and odds correlation chart](docs/newsodds_en.png)

**A selectable watchlist — each card shows per-platform odds and a multi-platform history line:**

![Watchlist with per-team odds history](docs/watchlist_en.png)

## Quick start

```bash
pip install -r requirements.txt

# Web dashboard (recommended):
python3 -m pm_watcher.serve --live --interval 30
# then open http://127.0.0.1:8765

# Or command line:
python3 -m pm_watcher.watch --query "World Cup" --board --live
```

No API key required. Some data sources may need a proxy in certain network environments (`export HTTPS_PROXY=...`).

Telegram alerts (optional): copy `.env.example` to `.env`, add the token from @BotFather and your chat id, then pass `--notify` (CLI mode).

## The interesting part: the process

Cross-platform price comparison sounds like "put a few numbers next to each other." In practice every platform had a catch — and **these field-level lessons are worth more than the code**:

1. **42's `price` field is not a probability.** It is a bonding-curve token price (on the order of 0.0008); the outcomes don't sum to 1. The implied probability lives in the `marketCap` share — each outcome's market cap over the market total, which does sum to 100%. Use `price` directly and the entire column is wrong.
2. **Kalshi changed its price unit.** The legacy field was in cents (divide by 100); the newer `last_price_dollars` is already 0–1. Both appear in the response — read the wrong one and every probability is off by 100×.
3. **42's endpoint rejects requests that don't look like a browser.** The same URL opens in a browser but is refused for a bare client. Adding `Origin`, `Referer` and a real User-Agent resolves it. The WAF doesn't tell you what it wants.
4. **Metaculus's "public API" is no longer public.** It reads as usable in the docs; the actual response is `only available to authenticated users`. The only way to know whether an API works is to call it.
5. **Predict.fun's official API needs a key, but its web frontend uses an open GraphQL endpoint.** Introspection is enabled — following the schema surfaced a full World Cup fixtures interface (80 matches, real kickoff times). The richest source was the one outside the documentation.
6. **42's single-match markets are exact-score markets** (e.g. `NED 0–1 JPN`), not win/draw/loss. This project aggregates the score-level probabilities into a three-way price; outcomes it cannot classify are dropped honestly, so 42's three-way total can fall slightly below 100% rather than being force-normalized.
7. **One team has five different names across five platforms.** Türkiye/Turkey, Korea Republic/South Korea, Cabo Verde/Cape Verde, two spellings of Bosnia. Without canonicalization, a cross-platform comparison treats one team as two.

## A few observations (as the group stage opens)

- Top-of-board pricing is tightly aligned: Spain and France stay within ~0.5pt between Polymarket and Kalshi — the efficiency of deep books is visible.
- 42 runs systematically low: Portugal, Germany and Argentina sit roughly 3pt below Polymarket/Kalshi — a fingerprint of thin liquidity and a market structure that diverts probability mass to an N/A outcome.
- The gap between play-money (Manifold) and cash markets is itself a signal: play-money forecasters rate Spain higher; real money is more cautious.

## Architecture

```
pm_watcher/
├── model.py        # Market/Outcome models + client base class
├── polymarket.py   # Gamma API (public REST)
├── kalshi.py       # trade-api v2 (public REST; KXWCGAME matches / KXWCGROUPWIN groups)
├── fortytwo.py     # rest.ft.42.space (public REST; score market → three-way derivation)
├── manifold.py     # public REST (play-money forecaster consensus)
├── predict.py      # frontend public GraphQL (includes 80-match schedule)
├── names.py        # country-name canonicalization + 48-team list
├── aggregator.py   # cross-platform merge (boards / fixtures)
├── history.py      # SQLite change-driven persistence
├── news.py         # BBC/Guardian/ESPN/Sky RSS + Dongqiudi, filtered by World Cup / team
├── notifier.py     # Telegram push
├── serve.py        # local dashboard server (http://127.0.0.1:8765)
├── dashboard.html  # single-page dashboard (no frontend build step)
└── watch.py        # command-line mode
```

The only dependency is `httpx` (plus optional `python-dotenv`). The dashboard is a self-contained HTML file with no frontend build chain.

## Data sources

| Platform | Type | Access | Probability field | Note |
|---|---|---|---|---|
| Polymarket | cash (Polygon) | public Gamma REST | `outcomePrices` (0–1) | deepest liquidity |
| Kalshi | cash (CFTC-regulated) | public trade-api | `last_price_dollars` | draw is labelled "Tie" |
| 42 | on-chain (Alpha) | public REST | `marketCap` share | thin; single-match = score market |
| Manifold | **play-money** | public REST | `probability` | forecaster consensus, not real money |
| Predict.fun | cash (BNB) | frontend public GraphQL | `chancePercentage` | integer-grained (sub-0.5pt is noise) |

## Honest boundaries

- **This is not an arbitrage tool.** Most of the spreads it shows are not executable: fees, settlement differences, capital lock-up, the unfillability of thin books, and platform and contract risk all consume the nominal gap. Its value is **understanding how markets price an event**, not extracting money from one.
- Platform availability varies significantly by jurisdiction. Verify the law where you are and each platform's terms yourself. This project only reads public market data; it involves no registration, trading or funds.
- The news × odds chart shows a **temporal relationship, not a causal conclusion.**
- Nothing here is investment advice.

## License

MIT © Yixuan

*Built in collaboration with Claude (Anthropic).*
