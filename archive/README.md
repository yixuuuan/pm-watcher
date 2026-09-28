# archive/ · 把实时看板变成永久的历史回放

2026 世界杯结束后，pm-watcher 从一个需要服务器常驻、每 30 秒抓取行情的实时服务，
变成了一个**纯静态网站**：不需要服务器、数据库或任何 API token，可以永久免费托管。

线上地址：**https://pmwatcher.wy-x.com/**

## 它是怎么工作的

原站的前端（`pm_watcher/dashboard.html`）每隔几秒请求 `/api/board`、`/api/recap`、
`/api/news`、`/api/history`、`/api/stats`。归档版不改前端逻辑，而是在页面里加了一层
**回放数据层**（`web/archive.js`）：

1. 读取 `data/archive.json`：赛事期间落盘的全部 20 万条定价变动记录、104 场赛果、每日访问量；
2. 按用户选定的时刻 T，用与 `serve.py` / `history.py` **完全相同的算法**，
   在浏览器里现场算出「T 时刻服务器会返回什么」，交给原前端渲染。

所以原站的每个板块（冠军榜、赛程、复盘、球队名片、淘汰赛对阵、平行宇宙冠军机、异动时间线、
弹幕……）都原样保留，只是「现在」变成了你选的历史时刻。

正确性：用原项目的 Python 代码在「截断到 T 的数据库」上直接运行，与浏览器版在 20 个时间点上
对拍了 11,808 项字段，**逐位一致**（包括四舍五入这种细节）。

有两处刻意的偏离：
- 盘口溢价只统计 48 支参赛队。早期混入的板球/球员盘结算后早已下架，但最后一笔价格会一直
  留在记录里，不能计入；
- 动量箭头（▲▼）只在最近一次刷新落在 90 秒内时显示，模拟原站「与上一次刷新相比」的语义。

## 文件

| 文件 | 作用 |
|---|---|
| `build_site.py` | 从数据库构建整个静态站点到 `site/` |
| `analyze.py` | 全程数据分析（由 build_site 调用），产出 `site/data/analysis.json` |
| `web/archive.js` / `web/archive.css` | 回放数据层 + 顶部时间轴控制栏 |
| `web/analysis.html` | 「2026 世界杯 · 全程数据复盘」子页 |
| `backfill_news.py` | 从互联网档案馆（Wayback Machine）补回赛事期间的新闻 |
| `news_archive.json` | 补回的新闻存档（原站新闻只存在内存里，没有落盘） |

## 重新构建

```bash
# 1)（可选）补回新闻：需要能访问 web.archive.org，国内需开代理
export https_proxy=http://127.0.0.1:7897 http_proxy=http://127.0.0.1:7897
python3 archive/backfill_news.py --check     # 自检网络
python3 archive/backfill_news.py             # 正式抓取，可中断重跑

# 2) 构建站点（默认读取仓库根目录的 history_railway.db，否则 history.db）
python3 archive/build_site.py --db history_railway.db --site-url https://pmwatcher.wy-x.com/

# 3) 本地预览
python3 -m http.server 8765 -d site          # 打开 http://localhost:8765
```

原始数据库（`*.db`）含访客 IP 的哈希，**不进公开仓库**，请自行私下备份。
公开仓库里只有 `site/data/` 中导出的定价、赛果与每日访问总数。

## 部署（Cloudflare Pages）

仓库连接 Cloudflare Pages：构建命令留空，输出目录填 `site`。之后每次 push 都会自动重新部署。

## 分享某一时刻

回放页的地址会带上时刻，例如 `https://pmwatcher.wy-x.com/?t=2026-06-25T2030Z`（UTC），
在任何时区打开看到的都是同一个时刻。
