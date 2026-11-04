"""交易规则：一键挂单时程序严格按这里的规则给每只持仓生成动作，不做规则以外的事。"""
import orders
import risk


def rules_text(params=None):
    p = {**risk.DEFAULT_PARAMS, **(params or {})}
    return [
        "只处理真实账户里已有的美股持仓，只平仓，不开新仓、不加仓、不反手",
        "每只持仓必须有止损保护；已有运行中的自动规则就跳过，不重复挂",
        f"动态止损 = 三条取最有利：相对成本最多亏 {p['loss_cap']:g} ATR；买入后最高点回撤 {p['trail']:g} ATR 跟踪；"
        f"浮盈 {p['breakeven']:g} ATR 后保本",
        "止损单挂在 moomoo 服务器（关机也触发），程序每秒对照动态止损，只往有利方向自动改单，永不放宽",
        f"止盈 = 成本 + {p['target']:g} ATR：常规交易时段价格到了，程序先撤止损、再限价全部卖出（需要程序和 OpenD 开着）",
        "价格已经跌破动态止损（做空为涨破）或已到止盈的持仓：不再挂保护单，按规则立即限价离场",
        "风险分 ≥ 70 但价格没破动态止损：照常挂保护单；是否提前离场由你勾选（默认不勾）",
        "所有单只在美股常规交易时段触发；数量不超过可卖数量",
        "现金账户 T+1：当天买入的股票当天不止盈卖出（防止用未交收资金买的股票当天卖出而违规），止损照常有效",
    ]


RULES = rules_text()


def buy_rules_text(buy_cfg, params=None):
    p = {**risk.DEFAULT_PARAMS, **(params or {})}
    return [
        "只买美股个股（不买指数），只用限价单，当日有效，只在常规交易时段成交",
        "趋势条件：收盘价站上 20 日均线，并且近 20 个交易日是上涨的；不满足就不允许买",
        f"止损先定好再买：止损价 = 买入价 − {p['loss_cap']:g} ATR；止盈 = 买入价 + {p['target']:g} ATR（和持仓同一套规则）",
        f"股数 = 以下三者取最小：打到止损最多亏总资产 {buy_cfg['risk_pct']:g}%；"
        f"这只股票市值不超过总资产 {buy_cfg['max_pos_pct']:g}%（含已持有部分）；不超过购买力",
        "算出来不足 1 股就不买，并告诉你差多少",
        "同一只股票已有未成交的买单时不重复下单",
        "成交后自动挂上动态止损单并交给自动规则管理（自动上调、自动撤单）",
    ]


