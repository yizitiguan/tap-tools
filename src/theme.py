"""Design tokens and ttk theming for 连点助手.

The usage scene decides the look: this is a precision timing console that gets
stared at, under time pressure, often at night, for ten hours at a stretch. So
it is dark, it is quiet except for the one number that matters, and every
reading is tabular so digits do not shuffle as they count down.
"""
import tkinter as tk
from tkinter import ttk

# -- palette --------------------------------------------------------------
BG = "#101317"          # window
SURFACE = "#171c22"     # panels, tab body
RAISED = "#1f262e"      # inputs, buttons
HOVER = "#28313b"
LINE = "#2a333d"
LINE_SOFT = "#222a33"

TEXT = "#e6ebf1"
DIM = "#9aa7b5"         # >=4.5:1 on SURFACE
FAINT = "#6f7d8c"       # decorative/tertiary only, never body copy

AMBER = "#f2a63b"       # armed, counting down
OK = "#4ade80"          # verified working
BAD = "#f2555a"         # aborted, offline, error
INFO = "#5aa9f2"        # resolved schedule, neutral highlight

# -- type -----------------------------------------------------------------
UI = "Microsoft YaHei UI"
MONO = "Cascadia Mono"
MONO_FALLBACK = "Consolas"


def mono(size, weight="normal"):
    return (MONO, size, weight)


def ui(size, weight="normal"):
    return (UI, size, weight)


# -- spacing scale --------------------------------------------------------
XS, S, M, L, XL = 4, 8, 12, 18, 28


def apply(root):
    """Install the dark console theme on every ttk widget class we use."""
    st = ttk.Style(root)
    st.theme_use("clam")

    st.configure(".", background=SURFACE, foreground=TEXT, font=ui(10),
                 borderwidth=0, relief="flat", padding=0)
    root.configure(bg=BG, highlightbackground=LINE)

    st.configure("TFrame", background=SURFACE)
    st.configure("Card.TFrame", background=RAISED)
    st.configure("Panel.TFrame", background=SURFACE)

    st.configure("TLabel", background=SURFACE, foreground=TEXT, font=ui(10))
    st.configure("Dim.TLabel", background=SURFACE, foreground=DIM, font=ui(9))
    st.configure("Section.TLabel", background=SURFACE, foreground=TEXT, font=ui(11, "bold"))

    # buttons: flat raised slab, colour shift is the only affordance Tk gives
    st.configure("TButton", background=RAISED, foreground=TEXT, font=ui(10),
                 padding=(M, S), bordercolor=LINE, relief="flat", focusthickness=0)
    st.map("TButton", background=[("disabled", LINE_SOFT), ("pressed", HOVER),
                                 ("active", HOVER)],
           foreground=[("disabled", FAINT)])
    st.configure("Primary.TButton", background=AMBER, foreground="#101317",
                 font=ui(10, "bold"), padding=(L, S + 2))
    st.map("Primary.TButton", background=[("disabled", LINE_SOFT),
                                          ("pressed", "#d8912c"),
                                          ("active", "#ffbb55")])
    st.configure("Ghost.TButton", background=SURFACE, foreground=DIM)
    st.map("Ghost.TButton", background=[("active", RAISED)],
           foreground=[("disabled", FAINT), ("active", TEXT)])
    st.configure("Danger.TButton", background=RAISED, foreground=BAD)
    st.map("Danger.TButton", background=[("active", "#3a2226")])

    st.configure("TCheckbutton", background=SURFACE, foreground=TEXT, font=ui(10),
                 indicatorcolor=RAISED, padding=(0, S))
    st.map("TCheckbutton", background=[("active", SURFACE)],
           foreground=[("disabled", FAINT)],
           indicatorcolor=[("selected", AMBER), ("active", HOVER)])
    st.configure("Card.TCheckbutton", background=RAISED, foreground=TEXT)
    st.map("Card.TCheckbutton", background=[("active", RAISED)],
           indicatorcolor=[("selected", AMBER), ("active", HOVER)])

    st.configure("TCombobox", fieldbackground=RAISED, background=RAISED,
                 foreground=TEXT, arrowcolor=DIM, padding=S, font=mono(10))
    st.map("TCombobox", fieldbackground=[("readonly", RAISED)],
           foreground=[("readonly", TEXT)], arrowcolor=[("active", TEXT)])
    root.option_add("*TCombobox*Listbox.background", RAISED)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", HOVER)
    root.option_add("*TCombobox*Listbox.selectForeground", AMBER)
    root.option_add("*TCombobox*Listbox.font", mono(10))

    st.configure("TSpinbox", fieldbackground=RAISED, background=RAISED,
                 foreground=TEXT, arrowcolor=DIM, bordercolor=LINE,
                 insertcolor=AMBER, padding=(S, 6), font=mono(10))
    st.map("TSpinbox", fieldbackground=[("focus", "#242d37")],
           bordercolor=[("focus", AMBER)])

    st.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
    st.configure("TNotebook.Tab", background=SURFACE, foreground=DIM, font=ui(10),
                 padding=(L, S + 2), borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", BG)],
           foreground=[("selected", TEXT), ("active", TEXT)],
           expand=[("selected", (0, 0, 0, 0))])

    st.configure("Vertical.TScrollbar", background=RAISED, troughcolor=SURFACE,
                 arrowcolor=DIM, bordercolor=SURFACE)
    st.map("Vertical.TScrollbar", background=[("active", HOVER)])
    return st


# -- plain-tk widgets ttk cannot reach ------------------------------------
def text_widget(parent, **kw):
    kw.setdefault("background", "#0c0f13")
    kw.setdefault("foreground", TEXT)
    kw.setdefault("insertbackground", AMBER)
    kw.setdefault("selectbackground", HOVER)
    kw.setdefault("selectforeground", TEXT)
    kw.setdefault("relief", "flat")
    kw.setdefault("borderwidth", 0)
    kw.setdefault("highlightthickness", 0)
    kw.setdefault("font", mono(9))
    return tk.Text(parent, **kw)


def canvas(parent, **kw):
    kw.setdefault("background", "#0c0f13")
    kw.setdefault("highlightthickness", 0)
    kw.setdefault("borderwidth", 0)
    return tk.Canvas(parent, **kw)


def rule(parent, orient="horizontal", **kw):
    """Hairline divider. Tk has no border-image, so a 1px Frame is the honest
    equivalent of a 1px rule and carries the same rhythm."""
    kw.setdefault("height" if orient == "horizontal" else "width", 1)
    kw.setdefault("background", LINE_SOFT)
    return tk.Frame(parent, bd=0, **kw)


def chip(parent, text, command, accent=None):
    """Compact quick-set control. Tk buttons cannot be pill-shaped, so the chip
    reads as a chip through size and colour rather than a fake border-radius."""
    fg = accent or DIM
    b = tk.Button(parent, text=text, command=command,
                  background=RAISED, foreground=fg, activebackground=HOVER,
                  activeforeground=TEXT, relief="flat", bd=0,
                  font=mono(10, "bold"), padx=M, pady=6, cursor="hand2",
                  highlightthickness=1, highlightbackground=LINE,
                  highlightcolor=accent or AMBER)
    b.bind("<Enter>", lambda e: b.config(background=HOVER))
    b.bind("<Leave>", lambda e: b.config(background=RAISED))
    return b
