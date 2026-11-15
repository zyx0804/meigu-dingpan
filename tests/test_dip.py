"""离线测试：不连 moomoo、不联网、不下真单。运行：python -m unittest discover -s tests"""
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import brackets  # noqa: E402
import common  # noqa: E402
import engine  # noqa: E402
import risk  # noqa: E402
import rules  # noqa: E402
import screener  # noqa: E402

engine.save = lambda cfg: None  # 测试里不写 stocks.json


def make_rows(n=260, pull=0.0):
    """稳步上涨的日 K（日期递增），最后 pull 元一口气回落。每根 (日期, 开, 收, 高, 低, 量)。"""
    rows, px, d0 = [], 100.0, datetime(2025, 1, 1)
    for i in range(n):
        o, c = px, px + 0.3
        rows.append(((d0 + timedelta(days=i)).strftime("%Y-%m-%d"), o, c, c + 1, o - 1, 1e6))
        px = c
    if pull:
        last = list(rows[-1])
        last[2] = last[1] - pull
        last[4] = min(last[4], last[2] - 0.5)
        rows[-1] = tuple(last)
    return rows


class Desk:
    """假 moomoo：记下所有下单、改单、撤单。"""

    def __init__(self):
        self.st, self.placed, self.modified, self.cancelled = {}, [], [], []

    def order_status(self, oid, env):
        return self.st.get(oid, "SUBMITTED")

    def place(self, spec, env):
        self.placed.append(spec)
        oid = f"O{len(self.placed)}"
        self.st[oid] = "SUBMITTED"
        return oid

    def cancel(self, oid, env):
        self.cancelled.append(oid)
        self.st[oid] = "CANCELLED_ALL"

    def modify_stop(self, oid, env, qty, trigger, price):
        self.modified.append((oid, qty, trigger))

    def held(self, env, code):
        return 99


STOP = {"order_type": "STOP", "aux_price": 90.0, "price": 90.0, "code": "US.TEST", "qty": 4, "trd_side": "SELL",
        "kind": "stop", "time_in_force": "GTC", "session": "RTH"}


class SplitTest(unittest.TestCase):
    def test_ten_for_one(self):
        rows = [("d1", 1000, 1000, 1010, 990, 1), ("d2", 100, 101, 102, 99, 10)]
        adj = screener.adjust_splits(rows)
        self.assertAlmostEqual(adj[0][2], 100.0)
        self.assertAlmostEqual(adj[0][5], 10)
        self.assertEqual(adj[1], rows[1])

    def test_real_crash_not_touched(self):
        rows = [("d1", 100, 100, 101, 99, 1), ("d2", 70, 70, 71, 69, 1)]  # 跌 30%，不是拆股
        self.assertEqual(screener.adjust_splits(rows), rows)


class DipPlanTest(unittest.TestCase):
    cfg = {"risk_pct": 2, "max_pos_pct": 50, "fractional": True}
    acct = {"total_assets": 1000.0, "power": 1000.0}

    def plan(self, rows, acct=None):
        price = rows[-1][2]
        rows = [r[:5] for r in rows]  # 程序里 fetch_daily 给的是 5 列
        return rules.buy_plan("usTEST", {"price": price, "name": "T"}, rows, acct or self.acct, 0, self.cfg,
                              style="dip")

    def test_pullback_buys_half(self):
        p = self.plan(make_rows(pull=8))
        self.assertTrue(p["ok"], p["reasons"])
        a = p["atr"]
        self.assertAlmostEqual(p["stop"], round(p["limit"] - 3 * a, 2), places=2)
        self.assertAlmostEqual(p["add_px"], round(p["limit"] - 1.5 * a, 2), places=2)
        full = min(1000 * 0.02 / p["per_share"], 1000 * 0.5 / p["limit"])
        self.assertLessEqual(p["qty"], full / 2 + 1e-4)  # 第一批只买一半

    def test_no_pullback_no_buy(self):
        p = self.plan(make_rows())
        self.assertFalse(p["ok"])

    def test_first_batch_limited_by_cash(self):
        p = self.plan(make_rows(pull=8), {"total_assets": 1000.0, "power": 20.0})
        self.assertLessEqual(p["qty"] * p["limit"], 20.0 * 0.99 + 0.01)


