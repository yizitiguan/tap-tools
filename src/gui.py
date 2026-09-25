"""连点助手 -- visual front-end for the adb double-tap scheduler.

Four steps down the left: 设备 -> 坐标 -> 标定 -> 发射.
All device work runs on a worker thread; Tk is only ever touched from the
main thread via a queue, and the countdown is pushed by the worker so there
is no repaint timer competing with the critical write().
"""
import json
import os
import queue
import statistics as st
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
from datetime import datetime, timedelta
from tkinter import simpledialog, ttk, messagebox

import adbutil
import fire
import theme

SCALE = 3


CFG_PATH = adbutil.pkgdir() / "config.json"


def load_cfg():
    try:
        return json.loads(CFG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"tap_a": None, "tap_b": None, "mode": "sequence", "gap_ms": 40,
                "gap_jitter_ms": 20,
                "lead_ms": 72, "server_bias_ms": 0, "stage": True,
                "warm": {"enabled": True, "before_ms": 2500,
                         "cmd": "input keyevent 0 & input keyevent 0 & wait"},
                "phone_minus_pc_ms": 0, "calibration": {}}


def save_cfg(cfg):
    CFG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def hms(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000.0).strftime("%H:%M:%S.%f")[:-3]


def _fmt_cd(ms):
    """Countdown text. Switches to millisecond precision inside 10s, because
    that is the window where you are deciding whether to intervene."""
    s = max(0.0, ms) / 1000.0
    if s >= 3600:
        return f"T-{int(s)//3600}:{int(s)%3600//60:02d}:{int(s)%60:02d}"
    if s >= 10:
        return f"T-{int(s)//60:02d}:{int(s)%60:02d}"
    return f"T-{s:06.3f}"


