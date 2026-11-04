"""交易日志：从 moomoo 真实成交记录整理出每一笔完整交易（买入 → 卖出），用来复盘和改进规则。只读。"""
from datetime import datetime, timedelta

import screener


def fetch_deals(link, days=720):
    """真实账户近 days 天的成交。moomoo 单次查询最多 360 天，所以分段查。"""
    from moomoo import RET_OK, TrdEnv
    out, end = [], datetime.now() + timedelta(days=1)
    while days > 0:
        span = min(days, 359)
        start = end - timedelta(days=span)
        ret, df = link.trd.history_deal_list_query(trd_env=TrdEnv.REAL, acc_id=link.acc_id,
                                                   start=start.strftime("%Y-%m-%d %H:%M:%S"),
                                                   end=end.strftime("%Y-%m-%d %H:%M:%S"))
        if ret != RET_OK:
            raise RuntimeError(f"读取成交记录失败：{df}")
        for _, r in df.iterrows():
            if str(r["code"]).startswith("US."):
                out.append({"code": "us" + r["code"].split(".", 1)[1], "name": r["stock_name"],
                            "side": str(r["trd_side"]), "qty": float(r["qty"]), "price": float(r["price"]),
                            "time": str(r["create_time"])[:19], "order_id": str(r["order_id"])})
        end, days = start, days - span
    return sorted(out, key=lambda d: d["time"])


def build_trades(deals):
    """先进先出配对：返回 (已平仓交易列表, 未平仓持仓批次 {代码: [批次]})。只处理做多。"""
    lots, closed = {}, []
    for d in deals:
        book = lots.setdefault(d["code"], [])
        if d["side"] in ("BUY",):
            book.append({"qty": d["qty"], "price": d["price"], "time": d["time"], "name": d["name"]})
            continue
        if d["side"] not in ("SELL",):
            continue
        left = d["qty"]
        while left > 1e-9 and book:
            lot = book[0]
            q = min(left, lot["qty"])
            closed.append({"code": d["code"], "name": d["name"], "qty": q, "entry": lot["price"],
                           "entry_time": lot["time"], "exit": d["price"], "exit_time": d["time"],
                           "exit_order": d["order_id"], "ret": d["price"] / lot["price"] - 1,
                           "pnl": (d["price"] - lot["price"]) * q})
            lot["qty"] -= q
            left -= q
            if lot["qty"] <= 1e-9:
                book.pop(0)
    return closed, {c: b for c, b in lots.items() if b}


def entry_date(open_lots, code):
    """持仓最早一批买入的日期（YYYY-MM-DD），没有记录返回 None。"""
    b = open_lots.get(code)
    return b[0]["time"][:10] if b else None


def enrich(trades, symbols, rule_orders):
    """补充每笔交易持有期内的最大浮盈 / 最大浮亏（按日 K）和离场方式。
    symbols：代码 -> 腾讯 symbol；rule_orders：规则挂出的订单号 -> 说明。"""
    cache = {}
    for t in trades:
        sym = symbols.get(t["code"])
        if sym and t["code"] not in cache:
            try:
                cache[t["code"]] = screener.kline(sym, 500)
            except Exception:
                cache[t["code"]] = []
        rows = [r for r in cache.get(t["code"], []) if t["entry_time"][:10] <= r[0] <= t["exit_time"][:10]]
        t["days"] = len(rows)
        t["mfe"] = (max(r[3] for r in rows) / t["entry"] - 1) if rows else None  # 持有期最大浮盈
        t["mae"] = (min(r[4] for r in rows) / t["entry"] - 1) if rows else None  # 持有期最大浮亏
        t["how"] = rule_orders.get(t["exit_order"], "手动")
    return trades


def stats(trades):
    if not trades:
        return None
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    avg = lambda xs: sum(xs) / len(xs) if xs else 0.0
    out = {"n": len(trades), "win": len(wins) / len(trades) * 100, "pnl": sum(t["pnl"] for t in trades),
           "avg_win": avg([t["ret"] for t in wins]) * 100, "avg_loss": avg([t["ret"] for t in losses]) * 100,
           "worst": min(t["ret"] for t in trades) * 100, "days": avg([t.get("days") or 0 for t in trades])}
    # 卖出后还有多少空间：持有期最大浮盈 vs 实际收益（卖早了 / 拿太久）
    gave = [t["mfe"] - t["ret"] for t in trades if t.get("mfe") is not None]
    out["gave_back"] = avg(gave) * 100 if gave else None
    manual = [t for t in trades if t.get("how") == "手动"]
    out["manual"] = len(manual)
    return out


def insights(trades, st):
    """从你的交易里找出规律，给出可以改进的地方（只描述事实，不是投资建议）。"""
    if not st:
        return ["还没有已平仓的交易。每卖出一笔，这里会自动复盘，规则也会随之调整。"]
    tips = []
    if st["avg_loss"] and st["avg_win"] and abs(st["avg_loss"]) > st["avg_win"]:
        tips.append(f"平均亏损 {st['avg_loss']:.1f}% 大于平均盈利 {st['avg_win']:.1f}%：亏的单拿得比赚的单久，止损可以更早执行")
    if st["gave_back"] is not None and st["gave_back"] > 3:
        tips.append(f"平均每笔从持有期最高点回吐 {st['gave_back']:.1f}%：跟踪止损可以收紧一些来锁住利润")
    big = [t for t in trades if t.get("mae") is not None and t["mae"] < -0.08 and t["ret"] < 0]
    if big:
        tips.append(f"有 {len(big)} 笔持有中浮亏超过 8% 才离场：限亏线可以更早触发")
    if st["manual"] and st["n"] >= 3 and st["manual"] / st["n"] > 0.5:
        tips.append(f"{st['manual']}/{st['n']} 笔是手动卖出、没按规则走：可以对比下面「按规则会怎样」")
    return tips or ["目前没发现明显问题，继续积累交易数据。"]
