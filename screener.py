"""每日自动筛选自选股：从一个固定的美股大盘股池里，按公开、可量化的规则打分。

规则只看走势和成交数据（趋势、放量、流动性、波动），不判断公司"值不值得买"；
结果用来缩小关注范围，买不买由你自己判断。
"""
import json
import statistics
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# 候选池：流动性好的美股大盘 / 热门股
UNIVERSE = """AAPL MSFT NVDA AMZN GOOGL META TSLA AVGO AMD NFLX COST PEP ADBE CSCO INTC QCOM TXN AMAT MU LRCX
KLAC ADI MRVL PANW CRWD SNPS CDNS ORCL CRM NOW INTU UBER ABNB SHOP PYPL PLTR COIN MSTR SMCI ARM ASML TSM
BABA PDD JD JPM BAC WFC GS MS V MA AXP BRK.B UNH LLY JNJ PFE MRK ABBV TMO ISRG XOM CVX COP WMT HD MCD NKE
SBUX DIS KO PG BA CAT GE HON LMT RTX DE UPS F GM RIVN SNOW NET DDOG ZS MDB TEAM WDAY ROKU DKNG HOOD SOFI
RBLX U APP""".split()

AUTO_N = 8         # 自动加入自选的数量上限
KEEP_RANK = 15     # 已自动加入的股票，排名掉出前 15 才移除（避免每天来回进出）
MIN_DOLLAR_VOL = 3e8  # 20 日平均成交额至少 3 亿美元
ATR_BAND = (1.5, 6.0)  # 日均波动在这个区间加分：太小没机会，太大风险过高


# 腾讯日 K 有多个等价接口，某个限流或报错时依次换下一个
KLINE_URLS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=us{s},day,,,{n},qfq",
    "https://web.ifzq.gtimg.cn/appstock/app/usfqkline/get?param=us{s},day,,,{n},qfq",
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get?param=us{s},day,,,{n},qfq",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline?param=us{s},day,,,{n}",
]
_good = [0]  # 上次成功的接口下标，下次优先用


def adjust_splits(rows):
    """有的接口给的日 K 没复权：拆股那天看起来像暴跌（如一拆十跌 90%），均线、ATR、止损都会算错。
    开盘比前一天收盘低到一半以下、且接近整数倍时按拆股处理，把之前的价格和成交量换算到拆股后。"""
    rows = [list(r) for r in rows]
    for j in range(1, len(rows)):
        ratio = rows[j - 1][2] / rows[j][1] if rows[j][1] else 0
        k = round(ratio)
        if ratio > 1.8 and abs(ratio - k) / k < 0.12:
            for t in range(j):
                rows[t][1:5] = [x / k for x in rows[t][1:5]]
                if len(rows[t]) > 5:
                    rows[t][5] *= k
    return [tuple(r) for r in rows]


