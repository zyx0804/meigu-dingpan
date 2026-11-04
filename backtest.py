"""离场规则回测：用历史日 K 比较不同的止损 / 止盈 / 跟踪止损规则，先回测再上真钱，减少试错成本。

入场统一用自动选股的条件（收盘站上 20 日线且 20 日上涨 → 次日开盘买入），只比较"怎么走"。
按日 K 模拟：开盘就跳空越过止损按开盘价成交；同一天既碰止损又碰止盈时按先止损算（保守）。
"""
import statistics

COST = 0.0005  # 单边滑点 + 费用 0.05%


def _atr(rows, i, period=14):
    trs = [max(rows[j][3] - rows[j][4], abs(rows[j][3] - rows[j - 1][2]), abs(rows[j][4] - rows[j - 1][2]))
           for j in range(max(1, i - period + 1), i + 1)]
    return sum(trs) / len(trs)


def _ma(rows, i, n):
    return statistics.mean(r[2] for r in rows[i - n + 1:i + 1])


# 每个策略：名称 -> (类型, 参数)
STRATEGIES = {
    "固定：止损 1ATR / 止盈 2ATR": ("fixed", 1.0, 2.0),
    "固定：止损 1.5ATR / 止盈 3ATR": ("fixed", 1.5, 3.0),
    "跟踪止损 1ATR": ("trail", 1.0),
    "跟踪止损 1.5ATR": ("trail", 1.5),
    "跟踪止损 2ATR": ("trail", 2.0),
    "跟踪止损 3ATR": ("trail", 3.0),
    "吊灯止损（22日高点 − 3ATR）": ("chandelier", 3.0),
    "趋势离场：收盘跌破 20 日线": ("ma", 20),
    "风险离场：收盘跌破 1.5ATR 跟踪线或波动率飙升": ("risk", 1.5),
    # 分段止损：先限亏 1ATR → 浮盈 1ATR 移到保本 → 浮盈 2ATR 后最高价回撤 1.5ATR 跟踪
    "分段止损：限亏1ATR→保本→1.5ATR跟踪": ("staged", 1.0, 1.0, 2.0, 1.5),
    "分段止损：限亏1.5ATR→保本→2ATR跟踪": ("staged", 1.5, 1.0, 2.0, 2.0),
    # 限亏 + 放宽跟踪：一开始最多亏 1ATR，之后最高价回撤 3ATR 跟踪；浮盈 3ATR 才移到保本
    "限亏1ATR + 3ATR跟踪（浮盈3ATR保本）": ("staged", 1.0, 3.0, 0.0, 3.0),
    "限亏1ATR + 2.5ATR跟踪（浮盈3ATR保本）": ("staged", 1.0, 3.0, 0.0, 2.5),
    "限亏1ATR + 3ATR跟踪 + 止盈4ATR": ("staged", 1.0, 3.0, 0.0, 3.0, 4.0),
    "限亏1ATR + 3ATR跟踪 + 止盈6ATR": ("staged", 1.0, 3.0, 0.0, 3.0, 6.0),
}


def staged_stop(entry, high, a, init=1.0, be_at=1.0, trail_at=2.0, trail=1.5):
    """分段止损价（做多）。entry 成本，high 持有期最高价，a 为 ATR。只会随最高价上移。"""
    stop = entry - init * a
    if high >= entry + be_at * a:
        stop = max(stop, entry * (1 + 2 * COST))  # 保本（含来回成本）
    if high >= entry + trail_at * a:
        stop = max(stop, high - trail * a)
    return stop
TRAIL_KS = {"跟踪止损 1ATR": 1.0, "跟踪止损 1.5ATR": 1.5, "跟踪止损 2ATR": 2.0, "跟踪止损 3ATR": 3.0}


def _signal(rows, i):
    return i >= 60 and rows[i][2] > _ma(rows, i, 20) and rows[i][2] > rows[i - 20][2]


