#!/usr/bin/env python3
"""
临时数据恢复服务（一次性用）。
把 Railway 数据卷里的 history.db 通过一个带口令的只读链接暴露出来，方便取回。
取回成功后：把 Railway 的 Start Command 改回原来的（见 Procfile），并删除本文件。

用法（Railway）：
  1) 把本文件随仓库推上去（或已在仓库里）
  2) Railway → 服务 → Settings → Deploy → Start Command 临时改为：
        python recover_db.py
     （确认 Volume 仍挂载，通常在 /data）
  3) 触发一次 Deploy，等部署成功
  4) 浏览器打开：https://<你的服务域名>/dl/d4e542699e0fbf61
     （先打开根路径 / 可看到探测到的 db 路径与大小）
  5) 下载得到 history.db，发回给我
  6) 把 Start Command 改回、删除本文件
"""
import os
import glob
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = "d4e542699e0fbf61"


def find_db():
    cands = []
    env = os.environ.get("PMW_DB")
    if env:
        cands.append(env)
    cands += ["/data/history.db", "/app/data/history.db", "/mnt/data/history.db",
              "/data/pm-watcher/history.db"]
    for pat in ("/data/**/*.db", "/mnt/**/*.db", "/app/**/history.db"):
        cands += glob.glob(pat, recursive=True)
    best, best_sz, seen = None, -1, set()
    for p in cands:
        if not p or p in seen:
            continue
        seen.add(p)
        try:
            sz = os.path.getsize(p)
        except OSError:
            continue
        if sz > best_sz:
            best, best_sz = p, sz
    return best, best_sz


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        db, sz = find_db()
        if path == "/":
            body = "<h3>pm-watcher · 数据恢复</h3>"
            if db:
                mb = sz / 1048576
                body += f"<p>探测到数据库：<code>{db}</code>（{mb:.1f} MB）</p>"
                body += f'<p><a href="/dl/{TOKEN}">⬇ 下载 history.db</a></p>'
            else:
                body += "<p style='color:red'>未在 /data 等路径找到 .db 文件，请确认 Volume 挂载路径。</p>"
                found = glob.glob("/**/*.db", recursive=True)[:20]
                body += "<p>全盘扫描到的 .db：<br>" + "<br>".join(found) + "</p>"
            self._send(200, body.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == f"/dl/{TOKEN}" and db:
            try:
                data = open(db, "rb").read()
            except Exception as e:
                self._send(500, str(e).encode(), "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", "attachment; filename=history.db")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except Exception:
                pass
            return
        self._send(404, b"not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    print("recover server on :%d  db=%r" % (port, find_db()))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