def kline(symbol, n=60):
    """日 K [(日期, 开, 收, 高, 低, 量)]。symbol 带交易所后缀，如 AAPL.OQ。"""
    err = None
    order = _good[:1] + [i for i in range(len(KLINE_URLS)) if i != _good[0]]
    for i in order:
        try:
            data = json.loads(urllib.request.urlopen(KLINE_URLS[i].format(s=symbol, n=n), timeout=8).read())["data"]
            v = next(iter(data.values()))
            rows = v.get("qfqday") or v.get("day") or []
            if rows:
                _good[0] = i
                return adjust_splits([(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
                                      for r in rows])
        except Exception as e:
            err = e
    raise RuntimeError(f"日 K 获取失败：{err}")


fetch_daily_vol = kline


def dip_metrics(rows):
    """回调抄底用的指标：200 日线、从 20 日高点回落了几个 ATR。数据不足返回 None。"""
    m = metrics(rows)
    if not m or len(rows) < 200:
        return None
    closes = [r[2] for r in rows]
    trs = [max(h - l, abs(h - rows[i - 1][2]), abs(l - rows[i - 1][2]))
           for i, (_, _, _, h, l, _) in enumerate(rows) if i > 0][-14:]
    a = sum(trs) / len(trs)
    ma200 = statistics.mean(closes[-200:])
    high20 = max(r[3] for r in rows[-20:])
    return {**m, "ma200": ma200, "above200": m["close"] > ma200, "trend200": m["close"] / ma200 - 1,
            "pullback_atr": (high20 - m["close"]) / a if a else 0}


def score_dip(data):
    """回调抄底打分：回落深度 50、长期趋势强度 30、成交额 20。符合条件：站上 200 日线且回落 ≥ 2.5 ATR。"""
    items = [(c, m) for c, m in data.items() if m and m["dollar_vol"] >= MIN_DOLLAR_VOL]
    if not items:
        return []
    pb = _pct_rank([m["pullback_atr"] for _, m in items])
    tr = _pct_rank([m["trend200"] for _, m in items])
    dv = _pct_rank([m["dollar_vol"] for _, m in items])
    out = []
    for i, (code, m) in enumerate(items):
        ok = m["above200"] and m["pullback_atr"] >= 2.5
        reasons = [f"从 20 日高点回落 {m['pullback_atr']:.1f} ATR",
                   f"{'站上' if m['above200'] else '跌破'} 200 日线（{m['trend200'] * 100:+.1f}%）",
                   f"5日 {m['ret5'] * 100:+.1f}%", f"日均波动 {m['atr_pct']:.1f}%"]
        out.append({"code": code, "score": round(50 * pb[i] + 30 * tr[i] + 20 * dv[i], 1), "eligible": ok,
                    "reason": "，".join(reasons), **m})
    out.sort(key=lambda x: (not x["eligible"], -x["score"]))
    for rank, x in enumerate(out, 1):
        x["rank"] = rank
    return out


def metrics(rows):
    """单只股票的原始指标；数据不足返回 None。"""
    if len(rows) < 51:
        return None
    closes = [r[2] for r in rows]
    vols = [r[5] for r in rows]
    c = closes[-1]
    ma20 = statistics.mean(closes[-20:])
    ma50 = statistics.mean(closes[-50:])
    trs = [max(h - l, abs(h - rows[i - 1][2]), abs(l - rows[i - 1][2]))
           for i, (_, _, _, h, l, _) in enumerate(rows) if i > 0][-14:]
    avg_vol = statistics.mean(vols[-21:-1]) or 1
    return {"close": c, "date": rows[-1][0], "ret5": c / closes[-6] - 1, "ret20": c / closes[-21] - 1,
            "ma20": ma20, "ma50": ma50, "above20": c > ma20, "above50": c > ma50,
            "rvol": vols[-1] / avg_vol, "dollar_vol": statistics.mean(
                [cl * v for cl, v in zip(closes[-20:], vols[-20:])]),
            "atr_pct": sum(trs) / len(trs) / c * 100}


def _pct_rank(values):
    """每个值在整体里的百分位（0~1）。"""
    order = sorted(values)
    n = max(1, len(values) - 1)
    return [order.index(v) / n for v in values]


def score_all(data):
    """data: 代码 -> metrics。返回按分数从高到低的列表，每项含分数和中文理由。"""
    items = [(c, m) for c, m in data.items() if m and m["dollar_vol"] >= MIN_DOLLAR_VOL]
    if not items:
        return []
    r20 = _pct_rank([m["ret20"] for _, m in items])
    r5 = _pct_rank([m["ret5"] for _, m in items])
    rv = _pct_rank([m["rvol"] for _, m in items])
    out = []
    for i, (code, m) in enumerate(items):
        trend = (0.5 if m["above20"] else 0) + (0.5 if m["above50"] else 0)
        vol_ok = ATR_BAND[0] <= m["atr_pct"] <= ATR_BAND[1]
        # 权重：中期动量 30、短期动量 15、放量 20、趋势 25、波动适中 10
        score = 30 * r20[i] + 15 * r5[i] + 20 * rv[i] + 25 * trend + (10 if vol_ok else 0)
        reasons = [f"20日 {m['ret20'] * 100:+.1f}%", f"5日 {m['ret5'] * 100:+.1f}%", f"量比 {m['rvol']:.1f}",
                   "站上20/50日线" if trend == 1 else "站上20日线" if m["above20"] else
                   "站上50日线" if m["above50"] else "在20/50日线下方",
                   f"日均波动 {m['atr_pct']:.1f}%" + ("" if vol_ok else "（偏离 1.5%~6% 区间）")]
        out.append({"code": code, "score": round(score, 1), "eligible": m["above20"] and m["ret20"] > 0,
                    "reason": "，".join(reasons), **m})
    out.sort(key=lambda x: -x["score"])
    for rank, x in enumerate(out, 1):
        x["rank"] = rank
    return out


def run(symbols, style="trend"):
    """symbols: 代码 -> 腾讯带后缀的 symbol（如 usAAPL -> AAPL.OQ）。并发拉日 K 后打分。
    style：trend 追涨打分 / dip 回调抄底打分（要 200 日线，多拉一些日 K）。"""
    def one(item):
        code, sym = item
        try:
            if style == "dip":
                return code, dip_metrics(fetch_daily_vol(sym, 260))
            return code, metrics(fetch_daily_vol(sym))
        except Exception:
            return code, None
    with ThreadPoolExecutor(max_workers=4) as ex:  # 并发别太高，免得被限流
        data = dict(ex.map(one, symbols.items()))
    return score_dip(data) if style == "dip" else score_all(data)


def why_remove(x, style="trend"):
    """这只股票为什么被清掉；还够格返回 None。x 为 None 表示拿不到数据。"""
    if x is None:
        return "拿不到行情数据"
    if style == "dip":
        if not x["above200"]:
            return "跌破 200 日线，长期趋势坏了"
        if x["pullback_atr"] < 2.5:
            return f"已经反弹（离 20 日高点只差 {x['pullback_atr']:.1f} ATR），不在回调区"
        return None
    if not x["above20"]:
        return f"跌破 20 日线（评分 {x['score']}）"
    if x["ret20"] <= 0:
        return f"20 日下跌 {x['ret20'] * 100:.1f}%（评分 {x['score']}）"
    if x["rank"] > KEEP_RANK:
        return f"评分 {x['score']}，排第 {x['rank']} 名，掉出前 {KEEP_RANK}"
    return None


def plan_changes(ranked, watchlist, protected, skip_add=(), style="trend"):
    """决定今天清掉哪些、加入哪些。
    watchlist：当前自选；protected：指数、持仓、你锁定的，永远不动；skip_add：7 天内你手动删过的，不自动加回。
    返回 (加入列表, {移除代码: 原因})。"""
    by_code = {x["code"]: x for x in ranked}
    remove = {}
    for c in watchlist:
        if c not in protected:
            reason = why_remove(by_code.get(c), style)
            if reason:
                remove[c] = reason
    keep = [c for c in watchlist if c not in protected and c not in remove]
    add = []
    for x in ranked:
        if len(keep) + len(add) >= AUTO_N:
            break
        c = x["code"]
        if x["eligible"] and c not in watchlist and c not in protected and c not in skip_add:
            add.append(c)
    return add, remove
