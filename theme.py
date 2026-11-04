"""深色交易软件风格：深灰底、橙色强调、绿涨红跌（与 moomoo 美股默认配色一致）。"""
import tkinter as tk
from tkinter import ttk

BG = "#121316"       # 窗口底色
PANEL = "#1b1c21"    # 表格 / 卡片
PANEL2 = "#24262c"   # 按钮、输入框、表头
STRIPE = "#1f2026"   # 隔行
BORDER = "#2e3038"
TEXT = "#e8e9ec"
SUB = "#8b8e97"
ACCENT = "#ff6a00"
UP = "#22c38e"
DOWN = "#f2495c"
SELECT = "#2c3a52"
RISK_HIGH = "#3d1d24"
RISK_MID = "#3a3219"
FONT = "Microsoft YaHei UI"


def apply(root):
    root.configure(bg=BG)
    dark_titlebar(root)
    s = ttk.Style(root)
    s.theme_use("clam")
    s.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL2, bordercolor=BORDER,
                lightcolor=BORDER, darkcolor=BORDER, troughcolor=PANEL, font=(FONT, 10),
                selectbackground=SELECT, selectforeground=TEXT, insertcolor=TEXT)
    s.configure("TFrame", background=BG)
    s.configure("Card.TFrame", background=PANEL)
    s.configure("TLabel", background=BG, foreground=TEXT)
    s.configure("Sub.TLabel", background=BG, foreground=SUB, font=(FONT, 9))
    s.configure("Title.TLabel", background=BG, foreground=ACCENT, font=(FONT, 14, "bold"))
    s.configure("CardSub.TLabel", background=PANEL, foreground=SUB, font=(FONT, 9))
    s.configure("CardBig.TLabel", background=PANEL, foreground=TEXT, font=(FONT, 15, "bold"))
    s.configure("Warn.TLabel", background=BG, foreground="#f0a24a", font=(FONT, 9))

    s.configure("TButton", background=PANEL2, foreground=TEXT, borderwidth=0, focusthickness=0,
                padding=(12, 5), relief="flat")
    s.map("TButton", background=[("active", BORDER), ("pressed", BORDER)])
    s.configure("Accent.TButton", background=ACCENT, foreground="white", font=(FONT, 10, "bold"))
    s.map("Accent.TButton", background=[("active", "#ff8533"), ("pressed", "#e05e00")])

    for w in ("TCheckbutton", "TRadiobutton"):
        s.configure(w, background=BG, foreground=TEXT, indicatorbackground=PANEL2, indicatorforeground=ACCENT)
        s.map(w, background=[("active", BG)], indicatorbackground=[("selected", PANEL2)])
    s.configure("Card.TCheckbutton", background=PANEL)
    s.map("Card.TCheckbutton", background=[("active", PANEL)])
    s.configure("TEntry", fieldbackground=PANEL2, foreground=TEXT, padding=5, borderwidth=0)
    s.configure("TCombobox", fieldbackground=PANEL2, background=PANEL2, foreground=TEXT, arrowcolor=SUB,
                padding=4, borderwidth=0)
    s.map("TCombobox", fieldbackground=[("readonly", PANEL2)], foreground=[("readonly", TEXT)],
          selectbackground=[("readonly", PANEL2)], selectforeground=[("readonly", TEXT)])
    root.option_add("*TCombobox*Listbox.background", PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0),
                bordercolor=BG, lightcolor=BG, darkcolor=BG)
    s.configure("TNotebook.Tab", background=BG, foreground=SUB, padding=(18, 8), borderwidth=0, font=(FONT, 10),
                bordercolor=BG, lightcolor=BG, darkcolor=BG, focuscolor=BG)
    s.map("TNotebook.Tab", background=[("selected", PANEL), ("active", PANEL2)],
          foreground=[("selected", ACCENT), ("active", TEXT)],
          lightcolor=[("selected", ACCENT)],  # 选中的标签顶部一条橙色线
          font=[("selected", (FONT, 10, "bold"))], expand=[("selected", (0, 0, 0, 0))])
    s.configure("TButton", focuscolor=PANEL2)
    s.configure("Accent.TButton", focuscolor=ACCENT)

    s.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT, rowheight=30,
                borderwidth=0, font=(FONT, 10))
    s.map("Treeview", background=[("selected", SELECT)], foreground=[("selected", TEXT)])
    s.configure("Treeview.Heading", background=PANEL2, foreground=SUB, relief="flat", borderwidth=0,
                padding=(4, 6), font=(FONT, 9))
    s.map("Treeview.Heading", background=[("active", BORDER)])
    s.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # 去掉表格外框
    s.configure("Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL, arrowcolor=SUB, borderwidth=0)


def text(widget, size=10):
    """把 tk.Text 调成深色。"""
    widget.configure(bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat", borderwidth=0,
                     highlightthickness=0, font=(FONT, size), padx=10, pady=8,
                     selectbackground=SELECT, selectforeground=TEXT)
    return widget


def dark_titlebar(win):
    """Windows 10/11 深色标题栏；其他系统或旧版本直接忽略。"""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE（新旧两个编号）
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def toplevel(root, title):
    win = tk.Toplevel(root, bg=BG)
    win.title(title)
    dark_titlebar(win)
    return win


def card(parent, title, var):
    """总资产之类的小卡片：灰色标题 + 大号数值。"""
    f = ttk.Frame(parent, style="Card.TFrame", padding=(14, 8))
    ttk.Label(f, text=title, style="CardSub.TLabel").pack(anchor="w")
    lbl = ttk.Label(f, textvariable=var, style="CardBig.TLabel")
    lbl.pack(anchor="w")
    return f, lbl
