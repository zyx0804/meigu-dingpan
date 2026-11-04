"""止损 + 止盈联动规则（OCO，一边成交另一边自动撤）。

- 止损单（固定或跟踪）挂在 moomoo 服务器上，电脑关机也会触发
- 止盈：先尝试也挂到服务器；moomoo 不允许同一批股票挂两张卖单时，改由本程序盯价，
  价格到了先撤止损单，撤成功后再卖出（程序和 OpenD 需要开着）
- 只会平掉建立规则时的那部分持仓；持仓没了（比如你手动卖了）就撤掉剩下的单并结束规则
规则由你在界面上确认一次后建立，之后按规则自动执行，每一步都写进日志。
"""
import time

MIN_STEP = 0.002   # 动态止损至少比原来高 0.2% 才去改单，避免频繁改（改单接口 30 秒限 20 次）
GAP_TO_PRICE = 0.005  # 新止损至少离现价 0.5%，免得一改就触发
DONE = {"FILLED_ALL"}
DEAD = {"CANCELLED_ALL", "CANCELLED_PART", "DELETED", "FAILED", "SUBMIT_FAILED", "DISABLED", "FILL_CANCELLED"}


class BracketManager:
    def __init__(self, desk, store, log):
        """store：持久化的规则列表（存在 stocks.json 里）；log(msg, alert=False)：写日志，alert=True 的会响铃并推送到手机。"""
        self.desk, self.store, self.log = desk, store, log

    def create(self, stop_spec, target_spec, env, server_target=True):
        """先挂止损，再尝试挂止盈（target_spec 可以为 None：只有止损，程序负责自动跟踪）。
        server_target=False：止盈不挂到服务器，直接由程序盯价（现金账户用：服务器止盈单可能在资金交收前成交而违规）。
        返回 (规则, 结果说明列表)。"""
        notes = []
        stop_id = self.desk.place(stop_spec, env)
        notes.append(f"✓ 止损已挂到 moomoo 服务器，订单号 {stop_id}")
        b = {"id": f"R{time.strftime('%m%d%H%M%S')}-{len(self.store) + 1}", "env": env, "code": stop_spec["code"], "qty": stop_spec["qty"],
             "side": stop_spec["trd_side"], "stop_id": stop_id, "stop_desc": stop_spec,
             "target": target_spec["price"] if target_spec else None, "target_id": None, "mode": "server",
             "status": "active", "ratchet": True, "created": time.strftime("%Y-%m-%d %H:%M")}
        if stop_spec["order_type"] in ("STOP", "STOP_LIMIT"):
            notes.append("· 固定止损会跟着动态止损自动上调（只升不降），每次调整都写进日志")
        if target_spec and not server_target:
            b["mode"] = "local"
            notes.append(f"止盈 {b['target']} 由程序盯价（现金账户：当天买入的股票第二个交易日才会止盈卖出，T+1）")
        elif target_spec:
            try:
                b["target_id"] = self.desk.place(target_spec, env)
                notes.append(f"✓ 止盈也挂到了服务器，订单号 {b['target_id']}；一边成交后程序会撤掉另一边")
            except Exception as e:
                b["mode"] = "local"
                notes.append(f"止盈没能挂到服务器（{str(e)[:60]}），改由本程序盯价：价格到 {b['target']} "
                             "时先撤止损再卖出——这部分需要程序和 OpenD 开着")
        self.store.append(b)
        tgt = f"止盈 {b['target']}（{'服务器挂单' if b['mode'] == 'server' else '程序盯价'}）" if b["target"] else "无止盈"
        self.log(f"建立规则 {b['id']} {b['code']} {b['qty']} 股：止损单 {stop_id}，{tgt}")
        return b, notes

    def create_local(self, stop_spec, target_spec, env):
        """碎股：moomoo 不支持给碎股挂止损单，止损和止盈都由程序盯价，到价时限价卖出。不在服务器上挂任何单。"""
        b = {"id": f"R{time.strftime('%m%d%H%M%S')}-{len(self.store) + 1}", "env": env, "code": stop_spec["code"],
             "qty": stop_spec["qty"], "side": stop_spec["trd_side"], "stop_id": None, "stop_local": True,
             "stop_desc": stop_spec, "target": target_spec["price"] if target_spec else None, "target_id": None,
             "mode": "local", "status": "active", "ratchet": True, "created": time.strftime("%Y-%m-%d %H:%M")}
        self.store.append(b)
        tgt = f"止盈 {b['target']}" if b["target"] else "无止盈"
        note = (f"碎股 {b['qty']:g} 股：moomoo 不支持碎股止损单，止损 {stop_spec['aux_price']}、{tgt} 都由程序盯价，"
                "常规时段到价就限价卖出（这部分需要程序和 OpenD 开着）")
        self.log(f"建立规则 {b['id']} {b['code']} {note}")
        return b, [note]

    def cancel_rule(self, b):
        for oid in (b["stop_id"], b["target_id"]):
            if oid:
                try:
                    self.desk.cancel(oid, b["env"])
                except Exception:
                    pass
        b["status"] = "cancelled"
        self.log(f"规则 {b['id']} 已手动停止，相关挂单已撤")

    def tick(self, prices, held, dyn_stops=None, targets=None, rth=True, hold=()):
        """后台定时调用。prices：代码(usXXX) -> 现价；held(env, usXXX) -> 持仓数；
        dyn_stops：代码 -> 程序算出的动态止损价，用来自动上调固定止损单；
        targets：代码 -> 按当前规则算出的止盈价（程序盯价的规则会跟着更新，没有止盈的会补上）；
        rth：是否在常规交易时段。盘前盘后、夜盘不触发止盈（不在那时撤掉止损）；
        hold：今天不能止盈卖出的代码（现金账户当天买入的，T+1）。止损单在服务器上，不受影响。"""
        for b in [x for x in self.store if x["status"] == "active"]:
            try:
                local = "us" + b["code"].split(".", 1)[1]
                tp = (targets or {}).get(local)
                if tp and b.get("phase") != "selling" and (b["mode"] == "local" or not b["target"]):
                    tp = round(tp, 2)
                    if b["target"] != tp:
                        if not b["target"]:
                            self.log(f"规则 {b['id']} 按当前规则补上止盈 {tp}（程序盯价）")
                        b.update(target=tp, mode="local")
                self._tick_one(b, prices, held, rth, local in hold)
                if b["status"] == "active" and b.get("ratchet", True) and dyn_stops:
                    self._ratchet(b, prices, dyn_stops)
            except Exception as e:
                self.log(f"规则 {b['id']} 检查出错（下次重试）：{str(e)[:80]}")

    def _ratchet(self, b, prices, dyn_stops):
        """固定止损单跟着动态止损往有利方向移：做多只上调、做空只下调，永远不放宽。
        跟踪止损单由 moomoo 服务器自己跟踪，不需要改。"""
        sd = b["stop_desc"]
        if sd["order_type"] not in ("STOP", "STOP_LIMIT") or b.get("phase") in ("selling", "stopping"):
            return
        local = "us" + b["code"].split(".", 1)[1]
        new, px = dyn_stops.get(local), prices.get(local)
        if not new or not px:
            return
        long = b["side"] == "SELL"
        cur = sd["aux_price"]
        better = new > cur * (1 + MIN_STEP) if long else new < cur * (1 - MIN_STEP)
        safe = new < px * (1 - GAP_TO_PRICE) if long else new > px * (1 + GAP_TO_PRICE)
        if not (better and safe):
            return
        new = round(new, 2)
        limit = round(new * (0.99 if long else 1.01), 2) if sd["order_type"] == "STOP_LIMIT" else new
        if not b.get("stop_local"):  # 程序盯价的止损只改记下来的价格，不用改单
            self.desk.modify_stop(b["stop_id"], b["env"], b["qty"], new, limit)
        sd.update(aux_price=new, price=limit)
        b["ratchets"] = b.get("ratchets", 0) + 1
        self.log(f"规则 {b['id']} 自动跟踪：止损从 {cur} {'上调' if long else '下调'}到 {new}（现价 {px}）")

    def _tick_local(self, b, prices, held, rth, t1):
        """程序盯价的止损 + 止盈（碎股）：常规时段到价就挂可立即成交的限价卖单；没成交就下一轮再挂。"""
        env, local = b["env"], "us" + b["code"].split(".", 1)[1]
        phase = b.get("phase")
        if phase in ("stopping", "selling"):
            st = self.desk.order_status(b["exit_id"], env)
            what = "止损" if phase == "stopping" else "止盈"
            if st in DONE:
                b["status"] = "done"
                self.log(f"规则 {b['id']} 程序{what}卖单已成交，规则结束", alert=True)
            elif st in DEAD:
                b.update(phase=None, exit_id=None)
                self.log(f"规则 {b['id']} 程序{what}卖单没成交（{st}），继续盯价", alert=True)
            return
        if held(env, local) <= 0:
            b["status"] = "done"
            self.log(f"规则 {b['id']} {b['code']} 已无持仓，规则结束", alert=True)
            return
        px = prices.get(local)
        if not px or not rth:
            return
        long = b["side"] == "SELL"
        stop = b["stop_desc"]["aux_price"]
        if px <= stop if long else px >= stop:
            phase, why = "stopping", f"跌破止损 {stop}" if long else f"涨破止损 {stop}"
        elif b["target"] and not t1 and (px >= b["target"] if long else px <= b["target"]):
            phase, why = "selling", f"到达止盈 {b['target']}"
        else:
            return
        limit = round(px * (0.995 if long else 1.005), 2)
        spec = {"order_type": "NORMAL", "aux_price": None, "price": limit, "code": b["code"], "qty": b["qty"],
                "trd_side": b["side"], "kind": "exit" if phase == "stopping" else "target", "time_in_force": "DAY",
                "session": "RTH"}
        b["exit_id"] = self.desk.place(spec, env)
        b["phase"] = phase
        self.log(f"规则 {b['id']} 价格 {px} {why}：限价 {limit} 卖出 {b['qty']:g} 股，订单号 {b['exit_id']}", alert=True)

    def _tick_one(self, b, prices, held, rth=True, t1=False):
        if b.get("stop_local"):
            return self._tick_local(b, prices, held, rth, t1)
        env, local = b["env"], "us" + b["code"].split(".", 1)[1]
        stop_st = self.desk.order_status(b["stop_id"], env)
        tgt_st = self.desk.order_status(b["target_id"], env) if b["target_id"] else None

        if b.get("phase") == "selling":
            # 为了止盈撤掉了止损、挂了限价卖单：卖单成交就结束；没成交（过期/被撤）就把止损挂回去
            if tgt_st in DONE:
                b["status"] = "done"
                self.log(f"规则 {b['id']} 止盈卖单已成交，规则结束", alert=True)
            elif tgt_st in DEAD:
                b["stop_id"] = self.desk.place(b["stop_desc"], env)
                b.update(target_id=None, mode="local", phase=None)
                self.log(f"规则 {b['id']} 止盈卖单没成交（{tgt_st}），已重新挂回止损单 {b['stop_id']}", alert=True)
            return

        if stop_st in DONE or tgt_st in DONE:
            other = b["target_id"] if stop_st in DONE else b["stop_id"]
            if other:
                self._cancel_quiet(other, env)
            b["status"] = "done"
            self.log(f"规则 {b['id']} {'止损' if stop_st in DONE else '止盈'}已成交，已撤销另一边，规则结束", alert=True)
            return
        if held(env, local) <= 0:  # 持仓已经没了（你手动卖掉了等）
            for oid in (b["stop_id"], b["target_id"]):
                if oid:
                    self._cancel_quiet(oid, env)
            b["status"] = "done"
            self.log(f"规则 {b['id']} {b['code']} 已无持仓，撤掉剩余挂单，规则结束", alert=True)
            return
        if stop_st in DEAD:
            b["status"] = "done"
            self.log(f"规则 {b['id']} 的止损单状态为 {stop_st}（可能被你在 moomoo 里撤了），规则结束", alert=True)
            return

        if b["mode"] == "local" and b["target"] and rth:
            px = prices.get(local)
            long = b["side"] == "SELL"
            if px and (px >= b["target"] if long else px <= b["target"]) and t1:
                today = time.strftime("%Y-%m-%d")
                if b.get("t1_note") != today:
                    b["t1_note"] = today
                    self.log(f"规则 {b['id']} 价格 {px} 已到止盈 {b['target']}，但这只今天刚买（现金账户 T+1），"
                             "下个交易日还在止盈价以上再卖；止损照常有效")
            elif px and (px >= b["target"] if long else px <= b["target"]):
                # 先撤止损，确认撤掉了再卖，避免两边都成交
                self.desk.cancel(b["stop_id"], env)
                if self.desk.order_status(b["stop_id"], env) not in DEAD:
                    self.log(f"规则 {b['id']} 到达止盈价，但止损单还没撤掉，下次再试")
                    return
                limit = round(px * (0.995 if long else 1.005), 2)  # 可立即成交的限价，防止滑太多
                spec = {"order_type": "NORMAL", "aux_price": None, "price": limit, "code": b["code"],
                        "qty": b["qty"], "trd_side": b["side"], "kind": "target", "time_in_force": "DAY",
                        "session": "RTH"}
                try:
                    b["target_id"] = self.desk.place(spec, env)
                except Exception as e:  # 卖单挂不出去：马上把止损挂回去，不能让持仓裸奔
                    b["stop_id"] = self.desk.place(b["stop_desc"], env)
                    self.log(f"规则 {b['id']} 止盈卖单失败（{str(e)[:60]}），已重新挂回止损单 {b['stop_id']}", alert=True)
                    return
                b["phase"] = "selling"
                self.log(f"规则 {b['id']} 价格 {px} 到达止盈 {b['target']}：已撤止损，限价 {limit} 卖出，订单号 {b['target_id']}", alert=True)

    def _cancel_quiet(self, oid, env):
        try:
            self.desk.cancel(oid, env)
        except Exception:
            pass
