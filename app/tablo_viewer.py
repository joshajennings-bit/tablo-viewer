"""
Tablo Viewer — watch live TV from a Tablo 4th Gen on Windows.

Unofficial. Not made or supported by Tablo/Nuvyyo/Scripps.
Video plays inside the window using mpv (embedded).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from mpv_player import MpvPlayer
from tablo_client import Airing, Channel, Device, TabloAuthError, TabloError, TabloSession

APP_NAME = "Tablo Viewer"
APP_VERSION = "2.2"
FROZEN = getattr(sys, "frozen", False)  # running as the packaged TabloViewer.exe
APP_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # where PyInstaller unpacked bundled files
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "TabloViewer"
CONFIG_FILE = DATA_DIR / "settings.json"
LOG_FILE = DATA_DIR / "tablo_viewer.log"
KEYRING_SERVICE = "TabloViewer"
IS_WIN = sys.platform == "win32"

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("tablo")

try:
    import keyring  # stores the password in Windows Credential Manager
except Exception:  # pragma: no cover
    keyring = None

# ---------------------------------------------------------------- theme

BG = "#0b0e14"         # window
PANEL = "#121722"      # header, sidebar, bars
PANEL2 = "#1a2130"     # inputs, cards
HOVER = "#212a3b"
ACTIVE = "#27324a"
BORDER = "#263045"
TEXT = "#eef1f6"
MUTED = "#98a2b5"
DIM = "#5f6a80"
ACCENT = "#3d8bff"
ACCENT_HOVER = "#5a9cff"
LIVE = "#ef4351"
TRACK = "#2a3346"

FONT = "Segoe UI"
FONT_SEMI = "Segoe UI Semibold"

ICON_GLYPHS = {  # Segoe Fluent Icons / Segoe MDL2 Assets code points
    "play": "\uE768", "pause": "\uE769", "back": "\uED3C", "fwd": "\uED3D",
    "up": "\uE70E", "down": "\uE70D", "vol": "\uE767", "mute": "\uE74F",
    "full": "\uE740", "unfull": "\uE73F", "refresh": "\uE72C", "search": "\uE721",
    "more": "\uE712",
}
ICON_FALLBACK = {
    "play": "▶", "pause": "❚❚", "back": "↺", "fwd": "↻", "up": "▲", "down": "▼",
    "vol": "♪", "mute": "✕", "full": "⛶", "unfull": "⤡", "refresh": "⟳",
    "search": "⌕", "more": "⋯",
}


# ---------------------------------------------------------------- settings

def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Couldn't save settings: %s", e)


def get_saved_password(email: str) -> str | None:
    if keyring and email:
        try:
            return keyring.get_password(KEYRING_SERVICE, email)
        except Exception as e:
            log.warning("keyring read failed: %s", e)
    return None


def set_saved_password(email: str, password: str | None) -> None:
    if not (keyring and email):
        return
    try:
        if password:
            keyring.set_password(KEYRING_SERVICE, email, password)
        else:
            keyring.delete_password(KEYRING_SERVICE, email)
    except Exception as e:
        log.info("keyring write/delete: %s", e)


# ---------------------------------------------------------------- player discovery

def find_mpv(cfg: dict) -> str | None:
    bundled = BUNDLE_DIR / "mpv" / "mpv.exe"
    if bundled.exists():  # the .exe build ships its own mpv
        return str(bundled)
    custom = cfg.get("player_path")
    if custom and Path(custom).exists() and "mpv" in Path(custom).name.lower():
        return custom
    which = shutil.which("mpv")
    if which:
        return which
    local = os.environ.get("LOCALAPPDATA", "")
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    for p in (APP_DIR / "mpv" / "mpv.exe", APP_DIR / "mpv.exe",
              Path(local) / "Microsoft" / "WinGet" / "Links" / "mpv.exe",
              Path(local) / "Programs" / "mpv" / "mpv.exe",
              Path(pf) / "mpv" / "mpv.exe",
              Path.home() / "scoop" / "shims" / "mpv.exe"):
        if p.exists():
            return str(p)
    return None


# ---------------------------------------------------------------- helpers

def fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%I:%M %p").lstrip("0")


def ellipsize(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def current_and_next(items: list[Airing]) -> tuple[Airing | None, Airing | None]:
    now = datetime.now(timezone.utc)
    cur = nxt = None
    for a in items:
        if a.start <= now < a.end:
            cur = a
        elif a.start > now:
            nxt = a
            break
    return cur, nxt


def make_app_icon() -> tk.PhotoImage:
    """32x32 blue rounded square with a white play triangle."""
    size, r = 32, 7
    img = tk.PhotoImage(width=size, height=size)
    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            cx = min(max(x, r), size - 1 - r)
            cy = min(max(y, r), size - 1 - r)
            inside = (x - cx) ** 2 + (y - cy) ** 2 <= r * r
            tri = 11 <= x <= 23 and abs(y - 15.5) <= (23 - x) * 0.62
            row.append("#ffffff" if inside and tri else (ACCENT if inside else BG))
        rows.append("{" + " ".join(row) + "}")
    img.put(" ".join(rows))
    return img


# ---------------------------------------------------------------- custom widgets

class IconButton(tk.Label):
    def __init__(self, master, icon: str, command=None, size: int = 14, fg: str = TEXT,
                 bg: str = PANEL, hover: str = HOVER, padx: int = 10, pady: int = 6):
        self._bg, self._hover = bg, hover
        self.command = command
        super().__init__(master, bg=bg, fg=fg, padx=padx, pady=pady, cursor="hand2")
        self.set_icon(icon, size)
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))
        self.bind("<Button-1>", lambda e: self.command and self.command())

    def set_icon(self, icon: str, size: int = 14):
        font_name = getattr(self.winfo_toplevel(), "icon_font_name", None)
        if font_name:
            self.config(text=ICON_GLYPHS.get(icon, icon), font=(font_name, size))
        else:
            self.config(text=ICON_FALLBACK.get(icon, icon), font=(FONT, size))


class PillButton(tk.Label):
    def __init__(self, master, text: str, command=None, primary: bool = True):
        self.c_bg = ACCENT if primary else PANEL2
        self.c_hover = ACCENT_HOVER if primary else HOVER
        super().__init__(master, text=text, bg=self.c_bg, fg="#ffffff" if primary else TEXT,
                         font=(FONT_SEMI, 10), padx=16, pady=7, cursor="hand2")
        self.command = command
        self.bind("<Enter>", lambda e: self.config(bg=self.c_hover))
        self.bind("<Leave>", lambda e: self.config(bg=self.c_bg))
        self.bind("<Button-1>", lambda e: self.command and self.command())


class Slider(tk.Canvas):
    """Minimal horizontal slider (0..maximum)."""

    def __init__(self, master, width=120, maximum=100, value=80, command=None, bg=PANEL):
        super().__init__(master, width=width, height=22, bg=bg, highlightthickness=0, cursor="hand2")
        self.maximum, self.value, self.command = maximum, value, command
        self.w = width
        self.bind("<Button-1>", self._drag)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<MouseWheel>", self._wheel)
        self._draw()

    def _x(self, v):
        return 8 + (self.w - 16) * (v / self.maximum)

    def _draw(self):
        self.delete("all")
        y = 11
        self.create_line(8, y, self.w - 8, y, fill=TRACK, width=4, capstyle="round")
        x = self._x(self.value)
        self.create_line(8, y, x, y, fill=ACCENT, width=4, capstyle="round")
        self.create_oval(x - 6, y - 6, x + 6, y + 6, fill="#ffffff", outline="")

    def _drag(self, e):
        self.set((e.x - 8) / max(1, self.w - 16) * self.maximum, notify=True)

    def _wheel(self, e):
        self.set(self.value + (5 if e.delta > 0 else -5), notify=True)

    def set(self, v, notify=False):
        self.value = max(0, min(self.maximum, v))
        self._draw()
        if notify and self.command:
            self.command(self.value)


class Toggle(tk.Frame):
    def __init__(self, master, text: str, value: bool, command=None, bg=PANEL):
        super().__init__(master, bg=bg, cursor="hand2")
        self.value, self.command = value, command
        self.cv = tk.Canvas(self, width=34, height=18, bg=bg, highlightthickness=0)
        self.cv.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=bg, fg=MUTED, font=(FONT, 9))
        self.lbl.pack(side="left", padx=(6, 0))
        for w in (self, self.cv, self.lbl):
            w.bind("<Button-1>", self._click)
        self._draw()

    def _draw(self):
        c = self.cv
        c.delete("all")
        col = ACCENT if self.value else TRACK
        c.create_oval(1, 1, 17, 17, fill=col, outline="")
        c.create_oval(17, 1, 33, 17, fill=col, outline="")
        c.create_rectangle(9, 1, 25, 17, fill=col, outline="")
        x = 25 if self.value else 9
        c.create_oval(x - 6, 3, x + 6, 15, fill="#ffffff", outline="")

    def _click(self, _e=None):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)


class ProgressLine(tk.Canvas):
    def __init__(self, master, height=4, bg=PANEL, fill=ACCENT):
        super().__init__(master, height=height, bg=bg, highlightthickness=0)
        self.frac, self.fill = 0.0, fill
        self.bind("<Configure>", lambda e: self._draw())

    def set(self, frac: float):
        self.frac = max(0.0, min(1.0, frac))
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_width(), int(self["height"])
        self.create_rectangle(0, 0, w, h, fill=TRACK, outline="")
        if self.frac > 0:
            self.create_rectangle(0, 0, int(w * self.frac), h, fill=self.fill, outline="")


class ScrollList(tk.Frame):
    """Vertically scrolling container with mouse-wheel support."""

    def __init__(self, master, bg=PANEL):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                style="Dark.Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, anchor="nw", window=self.inner)
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind("<Enter>", lambda e: self.bind_all("<MouseWheel>", self._wheel))
        self.bind("<Leave>", lambda e: self.unbind_all("<MouseWheel>"))

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120) * 2, "units")

    def see(self, widget: tk.Widget):
        self.update_idletasks()
        total = max(1, self.inner.winfo_height())
        y, h = widget.winfo_y(), widget.winfo_height()
        top, bottom = self.canvas.yview()
        if y / total < top or (y + h) / total > bottom:
            self.canvas.yview_moveto(max(0, (y - 40) / total))


class ChannelRow(tk.Frame):
    H = 66

    def __init__(self, master, ch: Channel, on_click):
        super().__init__(master, bg=PANEL, height=self.H, cursor="hand2")
        self.pack_propagate(False)
        self.ch = ch
        self.playing = False
        self.badge = tk.Label(self, text=ch.number or "WEB", bg=PANEL2, fg=TEXT,
                              font=(FONT_SEMI, 11), width=5, pady=8)
        self.badge.pack(side="left", padx=(14, 12), pady=10)
        col = tk.Frame(self, bg=PANEL)
        col.pack(side="left", fill="both", expand=True, pady=(9, 8), padx=(0, 14))
        top = tk.Frame(col, bg=PANEL)
        top.pack(fill="x")
        self.net = tk.Label(top, text=ch.network or ch.call_sign, bg=PANEL, fg=TEXT,
                            font=(FONT_SEMI, 10), anchor="w")
        self.net.pack(side="left")
        self.call = tk.Label(top, text=ch.call_sign if ch.network else "", bg=PANEL, fg=DIM,
                             font=(FONT, 8), anchor="w")
        self.call.pack(side="left", padx=(8, 0))
        self.now = tk.Label(col, text="", bg=PANEL, fg=DIM, font=(FONT, 9), anchor="w")
        self.now.pack(fill="x", pady=(1, 3))
        self.bar = ProgressLine(col, height=3, bg=PANEL)
        self.bar.pack(fill="x")
        self._bgs = [self, col, top, self.net, self.call, self.now]
        for w in self._bgs + [self.badge, self.bar]:
            w.bind("<Button-1>", lambda e: on_click(self.ch))
            w.bind("<Enter>", lambda e: self._paint(hover=True))
            w.bind("<Leave>", lambda e: self._paint(hover=False))

    def _paint(self, hover=False):
        bg = ACTIVE if self.playing else (HOVER if hover else PANEL)
        for w in self._bgs:
            w.config(bg=bg)
        self.bar.config(bg=bg)
        self.badge.config(bg=ACCENT if self.playing else PANEL2,
                          fg="#ffffff" if self.playing else TEXT)

    def set_playing(self, playing: bool):
        self.playing = playing
        self._paint()

    def set_guide(self, cur: Airing | None):
        if cur:
            self.now.config(text=ellipsize(cur.title, 40), fg=MUTED)
            total = (cur.end - cur.start).total_seconds() or 1
            self.bar.set((datetime.now(timezone.utc) - cur.start).total_seconds() / total)
        else:
            self.now.config(text="No guide info", fg=DIM)
            self.bar.set(0)


# ---------------------------------------------------------------- dialogs

def _modal(win: tk.Toplevel) -> None:
    try:
        win.wait_visibility()
        win.grab_set()
    except tk.TclError:
        pass


def _entry(master, show: str = "") -> tk.Entry:
    return tk.Entry(master, bg=PANEL2, fg=TEXT, insertbackground=TEXT, relief="flat",
                    font=(FONT, 11), show=show, highlightthickness=1, width=36,
                    highlightbackground=BORDER, highlightcolor=ACCENT)


class LoginDialog(tk.Toplevel):
    def __init__(self, master, email: str = "", error: str = ""):
        super().__init__(master, bg=PANEL)
        self.title("Sign in")
        self.resizable(False, False)
        self.transient(master)
        self.result = None
        frm = tk.Frame(self, bg=PANEL, padx=28, pady=24)
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="Sign in to Tablo", bg=PANEL, fg=TEXT, font=(FONT_SEMI, 16)).pack(anchor="w")
        tk.Label(frm, text="Use the same email and password as your Tablo app.", bg=PANEL,
                 fg=MUTED, font=(FONT, 10)).pack(anchor="w", pady=(2, 14))
        if error:
            tk.Label(frm, text=error, bg=PANEL, fg=LIVE, font=(FONT, 10), wraplength=340,
                     justify="left").pack(anchor="w", pady=(0, 10))
        tk.Label(frm, text="Email", bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        self.email = _entry(frm)
        self.email.insert(0, email)
        self.email.pack(fill="x", ipady=6, pady=(2, 10))
        tk.Label(frm, text="Password", bg=PANEL, fg=MUTED, font=(FONT, 9)).pack(anchor="w")
        self.pw = _entry(frm, show="•")
        self.pw.pack(fill="x", ipady=6, pady=(2, 10))
        self.remember = tk.BooleanVar(value=keyring is not None)
        cb = tk.Checkbutton(frm, text="Remember me on this PC", variable=self.remember, bg=PANEL,
                            fg=MUTED, selectcolor=PANEL2, activebackground=PANEL,
                            activeforeground=TEXT, font=(FONT, 9), bd=0, highlightthickness=0)
        cb.pack(anchor="w")
        if keyring is None:
            cb.config(state="disabled")
        btns = tk.Frame(frm, bg=PANEL)
        btns.pack(fill="x", pady=(18, 0))
        PillButton(btns, "Sign in", self._ok).pack(side="right")
        PillButton(btns, "Cancel", self.destroy, primary=False).pack(side="right", padx=8)
        self.bind("<Return>", lambda e: self._ok())
        self.bind("<Escape>", lambda e: self.destroy())
        (self.pw if email else self.email).focus_set()
        _modal(self)
        self.wait_window()

    def _ok(self):
        e, p = self.email.get().strip(), self.pw.get()
        if e and p:
            self.result = (e, p, self.remember.get())
            self.destroy()


class DevicePicker(tk.Toplevel):
    def __init__(self, master, devices: list[Device]):
        super().__init__(master, bg=PANEL)
        self.title("Choose your Tablo")
        self.transient(master)
        self.result = None
        frm = tk.Frame(self, bg=PANEL, padx=24, pady=20)
        frm.pack(fill="both", expand=True)
        tk.Label(frm, text="Which Tablo?", bg=PANEL, fg=TEXT, font=(FONT_SEMI, 14)).pack(anchor="w", pady=(0, 10))
        for d in devices:
            PillButton(frm, f"{d.name}   ·   {d.url}", lambda d=d: self._pick(d),
                       primary=False).pack(fill="x", pady=3)
        _modal(self)
        self.wait_window()

    def _pick(self, d):
        self.result = d
        self.destroy()


# ---------------------------------------------------------------- main window

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.withdraw()
        self.title(APP_NAME)
        self.configure(bg=BG)
        self.geometry("1280x760")
        self.minsize(960, 600)
        families = set(tkfont.families(self))
        self.icon_font_name = next((f for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets") if f in families), None)
        try:
            self._icon = make_app_icon()
            self.iconphoto(True, self._icon)
        except tk.TclError:
            pass
        self._style()

        self.cfg = load_config()
        self.session: TabloSession | None = None
        self.channels: list[Channel] = []
        self.rows: dict[str, ChannelRow] = {}
        self.guide: dict[str, list[Airing]] = {}
        self.guide_fetched: dict[str, datetime] = {}
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.jobs: queue.Queue = queue.Queue()
        self.player: MpvPlayer | None = None
        self.playing: Channel | None = None
        self.tune_req = 0
        self._start_lock = threading.Lock()
        self._loaded_at = 0.0
        self._sid = None
        self.fullscreen = False
        self.ps = {"pause": False, "volume": float(self.cfg.get("volume", 80)), "mute": False,
                      "buffering": False, "idle": True, "ahead": 0.0,
                      "captions": False}  # closed captions always start off

        self._build()
        self._render_nowbar()
        self.deiconify()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._pump)
        self.after(30_000, self._tick)
        self.after(150, self.start_sign_in)

    # ---------- styling / layout ----------

    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure("Dark.Vertical.TScrollbar", background=PANEL2, troughcolor=PANEL,
                     bordercolor=PANEL, arrowcolor=MUTED, lightcolor=PANEL2, darkcolor=PANEL2,
                     gripcount=0, arrowsize=12)
        st.map("Dark.Vertical.TScrollbar", background=[("active", HOVER)])

    def _build(self):
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.grid_propagate(False)  # keep the window size fixed when panels hide

        # header
        self.header = tk.Frame(self, bg=PANEL, height=58)
        self.header.grid(row=0, column=0, columnspan=2, sticky="ew")
        self.header.pack_propagate(False)
        logo = tk.Frame(self.header, bg=PANEL)
        logo.pack(side="left", padx=(18, 0))
        tk.Label(logo, text="TABLO", bg=PANEL, fg=TEXT, font=(FONT, 15, "bold")).pack(side="left")
        tk.Label(logo, text="VIEWER", bg=PANEL, fg=ACCENT, font=(FONT, 15)).pack(side="left", padx=(5, 0))
        self.device_lbl = tk.Label(self.header, text="Not signed in", bg=PANEL2, fg=MUTED,
                                   font=(FONT, 9), padx=10, pady=3)
        self.device_lbl.pack(side="left", padx=18)

        self.menu_btn = IconButton(self.header, "more", self._show_menu, size=14)
        self.menu_btn.pack(side="right", padx=(4, 12))
        IconButton(self.header, "refresh", self.refresh_channels, size=13).pack(side="right")
        self.ott_toggle = Toggle(self.header, "Free streaming channels",
                                 self.cfg.get("show_streaming", False), self._toggle_ott)
        self.ott_toggle.pack(side="right", padx=16)

        sbox = tk.Frame(self.header, bg=PANEL2, highlightthickness=1, highlightbackground=BORDER)
        sbox.pack(side="right", pady=13)
        sicon = tk.Label(sbox, bg=PANEL2, fg=DIM)
        if self.icon_font_name:
            sicon.config(text=ICON_GLYPHS["search"], font=(self.icon_font_name, 10))
        else:
            sicon.config(text="⌕", font=(FONT, 11))
        sicon.pack(side="left", padx=(8, 0))
        self.search = tk.StringVar()
        self.search_entry = tk.Entry(sbox, textvariable=self.search, bg=PANEL2, fg=TEXT, width=24,
                                     insertbackground=TEXT, relief="flat", font=(FONT, 10),
                                     highlightthickness=0, bd=0)
        self.search_entry.pack(side="left", padx=8, ipady=3)
        self.search.trace_add("write", lambda *a: self._render_list())

        # main area
        self.main = tk.Frame(self, bg=BG)
        self.main.grid(row=1, column=0, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(0, weight=1)
        self.main.grid_propagate(False)

        self.video = tk.Frame(self.main, bg="#000000")
        self.video.grid(row=0, column=0, sticky="nsew")
        self.placeholder = tk.Label(self.video, bg="#000000", fg=DIM, font=(FONT, 14),
                                    text="Pick a channel from the guide")
        self.placeholder.place(relx=0.5, rely=0.5, anchor="center")
        self.video.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())
        self.placeholder.bind("<Double-Button-1>", lambda e: self.toggle_fullscreen())

        self._build_nowbar()
        self._build_controls()

        # guide sidebar
        self.sidebar = tk.Frame(self, bg=PANEL, width=372)
        self.sidebar.grid(row=1, column=1, sticky="ns")
        self.sidebar.pack_propagate(False)
        tk.Frame(self.sidebar, bg=BORDER, width=1).pack(side="left", fill="y")
        sh = tk.Frame(self.sidebar, bg=PANEL)
        sh.pack(fill="x", padx=16, pady=(14, 10))
        tk.Label(sh, text="Channel guide", bg=PANEL, fg=TEXT, font=(FONT_SEMI, 12)).pack(side="left")
        self.clock = tk.Label(sh, text="", bg=PANEL, fg=DIM, font=(FONT, 9))
        self.clock.pack(side="right")
        tk.Frame(self.sidebar, bg=BORDER, height=1).pack(fill="x")
        self.list = ScrollList(self.sidebar, bg=PANEL)
        self.list.pack(fill="both", expand=True)
        self.empty_lbl = tk.Label(self.list.inner, text="Signing in…", bg=PANEL, fg=DIM, font=(FONT, 10))
        self.empty_lbl.pack(pady=40)
        self._update_clock()

        # keyboard
        self.bind_all("<Key>", self._on_key, add="+")

    def _build_nowbar(self):
        nb = tk.Frame(self.main, bg=PANEL, height=96)
        nb.grid(row=1, column=0, sticky="ew")
        nb.pack_propagate(False)
        self.nowbar = nb
        self.np_badge = tk.Label(nb, text="—", bg=PANEL2, fg=TEXT, font=(FONT_SEMI, 15), width=5, pady=12)
        self.np_badge.pack(side="left", padx=(20, 16), pady=16)
        right = tk.Frame(nb, bg=PANEL, width=260)
        right.pack(side="right", fill="y", padx=(10, 20), pady=16)
        right.pack_propagate(False)
        tk.Label(right, text="UP NEXT", bg=PANEL, fg=DIM, font=(FONT, 8, "bold"), anchor="w").pack(fill="x")
        self.np_next = tk.Label(right, text="", bg=PANEL, fg=MUTED, font=(FONT, 10), anchor="nw",
                                justify="left", wraplength=250)
        self.np_next.pack(fill="both", expand=True, pady=(2, 0))
        mid = tk.Frame(nb, bg=PANEL)
        mid.pack(side="left", fill="both", expand=True, pady=(14, 12))
        line1 = tk.Frame(mid, bg=PANEL)
        line1.pack(fill="x")
        self.np_live = tk.Label(line1, text="", bg=PANEL, fg="#ffffff", font=(FONT, 8, "bold"), padx=6)
        self.np_live.pack(side="left", padx=(0, 8))
        self.np_live.bind("<Button-1>", lambda e: self.go_live())
        self.np_title = tk.Label(line1, text="", bg=PANEL, fg=TEXT, font=(FONT_SEMI, 15), anchor="w")
        self.np_title.pack(side="left", fill="x")
        self.np_sub = tk.Label(mid, text="", bg=PANEL, fg=MUTED, font=(FONT, 10), anchor="w")
        self.np_sub.pack(fill="x", pady=(2, 6))
        self.np_bar = ProgressLine(mid, height=4, bg=PANEL)
        self.np_bar.pack(fill="x")

    def _build_controls(self):
        cb = tk.Frame(self.main, bg=PANEL, height=54)
        cb.grid(row=2, column=0, sticky="ew")
        cb.pack_propagate(False)
        self.controls = cb
        tk.Frame(cb, bg=BORDER, height=1).pack(side="top", fill="x")
        left = tk.Frame(cb, bg=PANEL)
        left.pack(side="left", padx=10)
        IconButton(left, "up", lambda: self.channel_step(-1), size=12).pack(side="left")
        IconButton(left, "down", lambda: self.channel_step(1), size=12).pack(side="left")
        tk.Frame(left, bg=BORDER, width=1, height=24).pack(side="left", padx=8)
        IconButton(left, "back", lambda: self.seek(-10), size=15).pack(side="left")
        self.play_btn = IconButton(left, "pause", self.toggle_pause, size=18, padx=14)
        self.play_btn.pack(side="left", padx=2)
        IconButton(left, "fwd", lambda: self.seek(30), size=15).pack(side="left")
        self.live_btn = PillButton(left, "Go live", self.go_live, primary=False)
        self.live_btn.config(font=(FONT_SEMI, 9), padx=10, pady=4)
        self.live_btn.pack(side="left", padx=10)

        right = tk.Frame(cb, bg=PANEL)
        right.pack(side="right", padx=10)
        self.full_btn = IconButton(right, "full", self.toggle_fullscreen, size=14)
        self.full_btn.pack(side="right")
        tk.Frame(right, bg=BORDER, width=1, height=24).pack(side="right", padx=8)
        self.vol_lbl = tk.Label(right, text="", bg=PANEL, fg=MUTED, font=(FONT, 9), width=5, anchor="w")
        self.vol_lbl.pack(side="right")
        self.vol = Slider(right, width=130, maximum=130, value=self.ps["volume"], command=self.set_volume)
        self.vol.pack(side="right", padx=(0, 6))
        self.mute_btn = IconButton(right, "vol", self.toggle_mute, size=14)
        self.mute_btn.pack(side="right")
        tk.Frame(right, bg=BORDER, width=1, height=24).pack(side="right", padx=8)
        self.cc_btn = tk.Label(right, text="CC", font=(FONT_SEMI, 9), padx=7, pady=2, cursor="hand2",
                               highlightthickness=1)
        self.cc_btn.pack(side="right")
        self.cc_btn.bind("<Button-1>", lambda e: self.toggle_captions())
        self._render_captions()
        self.status = tk.Label(cb, text="", bg=PANEL, fg=DIM, font=(FONT, 9))
        self.status.pack(side="right", padx=16)
        self._render_volume()

    # ---------- menu ----------

    def _show_menu(self):
        m = tk.Menu(self, tearoff=False, bg=PANEL2, fg=TEXT, activebackground=ACCENT,
                    activeforeground="#ffffff", bd=0, font=(FONT, 10))
        m.add_command(label="Refresh channels    F5", command=self.refresh_channels)
        m.add_command(label="Switch Tablo…", command=self.switch_device)
        m.add_command(label="Locate mpv.exe…", command=self.choose_player)
        m.add_separator()
        m.add_command(label="Keyboard shortcuts", command=self.show_shortcuts)
        m.add_command(label="Open log folder", command=self.open_logs)
        m.add_command(label="About", command=self.about)
        m.add_separator()
        m.add_command(label="Sign out", command=self.sign_out)
        x = self.menu_btn.winfo_rootx() - 170
        y = self.menu_btn.winfo_rooty() + self.menu_btn.winfo_height()
        try:
            m.tk_popup(x, y)
        finally:
            m.grab_release()

    # ---------- background work ----------

    def post(self, fn, *args):
        """Run fn(*args) on the UI thread."""
        self.jobs.put((lambda _r: fn(*args), None, None, ""))

    def run_bg(self, fn, on_ok=None, on_err=None, busy: str = ""):
        if busy:
            self.set_status(busy)
        name = getattr(fn, "__name__", "task")

        def worker():
            try:
                res = fn()
                self.jobs.put((on_ok, res, None, busy))
            except Exception as e:  # noqa: BLE001
                log.exception("background task %s failed", name)
                self.jobs.put((on_err, None, e, busy))
        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        try:
            while True:
                try:
                    cb, res, err, busy = self.jobs.get_nowait()
                except queue.Empty:
                    break
                if busy and self.status.cget("text") == busy:
                    self.set_status("")
                try:
                    if err is not None:
                        (cb or self._error)(err)
                    elif cb:
                        cb(res)
                except Exception as e:  # noqa: BLE001
                    log.exception("UI callback failed")
                    self.set_status(f"Error: {e}")
        except Exception:  # noqa: BLE001
            log.exception("pump failed")
        finally:
            self.after(60, self._pump)

    def report_callback_exception(self, exc, val, tb):
        log.error("Tk callback error", exc_info=(exc, val, tb))
        self.set_status(f"Error: {val}")

    def set_status(self, text: str):
        self.status.config(text=text)

    def _error(self, err: Exception):
        if isinstance(err, TabloAuthError):
            messagebox.showwarning(APP_NAME, f"{err}\n\nPlease sign in again.", parent=self)
            self.start_sign_in(force_prompt=True)
        elif isinstance(err, (TabloError, RuntimeError)):
            messagebox.showerror(APP_NAME, str(err), parent=self)
        else:
            messagebox.showerror(APP_NAME, f"Unexpected error: {err}\n\nDetails are in the log "
                                 f"(⋯ menu → Open log folder).", parent=self)

    # ---------- sign-in ----------

    def start_sign_in(self, force_prompt: bool = False, error: str = ""):
        email = self.cfg.get("email", "")
        pw = None if force_prompt else get_saved_password(email)
        remember = True
        if not pw:
            dlg = LoginDialog(self, email=email, error=error)
            if not dlg.result:
                self.channels = []
                self._render_list(message="Not signed in.\nClick ⟳ to sign in.")
                return
            email, pw, remember = dlg.result
        session = TabloSession()

        def ok(devices):
            self.cfg["email"] = email
            save_config(self.cfg)
            set_saved_password(email, pw if remember else None)
            self.session = session
            self._pick_device(devices)

        def fail(err):
            if isinstance(err, TabloAuthError):
                set_saved_password(email, None)
                self.start_sign_in(force_prompt=True, error=str(err))
            else:
                self._error(err)

        self.run_bg(lambda: session.login(email, pw), ok, fail, busy="Signing in…")

    def _pick_device(self, devices: list[Device]):
        dev = next((d for d in devices if d.sid == self.cfg.get("device_sid")), None)
        if not dev:
            dev = devices[0] if len(devices) == 1 else (DevicePicker(self, devices).result or devices[0])
        self.cfg["device_sid"] = dev.sid
        save_config(self.cfg)

        def ok(_):
            self.device_lbl.config(text=f"●  {dev.name}", fg=TEXT)
            self.refresh_channels()
        self.run_bg(lambda: self.session.select_device(dev), ok, busy=f"Connecting to {dev.name}…")

    def switch_device(self):
        self.cfg.pop("device_sid", None)
        self.start_sign_in()

    def sign_out(self):
        self.stop_playback()
        set_saved_password(self.cfg.get("email", ""), None)
        self.session = None
        self.channels = []
        self._render_list()
        self.device_lbl.config(text="Not signed in", fg=MUTED)
        self.start_sign_in(force_prompt=True)

    # ---------- channels & guide ----------

    def _toggle_ott(self, value: bool):
        self.cfg["show_streaming"] = value
        save_config(self.cfg)
        self.refresh_channels()

    def refresh_channels(self):
        if not self.session or not self.session.lighthouse:
            self.start_sign_in()
            return
        inc = self.ott_toggle.value

        def ok(chs):
            self.channels = chs
            self._render_list()
            self.fetch_guide(force=True)
        self.run_bg(lambda: self.session.channels(include_streaming=inc), ok, busy="Loading channels…")

    def _visible_channels(self) -> list[Channel]:
        q = self.search.get().strip().lower()
        if not q:
            return self.channels
        out = []
        for c in self.channels:
            cur, _ = current_and_next(self.guide.get(c.identifier, []))
            hay = f"{c.call_sign} {c.network} {c.number} {cur.title if cur else ''}".lower()
            if q in hay:
                out.append(c)
        return out

    def _render_list(self, message: str = ""):
        for w in self.list.inner.winfo_children():
            w.destroy()
        self.rows = {}
        vis = self._visible_channels()
        if not vis:
            msg = message or ("No channels match your search." if self.channels else "No channels yet.")
            tk.Label(self.list.inner, text=msg, bg=PANEL, fg=DIM, font=(FONT, 10),
                     justify="center").pack(pady=40)
            return
        for c in vis:
            row = ChannelRow(self.list.inner, c, self.tune)
            row.pack(fill="x")
            tk.Frame(self.list.inner, bg=BORDER, height=1).pack(fill="x", padx=14)
            if c.identifier in self.guide:
                row.set_guide(current_and_next(self.guide[c.identifier])[0])
            row.set_playing(self.playing is not None and c.identifier == self.playing.identifier)
            self.rows[c.identifier] = row
        self.list.canvas.yview_moveto(0)

    def fetch_guide(self, force: bool = False):
        if not self.session:
            return
        session = self.session
        now = datetime.now()
        stale = [c for c in self.channels
                 if force or c.identifier not in self.guide_fetched
                 or now - self.guide_fetched[c.identifier] > timedelta(minutes=20)
                 or current_and_next(self.guide.get(c.identifier, []))[0] is None
                 and now - self.guide_fetched[c.identifier] > timedelta(minutes=5)]
        for c in stale[:150]:
            self.guide_fetched.setdefault(c.identifier, now)
            self.pool.submit(self._fetch_one, session, c.identifier)

    def _fetch_one(self, session: TabloSession, cid: str):
        today = datetime.now().date()
        days = [today - timedelta(days=1), today]
        if datetime.now().hour >= 21:
            days.append(today + timedelta(days=1))
        items: list[Airing] = []
        for d in days:
            try:
                items += session.airings(cid, d)
            except Exception as e:  # noqa: BLE001
                log.info("guide %s %s: %s", cid, d, e)
        seen, uniq = set(), []
        for a in sorted(items, key=lambda x: x.start):
            if a.start not in seen:
                seen.add(a.start)
                uniq.append(a)
        self.post(self._guide_loaded, cid, uniq)

    def _guide_loaded(self, cid: str, items: list[Airing]):
        self.guide[cid] = items
        self.guide_fetched[cid] = datetime.now()
        row = self.rows.get(cid)
        if row:
            row.set_guide(current_and_next(items)[0])
        if self.playing and self.playing.identifier == cid:
            self._render_nowbar()

    def _tick(self):
        try:
            for cid, row in self.rows.items():
                if cid in self.guide:
                    row.set_guide(current_and_next(self.guide[cid])[0])
            self._render_nowbar()
            self._update_clock()
            self.fetch_guide()
        finally:
            self.after(30_000, self._tick)

    def _update_clock(self):
        self.clock.config(text=datetime.now().strftime("%a  %I:%M %p").replace("  0", "  "))

    # ---------- now playing ----------

    def _render_nowbar(self):
        ch = self.playing
        if not ch:
            self.np_badge.config(text="—", bg=PANEL2)
            self.np_title.config(text="Nothing playing")
            self.np_sub.config(text="Pick a channel from the guide on the right.")
            self.np_next.config(text="—")
            self.np_bar.set(0)
            self.np_live.config(text="", bg=PANEL)
            self.np_live.pack_forget()
            return
        self.np_live.pack(side="left", padx=(0, 8), before=self.np_title)
        self.np_badge.config(text=ch.number or "WEB", bg=ACCENT)
        cur, nxt = current_and_next(self.guide.get(ch.identifier, []))
        name = f"{ch.network}  ·  {ch.call_sign}" if ch.network else ch.call_sign
        if cur:
            self.np_title.config(text=ellipsize(cur.title, 60))
            parts = [f"{fmt_time(cur.start)} – {fmt_time(cur.end)}", name]
            if cur.episode:
                parts.insert(1, cur.episode)
            self.np_sub.config(text="   ·   ".join(parts))
            total = (cur.end - cur.start).total_seconds() or 1
            self.np_bar.set((datetime.now(timezone.utc) - cur.start).total_seconds() / total)
        else:
            self.np_title.config(text=name)
            self.np_sub.config(text="Loading guide…" if ch.identifier not in self.guide
                               else "No guide information for this channel")
            self.np_bar.set(0)
        self.np_next.config(text=f"{fmt_time(nxt.start)}   {ellipsize(nxt.title, 60)}" if nxt else "—")
        self._render_state()

    def _render_state(self):
        s = self.ps
        self.play_btn.set_icon("play" if s["pause"] else "pause", 18)
        if not self.playing:
            return
        if s["idle"]:
            self.np_live.config(text=" TUNING ", bg=TRACK)
        elif s["buffering"]:
            self.np_live.config(text=" BUFFERING ", bg=TRACK)
        elif s["pause"]:
            self.np_live.config(text=" PAUSED ", bg=TRACK)
        elif s["ahead"] > 12:
            self.np_live.config(text=" BEHIND LIVE ", bg=TRACK)
        else:
            self.np_live.config(text=" ● LIVE ", bg=LIVE)

    def _render_volume(self):
        s = self.ps
        self.vol.set(s["volume"])
        self.vol_lbl.config(text="Muted" if s["mute"] else f"{int(s['volume'])}%")
        self.mute_btn.set_icon("mute" if s["mute"] or s["volume"] == 0 else "vol", 14)

    # ---------- playback ----------

    def _ensure_player(self) -> MpvPlayer | None:
        if self.player and self.player.running:
            return self.player
        path = find_mpv(self.cfg)
        if not path:
            if messagebox.askyesno(APP_NAME, "mpv wasn't found. It's needed to play video in this "
                                   "window.\n\nRun Setup.bat to install it, or click Yes to locate "
                                   "mpv.exe yourself.", parent=self):
                self.choose_player()
                path = find_mpv(self.cfg)
            if not path:
                return None
        self.update_idletasks()
        self.player = MpvPlayer(path, self.video.winfo_id(), DATA_DIR, self._on_mpv_event,
                                volume=int(self.ps["volume"]))
        return self.player

    def tune(self, ch: Channel):
        if not self.session or not self.session.device:
            self.start_sign_in()
            return
        player = self._ensure_player()
        if not player:
            return
        self.tune_req += 1
        req = self.tune_req
        prev = self.playing
        self.playing = ch
        self.cfg["last_channel"] = ch.identifier
        save_config(self.cfg)
        for cid, row in self.rows.items():
            row.set_playing(cid == ch.identifier)
        self.ps.update(idle=True, buffering=False, pause=False, ahead=0.0)
        self._render_nowbar()
        title = f"{ch.number} {ch.network or ch.call_sign}".strip()
        self.placeholder.config(text=f"Tuning {title}…")
        if ch.identifier not in self.guide:
            self.pool.submit(self._fetch_one, self.session, ch.identifier)
        session = self.session
        start_lock = self._start_lock

        def work():
            url = session.watch(ch.identifier)
            with start_lock:
                if not player.running:
                    player.start()
            return url

        def ok(url):
            if req != self.tune_req:
                return
            log.info("Playing %s", ch.identifier)
            self._loaded_at = time.monotonic()
            player.play(url, title, captions=self.ps["captions"])
            player.set_osc(self.fullscreen)
            if self.ps["mute"]:
                player.command("set_property", "mute", True)

        def fail(err):
            if req != self.tune_req:
                return
            self.playing = prev if prev and self.player and self.player.running else None
            for cid, row in self.rows.items():
                row.set_playing(self.playing is not None and cid == self.playing.identifier)
            self._render_nowbar()
            if not self.playing:
                self.placeholder.config(text="Pick a channel from the guide")
            self._error(err)

        self.run_bg(work, ok, fail, busy=f"Tuning {title}…")

    def stop_playback(self):
        if self.player:
            self.player.stop()
        self.playing = None
        self.placeholder.config(text="Pick a channel from the guide")
        self._render_nowbar()

    def channel_step(self, step: int):
        vis = self._visible_channels()
        if not vis:
            return
        ids = [c.identifier for c in vis]
        if self.playing and self.playing.identifier in ids:
            i = (ids.index(self.playing.identifier) + step) % len(vis)
        else:
            i = 0
        ch = vis[i]
        if ch.identifier in self.rows:
            self.list.see(self.rows[ch.identifier])
        self.tune(ch)

    def toggle_pause(self):
        if self.player and self.playing:
            self.player.command("cycle", "pause")

    def seek(self, secs: int):
        if self.player and self.playing:
            self.player.command("seek", secs, "relative")

    def go_live(self):
        if self.player and self.playing:
            ahead = self.ps["ahead"]
            if ahead > 4:
                self.player.command("seek", round(ahead - 3, 1), "relative")
            self.player.command("set_property", "pause", False)

    def set_volume(self, v: float):
        self.ps["volume"] = round(max(0, min(130, v)))
        if self.ps["mute"] and v > 0:
            self.ps["mute"] = False
            if self.player:
                self.player.command("set_property", "mute", False)
        if self.player:
            self.player.set_volume(self.ps["volume"])
        self._render_volume()

    def toggle_captions(self, value: bool | None = None):
        self.ps["captions"] = (not self.ps["captions"]) if value is None else value
        self._loaded_at = 0.0  # a deliberate choice; don't override it
        if self.player:
            self.player.set_captions(self.ps["captions"])
            if self.playing:
                self.player.show_text("Captions on" if self.ps["captions"] else "Captions off", 1200)
        self._render_captions()

    def _render_captions(self):
        on = self.ps["captions"]
        self.cc_btn.config(bg=ACCENT if on else PANEL, fg="#ffffff" if on else MUTED,
                           highlightbackground=ACCENT if on else DIM)

    def toggle_mute(self):
        self.ps["mute"] = not self.ps["mute"]
        if self.player:
            self.player.command("set_property", "mute", self.ps["mute"])
        self._render_volume()

    def toggle_fullscreen(self, value: bool | None = None):
        value = (not self.fullscreen) if value is None else value
        if value == self.fullscreen:
            return
        self.fullscreen = value
        if value:
            self.header.grid_remove()
            self.sidebar.grid_remove()
            self.nowbar.grid_remove()
            self.controls.grid_remove()
            self.attributes("-fullscreen", True)
            self.full_btn.set_icon("unfull", 14)
        else:
            self.attributes("-fullscreen", False)
            self.header.grid()
            self.sidebar.grid()
            self.nowbar.grid()
            self.controls.grid()
            self.full_btn.set_icon("full", 14)
        if self.player:
            self.player.set_osc(value)
            if value and self.playing:
                cur, _ = current_and_next(self.guide.get(self.playing.identifier, []))
                self.player.show_text(f"{self.playing.number}  {self.playing.network or self.playing.call_sign}"
                                      + (f"   ·   {cur.title}" if cur else ""), 2500)

    # ---------- mpv events (arrive on a background thread) ----------

    def _on_mpv_event(self, msg: dict):
        self.post(self._handle_mpv, msg)

    def _handle_mpv(self, msg: dict):
        ev = msg.get("event")
        if ev == "property-change":
            name, data = msg.get("name"), msg.get("data")
            if name == "pause":
                self.ps["pause"] = bool(data)
            elif name == "volume" and data is not None:
                self.ps["volume"] = float(data)
                self._render_volume()
            elif name == "mute":
                self.ps["mute"] = bool(data)
                self._render_volume()
            elif name == "paused-for-cache":
                self.ps["buffering"] = bool(data)
            elif name == "idle-active":
                self.ps["idle"] = bool(data)
            elif name == "video-params/w" and data:
                self.ps["idle"] = False
                if self.status.cget("text").startswith("Tuning"):
                    self.set_status("")
            elif name == "demuxer-cache-duration":
                self.ps["ahead"] = float(data or 0)
            elif name == "sid":
                # mpv reports False when captions are off, a track number when on.
                # Keep the button honest if mpv's own controls changed it.
                showing = data not in (False, None, "no")
                was_showing = self._sid not in (False, None, "no")
                self._sid = data
                just_tuned = time.monotonic() - self._loaded_at < 20
                if showing and not self.ps["captions"]:
                    if just_tuned:
                        if self.player:
                            self.player.set_captions(False)  # stream switched them on by itself
                    else:
                        self.ps["captions"] = True  # turned on from mpv's on-screen controls
                        self._render_captions()
                elif not showing and was_showing and self.ps["captions"] and not just_tuned:
                    self.ps["captions"] = False  # turned off from mpv's on-screen controls
                    self._render_captions()
            self._render_state()
        elif ev == "client-message":
            args = msg.get("args") or []
            if args[:1] == ["tv"] and len(args) > 1:
                {"fullscreen": lambda: self.toggle_fullscreen(),
                 "exit-fullscreen": lambda: self.toggle_fullscreen(False),
                 "channel-up": lambda: self.channel_step(-1),
                 "channel-down": lambda: self.channel_step(1),
                 "captions": lambda: self.toggle_captions()}.get(args[1], lambda: None)()
        elif ev == "end-file":
            if msg.get("reason") == "error":
                log.warning("mpv end-file error: %s", msg)
                self.set_status("The stream stopped. Pick the channel again to retry.")
                self.placeholder.config(text="Stream stopped — pick a channel to retry")
        elif ev == "player-exited":
            log.warning("mpv exited")
            self.player = None
            if self.playing:
                self.set_status("The video player closed unexpectedly. Pick a channel to restart it.")
            self.playing = None
            self.placeholder.config(text="Pick a channel from the guide")
            self._render_nowbar()
            for row in self.rows.values():
                row.set_playing(False)

    # ---------- keyboard ----------

    def _on_key(self, e):
        if isinstance(e.widget, tk.Entry):
            if e.keysym == "Escape":
                self.search.set("")
                self.focus_set()
            return
        if not isinstance(e.widget, tk.Misc) or e.widget.winfo_toplevel() is not self:
            return  # keys typed in a dialog
        k = e.keysym
        if k in ("space", "k"):
            self.toggle_pause()
        elif k in ("f", "F", "F11"):
            self.toggle_fullscreen()
        elif k == "Escape":
            self.toggle_fullscreen(False)
        elif k in ("m", "M"):
            self.toggle_mute()
        elif k in ("Up", "Prior"):
            self.channel_step(-1)
        elif k in ("Down", "Next"):
            self.channel_step(1)
        elif k == "Left":
            self.seek(-10)
        elif k == "Right":
            self.seek(30)
        elif k in ("plus", "equal", "KP_Add", "0"):
            self.set_volume(self.ps["volume"] + 5)
        elif k in ("minus", "KP_Subtract", "9"):
            self.set_volume(self.ps["volume"] - 5)
        elif k in ("c", "C"):
            self.toggle_captions()
        elif k in ("l", "L"):
            self.go_live()
        elif k == "F5":
            self.refresh_channels()
        elif k == "slash":
            self.search_entry.focus_set()

    # ---------- misc ----------

    def choose_player(self):
        path = filedialog.askopenfilename(parent=self, title="Locate mpv.exe",
                                          filetypes=[("mpv", "mpv.exe"), ("Programs", "*.exe"),
                                                     ("All files", "*.*")])
        if path:
            self.cfg["player_path"] = path
            save_config(self.cfg)

    def open_logs(self):
        if IS_WIN:
            os.startfile(DATA_DIR)  # noqa: S606
        else:
            messagebox.showinfo(APP_NAME, str(DATA_DIR), parent=self)

    def show_shortcuts(self):
        messagebox.showinfo(APP_NAME, (
            "Space        Pause / play\n"
            "← / →        Back 10 s / forward 30 s\n"
            "↑ / ↓        Channel up / down\n"
            "+ / −        Volume (or mouse wheel on the video)\n"
            "M            Mute\n"
            "C            Closed captions on / off\n"
            "L            Jump to live\n"
            "F / F11      Fullscreen (or double-click the video)\n"
            "Esc          Leave fullscreen\n"
            "/            Search channels\n"
            "F5           Refresh channels"), parent=self)

    def about(self):
        dev = self.session.device.url if self.session and self.session.device else "—"
        messagebox.showinfo(APP_NAME, (
            f"{APP_NAME} {APP_VERSION}\n\nUnofficial viewer for Tablo 4th Gen. Not affiliated with "
            "Tablo, Nuvyyo or Scripps. Uses an undocumented API that may change.\n\n"
            f"Tablo address: {dev}\nmpv: {find_mpv(self.cfg) or 'not found'}\n"
            f"Settings & logs: {DATA_DIR}"), parent=self)

    def on_close(self):
        self.cfg["volume"] = int(self.ps["volume"])
        save_config(self.cfg)
        try:
            if self.player:
                self.player.quit()
        finally:
            self.pool.shutdown(wait=False, cancel_futures=True)
            self.destroy()


def selftest() -> int:
    """Used by the build: checks the packaged app can load everything and run mpv."""
    import subprocess
    out = DATA_DIR / "selftest.txt"
    lines = [f"{APP_NAME} {APP_VERSION}", f"frozen={FROZEN}", f"bundle={BUNDLE_DIR}",
             f"keyring={'yes' if keyring else 'no'}"]
    code = 0
    mpv = find_mpv({})
    lines.append(f"mpv={mpv}")
    if not mpv:
        code = 2
    else:
        try:
            r = subprocess.run([mpv, "--no-config", "--version"], capture_output=True, text=True, timeout=30)
            lines.append(f"mpv exit={r.returncode} {r.stdout.splitlines()[0] if r.stdout else ''}")
            code = 0 if r.returncode == 0 else 3
        except Exception as e:  # noqa: BLE001
            lines.append(f"mpv failed: {e}")
            code = 3
    try:
        tk.Tk().destroy()
        lines.append("tk=ok")
    except Exception as e:  # noqa: BLE001
        lines.append(f"tk failed: {e}")
        code = code or 4
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return code


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    log.info("Starting %s %s (Python %s%s)", APP_NAME, APP_VERSION, sys.version.split()[0],
             ", packaged" if FROZEN else "")
    App().mainloop()


if __name__ == "__main__":
    main()
