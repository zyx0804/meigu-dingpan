"""自动学习：三个档位（保守 / 均衡 / 激进）+ 候选规则库 + 影子交易 + 每月升级。

- 档位决定风险胃口：单笔最多亏多少、要不要看大盘；具体的买卖参数可以被每月升级换掉
- 候选规则库：交易员常用的抄底做法写成一组组参数，激进的、保守的都有
- 影子交易：每天收盘后，让所有候选规则按真实日 K「假装交易」，只记账不下单。只统计开始记录之后的交易，
  这部分是规则从没「见过」的新行情，比历史回测更可信
- 每月升级：某个候选规则在影子交易里跑满足够笔数、并且同时跑赢当前规则（平均收益更高、最差一笔不更差），
  历史回测也不更差，才在对应档位里换上，推送提醒，可以退回
"""
import statistics

COST = 0.0005  # 单边滑点 + 费用

# 档位：风险胃口（固定）+ 默认买卖参数（每月可能被升级）
PROFILES = {
    "conservative": {"name": "保守", "risk_pct": 1.0, "market": True,
                     "params": {"pullback": 3.0, "stop": 3.0, "add": 1.5, "exit_ma": 10, "trail": 2.5, "max_days": 20}},
    "balanced": {"name": "均衡", "risk_pct": 2.0, "market": False,
                 "params": {"pullback": 2.5, "stop": 3.0, "add": 1.5, "exit_ma": 10, "trail": 2.5, "max_days": 20}},
    "aggressive": {"name": "激进", "risk_pct": 3.0, "market": False,
                   "params": {"pullback": 2.0, "stop": 3.0, "add": 1.0, "exit_ma": 10, "trail": 3.0, "max_days": 25}},
}
PARAM_KEYS = ("pullback", "stop", "add", "exit_ma", "trail", "max_days")

# 候选规则库：(名字, 参数, 适合的档位)。新的交易员做法加在这里，影子交易会自动开始跟踪
CANDIDATES = [
    ("均衡默认：回落2.5ATR，10日线卖一半", PROFILES["balanced"]["params"], ("balanced",)),
    ("保守默认：回落3ATR，10日线卖一半", PROFILES["conservative"]["params"], ("conservative",)),
    ("激进默认：回落2ATR，补仓1ATR，宽跟踪3ATR", PROFILES["aggressive"]["params"], ("aggressive",)),
    ("深跌才买：回落3.5ATR", {"pullback": 3.5, "stop": 3.0, "add": 1.5, "exit_ma": 10, "trail": 2.5, "max_days": 20},
     ("conservative", "balanced")),
    ("快进快出：5日线卖一半，最多10天", {"pullback": 2.5, "stop": 2.5, "add": 1.0, "exit_ma": 5, "trail": 2.0, "max_days": 10},
     ("balanced", "aggressive")),
    ("拿得更久：20日线卖一半，最多30天", {"pullback": 2.5, "stop": 3.0, "add": 1.5, "exit_ma": 20, "trail": 3.0, "max_days": 30},
     ("balanced", "aggressive")),
    ("紧止损：止损2ATR，补仓1ATR", {"pullback": 2.5, "stop": 2.0, "add": 1.0, "exit_ma": 10, "trail": 2.0, "max_days": 20},
     ("conservative", "balanced")),
    ("宽止损：止损4ATR，补仓2ATR", {"pullback": 2.5, "stop": 4.0, "add": 2.0, "exit_ma": 10, "trail": 3.0, "max_days": 20},
     ("balanced", "aggressive")),
    ("不分批：一次买满，10日线全卖", {"pullback": 2.5, "stop": 3.0, "add": 0, "exit_ma": 10, "trail": 0, "max_days": 15},
     ("aggressive",)),
    ("浅回调就上：回落1.5ATR，宽跟踪", {"pullback": 1.5, "stop": 3.0, "add": 1.0, "exit_ma": 10, "trail": 3.0, "max_days": 20},
     ("aggressive",)),
]
MIN_SHADOW_TRADES = 30   # 影子交易至少这么多笔才考虑升级
MIN_GAIN = 0.10          # 平均每笔至少多赚 0.1 个百分点
WORST_TOL = 0.5          # 最差一笔最多差 0.5 个百分点


def _atr(rows, i, period=14):
    trs = [max(rows[j][3] - rows[j][4], abs(rows[j][3] - rows[j - 1][2]), abs(rows[j][4] - rows[j - 1][2]))
           for j in range(max(1, i - period + 1), i + 1)]
    return sum(trs) / len(trs)


def _ma(rows, i, n):
    return statistics.mean(r[2] for r in rows[max(0, i - n + 1):i + 1])


def market_ok_map(spx):
    """标普日 K -> {日期: 大盘是否健康（收盘在 200 日线上方）}。"""
    return {spx[i][0]: spx[i][2] > _ma(spx, i, 200) for i in range(200, len(spx))}


