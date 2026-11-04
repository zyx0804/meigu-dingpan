"""无界面引擎：行情、持仓风险、自动规则、选股、回测、复盘、买入。网页版通过它工作。

规则和桌面版完全一样（共用 risk / rules / brackets / orders / screener / backtest / journal / tuner）。
"""
import json
import os
import threading
import time
from datetime import datetime

import backtest
import brackets
import journal
import notify
import orders
import risk
import rules
import screener
import tuner
from common import (DATA, ENV_NAME, INDICES, fetch, fetch_daily, last_session, load, market_open, normalize, save, us_date,
                    us_now)
from moomoo_link import MoomooLink, opend_running

STATUS_NAME = {"active": "运行中", "done": "已结束", "cancelled": "已停止"}
AUTO_PROTECT_WAIT = 30    # 发现没保护的持仓后等 30 秒再挂，给程序自己的买单流程留时间，也避免读到不完整的持仓
AUTO_PROTECT_RETRY = 600  # 挂不上（没解锁交易、可卖不足等）时 10 分钟后再试
DOWN_ALERT_SECS = 120     # moomoo 连接断开超过 2 分钟就推送到手机
CHECK_FROM, CHECK_TO = (9, 10), (9, 30)  # 开盘前自检：美东 9:10~9:30 之间做一次