class DipExitTest(unittest.TestCase):
    def make(self, local=False):
        d = Desk()
        bm = brackets.BracketManager(d, [], lambda m, alert=False: None)
        extra = {"style": "dip", "entry": "2025-01-01", "stop0": 90.0, "atr0": 2.0, "cost": 100.0, "half_sold": False}
        b, _ = (bm.create_local if local else bm.create)(dict(STOP), None, "REAL", extra=extra)
        return d, bm, b

    def tick(self, bm, px, act=None):
        bm.tick({"usTEST": px}, lambda e, c: 4, None, None, rth=True, hold=(), actions={bm.store[0]["id"]: act} if act else None)

    def test_sell_half_then_filled(self):
        d, bm, b = self.make()
        self.tick(bm, 105, "half")
        self.assertEqual(d.modified[-1][1], 2)  # 止损先改成剩下的 2 股
        self.assertEqual(d.placed[-1]["qty"], 2)
        d.st[b["exit_id"]] = "FILLED_ALL"
        self.tick(bm, 105)
        self.assertTrue(b["half_sold"])
        self.assertEqual(b["qty"], 2)

    def test_sell_half_unfilled_restores_stop(self):
        d, bm, b = self.make()
        self.tick(bm, 105, "half")
        d.st[b["exit_id"]] = "CANCELLED_ALL"
        self.tick(bm, 105)
        self.assertEqual(d.modified[-1][1], 4)
        self.assertFalse(b["half_sold"])
        self.assertIsNone(b.get("phase"))

    def test_sell_all_cancels_stop_first(self):
        d, bm, b = self.make()
        stop_id = b["stop_id"]
        self.tick(bm, 105, "all")
        self.assertIn(stop_id, d.cancelled)
        self.assertEqual(d.placed[-1]["qty"], 4)
        d.st[b["exit_id"]] = "FILLED_ALL"
        self.tick(bm, 105)
        self.assertEqual(b["status"], "done")

    def test_fractional_rule_sells_half_without_modify(self):
        d, bm, b = self.make(local=True)
        self.tick(bm, 105, "half")
        self.assertFalse(d.modified)
        self.assertEqual(d.placed[-1]["qty"], 2)

    def test_t1_blocks_dip_sell(self):
        d, bm, b = self.make()
        n = len(d.placed)
        bm.tick({"usTEST": 105}, lambda e, c: 4, None, None, rth=True, hold={"usTEST"}, actions={b["id"]: "half"})
        self.assertEqual(len(d.placed), n)


class EngineDipTest(unittest.TestCase):
    def make(self, price, entry, half_sold=False, add=None):
        e = engine.Engine.__new__(engine.Engine)
        e.lock, e.trade_lock = threading.RLock(), threading.Lock()
        e.cfg = json.loads(json.dumps(common.DEFAULT))
        e.desk, e.logs = Desk(), []
        e.log = lambda m, **k: e.logs.append(m)
        e.link = type("L", (), {"trd": True, "acc_type": "CASH"})()
        e.acct = {"total_assets": 1000.0, "power": 1000.0}
        rows = make_rows(n=40)
        e.hist = {"usTEST": ("x", rows)}
        e.quotes = {"usTEST": {"price": price, "name": "T"}}
        e.cfg["brackets"] = [{"id": "R1", "env": "REAL", "code": "US.TEST", "qty": 4, "status": "active", "style": "dip",
                              "entry": entry, "stop0": 90.0, "atr0": 2.0, "cost": 100.0, "half_sold": half_sold,
                              "stop_desc": dict(STOP), "stop_id": "S1", **({"add": add} if add else {})}]
        return e, rows

    def at(self, hour, minute, date="2025-03-01"):
        t = datetime.strptime(f"{date} {hour}:{minute}", "%Y-%m-%d %H:%M")
        engine.us_now = lambda: t
        engine.us_date = lambda: date
        engine.market_open = lambda m=0: True

    def test_half_after_close_window_above_ma10(self):
        e, rows = self.make(price=200.0, entry="2025-02-05")
        self.at(15, 50)
        _, acts = e.dip_plan_exits()
        self.assertEqual(acts, {"R1": "half"})
        self.at(14, 0)
        self.assertEqual(e.dip_plan_exits()[1], {})  # 还没到收盘前，不卖

    def test_max_days_sells_all(self):
        e, rows = self.make(price=50.0, entry="2025-01-01")  # 早就超过 20 个交易日
        self.at(15, 50)
        self.assertEqual(e.dip_plan_exits()[1], {"R1": "all"})

    def test_after_half_stop_breakeven_and_trail(self):
        e, rows = self.make(price=200.0, entry="2025-02-05", half_sold=True)
        self.at(12, 0)
        dyn, _ = e.dip_plan_exits()
        self.assertAlmostEqual(dyn["R1"], max(100.0 * 1.001, 200.0 - 2.5 * 2.0))

    def test_addon_buys_once_when_price_drops(self):
        e, _ = self.make(price=96.0, entry="2025-02-28", add={"px": 97.0, "qty": 4, "done": False})
        self.at(11, 0)
        e.dip_addons()
        self.assertEqual(e.desk.placed[-1]["trd_side"], "BUY")
        self.assertEqual(e.desk.placed[-1]["qty"], 4)
        self.assertEqual(e.cfg["pending_buys"][-1]["addon_of"], "R1")
        n = len(e.desk.placed)
        e.dip_addons()
        self.assertEqual(len(e.desk.placed), n)  # 只补一次

    def test_addon_skipped_below_stop(self):
        e, _ = self.make(price=89.0, entry="2025-02-28", add={"px": 97.0, "qty": 4, "done": False})
        self.at(11, 0)
        e.dip_addons()
        self.assertFalse(e.desk.placed)

    def test_protect_puts_addon_on_one_rule_only(self):
        e, _ = self.make(price=100.0, entry="2025-02-28")
        e.cfg["brackets"] = []
        e.bm = brackets.BracketManager(e.desk, e.cfg["brackets"], lambda m, alert=False: None)
        extra = e._dip_extra("usTEST", 90.0, 2.0, 100.0, "2025-02-28", {"px": 97.0, "qty": 2.5, "done": False})
        e._protect({**STOP, "qty": 2.5}, None, extra)
        self.assertEqual([b["qty"] for b in e.cfg["brackets"]], [2, 0.5])
        self.assertEqual([("add" in b) for b in e.cfg["brackets"]], [True, False])


if __name__ == "__main__":
    unittest.main()