def simulate(rows, p, market=None, since=None):
    """按抄底 + 分批规则逐日模拟一只股票。rows：[(日期, 开, 收, 高, 低, ...)]，已复权；
    p：参数（PARAM_KEYS）；market：{日期: 大盘健康}，给了就只在大盘健康时买；since：只统计这天及以后买入的交易。
    返回 [(买入日, 收益率, 持有天数)]，收益按计划的整笔仓位算（没补上的那一半算空仓）。"""
    trades, i, n = [], 210, len(rows)
    scale = p["add"] > 0
    while i < n - 1:
        c = rows[i][2]
        a = _atr(rows, i)
        setup = (c > _ma(rows, i, 200) and a > 0 and max(r[3] for r in rows[i - 19:i + 1]) - c >= p["pullback"] * a
                 and (market is None or market.get(rows[i][0], True)))
        if not setup:
            i += 1
            continue
        e = i + 1
        ent = rows[e][1] * (1 + COST)
        stop = ent - p["stop"] * a
        add_px = ent - p["add"] * a if scale else None
        left, cost_basis, realized = (0.5 if scale else 1.0), ent, 0.0
        high, half_sold, j = rows[e][3], False, e
        while j < n:
            o, cl, h, lo = rows[j][1], rows[j][2], rows[j][3], rows[j][4]
            if add_px and j > e and lo <= add_px and add_px > stop:
                px = min(o, add_px) * (1 + COST)
                cost_basis = (cost_basis * left + px * 0.5) / (left + 0.5)
                left += 0.5
                add_px = None
            out = o if (j > e and o <= stop) else stop if lo <= stop else None
            if out is not None:
                realized += left * (out * (1 - COST) / cost_basis - 1)
                left = 0
                break
            if j > e and cl > _ma(rows, j, p["exit_ma"]):
                if scale and not half_sold:
                    sell = left / 2
                    realized += sell * (cl * (1 - COST) / cost_basis - 1)
                    left -= sell
                    half_sold, add_px = True, None
                    stop = max(stop, cost_basis * (1 + 2 * COST))
                elif not scale:
                    realized += left * (cl * (1 - COST) / cost_basis - 1)
                    left = 0
                    break
            high = max(high, h)
            if half_sold and p["trail"]:
                stop = max(stop, high - p["trail"] * a)
            if j - e + 1 >= p["max_days"]:
                realized += left * (cl * (1 - COST) / cost_basis - 1)
                left = 0
                break
            j += 1
        if left > 0:
            j = n - 1
            realized += left * (rows[-1][2] * (1 - COST) / cost_basis - 1)
        if since is None or rows[e][0] >= since:
            trades.append((rows[e][0], realized, j - e + 1))
        i = j + 1
    return trades


def summarize(trades):
    if not trades:
        return {"trades": 0}
    rets = [t[1] * 100 for t in trades]
    wins = sum(r for r in rets if r > 0)
    loss = -sum(r for r in rets if r <= 0)
    mid = sorted(t[0] for t in trades)[len(trades) // 2]
    h1 = [t[1] * 100 for t in trades if t[0] < mid] or [0]
    h2 = [t[1] * 100 for t in trades if t[0] >= mid] or [0]
    return {"trades": len(rets), "win": sum(r > 0 for r in rets) / len(rets) * 100, "avg": statistics.mean(rets),
            "median": statistics.median(rets), "worst": min(rets), "pf": wins / loss if loss else 99.0,
            "days": statistics.mean(t[2] for t in trades), "half1": statistics.mean(h1), "half2": statistics.mean(h2)}


def evaluate(data, spx, since):
    """所有候选 × 有没有大盘过滤：返回 [{name, params, market, profiles, backtest, shadow}]。
    data：代码 -> 已复权日 K；since：影子交易开始日期（只统计这天之后买入的）。"""
    mkt = market_ok_map(spx) if spx else None
    out = []
    for name, p, profiles in CANDIDATES:
        for use_mkt in (False, True):
            bt, sh = [], []
            for rows in data.values():
                if len(rows) < 260:
                    continue
                tr = simulate(rows, p, mkt if use_mkt else None)
                bt += tr
                sh += [t for t in tr if since and t[0] >= since]
            out.append({"name": name + ("（看大盘）" if use_mkt else ""), "params": dict(p), "market": use_mkt,
                        "profiles": list(profiles), "backtest": summarize(bt), "shadow": summarize(sh)})
    return out


def pick_upgrade(results, profile, current_params):
    """在这个档位能用的候选里，找出同时在影子交易和历史回测里都跑赢当前规则的那个；没有返回 None。"""
    need_mkt = PROFILES[profile]["market"]
    cur = next((r for r in results if r["params"] == current_params and r["market"] == need_mkt), None)
    if not cur or cur["shadow"].get("trades", 0) < MIN_SHADOW_TRADES:
        return None
    best = None
    for r in results:
        if profile not in r["profiles"] or r["market"] != need_mkt or r["params"] == current_params:
            continue
        s, c, b, cb = r["shadow"], cur["shadow"], r["backtest"], cur["backtest"]
        if s.get("trades", 0) < MIN_SHADOW_TRADES or not b.get("trades"):
            continue
        if s["avg"] < c["avg"] + MIN_GAIN or s["worst"] < c["worst"] - WORST_TOL:
            continue
        if b["avg"] < cb["avg"] or b["worst"] < cb["worst"] - WORST_TOL or min(b["half1"], b["half2"]) < 0:
            continue
        if best is None or s["avg"] > best["shadow"]["avg"]:
            best = r
    return best
