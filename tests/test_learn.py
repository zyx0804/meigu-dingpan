"""自动学习（档位、影子交易、每月升级）的离线测试。"""
import json
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import common  # noqa: E402
import engine  # noqa: E402
import risk  # noqa: E402
import rules  # noqa: E402
import shadow  # noqa: E402
from test_dip import make_rows  # noqa: E402

engine.save = lambda cfg: None


def summ(trades, avg, worst, win=70):
    return {"trades": trades, "avg": avg, "worst": worst, "win": win, "half1": avg, "half2": avg, "median": avg,
            "pf": 1.3, "days": 10}


class PickUpgradeTest(unittest.TestCase):
    cur = shadow.PROFILES["balanced"]["params"]
    other = shadow.CANDIDATES[3][1]  # 深跌才买（适合保守 / 均衡）

    def results(self, other_shadow, other_bt=None):
        return [{"name": "当前", "params": dict(self.cur), "market": False, "profiles": ["balanced"],
                 "shadow": summ(40, 0.3, -10), "backtest": summ(900, 0.4, -20)},
                {"name": "候选", "params": dict(self.other), "market": False, "profiles": ["conservative", "balanced"],
                 "shadow": other_shadow, "backtest": other_bt or summ(500, 0.45, -20)}]

    def test_upgrades_when_better_everywhere(self):
        best = shadow.pick_upgrade(self.results(summ(35, 0.6, -9)), "balanced", dict(self.cur))
        self.assertEqual(best["name"], "候选")

    def test_needs_enough_shadow_trades(self):
        self.assertIsNone(shadow.pick_upgrade(self.results(summ(10, 2.0, -5)), "balanced", dict(self.cur)))

    def test_worse_worst_trade_blocks(self):
        self.assertIsNone(shadow.pick_upgrade(self.results(summ(35, 0.6, -15)), "balanced", dict(self.cur)))

    def test_worse_backtest_blocks(self):
        self.assertIsNone(shadow.pick_upgrade(self.results(summ(35, 0.6, -9), summ(500, 0.2, -20)), "balanced",
                                              dict(self.cur)))

    def test_wrong_profile_not_used(self):
        self.assertIsNone(shadow.pick_upgrade(self.results(summ(35, 0.6, -9)), "aggressive", dict(self.cur)))


class SimulateTest(unittest.TestCase):
    def test_since_only_counts_new_trades(self):
        rows = make_rows(n=400)
        # 中间放几次回调，制造交易
        rows = [list(r) for r in rows]
        for k in (250, 300, 350):
            rows[k][2] -= 10
            rows[k][4] -= 10
        rows = [tuple(r) for r in rows]
        p = shadow.PROFILES["balanced"]["params"]
        all_tr = shadow.simulate(rows, p)
        self.assertTrue(all_tr)
        since = all_tr[-1][0]
        self.assertEqual(shadow.simulate(rows, p, since=since), [t for t in all_tr if t[0] >= since])


class ProfileEngineTest(unittest.TestCase):
    def make(self, profile, healthy=True):
        e = engine.Engine.__new__(engine.Engine)
        e.lock, e.trade_lock = threading.RLock(), threading.Lock()
        e.cfg = json.loads(json.dumps(common.DEFAULT))
        e.cfg["profile"] = profile
        e.log = lambda *a, **k: None
        e._spx = ("x", [])
        e.market_healthy = lambda: healthy
        e.apply_profile()
        return e

    def tearDown(self):
        risk.DIP.update(shadow.PROFILES["balanced"]["params"])

    def test_profile_sets_params(self):
        self.make("aggressive")
        self.assertEqual(risk.DIP["pullback"], 2.0)
        self.make("conservative")
        self.assertEqual(risk.DIP["pullback"], 3.0)

    def test_upgrade_overrides_and_rollback(self):
        e = self.make("balanced")
        new = dict(shadow.CANDIDATES[3][1])
        e.cfg["dip_over"]["balanced"] = new
        e.cfg["dip_history"].append({"profile": "balanced", "old": dict(shadow.PROFILES["balanced"]["params"]),
                                     "new": new, "name": "x", "date": "d"})
        e.apply_profile()
        self.assertEqual(risk.DIP["pullback"], 3.5)
        e.dip_rollback("balanced")
        self.assertEqual(risk.DIP["pullback"], 2.5)
        self.assertNotIn("balanced", e.cfg["dip_over"])

    def test_conservative_blocks_when_market_bad(self):
        e = self.make("conservative", healthy=False)
        self.assertTrue(e.market_blocks())
        self.assertFalse(self.make("balanced", healthy=False).market_blocks())

    def test_no_scaling_buys_all_at_once(self):
        risk.DIP.update({**shadow.PROFILES["balanced"]["params"], "add": 0})
        rows = [r[:5] for r in make_rows(pull=8)]
        p = rules.buy_plan("usTEST", {"price": rows[-1][2], "name": "T"}, rows, {"total_assets": 1000.0, "power": 1000.0},
                           0, {"risk_pct": 2, "max_pos_pct": 50, "fractional": True}, style="dip")
        self.assertTrue(p["ok"], p["reasons"])
        self.assertIsNone(p["add_px"])
        full = min(1000 * 0.02 / p["per_share"], 1000 * 0.5 / p["limit"])
        self.assertAlmostEqual(p["qty"], full, delta=0.001)


if __name__ == "__main__":
    unittest.main()
