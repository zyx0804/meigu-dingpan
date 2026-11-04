"""通过本机 moomoo OpenD 获取美股账户资产、持仓和行情（只读）。

下单相关的代码单独放在 orders.py；这里没有解锁交易的代码。
"""
import socket

HOST, PORT = "127.0.0.1", 11111


def opend_running(host=HOST, port=PORT):
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def to_moomoo(code):
    """usAAPL -> US.AAPL"""
    return "US." + code[2:]


def to_local(code):
    """US.AAPL -> usAAPL"""
    return "us" + code.split(".", 1)[1]


class MoomooLink:
    def __init__(self, host=HOST, port=PORT):
        self.host, self.port = host, port
        self.trd = None
        self.quote = None
        self.acc_id = None
        self.acc_ids = {}  # "REAL" / "SIMULATE" -> 账户 ID
        self.acc_type = None  # 真实账户类型：CASH 现金账户 / MARGIN 融资账户

    def connect(self):
        from moomoo import OpenQuoteContext, OpenSecTradeContext, RET_OK, SecurityFirm, TrdEnv, TrdMarket
        self.close()
        self.trd = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=self.host, port=self.port,
                                       security_firm=SecurityFirm.NONE)
        ret, accs = self.trd.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"读取账户列表失败：{accs}")
        real = accs[accs["trd_env"] == TrdEnv.REAL]
        if real.empty:
            raise RuntimeError("没有找到美股真实账户（OpenD 是否已登录？）")
        self.acc_id = int(real.iloc[0]["acc_id"])
        self.acc_type = str(real.iloc[0].get("acc_type", ""))
        sim = accs[accs["trd_env"] == TrdEnv.SIMULATE]
        self.acc_ids = {"REAL": self.acc_id}
        if not sim.empty:
            self.acc_ids["SIMULATE"] = int(sim.iloc[0]["acc_id"])
        self.quote = OpenQuoteContext(host=self.host, port=self.port)

    def close(self):
        for ctx in (self.trd, self.quote):
            if ctx:
                try:
                    ctx.close()
                except Exception:
                    pass
        self.trd = self.quote = None

    def account(self):
        from moomoo import Currency, RET_OK, TrdEnv
        ret, df = self.trd.accinfo_query(trd_env=TrdEnv.REAL, acc_id=self.acc_id, currency=Currency.USD)
        if ret != RET_OK:
            raise RuntimeError(f"读取资产失败：{df}")
        r = df.iloc[0]

        def num(k):
            try:
                return float(r[k])
            except (KeyError, TypeError, ValueError):
                return None
        return {"total_assets": num("total_assets"), "market_val": num("market_val"),
                "cash": num("us_cash") if num("us_cash") is not None else num("cash"),
                "power": num("power")}

    def positions(self):
        from moomoo import RET_OK, TrdEnv
        ret, df = self.trd.position_list_query(trd_env=TrdEnv.REAL, acc_id=self.acc_id)
        if ret != RET_OK:
            raise RuntimeError(f"读取持仓失败：{df}")
        out = []
        for _, r in df.iterrows():
            qty = float(r["qty"])
            if qty == 0 or not str(r["code"]).startswith("US."):
                continue
            cost = float(r["cost_price"]) if r.get("cost_price_valid", True) else float(r["average_cost"])
            short = str(r.get("position_side", "")).upper() == "SHORT" or qty < 0
            out.append({"code": to_local(r["code"]), "name": r["stock_name"], "qty": abs(qty),
                        "cost": cost, "price": float(r["nominal_price"]),
                        "market_val": float(r["market_val"]), "side": "short" if short else "long",
                        "today_buy": float(r.get("today_buy_qty", 0) or 0)})
        return out

    def snapshot(self, codes):
        """moomoo 实时行情；没有行情权限时返回空，由调用方退回腾讯接口。"""
        from moomoo import RET_OK
        ret, df = self.quote.get_market_snapshot([to_moomoo(c) for c in codes])
        if ret != RET_OK:
            return {}
        out = {}
        for _, r in df.iterrows():
            prev = float(r["prev_close_price"]) or 1
            last = float(r["last_price"])
            if last <= 0:  # 指数等 moomoo 没有行情的代码会返回 0，留给腾讯数据
                continue
            out[to_local(r["code"])] = {
                "name": r["name"], "price": last, "prev": prev, "open": float(r["open_price"]),
                "chg": round(last - prev, 4), "pct": round((last - prev) / prev * 100, 2),
                "high": float(r["high_price"]), "low": float(r["low_price"]),
                "turnover": r.get("turnover_rate", ""), "time": str(r["update_time"])}
        return out
