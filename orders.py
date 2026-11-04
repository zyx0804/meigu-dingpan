"""挂单：给持仓挂止损 / 止盈的平仓单（GTC 长期有效，由 moomoo 服务器托管执行），以及买入板块的限价买单。

安全约束：
- 平仓单只能平掉已有持仓（多头卖出、空头买回），数量不超过持仓，不能反手
- 买单只能由 build_buy 生成，并且必须先通过 rules.buy_plan 的规则检查（仓位、风险、趋势）
- 每张单 / 每条自动规则都要用户在界面上点确认；之后只执行规则内的撤单、改单、平仓
- 默认模拟盘；真实账户必须由用户先在 moomoo OpenD 界面里手动「解锁交易」，程序不接触交易密码
"""

import time

MIN_FRAC_USD = 1.0  # moomoo 碎股单笔最低金额
ACTIVE = {"UNSUBMITTED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART"}


def build_buy(code, limit, qty):
    """买入单：限价、当日有效、只在常规交易时段。开仓只能走这里，并且必须先通过 rules.buy_plan 的检查。"""
    qty = round(float(qty), 4)
    qty = int(qty) if qty.is_integer() else qty
    limit = round(float(limit), 2)
    if limit <= 0:
        return None, "价格必须大于 0"
    if qty <= 0:
        return None, "数量必须大于 0"
    if not float(qty).is_integer() and qty * limit < MIN_FRAC_USD:
        return None, f"碎股单笔金额至少 ${MIN_FRAC_USD:g}"
    return {"order_type": "NORMAL", "aux_price": None, "price": limit, "code": "US." + code[2:], "qty": qty,
            "trd_side": "BUY", "kind": "buy", "time_in_force": "DAY", "session": "RTH"}, None


def build(pos, price, kind, trigger, qty, stop_limit=False):
    """生成一张平仓单的参数并做检查。
    kind: 'stop' 止损 / 'target' 止盈 / 'trail' 跟踪止损（trigger 此时是回撤百分比，如 4.5 表示 4.5%）。
    返回 (spec, 错误信息)。"""
    long = pos["side"] == "long"
    qty = int(qty) if float(qty).is_integer() else float(qty)
    trigger = round(float(trigger), 2)
    if qty <= 0:
        return None, "数量必须大于 0"
    if qty > pos["qty"]:
        return None, f"数量 {qty} 超过持仓 {pos['qty']:g} 股（只能平仓，不能加仓或反手）"
    if trigger <= 0:
        return None, "价格必须大于 0"
    side = "SELL" if long else "BUY_BACK"
    if kind == "exit":
        # 按规则立即离场：可立即成交的限价单（比现价差 0.5%），当日有效
        limit = round(price * (0.995 if long else 1.005), 2)
        return {"order_type": "NORMAL", "aux_price": None, "price": limit, "code": "US." + pos["code"][2:],
                "qty": qty, "trd_side": side, "kind": "exit", "time_in_force": "DAY", "session": "RTH"}, None
    if kind == "trail":
        if not 0.5 <= trigger <= 30:
            return None, "跟踪幅度应在 0.5% ~ 30% 之间"
        # 跟踪止损：从下单后的最高价（做空为最低价）回撤 trigger% 就按市价平仓，止损线只升不降
        return {"order_type": "TRAILING_STOP", "aux_price": None, "price": round(price, 2),
                "trail_type": "RATIO", "trail_value": trigger, "code": "US." + pos["code"][2:], "qty": qty,
                "trd_side": side, "kind": "trail", "time_in_force": "GTC", "session": "RTH"}, None
    if kind == "stop" and (trigger >= price if long else trigger <= price):
        return None, f"止损价 {trigger} 应该{'低于' if long else '高于'}现价 {price}，否则会立即触发"
    if kind == "target" and (trigger <= price if long else trigger >= price):
        return None, f"止盈价 {trigger} 应该{'高于' if long else '低于'}现价 {price}，否则会立即成交"

    if kind == "stop":
        # 止损限价：触发后最多比触发价差 1%，防止跳空时以过低价格成交（但可能卖不掉）
        limit = round(trigger * (0.99 if long else 1.01), 2)
        spec = {"order_type": "STOP_LIMIT" if stop_limit else "STOP", "aux_price": trigger,
                "price": limit if stop_limit else trigger}
    else:
        spec = {"order_type": "NORMAL", "aux_price": None, "price": trigger}  # 普通限价单，挂着等价格到
    spec.update(code="US." + pos["code"][2:], qty=qty, trd_side=side, kind=kind,
                time_in_force="GTC", session="RTH")
    return spec, None


KIND_NAME = {"stop": "止损", "target": "止盈", "trail": "跟踪止损", "exit": "规则离场", "buy": "买入"}


def describe(spec, env):
    kind = KIND_NAME[spec["kind"]]
    side = {"SELL": "卖出", "BUY": "买入"}.get(spec["trd_side"], "买回")
    if spec["order_type"] == "TRAILING_STOP":
        how = (f"从下单后的{'最高价回落' if spec['trd_side'] == 'SELL' else '最低价反弹'} {spec['trail_value']}% "
               f"时按市价{side}，止损线随价格自动移动")
    elif spec["order_type"] == "STOP":
        how = f"价格碰到 {spec['aux_price']} 时按市价{side}"
    elif spec["order_type"] == "STOP_LIMIT":
        how = f"价格碰到 {spec['aux_price']} 时挂限价 {spec['price']} {side}"
    elif spec["kind"] == "exit":
        how = f"限价 {spec['price']} 立即{side}"
    elif spec["kind"] == "buy":
        how = f"限价 {spec['price']} 买入（成交后自动挂动态止损）"
    else:
        how = f"限价 {spec['price']} {side}"
    acct = "真实账户" if env == "REAL" else "模拟盘"
    tif = "长期有效(GTC)" if spec["time_in_force"] == "GTC" else "当日有效"
    return f"[{acct}] {kind}：{spec['code']} {spec['qty']} 股，{how}，{tif}，仅常规交易时段"


