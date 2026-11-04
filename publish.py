"""发布新版本给朋友（只有你用）：python publish.py "这次更新了什么"

会做：版本号 +0.0.1 → 提交代码 → 推送到 GitHub → 创建 Release。朋友的程序检测到新 Release 会自动更新。
个人数据（stocks.json、data/、backup/）在 .gitignore 里，不会被上传。
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def run(*cmd):
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)


def main():
    notes = sys.argv[1] if len(sys.argv) > 1 else "更新"
    for f in ("stocks.json", "data"):
        tracked = subprocess.run(["git", "ls-files", f], cwd=ROOT, capture_output=True, text=True).stdout.strip()
        if tracked:
            sys.exit(f"停止：{f} 被 git 跟踪了，里面是个人数据，不能上传")
    path = os.path.join(ROOT, "VERSION")
    major, minor, patch = (int(x) for x in open(path, encoding="utf-8").read().strip().split("."))
    ver = f"{major}.{minor}.{patch + 1}"
    with open(path, "w", encoding="utf-8") as f:
        f.write(ver + "\n")
    run("git", "add", "-A")
    run("git", "commit", "-m", f"v{ver}: {notes}")
    run("git", "push")
    run("gh", "release", "create", f"v{ver}", "--title", f"v{ver}", "--notes", notes)
    print(f"已发布 v{ver}，朋友的程序会在 6 小时内（或下次启动时）自动更新。")


if __name__ == "__main__":
    main()
