"""持仓风险评估：全部从账户数据和历史日 K 自动算，不需要手动输入参数。

所有"参考位"都是按波动率的通用算法得出的数值，用来衡量风险，不是买卖建议。
"""
import math
import statistics

LEVELS = ("低", "中", "高")
# 持仓周期 -> (参考止损离现价几个 ATR, 参考目标几个 ATR)，都保持 2:1。
# 倍数越小亏得越少，但越容易被正常波动打到
MODES = {"swing": ("波段", 1.5, 3.0), "short": ("超短线 T+1", 1.0, 2.0)}
RISK_PER_TRADE = 1.0  # 计算"建议最多持有股数"用：打到参考止损时最多亏总资产的 1%（通用的保守标准）
EXIT_SCORE = 70       # 风险分达到这个值就发出离场信号
TRAIL_LOOKBACK = {"short": 10, "swing": 22}  # 动态止损从最近几天的最高点往下算
LOSS_CAP_ATR = 1.0   # 动态止损：相对成本最多亏 1 个 ATR
TRAIL_ATR = 3.0      # 从最高点回撤 3 个 ATR 跟踪
BREAKEVEN_ATR = 3.0  # 浮盈 3 个 ATR 后至少保本
TARGET_ATR = 6.0     # 止盈：成本 + 6 个 ATR 就卖（每月评估在 4/6/8 里调整）


def atr(rows, period=14):
    """平均真实波幅：近 period 天 max(高-低, |高-昨收|, |低-昨收|) 的平均。rows: [(日期, 开, 收, 高, 低)]"""
    trs = [max(h - l, abs(h - rows[i - 1][2]), abs(l - rows[i - 1][2]))
           for i, (_, _, _, h, l) in enumerate(rows) if i > 0]
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else None


def _returns(rows):
    c = [r[2] for r in rows]
    return [c[i] / c[i - 1] - 1 for i in range(1, len(c)) if c[i - 1]]


