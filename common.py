"""桌面版和网页版共用的配置读写、代码格式和行情获取。"""
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta, timezone

import screener

# 打包成 exe 后，配置和数据放在 exe 旁边；直接运行 .py 时放在代码旁边
ROOT = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONF = os.path.join(ROOT, "stocks.json")
DATA = os.path.join(ROOT, "data", "stock")
DEFAULT = {
    "interval": 1,  # 行情和规则检测间隔（秒）
    "stocks": ["usDJI", "usIXIC", "usINX", "usAAPL", "usNVDA", "usMSFT", "usTSLA"],
    "alerts": {},  # 代码 -> {"above": 价格, "below": 价格, "pct": 涨跌幅绝对值}
    "mode": "short",  # 持仓周期：short 超短线 T+1 / swing 波段，决定参考止损的松紧
    "auto_screen": True,  # 每天自动筛选并更新自选
    "auto": [],           # 由自动筛选加入的代码（手动添加的不在这里，永远不会被自动删除）
    "ignore": {},         # 你手动删掉的：代码 -> 日期，7 天内不再自动加回
    "locked": [],         # 你锁定的：每日清理不会删除
    "screen_date": "",
    "screen": {},         # 代码 -> {score, rank, reason}，最近一次筛选结果
    "brackets": [],       # 止损 + 止盈联动规则
    "bt": {},             # 最近一次离场规则回测：{date, k, scope, results}
    # 动态止损参数：会根据你的交易逐步调整；每次调整都记在 rule_history 里
    "rule_params": {"loss_cap": 1.0, "trail": 3.0, "breakeven": 3.0, "version": 1, "since": ""},
    "rule_history": [],
    "journal": {},        # 交易复盘：{date, trades, stats, insights, open}
    "suggestion": None,   # 规则进化建议（需要你点「采用」才生效）
    # 买入规则：单笔打到止损最多亏总资产的 risk_pct%；单只股票市值最多占总资产 max_pos_pct%
    "buy": {"risk_pct": 2.0, "max_pos_pct": 50.0},
    "pending_buys": [],   # 已下单、等待成交的买单（成交后自动挂止损）
}
INDICES = ("usDJI", "usIXIC", "usINX")
COLS = ("name", "code", "src", "score", "price", "pct", "chg", "open", "high", "low", "turnover", "alert", "time")
HEAD = ("名称", "代码", "来源", "评分", "现价($)", "涨跌幅", "涨跌额", "开盘", "最高", "最低", "换手%", "提醒", "更新时间(美东)")
WIDTH = (90, 75, 55, 55, 80, 70, 70, 70, 70, 70, 55, 110, 140)
PCOLS = ("name", "code", "qty", "cost", "price", "pl", "weight", "vol", "rscore", "tstop", "prot", "signal", "level")
PHEAD = ("名称", "代码", "股数", "成本", "现价", "盈亏%", "仓位占比", "日均波动", "风险分",
         "动态止损", "保护状态", "离场信号", "风险等级")
PWIDTH = (75, 70, 45, 65, 65, 60, 65, 65, 55, 75, 90, 160, 65)
OCOLS = ("env", "code", "name", "side", "type", "qty", "price", "aux", "tif", "status", "time")
OHEAD = ("账户", "代码", "名称", "方向", "类型", "数量", "价格", "触发价", "有效期", "状态", "下单时间")
OWIDTH = (70, 80, 80, 70, 100, 50, 70, 70, 60, 90, 140)
ENV_NAME = {"SIMULATE": "模拟盘", "REAL": "真实账户"}
RCOLS = ("id", "env", "code", "qty", "stop", "target", "mode", "status", "created")
RHEAD = ("规则", "账户", "代码", "数量", "止损单", "止盈价", "止盈执行方式", "状态", "建立时间")
RWIDTH = (110, 70, 80, 50, 210, 70, 150, 70, 130)
STATUS_NAME = {"active": "运行中", "done": "已结束", "cancelled": "已停止"}


BACKUP = CONF + ".bak"


# ---------- 美东时间：美股一个交易日跨北京时间零点，按日期判断的事都用美东时间 ----------
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1)
    return d + timedelta(days=(6 - d.weekday()) % 7 + 7 * (n - 1))