def buy_plan(code, quote, rows, acct, held_qty, buy_cfg, params=None, pending_codes=()):
    """按买入规则算出计划。quote：现价等；rows：日 K（至少 60 根）；held_qty：已持有股数。
    返回 dict：ok（能不能买）、reasons（不能买的原因）、以及价格 / 股数 / 止损 / 风险。"""
    p = {**risk.DEFAULT_PARAMS, **(params or {})}
    out = {"code": code, "name": quote.get("name", code), "ok": False, "reasons": [], "checks": []}
    price = quote.get("price")
    if not price or len(rows) < 30:
        out["reasons"].append("拿不到现价或历史数据")
        return out
    closes = [r[2] for r in rows]
    ma20 = sum(closes[-20:]) / 20
    ret20 = closes[-1] / closes[-21] - 1
    a = risk.atr(rows)
    limit = round(price * 1.002, 2)  # 比现价高 0.2% 的限价，基本能成交又不会追太高
    stop = round(limit - p["loss_cap"] * a, 2)
    target = round(limit + p["target"] * a, 2)
    per_share = limit - stop
    total = (acct or {}).get("total_assets") or 0
    power = (acct or {}).get("power") or 0

    trend_ok = price > ma20 and ret20 > 0
    out["checks"] += [(f"现价 {price} {'>' if price > ma20 else '≤'} 20 日均线 {ma20:.2f}", price > ma20),
                      (f"近 20 日涨跌 {ret20 * 100:+.1f}%", ret20 > 0)]
    if not trend_ok:
        out["reasons"].append("不满足趋势条件（需要站上 20 日线且 20 日上涨）")
    if code in pending_codes:
        out["reasons"].append("这只股票已有未成交的买单")
    if not total:
        out["reasons"].append("读不到账户总资产（OpenD 是否已连接？）")

    by_risk = int(total * buy_cfg["risk_pct"] / 100 // per_share) if per_share > 0 else 0
    by_pos = int(max(0.0, total * buy_cfg["max_pos_pct"] / 100 - held_qty * price) // limit)
    by_cash = int(power * 0.99 // limit)
    qty = max(0, min(by_risk, by_pos, by_cash))
    out["checks"] += [(f"按风险最多 {by_risk} 股（亏损上限 ${total * buy_cfg['risk_pct'] / 100:,.2f}，"
                       f"每股风险 ${per_share:.2f}）", by_risk >= 1),
                      (f"按仓位上限最多 {by_pos} 股（{buy_cfg['max_pos_pct']:g}% 总资产，已持有 {held_qty:g} 股）",
                       by_pos >= 1),
                      (f"按购买力最多 {by_cash} 股（购买力 ${power:,.2f}）", by_cash >= 1)]
    if total and qty < 1:
        need = []
        if by_risk < 1:
            need.append(f"风险预算不够：买 1 股要总资产至少 ${per_share / (buy_cfg['risk_pct'] / 100):,.0f}")
        if by_pos < 1:
            room = max(0.0, total * buy_cfg["max_pos_pct"] / 100 - held_qty * price)
            need.append(f"仓位上限只剩 ${room:,.2f}，不够 1 股" if held_qty == 0 else
                        f"已持有 {held_qty:g} 股，仓位已到上限")
        if by_cash < 1:
            need.append(f"购买力不够：1 股需要 ${limit:,.2f}")
        out["reasons"].append("算出来不足 1 股：" + "；".join(need))
    out.update(price=price, limit=limit, stop=stop, target=target, atr=a, qty=qty, per_share=per_share,
               cost=qty * limit, max_loss=qty * per_share, max_loss_pct=qty * per_share / total * 100 if total else None)
    out["ok"] = not out["reasons"]
    return out


def plan(positions, reports, active_codes, t1=()):
    """返回每只持仓的计划动作列表。active_codes：已有运行中规则的代码；
    t1：今天不能止盈卖出的代码（现金账户当天买入的）——到了止盈也只挂保护单，不离场。"""
    items = []
    for p in positions:
        c, r = p["code"], reports.get(p["code"])
        item = {"code": c, "name": p["name"], "qty": p["qty"], "pos": p, "specs": [], "optional_exit": None}
        if c in active_codes:
            item.update(action="skip", why="已有运行中的自动规则")
        elif not r or r.get("level") is None or not r.get("trail_stop"):
            item.update(action="skip", why="历史数据不足，暂时无法按规则计算")
        else:
            price = r["price"]
            broke = any(s.startswith(("跌破动态止损", "涨破动态止损")) for s in r["exit_signals"])
            tp = r.get("take_profit")
            hit = tp and any(s.startswith("到达止盈") for s in r["exit_signals"]) and c not in t1
            if broke or hit:
                s, e = orders.build(p, price, "exit", price, p["qty"])
                why = f"已破动态止损 {r['trail_stop']:.2f}" if broke else f"已到止盈 {tp:.2f}"
                item.update(action="exit", why=why, specs=[s] if s else [], error=e)
            else:
                st, e = orders.build(p, price, "stop", round(r["trail_stop"], 2), p["qty"])
                tg, e2 = orders.build(p, price, "target", round(tp, 2), p["qty"]) if tp else (None, None)
                if c in t1 and e2:  # 当天买入且现价已过止盈：止盈留给程序盯价，明天再卖
                    tg, e2 = None, None
                loss = r["stop_loss_usd"]
                item.update(action="protect", specs=[x for x in (st, tg) if x] if st else [], error=e or e2, why=(
                    f"止损 {r['trail_stop']:.2f}（阶段：{r['stop_stage']}），"
                    + (f"打到止损亏 ${loss:,.2f}" if loss > 0 else f"打到止损仍赚 ${-loss:,.2f}")
                    + f"；最多亏 ${r['max_loss']:,.2f}，之后程序每秒自动上调"
                    + (f"；止盈 {tp:.2f}" if tp else "")))
                if r["risk_score"] is not None and r["risk_score"] >= 70:
                    ex, _ = orders.build(p, price, "exit", price, p["qty"])
                    item["optional_exit"] = ex
                    item["why"] += f"；风险分 {r['risk_score']} ≥ 70，可选提前离场"
        items.append(item)
    return items