def _percentile(xs, p):
    xs = sorted(xs)
    k = (len(xs) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def _max_drawdown(rows, sign):
    """近期最大回撤（做空时按反向计算），返回正的百分比。"""
    worst, peak = 0.0, None
    for _, _, c, _, _ in rows:
        v = c if sign > 0 else 1 / c
        peak = v if peak is None else max(peak, v)
        worst = max(worst, (peak - v) / peak)
    return worst * 100


def _level(value, mid, high):
    return 2 if value >= high else 1 if value >= mid else 0


def _gaps(rows):
    """隔夜跳空：今天开盘相对昨天收盘的涨跌幅。"""
    return [rows[i][1] / rows[i - 1][2] - 1 for i in range(1, len(rows)) if rows[i - 1][2]]


DEFAULT_PARAMS = {"loss_cap": LOSS_CAP_ATR, "trail": TRAIL_ATR, "breakeven": BREAKEVEN_ATR, "target": TARGET_ATR}


def assess(pos, price, total_assets, rows, mode="swing", trail_k=None, params=None, entry_date=None):
    """单只持仓的风险评估。rows 为近半年日 K；trail_k 为回测选出的跟踪止损 ATR 倍数；
    params：动态止损参数（会随你的交易逐步调整）；entry_date：真实买入日期，用来算"买入后的最高点"。"""
    params = {**DEFAULT_PARAMS, **(params or {})}
    mode_name, stop_atr, target_atr = MODES[mode]
    trail_k = trail_k or stop_atr
    sign = 1 if pos["side"] == "long" else -1
    qty, cost = pos["qty"], pos["cost"]
    value = qty * price
    total = total_assets or value or 1
    weight = value / total * 100
    a = atr(rows)
    rets = _returns(rows)
    out = {"value": value, "weight": weight, "pl_pct": (price - cost) / cost * 100 * sign if cost else 0,
           "atr": a, "atr_pct": a / price * 100 if a else None}
    if not a or len(rets) < 20:
        out.update(level=None, reasons=["历史数据不足，暂时无法评估"], stop=None, target=None, scenarios=[],
                   trail_stop=None, trail_pct=None, risk_score=None, exit_signals=[])
        return out

    my = [r * sign for r in rets]  # 站在持仓方向看的每日收益
    vol_y = statistics.stdev(rets) * math.sqrt(252) * 100
    var95 = -_percentile(my, 5)  # 95% 的交易日单日亏损不超过这个比例（历史模拟）
    var_usd = max(0.0, var95) * value
    worst_day = -min(my)
    mdd = _max_drawdown(rows, sign)
    recent = rows[-20:]
    low20, high20 = min(r[4] for r in recent), max(r[3] for r in recent)

    # 波动参考位：从现价往不利方向 stop_atr 个 ATR；目标按 2:1 往有利方向 target_atr 个 ATR
    stop = price - sign * stop_atr * a
    target = price + sign * target_atr * a
    stop_dist = stop_atr * a / price  # 止损距离（比例）

    # 隔夜跳空：止损单只在交易时段触发，开盘直接跳过止损价时会以更差的价格成交
    my_gaps = [g * sign for g in _gaps(rows)]
    gap95 = -_percentile(my_gaps, 5)
    worst_gap = -min(my_gaps)
    gap_through = sum(1 for g in my_gaps if -g > stop_dist) / len(my_gaps) * 100  # 跳空直接越过止损的天数占比

    # 打到参考止损时最多亏总资产 RISK_PER_TRADE% 的话，能持有多少股
    per_share = stop_atr * a
    max_shares = math.floor(total * RISK_PER_TRADE / 100 / per_share) if per_share else None

    def scen(label, p):
        pnl_now = (p - price) * sign * qty
        pnl_cost = (p - cost) * sign * qty
        return {"label": label, "price": p, "pnl_now": pnl_now, "pnl_cost": pnl_cost,
                "pct_assets": pnl_now / total * 100}
    adverse = "跌" if sign > 0 else "涨"
    scenarios = [scen(f"{adverse} {stop_atr:g} 个 ATR（参考止损）", stop)]
    if stop_atr != 1:
        scenarios.insert(0, scen(f"{adverse} 1 个 ATR（一天的正常波动）", price - sign * a))
    scenarios += [scen(f"{adverse} 2 个 ATR", price - sign * 2 * a),
                  scen(f"{adverse} 3 个 ATR", price - sign * 3 * a),
                  scen(f"隔夜跳空（5% 概率会更差，{-gap95 * 100:+.1f}%）", price * (1 - sign * gap95)),
                  scen(f"重演近半年最差隔夜跳空（{-worst_gap * 100:+.1f}%）", price * (1 - sign * worst_gap)),
                  scen("回到 20 日" + ("低点" if sign > 0 else "高点"), low20 if sign > 0 else high20),
                  scen(f"重演近半年最差单日（{-worst_day * 100:+.1f}%）", price * (1 - sign * worst_day)),
                  scen("回到成本价", cost)]

    var_pct_assets = var_usd / total * 100
    factors = [
        (_level(weight, 20, 50), f"仓位占总资产 {weight:.0f}%" + ("，高度集中" if weight >= 50 else "，较集中" if weight >= 20 else "")),
        (_level(var_pct_assets, 1, 3), f"按近半年走势，单日亏损有 5% 的概率超过 ${var_usd:,.2f}（占总资产 {var_pct_assets:.1f}%）"),
        (_level(vol_y, 30, 50), f"年化波动率 {vol_y:.0f}%，日均波动约 {out['atr_pct']:.1f}%"),
        (_level(mdd, 15, 30), f"近半年最大回撤 {mdd:.0f}%"),
        (_level(gap_through, 3, 10), f"近半年 {gap_through:.0f}% 的交易日开盘就跳空越过参考止损（止损单挡不住）"),
    ]
    if mode == "short" and max_shares is not None and qty > max_shares:
        # 超短线更看重单笔风险：持股数超过 1% 风险预算对应的股数就提示
        factors.append((2 if qty > 2 * max(max_shares, 1) else 1,
                        f"按单笔最多亏总资产 {RISK_PER_TRADE:g}%，参考止损下最多持有 {max_shares} 股，现在持有 {qty:g} 股"))
    level = max(f[0] for f in factors)
    reasons = [t for l, t in sorted(factors, key=lambda f: -f[0])]

    # 动态止损（限亏 + 跟踪，回测里最差一笔亏损最小、回撤最低的规则）：
    #   ① 最多亏 LOSS_CAP_ATR 个 ATR（相对成本）
    #   ② 最近 N 天最高点（做空用最低点）回撤 TRAIL_ATR 个 ATR 跟踪，价格创新高它就上移
    #   ③ 浮盈超过 BREAKEVEN_ATR 个 ATR 后至少保本
    #   三条取对你最有利的那条，只往有利方向移
    # 跟踪的起点：有真实买入日期就用买入后的最高点，否则用最近 N 天
    look = [r for r in rows if entry_date and r[0] >= entry_date] or rows[-TRAIL_LOOKBACK[mode]:]
    anchor = max(r[3] for r in look) if sign > 0 else min(r[4] for r in look)
    anchor = max(anchor, price) if sign > 0 else min(anchor, price)
    cap = cost - sign * params["loss_cap"] * a
    trail = anchor - sign * params["trail"] * a
    trail_stop = max(cap, trail) if sign > 0 else min(cap, trail)
    if (anchor - cost) * sign >= params["breakeven"] * a:
        be = cost * (1 + sign * 0.001)
        trail_stop = max(trail_stop, be) if sign > 0 else min(trail_stop, be)
    stage = ("保本" if (anchor - cost) * sign >= params["breakeven"] * a else
             "跟踪" if (trail - cap) * sign > 0 else "限亏")
    ma20 = statistics.mean(r[2] for r in rows[-20:])
    # 风险分 0~100：仓位集中 30、单日风险 25、波动 15、跳空 15、跌破 20 日线 15
    risk_score = round(min(weight, 100) / 100 * 30 + min(var_pct_assets / 3, 1) * 25 + min(vol_y / 60, 1) * 15
                       + min(gap_through / 10, 1) * 15 + (15 if (price - ma20) * sign < 0 else 0))
    take_profit = cost + sign * params["target"] * a if params.get("target") else None
    exit_signals = []
    if take_profit and (price - take_profit) * sign >= 0:
        exit_signals.append(f"到达止盈 {take_profit:.2f}")
    if (price - trail_stop) * sign <= 0:
        exit_signals.append(f"跌破动态止损 {trail_stop:.2f}" if sign > 0 else f"涨破动态止损 {trail_stop:.2f}")
    if risk_score >= EXIT_SCORE:
        exit_signals.append(f"风险分 {risk_score} ≥ {EXIT_SCORE}")

    if (price - cost) * sign > 0 and (stop - cost) * sign > 0:
        reasons.append(f"目前浮盈已经超过 {stop_atr:g} 个 ATR，参考止损在成本之上")

    out.update(level=level, reasons=reasons, vol_y=vol_y, var95=var95, var_usd=var_usd, mdd=mdd,
               worst_day=worst_day, low20=low20, high20=high20, stop=stop, target=target, scenarios=scenarios,
               mode=mode, mode_name=mode_name, stop_atr=stop_atr, target_atr=target_atr,
               gap95=gap95, worst_gap=worst_gap, gap_through=gap_through, max_shares=max_shares,
               trail_k=trail_k, trail_stop=trail_stop, take_profit=take_profit, trail_pct=trail_k * a / price * 100, ma20=ma20,
               stop_stage=stage, max_loss=(cost - cap) * sign * qty, params=params, anchor=anchor,
               anchor_from=look[0][0] if look else None,
               stop_loss_usd=(cost - trail_stop) * sign * qty,
               risk_score=risk_score, exit_signals=exit_signals)
    return out


def assess_account(acct, reports):
    """账户整体：现金比例、持仓集中度、合计单日风险。"""
    total = acct.get("total_assets") or 0
    if not total:
        return {"level": None, "reasons": ["读不到总资产"]}
    cash_pct = (acct.get("cash") or 0) / total * 100
    var_sum = sum(r.get("var_usd") or 0 for r in reports.values())
    top = max(reports.values(), key=lambda r: r["weight"], default=None)
    factors = [
        (_level(var_sum / total * 100, 1.5, 4), f"所有持仓合计单日风险约 ${var_sum:,.2f}（占总资产 {var_sum / total * 100:.1f}%，按各只独立估算，偏保守）"),
        (2 if cash_pct < 5 else 1 if cash_pct < 15 else 0, f"现金占 {cash_pct:.0f}%" + ("，几乎满仓" if cash_pct < 5 else "")),
    ]
    if top:
        factors.append((_level(top["weight"], 30, 60), f"最大单只持仓占 {top['weight']:.0f}%，共 {len(reports)} 只"))
    level = max(f[0] for f in factors)
    return {"level": level, "cash_pct": cash_pct, "var_sum": var_sum,
            "reasons": [t for l, t in sorted(factors, key=lambda f: -f[0])]}
