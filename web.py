"""网页版：在本机启动一个只给自己用的网页服务器，浏览器打开 http://127.0.0.1:8765 使用。

安全：
- 只监听 127.0.0.1，局域网和外网都连不上
- 每次启动生成随机令牌，网页里带着它才能执行操作；别的网站即使在你浏览器里也拿不到令牌
- 检查 Host 头，防止 DNS 重绑定攻击
- 下单类操作在服务器端重新按规则计算，不信任网页传来的价格和股数上限
"""
import json
import os
import secrets
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from common import ROOT

PORT = 8765
LOCK_PORT = 47391  # 和桌面版共用：两个版本不能同时运行，否则自动规则会被执行两次
TOKEN = secrets.token_urlsafe(24)
WEB_DIR = os.path.join(ROOT, "web")
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


class Handler(BaseHTTPRequestHandler):
    engine = None
    updater = None

    def log_message(self, *a):  # 不在控制台刷屏
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        return self.headers.get("Host", "") in ALLOWED_HOSTS

    def _token_ok(self):
        return secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(WEB_DIR, "index.html"), encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", TOKEN)
            return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        if not self._token_ok():  # 读数据也要令牌：别的网站读不到你的账户
            return self._send(403, {"error": "bad token"})
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        e = self.engine
        try:
            if u.path == "/api/state":
                st = e.state(float(qs.get("since", 0)))
                st["version"] = self.updater.local_version() if self.updater else ""
                st["update"] = self.updater.status if self.updater else None
                return self._send(200, st)
            if u.path == "/api/protect/plan":
                return self._send(200, e.protect_plan())
            if u.path == "/api/buy/plan":
                return self._send(200, e.buy_plan(qs.get("code", "")))
            if u.path == "/api/journal":
                return self._send(200, e.journal_state())
            if u.path == "/api/report":
                r = next((p for p in e.state()["positions"] if p["code"] == qs.get("code")), None)
                return self._send(200 if r else 404, r or {"error": "没有这只持仓"})
        except ValueError as ex:
            return self._send(400, {"error": str(ex)})
        except Exception as ex:
            return self._send(500, {"error": str(ex)})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok() or not self._token_ok():
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except Exception:
            return self._send(400, {"error": "bad json"})
        e, path = self.engine, urlparse(self.path).path
        try:
            if path == "/api/watch/add":
                return self._send(200, {"code": e.add_stock(body.get("code"))})
            if path == "/api/watch/remove":
                e.remove_stock(body["code"])
            elif path == "/api/watch/lock":
                e.toggle_lock(body["code"])
            elif path == "/api/alert":
                e.set_alert(body["code"], body.get("above"), body.get("below"), body.get("pct"))
            elif path == "/api/option":
                e.set_option(body["key"], body["value"])
            elif path == "/api/screen":
                e.start_screen(force=True)
            elif path == "/api/backtest":
                e.start_backtest(force=True)
            elif path == "/api/rule/adopt":
                e.adopt_suggestion()
            elif path == "/api/rule/rollback":
                e.rollback_rule()
            elif path == "/api/rule/stop":
                e.stop_rule(body["id"])
            elif path == "/api/push/test":
                e.push_test()
            elif path == "/api/selfcheck":
                return self._send(200, {"result": e.self_check()})
            elif path == "/api/order/cancel":
                e.cancel_order(body["env"], body["order_id"])
            elif path == "/api/order/cancel_all":
                return self._send(200, {"fails": e.cancel_all()})
            elif path == "/api/protect/execute":
                if body.get("confirm") is not True:
                    return self._send(400, {"error": "需要确认"})
                return self._send(200, {"results": e.protect_execute(body.get("picks", []))})
            elif path == "/api/buy/execute":
                if body.get("confirm") is not True:
                    return self._send(400, {"error": "需要确认"})
                return self._send(200, {"result": e.buy_execute(body.get("code"), body.get("qty", 0))})
            elif path == "/api/update/apply":
                if not self.updater:
                    return self._send(400, {"error": "没有配置自动更新"})
                return self._send(200, {"result": self.updater.apply()})
            else:
                return self._send(404, {"error": "not found"})
        except (ValueError, KeyError) as ex:
            return self._send(400, {"error": str(ex)})
        except Exception as ex:
            return self._send(500, {"error": str(ex)})
        return self._send(200, {"ok": True})


def main(open_browser=True):
    lock = single_instance()
    url = f"http://127.0.0.1:{PORT}/"
    if lock is None:  # 已经在运行：通知它（网页版会打开网页，桌面版会把窗口调到前面）
        try:
            socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2).close()
        except OSError:
            pass
        print("盯盘程序已经在运行。若弹出的是桌面版窗口，请先关掉桌面版再运行网页版。")
        return
    from engine import Engine
    import updater
    engine = Engine()
    engine.start()
    Handler.engine = engine
    Handler.updater = updater.Updater(engine.log)
    Handler.updater.start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)

    def accept_show():  # 再次双击启动时：打开网页
        while True:
            try:
                c, _ = lock.accept()
                c.close()
                webbrowser.open(url)
            except OSError:
                return
    threading.Thread(target=accept_show, daemon=True).start()
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main(open_browser="--no-browser" not in sys.argv)