def _dur(ms):
    if ms < 0:
        return "已过"
    s = ms / 1000.0
    if s < 90:
        return f"{s:.1f}秒"
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}小时{m}分{sec}秒" if h else f"{m}分{sec}秒"


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("连点助手  ·  Android 双点准时发射")
        # the phone screenshot is 1080x2400, so the 坐标 page needs real height
        self.geometry("1280x920")
        self.minsize(1040, 720)
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.busy = False
        self.cancel = threading.Event()
        self.pts = []
        self._armed = False
        self._t0 = None
        self._notice_job = None
        self._logfh = None
        self._logpath = None
        self._follow = True
        self._behind = 0
        theme.apply(self)
        # A Tk callback exception would otherwise surface as a modal (or, in
        # the --windowed build where stdout is None, as nothing at all).
        self.report_callback_exception = self._on_callback_error
        self._build()
        self.after(80, self._pump)
        self.log(f"adb: {adbutil.ADB}")
        self.log(f"config: {CFG_PATH}")
        self.refresh_device()

    # ---------- layout ----------
    def _build(self):
        self.columnconfigure(1, weight=1)
        self.rowconfigure(2, weight=1)

        # -- status rail: the countdown must survive tab switches -------------
        # Buried in tab 4 before, so clicking 坐标 to re-check a point hid the
        # one number you are actually waiting on.
        rail = tk.Frame(self, bg=theme.BG)
        rail.grid(row=0, column=0, columnspan=2, sticky="ew")
        rail.columnconfigure(1, weight=1)

        brand = tk.Frame(rail, bg=theme.BG)
        brand.grid(row=0, column=0, sticky="nw", padx=(theme.L, theme.L),
                   pady=(theme.M, 0))
        tk.Label(brand, text="连点助手", bg=theme.BG, fg=theme.TEXT,
                 font=theme.ui(12, "bold")).pack(anchor="w")
        self.rail_dev = tk.Label(brand, text="设备未连接", bg=theme.BG,
                                fg=theme.BAD, font=theme.ui(9))
        self.rail_dev.pack(anchor="w")
        self.rail_preset = tk.Label(brand, text="未设置坐标", bg=theme.BG,
                                    fg=theme.FAINT, font=theme.mono(9))
        self.rail_preset.pack(anchor="w", pady=(2, 0))

        clock = tk.Frame(rail, bg=theme.BG)
        clock.grid(row=0, column=1, sticky="n")
        self.big = tk.Label(clock, text="T- --:--", bg=theme.BG, fg=theme.FAINT,
                            font=theme.mono(44, "bold"))
        self.big.pack()
        self.rail_next = tk.Label(clock, text="尚未预约", bg=theme.BG,
                                  fg=theme.DIM, font=theme.mono(10))
        self.rail_next.pack(pady=(0, theme.S))

        side_ctl = tk.Frame(rail, bg=theme.BG)
        side_ctl.grid(row=0, column=2, sticky="ne", padx=(theme.L, theme.L))
        self.state_lbl = tk.Label(side_ctl, text="待命", bg=theme.LINE,
                                  fg=theme.TEXT, font=theme.ui(10, "bold"),
                                  padx=theme.L, pady=theme.S)
        self.state_lbl.pack(anchor="e")
        self.rail_mode = tk.Label(side_ctl, text="", bg=theme.BG, fg=theme.FAINT,
                                  font=theme.mono(9))
        self.rail_mode.pack(anchor="e", pady=(theme.S, 0))
        # Replaces the "you clicked while busy" and "task failed" dialogs. A
        # modal runs a nested Tk loop and hides the countdown, which is exactly
        # what you cannot afford during a watch.
        self.notice = tk.Label(rail, text="", bg=theme.BG, fg=theme.DIM,
                               font=theme.ui(9), anchor="w", justify="left")
        self.notice.grid(row=1, column=0, columnspan=3, sticky="ew",
                         padx=(theme.L, theme.L))
        theme.rule(rail).grid(row=2, column=0, columnspan=3, sticky="ew",
                              pady=(theme.XS, 0))

        # -- tabs -------------------------------------------------------------
        nb = ttk.Notebook(self)
        nb.grid(row=2, column=0, sticky="nsew",
                padx=(theme.L, theme.S), pady=(theme.S, theme.L))
        self.tab_dev = ttk.Frame(nb); self.tab_pos = ttk.Frame(nb)
        self.tab_cal = ttk.Frame(nb); self.tab_fire = ttk.Frame(nb)
        for t, name in ((self.tab_dev, "1 设备"), (self.tab_pos, "2 坐标"),
                        (self.tab_cal, "3 标定"), (self.tab_fire, "4 发射")):
            nb.add(t, text=name)
        self.nb = nb

        # -- log --------------------------------------------------------------
        side = tk.Frame(self, bg=theme.SURFACE)
        side.grid(row=2, column=1, sticky="nsew",
                  padx=(theme.S, theme.L), pady=(theme.S, theme.L))
        side.rowconfigure(1, weight=1)
        side.columnconfigure(0, weight=1)
        head = tk.Frame(side, bg=theme.SURFACE)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, theme.S))
        tk.Label(head, text="运行日志", bg=theme.SURFACE, fg=theme.DIM,
                 font=theme.ui(9, "bold")).pack(side="left")
        # Clickable, and it is the only honest way to say "new lines arrived
        # while you were reading history" without yanking the viewport.
        self.newlines_lbl = tk.Label(head, text="", bg=theme.SURFACE,
                                     fg=theme.FAINT, font=theme.ui(9),
                                     cursor="hand2")
        self.newlines_lbl.pack(side="right")
        self.newlines_lbl.bind("<Button-1>", lambda e: self._jump_to_end())

        # Left permanently selectable. `state="disabled"` used to make the whole
        # panel uncopyable, which is the opposite of what a log is for; typing
        # is blocked per-event instead.
        self.txt = theme.text_widget(side, width=56, height=10,
                                     font=theme.mono(9), wrap="word")
        self.txt.grid(row=1, column=0, sticky="nsew")
        self.txt.bind("<Key>", lambda e: "break")
        for seq in ("<<Paste>>", "<Control-v>", "<Control-V>", "<Control-y>",
                    "<Button-2>"):
            self.txt.bind(seq, lambda e: "break")
        sb = ttk.Scrollbar(side, orient="vertical", style="Vertical.TScrollbar",
                           command=self.txt.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.txt.config(yscrollcommand=self._scroll_follow)
        # bound per-widget, never bind_all: that would steal wheel events from
        # the 坐标 canvas and from every Combobox dropdown
        self.txt.bind("<MouseWheel>", self._wheel)
        sb.bind("<MouseWheel>", self._wheel)
        self.txt.bind("<Button-4>", lambda e: self.txt.yview_scroll(-3, "units"))
        self.txt.bind("<Button-5>", lambda e: self.txt.yview_scroll(3, "units"))
        for tag, col in (("ok", theme.OK), ("bad", theme.BAD),
                         ("warn", theme.AMBER), ("dim", theme.FAINT)):
            self.txt.tag_configure(tag, foreground=col)
        bar = tk.Frame(side, bg=theme.SURFACE)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(theme.S, 0))
        ttk.Button(bar, text="清空", style="Ghost.TButton",
                   command=lambda: (self.txt.delete("1.0", "end"),
                                    self._jump_to_end())).pack(side="left")
        ttk.Button(bar, text="打开日志文件", style="Ghost.TButton",
                   command=self._open_log).pack(side="left", padx=(theme.S, 0))

        self._build_dev(); self._build_pos(); self._build_cal(); self._build_fire()
        self._sync_rail()
        # opening 坐标 with a stale or blank canvas is the most common way to
        # pick coordinates for the wrong screen, so grab on first view
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)
        for arg in sys.argv[1:]:
            if arg.isdigit() and 1 <= int(arg) <= 4:
                self.nb.select(self.nb.tabs()[int(arg) - 1])

    def _open_log(self):
        p = adbutil.pkgdir() / "doubletap.log"
        if p.exists():
            os.startfile(str(p))
        else:
            self.log("还没有日志文件")

    def _sync_rail(self):
        """Keep the always-visible summary honest: device, active preset, mode."""
        a, b = self.cfg.get("tap_a"), self.cfg.get("tap_b")
        if a and b:
            self.rail_preset.config(
                text=f"{self.cfg.get('active_preset') or '未命名'}   "
                     f"A({a['x']},{a['y']})  B({b['x']},{b['y']})",
                fg=theme.DIM)
        else:
            self.rail_preset.config(text="未设置坐标", fg=theme.BAD)
        m = self.v_mode.get()
        self.rail_mode.config(
            text=f"{m}   lead {fire.auto_lead(self.cfg, m):.0f}ms   "
                 f"gap {self.v_gap.get()}+{self.v_jit.get()}ms")

    def _tab_changed(self, _e=None):
        # grab once so the page is never blank; after that the user decides,
        # otherwise every tab switch costs a ~1.3s screencap
        try:
            if (self.nb.index("current") == self.nb.index(self.tab_pos)
                    and not getattr(self, "_grabbed_once", False)):
                self._grabbed_once = True
                self.after(50, self.grab)
        except Exception:
            pass

    def _fit_scale(self, h):
        """Pick the largest 1/N that still shows the WHOLE phone screen.

        A cropped bottom is not a cosmetic bug here: purchase buttons live at
        the bottom, and clicking a hidden region is exactly how you mis-aim.
        """
        budget = max(320, self.winfo_height() - 210)
        return min(6, max(1, -(-h // budget)))

    def _build_dev(self):
        f = self.tab_dev
        head = ttk.Frame(f); head.pack(fill="x", padx=theme.L, pady=(theme.L, theme.S))
        ttk.Label(head, text="连接状态", style="Section.TLabel").pack(side="left")
        ttk.Button(head, text="刷新", command=self.refresh_device).pack(side="right")

        card = self._card(f)
        self.dev_info = tk.Label(card, justify="left", anchor="w",
                                 bg=theme.RAISED, fg=theme.TEXT,
                                 font=theme.mono(11), padx=theme.L, pady=theme.M)
        self.dev_info.pack(fill="x")
        theme.rule(card).pack(fill="x")
        row = ttk.Frame(card, style="Card.TFrame"); row.pack(fill="x",
                                                            padx=theme.L, pady=theme.M)
        ttk.Button(row, text="音量键注入检测", command=self.check_inject).pack(side="left")
        self.v_showtaps = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="在屏幕上显示点按位置", style="Card.TCheckbutton",
                        variable=self.v_showtaps,
                        command=lambda: self._toggle_taps(self.v_showtaps.get())
                        ).pack(side="left", padx=theme.L)

        note = self._callout(f, theme.AMBER, (
            "若点击无反应：手机「开发者选项」里的「USB 调试（安全设置）」必须打开，\n"
            "否则 input tap 会假装成功。澎湃 OS 在重启或升级后会自动关掉它。\n"
            "注意：音量键检测在设置一类的非媒体页面会误报，不代表注入失败。"))

    def _card(self, parent):
        c = tk.Frame(parent, bg=theme.RAISED, highlightbackground=theme.LINE_SOFT,
                     highlightcolor=theme.LINE_SOFT, highlightthickness=1, bd=0)
        c.pack(fill="x", padx=theme.L, pady=(0, theme.M))
        return c

    def _callout(self, parent, color, text):
        box = tk.Frame(parent, bg=theme.SURFACE)
        box.pack(fill="x", padx=theme.L, pady=(theme.S, theme.L))
        tk.Frame(box, bg=color, width=1).pack(side="left", fill="y")
        tk.Label(box, text=text, justify="left", anchor="w", bg=theme.SURFACE,
                 fg=theme.DIM, font=theme.ui(9), padx=theme.M, pady=theme.S
                 ).pack(side="left", fill="x")
        return box

    def _build_pos(self):
        f = self.tab_pos
        bar = ttk.Frame(f); bar.pack(fill="x", padx=theme.L, pady=(theme.L, theme.S))

        ttk.Label(bar, text="预设").pack(side="left")
        self.v_preset = tk.StringVar(value=self.cfg.get("active_preset", ""))
        self.cb_preset = ttk.Combobox(bar, textvariable=self.v_preset, width=14,
                                      values=sorted(self.cfg.get("presets", {})))
        self.cb_preset.pack(side="left", padx=(theme.S, 0))
        # ttk.Combobox takes an event binding, not command= (see _build_fire)
        self.cb_preset.bind("<<ComboboxSelected>>", lambda e: self._apply_preset())
        ttk.Button(bar, text="另存为新预设", command=self._save_preset_as
                   ).pack(side="left", padx=theme.S)
        ttk.Button(bar, text="删除", style="Ghost.TButton",
                   command=self._del_preset).pack(side="left")

        right = ttk.Frame(bar, style="Panel.TFrame")
        right.pack(side="right")
        ttk.Label(right, text="比例 1/").pack(side="left")
        self.v_scale = tk.IntVar(value=SCALE)
        self.v_auto = tk.BooleanVar(value=True)
        ttk.Spinbox(right, from_=1, to=6, textvariable=self.v_scale, width=3,
                    command=self._manual_scale).pack(side="left")
        ttk.Checkbutton(right, text="自动", variable=self.v_auto,
                        command=self.grab).pack(side="left", padx=(theme.S, 0))
        ttk.Button(right, text="重新截图", command=self.grab).pack(side="left",
                                                                  padx=theme.S)
        ttk.Button(right, text="保存坐标", style="Primary.TButton",
                   command=self.save_points).pack(side="left")

        self.coord_lbl = tk.Label(f, anchor="w", bg=theme.SURFACE, fg=theme.DIM,
                                  font=theme.mono(11), padx=theme.L)
        self.coord_lbl.pack(fill="x")
        seed = self._pts_from_cfg()
        self.coord_lbl.config(text="尚未取样" if not seed else "  ".join(
            f"#{i+1} = ({p[0]}, {p[1]})" for i, p in enumerate(seed)) + "   （已保存）")

        self.canvas = theme.canvas(f)
        self.canvas.pack(fill="both", expand=True, padx=theme.L, pady=theme.S)
        self.canvas.bind("<Button-1>", self.on_canvas)
        for key, dx, dy in (("<Up>", 0, -1), ("<Down>", 0, 1),
                            ("<Left>", -1, 0), ("<Right>", 1, 0)):
            self.bind(key, lambda e, x=dx, y=dy: self._nudge(x, y, 1))
            self.bind(key.replace("<", "<Shift-"), lambda e, x=dx, y=dy: self._nudge(x, y, 10))
        self._callout(f, theme.INFO,
                      "在图上依次点两个按钮 → 方向键微调 1px（Shift 为 10px）→ 保存坐标 → 另存为预设。"
                      "坐标属于当前这个界面，换页面必须重新取。")

    def _build_cal(self):
        f = self.tab_cal
        head = ttk.Frame(f); head.pack(fill="x", padx=theme.L, pady=(theme.L, theme.S))
        ttk.Label(head, text="标定链路耗时", style="Section.TLabel").pack(side="left")

        bar = ttk.Frame(f); bar.pack(fill="x", padx=theme.L)
        ttk.Label(bar, text="排练次数").pack(side="left")
        self.v_reps = tk.IntVar(value=8)
        ttk.Spinbox(bar, from_=3, to=30, textvariable=self.v_reps, width=4
                    ).pack(side="left", padx=theme.S)
        ttk.Button(bar, text="测本机时钟偏差", command=self.do_ntp).pack(side="left",
                                                                        padx=theme.S)
        ttk.Button(bar, text="开始排练", style="Primary.TButton",
                   command=self.do_rehearse).pack(side="left")

        card = self._card(f)
        self.cal_lbl = tk.Label(card, justify="left", anchor="w",
                                bg=theme.RAISED, fg=theme.TEXT,
                                font=theme.mono(11), padx=theme.L, pady=theme.M,
                                text="尚未排练。排练结果会显示在这里。")
        self.cal_lbl.pack(fill="x")
        self._callout(f, theme.INFO,
                      "排练用无害的空键码代替点击，走完全相同的时序路径，所以能在不点歪的前提下"
                      "把 lead_ms 标定准。\n每次临出发前重测一次：链路耗时会随温度和负载漂移几十毫秒。")

    def _build_fire(self):
        f = self.tab_fire
        f.columnconfigure(0, weight=3)
        f.columnconfigure(1, weight=2)

        # -- schedule ---------------------------------------------------------
        left = ttk.Frame(f, style="Panel.TFrame")
        left.grid(row=0, column=0, sticky="nsew", padx=(theme.L, theme.S),
                  pady=theme.L)
        head = ttk.Frame(left, style="Panel.TFrame"); head.pack(fill="x")
        ttk.Label(head, text="目标时刻", style="Section.TLabel").pack(side="left")
        ttk.Label(head, text="每行一个，到点依次自动开火", style="Dim.TLabel"
                  ).pack(side="left", padx=(theme.M, 0), pady=(3, 0))

        row = ttk.Frame(left, style="Panel.TFrame"); row.pack(fill="x",
                                                                  pady=(theme.S, 0))
        self.txt_times = theme.text_widget(row, width=19, height=4,
                                           font=theme.mono(14), padx=theme.M,
                                           pady=theme.S, spacing1=3, spacing3=3)
        self.txt_times.pack(side="left")
        saved = self.cfg.get("target_times") or ["10:00:00.000"]
        self.txt_times.insert("1.0", "\n".join(saved))

        chips = ttk.Frame(row, style="Panel.TFrame"); self._chips = chips
        chips.pack(side="left", padx=(theme.M, 0), anchor="n")
        for label, secs in (("+30 秒", 30), ("+1 分", 60), ("+5 分", 300)):
            theme.chip(chips, label, lambda s=secs: self._quick_set(s)
                       ).pack(anchor="w", pady=(0, theme.S))
        theme.chip(chips, "清空", lambda: self.txt_times.delete("1.0", "end")
                   ).pack(anchor="w")

        prev = tk.Frame(left, bg=theme.RAISED,
                        highlightbackground=theme.LINE_SOFT,
                        highlightcolor=theme.LINE_SOFT, highlightthickness=1, bd=0)
        prev.pack(fill="x", pady=(theme.M, 0))
        self.v_preview = tk.StringVar(value="")
        self._prev_lbl = tk.Label(prev, textvariable=self.v_preview, justify="left",
                                  anchor="w", bg=theme.RAISED, fg=theme.INFO,
                                  font=theme.mono(11), padx=theme.L, pady=theme.M)
        self._prev_lbl.pack(fill="x")
        self._update_preview()

        # -- parameters -------------------------------------------------------
        right = ttk.Frame(f, style="Panel.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=(theme.S, theme.L),
                   pady=theme.L)
        ttk.Label(right, text="参数", style="Section.TLabel").pack(anchor="w")
        self.v_mode = tk.StringVar(value=self.cfg.get("mode", "sequence"))
        self.v_gap = tk.IntVar(value=self.cfg.get("gap_ms", 40))
        self.v_jit = tk.IntVar(value=self.cfg.get("gap_jitter_ms", 20))
        self.v_lead = tk.DoubleVar(value=fire.auto_lead(self.cfg))
        self._lead_mode = self.v_mode.get()
        self.v_bias = tk.DoubleVar(value=self.cfg.get("server_bias_ms", 0))
        self.v_ntp = tk.BooleanVar(value=True)
        self.v_keepawake = tk.BooleanVar(value=True)
        self.v_phone_awake = tk.BooleanVar(value=self.cfg.get("phone_stay_awake", True))

        grid = ttk.Frame(right, style="Panel.TFrame"); grid.pack(fill="x",
                                                                     pady=(theme.S, 0))
        for r, (lab, var, w, help_) in enumerate((
                ("链路耗时补偿 lead_ms", self.v_lead, 7,
                 "从写下命令到手机上真按下去要多久，程序提前这么久发。排练页会给出实测值。"),
                ("命中偏移 bias_ms", self.v_bias, 7,
                 "正数=命中更晚。抢票场景建议 +70，宁可晚不可早：早了按钮还没生效等于白按。"),
                ("两下间隔下限", self.v_gap, 6,
                 "sequence 模式下第一下与第二下的最小间隔。"),
                ("两下间隔随机幅度", self.v_jit, 6,
                 "每发在 下限 到 下限+幅度 之间随机。固定间隔本身就是机器特征。"))):
            ttk.Label(grid, text=lab, style="Dim.TLabel").grid(
                row=r, column=0, sticky="w", pady=(0 if r == 0 else theme.S))
            sp = ttk.Spinbox(grid, from_=-999, to=9999, textvariable=var, width=w,
                             increment=1)
            sp.grid(row=r, column=1, sticky="e", padx=(theme.M, 0),
                    pady=(0 if r == 0 else theme.S))
            sp.bind("<Enter>", lambda e, h=help_: self._tip.config(text=h))
            sp.bind("<Leave>", lambda e: self._tip.config(text=""))

        opts = ttk.Frame(right, style="Panel.TFrame"); opts.pack(fill="x",
                                                                     pady=(theme.M, 0))
        ttk.Label(opts, text="两下方式").pack(side="left")
        # ttk.Combobox has no `command=` option (that is the classic tk one);
        # passing it raises TclError at construction, so bind the event instead
        cb = ttk.Combobox(opts, textvariable=self.v_mode,
                          values=["concurrent", "sequence"], state="readonly",
                          width=12)
        cb.pack(side="left", padx=theme.S)
        cb.bind("<<ComboboxSelected>>", lambda e: (self._mode_changed(),
                                                   self._sync_rail()))
        self._tip = tk.Label(right, justify="left", anchor="w",
                             bg=theme.SURFACE, fg=theme.FAINT, font=theme.ui(9),
                             wraplength=330, height=2,
                             text="把鼠标停在任一数字框上，这里会解释它的作用。")
        self._tip.pack(fill="x", pady=(theme.S, 0))

        for var, text in ((self.v_ntp, "发射前自动校正本机时钟"),
                          (self.v_phone_awake, "保持手机亮屏（USB 供电时不休眠）"),
                          (self.v_keepawake, "值守期间阻止电脑睡眠")):
            ttk.Checkbutton(right, text=text, variable=var).pack(anchor="w")

        # -- actions ----------------------------------------------------------
        theme.rule(f).grid(row=1, column=0, columnspan=2, sticky="ew",
                           padx=theme.L)
        bar = ttk.Frame(f, style="Panel.TFrame")
        bar.grid(row=2, column=0, columnspan=2, sticky="ew",
                 padx=theme.L, pady=theme.L)
        ttk.Button(bar, text="预约发射", style="Primary.TButton",
                   command=self.arm).pack(side="left")
        ttk.Button(bar, text="取消", style="Danger.TButton",
                   command=self.cancel.set).pack(side="left", padx=theme.S)
        ttk.Button(bar, text="试射（10 秒后，不真点屏幕）", style="Ghost.TButton",
                   command=self.test_shot).pack(side="left")
        ttk.Button(bar, text="打开日志文件", style="Ghost.TButton",
                   command=self._open_log).pack(side="right")

    def _quick_set(self, secs):
        """Write an absolute timestamp, not a bare HH:MM:SS.

        A bare time means 'next occurrence', so a +30s countdown that sits
        untouched for half a minute would silently roll over to tomorrow.
        """
        t = (datetime.now() + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S.000")
        self.txt_times.delete("1.0", "end")
        self.txt_times.insert("1.0", t)
        self.log(f"已设为 {secs} 秒后：{t}")

    # ---------- plumbing ----------
    LOG_MAX = 512 * 1024

    def log(self, msg):
        msg = str(msg)
        self._write_log_file(msg)
        self.q.put(("log", msg))

    def _write_log_file(self, msg):
        """Append one line with a persistent handle.

        The old code opened and closed the file per line. That is a syscall
        pair on whatever thread called log(), and fire.py calls say() right
        after the newline that triggers tap B -- so in sequence mode a file
        write sat between the two taps.
        """
        try:
            if self._logfh is None:
                self._logpath = adbutil.pkgdir() / "doubletap.log"
                self._logfh = open(self._logpath, "a", encoding="utf-8")
            # Renaming under an AV filter can spike to milliseconds, and the
            # only moment that matters is the watch itself.
            if (not self._armed and self._logpath.stat().st_size > self.LOG_MAX):
                self._rotate_log()
            stamp = datetime.now().strftime("%m-%d %H:%M:%S.%f")[:-3]
            self._logfh.write(f"[{stamp}] {msg}\n")
            self._logfh.flush()
        except OSError:
            # logging must never be the reason a shot fails
            self._logfh = None

    def _rotate_log(self):
        self._logfh.close()
        self._logfh = None
        for i in (2, 1, 0):
            src = self._logpath if i == 0 else self._logpath.with_suffix(f".log.{i}")
            if not src.exists():
                continue
            dst = self._logpath.with_suffix(f".log.{i + 1}") if i < 2 else None
            if dst is not None:
                os.replace(src, dst)
        self._logfh = open(self._logpath, "w", encoding="utf-8")

    def _pump(self):
        """Drain the queue and apply it to widgets.

        Two hard rules, both learned the expensive way:
        * the reschedule lives in `finally`. A handler that raises must not
          take the countdown and the log panel down with it for the rest of
          the session.
        * no modal is ever opened from here. messagebox runs a nested Tk
          event loop, so _pump re-enters itself while the dialog is up.
        """
        try:
            events, logs = [], []
            for _ in range(400):
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                if item[0] == "log":
                    logs.append(item[1])
                else:
                    events.append(item)
            # the number the user is watching must never sit behind a wall of
            # log lines, so apply status first and only the last countdown
            cd = [e for e in events if e[0] == "cd"]
            order = [e for e in events if e[0] == "state"] \
                + cd[-1:] + [e for e in events if e[0] not in ("state", "cd")]
            for kind, *rest in order:
                try:
                    self._apply(kind, *rest)
                except BaseException as e:
                    self.txt.insert("end", f"!! 事件处理失败 {kind}: "
                                f"{e.__class__.__name__}: {e}\n", ("bad",))
            if logs:
                self._append_logs(logs)
        finally:
            self.after(60, self._pump)

    def _apply(self, kind, *rest):
        if kind == "cd":
            self.big.config(text=_fmt_cd(rest[0]), foreground=theme.AMBER)
        elif kind == "state":
            text, color = rest[0]
            self.state_lbl.config(text=text, background=color, fg="#0d1014")
        elif kind == "notice":
            text, color, ms = rest[0]
            self._show_notice(text, color, ms)
        elif kind == "result":
            name, payload = rest[0]
            if name == "arm":
                n = payload.get("shots") if isinstance(payload, dict) else None
                self._set_idle(f"已发射 {n} 发" if n else "未发射",
                               theme.OK if n else theme.BAD)
            elif name == "cal":
                self._show_cal(payload)
        elif kind == "error":
            self._set_idle("失败 · 未发射", theme.BAD)
            self._show_notice(rest[0], theme.BAD, 15000)
        elif kind == "cal":
            self._show_cal(rest[0])

    def _append_logs(self, lines):
        blob = "".join(l + "\n" for l in lines)
        tags = [self._tag_for(l) for l in lines]
        # Tag by explicit line number. The relative form (end-Nlines) is easy to
        # get wrong and silently miscolours the wrong rows.
        first = int(self.txt.index("end-1c").split(".")[0])
        self.txt.insert("end", blob)
        for i, t in enumerate(tags):
            if t:
                self.txt.tag_add(t[0], f"{first + i}.0", f"{first + i + 1}.0")
        if self._follow:
            self.txt.see("end")
        else:
            self._behind += len(lines)
            self.newlines_lbl.config(text=f"↓ {self._behind} 条新日志", fg=theme.AMBER)
        # trim only while pinned to the bottom: deleting above the viewport
        # while the user is reading history makes the view jump
        if self._follow and int(self.txt.index("end-1c").split(".")[0]) > 2000:
            self.txt.delete("1.0", f"{first - 1000}.0")

    @staticmethod
    def _tag_for(line):
        """Colour the log by meaning, not by decoration: the lines you scan for
        under pressure are the failures and the confirmations."""
        if line.startswith("!!"):
            return ("bad",)
        if "生效了" in line or line.startswith("  =>"):
            return ("ok",)
        if "落空" in line or "已错过" in line or "不可达" in line:
            return ("warn",)
        if line.startswith("  ") or line.startswith("["):
            return ("dim",)
        return ()

    def _scroll_follow(self, first, last):
        """The scrollbar callback is the one signal that catches every way of
        moving the view, including dragging the thumb."""
        try:
            self._follow = float(last) >= 0.999
        except ValueError:
            return
        if self._follow:
            self._behind = 0
            self.newlines_lbl.config(text="", fg=theme.FAINT)

    def _unfollow(self, event=None):
        if event is not None and getattr(event, "delta", 0) > 0:
            pass
        if self.txt.yview() != ("0.0", "1.0"):
            self._follow = False
        return None

    def _wheel(self, event):
        step = -1 * (event.delta // abs(event.delta)) * 3
        self.txt.yview_scroll(step, "units")
        self._follow = float(self.txt.yview()[1]) < 0.999
        if self._follow:
            self._behind = 0
            self.newlines_lbl.config(text="", fg=theme.FAINT)
        return "break"

    def _jump_to_end(self):
        self.txt.see("end")
        self._follow = True
        self._behind = 0
        self.newlines_lbl.config(text="", fg=theme.FAINT)

    def _on_callback_error(self, exc_type, exc, tb):
        """Keep a Tk callback failure inside the log instead of a popup."""
        try:
            self.txt.insert("end",
                            "!! 未捕获异常 " + exc_type.__name__ + ": " + str(exc) + "\n",
                            ("bad",))
            self.txt.see("end")
        except Exception:
            pass

    def _show_notice(self, text, color, ms):
        self.notice.config(text=text, fg=color)
        if self._armed:
            return  # do not wipe a warning during the watch
        if self._notice_job:
            self.after_cancel(self._notice_job)
        self._notice_job = self.after(ms, self._clear_notice)

    def _clear_notice(self):
        self._notice_job = None
        if not self._armed:
            self.notice.config(text="")

    def _set_idle(self, state_text, color):
        self.state_lbl.config(text=state_text, background=color, fg="#0d1014")
        self.big.config(text=datetime.now().strftime("%H:%M:%S"),
                        foreground=theme.FAINT)
        self.rail_next.config(text=state_text)

    def _worker(self, fn, *a, name="task", label=None):
        """Run fn on a thread. Refusing a double-start is a rail notice, never
        a dialog -- a modal hides the countdown at the worst possible moment."""
        if self.busy or self._armed:
            what = label or "上一个任务"
            secs = (adbutil.now_ms() - self._t0) / 1000.0 if self._t0 else 0
            self.q.put(("notice", (f"{what}还在进行（{secs:.0f}s），这次点击已忽略",
                                   theme.AMBER, 6000)))
            return
        self.busy = True
        self._t0 = adbutil.now_ms()
        self.cancel.clear()

        def run():
            try:
                r = fn(*a)
                self.q.put(("result", (name, r)))
            except BaseException as e:
                # SystemExit included: adbutil.ensure_device raises it, and an
                # escaping exception here used to look like a successful no-op.
                msg = f"{e.__class__.__name__}: {e}"
                self.log(f"!! {msg}")
                self.log(traceback.format_exc(limit=4))
                self.q.put(("error", msg))
            finally:
                self.busy = False
                self._t0 = None
        threading.Thread(target=run, daemon=True).start()

    def _say(self, m):
        self.log(m)

    def _tick(self, remain, label):
        self.q.put(("cd", remain))

    # ---------- 1 device ----------
    def refresh_device(self):
        try:
            serial = adbutil.ensure_device()
            info = [adbutil.sh(f"getprop {p}") for p in
                    ("ro.product.brand", "ro.product.model", "ro.build.version.release")]
            w, h = adbutil.dev_resolution()
            self.dev_info.config(fg=theme.TEXT, text=(
                f"序列号    {serial}\n"
                f"型号      {info[0]} {info[1]}\n"
                f"系统      Android {info[2]}   ·   分辨率 {w}x{h}（坐标 1:1）\n"
                f"adb       {adbutil.ADB}"))
            self.rail_dev.config(text=f"● {info[0]} {info[1]} · {w}x{h}",
                                 fg=theme.OK)
            self.log(f"设备在线：{info[0]} {info[1]}  {w}x{h}")
        except SystemExit as e:
            self.dev_info.config(text="未检测到设备\n\n检查数据线、USB 调试和授权弹窗",
                                 fg=theme.BAD)
            self.rail_dev.config(text="● 设备未连接", fg=theme.BAD)
            self.log(f"!! {e}")
        self._sync_rail()

    def check_inject(self):
        def go():
            try:
                b = adbutil.sh("settings get system volume_music_speaker")
                adbutil.sh("input keyevent 24")
                time.sleep(0.8)
                a = adbutil.sh("settings get system volume_music_speaker")
                ok = b != a
                self.log(f"音量键检测: {'通过' if ok else '未观察到音量变化'}"
                         f"  (音量 {b} -> {a})")
                if not ok:
                    # A flat volume here does NOT mean injection is broken: the
                    # media stream only moves under a media-capable window, so
                    # on Settings or a launcher this reads as a false negative.
                    # Claiming "去开 USB调试(安全设置)" sent us chasing a
                    # non-existent problem once already.
                    self.log("  （此项在非媒体页面不可靠，不代表注入失败。"
                             "要确认请用「立即单发」配合屏幕上的点按圆圈）")
                else:
                    adbutil.sh("input keyevent 25")
            except Exception as e:
                self.log(f"!! {e}")
            return None
        self._worker(go, name="dev", label="音量键检测")

    def _toggle_taps(self, on):
        try:
            adbutil.sh(f"settings put secure show_taps {1 if on else 0}")
            self.log(f"点按显示: {'开' if on else '关'}")
        except Exception as e:
            self.log(f"!! {e}")

    # ---------- 2 coordinates ----------
    def _manual_scale(self):
        self.v_auto.set(False)
        self.grab()

    def grab(self):
        try:
            adbutil.ensure_device()
            self.W, self.H = adbutil.dev_resolution()
            if self.v_auto.get():
                self.scale = self._fit_scale(self.H)
                self.v_scale.set(self.scale)
            else:
                self.scale = max(1, min(6, int(self.v_scale.get())))
            shot = adbutil.pkgdir() / "shot.png"
            with open(shot, "wb") as fh:
                subprocess.run(adbutil.base() + ["exec-out", "screencap", "-p"],
                               stdout=fh, stderr=subprocess.DEVNULL, check=True, timeout=30)
            if shot.stat().st_size < 1000:
                raise RuntimeError("screencap 返回空，手机可能锁屏了")
            self._shot = tk.PhotoImage(file=str(shot))
            self._disp = self._shot.subsample(self.scale, self.scale)
            self.canvas.config(width=self.W // self.scale, height=self.H // self.scale)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self._disp)
            # re-show what is already saved -- without this the page looks
            # unset even when config holds good coordinates, and people
            # re-pick them by hand and get it wrong
            self.pts = self._pts_from_cfg()
            self._redraw_marks()
            saved = len(self.pts)
            self.log(f"截图 {self.W}x{self.H}，显示 1/{self.scale}"
                     + (f"，已载入保存的 {saved} 个点" if saved else "，尚无已存坐标"))
        except (Exception, SystemExit) as e:
            # SystemExit is not an Exception subclass, and adbutil.ensure_device
            # raises exactly that -- without this a unplugged phone killed the app
            self.log(f"!! 截图失败 {e}")

    def _pts_from_cfg(self):
        out = []
        for k in ("tap_a", "tap_b"):
            p = self.cfg.get(k)
            if isinstance(p, dict) and "x" in p and "y" in p:
                out.append([int(p["x"]), int(p["y"])])
        return out

    def on_canvas(self, ev):
        if not hasattr(self, "_disp"):
            self.log("先点「重新截图」")
            return
        x = min(max(int(round(ev.x * self.scale)), 0), self.W - 1)
        y = min(max(int(round(ev.y * self.scale)), 0), self.H - 1)
        if len(self.pts) >= 2:
            self.pts = []
        self.pts.append([x, y])
        self._redraw_marks()

    def _nudge(self, dx, dy, m):
        if not self.pts:
            return
        p = self.pts[-1]
        p[0] = min(max(p[0] + dx * m, 0), self.W - 1)
        p[1] = min(max(p[1] + dy * m, 0), self.H - 1)
        self._redraw_marks()
        self.coord_lbl.config(text=f"#{len(self.pts)} = ({p[0]}, {p[1]})   (微调 {m}px)")

    def _redraw_marks(self):
        self.canvas.delete("mark")
        for i, (x, y) in enumerate(self.pts):
            cx, cy = x / self.scale, y / self.scale
            col = "#f33" if i == 0 else "#3f6"
            self.canvas.create_oval(cx - 12, cy - 12, cx + 12, cy + 12,
                                    outline=col, width=2, tags="mark")
            self.canvas.create_line(cx - 20, cy, cx + 20, cy, fill=col, tags="mark")
            self.canvas.create_line(cx, cy - 20, cx, cy + 20, fill=col, tags="mark")
            self.canvas.create_text(cx, cy - 24, text=f"#{i+1}", fill=col, tags="mark")
        self.coord_lbl.config(text="  ".join(
            f"#{i+1} = ({p[0]}, {p[1]})" for i, p in enumerate(self.pts)) or "尚未取样")

    def save_points(self):
        if len(self.pts) < 2:
            self.log("需要两个点")
            return
        self.cfg["tap_a"] = {"x": self.pts[0][0], "y": self.pts[0][1]}
        self.cfg["tap_b"] = {"x": self.pts[1][0], "y": self.pts[1][1]}
        self.cfg["focus"] = adbutil.focus()
        name = self.v_preset.get().strip()
        if name:
            # keep the named preset in step with the edit, otherwise "保存坐标"
            # silently diverges from what 应用 would restore later
            self.cfg.setdefault("presets", {})[name] = {
                "tap_a": self.cfg["tap_a"], "tap_b": self.cfg["tap_b"],
                "focus": self.cfg["focus"]}
        self._sync_rail()
        save_cfg(self.cfg)
        self.log(f"已保存坐标 A{self.pts[0]} B{self.pts[1]}  界面={self.cfg['focus'] or '未知'}"
                 + (f" → 预设「{name}」" if name else ""))

    def _refresh_preset_list(self, select=None):
        names = sorted(self.cfg.get("presets", {}))
        self.cb_preset.config(values=names)
        if select is not None:
            self.v_preset.set(select)

    def _save_preset_as(self):
        if len(self.pts) < 2:
            self.log("先在图上取两个点")
            return
        name = simpledialog.askstring("新建预设", "给这套坐标起个名字：",
                                      initialvalue=self.v_preset.get().strip(),
                                      parent=self)
        if not name:
            return
        name = name.strip()
        self.save_points()
        self.cfg.setdefault("presets", {})[name] = {
            "tap_a": self.cfg["tap_a"], "tap_b": self.cfg["tap_b"]}
        self.cfg["active_preset"] = name
        save_cfg(self.cfg)
        self._refresh_preset_list(name)
        self.log(f"已建立预设「{name}」")

    def _apply_preset(self):
        name = self.v_preset.get().strip()
        p = self.cfg.get("presets", {}).get(name)
        if not p:
            self.log(f"没有名为「{name}」的预设")
            return
        self.cfg["tap_a"] = dict(p["tap_a"])
        self.cfg["tap_b"] = dict(p["tap_b"])
        self.cfg["focus"] = p.get("focus", "")
        self.cfg["active_preset"] = name
        save_cfg(self.cfg)
        self._sync_rail()
        self.pts = self._pts_from_cfg()
        if hasattr(self, "scale"):
            self._redraw_marks()
        else:
            self.coord_lbl.config(text="  ".join(
                f"#{i+1} = ({q[0]}, {q[1]})" for i, q in enumerate(self.pts)))
        self.log(f"已应用预设「{name}」 A{self.pts[0]} B{self.pts[1]}")

    def _del_preset(self):
        name = self.v_preset.get().strip()
        if name not in self.cfg.get("presets", {}):
            self.log("先在下拉里选中要删的预设")
            return
        if not messagebox.askyesno("删除预设", f"删除「{name}」？坐标本身不会丢。"):
            return
        del self.cfg["presets"][name]
        if self.cfg.get("active_preset") == name:
            self.cfg["active_preset"] = ""
        save_cfg(self.cfg)
        self._refresh_preset_list("")
        self.log(f"已删除预设「{name}」")

    # ---------- 3 calibration ----------
    def do_ntp(self):
        def go():
            off, detail = adbutil.ntp_offset_ms()
            if off is None:
                self.log("!! NTP 全部不可达（UDP 123 可能被拦），时钟不可信")
            else:
                for srv, (med, lo, n) in detail.items():
                    self.log(f"  {srv:<18} 中位 {med:+8.1f}ms  最优 {lo:+8.1f}ms")
                self.log(f"=> 真实时间 = 本机时间 {off:+.1f}ms，本机{'慢' if off > 0 else '快'}"
                         f"了 {abs(off):.0f}ms")
            return None
        self._worker(go, name="dev", label="时钟测量")

    def _mode_changed(self):
        """Each mode has its own measured lead, so swap the field when the user
        switches rather than letting one value silently break the other mode."""
        old = self._lead_mode
        new = self.v_mode.get()
        if old != new:
            self.cfg.setdefault("lead_ms_by_mode", {})[old] = float(self.v_lead.get())
            self._lead_mode = new
            self.v_lead.set(fire.auto_lead(self.cfg, new))
        save_cfg(self.cfg)

    def _collect_cfg(self):
        # rehearse is a per-call flag, never a stored setting: a test run that
        # mutated self.cfg used to persist it, and then every real shot fired
        # no-op keyevents forever while the UI cheerfully reported 完成.
        self.cfg.pop("rehearse", None)
        self.cfg["mode"] = self.v_mode.get()
        self.cfg["gap_ms"] = int(self.v_gap.get())
        self.cfg["gap_jitter_ms"] = int(self.v_jit.get())
        lead = float(self.v_lead.get())
        self.cfg.setdefault("lead_ms_by_mode", {})[self.cfg["mode"]] = lead
        self.cfg["lead_ms"] = lead
        self._lead_mode = self.cfg["mode"]
        self.cfg["server_bias_ms"] = float(self.v_bias.get())
        save_cfg(self.cfg)

    def do_rehearse(self):
        self._collect_cfg()
        reps = int(self.v_reps.get())

        def go():
            cfg = dict(self.cfg)
            res = []
            for i in range(reps):
                if self.cancel.is_set():
                    break
                t = adbutil.now_ms() + 4000
                self.log(f"--- 排练 {i+1}/{reps} ---")
                r = fire.one_shot(cfg, t, True, rehearse=True, say=self._say, tick=self._tick,
                                  cancel=self.cancel.is_set)
                if r:
                    res.append(r)
                time.sleep(0.4)
            if res:
                self.q.put(("cal", res))
            return None
        self._worker(go, name="cal", label="排练")

    def _show_cal(self, res):
        e = [x["first_late_ms"] for x in res]
        sp = [x["spacing_ms"] for x in res]
        mode = self.v_mode.get()
        lead = float(self.v_lead.get()); bias = float(self.v_bias.get())
        suggest = lead - (st.median(e) - bias)
        asked = [x.get("gap_req_ms", 0.0) for x in res]
        txt = (f"模式 {mode}   样本 {len(res)} 次\n"
               f"命中误差  中位 {st.median(e):+.1f}ms\n"
               f"          区间 {min(e):+.1f} ~ {max(e):+.1f}ms\n"
               f"两下间隔  实测中位 {st.median(sp):.1f}ms"
               + (f"  (请求中位 {st.median(asked):.0f}ms)" if max(asked) > 0 else "")
               + f"\n          最差 {max(sp):.1f}ms\n"
               f"\n建议 lead_ms[{mode}] = {suggest:.0f}\n"
               f"永不为早 → bias = {-min(e):.0f}\n"
               f"永不为晚 → bias = {-max(e):.0f}")
        self.cal_lbl.config(text=txt, fg=theme.TEXT)
        if not (5 <= suggest <= 400):
            self.log(f"!! 建议值 {suggest:.0f}ms 超出合理范围(5~400)，已忽略；"
                     "请重跑一次排练")
            return
        if messagebox.askyesno("应用", f"把 {mode} 的 lead_ms 改成 {suggest:.0f} 吗？"):
            self.v_lead.set(round(suggest))
            self._collect_cfg()
            self.log(f"lead_ms[{mode}] -> {round(suggest)}")

    # ---------- 4 fire ----------
    TIME_FORMATS = ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%H:%M:%S.%f", "%H:%M:%S", "%H:%M")

    def _time_lines(self):
        return [l.strip() for l in self.txt_times.get("1.0", "end").splitlines() if l.strip()]

    def _resolve_times(self):
        """Next occurrence of each typed line, as PC-clock epoch ms, sorted.

        A bare HH:MM:SS means "next time the clock reads that", so one saved
        list keeps working every day untouched -- which is what a daily
        10:00 / 20:00 pair actually needs.
        """
        now = datetime.now()
        out = []
        for s in self._time_lines():
            dt = fmt = None
            for f in self.TIME_FORMATS:
                try:
                    dt = datetime.strptime(s, f); fmt = f; break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError(f"看不懂的时间：{s}")
            if fmt.startswith("%H"):
                dt = dt.replace(year=now.year, month=now.month, day=now.day)
                if dt <= now:
                    dt += timedelta(days=1)
            out.append(dt.timestamp() * 1000.0)
        out.sort()
        return out

    def _update_preview(self):
        try:
            ts = self._resolve_times()
        except ValueError as e:
            self.v_preview.set(f"{e}\n(改好这行再继续)")
        except Exception as e:
            self._prev_lbl.config(fg=theme.BAD)
            self.v_preview.set(str(e))
        else:
            rows = [f"{datetime.fromtimestamp(t/1000):%m-%d %H:%M:%S}   还有 {_dur(t - adbutil.now_ms())}"
                    for t in ts]
            self.v_preview.set("\n".join(rows) if rows else "（空）")
        self.after(1000, self._update_preview)

    def arm(self):
        self._collect_cfg()
        self.cfg["target_times"] = self._time_lines()
        save_cfg(self.cfg)
        if not self.cfg.get("tap_a") or not self.cfg.get("tap_b"):
            self.log("!! 先在「2 坐标」里取两个点")
            return
        # pre-flight, so a dead connection fails the moment you click rather
        # than after a countdown that looked like it was working
        try:
            adbutil.ensure_device()
        except SystemExit as e:
            self.big.config(text="设备不在线", foreground="#c00")
            messagebox.showerror("设备不在线", f"{e}\n\n检查数据线、USB 调试和授权弹窗")
            return
        # tell them now, not after the countdown: coordinates belong to a
        # specific screen, and a logged-out or scrolled-away page taps void
        want = self.cfg.get("focus") or ""
        cur = adbutil.focus()
        if want and cur and cur != want:
            self.log(f"!! 当前界面 {cur} 与选坐标时的 {want} 不一致")
            if not messagebox.askyesno(
                    "界面不一致",
                    f"当前手机界面：\n  {cur}\n\n"
                    f"选坐标时的界面：\n  {want}\n\n"
                    "坐标可能落在错误的位置。仍要预约吗？"):
                return
        try:
            ts = self._resolve_times()
        except ValueError as e:
            self.log(f"!! {e}")
            return
        if not ts:
            self.log("!! 目标时刻是空的")
            return
        if ts[0] - adbutil.now_ms() < 3500:
            self.log("!! 最近一发不足 4 秒，来不及做预热")
            return
        # Snapshot every Tk-backed value NOW, on the main thread. Reading a
        # variable from the worker raises "main thread is not in main loop",
        # which used to abort the whole schedule before the first shot.
        opts = {"keepawake": self.v_keepawake.get(), "ntp": self.v_ntp.get(),
                "want_focus": self.cfg.get("focus") or "",
                "phone_awake": self.v_phone_awake.get()}
        self.state_lbl.config(text="已预约", background=theme.AMBER, fg="#0d1014")
        self.rail_next.config(text=f"{len(ts)} 个时刻 · "
                             + ", ".join(datetime.fromtimestamp(t/1000).strftime("%H:%M:%S")
                                         for t in ts[:4]))
        self.cfg["phone_stay_awake"] = opts["phone_awake"]
        save_cfg(self.cfg)
        self._worker(lambda: self._run_schedule(ts, opts))

    def _run_schedule(self, times, opts):
        cfg = dict(self.cfg)
        want = opts["want_focus"]
        if opts["phone_awake"]:
            adbutil.stay_awake(True)
            self.log("  已设为 USB 供电时手机不休眠")

        def precheck():
            """Runs ~2.5s before the instant -- late enough to reflect the state
            the taps will actually land on, early enough to still abort cleanly.
            A dark screen is a guaranteed miss, so that one does abort."""
            if opts["phone_awake"] and adbutil.screen_on() is False:
                adbutil.wake_screen()
                time.sleep(0.6)
                self.log("  屏幕是熄的，已发送唤醒")
            if adbutil.screen_on() is False:
                return "ABORT 屏幕仍处于熄灭状态，点击不会生效，已放弃这一发"
            if not want:
                return None
            cur = adbutil.focus()
            if not cur or cur == want:
                return None
            self.log(f"  界面已变 期望 {want} / 实际 {cur}")
            return None
        self._keep_awake(opts["keepawake"])
        # shrink the GIL hand-off quantum so the GUI thread cannot make us wait
        # a full default 5ms slice right before the write
        old = sys.getswitchinterval()
        sys.setswitchinterval(0.0002)
        off, off_at = 0.0, 0.0
        shots = []
        try:
            for i, raw in enumerate(times):
                if self.cancel.is_set():
                    self.log(f"已取消，剩余 {len(times)-i} 个时刻不再执行")
                    break
                # an NTP round trip costs ~0.6s, so only spend a fresh one when
                # there is room; hours apart is exactly when it matters
                if opts["ntp"] and (raw - adbutil.now_ms()) > 3000:
                    if (adbutil.now_ms() - off_at) > 60000:
                        o, _ = adbutil.ntp_offset_ms()
                        if o is None:
                            self.log("!! NTP 不可达，这一发按本机表发射")
                        else:
                            off, off_at = o, adbutil.now_ms()
                            self.log(f"  本机时钟偏差 {off:+.0f}ms 已补偿")
                self.log(f"--- 第 {i+1}/{len(times)} 发  目标 {hms(raw - off)} ---")
                before = adbutil.focus()
                r = fire.one_shot(cfg, raw - off, False, say=self._say,
                                  tick=self._tick, cancel=self.cancel.is_set,
                                  precheck=precheck)
                if r:
                    shots.append((i, before))
                else:
                    self.log(f"  第 {i+1} 发未执行（错过时刻或被拦截）")
                if not self.cancel.is_set():
                    time.sleep(1.0)
        finally:
            sys.setswitchinterval(old)
            self._keep_awake(False)
            if opts["phone_awake"]:
                adbutil.stay_awake(False)
                self.log("  已恢复手机休眠策略")
        # HyperOS 3 ignores show_taps, so there is no white circle to watch.
        # Whether the foreground window changed is an objective substitute:
        # a tap that did nothing anywhere is a tap you need to know about.
        for i, before in shots:
            after = adbutil.focus()
            if not before:
                continue
            if after != before:
                self.log(f"  第 {i+1} 发后界面已变化：{before.split('/')[-1]} → "
                         f"{after.split('/')[-1]}  ← 点击确实生效了")
            else:
                self.log(f"  第 {i+1} 发后界面没变（{after.split('/')[-1]}）"
                         "  ← 点击可能落空：坐标不对、按钮未启用、或页面已变")
        return {"shots": len(shots)} if shots else None

    def _keep_awake(self, on):
        """A 10:00 and a 20:00 shot are 10 hours apart; a sleeping PC misses both.

        SetThreadExecutionState is per-thread and drops when this worker exits,
        but pass 0 explicitly anyway so a cancelled watch cannot linger.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes
            ES_CONTINUOUS, ES_SYSTEM_REQUIRED, ES_DISPLAY_REQUIRED = (
                0x80000000, 0x00000001, 0x00000002)
            flags = (ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED) if on else 0
            ctypes.windll.kernel32.SetThreadExecutionState(flags)
            self.log("  已阻止电脑睡眠" if on else "  已恢复电脑睡眠策略")
        except Exception as e:
            self.log(f"!! 无法设置防睡眠：{e}")

    def test_shot(self):
        """One verify-mode shot 10s out with no-op keyevents, so the whole
        schedule path can be exercised without touching the target app."""
        self._collect_cfg()
        t = adbutil.now_ms() + 10000
        cfg = dict(self.cfg)

        def go():
            self.log("--- 试射（排练模式，不会真点屏幕）---")
            # report the measurement: this is the only way to see the timing
            # of the actual GUI code path, since the CLI never imports gui.py
            r = fire.one_shot(cfg, t, True, rehearse=True, say=self._say,
                              tick=self._tick, cancel=self.cancel.is_set)
            if r:
                self.q.put(("cal", [r]))
            return None
        self._worker(go, name="test", label="试射")


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        (adbutil.pkgdir() / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
