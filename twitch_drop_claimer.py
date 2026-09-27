"""
Twitch Drop Miner - earns Twitch drops for you and claims them automatically.
Doki Doki Literature Club theme, Monika edition.

  * Campaigns: every drop campaign currently live on Twitch, with search.
    Queue specific drops (e.g. Rust streamer drops) or tick "Mine game".
  * The miner reports watch time (no video is downloaded), shows live progress
    bars and claims each drop as soon as it completes.
  * Multiple Twitch accounts, two themes ("Doki Doki" / "Just Monika"),
    and optional start with Windows.

Run:  pythonw twitch_drop_claimer.py   (or double-click Start.bat)
Only the Python standard library is used.
"""

import os
import queue
import sys
import threading
import traceback
import webbrowser
import tkinter as tk
from datetime import datetime
from tkinter import ttk, messagebox

from twitch_api import (
    APP_VERSION, AccountStore, CLIENT_URL, DROPS_PAGE_URL, SETTINGS_FILE, TwitchClient, TwitchError,
    game_slug, load_settings, save_json,
)
from miner import CampaignStore, Discovery, Miner, fmt_minutes
from doki_theme import (
    ASSETS, MASCOT_H, MASCOT_W, PALETTES, DotCanvas, MenuButton, Sidebar, download_fonts, load_private_fonts,
    make_fonts, missing_fonts, refresh_fonts,
)

try:
    import winreg
except ImportError:  # not on Windows
    winreg = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TwitchDropMiner"
APP_ID = "Moni.TwitchDropMiner"  # taskbar identity; must match the desktop shortcut's AppUserModelID
FILTERS = ["All campaigns", "Queued drops", "Streamer drops", "Picked games", "Available to mine",
           "Completed"]
STATE_COLORS = {
    "Mining": "accent", "Queued": "fg", "Available": "muted", "Offline": "muted", "Not started": "muted",
    "Not progressing": "warn", "Account not linked": "danger", "Upcoming": "muted", "Completed": "success",
    "Ended": "muted",
}
PAD = 16
MAX_LOG_LINES = 2000


# --------------------------------------------------------------------------
# Windows helpers
# --------------------------------------------------------------------------
def startup_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --autostart'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw
    return f'"{exe}" "{os.path.abspath(__file__)}" --autostart'


def get_run_on_startup():
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except OSError:
        return False


def set_run_on_startup(enabled):
    if winreg is None:
        raise OSError("Run-on-startup is only supported on Windows")
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def set_app_id():
    """Give the app its own taskbar identity, so Windows shows our icon (not Python's) and doesn't
    group the window with other Python programs. Must run before the window is created.
    Not needed for the packaged .exe: Windows already gives it its own identity and icon."""
    if sys.platform != "win32" or getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass


