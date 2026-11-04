"""发布新版本给朋友（只有你用）：python publish.py "这次更新了什么"

会做：版本号 +0.0.1 → 提交代码 → 推送到 GitHub → 用你的私钥给文件清单签名 → 创建 Release（附带签名）。
朋友的程序检测到新 Release，验证签名通过后才会自动更新（只在美股收盘后安装）。
个人数据（stocks.json、data/、backup/）在 .gitignore 里，不会被上传。

签名私钥在 %USERPROFILE%\\.meigu-dingpan\\release_key.pem，第一次发布时自动生成。
它只在你电脑上，不会上传；请另外备份一份（U 盘等）。丢了它，朋友的程序就不认新版本了，只能手动重新下载安装。
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
KEY_PATH = os.path.join(os.path.expanduser("~"), ".meigu-dingpan", "release_key.pem")
PUB_FILE = os.path.join(ROOT, "release_key.py")


def run(*cmd):
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def git_out(*cmd):
    return subprocess.run(["git", *cmd], cwd=ROOT, capture_output=True, check=True).stdout


def ensure_key():
    """没有私钥就生成一对；保证仓库里的 release_key.py 是对应的公钥。返回私钥。"""
    from Crypto.PublicKey import ECC
    if os.path.exists(KEY_PATH):
        with open(KEY_PATH, encoding="utf-8") as f:
            key = ECC.import_key(f.read())
    else:
        key = ECC.generate(curve="ed25519")
        os.makedirs(os.path.dirname(KEY_PATH), exist_ok=True)
        with open(KEY_PATH, "w", encoding="utf-8") as f:
            f.write(key.export_key(format="PEM"))
        print(f"已生成签名私钥：{KEY_PATH}\n请另外备份这个文件，丢了就没法给朋友发自动更新了。")
    pub = key.public_key().export_key(format="PEM")
    text = ('"""发布者公钥：自动更新用它验证新版本确实是发布者签名的。私钥只在发布者电脑上。"""\n'
            f'PUBLIC_KEY = """{pub}"""\n')
    old = open(PUB_FILE, encoding="utf-8").read() if os.path.exists(PUB_FILE) else None
    if old != text:
        if old is not None:
            sys.exit("停止：release_key.py 里的公钥和本机私钥不一致。换了私钥朋友的程序会拒绝更新，"
                     "请先找回原来的私钥。")
        with open(PUB_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    return key


def signed_manifest(ver, key):
    """按刚提交的版本（HEAD）生成文件清单并签名，内容和 GitHub 打包的源码 zip 完全一致。"""
    from Crypto.Signature import eddsa
    from updater import installable
    names = [n for n in git_out("ls-files", "-z").decode("utf-8").split("\0") if installable(n)]
    files = {n: hashlib.sha256(git_out("show", f"HEAD:{n}")).hexdigest() for n in names}
    manifest = json.dumps({"version": ver, "files": files}, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
    return manifest, eddsa.new(key, "rfc8032").sign(manifest)


def main():
    notes = sys.argv[1] if len(sys.argv) > 1 else "更新"
    for f in ("stocks.json", "data"):
        tracked = subprocess.run(["git", "ls-files", f], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if tracked:
            sys.exit(f"停止：{f} 被 git 跟踪了，里面是个人数据，不能上传")
    branch = git_out("branch", "--show-current").decode().strip()
    if branch != "main":
        sys.exit(f"停止：现在在 {branch} 分支。请在主目录（main 分支）里运行 publish.py，"
                 "否则 Release 打包的代码和签名对不上")
    key = ensure_key()
    path = os.path.join(ROOT, "VERSION")
    major, minor, patch = (int(x) for x in open(path, encoding="utf-8").read().strip().split("."))
    ver = f"{major}.{minor}.{patch + 1}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(ver + "\n")
    run("git", "add", "-A")
    run("git", "commit", "-m", f"v{ver}: {notes}")
    manifest, sig = signed_manifest(ver, key)
    head = git_out("rev-parse", "HEAD").decode().strip()
    run("git", "push")
    with tempfile.TemporaryDirectory() as d:
        mp, sp = os.path.join(d, "manifest.json"), os.path.join(d, "manifest.sig")
        with open(mp, "wb") as f:
            f.write(manifest)
        with open(sp, "wb") as f:
            f.write(sig)
        run("gh", "release", "create", f"v{ver}", "--target", head, "--title", f"v{ver}", "--notes", notes, mp, sp)
    print(f"已发布 v{ver}（已签名），朋友的程序会在 6 小时内（或下次启动时）收盘后自动更新。")


if __name__ == "__main__":
    main()
