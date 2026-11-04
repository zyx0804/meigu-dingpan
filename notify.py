"""手机推送：通过 ntfy 把重要提醒发到手机（苹果 / 安卓装 ntfy App，订阅程序生成的主题名就能收到）。

- 主题名是程序随机生成的一长串字符，只有知道它的人能收到；推送内容只有股票、价格和提醒文字，不含账户号
- 后台线程发送，不会卡住盯盘；同一条 10 分钟内只发一次，每小时最多 30 条，防止刷屏
- 发送失败只记在状态里，不影响交易规则
"""
import json
import queue
import secrets
import threading
import time
import urllib.request

DEFAULT_SERVER = "https://ntfy.sh"
DEDUPE_SECS = 600
MAX_PER_HOUR = 30


def new_topic():
    return "meigu-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")


class Pusher:
    def __init__(self, conf):
        """conf：stocks.json 里的 push 设置 {enabled, server, topic}（会被网页上的开关改动，这里每次发送时现读）。"""
        self.conf = conf
        self.q = queue.Queue()
        self.sent = {}       # 内容 -> 上次发送时间
        self.recent = []     # 最近一小时的发送时间
        self.status = {"last": None, "error": None}
        threading.Thread(target=self._loop, daemon=True).start()

    def send(self, title, msg, priority=3, tags=(), force=False):
        """priority：1 最低 ~ 5 最高（4 及以上手机会响铃震动）。force：测试推送，不检查开关和去重。"""
        if not force and not self.conf.get("enabled"):
            return
        now = time.time()
        if not force:
            if now - self.sent.get(msg, 0) < DEDUPE_SECS:
                return
            self.recent = [t for t in self.recent if now - t < 3600]
            if len(self.recent) >= MAX_PER_HOUR:
                return
            self.recent.append(now)
            self.sent[msg] = now
        self.q.put({"title": title, "message": msg[:1000], "priority": priority, "tags": list(tags)})

    def _loop(self):
        while True:
            item = self.q.get()
            topic, server = self.conf.get("topic"), (self.conf.get("server") or DEFAULT_SERVER).rstrip("/")
            if not topic or not server.startswith("https://"):
                continue
            body = json.dumps({"topic": topic, **item}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(server, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
            for attempt in range(3):
                try:
                    urllib.request.urlopen(req, timeout=10).read()
                    self.status = {"last": time.strftime("%H:%M:%S"), "error": None}
                    break
                except Exception as e:
                    self.status["error"] = str(e)[:80]
                    time.sleep(5 * (attempt + 1))
