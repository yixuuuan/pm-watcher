# Changelog

All notable changes to **pm-watcher** are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/); versions follow [SemVer](https://semver.org/).

中文摘要见每个版本末尾。

---

## [0.2.0] — 2026-06-24

The retrospective release: once a match is over, the board stops asking *"what will happen"* and starts asking *"who priced it right — and how wrong were the rest?"* Built around the guiding idea **calibration, not prediction (校准而非预测)**.

### Added
- **Match Recap board** — a per-match *priced-vs-actual* recap: the pre-match five-platform consensus set against the real Win / Draw / Loss result, with a hit-or-miss verdict.
- **Market-consensus record** pinned to the top of the recap — how often the consensus favourite actually delivered — with one-tap *delivered / fell-short* filtering.
- **Platform calibration** — a calibration chart plus a **Brier-score accuracy ranking** of the five platforms.
- **Shareable match headlines** — a one-line headline on every card, varied (not templated) and bilingual: an English headline with a Chinese sub-line in 中文 mode.
- **Horizontal date tabs** to jump to any match-day in the recap.
- **Recap card export** — one-tap 1080×1350 PNG carrying the headline band, the five-platform table, and a **QR code + link** back to the live board.
- **Team cards (球队名片)** — a collectible card for all **48 nations** in the official WC26-poster visual language; each nation has a **fixed colour + texture** (12 bold textures, no two nations alike), locked so only the data updates.
- **Surprise Index** (S / A / B / C, 0–100) and a giant **±% gap** number on each team card — how far the market's pricing missed that team's real results (it measures how wrong the market was, not a team's strength).
- **Team-card PNG export** with a QR deep-link straight to that team on the board; bilingual EN / 中文; gold stars for past champions.

### Changed
- News danmaku now **defaults to on** when the page is opened.

> 中文：本次为「复盘」版本。新增 **复盘看板**(赛前定价 vs 真实结果、市场共识战绩、平台校准 + Brier 排行、可传播标题、横向日期标签、带二维码的导出图)与 **48 国球队名片**(锁定的 WC26 视觉系统、意外指数、带二维码深链的导出图)。弹幕改为打开页面默认开启。

---

## [0.1.0] — 2026-06-16

First public version: one World Cup, five prediction markets, side by side.

### Added
- **Champion board** — title odds for all 48 teams across Polymarket, Kalshi, 42, Manifold and Predict.fun, with a consensus price and a divergence heatmap.
- **Group winner** — qualifying odds for all 12 groups.
- **Fixtures** — ~80 group-stage matches with a per-match five-platform Win / Draw / Loss comparison and the spread.
- **Live news × odds** — BBC / Guardian / ESPN / Sky football feed plus Dongqiudi, filtered by team; tap a story to see that team's per-platform odds for ±3 hours around it.
- **News danmaku** — recent and newly arrived headlines drift across the top as a bullet-screen; hover to pause, click to open, top-right button to toggle off.
- **Persisted history** — change-driven SQLite (`history.db`): nothing is written while a price holds steady.
- **Telegram alerts** — optional push when a cross-platform spread crosses a threshold.
- **Bilingual UI** — English / 中文 toggle.
- Field-level integration lessons for all five platforms documented in the README (42's bonding-curve price vs market-cap share, Kalshi's price-unit change, 42's WAF, Metaculus's closed API, Predict.fun's open GraphQL, score-market → three-way derivation, cross-platform name canonicalization).

> 中文：首版。五平台并排呈现 2026 世界杯定价 —— 冠军榜(含分歧热力)、小组第一、约 80 场赛程的五平台胜平负对比、新闻×赔率时间线、新闻弹幕、变动驱动的历史落盘、Telegram 推送、中英双语。

---

[0.2.0]: https://github.com/yixuuuan/pm-watcher/releases/tag/v0.2.0
[0.1.0]: https://github.com/yixuuuan/pm-watcher/releases/tag/v0.1.0