def set_titlebar_dark(window, dark):
    """Match the Windows 10/11 title bar to the theme (no-op elsewhere)."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new / old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
        if window.winfo_viewable():  # nudge the frame so it repaints now
            w, h = window.winfo_width(), window.winfo_height()
            window.geometry(f"{w + 1}x{h}")
            window.geometry(f"{w}x{h}")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Campaign card (Campaigns page)
# --------------------------------------------------------------------------
class CampaignCard:
    def __init__(self, app, parent, data):
        self.app = app
        self.id = data["id"]
        self.slug = data["slug"]
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        self.win = None  # canvas window item

        head = ttk.Frame(self.frame, style="CardBody.TFrame")
        head.pack(fill="x")
        ttk.Checkbutton(head, text="Mine game", style="Card.TCheckbutton",
                        variable=app.game_var(data["slug"]),
                        command=lambda: app.on_toggle_game(data["slug"], data["game"])).pack(side="left")
        self.badge = tk.Label(head, padx=8, pady=1, bd=0)
        self.badge.pack(side="right")
        ttk.Label(head, text=data["game"], style="CardTitle.TLabel").pack(side="left", padx=(10, 6))
        ttk.Label(head, text=data["name"], style="CardMuted.TLabel").pack(side="left")

        # Second line: drop type, end date and streamers (kept off the title row so nothing gets clipped)
        meta = ttk.Frame(self.frame, style="CardBody.TFrame")
        meta.pack(fill="x", pady=(6, 0))
        self.kind = tk.Label(meta, padx=6, pady=0, bd=0)
        self.kind.pack(side="left", padx=(0, 10))
        self.ends = ttk.Label(meta, style="CardMuted.TLabel")
        self.ends.pack(side="left")
        self.streamers_frame = ttk.Frame(meta, style="CardBody.TFrame")
        self.streamer_key = None
        self.dots = []

        self.body = ttk.Frame(self.frame, style="CardBody.TFrame")
        self.body.pack(fill="x", pady=(8, 0))
        self.body.columnconfigure(1, weight=1)
        self.drop_ids = None
        self.rows = {}
        self.update(data)

    def _build_rows(self, drops):
        for child in self.body.winfo_children():
            child.destroy()
        self.rows = {}
        for i, d in enumerate(drops):
            ttk.Label(self.body, text=d["name"], style="Card.TLabel", width=26).grid(row=i, column=0, sticky="w",
                                                                                     pady=2)
            bar = ttk.Progressbar(self.body, maximum=100, style="Accent.Horizontal.TProgressbar")
            bar.grid(row=i, column=1, sticky="ew", padx=10, pady=2)
            text = ttk.Label(self.body, style="CardMuted.TLabel", width=14, anchor="e")
            text.grid(row=i, column=2, sticky="e", pady=2)
            btn = ttk.Button(self.body, style="Small.TButton", width=9,
                             command=lambda drop_id=d["id"]: self.app.on_queue_toggle(self.data, drop_id))
            btn.grid(row=i, column=3, sticky="e", padx=(10, 0), pady=1)
            btn.bind("<Button-3>", lambda e, drop_id=d["id"]: self.app.queue_context_for_drop(drop_id, e))
            self.rows[d["id"]] = (bar, text, btn)
        self.drop_ids = [d["id"] for d in drops]

    def _build_streamers(self, streamers):
        for child in self.streamers_frame.winfo_children():
            child.destroy()
        self.dots = []
        if not streamers:
            self.streamers_frame.pack_forget()
            return
        ttk.Label(self.streamers_frame, text="·   Streamers:", style="CardMuted.TLabel").pack(side="left")
        for login, live in streamers[:12]:
            dot = tk.Label(self.streamers_frame, text="●", bd=0, font=self.app.fonts["small"])
            dot.pack(side="left", padx=(8, 2))
            lbl = ttk.Label(self.streamers_frame, text=login, style="Card.TLabel", cursor="hand2")
            lbl.pack(side="left")
            lbl.bind("<Button-1>", lambda e, l=login: webbrowser.open(f"{CLIENT_URL}/{l}"))
            self.dots.append((dot, live))
        if len(streamers) > 12:
            ttk.Label(self.streamers_frame, text=f"+{len(streamers) - 12} more",
                      style="CardMuted.TLabel").pack(side="left", padx=8)
        self.streamers_frame.pack(side="left", padx=(10, 0))

    def update(self, data):
        self.data = data
        self.state = data["state"]
        self.selected = data["selected"]
        self.kind_text = data["kind"]
        self.queued = any(d["queued"] for d in data["drops"])
        self.search_text = " ".join([data["game"], data["name"]] + [d["name"] for d in data["drops"]]
                                    + [s for s, _ in data["streamers"]]).lower()
        if [d["id"] for d in data["drops"]] != self.drop_ids:
            self._build_rows(data["drops"])
        key = tuple(data["streamers"])
        if key != self.streamer_key:
            self.streamer_key = key
            self._build_streamers(data["streamers"])
        for d in data["drops"]:
            bar, text, btn = self.rows[d["id"]]
            if d["claimed"]:
                bar.configure(value=100, style="Done.Horizontal.TProgressbar")
                text.configure(text="Claimed ♥", style="CardSuccess.TLabel")
            else:
                bar.configure(value=100 * d["cur"] / d["req"] if d["req"] else 0,
                              style="Accent.Horizontal.TProgressbar" if d["status"] == "active"
                              else "Idle.Horizontal.TProgressbar")
                label = {"active": f"{d['cur']}/{d['req']} min", "upcoming": "Not started",
                         "expired": "Expired"}[d["status"]]
                text.configure(text=label, style="CardMuted.TLabel")
            if d["queued"]:
                btn.configure(text=f"#{d['queued']}  ✕", style="SmallAccent.TButton", state="normal")
            else:
                btn.configure(text="+ Queue", style="Small.TButton",
                              state="disabled" if d["claimed"] or d["status"] == "expired" else "normal")
        self.ends.configure(text=f"Ends {data['ends']}" if data["ends"] else "")
        self.refresh_theme()

    def refresh_theme(self):
        p, fonts = self.app.p, self.app.fonts
        color = p[STATE_COLORS.get(self.state, "muted")]
        filled = self.state in ("Mining", "Completed", "Account not linked", "Not progressing")
        self.badge.configure(text=self.state, font=fonts["badge"], bg=color if filled else p["panel"],
                             fg="#ffffff" if filled else color)
        if self.kind_text:
            streamer = self.kind_text == "Streamer drop"
            self.kind.configure(text=self.kind_text.upper(), font=fonts["badge"],
                                bg=p["select"] if streamer else p["trough"],
                                fg=p["heading"] if streamer else p["muted"])
            self.kind.pack(side="left", padx=(0, 10), before=self.ends)
        else:
            self.kind.pack_forget()
        for dot, live in self.dots:
            dot.configure(bg=p["panel"], fg=p["accent"] if live else p["muted"])


# --------------------------------------------------------------------------
# One logged-in account: its API client, campaign store and (optional) miner
# --------------------------------------------------------------------------
class Session:
    def __init__(self, app, account):
        self.login = account.get("login")  # None for an old single-account file until validated
        self.client = TwitchClient(app.settings, app.accounts.device_id, account)
        self.store = CampaignStore(self.client, app.settings, app.emitter(self), app.discovery)
        self.miner = None
        self.state = "checking"      # checking -> ready | expired
        self.scanning = False
        self.status = "Checking login..."
        self.watching = ""           # short "now watching" text for the accounts table
        self.last = {}               # latest payload per event kind, replayed when this account is viewed

    @property
    def name(self):
        return self.login or "(checking...)"


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------
class App(tk.Tk):
    # The UI always shows one account (self.view); these point at it.
    client = property(lambda self: self.view.client if self.view else None)
    store = property(lambda self: self.view.store if self.view and self.view.state == "ready" else None)
    miner = property(lambda self: self.view.miner if self.view else None)
    scanning = property(lambda self: bool(self.view and self.view.scanning))
    def __init__(self, autostart=False):
        load_private_fonts()
        set_app_id()
        super().__init__()
        self.title("Twitch Drop Miner")
        self.geometry("1400x860")
        self.minsize(1150, 700)
        self.report_callback_exception = self._on_tk_error

        self.settings = load_settings()
        save_json(SETTINGS_FILE, self.settings)  # writes defaults so users can edit
        self.accounts = AccountStore()
        self.events = queue.Queue()
        self.discovery = Discovery(self.settings, self.emit)  # one campaign scan shared by all accounts
        self.sessions = []           # one Session per logged-in account
        self.view = None             # the session whose progress / campaigns the UI shows
        self.mining = False          # Start mining pressed (applies to every account ticked "Mine")
        self.login_cancel = threading.Event()
        self.cards = {}
        self.card_order = []
        self.game_vars = {}
        self.queue_rows = []
        self.tk_widgets = []         # plain tk widgets that need manual recoloring
        self.dot_canvases = []
        self.menu_buttons = []
        self.pages = {}
        self.page = None
        self._laying_out = False

        self.fonts = make_fonts(self)
        self.p = PALETTES["monika" if self.settings.get("dark_mode") else "doki"]
        self.images = {}
        for name in ("monika", "monika_glow", "icon_256", "icon_32"):
            try:
                self.images[name] = tk.PhotoImage(master=self, file=os.path.join(ASSETS, f"{name}.png"))
            except tk.TclError:
                self.images[name] = None
        self.mascot_img = None
        icons = [img for img in (self.images["icon_256"], self.images["icon_32"]) if img is not None]
        if icons:
            # Big one for the taskbar / Alt-Tab, small one for the title bar (and dialogs).
            # Not iconbitmap(icon.ico): Tk can't read the PNG-compressed .ico and shows a blank icon.
            # icon.ico is only for the Windows shortcuts.
            self.iconphoto(True, *icons)

        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self._build_ui()
        self.apply_theme()

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.after(100, self._process_events)
        self.after(60_000, self._idle_rescan)
        if autostart and self.settings.get("start_minimized"):
            self.iconify()
        self.after(150, self._restore_account)
        if missing_fonts():
            self._bg(self._fetch_fonts)

    def _fetch_fonts(self):
        """First launch: download Riffic + Aller (not redistributable, so not bundled)."""
        self.log("Downloading the Doki Doki fonts (first launch only)...")
        try:
            ok = download_fonts()
        except Exception as e:
            self.log(f"Couldn't download the fonts ({e}) - using Windows fonts; will retry next launch")
            return
        self.emit("fonts_ready", ok)

    def _ui_fonts_ready(self, ok):
        if ok and refresh_fonts(self, self.fonts):
            self.apply_theme()  # re-measure / redraw everything drawn on canvases
            self.log("Fonts installed for this app ♥")

    # ======================================================================
    # Layout
    # ======================================================================
    def _build_ui(self):
        self.sidebar = Sidebar(self, self, self.on_menu)
        self.sidebar.pack(side="left", fill="y")
        self.content = tk.Frame(self, bd=0, highlightthickness=0)
        self.content.pack(side="left", fill="both", expand=True)
        self.tk_widgets.append((self.content, "bg"))
        self.pages["mining"] = self._build_mining_page()
        self.pages["campaigns"] = self._build_campaigns_page()
        self.pages["settings"] = self._build_settings_page()
        self.show_page("mining")

    def panel(self, parent, title=None):
        """White (or dark) box with a pink border, like DDLC's text boxes."""
        outer = ttk.Frame(parent, style="Panel.TFrame", padding=(14, 8 if title else 12, 14, 12))
        if title:
            ttk.Label(outer, text=title, style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 6))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        return outer, body

    def _build_mining_page(self):
        pg = DotCanvas(self.content, self, mascot=True)
        pg.columnconfigure(0, weight=2)
        pg.columnconfigure(1, weight=3)
        pg.columnconfigure(2, minsize=MASCOT_W)
        pg.rowconfigure(3, weight=1, minsize=MASCOT_H + 10)

        outer, acc = self.panel(pg, "Accounts")
        outer.grid(row=0, column=0, sticky="nsew", padx=(PAD, 8), pady=(PAD, 8))
        self.acct_tree = ttk.Treeview(acc, columns=("mine", "account", "status"), show="headings", height=3,
                                      selectmode="browse")
        for col, text, width, anchor, stretch in (("mine", "Mine", 50, "center", False),
                                                  ("account", "Account", 130, "w", False),
                                                  ("status", "Status", 230, "w", True)):
            self.acct_tree.heading(col, text=text)
            self.acct_tree.column(col, width=width, anchor=anchor, stretch=stretch)
        self.acct_tree.pack(fill="x")
        self.acct_tree.bind("<ButtonPress-1>", self._on_acct_click)
        row = ttk.Frame(acc)
        row.pack(fill="x", pady=(6, 0))
        self.add_btn = MenuButton(row, self, "Add account", self.on_add_account)
        self.add_btn.pack(side="left")
        MenuButton(row, self, "Remove", self.on_remove_account).pack(side="left", padx=(8, 0))
        self.account_status_var = tk.StringVar(value="Not logged in")
        ttk.Label(row, textvariable=self.account_status_var, style="Muted.TLabel").pack(side="left", padx=(12, 0))

        outer, ctl = self.panel(pg, "Mining")
        outer.grid(row=0, column=1, columnspan=2, sticky="nsew", padx=(8, PAD), pady=(PAD, 8))
        row = ttk.Frame(ctl)
        row.pack(fill="x")
        self.start_btn = MenuButton(row, self, "Start mining", self.on_start_stop, size="button_big")
        self.start_btn.pack(side="left")
        self.refresh_btn = MenuButton(row, self, "Refresh campaigns", self.on_refresh)
        self.refresh_btn.pack(side="left", padx=14)
        self.mining_summary_var = tk.StringVar(value="Not mining")
        ttk.Label(ctl, textvariable=self.mining_summary_var, style="Link.TLabel").pack(anchor="w", pady=(6, 0))
        self.mining_status_var = tk.StringVar(value="Idle")
        ttk.Label(ctl, textvariable=self.mining_status_var, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        ttk.Label(ctl, text="Start mining runs every account ticked \"Mine\". Click an account to see its "
                            "progress and campaigns below.", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

        self.blocked_banner = tk.Label(
            pg, anchor="w", justify="left", padx=12, pady=8, wraplength=1000,
            text="Watch time isn't reaching Twitch: spade.twitch.tv is blocked by your DNS "
                 "(Mullvad: turn off BOTH the Ads and Trackers DNS content blockers; or Pi-hole / AdGuard). "
                 "Drops won't progress until it's unblocked - this clears by itself within ~5 minutes.")
        self.blocked_banner.grid(row=1, column=0, columnspan=3, sticky="ew", padx=PAD, pady=(0, 8))
        self.blocked_banner.grid_remove()
        self.tk_widgets.append((self.blocked_banner, "banner"))

        outer, prog = self.panel(pg, "Progress")
        outer.grid(row=2, column=0, columnspan=3, sticky="nsew", padx=PAD, pady=(0, 8))
        prog.columnconfigure(1, weight=1)
        ttk.Label(prog, text="Channel", style="Muted.TLabel", width=10).grid(row=0, column=0, sticky="w")
        self.channel_var = tk.StringVar(value="Not watching")
        link = ttk.Label(prog, textvariable=self.channel_var, style="Link.TLabel", cursor="hand2")
        link.grid(row=0, column=1, columnspan=2, sticky="w")
        link.bind("<Button-1>", self._open_channel)
        self.watch_login = None
        self.camp_title_var, self.camp_text_var = tk.StringVar(value="-"), tk.StringVar()
        self.drop_title_var, self.drop_text_var = tk.StringVar(value="-"), tk.StringVar()
        bars = []
        for r, label, title_var, text_var in ((1, "Campaign", self.camp_title_var, self.camp_text_var),
                                              (3, "Drop", self.drop_title_var, self.drop_text_var)):
            ttk.Label(prog, text=label, style="Muted.TLabel").grid(row=r, column=0, sticky="w", pady=(10, 0))
            ttk.Label(prog, textvariable=title_var, style="Title.TLabel").grid(row=r, column=1, sticky="w",
                                                                               pady=(10, 0))
            ttk.Label(prog, textvariable=text_var, style="Muted.TLabel").grid(row=r, column=2, sticky="e",
                                                                              pady=(10, 0))
            bar = ttk.Progressbar(prog, maximum=100, style="Big.Accent.Horizontal.TProgressbar")
            bar.grid(row=r + 1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
            bars.append(bar)
        self.camp_bar, self.drop_bar = bars

        outer, chan = self.panel(pg, "Channels")
        outer.grid(row=3, column=0, sticky="nsew", padx=(PAD, 8), pady=(0, PAD))
        self.chan_tree = ttk.Treeview(chan, columns=("channel", "viewers", "status"), show="headings", height=6)
        for col, text, width, anchor in (("channel", "Channel", 150, "w"), ("viewers", "Viewers", 70, "e"),
                                         ("status", "Status", 80, "w")):
            self.chan_tree.heading(col, text=text)
            self.chan_tree.column(col, width=width, anchor=anchor)
        self.chan_tree.pack(fill="both", expand=True)
        self.chan_tree.bind("<Double-1>", self._open_tree_channel)

        outer, con = self.panel(pg, "Console")
        outer.grid(row=3, column=1, sticky="nsew", padx=(8, 8), pady=(0, PAD))
        self.log_box = tk.Text(con, height=8, width=40, state="disabled", wrap="word", relief="flat", bd=0,
                               font=self.fonts["mono"], padx=6, pady=4)
        lsb = ttk.Scrollbar(con, orient="vertical", command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=lsb.set)
        self.log_box.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")
        self.tk_widgets.append((self.log_box, "text"))
        return pg

    def _build_campaigns_page(self):
        pg = DotCanvas(self.content, self, mascot=True)
        pg.columnconfigure(0, weight=1)
        pg.columnconfigure(2, minsize=MASCOT_W + 90)
        pg.rowconfigure(1, weight=1)
        pg.rowconfigure(2, minsize=MASCOT_H + 6)

        outer, bar = self.panel(pg)
        outer.grid(row=0, column=0, columnspan=3, sticky="ew", padx=PAD, pady=(PAD, 10))
        ttk.Label(bar, text="Search", style="PanelTitle.TLabel").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.apply_filter(reset_scroll=True))
        search = ttk.Entry(bar, textvariable=self.search_var, width=34, font=self.fonts["body"])
        search.pack(side="left", padx=(8, 10))
        search.bind("<Escape>", lambda e: self.search_var.set(""))
        self.filter_var = tk.StringVar(value=FILTERS[0])
        cb = ttk.Combobox(bar, textvariable=self.filter_var, values=FILTERS, state="readonly", width=18,
                          font=self.fonts["body"])
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.apply_filter(reset_scroll=True))
        self.comboboxes = [cb]
        self.refresh_btn2 = MenuButton(bar, self, "Refresh campaigns", self.on_refresh)
        self.refresh_btn2.pack(side="left", padx=12)
        self.scan_info_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.scan_info_var, style="Muted.TLabel").pack(side="right")

        self.cards_canvas = DotCanvas(pg, self, scroll=True)
        self.cards_canvas.grid(row=1, column=0, rowspan=2, sticky="nsew", padx=(PAD, 0), pady=(0, PAD))
        sb = ttk.Scrollbar(pg, orient="vertical", command=self.cards_canvas.yview)
        sb.grid(row=1, column=1, rowspan=2, sticky="ns", padx=(4, 0), pady=(0, PAD))
        self.cards_canvas.configure(yscrollcommand=sb.set)
        self.cards_canvas.bind("<Configure>", lambda e: self.apply_filter(), add="+")

        side = ttk.Notebook(pg, style="Side.TNotebook")
        side.grid(row=1, column=2, sticky="nsew", padx=(12, PAD), pady=(0, 8))
        qtab = ttk.Frame(side, style="Panel.TFrame", padding=12)
        side.add(qtab, text="Drop queue")
        ttk.Label(qtab, text="Click \"+ Queue\" on any drop. The top live drop is\n"
                             "mined; when a higher one's streamer comes\nonline, the miner switches back to it.",
                  style="Muted.TLabel", justify="left").pack(anchor="w")
        self.queue_list = tk.Listbox(qtab, height=8, width=44, activestyle="none", exportselection=False,
                                     selectmode="extended", relief="flat", bd=0, highlightthickness=1,
                                     font=self.fonts["small"])
        self.queue_list.pack(fill="both", expand=True, pady=(8, 2))
        self.tk_widgets.append((self.queue_list, "listbox"))
        ql = self.queue_list
        ql.bind("<ButtonPress-1>", self._queue_press)
        ql.bind("<B1-Motion>", self._queue_drag)
        ql.bind("<ButtonRelease-1>", self._queue_release)
        ql.bind("<Button-3>", self._queue_context)
        ql.bind("<Alt-Up>", lambda e: self._queue_move_selected(-1) or "break")
        ql.bind("<Alt-Down>", lambda e: self._queue_move_selected(1) or "break")
        ql.bind("<Alt-Home>", lambda e: self._queue_move_selected("top") or "break")
        ql.bind("<Alt-End>", lambda e: self._queue_move_selected("bottom") or "break")
        ql.bind("<Delete>", lambda e: self.on_queue_remove())
        self._queue_ids = []
        self._drag = None
        ttk.Label(qtab, text="Drag to reorder  ·  right-click for more  ·  Ctrl+click two, then Swap",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))
        btns = ttk.Frame(qtab)
        btns.pack(fill="x")
        ttk.Button(btns, text="Top", width=6, command=lambda: self._queue_move_selected("top")).pack(side="left")
        ttk.Button(btns, text="▲", width=3, command=lambda: self._queue_move_selected(-1)).pack(side="left",
                                                                                                padx=(4, 0))
        ttk.Button(btns, text="▼", width=3, command=lambda: self._queue_move_selected(1)).pack(side="left",
                                                                                               padx=(4, 0))
        ttk.Button(btns, text="Bottom", width=7, command=lambda: self._queue_move_selected("bottom")).pack(
            side="left", padx=(4, 0))
        ttk.Button(btns, text="Swap", width=6, command=self.on_queue_swap).pack(side="left", padx=(4, 0))
        btns = ttk.Frame(qtab)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="Remove", command=self.on_queue_remove).pack(side="left")
        ttk.Button(btns, text="Clear finished", command=self.on_queue_clear_done).pack(side="left", padx=4)
        self.queue_menu = tk.Menu(self, tearoff=False)
        self.tk_widgets.append((self.queue_menu, "menu"))

        gtab = ttk.Frame(side, style="Panel.TFrame", padding=12)
        side.add(gtab, text="Games")
        ttk.Label(gtab, text="Tick \"Mine game\" on a campaign to add its\ngame. Mined after the drop queue, "
                             "top first.", style="Muted.TLabel", justify="left").pack(anchor="w")
        self.prio_list = tk.Listbox(gtab, height=6, width=44, activestyle="none", exportselection=False,
                                    relief="flat", bd=0, highlightthickness=1, font=self.fonts["body"])
        self.prio_list.pack(fill="both", expand=True, pady=8)
        self.tk_widgets.append((self.prio_list, "listbox"))
        btns = ttk.Frame(gtab)
        btns.pack(fill="x")
        ttk.Button(btns, text="▲", width=3, command=lambda: self.on_move_game(-1)).pack(side="left")
        ttk.Button(btns, text="▼", width=3, command=lambda: self.on_move_game(1)).pack(side="left", padx=4)
        ttk.Button(btns, text="Remove", command=self.on_remove_prio).pack(side="left")
        self.prefer_var = tk.BooleanVar(value=bool(self.settings.get("prefer_streamer_drops", True)))
        ttk.Checkbutton(gtab, text="Streamer drops before general drops\n(general drops progress on streamer "
                                   "channels too)", variable=self.prefer_var,
                        command=self.on_setting_changed).pack(anchor="w", pady=(10, 0))
        self.unselected_var = tk.BooleanVar(value=bool(self.settings.get("mine_unselected")))
        ttk.Checkbutton(gtab, text="Also mine games I haven't picked\n(only when these and the queue have nothing left)",
                        variable=self.unselected_var, command=self.on_toggle_unselected).pack(anchor="w", pady=(6, 0))
        self._refresh_prio_list()
        return pg

    def _build_settings_page(self):
        pg = DotCanvas(self.content, self, mascot=True)
        pg.columnconfigure(0, weight=1)
        pg.columnconfigure(1, minsize=MASCOT_W + 20)
        pg.rowconfigure(4, weight=1)

        outer, look = self.panel(pg, "Appearance")
        outer.grid(row=0, column=0, sticky="ew", padx=(PAD, 0), pady=(PAD, 10))
        self.theme_var = tk.StringVar(value="monika" if self.settings.get("dark_mode") else "doki")
        ttk.Radiobutton(look, text="Doki Doki  -  pink polka dots, like the main menu", value="doki",
                        variable=self.theme_var, command=self.on_theme_changed).pack(anchor="w", pady=2)
        ttk.Radiobutton(look, text="Just Monika  -  dark, Monika's starry space room", value="monika",
                        variable=self.theme_var, command=self.on_theme_changed).pack(anchor="w", pady=2)

        outer, start = self.panel(pg, "Startup")
        outer.grid(row=1, column=0, sticky="ew", padx=(PAD, 0), pady=(0, 10))
        self.startup_var = tk.BooleanVar(value=get_run_on_startup())
        ttk.Checkbutton(start, text="Open Twitch Drop Miner when Windows starts",
                        variable=self.startup_var, command=self.on_toggle_startup).pack(anchor="w", pady=2)
        self.minimized_var = tk.BooleanVar(value=bool(self.settings.get("start_minimized")))
        ttk.Checkbutton(start, text="Start minimized when opened at Windows startup",
                        variable=self.minimized_var, command=self.on_setting_changed).pack(anchor="w", padx=22, pady=2)
        self.automine_var = tk.BooleanVar(value=bool(self.settings.get("auto_mine_on_launch")))
        ttk.Checkbutton(start, text="Start mining automatically when the app opens",
                        variable=self.automine_var, command=self.on_setting_changed).pack(anchor="w", pady=2)

        outer, mine = self.panel(pg, "Mining")
        outer.grid(row=2, column=0, sticky="ew", padx=(PAD, 0), pady=(0, 10))
        ttk.Checkbutton(mine, text="Within a game, mine streamer-specific drops before general drops "
                                   "(general drops progress on streamer channels too)",
                        variable=self.prefer_var, command=self.on_setting_changed).pack(anchor="w", pady=2)
        ttk.Checkbutton(mine, text="Also mine games I haven't picked (only when the drop queue and picked games "
                                   "have nothing left). Off = the miner waits instead.",
                        variable=self.unselected_var, command=self.on_toggle_unselected).pack(anchor="w", pady=2)
        row = ttk.Frame(mine)
        row.pack(anchor="w", pady=6)
        ttk.Label(row, text="Rescan Twitch for new campaigns every").pack(side="left")
        self.refresh_var = tk.IntVar(value=int(self.settings.get("refresh_minutes", 10)))
        sp = ttk.Spinbox(row, from_=2, to=120, width=5, textvariable=self.refresh_var,
                         command=self.on_setting_changed, font=self.fonts["body"])
        sp.pack(side="left", padx=6)
        sp.bind("<FocusOut>", lambda e: self.on_setting_changed())
        ttk.Label(row, text="minutes").pack(side="left")
        row = ttk.Frame(mine)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text="Check the top").pack(side="left")
        self.cats_var = tk.IntVar(value=int(self.settings.get("scan_categories", 300)))
        sp = ttk.Spinbox(row, from_=50, to=1000, increment=50, width=6, textvariable=self.cats_var,
                         command=self.on_setting_changed, font=self.fonts["body"])
        sp.pack(side="left", padx=6)
        sp.bind("<FocusOut>", lambda e: self.on_setting_changed())
        ttk.Label(row, text="Twitch categories for campaigns (more finds smaller games, scans take longer)",
                  style="Muted.TLabel").pack(side="left")

        outer, help_ = self.panel(pg, "If drops aren't progressing")
        outer.grid(row=3, column=0, sticky="ew", padx=(PAD, 0), pady=(0, 10))
        for tip in (
            "♥  Link the game's account on Twitch, otherwise Twitch counts nothing. Campaigns that get no "
            "progress for 8 minutes are marked \"Not progressing\" and skipped.",
            "♥  Watch time is sent to spade.twitch.tv. VPN / ad-block DNS must not block it "
            "(Mullvad: turn off the Ads and Trackers content blockers).",
        ):
            ttk.Label(help_, text=tip, wraplength=760, justify="left").pack(anchor="w", pady=2)
        link = ttk.Label(help_, text="Open twitch.tv/drops/campaigns to link accounts", style="Link.TLabel",
                         cursor="hand2")
        link.pack(anchor="w", pady=(6, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(DROPS_PAGE_URL))
        ttk.Label(help_, text=f"Settings file: {SETTINGS_FILE}", style="Muted.TLabel").pack(anchor="w", pady=(6, 0))
        ttk.Label(help_, text=f"Doki Twitch Miner v{APP_VERSION}", style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
        return pg

    # -- Pages ------------------------------------------------------------------
    def show_page(self, key):
        if self.page == key:
            return
        if self.page:
            self.pages[self.page].pack_forget()
        self.page = key
        page = self.pages[key]
        page.pack(fill="both", expand=True)
        self.sidebar.select(key)
        self.update_idletasks()  # lay the page out so its canvases know where they are
        self._redraw_backgrounds(page)
        if key == "campaigns":
            self.apply_filter()

    def _redraw_backgrounds(self, page=None):
        for canvas in self.dot_canvases:
            widget = canvas
            while page is not None and widget is not None and widget is not page:
                widget = widget.master
            if page is None or widget is page:
                canvas.redraw()

    def on_menu(self, key):
        if key == "quit":
            if messagebox.askyesno("Quit", "Are you sure you want to quit?"):
                self.on_close()
            return
        self.show_page(key)

    # ======================================================================
    # Theme
    # ======================================================================
    def apply_theme(self):
        dark = bool(self.settings.get("dark_mode"))
        p = self.p = PALETTES["monika" if dark else "doki"]
        F = self.fonts
        self.mascot_img = self.images["monika_glow" if dark else "monika"] or self.images["monika"]
        self.title("Twitch Drop Miner" + ("  ♥  Just Monika" if dark else ""))
        s = self.style
        s.configure(".", background=p["panel"], foreground=p["fg"], fieldbackground=p["field"],
                    bordercolor=p["panel_border"], lightcolor=p["panel"], darkcolor=p["panel"],
                    troughcolor=p["trough"], selectbackground=p["accent"], selectforeground="#ffffff",
                    insertcolor=p["fg"], font=F["body"])
        s.configure("TFrame", background=p["panel"])
        s.configure("Panel.TFrame", background=p["panel"], bordercolor=p["panel_border"],
                    lightcolor=p["panel_border"], darkcolor=p["panel_border"], relief="solid", borderwidth=2)
        s.configure("Card.TFrame", background=p["panel"], bordercolor=p["panel_border"],
                    lightcolor=p["panel_border"], darkcolor=p["panel_border"], relief="solid", borderwidth=2)
        s.configure("CardBody.TFrame", background=p["panel"], borderwidth=0)
        s.configure("TLabel", background=p["panel"], foreground=p["fg"], font=F["body"])
        s.configure("Muted.TLabel", foreground=p["muted"])
        s.configure("Title.TLabel", font=F["title"], foreground=p["fg"])
        s.configure("PanelTitle.TLabel", font=F["panel"], foreground=p["heading"])
        s.configure("Link.TLabel", foreground=p["accent"], font=F["body_b"])
        s.configure("Card.TLabel", background=p["panel"], foreground=p["fg"])
        s.configure("CardTitle.TLabel", background=p["panel"], foreground=p["fg"], font=F["card_title"])
        s.configure("CardMuted.TLabel", background=p["panel"], foreground=p["muted"])
        s.configure("CardSuccess.TLabel", background=p["panel"], foreground=p["success"], font=F["body_b"])

        s.configure("TButton", background=p["button"], foreground=p["heading"], bordercolor=p["panel_border"],
                    lightcolor=p["button"], darkcolor=p["button"], focuscolor=p["button"], padding=(10, 3),
                    font=F["tab"])
        s.map("TButton",
              background=[("disabled", p["panel"]), ("pressed", p["button_active"]), ("active", p["button_hover"])],
              lightcolor=[("pressed", p["button_active"]), ("active", p["button_hover"])],
              darkcolor=[("pressed", p["button_active"]), ("active", p["button_hover"])],
              foreground=[("disabled", p["muted"])])
        s.configure("Small.TButton", padding=(4, 1), font=F["small_b"])
        s.configure("SmallAccent.TButton", padding=(4, 1), font=F["small_b"], background=p["accent"],
                    foreground="#ffffff", bordercolor=p["accent_dark"], lightcolor=p["accent"],
                    darkcolor=p["accent"])
        s.map("SmallAccent.TButton", background=[("active", p["accent_hover"])],
              lightcolor=[("active", p["accent_hover"])], darkcolor=[("active", p["accent_hover"])])

        for style in ("TCheckbutton", "Card.TCheckbutton", "TRadiobutton"):
            s.configure(style, background=p["panel"], foreground=p["fg"], indicatorbackground=p["field"],
                        indicatorforeground="#ffffff", upperbordercolor=p["panel_border"],
                        lowerbordercolor=p["panel_border"], focuscolor=p["panel"], font=F["body"])
            s.map(style, background=[("active", p["panel"])],
                  indicatorbackground=[("selected", p["accent"]), ("active", p["button_hover"])],
                  upperbordercolor=[("selected", p["accent_dark"])], lowerbordercolor=[("selected", p["accent_dark"])])

        s.configure("TEntry", fieldbackground=p["field"], foreground=p["fg"], bordercolor=p["panel_border"],
                    lightcolor=p["field"], darkcolor=p["field"], insertcolor=p["fg"], padding=4)
        s.map("TEntry", bordercolor=[("focus", p["accent"])], lightcolor=[("focus", p["accent"])])
        s.configure("TCombobox", fieldbackground=p["field"], background=p["button"], foreground=p["fg"],
                    arrowcolor=p["heading"], bordercolor=p["panel_border"], lightcolor=p["field"],
                    darkcolor=p["field"], padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("readonly", p["fg"])],
              selectbackground=[("readonly", p["field"])], selectforeground=[("readonly", p["fg"])],
              background=[("active", p["button_hover"])], bordercolor=[("focus", p["accent"])])
        s.configure("TSpinbox", fieldbackground=p["field"], background=p["button"], foreground=p["fg"],
                    arrowcolor=p["heading"], bordercolor=p["panel_border"], lightcolor=p["field"],
                    darkcolor=p["field"])
        self.option_add("*TCombobox*Listbox.background", p["field"])
        self.option_add("*TCombobox*Listbox.foreground", p["fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.option_add("*TCombobox*Listbox.font", F["body"])
        for cb in self.comboboxes:  # popdowns that already exist
            try:
                popdown = self.tk.call("ttk::combobox::PopdownWindow", cb)
                self.tk.call(f"{popdown}.f.l", "configure", "-background", p["field"], "-foreground", p["fg"],
                             "-selectbackground", p["accent"], "-selectforeground", "#ffffff")
            except tk.TclError:
                pass

        s.configure("Treeview", background=p["panel"], fieldbackground=p["panel"], foreground=p["fg"],
                    bordercolor=p["panel_border"], lightcolor=p["panel"], darkcolor=p["panel"], rowheight=24,
                    font=F["body"])
        s.map("Treeview", background=[("selected", p["select"])], foreground=[("selected", p["fg"])])
        s.configure("Treeview.Heading", background=p["button"], foreground=p["heading"],
                    bordercolor=p["panel_border"], lightcolor=p["button"], darkcolor=p["button"], relief="flat",
                    font=F["tab"])
        s.map("Treeview.Heading", background=[("active", p["button_hover"])])
        self.chan_tree.tag_configure("watching", foreground=p["accent"])
        self.acct_tree.tag_configure("mining", foreground=p["accent"])
        self.acct_tree.tag_configure("off", foreground=p["muted"])

        s.configure("Side.TNotebook", background=p["bg"], bordercolor=p["panel_border"], lightcolor=p["bg"],
                    darkcolor=p["bg"], tabmargins=(0, 0, 0, 0))
        s.configure("Side.TNotebook.Tab", background=p["button"], foreground=p["muted"], padding=(14, 5),
                    bordercolor=p["panel_border"], lightcolor=p["button"], darkcolor=p["button"], font=F["tab"])
        s.map("Side.TNotebook.Tab", background=[("selected", p["panel"])],
              foreground=[("selected", p["heading"])], lightcolor=[("selected", p["panel"])])

        for name, color in (("Accent", p["accent"]), ("Done", p["success"]), ("Idle", p["muted"])):
            s.configure(f"{name}.Horizontal.TProgressbar", background=color, troughcolor=p["trough"],
                        bordercolor=p["panel_border"], lightcolor=color, darkcolor=color, thickness=10)
        s.configure("Big.Accent.Horizontal.TProgressbar", thickness=18)
        s.configure("Vertical.TScrollbar", background=p["button"], troughcolor=p["panel"],
                    bordercolor=p["panel_border"], arrowcolor=p["heading"], lightcolor=p["button"],
                    darkcolor=p["button"], gripcount=0)
        s.map("Vertical.TScrollbar", background=[("active", p["button_hover"])])

        self.configure(bg=p["bg"])
        for widget, role in self.tk_widgets:
            if role == "text":
                widget.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"],
                                 selectbackground=p["select"], selectforeground=p["fg"])
                widget.tag_configure("good", foreground=p["success"])
                widget.tag_configure("warn", foreground=p["warn"])
                widget.tag_configure("time", foreground=p["muted"])
                widget.tag_configure("who", foreground=p["heading"])
            elif role == "listbox":
                widget.configure(bg=p["field"], fg=p["fg"], selectbackground=p["accent"],
                                 selectforeground="#ffffff", highlightbackground=p["panel_border"],
                                 highlightcolor=p["accent"])
            elif role == "banner":
                widget.configure(bg=p["danger"], fg="#ffffff", font=self.fonts["body_b"])
            elif role == "bg":
                widget.configure(bg=p["bg"])
            elif role == "menu":
                widget.configure(bg=p["field"], fg=p["fg"], activebackground=p["accent"],
                                 activeforeground="#ffffff", disabledforeground=p["heading"],
                                 font=self.fonts["small"], bd=0)
        for button in self.menu_buttons:
            button.redraw()
        for card in self.cards.values():
            card.refresh_theme()
        self._refresh_queue_list()
        self.sidebar.redraw()
        self._redraw_backgrounds()
        set_titlebar_dark(self, dark)

    # ======================================================================
    # Event plumbing (worker threads -> UI thread)
    # ======================================================================
    def _bg(self, fn, *args):
        threading.Thread(target=fn, args=args, daemon=True).start()

    def emit(self, kind, *payload):
        """Global events (not tied to an account)."""
        self.events.put((None, kind, payload))

    def emitter(self, session):
        """Events from one account's store / miner, tagged with its session."""
        return lambda kind, *payload: self.events.put((session, kind, payload))

    def log(self, msg):
        self.emit("log", msg)

    VIEW_EVENTS = ("campaigns", "progress", "channels", "blocked")  # shown for the viewed account only

    def _process_events(self):
        try:
            while True:
                session, kind, payload = self.events.get_nowait()
                if session is None:
                    getattr(self, f"_ui_{kind}")(*payload)
                    continue
                if session not in self.sessions:
                    continue  # from an account that was removed
                if kind in self.VIEW_EVENTS:
                    session.last[kind] = payload  # replayed when this account gets viewed
                handler = getattr(self, f"_sess_{kind}", None)
                if handler:
                    handler(session, *payload)
                if kind in self.VIEW_EVENTS and session is self.view:
                    getattr(self, f"_ui_{kind}")(*payload)
        except queue.Empty:
            pass
        self.after(100, self._process_events)

    def _on_tk_error(self, exc, val, tb):
        self._ui_log("Internal error: " + "".join(traceback.format_exception(exc, val, tb)).strip())

    # -- UI handlers ------------------------------------------------------------
    def _ui_log(self, msg, who=None):
        tag = "good" if msg.startswith(("CLAIMED", "New campaign")) else \
              "warn" if msg.startswith(("WARNING", "No progress", "Failed")) else None
        if msg.startswith("CLAIMED"):
            msg = "♥ " + msg
        self.log_box.configure(state="normal")
        self.log_box.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        if who:
            self.log_box.insert("end", f"[{who}] ", "who")
        self.log_box.insert("end", msg + "\n", tag)
        lines = int(self.log_box.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:  # keep the console from growing forever while mining for days
            self.log_box.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _ui_status(self, text):
        """App-wide status (e.g. the shared campaign scan)."""
        self.sidebar.set_status(text)

    def _ui_blocked(self, blocked):
        if blocked:
            self.blocked_banner.grid()
        else:
            self.blocked_banner.grid_remove()

    def _ui_progress(self, info):
        if not info:
            self.watch_login = None
            self.channel_var.set("Not watching")
            self.camp_title_var.set("-")
            self.drop_title_var.set("-")
            self.camp_text_var.set("")
            self.drop_text_var.set("")
            self.camp_bar["value"] = 0
            self.drop_bar["value"] = 0
            return
        ch = info["channel"]
        self.watch_login = ch["login"]
        self.channel_var.set(f"{ch['display']}  ·  {ch['game_name']}  ·  {ch.get('viewers', 0):,} viewers")
        kind = f"   [{info['kind']}]" if info.get("kind") else ""
        self.camp_title_var.set(f"{info['game']}  —  {info['campaign']}{kind}")
        self.camp_bar["value"] = info["camp_pct"]
        self.camp_text_var.set(f"{info['claimed']}/{info['count']} drops  ·  {info['camp_pct']:.0f}%  ·  "
                               f"{fmt_minutes(info['camp_left'])} left")
        drop = info["drop"]
        if drop:
            pct = 100 * drop["cur"] / drop["req"] if drop["req"] else 0
            self.drop_title_var.set(drop["name"])
            self.drop_bar["value"] = pct
            self.drop_text_var.set(f"{drop['cur']}/{drop['req']} min  ·  {pct:.0f}%  ·  "
                                   f"{fmt_minutes(drop['req'] - drop['cur'])} left")
        else:
            self.drop_title_var.set("Waiting for Twitch to report progress...")
            self.drop_bar["value"] = 0
            self.drop_text_var.set("")

    def _ui_channels(self, channels, watching):
        self.chan_tree.delete(*self.chan_tree.get_children())
        for ch in sorted(channels, key=lambda c: -int(c.get("viewers") or 0)):
            is_watching = ch["login"] == watching
            self.chan_tree.insert("", "end", iid=ch["login"],
                                  values=(ch["display"], f"{int(ch.get('viewers') or 0):,}",
                                          "Watching" if is_watching else "Online"),
                                  tags=("watching",) if is_watching else ())

    def _ui_campaigns(self, cards, claimed_total, last_scan, queue_rows=None):
        if queue_rows is not None:
            self.queue_rows = queue_rows
            self._refresh_queue_list()
        ids = [c["id"] for c in cards]
        if set(ids) != set(self.cards):
            for card in self.cards.values():
                card.frame.destroy()
            self.cards_canvas.delete("card")
            self.cards = {}
            for c in cards:
                card = CampaignCard(self, self.cards_canvas, c)
                card.win = self.cards_canvas.create_window(0, 0, window=card.frame, anchor="nw",
                                                           state="hidden", tags="card")
                self.cards[c["id"]] = card
        else:
            for c in cards:
                self.cards[c["id"]].update(c)
        self.card_order = ids
        selected = set(self.discovery.selected_slugs())
        for slug, var in self.game_vars.items():
            var.set(slug in selected)
        games = len({c["slug"] for c in cards})
        when = datetime.fromtimestamp(last_scan).strftime("%H:%M") if last_scan else "-"
        self.scan_info_var.set(f"{len(cards)} campaigns · {games} games · scanned {when}")
        self.apply_filter()

    # -- Per-account events -----------------------------------------------------
    def _sess_log(self, s, msg):
        self._ui_log(msg, who=s.name if len(self.sessions) > 1 else None)

    def _sess_status(self, s, text):
        s.status = text
        if s is self.view:
            self.sidebar.set_status(text)
            if s.miner:
                self.mining_status_var.set(text)
        self._refresh_accounts_table()

    def _sess_progress(self, s, info):
        if info:
            drop = info.get("drop")
            s.watching = f"▶ {info['channel']['display']}  ·  " + (
                f"{drop['name']} {drop['cur']}/{drop['req']}m" if drop else info["campaign"])
        else:
            s.watching = ""
        self._refresh_accounts_table()

    def _sess_campaigns(self, s, cards, claimed_total, last_scan, queue_rows=None):
        self.sidebar.set_claimed(sum(x.store.claimed_total for x in self.sessions))

    def _sess_stopped(self, s, miner):
        if s.miner is miner:
            s.miner = None
            self._forget_mining_view(s)
            self._refresh_accounts_table()
            self._update_start_button()

    def _sess_session_expired(self, s):
        login = s.login
        self._remove_session(s)
        if login:
            self.accounts.remove(login)
            messagebox.showwarning("Twitch login expired",
                                   f"The login for {login} expired or was revoked.\nUse \"Add account\" to log in again.")

    def _sess_scan_done(self, s):
        s.scanning = False
        if s is self.view:
            self._update_view_buttons()
            if not s.miner:
                self.sidebar.set_status("Ready")
            self.apply_filter()

    def _sess_account_ready(self, s, ok):
        if not ok:
            s.state, s.status = "expired", "Login expired - use Add account"
            self.log(f"The saved login for {s.name} is no longer valid - use Add account to log in again")
        else:
            s.state, s.status, s.login = "ready", "Ready", s.client.login
            if s.login not in self.accounts.logins():  # migrated old single-account file
                self.accounts.upsert(s.login, s.client.user_id, s.client.token)
            self.log(f"Logged in as {s.login}")
        self._refresh_accounts_table()
        self._update_start_button()
        if s is self.view:
            self._show_view()
        if s.state == "ready":
            if self.mining and self._mine_enabled(s):
                self._start_session(s)
            elif s is self.view:
                self.start_scan(s)

    def _ui_device_code(self, device):
        url = device.get("verification_uri", "https://www.twitch.tv/activate")
        code = device["user_code"]
        self.clipboard_clear()
        self.clipboard_append(code)
        self.log(f"Enter code {code} at {url}")
        messagebox.showinfo(
            "Add a Twitch account",
            f"Your browser will open {url}\n\nConfirm this code there:\n\n    {code}\n\n"
            "The code is on your clipboard.\n\nTo add a DIFFERENT account, make sure that account is the one "
            "logged in on twitch.tv in your browser (or open the link in a private window).")
        webbrowser.open(url)

    def _ui_add_done(self, account):
        self.add_btn.set(state="normal")
        if not account:
            return
        self.accounts.upsert(account["login"], account["user_id"], account["token"])
        self.log(f"Added account {account['login']}")
        old = next((s for s in self.sessions if s.login == account["login"]), None)
        if old:
            self._remove_session(old)  # logged in again: replace the old session
        self._set_view(self._add_session(self.accounts.get(account["login"])))

    # ======================================================================
    # Accounts (every saved account gets a session; the UI views one of them)
    # ======================================================================
    def _restore_account(self):
        self.mining = bool(self.settings.get("auto_mine_on_launch"))
        for account in self.accounts.accounts:
            self._add_session(account)
        if not self.sessions:
            self.mining = False
            self._set_view(None)
            return
        view = next((s for s in self.sessions if s.login and s.login == self.accounts.active), self.sessions[0])
        self._set_view(view)
        self._update_start_button()

    def _add_session(self, account):
        s = Session(self, account)
        self.sessions.append(s)
        emit = self.emitter(s)

        def check():
            try:
                ok = s.client.validate()
            except TwitchError as e:
                emit("log", str(e))
                ok = False
            emit("account_ready", ok)
        self._bg(check)
        self._refresh_accounts_table()
        return s

    def _remove_session(self, s):
        self._stop_session(s)
        if s in self.sessions:
            self.sessions.remove(s)
        if s is self.view:
            self._set_view(self.sessions[0] if self.sessions else None)
        self._refresh_accounts_table()
        self._update_start_button()

    def _set_view(self, s):
        """Show `s`'s progress, channels and campaigns (mining keeps running on every account)."""
        self.view = s
        if s and s.login and self.accounts.active != s.login:
            self.accounts.active = s.login
            self.accounts.save()
        self._show_view()
        if s and s.state == "ready" and not s.miner:
            self.start_scan(s, force=False)  # adopt the latest shared scan for this account

    def _show_view(self):
        """Repaint everything account-specific from the viewed session's latest data."""
        s = self.view
        self._ui_campaigns_clear()
        self._ui_blocked(False)
        self._ui_progress(None)
        self._ui_channels([], None)
        login = (s.login if s else "") or ""
        if self.sidebar.login != login:
            self.sidebar.login = login
            self.sidebar.redraw()
        if not s:
            self.account_status_var.set("Not logged in - click Add account")
            self.sidebar.set_status("Add a Twitch account to get started")
        else:
            self.account_status_var.set({"checking": "Checking login...",
                                         "expired": "Login expired - use Add account"}.get(
                s.state, f"Showing {s.login}"))
            for kind in self.VIEW_EVENTS:
                if kind in s.last:
                    getattr(self, f"_ui_{kind}")(*s.last[kind])
        self.mining_status_var.set(s.status if s and s.miner else "Idle")
        self._refresh_accounts_table()
        self._update_view_buttons()

    def _forget_mining_view(self, s):
        """Drop an account's live mining data (after its miner stops)."""
        s.watching = ""
        for kind in ("progress", "channels", "blocked"):
            s.last.pop(kind, None)
        if s is self.view:
            self._ui_blocked(False)
            self._ui_progress(None)
            self._ui_channels([], None)
            self.mining_status_var.set("Idle")

    def _mine_enabled(self, s):
        return s.login not in self.settings.get("mining_disabled", [])

    def _refresh_accounts_table(self):
        tree = self.acct_tree
        tree.delete(*tree.get_children())
        for s in self.sessions:
            enabled = self._mine_enabled(s)
            if s.state == "checking":
                status = "Checking login..."
            elif s.state == "expired":
                status = "Login expired - use Add account"
            elif s.miner:
                status = s.watching or s.status
            elif not enabled:
                status = "Not mining (tick to include)"
            elif self.mining:
                status = "Starting..."
            else:
                status = "Idle"
            tag = "mining" if s.miner else "off" if (not enabled or s.state == "expired") else ""
            tree.insert("", "end", iid=str(id(s)), values=("☑" if enabled else "☐", s.name, status),
                        tags=(tag,) if tag else ())
        if self.view in self.sessions:
            tree.selection_set(str(id(self.view)))
        tree.configure(height=max(1, min(4, len(self.sessions))))
        active = [s.name for s in self.sessions if s.miner]
        if not self.mining:
            summary = "Not mining"
        elif active:
            summary = f"Mining on {len(active)} account{'s' if len(active) != 1 else ''}: {', '.join(active)}"
        else:
            summary = "Waiting for an account ticked \"Mine\" to be ready..."
        self.mining_summary_var.set(summary)

    def _on_acct_click(self, event):
        """Click a row to view that account; click the Mine box to include / exclude it."""
        tree = self.acct_tree
        s = next((x for x in self.sessions if str(id(x)) == tree.identify_row(event.y)), None)
        if s is not None:
            if tree.identify_column(event.x) == "#1":
                self._toggle_mine(s)
            elif s is not self.view:
                self._set_view(s)
        return "break"  # selection always follows the viewed account

    def _toggle_mine(self, s):
        if not s.login:
            return
        disabled = set(self.settings.get("mining_disabled", []))
        enable = s.login in disabled
        (disabled.discard if enable else disabled.add)(s.login)
        self.settings["mining_disabled"] = sorted(disabled)
        save_json(SETTINGS_FILE, self.settings)
        self.log(f"{'Will mine' if enable else 'Will not mine'} with {s.login}")
        if self.mining and s.state == "ready":
            if enable:
                self._start_session(s)
            else:
                self._stop_session(s)
        self._refresh_accounts_table()
        self._update_start_button()

    def on_add_account(self):
        self.add_btn.set(state="disabled")
        self.login_cancel.clear()
        self._bg(self._add_account_flow)

    def _add_account_flow(self):
        tmp = TwitchClient(self.settings, self.accounts.device_id)
        try:
            device = tmp.start_device_login()
            self.emit("device_code", device)
            self.emit("status", "Waiting for you to confirm the code on twitch.tv...")
            if tmp.poll_device_login(device, self.login_cancel):
                self.emit("add_done", {"login": tmp.login, "user_id": tmp.user_id, "token": tmp.token})
                return
            self.log("Login cancelled or timed out")
        except TwitchError as e:
            self.log(str(e))
        self.emit("add_done", None)

    def on_remove_account(self):
        s = self.view
        if not s:
            return
        if not messagebox.askyesno("Remove account", f"Log out and remove {s.name} from this app?"):
            return
        if s.login:
            self.accounts.remove(s.login)
        self._remove_session(s)
        self.log(f"Removed account {s.name}")

    # ======================================================================
    # Mining / scanning
    # ======================================================================
    def _update_start_button(self):
        ready = any(s.state == "ready" for s in self.sessions)
        self.start_btn.set(text="Stop mining" if self.mining else "Start mining",
                           state="normal" if (ready or self.mining) else "disabled")

    def _update_view_buttons(self):
        s = self.view
        ok = bool(s and s.state == "ready" and not s.scanning)
        for b in (self.refresh_btn, self.refresh_btn2):
            b.set(state="normal" if ok else "disabled")

    def _start_session(self, s):
        if s.miner or s.state != "ready":
            return
        s.miner = Miner(s.store)
        s.status = "Starting..."
        threading.Thread(target=s.miner.run, daemon=True).start()
        self._sess_log(s, "Mining started")
        if s is self.view:
            self.mining_status_var.set(s.status)
        self._refresh_accounts_table()

    def _stop_session(self, s):
        if not s.miner:
            return
        s.miner.stop_event.set()
        s.miner.wake.set()
        s.miner = None
        s.store.target_id = None
        self._bg(s.store.publish)
        self._forget_mining_view(s)

    def on_start_stop(self):
        if self.mining:
            self.mining = False
            for s in self.sessions:
                self._stop_session(s)
            self.sidebar.set_status("Stopped")
            self.log("Mining stopped")
        else:
            targets = [s for s in self.sessions if s.state == "ready" and self._mine_enabled(s)]
            if not targets:
                messagebox.showinfo("Start mining", "No logged-in account is ticked \"Mine\" in the Accounts table.")
                return
            self.mining = True
            for s in targets:
                self._start_session(s)
        self._refresh_accounts_table()
        self._update_start_button()

    def start_scan(self, s=None, force=True):
        s = s or self.view
        if not s or s.state != "ready" or s.scanning:
            return
        s.scanning = True
        if s is self.view:
            self._update_view_buttons()
        store, emit = s.store, self.emitter(s)
        max_age = 0 if force else max(2, int(self.settings.get("refresh_minutes", 10))) * 60

        def run():
            try:
                store.ensure_fresh(max_age)
            except TwitchError as e:
                emit("log", f"Campaign scan failed: {e}")
            emit("scan_done")
        self._bg(run)

    def on_refresh(self):
        s = self.view
        if not s or s.state != "ready":
            return
        for x in self.sessions:
            x.store.stalled.clear()
        self.start_scan(s, force=True)
        self._wake_miners()

    def _wake_miners(self):
        for s in self.sessions:
            if s.miner:
                s.miner.wake.set()  # re-pick what to watch right away

    def _publish_all(self):
        for s in self.sessions:
            if s.state == "ready":
                self._bg(s.store.publish)

    def _idle_rescan(self):
        if self.view and not self.view.miner:
            self.start_scan(self.view, force=False)  # keeps the viewed account's list current
        self.after(60_000, self._idle_rescan)

    # ======================================================================
    # Campaign list
    # ======================================================================
    def _ui_campaigns_clear(self):
        for card in self.cards.values():
            card.frame.destroy()
        self.cards_canvas.delete("card")
        self.cards, self.card_order = {}, []
        self.scan_info_var.set("")
        self.apply_filter()

    def game_var(self, slug):
        if slug not in self.game_vars:
            selected = self.discovery.selected_slugs()
            self.game_vars[slug] = tk.BooleanVar(value=slug in selected)
        return self.game_vars[slug]

    def apply_filter(self, reset_scroll=False):
        """Lay the matching campaign cards out on the (scrolling, polka-dotted) canvas."""
        if self._laying_out:
            return  # re-entered from a <Configure> fired while measuring; the outer pass finishes
        self._laying_out = True
        try:
            self._layout_cards(reset_scroll)
        finally:
            self._laying_out = False

    def _layout_cards(self, reset_scroll):
        canvas = self.cards_canvas
        query = self.search_var.get().strip().lower()
        mode = self.filter_var.get()
        order = self.card_order
        if mode == "Queued drops":  # show them in queue order
            order = sorted(order, key=lambda cid: min(
                (d["queued"] for d in self.cards[cid].data["drops"] if d["queued"]), default=999))
        shown = []
        for cid in order:
            card = self.cards[cid]
            if query and query not in card.search_text:
                continue
            if mode == "Queued drops" and not card.queued:
                continue
            if mode == "Streamer drops" and card.kind_text != "Streamer drop":
                continue
            if mode == "Picked games" and not card.selected:
                continue
            if mode == "Available to mine" and card.state not in ("Mining", "Queued", "Available"):
                continue
            if mode == "Completed" and card.state != "Completed":
                continue
            shown.append(card)
        for card in self.cards.values():
            canvas.itemconfigure(card.win, state="hidden")
        canvas.update_idletasks()  # card sizes + canvas width are only known after layout
        width = max(200, canvas.winfo_width() - 2)
        y = 0
        for card in shown:
            canvas.coords(card.win, 0, y)
            canvas.itemconfigure(card.win, width=width, state="normal")
            y += card.frame.winfo_reqheight() + 12
        canvas.delete("empty")
        if not shown:
            if not self.store:
                text = "Log in to see the drop campaigns running on Twitch."
            elif not self.cards:
                busy = self.scanning or self.miner or not self.store.last_scan
                text = "Scanning Twitch for drop campaigns..." if busy else "No campaigns found."
            else:
                text = "No campaigns match your search."
            canvas.create_text(width / 2, 60, text=text, font=self.fonts["panel"], fill=self.p["heading"],
                               tags="empty")
        canvas.content_height = y
        canvas.configure(scrollregion=(0, 0, width, max(y, canvas.winfo_height())))
        if reset_scroll:
            canvas.yview_moveto(0)
        canvas.redraw()

    def _on_wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        canvas = self.cards_canvas
        while widget is not None and widget is not canvas:
            widget = widget.master
        if widget is canvas and canvas.content_height > canvas.winfo_height():
            canvas.yview_scroll(int(-event.delta / 120), "units")

    def _refresh_prio_list(self, select=None):
        self.prio_list.delete(0, "end")
        for i, game in enumerate(self.settings["priority_games"], 1):
            self.prio_list.insert("end", f"  {i}.  {game}")
        if select is not None and 0 <= select < self.prio_list.size():
            self.prio_list.selection_set(select)

    def _save_priority(self, games, select=None):
        self.settings["priority_games"] = games  # replaced atomically; the miner reads it
        save_json(SETTINGS_FILE, self.settings)
        self._refresh_prio_list(select)
        self._publish_all()
        self._wake_miners()

    def on_toggle_game(self, slug, game_name):
        games = [g for g in self.settings["priority_games"] if game_slug(self.settings, g) != slug]
        if self.game_var(slug).get():
            games.append(game_name)
            self.settings.setdefault("game_slugs", {})[game_name] = slug
            self.log(f"Mining {game_name} (priority {len(games)})")
        else:
            self.log(f"No longer mining {game_name}")
        self._save_priority(games)

    def on_move_game(self, delta):
        sel = self.prio_list.curselection()
        if not sel:
            return
        i, games = sel[0], list(self.settings["priority_games"])
        j = i + delta
        if 0 <= j < len(games):
            games[i], games[j] = games[j], games[i]
            self._save_priority(games, j)

    def on_remove_prio(self):
        sel = self.prio_list.curselection()
        if not sel:
            return
        games = list(self.settings["priority_games"])
        removed = games.pop(sel[0])
        if game_slug(self.settings, removed) in self.game_vars:
            self.game_vars[game_slug(self.settings, removed)].set(False)
        self.log(f"No longer mining {removed}")
        self._save_priority(games, min(sel[0], len(games) - 1))

    # -- Drop queue -------------------------------------------------------------
    QUEUE_COLORS = {"Mining": "accent", "Live": "accent", "Offline": "muted", "Claimed": "success",
                    "Expired": "muted", "Not found": "muted", "Not linked": "danger", "Not started": "muted",
                    "": "muted"}

    def _queue_rows_now(self):
        """Display rows in the current queue order (status from the latest snapshot)."""
        queue_ = self.settings.get("drop_queue", [])
        rows = self.queue_rows
        if [r["drop_id"] for r in rows] != [e["drop_id"] for e in queue_]:
            known = {r["drop_id"]: r for r in rows}  # queue edited since the last snapshot
            rows = [known.get(e["drop_id"]) or {"drop_id": e["drop_id"], "label": e.get("label", "?"),
                                                "status": ""} for e in queue_]
        return rows

    def _refresh_queue_list(self, select_ids=None):
        """Redraw the queue list, keeping the same drops selected and the scroll position."""
        ql = self.queue_list
        if select_ids is None:
            select_ids = {self._queue_ids[i] for i in ql.curselection() if i < len(self._queue_ids)}
        top = ql.yview()[0]
        rows = self._queue_rows_now()
        ql.delete(0, "end")
        for i, row in enumerate(rows, 1):
            status = row["status"]
            mark = {"Mining": "▶", "Live": "●", "Claimed": "♥"}.get(status, "○")
            ql.insert("end", f" {mark} {i}. {row['label']}" + (f"   — {status}" if status else ""))
            ql.itemconfigure(i - 1, foreground=self.p[self.QUEUE_COLORS.get(status, "fg")])
        self._queue_ids = [r["drop_id"] for r in rows]
        ql.yview_moveto(top)
        for i, drop_id in enumerate(self._queue_ids):
            if drop_id in select_ids:
                ql.selection_set(i)
                ql.see(i)

    def _save_queue(self, queue_, select_ids=None, persist=True):
        self.settings["drop_queue"] = queue_  # replaced atomically; the miner reads it
        self._refresh_queue_list(select_ids)
        if persist:
            self._persist_queue()

    def _persist_queue(self):
        save_json(SETTINGS_FILE, self.settings)
        self._publish_all()  # updates the "#n" badges on the campaign cards
        self._wake_miners()  # every account re-picks what to watch right away

    # -- Reordering ---------------------------------------------------------------
    def _queue_move(self, src, dst, persist=True):
        """Move the drop at position src to position dst (0-based)."""
        queue_ = list(self.settings.get("drop_queue", []))
        if not 0 <= src < len(queue_):
            return
        dst = max(0, min(dst, len(queue_) - 1))
        if src == dst:
            return
        entry = queue_.pop(src)
        queue_.insert(dst, entry)
        self._save_queue(queue_, {entry["drop_id"]}, persist)

    def _queue_swap(self, i, j):
        queue_ = list(self.settings.get("drop_queue", []))
        if i == j or not (0 <= i < len(queue_) and 0 <= j < len(queue_)):
            return
        queue_[i], queue_[j] = queue_[j], queue_[i]
        self.log(f"Swapped queue #{i + 1} and #{j + 1}")
        self._save_queue(queue_, {queue_[i]["drop_id"], queue_[j]["drop_id"]})

    def _queue_move_selected(self, where):
        """where: -1 / +1 (one step), "top" or "bottom". Works on the first selected drop."""
        sel = self.queue_list.curselection()
        if not sel:
            return
        i, n = sel[0], len(self.settings.get("drop_queue", []))
        dst = {"top": 0, "bottom": n - 1}.get(where, i + where if isinstance(where, int) else i)
        self._queue_move(i, dst)

    def on_queue_swap(self):
        sel = self.queue_list.curselection()
        if len(sel) != 2:
            messagebox.showinfo("Swap drops", "Ctrl+click two drops in the queue, then click Swap.\n\n"
                                              "(Or right-click a drop and use \"Swap with\".)")
            return
        self._queue_swap(sel[0], sel[1])

    # -- Drag and drop --------------------------------------------------------------
    def _queue_press(self, event):
        ql = self.queue_list
        self._drag = None
        if event.state & 0x0005:  # Shift / Ctrl: normal multi-select
            return
        i = ql.nearest(event.y)
        if 0 <= i < ql.size() and ql.bbox(i):
            self._drag = {"index": i, "moved": False}

    def _queue_drag(self, event):
        if not self._drag:
            return
        ql = self.queue_list
        if event.y < 0:
            ql.yview_scroll(-1, "units")  # auto-scroll while dragging past the edges
        elif event.y > ql.winfo_height():
            ql.yview_scroll(1, "units")
        target = ql.nearest(event.y)
        if 0 <= target < ql.size() and target != self._drag["index"]:
            self._queue_move(self._drag["index"], target, persist=False)
            self._drag.update(index=target, moved=True)
            ql.configure(cursor="sb_v_double_arrow")
        return "break"  # don't let the listbox turn the drag into a range selection

    def _queue_release(self, event):
        if self._drag and self._drag["moved"]:
            i = self._drag["index"]
            self.log(f"Moved {self.settings['drop_queue'][i].get('label', '?')} to #{i + 1}")
            self._persist_queue()
        self._drag = None
        self.queue_list.configure(cursor="")

    # -- Right-click menu ------------------------------------------------------------
    def _queue_menu_for(self, i):
        """Build the context menu for queue position i."""
        queue_ = self.settings.get("drop_queue", [])
        n = len(queue_)
        p, font = self.p, self.fonts["small"]
        menu = self.queue_menu
        menu.delete(0, "end")
        for child in menu.winfo_children():
            child.destroy()
        style = dict(tearoff=False, bg=p["field"], fg=p["fg"], activebackground=p["accent"],
                     activeforeground="#ffffff", disabledforeground=p["heading"], font=font, bd=0)
        menu.configure(**{k: v for k, v in style.items() if k != "tearoff"})
        label = queue_[i].get("label", "?")
        menu.add_command(label=f"#{i + 1}  {label}", state="disabled")
        menu.add_separator()
        menu.add_command(label="Move to top", command=lambda: self._queue_move(i, 0), state="normal" if i else "disabled")
        menu.add_command(label="Move up", command=lambda: self._queue_move(i, i - 1), state="normal" if i else "disabled")
        menu.add_command(label="Move down", command=lambda: self._queue_move(i, i + 1),
                         state="normal" if i < n - 1 else "disabled")
        menu.add_command(label="Move to bottom", command=lambda: self._queue_move(i, n - 1),
                         state="normal" if i < n - 1 else "disabled")
        move_to = tk.Menu(menu, **style)
        swap = tk.Menu(menu, **style)
        for k, other in enumerate(queue_):
            if k == i:
                continue
            move_to.add_command(label=f"#{k + 1}", command=lambda k=k: self._queue_move(i, k))
            swap.add_command(label=f"#{k + 1}  {other.get('label', '?')}", command=lambda k=k: self._queue_swap(i, k))
        menu.add_cascade(label="Move to position", menu=move_to, state="normal" if n > 1 else "disabled")
        menu.add_cascade(label="Swap with", menu=swap, state="normal" if n > 1 else "disabled")
        menu.add_separator()
        menu.add_command(label="Remove from queue", command=lambda: self._queue_remove_at(i))
        return menu

    def _queue_context(self, event):
        ql = self.queue_list
        i = ql.nearest(event.y)
        if not (0 <= i < ql.size() and ql.bbox(i)):
            return
        if i not in ql.curselection():
            ql.selection_clear(0, "end")
            ql.selection_set(i)
        self._queue_menu_for(i).tk_popup(event.x_root, event.y_root)

    def queue_context_for_drop(self, drop_id, event):
        """Right-click on a card's "#n" badge: same menu, for that drop."""
        ids = [e["drop_id"] for e in self.settings.get("drop_queue", [])]
        if drop_id in ids:
            self._queue_menu_for(ids.index(drop_id)).tk_popup(event.x_root, event.y_root)

    def _queue_remove_at(self, i):
        queue_ = list(self.settings.get("drop_queue", []))
        if 0 <= i < len(queue_):
            removed = queue_.pop(i)
            self.log(f"Removed {removed.get('label', '?')} from the drop queue")
            self._save_queue(queue_, set())

    def on_queue_toggle(self, card_data, drop_id):
        queue_ = list(self.settings.get("drop_queue", []))
        existing = [e for e in queue_ if e["drop_id"] == drop_id]
        drop = next(d for d in card_data["drops"] if d["id"] == drop_id)
        if existing:
            queue_ = [e for e in queue_ if e["drop_id"] != drop_id]
            self.log(f"Removed {drop['name']} from the drop queue")
        else:
            queue_.append({"drop_id": drop_id, "campaign_id": card_data["id"], "game": card_data["slug"],
                           "game_name": card_data["game"], "label": f"{card_data['game']} · {drop['name']}",
                           "logins": [s for s, _ in card_data["streamers"]][:4]})
            self.log(f"Queued {drop['name']} ({card_data['game']}) at position {len(queue_)}")
        self._save_queue(queue_)

    def on_queue_remove(self):
        sel = self.queue_list.curselection()
        if not sel:
            return
        queue_ = list(self.settings.get("drop_queue", []))
        removed = [queue_[i] for i in sel if i < len(queue_)]
        queue_ = [e for i, e in enumerate(queue_) if i not in sel]
        for entry in removed:
            self.log(f"Removed {entry.get('label', '?')} from the drop queue")
        nxt = queue_[min(sel[0], len(queue_) - 1)]["drop_id"] if queue_ else None
        self._save_queue(queue_, {nxt} if nxt else set())

    def on_queue_clear_done(self):
        done = {r["drop_id"] for r in self.queue_rows if r["status"] in ("Claimed", "Expired", "Not found")}
        queue_ = [e for e in self.settings.get("drop_queue", []) if e["drop_id"] not in done]
        if len(queue_) != len(self.settings.get("drop_queue", [])):
            self.log(f"Cleared {len(self.settings['drop_queue']) - len(queue_)} finished drop(s) from the queue")
            self._save_queue(queue_)

    # ======================================================================
    # Settings
    # ======================================================================
    def on_theme_changed(self):
        self.settings["dark_mode"] = self.theme_var.get() == "monika"
        save_json(SETTINGS_FILE, self.settings)
        self.apply_theme()

    def on_toggle_startup(self):
        want = self.startup_var.get()
        try:
            set_run_on_startup(want)
            self.log("Will open when Windows starts" if want else "Removed from Windows startup")
        except OSError as e:
            self.startup_var.set(not want)
            messagebox.showerror("Startup setting", f"Could not change the startup setting:\n{e}")

    def on_toggle_unselected(self):
        """Turning on "mine games I haven't picked" needs a yes - it's easy to tick by accident."""
        on = bool(self.unselected_var.get())
        if on and not messagebox.askyesno(
                "Mine games you haven't picked?",
                "When your drop queue and picked games have nothing left to mine, the miner will watch "
                "campaigns for ANY other game on Twitch (for example Marvel Rivals or World of Warcraft).\n\n"
                "Leave this off to only mine the games you picked - the miner then waits for them "
                "instead.\n\nTurn it on?"):
            self.unselected_var.set(False)
            on = False
        self.settings["mine_unselected_confirmed"] = on
        self.log("Will also mine games you haven't picked" if on else "Only mining your picked games and queue")
        self.on_setting_changed()

    def on_setting_changed(self):
        self.settings["start_minimized"] = bool(self.minimized_var.get())
        self.settings["auto_mine_on_launch"] = bool(self.automine_var.get())
        self.settings["mine_unselected"] = bool(self.unselected_var.get())
        self.settings["prefer_streamer_drops"] = bool(self.prefer_var.get())
        for key, var, low in (("refresh_minutes", self.refresh_var, 2), ("scan_categories", self.cats_var, 50)):
            try:
                self.settings[key] = max(low, int(var.get()))
            except (tk.TclError, ValueError):
                pass
        save_json(SETTINGS_FILE, self.settings)
        self._wake_miners()

    # ======================================================================
    # Misc
    # ======================================================================
    def _open_channel(self, _event=None):
        if self.watch_login:
            webbrowser.open(f"{CLIENT_URL}/{self.watch_login}")

    def _open_tree_channel(self, _event=None):
        login = self.chan_tree.focus()
        if login:
            webbrowser.open(f"{CLIENT_URL}/{login}")

    def on_close(self):
        self.login_cancel.set()
        for s in self.sessions:
            self._stop_session(s)
        self.destroy()


if __name__ == "__main__":
    App(autostart="--autostart" in sys.argv).mainloop()
