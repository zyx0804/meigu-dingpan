"""规则进化：用「你的真实买入」+「候选池回测」评估一组组止损参数，找到更好的才提出建议。

防过拟合的护栏（全部满足才会建议）：
- 平均每笔收益比当前规则高至少 0.1 个百分点
- 最差一笔不比当前规则更差（容差 0.5 个百分点），最大回撤不高于当前 2 个百分点以上
- 前半段、后半段数据分开测都不比当前规则差
- 你的已平仓交易越多，它们在评分里的权重越大（20 笔后完全按你的交易）
"""
import itertools

import backtest

GRID = {"loss_cap": (0.75, 1.0, 1.25, 1.5), "trail": (2.0, 2.5, 3.0, 3.5), "breakeven": (2.0, 3.0, 4.0),
        "target": (4.0, 6.0, 8.0)}  # 止盈：买入价 + N 个 ATR（始终保留止盈，只调位置）
KEYS = tuple(GRID)
FULL_WEIGHT_TRADES = 20


def _strat(p):
    return ("staged", p["loss_cap"], p["breakeven"], 0.0, p["trail"], p.get("target", 0.0))


def replay_entry(rows, entry_date, entry_price, p):
    """从你真实的买入日开始，按参数 p 模拟离场。返回收益率（到数据末尾还没走就按最后收盘价算）。"""
    idx = next((i for i, r in enumerate(rows) if r[0] >= entry_date), None)
    if idx is None or idx < 15:
        return None
    a0 = backtest._atr(rows, idx - 1)
    entry = entry_price * (1 + backtest.COST)
    stop = backtest.staged_stop(entry, entry, a0, *_strat(p)[1:5])
    target = entry + p["target"] * a0 if p.get("target") else None
    high = rows[idx][3]
    for j in range(idx, len(rows)):
        o, h, l = rows[j][1], rows[j][3], rows[j][4]
        if j > idx and o <= stop:
            return o * (1 - backtest.COST) / entry - 1
        if l <= stop:
            return stop * (1 - backtest.COST) / entry - 1
        if target and h >= target:
            return max(o, target) * (1 - backtest.COST) / entry - 1
        high = max(high, h)
        stop = max(stop, backtest.staged_stop(entry, high, a0, *_strat(p)[1:5]))
    return rows[-1][2] * (1 - backtest.COST) / entry - 1


def evaluate(p, pooled, user_entries):
    """pooled：代码 -> 日K；user_entries：[(日K, 买入日期, 买入价)]。"""
    def pooled_summary(data):
        trades = []
        for rows in data.values():
            if len(rows) > 80:
                trades += backtest.run_one(rows, _strat(p))
        return backtest.summarize(trades)
    s = pooled_summary(pooled)
    halves = [pooled_summary({c: r[:len(r) // 2 + 30] for c, r in pooled.items()}),
              pooled_summary({c: r[len(r) // 2 - 30:] for c, r in pooled.items()})]
    user = [x for x in (replay_entry(rows, d, px, p) for rows, d, px in user_entries) if x is not None]
    return {"params": p, "pooled": s, "halves": halves,
            "user_avg": sum(user) / len(user) * 100 if user else None, "user_n": len(user)}


def suggest(current, pooled, user_entries, closed_count):
    """返回 (当前规则评估, 建议的新参数评估或 None, 全部候选按分数排序)。"""
    w = min(1.0, closed_count / FULL_WEIGHT_TRADES)

    def score(ev):
        s = ev["pooled"]["avg"] if ev["pooled"] else -99
        if ev["user_avg"] is not None and w > 0:
            return (1 - w) * s + w * ev["user_avg"]
        return s

    current = {k: current.get(k, 0.0) for k in KEYS}
    base = evaluate(current, pooled, user_entries)
    cands = []
    for values in itertools.product(*GRID.values()):
        p = dict(zip(KEYS, values))
        ev = base if p == current else evaluate(p, pooled, user_entries)
        ev["score"] = score(ev)
        cands.append(ev)
    base["score"] = score(base)
    cands.sort(key=lambda e: -e["score"])

    def passes(ev):
        b, s = base["pooled"], ev["pooled"]
        if not s or not b or s["trades"] < 30:
            return False
        if ev["score"] < base["score"] + 0.1:
            return False
        if s["worst"] < b["worst"] - 0.5 or s["mdd"] > b["mdd"] + 2:
            return False
        return all(h and bh and h["avg"] >= bh["avg"] for h, bh in zip(ev["halves"], base["halves"]))

    best = next((e for e in cands if passes(e)), None)
    return base, best, cands, w
