"""
淘汰赛单场诊断探针 —— 在仓库根目录运行：  python probe_ko.py
（需要你的 .env / 代理可达各平台，和正常采集同一套环境）

它会逐平台抓 search_matches()，打印目标这场的：原始标题、_split_vs 解析出的两队、
每个结果的原始名 / 价格 / 归一后的 label / 会不会被 build_matchboard 写入。
据此就能判断每个平台是「市场没上架」「标题非 A vs B」还是「结果名带额外词被丢弃」。
"""
import asyncio

from pm_watcher import make_client, ALL_PLATFORMS
from pm_watcher.names import canonical_country
from pm_watcher.polymarket import _split_vs

# 想查的那场（用规范名；想换别的场改这里即可）
TARGET = {"England", "DR Congo"}
KW = ("england", "congo")   # 标题模糊命中关键词（小写）


async def main():
    clients = [make_client(p, live=True) for p in ALL_PLATFORMS]
    res = await asyncio.gather(*[c.search_matches() for c in clients],
                               return_exceptions=True)
    for p, r in zip(ALL_PLATFORMS, res):
        print("=" * 64)
        print("平台:", p)
        if isinstance(r, Exception):
            print("  抓取异常:", repr(r))
            continue
        hit = 0
        for m in r:
            sides = _split_vs(m.title or "")
            teams = {canonical_country(s) for s in sides} if sides else set()
            t = (m.title or "").lower()
            if (TARGET & teams) or all(k in t for k in KW):
                hit += 1
                print(f"  标题: {m.title!r}")
                print(f"      _split_vs -> {sides}  归一两队 -> {teams or '解析失败'}")
                for o in m.outcomes:
                    nm = (o.name or "").strip()
                    lab = "Draw" if nm.lower() in ("draw", "tie") else canonical_country(nm)
                    keep = (lab in teams) or (lab == "Draw")
                    print(f"        结果名={nm!r:32} 价={o.price}  归一={lab!r}  "
                          f"{'✓写入' if keep else '✗被丢弃'}")
        if not hit:
            print("  ✗ 没抓到这场 —— 市场可能尚未上架 / 标题非 'A vs B' / 不在 search_matches 返回里")
    print("=" * 64)
    print("说明：凡是 ✗被丢弃 的结果，就是因为结果名归一不回那两支队（多半带了 "
          "'to advance'/'wins'/'Yes' 之类额外词）；凡是整段没抓到，就是该平台这场还没数据。")


if __name__ == "__main__":
    asyncio.run(main())
