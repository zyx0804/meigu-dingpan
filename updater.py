"""自动更新：从 update.json 里写的 GitHub 仓库检查新版本，下载后只替换程序文件，然后自动重启。

- 不会碰你的 stocks.json、data 文件夹、update.json
- 替换前把旧文件备份到 backup/<旧版本号>/，出问题可以手动还原
- 只从 api.github.com / codeload.github.com 下载，地址写死在 update.json 的仓库名上
"""
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from common import ROOT

CHECK_EVERY = 6 * 3600
KEEP = {"stocks.json", "update.json", "data", "backup", "__pycache__", ".git"}  # 更新时永远不动
ALLOWED_EXT = {".py", ".html", ".css", ".js", ".md", ".txt", ".bat"}


def _ver(s):
    s = (s or "").strip().lstrip("vV")
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (0,)


class Updater:
    def __init__(self, log):
        self.log = log
        self.status = {"enabled": False}
        self.repo = None
        path = os.path.join(ROOT, "update.json")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                conf = json.load(f)
            self.repo = conf.get("repo")
            self.auto = conf.get("auto", True)
            self.status = {"enabled": bool(self.repo), "repo": self.repo, "auto": self.auto}

    def local_version(self):
        p = os.path.join(ROOT, "VERSION")
        return open(p, encoding="utf-8").read().strip() if os.path.exists(p) else "0.0.0"

    def start(self):
        if self.repo:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        time.sleep(20)
        while True:
            try:
                self.check()
                if self.auto and self.status.get("available"):
                    self.log(f"发现新版本 {self.status['available']}，自动更新中…")
                    self.apply()
            except Exception as e:
                self.status["error"] = str(e)[:120]
            time.sleep(CHECK_EVERY)

    def _get(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "meigu-dingpan-updater",
                                                    "Accept": "application/vnd.github+json"})
        return urllib.request.urlopen(req, timeout=30).read()

    def check(self):
        rel = json.loads(self._get(f"https://api.github.com/repos/{self.repo}/releases/latest"))
        latest = rel.get("tag_name", "")
        self.status.update(checked=time.strftime("%Y-%m-%d %H:%M"), latest=latest, notes=(rel.get("body") or "")[:800],
                           zip=rel.get("zipball_url"), error=None)
        self.status["available"] = latest if _ver(latest) > _ver(self.local_version()) else None
        return self.status["available"]

    def apply(self):
        if not self.status.get("available") and not self.check():
            return "已经是最新版本"
        url = self.status["zip"]
        if not url.startswith(f"https://api.github.com/repos/{self.repo}/"):
            raise RuntimeError("更新地址不对，已拒绝")
        z = zipfile.ZipFile(io.BytesIO(self._get(url)))
        names = [n for n in z.namelist() if not n.endswith("/")]
        top = names[0].split("/", 1)[0] + "/"
        files = {}
        for n in names:
            rel = n[len(top):]
            first = rel.split("/", 1)[0]
            if not rel or first in KEEP or os.path.splitext(rel)[1].lower() not in ALLOWED_EXT or ".." in rel:
                continue
            files[rel] = z.read(n)
        if "stock.py" not in files or "web.py" not in files:
            raise RuntimeError("下载的更新包不完整，已放弃")
        old = self.local_version()
        bak = os.path.join(ROOT, "backup", old)
        for rel, data in files.items():
            dst = os.path.join(ROOT, rel)
            if os.path.exists(dst):
                os.makedirs(os.path.dirname(os.path.join(bak, rel)), exist_ok=True)
                shutil.copy2(dst, os.path.join(bak, rel))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst, "wb") as f:
                f.write(data)
        new = self.status["available"]
        with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
            f.write(new.lstrip("vV"))
        self.log(f"已更新到 {new}（{len(files)} 个文件，旧版本备份在 backup/{old}），3 秒后自动重启")
        self._restart()
        return f"已更新到 {new}，程序会自动重启，几秒后刷新网页"

    def _restart(self):
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(["cmd", "/c", f'timeout /t 3 /nobreak >nul & "{exe}" web.py --no-browser'],
                         cwd=ROOT, creationflags=flags, close_fds=True)
        threading.Timer(1.0, os._exit, args=(0,)).start()
