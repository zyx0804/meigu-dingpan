"""自动更新：从 update.json 里写的 GitHub 仓库检查新版本，验证签名后只替换程序文件，然后自动重启。

- 不会碰你的 stocks.json、data 文件夹、update.json
- 每个版本都带一份签名过的文件清单（manifest.json + manifest.sig）：用本机 release_key.py 里的公钥验证，
  每个文件的 SHA-256 都要对得上才安装。即使 GitHub 账号被盗，没有发布者电脑上的私钥也做不出能装上的更新
- 自动更新只在美股常规交易时段之外安装，盘中不会重启打断自动规则
- 替换前把旧文件备份到 backup/<旧版本号>/，出问题可以手动还原
- 只从 api.github.com / github.com 下载，地址写死在 update.json 的仓库名上
"""
import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

from common import ROOT, market_open

CHECK_EVERY = 6 * 3600
RETRY_EVERY = 600  # 有新版本但正在盘中：每 10 分钟看一次是否收盘
KEEP = {"stocks.json", "update.json", "data", "backup", "__pycache__", ".git"}  # 更新时永远不动
ALLOWED_EXT = {".py", ".html", ".css", ".js", ".md", ".txt", ".bat"}


def _ver(s):
    s = (s or "").strip().lstrip("vV")
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        return (0,)


def installable(rel):
    """更新包里哪些文件会被安装。publish.py 生成清单时用同一个判断，两边的文件列表才能对上。"""
    first = rel.split("/", 1)[0]
    return (bool(rel) and first not in KEEP and ".." not in rel and not rel.startswith("/")
            and os.path.splitext(rel)[1].lower() in ALLOWED_EXT)


def verify_manifest(manifest, sig):
    """用本机公钥验证清单签名，返回清单内容；签名不对就抛异常。"""
    from Crypto.PublicKey import ECC
    from Crypto.Signature import eddsa
    path = os.path.join(ROOT, "release_key.py")
    if not os.path.exists(path):
        raise RuntimeError("本机没有发布公钥 release_key.py，无法验证更新包，已拒绝")
    ns = {}
    with open(path, encoding="utf-8") as f:
        exec(f.read(), ns)
    try:
        eddsa.new(ECC.import_key(ns["PUBLIC_KEY"]), "rfc8032").verify(manifest, sig)
    except ValueError:
        raise RuntimeError("更新包签名不对，可能被篡改，已拒绝") from None
    return json.loads(manifest)


class Updater:
    def __init__(self, log):
        self.log = log
        self.status = {"enabled": False}
        self.repo = None
        self.auto = False
        self._failed = None  # 安装失败过的版本，自动模式下不再反复重试
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
        last = 0
        while True:
            try:
                if time.time() - last >= CHECK_EVERY:
                    last = time.time()
                    self.check()
                new = self.status.get("available")
                if self.auto and new and new != self._failed:
                    if market_open(10):
                        self.status["note"] = "盘中不自动更新，收盘后自动安装"
                    else:
                        self.log(f"发现新版本 {new}，验证签名后自动更新…")
                        try:
                            self.apply()
                        except Exception as e:
                            self._failed = new
                            self.log(f"自动更新 {new} 没有安装：{str(e)[:120]}")
                            raise
            except Exception as e:
                self.status["error"] = str(e)[:120]
            time.sleep(RETRY_EVERY)

    def _get(self, url, accept="application/vnd.github+json"):
        req = urllib.request.Request(url, headers={"User-Agent": "meigu-dingpan-updater", "Accept": accept})
        return urllib.request.urlopen(req, timeout=30).read()

    def check(self):
        rel = json.loads(self._get(f"https://api.github.com/repos/{self.repo}/releases/latest"))
        latest = rel.get("tag_name", "")
        assets = {a.get("name"): a.get("browser_download_url", "") for a in rel.get("assets") or []}
        self.status.update(checked=time.strftime("%Y-%m-%d %H:%M"), latest=latest, notes=(rel.get("body") or "")[:800],
                           zip=rel.get("zipball_url"), manifest=assets.get("manifest.json"),
                           sig=assets.get("manifest.sig"), error=None, note=None)
        self.status["signed"] = bool(self.status["manifest"] and self.status["sig"])
        self.status["available"] = latest if _ver(latest) > _ver(self.local_version()) else None
        return self.status["available"]

    def _download(self, key, prefix):
        url = self.status.get(key) or ""
        if not url.startswith(prefix):
            raise RuntimeError("更新地址不对，已拒绝")
        return self._get(url, "application/octet-stream")

    def apply(self):
        if not self.status.get("available") and not self.check():
            return "已经是最新版本"
        new = self.status["available"]
        if not self.status.get("signed"):
            raise RuntimeError(f"{new} 没有签名文件，无法确认是发布者本人发的，已拒绝安装")
        dl = f"https://github.com/{self.repo}/releases/download/"
        m = verify_manifest(self._download("manifest", dl), self._download("sig", dl))
        if _ver(m.get("version")) != _ver(new):
            raise RuntimeError("签名清单的版本号和 Release 不一致，已拒绝")
        z = zipfile.ZipFile(io.BytesIO(self._download("zip", f"https://api.github.com/repos/{self.repo}/")))
        names = [n for n in z.namelist() if not n.endswith("/")]
        top = names[0].split("/", 1)[0] + "/"
        files = {n[len(top):]: z.read(n) for n in names if installable(n[len(top):])}
        want = m.get("files") or {}
        if set(files) != set(want):
            raise RuntimeError("更新包里的文件和签名清单对不上，已拒绝")
        bad = [r for r, data in files.items() if hashlib.sha256(data).hexdigest() != want[r]]
        if bad:
            raise RuntimeError(f"{bad[0]} 等 {len(bad)} 个文件的校验值不对，已拒绝")
        if "engine.py" not in files or "web.py" not in files:
            raise RuntimeError("下载的更新包不完整，已放弃")
        old = self.local_version()
        bak = os.path.join(ROOT, "backup", old)
        for rel, data in files.items():
            dst = os.path.join(ROOT, rel)
            if os.path.exists(dst):
                os.makedirs(os.path.dirname(os.path.join(bak, rel)), exist_ok=True)
                with open(dst, "rb") as src, open(os.path.join(bak, rel), "wb") as f:
                    f.write(src.read())
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with open(dst + ".new", "wb") as f:
                f.write(data)
            os.replace(dst + ".new", dst)
        with open(os.path.join(ROOT, "VERSION"), "w", encoding="utf-8") as f:
            f.write(new.lstrip("vV"))
        self.log(f"已更新到 {new}（签名验证通过，{len(files)} 个文件，旧版本备份在 backup/{old}），3 秒后自动重启")
        self._restart()
        return f"已更新到 {new}，程序会自动重启，几秒后刷新网页"

    def _restart(self):
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else sys.executable
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(["cmd", "/c", f'timeout /t 3 /nobreak >nul & "{exe}" web.py --no-browser'],
                         cwd=ROOT, creationflags=flags, close_fds=True)
        threading.Timer(1.0, os._exit, args=(0,)).start()
