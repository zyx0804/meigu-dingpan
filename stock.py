"""美股盯盘 —— 轻量版：行情、到价提醒，连上 moomoo 后自动评估持仓风险，可为持仓挂止损/止盈单（需逐张确认）。"""
import json
import os
import sys
import threading
import time
import tkinter as tk
import urllib.request
from datetime import datetime
from tkinter import messagebox, simpledialog, ttk

import backtest
import brackets
import journal
import orders
import risk
import rules
import screener
import theme
import tuner
from moomoo_link import MoomooLink, opend_running

from common import (COLS, CONF, DATA, DEFAULT, ENV_NAME, HEAD, INDICES, OCOLS, OHEAD, OWIDTH, PCOLS,  # noqa: F401
                    PHEAD, PWIDTH, RCOLS, RHEAD, ROOT, RWIDTH, STATUS_NAME, WIDTH, fetch, fetch_daily, load,
                    normalize, save)


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = load()
        self.quotes = {}
        self.fired = {}  # (代码, 类型) -> 日期，同一条提醒每天只响一次
        self.mini = False
        self.link = MoomooLink()
        self.desk = orders.OrderDesk(self.link)
        self.bm = brackets.BracketManager(self.desk, self.cfg["brackets"],
                                          lambda m: (self.log_order(m), self.root.after(0, self.notify_log, m)))
        self.backtesting = False
        self.acct = None  # 账户资产
        self.positions = []
        self.acc_status = "未连接：请先打开 moomoo OpenD 并登录"
        self.hist = {}  # 代码 -> (日期, 日K)
        self.reports = {}
        root.title("美股盯盘")
        root.geometry("1180x640")
        root.minsize(900, 480)
        theme.apply(root)

        # ---- 顶栏：标题 + 自选操作 + 连接状态 ----
        self.topbar = bar = ttk.Frame(root, padding=(14, 10, 14, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="美股盯盘", style="Title.TLabel").pack(side="left", padx=(0, 16))
        self.entry = ttk.Entry(bar, width=16)
        self.entry.pack(side="left")
        self.entry.bind("<Return>", lambda e: self.add())
        self._placeholder(self.entry, "输入代码 如 AAPL")
        ttk.Button(bar, text="＋ 添加", style="Accent.TButton", command=self.add).pack(side="left", padx=(6, 2))
        ttk.Button(bar, text="删除", command=self.remove).pack(side="left", padx=2)
        ttk.Button(bar, text="到价提醒", command=self.set_alert).pack(side="left", padx=2)
        ttk.Button(bar, text="锁定/解锁", command=self.toggle_lock).pack(side="left", padx=2)
        self.screen_btn = ttk.Button(bar, text="立即选股", command=lambda: self.start_screen(force=True))
        self.screen_btn.pack(side="left", padx=2)
        self.auto_var = tk.BooleanVar(value=self.cfg["auto_screen"])
        ttk.Checkbutton(bar, text="每日自动选股", variable=self.auto_var, command=self.toggle_auto).pack(side="left", padx=6)
        self.conn_var = tk.StringVar(value="● 未连接 moomoo")
        self.conn_lbl = ttk.Label(bar, textvariable=self.conn_var, foreground=theme.DOWN)
        self.conn_lbl.pack(side="right", padx=10)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=14)

        # ---- 自选行情 ----
        wf = ttk.Frame(self.nb, style="Card.TFrame")
        self.nb.add(wf, text="  自选行情  ")
        self.tree = self._table(wf, COLS, HEAD, WIDTH)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda e: self.set_alert())
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_reason())
        self.reason_var = tk.StringVar(value="选中一只股票可以看它的筛选理由。自动选股只看走势和成交数据，不代表推荐，买不买由你判断。")
        ttk.Label(wf, textvariable=self.reason_var, style="Sub.TLabel", background=theme.PANEL,
                  padding=(10, 6)).pack(fill="x")
        self.screening = False

        # ---- 我的持仓：资产卡片 + 风险表 + 说明 ----
        pf = ttk.Frame(self.nb, padding=(0, 10, 0, 0))
        self.nb.add(pf, text="  持仓 · 风险  ")
        cards = ttk.Frame(pf)
        cards.pack(fill="x")
        self.card_vars = {k: tk.StringVar(value="--") for k in ("total", "mv", "cash", "pl", "risk")}
        self.card_lbls = {}
        for i, (k, title) in enumerate((("total", "总资产 (USD)"), ("mv", "持仓市值"), ("cash", "现金"),
                                         ("pl", "持仓盈亏"), ("risk", "账户整体风险"))):
            f, lbl = theme.card(cards, title, self.card_vars[k])
            f.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 8, 0))
            cards.columnconfigure(i, weight=1)
            self.card_lbls[k] = lbl
        self.acct_var = tk.StringVar(value=self.acc_status)

        pbar = ttk.Frame(pf, padding=(0, 10, 0, 6))
        pbar.pack(fill="x")
        ttk.Label(pbar, text="持仓周期").pack(side="left")
        self.mode_var = tk.StringVar(value=risk.MODES[self.cfg["mode"]][0])
        mode_box = ttk.Combobox(pbar, textvariable=self.mode_var, state="readonly", width=11,
                                values=[m[0] for m in risk.MODES.values()])
        mode_box.pack(side="left", padx=6)
        mode_box.bind("<<ComboboxSelected>>", lambda e: self.set_mode())
        ttk.Label(pbar, textvariable=self.acct_var, style="Sub.TLabel").pack(side="left", padx=10)
        ttk.Button(pbar, text="一键挂保护单", style="Accent.TButton", command=self.one_click).pack(side="right")
        ttk.Button(pbar, text="手动挂单", command=self.order_dialog).pack(side="right", padx=(6, 6))
        ttk.Button(pbar, text="参考位设为提醒", command=self.reports_to_alerts).pack(side="right", padx=6)
        ttk.Button(pbar, text="回测离场规则", command=self.show_backtest).pack(side="right")
        ttk.Button(pbar, text="交易复盘 · 规则进化", command=self.show_journal).pack(side="right", padx=(0, 6))

        pt = ttk.Frame(pf, style="Card.TFrame")
        pt.pack(fill="both", expand=True)
        self.ptree = self._table(pt, PCOLS, PHEAD, PWIDTH)
        self.ptree.tag_configure("lv2", background=theme.RISK_HIGH)
        self.ptree.tag_configure("lv1", background=theme.RISK_MID)
        self.ptree.pack(fill="both", expand=True)
        self.ptree.bind("<Double-1>", lambda e: self.show_report())
        self.summary = theme.text(tk.Text(pf, height=4, state="disabled"), 10)
        self.summary.pack(fill="x", pady=(8, 0))
        ttk.Label(pf, text="双击持仓看完整风险报告 · 参考位按波动率计算，不构成投资建议 · 只有你点确认才会挂单",
                  style="Sub.TLabel").pack(anchor="w", pady=(4, 0))

        # ---- 挂单 ----
        of = ttk.Frame(self.nb, padding=(0, 10, 0, 0))
        self.nb.add(of, text="  挂单  ")
        obar = ttk.Frame(of, padding=(0, 0, 0, 6))
        obar.pack(fill="x")
        ttk.Button(obar, text="刷新", command=self.refresh_orders).pack(side="left")
        ttk.Button(obar, text="撤销选中", command=self.cancel_order).pack(side="left", padx=6)
        ttk.Button(obar, text="全部撤销", command=self.cancel_all).pack(side="left")
        self.order_msg = tk.StringVar(value="每 30 秒自动刷新模拟盘和真实账户里还没成交的挂单")
        ttk.Label(obar, textvariable=self.order_msg, style="Sub.TLabel").pack(side="left", padx=8)
        ot = ttk.Frame(of, style="Card.TFrame")
        ot.pack(fill="both", expand=True)
        self.otree = self._table(ot, OCOLS, OHEAD, OWIDTH)
        self.otree.pack(fill="both", expand=True)
        rbar = ttk.Frame(of, padding=(0, 10, 0, 6))
        rbar.pack(fill="x")
        ttk.Label(rbar, text="联动规则（止损 + 止盈，一边成交自动撤另一边）", font=(theme.FONT, 10, "bold")).pack(side="left")
        ttk.Button(rbar, text="停止选中规则", command=self.stop_rule).pack(side="right")
        rt = ttk.Frame(of, style="Card.TFrame")
        rt.pack(fill="x")
        self.rtree = self._table(rt, RCOLS, RHEAD, RWIDTH)
        self.rtree.configure(height=4)
        self.rtree.pack(fill="x")
        self.root.after(500, self.render_rules)

        # ---- 底部：提醒记录 + 状态栏 ----
        self.log = theme.text(tk.Text(root, height=3, state="disabled"), 9)
        self.log.pack(fill="x", padx=14, pady=(8, 0))
        self.statusbar = sb = ttk.Frame(root, padding=(14, 4, 14, 6))
        sb.pack(fill="x")
        self.status = tk.StringVar(value="加载中…")
        ttk.Label(sb, textvariable=self.status, style="Sub.TLabel").pack(side="left")
        ttk.Button(sb, text="迷你模式", command=self.toggle_mini).pack(side="right", padx=(6, 0))
        self.top_var = tk.BooleanVar()
        ttk.Checkbutton(sb, text="窗口置顶", variable=self.top_var,
                        command=lambda: root.attributes("-topmost", self.top_var.get())).pack(side="right", padx=6)
        ttk.Label(sb, text="数据：moomoo OpenD / 腾讯财经", style="Sub.TLabel").pack(side="right", padx=10)

        os.makedirs(DATA, exist_ok=True)
        threading.Thread(target=self.loop, daemon=True).start()

    def _placeholder(self, entry, hint):
        """输入框里的灰色提示字。"""
        def show(_=None):
            if not entry.get():
                entry.insert(0, hint)
                entry.configure(foreground=theme.SUB)
        def hide(_=None):
            if entry.get() == hint:
                entry.delete(0, "end")
                entry.configure(foreground=theme.TEXT)
        entry.bind("<FocusIn>", hide, add="+")
        entry.bind("<FocusOut>", show, add="+")
        self._hint = hint
        show()

    @staticmethod
    def _sync(tree, rows):
        """rows: [(iid, values, tags)]。行没变就原地更新，避免每秒重建表格导致闪烁、滚动条跳回顶部。"""
        if list(tree.get_children()) == [r[0] for r in rows]:
            for iid, values, tags in rows:
                tree.item(iid, values=values, tags=tags)
            return
        sel = tree.selection()
        tree.delete(*tree.get_children())
        for iid, values, tags in rows:
            tree.insert("", "end", iid=iid, values=values, tags=tags)
        for s in sel:
            if tree.exists(s):
                tree.selection_add(s)

    def _table(self, parent, cols, heads, widths):
        t = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            t.heading(c, text=h)
            t.column(c, width=w, minwidth=40, anchor="w" if c == "name" else "e" if c not in ("code", "level", "env", "status", "side", "type", "tif", "alert", "time", "src") else "center")
        t.tag_configure("odd", background=theme.STRIPE)
        t.tag_configure("up", foreground=theme.UP)
        t.tag_configure("down", foreground=theme.DOWN)
        t.tag_configure("hit", background=theme.RISK_MID)
        return t

    # ---------- 后台线程：只做网络请求，界面更新交给主线程 ----------
    def loop(self):
        """每秒一轮：行情 + 规则检测。账户和挂单列表每 5 秒（读 OpenD 缓存，不受频率限制）。"""
        last_acc = last_tx = last_daily = 0
        base = {}  # 腾讯行情（名称、交易所后缀、指数），moomoo 有行情时 10 秒更新一次就够
        while True:
            t0 = time.time()
            if t0 - last_acc >= 5:
                last_acc = t0
                self.refresh_account()
                if self.link.trd:
                    self.root.after(0, self.refresh_orders)
            if t0 - last_daily >= 60:
                last_daily = t0
                self.root.after(0, self.start_screen)    # 每天第一次检查时自动选股
                self.root.after(0, self.start_backtest)  # 每天回测一次离场规则
            try:
                codes = list(dict.fromkeys(self.cfg["stocks"] + [p["code"] for p in self.positions]))
                snap = {}
                if self.link.quote:
                    try:  # moomoo 实时行情：快照接口 30 秒最多 60 次，每秒 1 次在限额内
                        snap = self.link.snapshot(codes)
                    except Exception:
                        snap = {}
                if not snap or t0 - last_tx >= 10 or any(c not in base for c in codes):
                    base = fetch(codes)
                    last_tx = t0
                q = {c: {**base.get(c, {}), **snap.get(c, {})} for c in codes if c in base or c in snap}
                self.quotes_live = q
                self.update_hist(q)
                self.run_brackets()  # 每秒检测一次规则（止盈、自动跟踪、自动撤单）
                self.root.after(0, self.render, q, None)
            except Exception as e:
                self.root.after(0, self.render, None, str(e))
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

    def run_brackets(self):
        """后台线程里检查联动规则（每 30 秒）。"""
        if not self.link.trd or not any(b["status"] == "active" for b in self.cfg["brackets"]):
            return
        prices = {c: d.get("price") for c, d in getattr(self, "quotes_live", self.quotes).items()}
        dyn = {c: r["trail_stop"] for c, r in self.reports.items() if r.get("trail_stop")}
        before = json.dumps(self.cfg["brackets"], sort_keys=True, default=str)
        self.bm.tick(prices, self.desk.position_qty, dyn)
        if json.dumps(self.cfg["brackets"], sort_keys=True, default=str) != before:  # 有变化才写盘
            self.root.after(0, lambda: (save(self.cfg), self.render_rules()))

    def update_hist(self, q):
        """持仓的半年日 K，每天拉一次。"""
        today = datetime.now().strftime("%Y-%m-%d")
        for p in self.positions:
            c = p["code"]
            if self.hist.get(c, (None,))[0] == today or "symbol" not in q.get(c, {}):
                continue
            try:
                self.hist[c] = (today, fetch_daily(q[c]["symbol"]))
            except Exception:
                pass

    # ---------- 持仓风险 ----------
    def render_positions(self):
        a = self.acct
        table = []
        self.reports = {}
        for i, p in enumerate(self.positions):
            c = p["code"]
            price = self.quotes.get(c, {}).get("price", p["price"])
            rows = self.hist.get(c, (None, []))[1]
            r = risk.assess(p, price, a and a["total_assets"], rows, self.cfg["mode"], self.cfg["bt"].get("k"),
                            params=self.cfg["rule_params"],
                            entry_date=journal.entry_date(self.cfg["journal"].get("open", {}), c))
            r.update(code=c, name=p["name"], price=price, pos=p)
            self.reports[c] = r
            f = lambda x, fmt: fmt.format(x) if x is not None else "-"
            tags = ["odd"] if i % 2 else []
            tags.append("up" if r["pl_pct"] > 0 else "down" if r["pl_pct"] < 0 else "")
            if r["level"]:
                tags.append(f"lv{r['level']}")
            if r["exit_signals"]:
                tags.append("lv2")
                self.exit_alert(c, r)
            table.append((c, (
                p["name"], c, f"{p['qty']:g}" + ("(空)" if p["side"] == "short" else ""), f"{p['cost']:.2f}", price,
                f"{r['pl_pct']:+.2f}%", f"{r['weight']:.0f}%", f(r["atr_pct"], "{:.1f}%"),
                f(r["risk_score"], "{}"), f(r["trail_stop"], "{:.2f}"),
                "已保护" if c in self.active_rule_codes() else "⚠ 未保护",
                "；".join(r["exit_signals"]) or "—",
                risk.LEVELS[r["level"]] if r["level"] is not None else "计算中"), tags))
        self._sync(self.ptree, table)

        connected = self.acc_status == "已连接"
        self.conn_var.set("● 已连接 moomoo" if connected else "● " + self.acc_status.split("：")[0])
        self.conn_lbl.configure(foreground=theme.UP if connected else theme.DOWN)
        if not a:
            self.acct_var.set(self.acc_status)
            for v in self.card_vars.values():
                v.set("--")
            self._set_summary("")
            return
        acc = risk.assess_account(a, self.reports)
        lv = risk.LEVELS[acc["level"]] if acc["level"] is not None else "-"
        mv = sum(r["value"] for r in self.reports.values())
        pl = sum((r["price"] - r["pos"]["cost"]) * r["pos"]["qty"] * (1 if r["pos"]["side"] == "long" else -1)
                 for r in self.reports.values())
        self.card_vars["total"].set(f"${a['total_assets'] or 0:,.2f}")
        self.card_vars["mv"].set(f"${mv:,.2f}")
        self.card_vars["cash"].set(f"${a['cash'] or 0:,.2f}")
        self.card_vars["pl"].set(f"{'+' if pl >= 0 else '-'}${abs(pl):,.2f}")
        self.card_lbls["pl"].configure(foreground=theme.UP if pl > 0 else theme.DOWN if pl < 0 else theme.TEXT)
        self.card_vars["risk"].set(lv)
        self.card_lbls["risk"].configure(foreground={"高": theme.DOWN, "中": "#f0a24a", "低": theme.UP}.get(lv, theme.TEXT))
        self.acct_var.set(f"持仓 {len(self.positions)} 只 · 购买力 ${a['power'] or 0:,.2f}")
        lines = ["账户：" + "；".join(acc["reasons"])]
        for r in sorted(self.reports.values(), key=lambda r: -(r["level"] or 0)):
            if r["level"] is not None:
                lines.append(f"{r['name']}（风险{risk.LEVELS[r['level']]}）：" + "；".join(r["reasons"][:2]))
        self._set_summary("\n".join(lines))

    def exit_alert(self, code, r):
        """离场信号：每只股票每种信号每天只响一次。"""
        today = datetime.now().strftime("%Y-%m-%d")
        for sig in r["exit_signals"]:
            key = (code, "exit:" + sig.split(" ")[0])
            if self.fired.get(key) != today:
                self.fired[key] = today
                self.notify(f"离场信号 · {r['name']}({code})：{sig}（现价 {r['price']}）")

    def set_mode(self):
        self.cfg["mode"] = next(k for k, m in risk.MODES.items() if m[0] == self.mode_var.get())
        save(self.cfg)
        self.render_positions()

    def _set_summary(self, text):
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text)
        self.summary.configure(state="disabled")

    def show_report(self):
        sel = self.ptree.selection()
        r = self.reports.get(sel[0]) if sel else None
        if not r:
            return
        p = r["pos"]
        lines = [f"{r['name']}（{r['code']}）  风险等级：{risk.LEVELS[r['level']] if r['level'] is not None else '-'}",
                 f"{p['qty']:g} 股 · 成本 {p['cost']:.2f} · 现价 {r['price']} · 市值 ${r['value']:,.2f} · 盈亏 {r['pl_pct']:+.2f}%",
                 "", "【风险因素】"] + [f"· {t}" for t in r["reasons"]]
        if r["stop"] is not None:
            lines += ["", f"【参考位 · {r['mode_name']}】（按波动率计算，不是买卖建议）",
                      f"· 波动参考止损：{r['stop']:.2f}（离现价 {r['stop_atr']:g} 个 ATR，约 {r['stop_atr']:g} 天的正常波动幅度）",
                      f"· 参考目标：{r['target']:.2f}（{r['target_atr']:g} 个 ATR，与参考止损构成 2:1）",
                      f"· 按单笔最多亏总资产 {risk.RISK_PER_TRADE:g}%：参考止损下最多持有 {r['max_shares']} 股（现在 {p['qty']:g} 股）",
                      f"· 20 日区间：{r['low20']:.2f} – {r['high20']:.2f}",
                      "", "【动态离场】（随行情移动，不是写死的价格）",
                      f"· 动态止损：{r['trail_stop']:.2f}（当前阶段：{r['stop_stage']}）",
                      f"  规则：相对成本最多亏 {risk.LOSS_CAP_ATR:g} ATR / 最近 {risk.TRAIL_LOOKBACK[self.cfg['mode']]} 天"
                      f"最高点回撤 {risk.TRAIL_ATR:g} ATR / 浮盈 {risk.BREAKEVEN_ATR:g} ATR 后保本，三条取最有利，只上移不下移",
                      f"· 打到止损：" + (f"亏 ${r['stop_loss_usd']:,.2f}" if r["stop_loss_usd"] > 0
                                     else f"仍赚 ${-r['stop_loss_usd']:,.2f}") + f"；最多亏 ${r['max_loss']:,.2f}",
                      f"· 风险分：{r['risk_score']} / 100，≥ {risk.EXIT_SCORE} 发出离场信号",
                      f"· 当前离场信号：{'；'.join(r['exit_signals']) or '无'}",
                      "", "【隔夜风险】（止损单只在交易时段触发，开盘跳空时挡不住）",
                      f"· 近半年 {r['gap_through']:.0f}% 的交易日开盘就直接跳过参考止损",
                      f"· 隔夜不利跳空：5% 的日子超过 {r['gap95'] * 100:.1f}%，最差一次 {r['worst_gap'] * 100:.1f}%",
                      "", "【如果价格变成…】  相对现在的盈亏 / 相对成本的盈亏 / 占总资产"]
            for s in r["scenarios"]:
                lines.append(f"· {s['label']}：{s['price']:.2f}  →  ${s['pnl_now']:+,.2f} / ${s['pnl_cost']:+,.2f} / {s['pct_assets']:+.1f}%")
        win = theme.toplevel(self.root, f"风险报告 — {r['name']}")
        t = theme.text(tk.Text(win, width=80, height=26), 10)
        t.configure(padx=18, pady=14, spacing1=2, spacing3=2)
        t.pack(fill="both", expand=True, padx=10, pady=10)
        t.tag_configure("h", foreground=theme.ACCENT, font=(theme.FONT, 11, "bold"))
        t.tag_configure("title", foreground=theme.TEXT, font=(theme.FONT, 13, "bold"))
        t.tag_configure("neg", foreground=theme.DOWN)
        t.tag_configure("pos", foreground=theme.UP)
        for i, line in enumerate(lines):
            tag = "title" if i == 0 else "h" if line.startswith("【") else None
            if tag:
                t.insert("end", line + "\n", tag)
            elif "→" in line:  # 情景行：把盈亏金额按正负上色
                head, tail = line.split("→", 1)
                t.insert("end", head + "→")
                for part in tail.split("/"):
                    t.insert("end", part, "neg" if "-" in part else "pos" if "+" in part else None)
                    t.insert("end", "/")
                t.delete("end-2c")
                t.insert("end", "\n")
            else:
                t.insert("end", line + "\n")
        t.configure(state="disabled")

    def reports_to_alerts(self):
        ready = {c: r for c, r in self.reports.items() if r["stop"] is not None}
        if not ready:
            messagebox.showinfo("提示", "还没有读到持仓或历史数据")
            return
        for c, r in ready.items():
            stop, target = round(r["stop"], 2), round(r["target"], 2)
            self.cfg["alerts"][c] = ({"below": stop, "above": target} if r["pos"]["side"] == "long"
                                     else {"above": stop, "below": target})
            for k in ("above", "below", "pct"):
                self.fired.pop((c, k), None)
        save(self.cfg)
        messagebox.showinfo("提示", f"已给 {len(ready)} 只持仓设置参考止损/目标的到价提醒")

    # ---------- 一键挂保护单（严格按 rules.py 的规则） ----------
    def active_rule_codes(self):
        return {"us" + b["code"].split(".", 1)[1] for b in self.cfg["brackets"]
                if b["status"] == "active" and b["env"] == "REAL"}

    def one_click(self):
        if not self.link.trd or not self.positions:
            messagebox.showinfo("提示", "先连上 moomoo（OpenD 登录），并且真实账户里要有持仓")
            return
        items = rules.plan(self.positions, self.reports, self.active_rule_codes())
        win = theme.toplevel(self.root, "一键挂保护单")
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack()
        ttk.Label(frm, text="交易规则（程序严格按这些执行）", font=(theme.FONT, 11, "bold"),
                  foreground=theme.ACCENT).pack(anchor="w")
        rule_lines = rules.rules_text(self.cfg["rule_params"])
        ttk.Label(frm, text=f"规则版本 v{self.cfg['rule_params']['version']}\n" + "\n".join(
            f"{i}. {t}" for i, t in enumerate(rule_lines, 1)), style="Sub.TLabel",
                  justify="left", wraplength=620).pack(anchor="w", pady=(4, 12))
        ttk.Label(frm, text="本次计划（真实账户）", font=(theme.FONT, 11, "bold")).pack(anchor="w")
        box = ttk.Frame(frm, style="Card.TFrame", padding=(12, 8))
        box.pack(fill="x", pady=(4, 10))
        picks = []
        label = {"protect": "挂保护单", "exit": "规则离场", "skip": "跳过"}
        for it in items:
            row = ttk.Frame(box, style="Card.TFrame")
            row.pack(fill="x", pady=3)
            on = tk.BooleanVar(value=it["action"] != "skip" and not it.get("error"))
            ex = tk.BooleanVar(value=False)
            cb = ttk.Checkbutton(row, variable=on, style="Card.TCheckbutton",
                                 text=f"{it['name']} {it['qty']:g} 股 · {label[it['action']]}")
            cb.pack(anchor="w")
            if it["action"] == "skip" or it.get("error"):
                cb.state(["disabled"])
            ttk.Label(row, text="   " + (it.get("error") or it["why"]), background=theme.PANEL,
                      foreground=theme.SUB, wraplength=600, justify="left").pack(anchor="w")
            if it["optional_exit"]:
                ttk.Checkbutton(row, variable=ex, style="Card.TCheckbutton",
                                text="改为按风险分提前离场（立即限价卖出，不挂保护单）").pack(anchor="w", padx=(16, 0))
            picks.append((it, on, ex))
        ttk.Label(frm, style="Warn.TLabel", wraplength=620, justify="left", text=(
            "· 需要先在 moomoo OpenD 窗口里「解锁交易」\n"
            "· 确认后按规则自动执行：跟踪止损在服务器上，止盈联动、自动撤单、自动跟踪由程序负责（程序和 OpenD 需开着）\n"
            "· 规则是按历史波动和回测算的，不代表未来，也不是买卖建议")).pack(anchor="w")

        def go():
            chosen = [(it, ex.get()) for it, on, ex in picks if on.get()]
            if not chosen:
                messagebox.showinfo("提示", "没有要执行的项目", parent=win)
                return
            lines = []
            for it, early in chosen:
                specs = [it["optional_exit"]] if early else it["specs"]
                lines += [orders.describe(s, "REAL") for s in specs]
            if not messagebox.askyesno("确认执行（真实账户）", "将在真实账户执行：\n\n" + "\n".join(lines) +
                                       "\n\n触发后会真的成交。确定吗？", icon="warning", default="no", parent=win):
                return
            results = []
            for it, early in chosen:
                try:
                    if self.desk.held("REAL", it["code"]) < it["qty"]:
                        results.append(f"✗ {it['name']}：可卖数量不足（可能已有挂单占用），跳过")
                        continue
                    if early or it["action"] == "exit":
                        s = it["optional_exit"] if early else it["specs"][0]
                        oid = self.desk.place(s, "REAL")
                        results.append(f"✓ {it['name']} 规则离场单已提交，订单号 {oid}")
                        self.log_order(f"一键：规则离场 {orders.describe(s, 'REAL')} 订单号 {oid}")
                    else:
                        _, notes = self.bm.create(it["specs"][0], it["specs"][1] if len(it["specs"]) > 1 else None,
                                                  "REAL")
                        results.append(f"✓ {it['name']}：" + "；".join(notes))
                except Exception as e:
                    msg = str(e)
                    if "unlock" in msg.lower() or "解锁" in msg:
                        msg += "（请先在 OpenD 窗口里点「解锁交易」）"
                    results.append(f"✗ {it['name']}：{msg}")
                    self.log_order(f"一键：{it['name']} 失败：{msg}")
            save(self.cfg)
            messagebox.showinfo("执行结果", "\n\n".join(results), parent=win)
            self.render_rules()
            self.refresh_orders()
            self.render_positions()
            win.destroy()

        bf = ttk.Frame(frm)
        bf.pack(pady=(12, 0))
        ttk.Button(bf, text="一键执行", style="Accent.TButton", command=go).pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=win.destroy).pack(side="left", padx=4)

    # ---------- 手动挂单（只平仓，每张都要确认） ----------
    def order_dialog(self):
        sel = self.ptree.selection()
        r = self.reports.get(sel[0]) if sel else None
        if not r or not self.link.trd:
            messagebox.showinfo("提示", "先连上 moomoo，并在「我的持仓」里选中一只股票")
            return
        p, price = r["pos"], r["price"]
        win = theme.toplevel(self.root, f"挂止损/止盈单 — {r['name']}")
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack()
        ttk.Label(frm, text=f"{r['name']}  {p['qty']:g} 股 · 成本 {p['cost']:.2f} · 现价 {price}",
                  font=(theme.FONT, 12, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

        env = tk.StringVar(value="SIMULATE")
        ef = ttk.Frame(frm)
        ef.grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(ef, text="账户：").pack(side="left")
        held = {}  # 两个账户各自能卖多少股：模拟盘和真实账户的持仓是分开的
        for k in ("SIMULATE", "REAL"):
            try:
                held[k] = self.desk.held(k, p["code"])
            except Exception:
                held[k] = None
            n = "查询失败" if held[k] is None else f"可卖 {held[k]:g} 股"
            ttk.Radiobutton(ef, text=f"{ENV_NAME[k]}（{n}）", value=k, variable=env).pack(side="left", padx=(0, 8))

        if held.get("SIMULATE") == 0 and (held.get("REAL") or 0) > 0:
            env.set("REAL")  # 持仓在真实账户就默认选真实账户（提交时仍要你确认两次）

        fmt = lambda x: f"{x:.2f}" if x is not None else ""
        defaults = {"跟踪止损": f"{r['trail_pct']:.2f}" if r.get("trail_pct") else "",
                    "止损市价": fmt(r["stop"]), "止损限价": fmt(r["stop"])}
        v = {"stop_on": tk.BooleanVar(value=True), "stop_type": tk.StringVar(value="跟踪止损"),
             "stop": tk.StringVar(value=defaults["跟踪止损"]), "tp_on": tk.BooleanVar(value=True),
             "tp": tk.StringVar(value=fmt(r["target"])), "qty": tk.StringVar(value=f"{p['qty']:g}")}
        unit = tk.StringVar(value="% 回撤")
        ttk.Checkbutton(frm, text="止损", variable=v["stop_on"]).grid(row=2, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=v["stop_type"], values=("跟踪止损", "止损市价", "止损限价"),
                     state="readonly", width=9).grid(row=2, column=1, sticky="w")
        ttk.Entry(frm, textvariable=v["stop"], width=10).grid(row=2, column=2, sticky="w", padx=4)
        ttk.Label(frm, textvariable=unit, style="Sub.TLabel").grid(row=2, column=3, sticky="w")

        def on_type(*_):
            v["stop"].set(defaults[v["stop_type"].get()])
            unit.set("% 回撤" if v["stop_type"].get() == "跟踪止损" else "触发价")
        v["stop_type"].trace_add("write", on_type)
        ttk.Checkbutton(frm, text="止盈", variable=v["tp_on"]).grid(row=3, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=v["tp"], width=10).grid(row=3, column=2, sticky="w", padx=4)
        ttk.Label(frm, text="限价", style="Sub.TLabel").grid(row=3, column=3, sticky="w")
        ttk.Label(frm, text="数量").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Entry(frm, textvariable=v["qty"], width=10).grid(row=4, column=2, sticky="w", padx=4)

        preview = tk.StringVar()
        pv = ttk.Frame(frm, style="Card.TFrame", padding=(12, 8))
        pv.grid(row=5, column=0, columnspan=4, sticky="we", pady=10)
        ttk.Label(pv, textvariable=preview, wraplength=440, justify="left", background=theme.PANEL).pack(anchor="w")
        ttk.Label(frm, wraplength=460, justify="left", style="Warn.TLabel", text=(
            f"· 已自动填好：跟踪幅度 = {r.get('trail_k') or '-'} 个 ATR（来自回测），止盈 = 参考目标\n"
            "· 跟踪止损：价格涨它跟着涨、只升不降，回落到设定幅度就卖；挂在 moomoo 服务器上，关机也会执行\n"
            "· 止损 + 止盈同时勾选 = 联动规则：一边成交程序自动撤另一边；如果 moomoo 不让同一批股票挂两张卖单，"
            "止盈改由程序盯价执行（这部分需要程序和 OpenD 开着）\n"
            "· 模拟盘只支持限价/市价单，止损类单子可能会被拒\n"
            "· 真实账户需要你先在 moomoo OpenD 窗口里点「解锁交易」")).grid(row=6, column=0, columnspan=4, sticky="w")

        def specs():
            out, errs = [], []
            try:
                qty = float(v["qty"].get())
                if v["stop_on"].get():
                    kind = "trail" if v["stop_type"].get() == "跟踪止损" else "stop"
                    s, e = orders.build(p, price, kind, float(v["stop"].get()), qty, v["stop_type"].get() == "止损限价")
                    out.append(s) if s else errs.append(e)
                if v["tp_on"].get():
                    s, e = orders.build(p, price, "target", float(v["tp"].get()), qty)
                    out.append(s) if s else errs.append(e)
            except ValueError:
                errs.append("价格和数量请填数字")
            return out, errs

        def refresh(*_):
            out, errs = specs()
            h = held.get(env.get())
            if h is not None and out and h < max(s["qty"] for s in out):
                errs.append(f"{ENV_NAME[env.get()]}里这只股票只能卖 {h:g} 股"
                            + ("——模拟盘和真实账户的持仓是分开的，你的持仓在真实账户" if env.get() == "SIMULATE"
                               and (held.get("REAL") or 0) > 0 else ""))
            preview.set("\n".join([orders.describe(s, env.get()) for s in out] + ["⚠ " + e for e in errs])
                        or "请至少勾选一项")
        for var in list(v.values()) + [env]:
            var.trace_add("write", refresh)
        refresh()

        def submit():
            out, errs = specs()
            if errs or not out:
                messagebox.showwarning("无法提交", "\n".join(errs) or "请至少勾选一项", parent=win)
                return
            e = env.get()
            try:
                held = self.desk.held(e, p["code"])
            except Exception as ex:
                messagebox.showerror("无法提交", str(ex), parent=win)
                return
            if held < max(s["qty"] for s in out):
                messagebox.showwarning("无法提交", f"{ENV_NAME[e]}里 {r['name']} 可卖数量只有 {held:g} 股"
                                       + ("（模拟盘的持仓和真实账户是分开的）" if e == "SIMULATE" else ""), parent=win)
                return
            stop_s = next((s for s in out if s["kind"] in ("stop", "trail")), None)
            tgt_s = next((s for s in out if s["kind"] == "target"), None)
            linked = bool(stop_s)  # 有止损就建规则：自动跟踪 / 自动撤单都靠规则
            text = "\n".join(orders.describe(s, e) for s in out)
            if linked:
                text += ("\n\n这会建立一条自动规则：" + ("一边成交后自动撤掉另一边；" if tgt_s else "")
                         + ("固定止损跟着动态止损自动上调；" if stop_s["kind"] == "stop" else "跟踪止损由 moomoo 服务器自动跟踪；")
                         + "持仓卖掉后自动撤掉剩余挂单。之后按规则自动执行，不再逐次询问。")
            if not messagebox.askyesno("确认挂单", text + "\n\n确定提交吗？", icon="warning", default="no", parent=win):
                return
            if e == "REAL" and not messagebox.askyesno(
                    "再次确认：真实账户", "这会在你的真实账户挂单，触发后会真的成交。\n\n" + text + "\n\n确定吗？",
                    icon="warning", default="no", parent=win):
                return

            def explain(ex):
                msg = str(ex)
                if e == "REAL" and ("unlock" in msg.lower() or "解锁" in msg):
                    msg += "\n请先在 moomoo OpenD 窗口里点「解锁交易」"
                return msg
            results = []
            if linked:
                try:
                    _, results = self.bm.create(stop_s, tgt_s, e)
                    save(self.cfg)
                    self.render_rules()
                except Exception as ex:
                    results = [f"✗ 止损没挂上，规则没有建立：{explain(ex)}"]
                    self.log_order(f"规则建立失败 {orders.describe(stop_s, e)}：{explain(ex)}")
            else:
                for s in out:
                    name = orders.KIND_NAME[s["kind"]]
                    try:
                        oid = self.desk.place(s, e)
                        results.append(f"✓ {name}已提交，订单号 {oid}")
                        self.log_order(f"挂单成功 {orders.describe(s, e)} 订单号 {oid}")
                    except Exception as ex:
                        results.append(f"✗ {name}失败：{explain(ex)}")
                        self.log_order(f"挂单失败 {orders.describe(s, e)}：{explain(ex)}")
            messagebox.showinfo("结果", "\n".join(results), parent=win)
            self.refresh_orders()
            win.destroy()

        bf = ttk.Frame(frm)
        bf.grid(row=7, column=0, columnspan=3, pady=(10, 0))
        ttk.Button(bf, text="提交（会再次确认）", style="Accent.TButton", command=submit).pack(side="left", padx=4)
        ttk.Button(bf, text="取消", command=win.destroy).pack(side="left", padx=4)

    def log_order(self, msg):
        with open(os.path.join(DATA, "orders.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")

    def refresh_orders(self):
        if not self.link.trd:
            self.order_msg.set("未连接 moomoo")
            return
        rows, notes = [], []
        for e in ("SIMULATE", "REAL"):
            try:
                for o in self.desk.active_orders(e):
                    rows.append((f"{e}|{o['order_id']}", (
                        ENV_NAME[e], o["code"], o["name"], o["side"], o["type"], o["qty"], o["price"],
                        o["aux"], o["tif"], o["status"], o["time"]), ["odd"] if len(rows) % 2 else []))
            except Exception as ex:
                notes.append(f"{ENV_NAME[e]}：{str(ex)[:40]}")
        self._sync(self.otree, rows)
        n = len(rows)
        self.order_msg.set(f"{datetime.now():%H:%M:%S} 共 {n} 张未成交挂单" + ("；" + "；".join(notes) if notes else ""))

    def render_rules(self):
        self.rtree.delete(*self.rtree.get_children())
        rules = sorted(self.cfg["brackets"], key=lambda b: (b["status"] != "active", b["created"]), reverse=False)
        for i, b in enumerate(rules[:30]):
            sd = b["stop_desc"]
            stop = (f"跟踪 {sd['trail_value']}%" if sd["order_type"] == "TRAILING_STOP" else f"触发 {sd['aux_price']}")
            mode = "moomoo 服务器挂单" if b["mode"] == "server" else "程序盯价（需开着）"
            if b.get("phase") == "selling":
                mode = "止盈卖出中"
            self.rtree.insert("", "end", iid=b["id"], tags=["odd"] if i % 2 else [], values=(
                b["id"], ENV_NAME[b["env"]], b["code"], b["qty"],
                f"{stop} · #{b['stop_id']}" + (f" · 已上调 {b['ratchets']} 次" if b.get("ratchets") else ""),
                b["target"] or "—", mode if b["target"] else "只有止损", STATUS_NAME[b["status"]], b["created"]))

    def stop_rule(self):
        sel = self.rtree.selection()
        b = next((x for x in self.cfg["brackets"] if sel and x["id"] == sel[0]), None)
        if not b or b["status"] != "active":
            messagebox.showinfo("提示", "先选中一条运行中的规则")
            return
        if not messagebox.askyesno("停止规则", f"停止规则 {b['id']} 并撤销它的止损/止盈挂单？\n"
                                   "撤销后这只股票就没有止损保护了。", default="no", icon="warning"):
            return
        self.bm.cancel_rule(b)
        save(self.cfg)
        self.render_rules()
        self.refresh_orders()

    def cancel_all(self):
        items = self.otree.get_children()
        if not items:
            messagebox.showinfo("提示", "没有未成交的挂单")
            return
        if not messagebox.askyesno("全部撤销", f"撤销列表里全部 {len(items)} 张挂单？\n"
                                   "撤掉止损单后，对应持仓就没有止损保护了；相关自动规则会自动结束。",
                                   default="no", icon="warning"):
            return
        fails = []
        for iid in items:
            e, oid = iid.split("|", 1)
            try:
                self.desk.cancel(oid, e)
                self.log_order(f"撤单 {ENV_NAME[e]} {oid}（全部撤销）")
            except Exception as ex:
                fails.append(f"{oid}：{ex}")
        for b in self.cfg["brackets"]:  # 规则的单都撤了，规则也一起停
            if b["status"] == "active":
                b["status"] = "cancelled"
                self.log_order(f"规则 {b['id']} 随全部撤销一起停止")
        save(self.cfg)
        if fails:
            messagebox.showerror("部分撤单失败", "\n".join(fails))
        self.refresh_orders()
        self.render_rules()

    def cancel_order(self):
        sel = self.otree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选中一张挂单")
            return
        e, oid = sel[0].split("|", 1)
        vals = self.otree.item(sel[0])["values"]
        if not messagebox.askyesno("确认撤单", f"撤销 {ENV_NAME[e]} 的 {vals[2]} {vals[4]} 单（订单号 {oid}）？",
                                   default="no"):
            return
        try:
            self.desk.cancel(oid, e)
            self.log_order(f"撤单 {ENV_NAME[e]} {oid}")
        except Exception as ex:
            messagebox.showerror("撤单失败", str(ex))
        self.refresh_orders()

    # ---------- 行情 ----------
    def render(self, q, err):
        if err:
            self.status.set("获取失败：" + err[:40])
            return
        self.quotes = q
        rows = []
        for i, code in enumerate(self.cfg["stocks"]):
            d = q.get(code)
            stripe = ["odd"] if i % 2 else []
            src = ("指数" if code in INDICES else "持仓" if code in {p["code"] for p in self.positions} else
                   "锁定" if code in self.cfg["locked"] else "自动" if code in self.cfg["auto"] else "手动")
            sc = self.cfg["screen"].get(code, {}).get("score", "")
            if not d:
                rows.append((code, ("?", code, src, sc) + ("-",) * 9, stripe))
                continue
            hit = self.check_alert(code, d)
            tags = stripe + ["up" if d["chg"] > 0 else "down" if d["chg"] < 0 else ""] + (["hit"] if hit else [])
            rows.append((code, (
                d["name"], code, src, sc, d["price"], f"{d['pct']:+.2f}%", f"{d['chg']:+.2f}", d["open"],
                d["high"], d["low"], d["turnover"], self.alert_text(code), d["time"]), tags))
        self._sync(self.tree, rows)
        for p in self.positions:  # 不在自选里的持仓也要检查提醒
            if p["code"] not in self.cfg["stocks"] and p["code"] in q:
                self.check_alert(p["code"], q[p["code"]])
        self.render_positions()
        self.status.set(f"{datetime.now():%H:%M:%S} 已更新 · 每 {self.cfg['interval']} 秒刷新")
        self.save_close()

    # ---------- 提醒 ----------
    def alert_text(self, code):
        a = self.cfg["alerts"].get(code, {})
        parts = []
        if a.get("above"):
            parts.append(f"≥{a['above']}")
        if a.get("below"):
            parts.append(f"≤{a['below']}")
        if a.get("pct"):
            parts.append(f"±{a['pct']}%")
        return " ".join(parts)

    def check_alert(self, code, d):
        a = self.cfg["alerts"].get(code)
        if not a:
            return False
        today = datetime.now().strftime("%Y-%m-%d")
        conds = [("above", a.get("above") and d["price"] >= a["above"], f"涨到 {d['price']}（≥{a.get('above')}）"),
                 ("below", a.get("below") and d["price"] <= a["below"], f"跌到 {d['price']}（≤{a.get('below')}）"),
                 ("pct", a.get("pct") and abs(d["pct"]) >= a["pct"], f"涨跌幅 {d['pct']:+.2f}%（超过 ±{a.get('pct')}%）")]
        hit = False
        for kind, ok, msg in conds:
            if ok:
                hit = True
                if self.fired.get((code, kind)) != today:
                    self.fired[(code, kind)] = today
                    self.notify(f"{d['name']}({code}) {msg}")
        return hit

    def notify(self, msg):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        self.log.configure(state="normal")
        self.log.insert("1.0", line + "\n")
        self.log.configure(state="disabled")
        with open(os.path.join(DATA, "alerts.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d} {line}\n")
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            pass
        win = theme.toplevel(self.root, "到价提醒")  # 不阻塞的弹窗
        win.attributes("-topmost", True)
        ttk.Label(win, text="到价提醒", foreground=theme.ACCENT, font=(theme.FONT, 10, "bold"),
                  padding=(20, 14, 20, 0)).pack(anchor="w")
        ttk.Label(win, text=msg, padding=(20, 6, 20, 14), font=(theme.FONT, 12)).pack()
        ttk.Button(win, text="知道了", style="Accent.TButton", command=win.destroy).pack(pady=(0, 16))

    def set_alert(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选中一只股票（也可以直接双击）")
            return
        code = sel[0]
        a = self.cfg["alerts"].get(code, {})
        name = self.quotes.get(code, {}).get("name", code)
        def ask(title, key):
            v = simpledialog.askstring(name, f"{title}（留空=不提醒）", initialvalue=a.get(key) or "", parent=self.root)
            if v is None:
                raise KeyboardInterrupt  # 用户点了取消
            return float(v) if v.strip() else None
        try:
            new = {"above": ask("价格高于", "above"), "below": ask("价格低于", "below"),
                   "pct": ask("涨跌幅绝对值超过 %", "pct")}
        except KeyboardInterrupt:
            return
        except ValueError:
            messagebox.showwarning("提示", "请输入数字")
            return
        new = {k: v for k, v in new.items() if v}
        if new:
            self.cfg["alerts"][code] = new
        else:
            self.cfg["alerts"].pop(code, None)
        for k in ("above", "below", "pct"):
            self.fired.pop((code, k), None)
        save(self.cfg)

    # ---------- 自选股 ----------
    def add(self):
        text = self.entry.get()
        code = normalize("" if text == self._hint else text)
        if not code:
            messagebox.showwarning("提示", "请输入美股代码，例如 AAPL、NVDA、BRK.B；指数：DJI 道指、IXIC 纳指、INX 标普")
            return
        if code not in self.cfg["stocks"]:
            self.cfg["stocks"].append(code)
        if code in self.cfg["auto"]:  # 自动加的股票你再手动加一次 = 转成手动，以后不会被自动删除
            self.cfg["auto"].remove(code)
        self.cfg["ignore"].pop(code, None)
        save(self.cfg)
        self.entry.delete(0, "end")

    def remove(self):
        for code in self.tree.selection():
            if code in self.cfg["stocks"]:
                self.cfg["stocks"].remove(code)
            if code in self.cfg["auto"]:
                self.cfg["auto"].remove(code)
            if code in self.cfg["locked"]:
                self.cfg["locked"].remove(code)
            self.cfg["ignore"][code] = datetime.now().strftime("%Y-%m-%d")  # 你删掉的 7 天内不再自动加回
            self.cfg["alerts"].pop(code, None)
        save(self.cfg)

    def toggle_lock(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先选中一只股票")
            return
        code = sel[0]
        if code in self.cfg["locked"]:
            self.cfg["locked"].remove(code)
        else:
            self.cfg["locked"].append(code)
        save(self.cfg)
        self.show_reason()

    # ---------- 离场规则回测 ----------
    def start_backtest(self, force=False, done=None):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.backtesting or not (force or self.cfg["bt"].get("date") != today):
            return
        self.backtesting = True
        codes = list(dict.fromkeys([p["code"] for p in self.positions] +
                                   [c for c in self.cfg["stocks"] if c not in INDICES]))

        def work():
            try:
                q = fetch(codes)
                data = {}
                for c in codes:  # 一只一只拉，别把接口打爆
                    if c in q:
                        try:
                            data[c] = screener.kline(q[c]["symbol"], 500)
                        except Exception:
                            pass
                        time.sleep(0.2)
                res = backtest.compare(data)
                try:  # 复盘失败不影响回测结果
                    learn = self.learn(data)
                except Exception as e:
                    learn = None
                    self.root.after(0, self.notify_log, f"交易复盘失败：{str(e)[:80]}")
                self.root.after(0, finish, res, len(data), None, learn)
            except Exception as e:
                self.root.after(0, finish, None, 0, str(e), None)

        def finish(res, n, err, learn):
            self.backtesting = False
            if learn:
                self.apply_learning(learn)
            if err or not res:
                self.notify_log(f"离场规则回测失败：{err or '没有数据'}")
            else:
                k = backtest.best_trail_k(res)
                self.cfg["bt"] = {"date": today, "k": k, "stocks": n, "results": res}
                save(self.cfg)
                self.notify_log(f"离场规则回测完成（{n} 只股票合并、近 2 年）：跟踪止损最优倍数 {k} 个 ATR，已用于动态止损")
                self.render_positions()
            if done:
                done()
        threading.Thread(target=work, daemon=True).start()

    # ---------- 交易复盘 · 规则进化 ----------
    def learn(self, data):
        """后台线程：读真实成交 → 整理交易 → 用你的买入 + 候选池评估参数。data：代码 -> 日 K（500 根）。"""
        if not self.link.trd:
            return None
        deals = journal.fetch_deals(self.link)
        closed, open_lots = journal.build_trades(deals)
        rule_orders = {}
        for b in self.cfg["brackets"]:
            rule_orders[str(b["stop_id"])] = "规则止损"
            if b.get("target_id"):
                rule_orders[str(b["target_id"])] = "规则止盈"
        symbols = {c: d.get("symbol") for c, d in getattr(self, "quotes_live", self.quotes).items()}
        journal.enrich(closed, symbols, rule_orders)
        st = journal.stats(closed)
        entries = [(data[t["code"]], t["entry_time"][:10], t["entry"]) for t in closed if t["code"] in data]
        entries += [(data[c], lot["time"][:10], lot["price"]) for c, lots in open_lots.items() if c in data
                    for lot in lots]
        cur = {k: self.cfg["rule_params"][k] for k in ("loss_cap", "trail", "breakeven")}
        base, best, cands, w = tuner.suggest(cur, data, entries, len(closed))
        brief = lambda ev: ev and {"params": ev["params"], "avg": ev["pooled"]["avg"], "worst": ev["pooled"]["worst"],
                                   "mdd": ev["pooled"]["mdd"], "trades": ev["pooled"]["trades"],
                                   "user_avg": ev["user_avg"], "score": ev["score"]}
        return {"date": datetime.now().strftime("%Y-%m-%d"), "trades": closed, "stats": st,
                "insights": journal.insights(closed, st), "open": open_lots, "deals": len(deals),
                "base": brief(base), "best": brief(best), "weight": w, "entries": len(entries),
                "top": [brief(c) for c in cands[:5]]}

    def apply_learning(self, learn):
        self.cfg["journal"] = {k: learn[k] for k in ("date", "trades", "stats", "insights", "open", "deals",
                                                      "base", "weight", "entries", "top")}
        cur = {k: self.cfg["rule_params"][k] for k in ("loss_cap", "trail", "breakeven")}
        best = learn["best"]
        self.cfg["suggestion"] = best if best and best["params"] != cur else None
        save(self.cfg)
        if self.cfg["suggestion"]:
            p = self.cfg["suggestion"]["params"]
            self.notify_log(f"规则进化：发现更好的参数（限亏 {p['loss_cap']:g} / 跟踪 {p['trail']:g} / 保本 {p['breakeven']:g} ATR），"
                            "在「交易复盘 · 规则进化」里查看，点「采用」才会生效")

    def adopt_suggestion(self, win=None):
        sug = self.cfg["suggestion"]
        if not sug:
            return
        old = dict(self.cfg["rule_params"])
        if not messagebox.askyesno("采用新规则", f"把动态止损参数从\n  限亏 {old['loss_cap']:g} / 跟踪 {old['trail']:g} / 保本 {old['breakeven']:g} ATR\n"
                                   f"改为\n  限亏 {sug['params']['loss_cap']:g} / 跟踪 {sug['params']['trail']:g} / 保本 {sug['params']['breakeven']:g} ATR？\n\n"
                                   "已挂的止损单之后会按新规则自动上调（只升不降，不会放宽已挂的止损）。",
                                   parent=win, default="no"):
            return
        self.cfg["rule_history"].append({**old, "until": datetime.now().strftime("%Y-%m-%d %H:%M")})
        self.cfg["rule_params"] = {**sug["params"], "version": old["version"] + 1,
                                   "since": datetime.now().strftime("%Y-%m-%d %H:%M")}
        self.cfg["suggestion"] = None
        save(self.cfg)
        self.log_order(f"规则升级到 v{self.cfg['rule_params']['version']}：{sug['params']}（旧：{old}）")
        self.render_positions()
        if win:
            win.destroy()
            self.show_journal()

    def rollback_rule(self, win=None):
        if not self.cfg["rule_history"]:
            return
        prev = self.cfg["rule_history"][-1]
        if not messagebox.askyesno("退回上一版", f"退回到 v{prev['version']}（限亏 {prev['loss_cap']:g} / 跟踪 {prev['trail']:g} / "
                                   f"保本 {prev['breakeven']:g} ATR）？", parent=win, default="no"):
            return
        self.cfg["rule_history"].pop()
        self.cfg["rule_params"] = {k: prev[k] for k in ("loss_cap", "trail", "breakeven", "version", "since")}
        save(self.cfg)
        self.log_order(f"规则退回 v{prev['version']}")
        self.render_positions()
        if win:
            win.destroy()
            self.show_journal()

    def show_journal(self):
        j, rp = self.cfg["journal"], self.cfg["rule_params"]
        win = theme.toplevel(self.root, "交易复盘 · 规则进化")
        t = theme.text(tk.Text(win, width=96, height=30), 10)
        t.configure(padx=18, pady=14, spacing1=2, spacing3=2)
        t.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        t.tag_configure("h", foreground=theme.ACCENT, font=(theme.FONT, 11, "bold"))
        t.tag_configure("pos", foreground=theme.UP)
        t.tag_configure("neg", foreground=theme.DOWN)
        H = lambda x: t.insert("end", x + "\n", "h")
        L = lambda x, tag=None: t.insert("end", x + "\n", tag)
        H(f"当前规则 v{rp['version']}" + (f"（{rp['since']} 起）" if rp.get("since") else ""))
        L(f"· 相对成本最多亏 {rp['loss_cap']:g} ATR · 买入后最高点回撤 {rp['trail']:g} ATR 跟踪 · 浮盈 {rp['breakeven']:g} ATR 后保本")
        for h in reversed(self.cfg["rule_history"][-5:]):
            L(f"  历史 v{h['version']}：限亏 {h['loss_cap']:g} / 跟踪 {h['trail']:g} / 保本 {h['breakeven']:g}（用到 {h['until']}）")
        if not j:
            L("\n还没有复盘数据，点下面「立即分析」（大约 30~60 秒）。")
        else:
            st = j.get("stats")
            H(f"\n你的交易（{j['date']} 分析，共 {j['deals']} 条成交）")
            if st:
                L(f"· 已平仓 {st['n']} 笔 · 胜率 {st['win']:.0f}% · 累计盈亏 ${st['pnl']:+,.2f} · 平均盈利 {st['avg_win']:+.1f}% · "
                  f"平均亏损 {st['avg_loss']:+.1f}% · 最差一笔 {st['worst']:+.1f}% · 平均持有 {st['days']:.1f} 天")
                for tr in j["trades"][-10:]:
                    L(f"  {tr['entry_time'][:10]} → {tr['exit_time'][:10]} {tr['name']} {tr['qty']:g} 股 "
                      f"{tr['entry']:.2f} → {tr['exit']:.2f}  {tr['ret'] * 100:+.1f}%（{tr.get('how', '手动')}）",
                      "pos" if tr["pnl"] > 0 else "neg")
            for c, lots in j.get("open", {}).items():
                for lot in lots:
                    L(f"· 持仓中：{lot['name']} {lot['qty']:g} 股，{lot['time'][:10]} 以 {lot['price']:.2f} 买入"
                      "（动态止损从这天之后的最高点算）")
            H("\n复盘发现")
            for tip in j.get("insights", []):
                L("· " + tip)
            H("\n规则进化")
            b = j.get("base")
            L(f"· 评估样本：候选池回测 {b['trades'] if b else 0} 笔 + 你的真实买入 {j.get('entries', 0)} 次；"
              f"你的交易在评分中的权重 {j.get('weight', 0) * 100:.0f}%（已平仓 20 笔后为 100%）")
            if b:
                L(f"· 当前规则：平均每笔 {b['avg']:+.2f}% · 最差一笔 {b['worst']:+.1f}% · 最大回撤 {b['mdd']:.1f}%"
                  + (f" · 按你的买入复盘平均 {b['user_avg']:+.2f}%" if b.get("user_avg") is not None else ""))
            sug = self.cfg["suggestion"]
            if sug:
                p = sug["params"]
                L(f"· 建议新规则：限亏 {p['loss_cap']:g} / 跟踪 {p['trail']:g} / 保本 {p['breakeven']:g} ATR → "
                  f"平均每笔 {sug['avg']:+.2f}% · 最差一笔 {sug['worst']:+.1f}% · 最大回撤 {sug['mdd']:.1f}%", "pos")
                L("  （已通过护栏：收益更高、最差一笔不更差、回撤不更高、前后两段数据分开测都不更差）")
            else:
                L("· 目前没有找到在所有护栏下都更好的参数，继续用当前规则")
        t.configure(state="disabled")
        bf = ttk.Frame(win, padding=(10, 10))
        bf.pack()
        if self.cfg["suggestion"]:
            ttk.Button(bf, text="采用新规则", style="Accent.TButton",
                       command=lambda: self.adopt_suggestion(win)).pack(side="left", padx=4)
        if self.cfg["rule_history"]:
            ttk.Button(bf, text="退回上一版", command=lambda: self.rollback_rule(win)).pack(side="left", padx=4)
        btn = ttk.Button(bf, text="立即分析")
        btn.pack(side="left", padx=4)

        def rerun():
            btn.configure(text="分析中…", state="disabled")
            self.start_backtest(force=True, done=lambda: (win.destroy(), self.show_journal()))
        btn.configure(command=rerun)

    def show_backtest(self):
        win = theme.toplevel(self.root, "离场规则回测")
        bt = self.cfg["bt"]
        info = tk.StringVar()
        ttk.Label(win, textvariable=info, padding=(16, 12, 16, 4), wraplength=900, justify="left").pack(anchor="w")
        cols = ("name", "trades", "win", "avg", "total", "mdd", "pf", "days", "shaken")
        heads = ("离场规则", "交易笔数", "胜率", "平均每笔", "累计收益", "最大回撤", "盈亏比", "平均持有天", "止损后又涨回")
        tf = ttk.Frame(win, style="Card.TFrame")
        tf.pack(fill="both", expand=True, padx=16)
        t = self._table(tf, cols, heads, (260, 70, 60, 80, 80, 80, 60, 85, 100))
        t.configure(height=10)
        t.pack(fill="both", expand=True)
        ttk.Label(win, style="Sub.TLabel", padding=(16, 8), wraplength=900, justify="left", text=(
            "入场统一为：收盘站上 20 日线且 20 日上涨 → 次日开盘买入；含 0.05% 单边成本；同日既碰止损又碰止盈按止损算。"
            "「止损后又涨回」= 被止损后 5 天内价格又涨回离场价上方 1 个 ATR 的比例，越高说明止损越容易被正常波动震掉。"
            "历史表现不代表未来，股票越多、交易笔数越多，结论越可靠。")).pack(anchor="w")
        btn = ttk.Button(win, text="重新回测", style="Accent.TButton")
        btn.pack(pady=(0, 14))

        def fill():
            b = self.cfg["bt"]
            t.delete(*t.get_children())
            if not b.get("results"):
                info.set("还没有回测结果，点「重新回测」开始（大约 10~30 秒）")
                return
            info.set(f"{b['date']} · {b['stocks']} 只股票（持仓 + 自选）合并回测近 2 年日 K · "
                     f"跟踪止损最优倍数 {b['k']} 个 ATR（已用于动态止损和挂单默认值）")
            for i, r in enumerate(b["results"]):
                tags = (["odd"] if i % 2 else []) + (["up"] if r["avg"] > 0 else ["down"])
                pf = "∞" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
                t.insert("", "end", tags=tags, values=(
                    r["name"], r["trades"], f"{r['win']:.0f}%", f"{r['avg']:+.2f}%", f"{r['total']:+.1f}%",
                    f"{r['mdd']:.1f}%", pf, f"{r['days']:.1f}", f"{r['shaken']:.0f}%"))

        def rerun():
            btn.configure(text="回测中…", state="disabled")
            info.set("正在拉取历史数据并回测…")
            self.start_backtest(force=True, done=lambda: (btn.configure(text="重新回测", state="normal"), fill()))
        btn.configure(command=rerun)
        fill()

    # ---------- 每日自动选股 ----------
    def toggle_auto(self):
        self.cfg["auto_screen"] = self.auto_var.get()
        save(self.cfg)
        if self.cfg["auto_screen"]:
            self.start_screen()

    def start_screen(self, force=False):
        today = datetime.now().strftime("%Y-%m-%d")
        if self.screening or not (force or (self.cfg["auto_screen"] and self.cfg["screen_date"] != today)):
            return
        self.screening = True
        self.screen_btn.configure(text="选股中…", state="disabled")
        threading.Thread(target=self._screen_worker, daemon=True).start()

    def _screen_worker(self):
        try:
            # 候选池 + 你自选里的其他股票一起打分，这样手动加的也能按同一标准清理
            pool = dict.fromkeys(["us" + c for c in screener.UNIVERSE] +
                                 [c for c in self.cfg["stocks"] if c not in INDICES])
            q = fetch(list(pool))
            ranked = screener.run({c: d["symbol"] for c, d in q.items()})
            for x in ranked:
                x["name"] = q.get(x["code"], {}).get("name", x["code"])
            self.root.after(0, self._apply_screen, ranked, None)
        except Exception as e:
            self.root.after(0, self._apply_screen, None, str(e))

    def _apply_screen(self, ranked, err):
        self.screening = False
        self.screen_btn.configure(text="立即选股", state="normal")
        if err or not ranked:
            self.notify_log(f"自动选股失败：{err or '没拿到数据'}")
            return
        today = datetime.now()
        cutoff = (today.toordinal() - 7)
        self.cfg["ignore"] = {c: d for c, d in self.cfg["ignore"].items()
                              if datetime.strptime(d, "%Y-%m-%d").toordinal() > cutoff}
        # 永远不清除：指数、已经买入的持仓、你锁定的
        protected = set(INDICES) | {p["code"] for p in self.positions} | set(self.cfg["locked"])
        add, remove = screener.plan_changes(ranked, self.cfg["stocks"], protected, set(self.cfg["ignore"]))
        by_code = {x["code"]: x for x in ranked}
        for c in remove:
            if c in self.cfg["auto"]:
                self.cfg["auto"].remove(c)
            if c in self.cfg["stocks"]:
                self.cfg["stocks"].remove(c)
            self.cfg["alerts"].pop(c, None)
        for c in add:
            self.cfg["auto"].append(c)
            if c not in self.cfg["stocks"]:
                self.cfg["stocks"].append(c)
        self.cfg["screen"] = {x["code"]: {"score": x["score"], "rank": x["rank"], "reason": x["reason"]}
                              for x in ranked}
        self.cfg["screen_date"] = today.strftime("%Y-%m-%d")
        save(self.cfg)

        path = os.path.join(DATA, f"screen_{today:%Y-%m-%d}.csv")
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("排名,代码,名称,评分,符合条件,理由,本次操作\n")
            for x in ranked:
                act = "加入" if x["code"] in add else ""
                f.write(f"{x['rank']},{x['code']},{x['name']},{x['score']},{'是' if x['eligible'] else '否'},"
                        f"\"{x['reason']}\",{act}\n")
            for c, why in remove.items():
                f.write(f",{c},{by_code.get(c, {}).get('name', '')},,,\"{why}\",清除\n")
        name = lambda c: by_code.get(c, {}).get("name", c)
        msg = f"今日选股（{len(ranked)} 只参与）：新增 {'、'.join(name(c) for c in add) or '无'}"
        msg += f"；清除 {'、'.join(f'{name(c)}（{why}）' for c, why in remove.items()) or '无'}"
        self.notify_log(msg)

    def notify_log(self, msg):
        """只写进下方记录框，不弹窗、不响铃。"""
        self.log.configure(state="normal")
        self.log.insert("1.0", f"[{datetime.now():%H:%M:%S}] {msg}\n")
        self.log.configure(state="disabled")

    def show_reason(self):
        sel = self.tree.selection()
        if not sel:
            return
        code = sel[0]
        s = self.cfg["screen"].get(code)
        tag = ("（已锁定，不会被清除）" if code in self.cfg["locked"] else
               "（持仓，不会被清除）" if code in {p["code"] for p in self.positions} else
               "（指数）" if code in INDICES else "")
        if s:
            self.reason_var.set(f"{code}{tag} 评分 {s['score']} · 第 {s['rank']} 名 · {s['reason']}  —— 只看走势和成交数据，不代表推荐")
        else:
            self.reason_var.set(f"{code}{tag} 还没有评分（下次选股时会算）")

    def toggle_mini(self):
        self.mini = not self.mini
        show = ("name", "price", "pct") if self.mini else COLS
        self.tree.configure(displaycolumns=show)
        self.nb.select(0)
        if self.mini:
            for w in (self.topbar, self.log, self.statusbar):
                w.pack_forget()
            self.minibar = ttk.Frame(self.root, padding=(10, 6))
            self.minibar.pack(fill="x", before=self.nb)
            ttk.Label(self.minibar, text="美股盯盘", style="Title.TLabel", font=(theme.FONT, 11, "bold")).pack(side="left")
            ttk.Button(self.minibar, text="还原", command=self.toggle_mini).pack(side="right")
            self.nb.configure(style="Mini.TNotebook")
            ttk.Style().layout("Mini.TNotebook.Tab", [])  # 迷你模式隐藏标签页
            self.root.minsize(260, 200)
            self.root.geometry("320x300")
            self.top_var.set(True)
            self.root.attributes("-topmost", True)
        else:
            self.minibar.destroy()
            self.nb.configure(style="TNotebook")
            self.topbar.pack(fill="x", before=self.nb)
            self.log.pack(fill="x", padx=14, pady=(8, 0))
            self.statusbar.pack(fill="x")
            self.root.minsize(900, 480)
            self.root.geometry("1180x640")

    # ---------- 收盘后存一份文字行情（按美东交易日） ----------
    def save_close(self):
        # 行情时间是美东时间，所有个股都停在 16:00 之后就算收盘（指数时间不准，不参与判断）
        stamps = [d["time"] for c, d in self.quotes.items() if c not in ("usDJI", "usIXIC", "usINX")]
        if not stamps or any(len(t) < 16 or t[11:16] < "16:00" for t in stamps):
            return
        path = os.path.join(DATA, f"{max(stamps)[:10]}.csv")
        if os.path.exists(path):
            return
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("代码,名称,收盘,涨跌幅%,开盘,最高,最低,换手%\n")
            for code, d in self.quotes.items():
                f.write(f"{code},{d['name']},{d['price']},{d['pct']},{d['open']},{d['high']},{d['low']},{d['turnover']}\n")


def single_instance():
    """同一时间只允许开一个盯盘程序：多开会让联动规则被执行多次。返回占位 socket，未拿到则返回 None。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47391))
        s.listen(1)
        return s
    except OSError:
        s.close()
        return None


def show_window(root):
    root.deiconify()
    root.state("normal")
    root.lift()
    root.attributes("-topmost", True)
    root.after(300, lambda: root.attributes("-topmost", False))
    root.focus_force()


def listen_for_show(lock, root):
    """已经在运行时，再次打开程序会连到这里：把现有窗口调到最前面，而不是再开一个。"""
    def run():
        while True:
            try:
                conn, _ = lock.accept()
                conn.close()
                root.after(0, show_window, root)
            except OSError:
                return
    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    lock = single_instance()
    if lock is None:
        import socket
        try:  # 通知正在运行的那个把窗口调出来
            socket.create_connection(("127.0.0.1", 47391), timeout=2).close()
        except OSError:
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo("美股盯盘", "盯盘程序已经在运行了（看看任务栏），不用重复打开。")
        raise SystemExit
    root = tk.Tk()
    App(root)
    listen_for_show(lock, root)
    root.mainloop()
