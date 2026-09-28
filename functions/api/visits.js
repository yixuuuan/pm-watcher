// Cloudflare Pages Function：实时累计访问量
//   GET  /api/visits  → { n }        只读
//   POST /api/visits  → { n }        计一次访问后返回
// n 是「存档上线之后」新增的访问量；页面会把它加到原站历史累计（data/ 里的 visits）上显示。
// 需要在 Pages 项目里绑定一个 KV 命名空间，变量名 VISITS（见 archive/README.md）。没绑定时返回 n=0，页面照常显示历史值。
export async function onRequest({ request, env }) {
  const headers = { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" };
  const kv = env.VISITS;
  if (!kv) return new Response(JSON.stringify({ n: 0, live: false }), { headers });
  let n = 0;
  try { n = parseInt((await kv.get("total")) || "0", 10) || 0; } catch (e) {}
  if (request.method === "POST") {
    n += 1;
    try { await kv.put("total", String(n)); } catch (e) {}   // KV 免费额度每天 1000 次写入，超出时只是这次不计数
  }
  return new Response(JSON.stringify({ n, live: true }), { headers });
}
