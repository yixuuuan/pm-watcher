// pm-watcher 历史存档站的 Worker：静态文件照常由 assets 提供，只多一个实时访问量接口
//   GET  /api/visits → { n }   只读
//   POST /api/visits → { n }   计一次访问后返回（页面每次打开/刷新 POST 一次）
// n 是「存档上线之后」新增的访问量；页面会把它加到原站历史累计上显示。
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/api/visits") return visits(request, env);
    return env.ASSETS.fetch(request);
  },
};

async function visits(request, env) {
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