def run_one(rows, strat):
    """单只股票、单个策略。rows: [(日期, 开, 收, 高, 低, 量)]。返回交易列表。"""
    kind, *p = strat
    trades, i, n = [], 60, len(rows)
    while i < n - 1:
        if not _signal(rows, i):
            i += 1
            continue
        e = i + 1  # 次日开盘入场
        entry = rows[e][1] * (1 + COST)
        a0 = _atr(rows, i)
        stop = target = None
        if kind == "fixed":
            stop, target = entry - p[0] * a0, entry + p[1] * a0
        elif kind == "trail":
            stop = entry - p[0] * a0
        elif kind == "staged":
            stop = staged_stop(entry, entry, a0, *p[:4])
            if len(p) > 4 and p[4]:  # 第 5 个参数：止盈 = 买入价 + N 个 ATR（0 表示不设止盈）
                target = entry + p[4] * a0
        high = rows[e][3]
        close_stop = entry - p[0] * a0 if kind == "risk" else None  # 只认收盘价，不怕盘中插针
        exit_px, j = None, e
        while j < n:
            o, c, h, l = rows[j][1], rows[j][2], rows[j][3], rows[j][4]
            if kind == "chandelier":
                stop = max(r[3] for r in rows[max(0, j - 22):j]) - p[0] * _atr(rows, j - 1)
            if stop is not None and j > e and o <= stop:
                exit_px = o  # 跳空直接越过止损
            elif stop is not None and l <= stop:
                exit_px = stop
            elif target is not None and h >= target:
                exit_px = max(o, target)
            elif kind == "ma" and c < _ma(rows, j, p[0]):
                exit_px = c
            elif kind == "risk":
                vol10 = statistics.stdev(rows[k][2] / rows[k - 1][2] - 1 for k in range(j - 9, j + 1))
                vol60 = statistics.stdev(rows[k][2] / rows[k - 1][2] - 1 for k in range(j - 59, j + 1))
                if c < close_stop or (vol10 > 1.8 * vol60 and c < rows[j - 1][2]):
                    exit_px = c
            if exit_px is not None:
                break
            high = max(high, h)
            if kind == "trail":  # 收盘后按新高上移止损，只升不降
                stop = max(stop, high - p[0] * a0)
            if kind == "staged":
                stop = max(stop, staged_stop(entry, high, a0, *p[:4]))
            if kind == "risk":
                close_stop = max(close_stop, c - p[0] * _atr(rows, j))
            j += 1
        if exit_px is None:  # 到数据末尾还没走，按最后收盘价算
            j, exit_px = n - 1, rows[-1][2]
        exit_px *= (1 - COST)
        ret = exit_px / entry - 1
        # 被"震出去"：走了之后 5 天内价格又涨回离场价上方 1 个 ATR
        after = rows[j + 1:j + 6]
        shaken = ret < 0 and bool(after) and max(r[3] for r in after) > exit_px + a0
        trades.append({"entry_date": rows[e][0], "exit_date": rows[j][0], "ret": ret,
                       "days": j - e + 1, "shaken": shaken})
        i = j + 1
    return trades


def summarize(trades):
    if not trades:
        return None
    rets = [t["ret"] for t in trades]
    wins = [r for r in rets if r > 0]
    losses = [-r for r in rets if r <= 0]
    equity, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        equity *= 1 + r
        peak = max(peak, equity)
        mdd = max(mdd, 1 - equity / peak)
    stops = [t for t in trades if t["ret"] < 0]
    return {"trades": len(trades), "win": len(wins) / len(rets) * 100, "avg": statistics.mean(rets) * 100,
            "total": (equity - 1) * 100, "mdd": mdd * 100,
            "pf": sum(wins) / sum(losses) if losses and sum(losses) else float("inf"),
            "days": statistics.mean(t["days"] for t in trades),
            "shaken": sum(t["shaken"] for t in stops) / len(stops) * 100 if stops else 0.0,
            "worst": min(rets) * 100}


def compare(rows_by_code):
    """多只股票合并比较所有策略。返回按平均每笔收益排序的列表。"""
    out = []
    for name, strat in STRATEGIES.items():
        trades = []
        for rows in rows_by_code.values():
            if len(rows) > 80:
                trades += run_one(rows, strat)
        s = summarize(trades)
        if s:
            out.append({"name": name, **s})
    out.sort(key=lambda x: -x["avg"])
    return out


def best_trail_k(results):
    """回测里表现最好的跟踪止损倍数；没有就用 1.5。"""
    for r in results:
        if r["name"] in TRAIL_KS and r["trades"] >= 5:
            return TRAIL_KS[r["name"]]
    return 1.5