def us_now():
    """美东当前时间（自动处理夏令时），不带时区信息。"""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:  # 没有时区数据库：按美国夏令时规则（3 月第 2 个周日 ~ 11 月第 1 个周日）自己算
        utc = datetime.now(timezone.utc).replace(tzinfo=None)
        start = _nth_sunday(utc.year, 3, 2) + timedelta(hours=7)
        end = _nth_sunday(utc.year, 11, 1) + timedelta(hours=6)
        return utc - timedelta(hours=4 if start <= utc < end else 5)


def us_date():
    """美东日期。「每天一次」的提醒用它，同一个交易日里不会因为北京时间过零点又响一次。"""
    return us_now().strftime("%Y-%m-%d")


def market_open(margin_min=0):
    """现在是否在美股常规交易时段（周一到周五 9:30~16:00 美东，不含节假日）；margin_min 前后各放宽几分钟。"""
    t = us_now()
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 9 * 60 + 30 - margin_min <= mins < 16 * 60 + margin_min


def last_session():
    """最近一个已经收盘的交易日（美东 16:30 之后算当天收完）。每日选股、回测按它判断，
    盘中不会拿还没收完的 K 线去清除自选。节假日不单独处理（最多多跑一次，结果一样）。"""
    t = us_now()
    d = t.date()
    if t.weekday() >= 5 or t.time() < dtime(16, 30):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def load():
    if os.path.exists(CONF):
        try:
            with open(CONF, encoding="utf-8") as f:
                stored = json.load(f)
        except ValueError:  # 文件坏了（比如写到一半断电）：用上一次的备份，坏文件留着备查
            if not os.path.exists(BACKUP):
                raise
            shutil.copy2(CONF, CONF + f".broken-{datetime.now():%Y%m%d%H%M%S}")
            with open(BACKUP, encoding="utf-8") as f:
                stored = json.load(f)
            shutil.copyfile(BACKUP, CONF)
        cfg = {**json.loads(json.dumps(DEFAULT)), **stored}
        cfg.pop("risk", None)  # 旧版手动风险规则，已不再使用
        if stored.get("interval", 5) >= 5:  # 旧版默认 5 秒，升级为每秒检测
            cfg["interval"] = 1
        if "auto" not in stored:  # 第一次启用自动选股：程序预设的几只（非你手动加的）交给自动选股管理
            cfg["auto"] = [c for c in cfg["stocks"] if c in DEFAULT["stocks"] and c not in INDICES]
        return cfg
    cfg = json.loads(json.dumps(DEFAULT))
    cfg["auto"] = [c for c in cfg["stocks"] if c not in INDICES]
    return cfg


def save(cfg):
    """先完整写到临时文件再替换，中途断电 / 崩溃也不会留下写了一半的 stocks.json；
    替换前把上一版存成 stocks.json.bak，读不出来时自动用它。"""
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    for i in range(5):  # Windows 上杀毒软件等短暂占用文件时，替换会失败，稍等重试
        try:
            if os.path.exists(CONF):
                shutil.copyfile(CONF, BACKUP)
            os.replace(tmp, CONF)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.1)


def normalize(code):
    """只收美股代码：AAPL / usAAPL / BRK.B -> usAAPL、usBRK.B。"""
    c = code.strip().upper()
    if c.startswith("US"):
        c = c[2:].lstrip(".")
    if c and c[0].isalpha() and c.replace(".", "").isalpha() and len(c) <= 8:
        return "us" + c
    return None


def fetch(codes):
    """腾讯行情接口，一次请求拿全部。"""
    if not codes:
        return {}
    url = "http://qt.gtimg.cn/q=" + ",".join(codes)
    raw = urllib.request.urlopen(url, timeout=5).read().decode("gbk", "ignore")
    out = {}
    for line in raw.split(";"):
        if '="' not in line:
            continue
        key = line.split("=")[0].strip().replace("v_", "")
        f = line.split('"')[1].split("~")
        if len(f) < 40:
            continue
        try:
            out[key] = {"name": f[1], "price": float(f[3]), "prev": float(f[4]), "open": float(f[5]),
                        "chg": float(f[31]), "pct": float(f[32]), "high": float(f[33]),
                        "low": float(f[34]), "turnover": f[38], "time": f[30], "symbol": f[2]}
        except ValueError:
            pass
    return out


def fetch_daily(symbol, n=130):
    """日 K 线 [(日期, 开, 收, 高, 低)]。symbol 要带交易所后缀，如 AAPL.OQ。"""
    return [r[:5] for r in screener.kline(symbol, n)]