class OrderDesk:
    """在已连接的 OpenSecTradeContext 上提交 / 查询 / 撤销订单。"""

    def __init__(self, link):
        self.link = link
        self._hist_at = {}  # 订单号 -> 上次查历史订单的时间

    def _acc(self, env):
        acc = self.link.acc_ids.get(env)
        if acc is None:
            raise RuntimeError("没有找到" + ("真实账户" if env == "REAL" else "模拟盘账户"))
        return acc

    def held(self, env, code):
        """下单前再查一次该账户的实际持仓，防止数量超出。"""
        from moomoo import RET_OK
        ret, df = self.link.trd.position_list_query(code="US." + code[2:], trd_env=env, acc_id=self._acc(env),
                                                    refresh_cache=True)
        if ret != RET_OK:
            raise RuntimeError(f"查询持仓失败：{df}")
        return float(df["can_sell_qty"].sum()) if not df.empty else 0.0

    def place(self, spec, env):
        from moomoo import RET_OK
        ret, df = self.link.trd.place_order(
            price=spec["price"], qty=spec["qty"], code=spec["code"], trd_side=spec["trd_side"],
            order_type=spec["order_type"], aux_price=spec["aux_price"], trd_env=env, acc_id=self._acc(env),
            time_in_force=spec["time_in_force"], session=spec["session"], remark="盯盘-" + spec["kind"],
            trail_type=spec.get("trail_type"), trail_value=spec.get("trail_value"))
        if ret != RET_OK:
            raise RuntimeError(str(df))
        return str(df["order_id"][0])

    def active_orders(self, env):
        from moomoo import RET_OK
        ret, df = self.link.trd.order_list_query(trd_env=env, acc_id=self._acc(env), refresh_cache=False)
        if ret != RET_OK:
            raise RuntimeError(f"查询订单失败：{df}")
        out = []
        for _, r in df.iterrows():
            if r["order_status"] in ACTIVE and str(r["code"]).startswith("US."):
                out.append({"env": env, "order_id": str(r["order_id"]), "code": r["code"], "name": r["stock_name"],
                            "side": r["trd_side"], "type": r["order_type"], "qty": r["qty"], "price": r["price"],
                            "aux": r.get("aux_price", ""), "tif": r.get("time_in_force", ""),
                            "status": r["order_status"], "time": r["create_time"]})
        return out

    def order_info(self, order_id, env):
        """订单状态 + 已成交数量 / 均价（买单成交后挂止损要用）。读 OpenD 缓存。"""
        from moomoo import RET_OK
        ret, df = self.link.trd.order_list_query(order_id=order_id, trd_env=env, acc_id=self._acc(env),
                                                 refresh_cache=False)
        if ret != RET_OK or df.empty:
            return {"status": self.order_status(order_id, env), "dealt_qty": None, "dealt_avg": None}
        r = df.iloc[0]
        return {"status": str(r["order_status"]), "dealt_qty": float(r["dealt_qty"] or 0),
                "dealt_avg": float(r["dealt_avg_price"] or 0)}

    def order_status(self, order_id, env):
        """查单个订单的状态。先读 OpenD 缓存（每秒查也不受限）；查不到再查历史订单（GTC 单可能跨天），
        历史查询有频率限制，每张单 30 秒最多查一次。"""
        from moomoo import RET_OK
        ret, df = self.link.trd.order_list_query(order_id=order_id, trd_env=env, acc_id=self._acc(env),
                                                 refresh_cache=False)
        if ret == RET_OK and not df.empty:
            return str(df.iloc[0]["order_status"])
        last = self._hist_at.get(order_id, 0)
        if time.time() - last < 30:
            raise RuntimeError(f"订单 {order_id} 暂时查不到，稍后再查")
        self._hist_at[order_id] = time.time()
        ret, df = self.link.trd.history_order_list_query(trd_env=env, acc_id=self._acc(env))
        if ret == RET_OK and not df.empty:
            hit = df[df["order_id"].astype(str) == str(order_id)]
            if not hit.empty:
                return str(hit.iloc[0]["order_status"])
        raise RuntimeError(f"查不到订单 {order_id}")

    def position_qty(self, env, code):
        """持仓总数（含被挂单冻结的部分），用来判断持仓还在不在。"""
        from moomoo import RET_OK
        ret, df = self.link.trd.position_list_query(code="US." + code[2:], trd_env=env, acc_id=self._acc(env),
                                                    refresh_cache=False)
        if ret != RET_OK:
            raise RuntimeError(f"查询持仓失败：{df}")
        return float(df["qty"].sum()) if not df.empty else 0.0

    def modify_stop(self, order_id, env, qty, trigger, price):
        """改止损单的触发价（和限价）。"""
        from moomoo import ModifyOrderOp, RET_OK
        ret, msg = self.link.trd.modify_order(ModifyOrderOp.NORMAL, order_id, qty, price, aux_price=trigger,
                                              trd_env=env, acc_id=self._acc(env))
        if ret != RET_OK:
            raise RuntimeError(str(msg))

    def cancel(self, order_id, env):
        from moomoo import ModifyOrderOp, RET_OK
        ret, msg = self.link.trd.modify_order(ModifyOrderOp.CANCEL, order_id, 0, 0, trd_env=env, acc_id=self._acc(env))
        if ret != RET_OK:
            raise RuntimeError(str(msg))