class Engine:
    def __init__(self):
        self.cfg = load()
        self.lock = threading.RLock()  # 网页请求线程和后台线程共用配置，改动时加锁
        # 下单类操作一次只做一笔：连点两次或开两个网页时，第二次要等第一次下完、记好账再重新检查，不会重复下单
        self.trade_lock = threading.Lock()
        self.quotes, self.hist, self.reports = {}, {}, {}
        self.fired = {}
        self.link = MoomooLink()
        self.desk = orders.OrderDesk(self.link)
        self.bm = brackets.BracketManager(self.desk, self.cfg["brackets"],
                                          lambda m, alert=False: self.log(m, order=True, alert=alert))
        if not self.cfg.get("push", {}).get("topic"):
            self.cfg["push"] = {"enabled": False, "server": notify.DEFAULT_SERVER, "topic": notify.new_topic()}
            save(self.cfg)
        self.push = notify.Pusher(self.cfg["push"])
        self._down_since, self._down_alerted, self._unlock_err = None, False, 0
        self.check_result = None
        self.acct, self.positions = None, []
        self.acc_status = "未连接：请先打开 moomoo OpenD 并登录"
        self.order_rows, self.order_note = [], ""
        self.logs, self.events = [], []  # events：给网页弹提醒用
        self.screening = self.backtesting = False
        self._unprot, self._ap_retry = {}, {}  # 自动保护：代码 -> 第一次发现没保护的时间 / 下次重试时间
        self.updated = ""
        os.makedirs(DATA, exist_ok=True)

    # ---------- 日志 / 提醒 ----------
    def log(self, msg, order=False, alert=False):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        low = msg.lower()
        if ("unlock" in low or "解锁" in msg) and not msg.startswith("开盘前自检"):
            self._unlock_err = time.time()
        if alert and getattr(self, "push", None) and "风险分" not in msg:  # 风险分每天都会报，不推送
            urgent = any(k in msg for k in ("止损", "失败", "⚠", "断开", "离场", "撤"))
            self.push.send("美股盯盘", msg, priority=4 if urgent else 3)
        with self.lock:
            self.logs.insert(0, line)
            del self.logs[200:]
            if alert:
                self.events.append({"id": time.time(), "msg": msg})
                del self.events[:-50]
        if order:
            with open(os.path.join(DATA, "orders.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        if alert:
            with open(os.path.join(DATA, "alerts.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d} {line}\n")

    def save(self):
        with self.lock:
            save(self.cfg)

    # ---------- 后台循环：每秒一轮 ----------
    def start(self):
        threading.Thread(target=self.loop, daemon=True).start()

    def loop(self):
        last_acc = last_tx = last_daily = 0
        base = {}
        while True:
            t0 = time.time()
            if t0 - last_acc >= 5:
                last_acc = t0
                self.refresh_account()
                self.refresh_orders()
                self.check_pending_buys()
                self.auto_protect()
                self.watch_connection(t0)
                self.maybe_self_check()
            if t0 - last_daily >= 60:
                last_daily = t0
                self.start_screen()
                self.start_backtest()
            try:
                codes = list(dict.fromkeys(self.cfg["stocks"] + [p["code"] for p in self.positions]))
                snap = {}
                if self.link.quote:
                    try:
                        snap = self.link.snapshot(codes)
                    except Exception:
                        snap = {}
                if not snap or t0 - last_tx >= 10 or any(c not in base for c in codes):
                    base = fetch(codes)
                    last_tx = t0
                self.quotes = {c: {**base.get(c, {}), **snap.get(c, {})} for c in codes if c in base or c in snap}
                self.update_hist()
                self.compute_positions()
                self.check_alerts()
                self.run_brackets()
                self.save_close()
                self.updated = datetime.now().strftime("%H:%M:%S")
            except Exception as e:
                self.updated = "获取失败：" + str(e)[:40]
            time.sleep(max(0.2, self.cfg.get("interval", 1) - (time.time() - t0)))

    def refresh_account(self):
        if not opend_running():
            self.link.close()
            self.acct, self.positions = None, []
            self.acc_status = "未连接：请先打开 moomoo OpenD 并登录"
            return
        try:
            if not self.link.trd:
                self.link.connect()
            self.acct = self.link.account()
            self.positions = self.link.positions()
            self.acc_status = "已连接"
        except Exception as e:
            self.link.close()
            self.acc_status = "moomoo 读取失败：" + str(e)[:60]

    def refresh_orders(self):
        if not self.link.trd:
            self.order_rows, self.order_note = [], "未连接 moomoo"
            return
        rows, notes = [], []
        for e in ("SIMULATE", "REAL"):
            try:
                for o in self.desk.active_orders(e):
                    rows.append({**o, "env_name": ENV_NAME[e]})
            except Exception as ex:
                notes.append(f"{ENV_NAME[e]}：{str(ex)[:40]}")
        self.order_rows, self.order_note = rows, "；".join(notes)

    def update_hist(self):
        today = us_date()
        for p in self.positions:
            c = p["code"]
            if self.hist.get(c, (None,))[0] == today or "symbol" not in self.quotes.get(c, {}):
                continue
            try:
                self.hist[c] = (today, fetch_daily(self.quotes[c]["symbol"]))
            except Exception:
                pass

    def _cash(self):
        return self.link.acc_type == "CASH"

    def t1_hold(self):
        """现金账户里今天买入过的代码：今天不止盈卖出（T+1）。"""
        if not self._cash():
            return set()
        return {p["code"] for p in self.positions if p.get("today_buy", 0) > 0}

    def active_rule_codes(self):
        return {"us" + b["code"].split(".", 1)[1] for b in self.cfg["brackets"]
                if b["status"] == "active" and b["env"] == "REAL"}

    def compute_positions(self):
        a, reports = self.acct, {}
        for p in self.positions:
            c = p["code"]
            price = self.quotes.get(c, {}).get("price", p["price"])
            r = risk.assess(p, price, a and a["total_assets"], self.hist.get(c, (None, []))[1], self.cfg["mode"],
                            self.cfg["bt"].get("k"), params=self.cfg["rule_params"],
                            entry_date=journal.entry_date(self.cfg["journal"].get("open", {}), c))
            r.update(code=c, name=p["name"], price=price, pos=p)
            reports[c] = r
            today = us_date()
            for sig in r["exit_signals"]:
                key = (c, "exit:" + sig.split(" ")[0])
                if self.fired.get(key) != today:
                    self.fired[key] = today
                    self.log(f"离场信号 · {p['name']}({c})：{sig}（现价 {price}）", alert=True)
        self.reports = reports

    def run_brackets(self):
        if not self.link.trd or not any(b["status"] == "active" for b in self.cfg["brackets"]):
            return
        prices = {c: d.get("price") for c, d in self.quotes.items()}
        dyn = {c: r["trail_stop"] for c, r in self.reports.items() if r.get("trail_stop")}
        tps = {c: r["take_profit"] for c, r in self.reports.items() if r.get("take_profit")}
        with self.lock:
            before = json.dumps(self.cfg["brackets"], sort_keys=True, default=str)
            self.bm.tick(prices, self.desk.position_qty, dyn, tps, market_open(), self.t1_hold())
            if json.dumps(self.cfg["brackets"], sort_keys=True, default=str) != before:
                save(self.cfg)

    # ---------- 到价提醒 ----------
    def check_alerts(self):
        today = us_date()
        for code, a in list(self.cfg["alerts"].items()):
            d = self.quotes.get(code)
            if not d:
                continue
            conds = [("above", a.get("above") and d["price"] >= a["above"], f"涨到 {d['price']}（≥{a.get('above')}）"),
                     ("below", a.get("below") and d["price"] <= a["below"], f"跌到 {d['price']}（≤{a.get('below')}）"),
                     ("pct", a.get("pct") and abs(d["pct"]) >= a["pct"], f"涨跌幅 {d['pct']:+.2f}%（超过 ±{a.get('pct')}%）")]
            for kind, ok, msg in conds:
                if ok and self.fired.get((code, kind)) != today:
                    self.fired[(code, kind)] = today
                    self.log(f"{d['name']}({code}) {msg}", alert=True)

    def set_alert(self, code, above=None, below=None, pct=None):
        new = {k: float(v) for k, v in (("above", above), ("below", below), ("pct", pct)) if v not in (None, "", 0)}
        with self.lock:
            if new:
                self.cfg["alerts"][code] = new
            else:
                self.cfg["alerts"].pop(code, None)
            for k in ("above", "below", "pct"):
                self.fired.pop((code, k), None)
            save(self.cfg)

    # ---------- 自选 ----------
    def add_stock(self, text):
        code = normalize(text or "")
        if not code:
            raise ValueError("请输入美股代码，例如 AAPL、NVDA、BRK.B；指数：DJI、IXIC、INX")
        with self.lock:
            if code not in self.cfg["stocks"]:
                self.cfg["stocks"].append(code)
            if code in self.cfg["auto"]:
                self.cfg["auto"].remove(code)
            self.cfg["ignore"].pop(code, None)
            save(self.cfg)
        return code

    def remove_stock(self, code):
        with self.lock:
            for key in ("stocks", "auto", "locked"):
                if code in self.cfg[key]:
                    self.cfg[key].remove(code)
            self.cfg["ignore"][code] = datetime.now().strftime("%Y-%m-%d")
            self.cfg["alerts"].pop(code, None)
            save(self.cfg)

    def toggle_lock(self, code):
        with self.lock:
            lk = self.cfg["locked"]
            lk.remove(code) if code in lk else lk.append(code)
            save(self.cfg)

    def set_option(self, key, value):
        with self.lock:
            if key == "mode" and value in risk.MODES:
                self.cfg["mode"] = value
            elif key in ("auto_screen", "auto_protect"):
                self.cfg[key] = bool(value)
            elif key == "push_enabled":
                self.cfg["push"]["enabled"] = bool(value)
            elif key in ("risk_pct", "max_pos_pct"):
                v = float(value)
                lim = (0.1, 10) if key == "risk_pct" else (5, 100)
                if not lim[0] <= v <= lim[1]:
                    raise ValueError(f"{key} 应在 {lim[0]}~{lim[1]} 之间")
                self.cfg["buy"][key] = v
            else:
                raise ValueError("未知设置")
            save(self.cfg)

    # ---------- 选股 ----------
    def start_screen(self, force=False):
        # 按「最近一个已收盘的交易日」判断：盘中不会用没收完的 K 线去清除自选
        if self.screening or not (force or (self.cfg["auto_screen"] and self.cfg["screen_date"] != last_session())):
            return
        self.screening = True
        threading.Thread(target=self._screen, daemon=True).start()

    def _screen(self):
        try:
            pool = dict.fromkeys(["us" + c for c in screener.UNIVERSE] +
                                 [c for c in self.cfg["stocks"] if c not in INDICES])
            q = fetch(list(pool))
            ranked = screener.run({c: d["symbol"] for c, d in q.items()})
            for x in ranked:
                x["name"] = q.get(x["code"], {}).get("name", x["code"])
            if not ranked:
                raise RuntimeError("没拿到数据")
            self._apply_screen(ranked)
        except Exception as e:
            self.log(f"自动选股失败：{e}")
        finally:
            self.screening = False

    def _apply_screen(self, ranked):
        today, session = datetime.now(), last_session()
        with self.lock:
            cutoff = today.toordinal() - 7
            self.cfg["ignore"] = {c: d for c, d in self.cfg["ignore"].items()
                                  if datetime.strptime(d, "%Y-%m-%d").toordinal() > cutoff}
            protected = set(INDICES) | {p["code"] for p in self.positions} | set(self.cfg["locked"])
            add, remove = screener.plan_changes(ranked, self.cfg["stocks"], protected, set(self.cfg["ignore"]))
            for c in remove:
                for key in ("auto", "stocks"):
                    if c in self.cfg[key]:
                        self.cfg[key].remove(c)
                self.cfg["alerts"].pop(c, None)
            for c in add:
                self.cfg["auto"].append(c)
                if c not in self.cfg["stocks"]:
                    self.cfg["stocks"].append(c)
            self.cfg["screen"] = {x["code"]: {"score": x["score"], "rank": x["rank"], "reason": x["reason"],
                                              "name": x["name"], "eligible": x["eligible"]} for x in ranked}
            self.cfg["screen_date"] = session
            save(self.cfg)
        by = {x["code"]: x for x in ranked}
        name = lambda c: by.get(c, {}).get("name", c)
        with open(os.path.join(DATA, f"screen_{session}.csv"), "w", encoding="utf-8-sig") as f:
            f.write("排名,代码,名称,评分,符合条件,理由,本次操作\n")
            for x in ranked:
                f.write(f"{x['rank']},{x['code']},{x['name']},{x['score']},{'是' if x['eligible'] else '否'},"
                        f"\"{x['reason']}\",{'加入' if x['code'] in add else ''}\n")
            for c, why in remove.items():
                f.write(f",{c},{name(c)},,,\"{why}\",清除\n")
        self.log(f"今日选股（{len(ranked)} 只参与）：新增 {'、'.join(name(c) for c in add) or '无'}；"
                 f"清除 {'、'.join(f'{name(c)}（{w}）' for c, w in remove.items()) or '无'}")

    # ---------- 回测 + 复盘 + 规则进化（每天一次） ----------
    def start_backtest(self, force=False):
        today = last_session()
        if self.backtesting or not (force or self.cfg["bt"].get("date") != today):
            return
        self.backtesting = True
        threading.Thread(target=self._backtest, args=(today,), daemon=True).start()

    def _backtest(self, today):
        try:
            codes = list(dict.fromkeys([p["code"] for p in self.positions] +
                                       [c for c in self.cfg["stocks"] if c not in INDICES]))
            q = fetch(codes)
            data = {}
            for c in codes:
                if c in q:
                    try:
                        data[c] = screener.kline(q[c]["symbol"], 500)
                    except Exception:
                        pass
                    time.sleep(0.2)
            res = backtest.compare(data)
            with self.lock:
                self.cfg["bt"] = {"date": today, "k": backtest.best_trail_k(res), "stocks": len(data), "results": res}
                save(self.cfg)
            try:
                self._learn(data, q)
            except Exception as e:
                self.log(f"交易复盘失败：{str(e)[:80]}")
            self.log(f"离场规则回测 + 交易复盘完成（{len(data)} 只股票、近 2 年）")
        except Exception as e:
            self.log(f"回测失败：{e}")
        finally:
            self.backtesting = False

    def _learn(self, data, q):
        if not self.link.trd:
            return
        deals = journal.fetch_deals(self.link)
        closed, open_lots = journal.build_trades(deals)
        rule_orders = {str(b["stop_id"]): "规则止损" for b in self.cfg["brackets"]}
        rule_orders.update({str(b["target_id"]): "规则止盈" for b in self.cfg["brackets"] if b.get("target_id")})
        journal.enrich(closed, {c: d.get("symbol") for c, d in q.items()}, rule_orders)
        st = journal.stats(closed)
        entries = [(data[t["code"]], t["entry_time"][:10], t["entry"]) for t in closed if t["code"] in data]
        entries += [(data[c], lot["time"][:10], lot["price"]) for c, lots in open_lots.items() if c in data
                    for lot in lots]
        cur = {k: self.cfg["rule_params"][k] for k in tuner.KEYS}
        month = last_session()[:7]
        # 交易规则每月评估一次（每月第一个交易日收盘后），盘中不换规则；复盘统计照常每天更新
        tune = self.cfg.get("rule_month") != month and not market_open(10)
        base, best, cands, w = tuner.suggest(cur, data, entries, len(closed)) if tune else (None, None, [], None)
        brief = lambda ev: ev and {"params": ev["params"], "avg": ev["pooled"]["avg"], "worst": ev["pooled"]["worst"],
                                   "mdd": ev["pooled"]["mdd"], "trades": ev["pooled"]["trades"],
                                   "user_avg": ev["user_avg"], "score": ev["score"]}
        with self.lock:
            old = self.cfg["journal"]
            self.cfg["journal"] = {"date": datetime.now().strftime("%Y-%m-%d"), "trades": closed, "stats": st,
                                   "insights": journal.insights(closed, st), "open": open_lots, "deals": len(deals),
                                   "base": brief(base) if tune else old.get("base"),
                                   "weight": w if tune else old.get("weight"), "entries": len(entries),
                                   "tune_result": old.get("tune_result")}
            if not tune:
                save(self.cfg)
                return
            self.cfg["rule_month"] = month
            b = brief(best)
            if b and b["params"] != cur:
                p = b["params"]
                self.cfg["journal"]["tune_result"] = (f"{month}：已自动换成 限亏 {p['loss_cap']:g} / 跟踪 {p['trail']:g} / "
                                                      f"保本 {p['breakeven']:g} / 止盈 {p['target']:g} ATR（平均每笔 {b['avg']:.2f}%，"
                                                      f"最差一笔 {b['worst']:.1f}%，最大回撤 {b['mdd']:.1f}%）")
                self._adopt(b, f"{month} 月度规则评估自动采用")
                self.log(f"月度规则更新：换成 限亏 {p['loss_cap']:g} / 跟踪 {p['trail']:g} / 保本 {p['breakeven']:g} / "
                         f"止盈 {p['target']:g} ATR"
                         f"（v{self.cfg['rule_params']['version']}），不满意可在「复盘」页退回上一版", alert=True)
            else:
                self.cfg["suggestion"] = None
                self.cfg["journal"]["tune_result"] = f"{month}：没有在所有护栏下都更好的参数，规则保持不变"
                save(self.cfg)
                self.log(f"{month} 月度规则评估：没有通过全部护栏的更好参数，规则保持 v{self.cfg['rule_params']['version']} 不变")

    def adopt_suggestion(self):
        with self.lock:
            sug = self.cfg["suggestion"]
            if not sug:
                raise ValueError("没有待采用的建议")
            self._adopt(sug, "手动采用")

    def _adopt(self, sug, how):
        with self.lock:
            old = dict(self.cfg["rule_params"])
            self.cfg["rule_history"].append({**old, "until": datetime.now().strftime("%Y-%m-%d %H:%M")})
            self.cfg["rule_params"] = {"target": old.get("target", 6.0), **sug["params"], "version": old["version"] + 1,
                                       "since": datetime.now().strftime("%Y-%m-%d %H:%M")}
            self.cfg["suggestion"] = None
            save(self.cfg)
        self.log(f"规则升级到 v{self.cfg['rule_params']['version']}（{how}）：{sug['params']}（旧：{old}）", order=True)

    def rollback_rule(self):
        with self.lock:
            if not self.cfg["rule_history"]:
                raise ValueError("没有可以退回的版本")
            prev = self.cfg["rule_history"].pop()
            self.cfg["rule_params"] = {"target": 6.0, **{k: prev[k] for k in prev if k != "until"}}
            save(self.cfg)
        self.log(f"规则退回 v{prev['version']}", order=True)

    # ---------- 一键挂保护单 ----------
    def protect_plan(self):
        items = rules.plan(self.positions, self.reports, self.active_rule_codes(), self.t1_hold())
        out = []
        for it in items:
            out.append({"code": it["code"], "name": it["name"], "qty": it["qty"], "action": it["action"],
                        "why": it.get("error") or it["why"], "error": bool(it.get("error")),
                        "orders": [orders.describe(s, "REAL") for s in it["specs"]],
                        "optional_exit": orders.describe(it["optional_exit"], "REAL") if it["optional_exit"] else None})
        return {"rules": rules.rules_text(self.cfg["rule_params"]), "version": self.cfg["rule_params"]["version"],
                "items": out}

    def _trading(self):
        if not self.trade_lock.acquire(timeout=0.5):
            raise ValueError("上一笔下单还在处理，请等它完成后再试")

    def protect_execute(self, picks):
        """picks：[{code, early}]。重新按规则生成计划再执行，不信任网页传来的价格。"""
        self._trading()
        try:
            return self._protect_execute(picks)
        finally:
            self.trade_lock.release()

    def _protect_execute(self, picks):
        items = {it["code"]: it for it in rules.plan(self.positions, self.reports, self.active_rule_codes(),
                                                     self.t1_hold())}
        results, seen = [], set()
        for pk in picks:
            if pk.get("code") in seen:  # 同一只只处理一次
                continue
            seen.add(pk.get("code"))
            it = items.get(pk.get("code"))
            if not it or it["action"] == "skip" or it.get("error"):
                results.append(f"✗ {pk.get('code')}：按规则不能执行（可能已有保护或数据不足）")
                continue
            early = bool(pk.get("early")) and it["optional_exit"]
            try:
                if self.desk.held("REAL", it["code"]) < it["qty"]:
                    results.append(f"✗ {it['name']}：可卖数量不足，跳过")
                    continue
                if early or it["action"] == "exit":
                    s = it["optional_exit"] if early else it["specs"][0]
                    oid = self.desk.place(s, "REAL")
                    results.append(f"✓ {it['name']} 规则离场单已提交，订单号 {oid}")
                    self.log(f"一键：规则离场 {orders.describe(s, 'REAL')} 订单号 {oid}", order=True)
                else:
                    with self.lock:
                        _, notes = self.bm.create(it["specs"][0], it["specs"][1] if len(it["specs"]) > 1 else None,
                                                  "REAL", server_target=not self._cash())
                        save(self.cfg)
                    results.append(f"✓ {it['name']}：" + "；".join(notes))
            except Exception as e:
                results.append(f"✗ {it['name']}：{self._explain(e)}")
                self.log(f"一键：{it['name']} 失败：{self._explain(e)}", order=True)
        self.refresh_orders()
        return results

    @staticmethod
    def _explain(e):
        msg = str(e)
        if "unlock" in msg.lower() or "解锁" in msg:
            msg += "（请先在 OpenD 窗口里点「解锁交易」）"
        return msg

    # ---------- 买入 ----------
    def buy_plan(self, code):
        code = normalize(code or "")
        if not code or code in INDICES:
            raise ValueError("请输入美股个股代码")
        q = self.quotes.get(code) or fetch([code]).get(code)
        if not q:
            raise ValueError("拿不到这只股票的行情")
        rows = fetch_daily(q["symbol"], 130)
        held = sum(p["qty"] for p in self.positions if p["code"] == code and p["side"] == "long")
        pending = {b["code"] for b in self.cfg["pending_buys"] if b["status"] == "pending"}
        plan = rules.buy_plan(code, q, rows, self.acct, held, self.cfg["buy"], self.cfg["rule_params"], pending)
        plan["rules"] = rules.buy_rules_text(self.cfg["buy"], self.cfg["rule_params"])
        plan["screen"] = self.cfg["screen"].get(code)
        if plan["ok"]:
            spec, _ = orders.build_buy(code, plan["limit"], plan["qty"])
            plan["order"] = orders.describe(spec, "REAL")
        return plan

    def buy_execute(self, code, qty):
        """重新按规则算一遍再下单；数量只能等于或少于规则算出的股数。"""
        self._trading()
        try:
            return self._buy_execute(code, qty)
        finally:
            self.trade_lock.release()

    def _buy_execute(self, code, qty):
        if not self.link.trd:
            raise ValueError("没有连接 moomoo")
        plan = self.buy_plan(code)
        if not plan["ok"]:
            raise ValueError("按规则不能买：" + "；".join(plan["reasons"]))
        qty = int(qty)
        if not 1 <= qty <= plan["qty"]:
            raise ValueError(f"股数必须在 1 ~ {plan['qty']} 之间（规则上限）")
        spec, err = orders.build_buy(plan["code"], plan["limit"], qty)
        if err:
            raise ValueError(err)
        try:
            oid = self.desk.place(spec, "REAL")
        except Exception as e:
            self.log(f"买入失败 {orders.describe(spec, 'REAL')}：{self._explain(e)}", order=True)
            raise ValueError(self._explain(e))
        with self.lock:
            self.cfg["pending_buys"].append({"order_id": oid, "code": plan["code"], "name": plan["name"], "qty": qty,
                                             "limit": plan["limit"], "status": "pending",
                                             "created": datetime.now().strftime("%Y-%m-%d %H:%M")})
            save(self.cfg)
        self.log(f"买入单已提交 {orders.describe(spec, 'REAL')} 订单号 {oid}", order=True)
        self.refresh_orders()
        return f"✓ 买入单已提交（订单号 {oid}）：{plan['name']} {qty} 股，限价 {plan['limit']}。成交后自动挂动态止损。"

    def check_pending_buys(self):
        """买单成交（含部分成交后结束）→ 按规则给成交的股数挂动态止损，交给自动规则管理。"""
        if not self.link.trd:
            return
        changed = False
        for b in [x for x in self.cfg["pending_buys"] if x["status"] == "pending"]:
            try:
                info = self.desk.order_info(b["order_id"], "REAL")
            except Exception:
                continue
            st, filled = info["status"], info["dealt_qty"] or 0
            done = st in brackets.DONE or (st in brackets.DEAD and filled > 0)
            if st in brackets.DEAD and filled <= 0:
                b["status"] = "cancelled"
                changed = True
                self.log(f"买单 {b['order_id']} {b['name']} 没有成交（{st}），已结束", order=True)
                continue
            if not done:
                continue
            try:
                self._protect_new_buy(b, filled, info["dealt_avg"] or b["limit"])
                b["status"] = "filled"
            except Exception as e:
                b["status"] = "filled_unprotected"
                self.log(f"⚠ {b['name']} 已买入 {filled:g} 股，但自动挂止损失败：{self._explain(e)}，"
                         "请到「持仓 · 风险」点「一键挂保护单」", order=True, alert=True)
            changed = True
        if changed:
            self.save()

    def _protect_new_buy(self, b, filled, avg):
        with self.trade_lock:  # 和网页上的「一键挂保护单」错开，同一只不会挂两张止损
            if b["code"] in self.active_rule_codes() and self.desk.held("REAL", b["code"]) < filled:
                # 可卖数量已被别的止损单占住：刚才「一键挂保护单」已经把这些股数一起保护了
                self.log(f"{b['name']} 买入成交 {filled:g} 股，已由现有止损规则覆盖，不再重复挂单", order=True)
                return
            self._protect_new_buy_locked(b, filled, avg)

    def _protect_new_buy_locked(self, b, filled, avg):
        q = self.quotes.get(b["code"]) or fetch([b["code"]]).get(b["code"]) or {}
        rows = fetch_daily(q["symbol"], 130)
        pos = {"code": b["code"], "name": b["name"], "qty": filled, "cost": avg, "price": q.get("price", avg),
               "side": "long", "market_val": filled * avg}
        r = risk.assess(pos, q.get("price", avg), (self.acct or {}).get("total_assets"), rows, self.cfg["mode"],
                        params=self.cfg["rule_params"], entry_date=us_date())
        spec, err = orders.build(pos, q.get("price", avg), "stop", round(r["trail_stop"], 2), filled)
        if err:
            raise RuntimeError(err)
        tgt = None
        if r.get("take_profit"):
            tgt, _ = orders.build(pos, q.get("price", avg), "target", round(r["take_profit"], 2), filled)
        with self.lock:
            _, notes = self.bm.create(spec, tgt, "REAL", server_target=not self._cash())
            save(self.cfg)
        self.log(f"{b['name']} 买入成交 {filled:g} 股 @ {avg:.2f}，已自动挂动态止损 {r['trail_stop']:.2f}"
                 + (f"、止盈 {r['take_profit']:.2f}" if r.get("take_profit") else ""), alert=True)

    # ---------- 掉线提醒 + 开盘前自检 ----------
    def watch_connection(self, now):
        ok = self.acc_status == "已连接"
        if ok:
            if self._down_alerted:
                self.log("moomoo 连接已恢复", alert=True)
            self._down_since, self._down_alerted = None, False
            return
        if self._down_since is None:
            self._down_since = now
        elif now - self._down_since > DOWN_ALERT_SECS and not self._down_alerted:
            self._down_alerted = True
            self.log(f"⚠ moomoo 连接断开超过 2 分钟：{self.acc_status}。止损单在服务器上照常有效，"
                     "但自动上调止损、止盈盯价、自动保护都停了", alert=True)

    def maybe_self_check(self):
        t = us_now()
        if t.weekday() >= 5 or not CHECK_FROM <= (t.hour, t.minute) < CHECK_TO or self.cfg.get("check_date") == us_date():
            return
        self.self_check()

    def self_check(self):
        """开盘前检查：连接、解锁、每只持仓的止损单是否真的在服务器上、选股回测是否跑了。结果推送到手机。"""
        problems, oks = [], []
        if not (self.link.trd and self.acc_status == "已连接"):
            problems.append(f"moomoo 没连上（{self.acc_status}）")
        else:
            oks.append("moomoo 已连接")
        if time.time() - self._unlock_err < 86400:
            problems.append("最近 24 小时有操作因为没解锁交易失败：请在 OpenD 窗口里点「解锁交易」")
        live = {o["order_id"] for o in self.order_rows if o["env"] == "REAL"}
        covered = {}
        for b in self.cfg["brackets"]:
            if b["status"] != "active" or b["env"] != "REAL":
                continue
            c = "us" + b["code"].split(".", 1)[1]
            covered[c] = covered.get(c, 0) + b["qty"]
            if self.link.trd and b["stop_id"] not in live:
                problems.append(f"{b['code']} 的止损单 {b['stop_id']} 在 moomoo 上找不到了（可能被撤或过期）")
        for p in self.positions:
            gap = p["qty"] - covered.get(p["code"], 0)
            if gap > 0:
                problems.append(f"{p['name']} 有 {gap:g} 股没有止损保护")
        if self.positions and not any("没有止损" in x or "找不到" in x for x in problems):
            oks.append(f"{len(self.positions)} 只持仓都有止损单")
        session = last_session()
        if self.cfg["auto_screen"] and self.cfg["screen_date"] != session:
            problems.append(f"{session} 收盘后的每日选股没跑成功")
        if self.cfg["bt"].get("date") != session:
            problems.append(f"{session} 收盘后的回测 / 复盘没跑成功")
        if str(self.updated).startswith("获取失败"):
            problems.append(f"行情获取失败：{self.updated}")
        with self.lock:
            self.cfg["check_date"] = us_date()
            save(self.cfg)
        self.check_result = {"time": datetime.now().strftime("%m-%d %H:%M"), "problems": problems, "oks": oks}
        if problems:
            msg = f"开盘前自检：发现 {len(problems)} 个问题\n" + "\n".join("· " + x for x in problems)
            self.log(msg.replace("\n", " "), order=True, alert=True)
        else:
            msg = "开盘前自检 ✓ 一切正常：" + "；".join(oks)
            self.log(msg)
            self.push.send("美股盯盘", msg, priority=2)
        return self.check_result

    def push_test(self):
        self.push.send("美股盯盘", f"测试推送 ✓ 能收到这条，就说明手机推送设置好了（{datetime.now():%H:%M:%S}）",
                       priority=3, force=True)

    # ---------- 自动保护：在 moomoo App 里买的股票也自动挂止损 + 止盈 ----------
    def _once(self, code, kind, msg, alert=True):
        key, today = (code, "ap:" + kind), us_date()
        if self.fired.get(key) != today:
            self.fired[key] = today
            self.log(msg, order=True, alert=alert)

    def auto_protect(self):
        """真实账户里没有被自动规则覆盖的股数（比如你在 moomoo App 里买的、或者加仓的部分），
        按「一键挂保护单」同一套规则自动挂止损 + 止盈。只会卖出已有的股票，不会买入或加仓。
        已经跌破止损 / 到了止盈的不会自动卖，只提醒你。"""
        if not self.cfg.get("auto_protect", True) or not self.link.trd:
            return
        now = time.time()
        pending = {b["code"] for b in self.cfg["pending_buys"] if b["status"] == "pending"}
        covered = {}
        for b in self.cfg["brackets"]:
            if b["status"] == "active" and b["env"] == "REAL":
                c = "us" + b["code"].split(".", 1)[1]
                covered[c] = covered.get(c, 0) + b["qty"]
        need_by = {p["code"]: p["qty"] - covered.get(p["code"], 0) for p in self.positions}
        created = False
        for c in [c for c in self._unprot if need_by.get(c, 0) <= 0]:
            self._unprot.pop(c, None)
        for p in self.positions:
            c, need = p["code"], need_by[p["code"]]
            if need <= 0 or c in pending or now < self._ap_retry.get(c, 0):
                continue
            if now - self._unprot.setdefault(c, now) < AUTO_PROTECT_WAIT or c not in self.reports:
                continue
            pc = {**p, "qty": int(need) if float(need).is_integer() else need}
            it = rules.plan([pc], self.reports, set(), self.t1_hold())[0]
            if it["action"] == "exit":
                self._once(c, "exit", f"⚠ {p['name']} 有 {need:g} 股没有保护，而且{it['why']}：没有自动卖出，"
                                      "请到「持仓 · 风险」点「一键挂保护单」确认离场，或在 moomoo 里自己处理")
                self._ap_retry[c] = now + AUTO_PROTECT_RETRY
                continue
            if it["action"] != "protect" or it.get("error") or not it["specs"]:
                self._ap_retry[c] = now + AUTO_PROTECT_RETRY
                continue
            if not self.trade_lock.acquire(blocking=False):  # 网页上正在下单：下一轮再来
                continue
            try:
                if self.desk.held("REAL", c) < need:
                    self._once(c, "frozen", f"{p['name']} 有 {need:g} 股没有程序的保护，但可卖数量不够"
                                            "（可能你在 moomoo 里已经挂了卖单），没有自动挂", alert=False)
                    self._ap_retry[c] = now + AUTO_PROTECT_RETRY
                    continue
                with self.lock:
                    specs = it["specs"]
                    _, notes = self.bm.create(specs[0], specs[1] if len(specs) > 1 else None, "REAL",
                                              server_target=not self._cash())
                    save(self.cfg)
                self._unprot.pop(c, None)
                created = True
                self.log(f"自动保护：{p['name']} {need:g} 股不在规则里，已按规则挂上保护（{it['why']}）", order=True,
                         alert=True)
            except Exception as e:
                self._ap_retry[c] = now + AUTO_PROTECT_RETRY
                self._once(c, "fail", f"⚠ 自动保护 {p['name']} 失败：{self._explain(e)}（10 分钟后再试）")
            finally:
                self.trade_lock.release()
        if created:
            self.refresh_orders()

    # ---------- 挂单 / 规则 ----------
    def cancel_order(self, env, oid):
        self.desk.cancel(oid, env)
        self.log(f"撤单 {ENV_NAME[env]} {oid}", order=True)
        self.refresh_orders()

    def cancel_all(self):
        fails = []
        for o in list(self.order_rows):
            try:
                self.desk.cancel(o["order_id"], o["env"])
                self.log(f"撤单 {ENV_NAME[o['env']]} {o['order_id']}（全部撤销）", order=True)
            except Exception as e:
                fails.append(f"{o['order_id']}：{e}")
        with self.lock:
            for b in self.cfg["brackets"]:
                if b["status"] == "active":
                    b["status"] = "cancelled"
            save(self.cfg)
        self.refresh_orders()
        return fails

    def stop_rule(self, rid):
        b = next((x for x in self.cfg["brackets"] if x["id"] == rid and x["status"] == "active"), None)
        if not b:
            raise ValueError("找不到运行中的这条规则")
        with self.lock:
            self.bm.cancel_rule(b)
            save(self.cfg)
        self.refresh_orders()

    # ---------- 收盘存档 ----------
    def save_close(self):
        stamps = [d.get("time", "") for c, d in self.quotes.items() if c not in INDICES]
        if not stamps or any(len(t) < 16 or t[11:16] < "16:00" for t in stamps):
            return
        path = os.path.join(DATA, f"{max(stamps)[:10]}.csv")
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("代码,名称,收盘,涨跌幅%,开盘,最高,最低,换手%\n")
            for code, d in self.quotes.items():
                f.write(f"{code},{d.get('name')},{d.get('price')},{d.get('pct')},{d.get('open')},{d.get('high')},"
                        f"{d.get('low')},{d.get('turnover')}\n")

    # ---------- 给网页的状态 ----------
    def state(self, since_event=0):
        cfg, a = self.cfg, self.acct
        pos_codes = {p["code"] for p in self.positions}
        watch = []
        for code in cfg["stocks"]:
            d = self.quotes.get(code, {})
            src = ("指数" if code in INDICES else "持仓" if code in pos_codes else "锁定" if code in cfg["locked"]
                   else "自动" if code in cfg["auto"] else "手动")
            s = cfg["screen"].get(code, {})
            watch.append({"code": code, "name": d.get("name", code), "src": src, "score": s.get("score"),
                          "reason": s.get("reason"), "rank": s.get("rank"), "price": d.get("price"), "pct": d.get("pct"),
                          "chg": d.get("chg"), "open": d.get("open"), "high": d.get("high"), "low": d.get("low"),
                          "time": d.get("time"), "alert": cfg["alerts"].get(code)})
        prot = self.active_rule_codes()
        pos = []
        for c, r in self.reports.items():
            p = r["pos"]
            pos.append({"code": c, "name": r["name"], "qty": p["qty"], "side": p["side"], "cost": p["cost"],
                        "price": r["price"], "pl_pct": r["pl_pct"], "weight": r["weight"], "atr_pct": r["atr_pct"],
                        "risk_score": r["risk_score"], "trail_stop": r["trail_stop"], "stage": r.get("stop_stage"),
                        "take_profit": r.get("take_profit"),
                        "protected": c in prot, "signals": r["exit_signals"], "reasons": r["reasons"],
                        "level": risk.LEVELS[r["level"]] if r["level"] is not None else None,
                        "max_loss": r.get("max_loss"), "stop_loss_usd": r.get("stop_loss_usd"),
                        "scenarios": r.get("scenarios", [])})
        account = None
        if a:
            acc = risk.assess_account(a, self.reports)
            mv = sum(r["value"] for r in self.reports.values())
            pl = sum((r["price"] - r["pos"]["cost"]) * r["pos"]["qty"] * (1 if r["pos"]["side"] == "long" else -1)
                     for r in self.reports.values())
            account = {**a, "mv": mv, "pl": pl, "reasons": acc["reasons"],
                       "level": risk.LEVELS[acc["level"]] if acc["level"] is not None else None}
        rules_list = []
        for b in sorted(cfg["brackets"], key=lambda b: (b["status"] != "active", b["created"]))[:30]:
            sd = b["stop_desc"]
            rules_list.append({"id": b["id"], "env": ENV_NAME[b["env"]], "code": b["code"], "qty": b["qty"],
                               "stop": f"跟踪 {sd['trail_value']}%" if sd["order_type"] == "TRAILING_STOP"
                               else f"触发 {sd['aux_price']}", "stop_id": b["stop_id"], "target": b["target"],
                               "ratchets": b.get("ratchets", 0), "status": STATUS_NAME[b["status"]],
                               "active": b["status"] == "active", "created": b["created"]})
        return {"updated": self.updated, "connected": self.acc_status == "已连接", "acc_status": self.acc_status,
                "watch": watch, "positions": pos, "account": account, "orders": self.order_rows,
                "order_note": self.order_note, "rules": rules_list, "logs": self.logs[:40],
                "events": [e for e in self.events if e["id"] > since_event],
                "pending_buys": [b for b in cfg["pending_buys"] if b["status"] == "pending"],
                "settings": {"mode": cfg["mode"], "modes": {k: v[0] for k, v in risk.MODES.items()},
                             "auto_screen": cfg["auto_screen"], "auto_protect": cfg.get("auto_protect", True), "buy": cfg["buy"],
                             "push": {**cfg["push"], **self.push.status}, "check": self.check_result,
                             "rule_params": cfg["rule_params"]},
                "screening": self.screening, "backtesting": self.backtesting,
                "screen_date": cfg["screen_date"], "bt_date": cfg["bt"].get("date"),
                "suggestion": cfg["suggestion"]}

    def journal_state(self):
        return {"journal": self.cfg["journal"], "rule_params": self.cfg["rule_params"],
                "rule_history": self.cfg["rule_history"][-5:], "suggestion": self.cfg["suggestion"],
                "bt": self.cfg["bt"], "rule_month": self.cfg.get("rule_month")}
