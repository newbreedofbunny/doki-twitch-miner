"""
Doki Doki Literature Club look for Twitch Drop Miner - Monika edition.

  * Two palettes: "Doki Doki" (the pink polka-dot main menu) and
    "Just Monika" (Monika's starry space room), both with Monika-green accents.
  * Fonts (Riffic for headings/menus, Aller for text) are downloaded on first
    launch into %APPDATA% (their licences don't allow redistributing them) and
    loaded privately for this process only - nothing is installed on Windows.
  * DotCanvas   - background pattern that lines up across nested canvases,
                  optionally with Monika peeking in from the bottom-right corner.
  * MenuButton  - DDLC main-menu style button: white text with a coloured outline.
  * Sidebar     - the pink main-menu panel with the logo and the page menu.
"""

import glob
import io
import math
import os
import sys
import tkinter as tk
import urllib.request
import zipfile
from tkinter import font as tkfont

# Inside a PyInstaller .exe the bundled files are unpacked to sys._MEIPASS.
ASSETS = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "assets")
FONT_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TwitchDropClaimer", "fonts")
# (download page, files to take from the zip). Each user downloads their own personal-use copy.
FONT_SOURCES = [
    ("https://dl.dafont.com/dl/?f=riffic", ["RifficFree-Bold.ttf"]),
    ("https://www.fontsquirrel.com/fonts/download/aller", ["Aller_Rg.ttf", "Aller_Bd.ttf",
                                                             "Aller Font License.txt"]),
]
RIFFIC, ALLER = "Riffic Free Medium", "Aller"
MASCOT_W, MASCOT_H = 255, 264  # size of the corner image (incl. glow padding)

PALETTES = {
    "doki": {
        "bg": "#ffffff", "pattern": "dots", "dots": "#ffe6f4",
        "panel": "#ffffff", "panel_border": "#ffbde1", "field": "#fff7fb",
        "fg": "#3f2b36", "muted": "#a07e90", "trough": "#ffe3f1",
        "button": "#ffe6f4", "button_hover": "#ffd3ea", "button_active": "#ffbde1",
        "accent": "#4caf64", "accent_hover": "#3f9a56", "accent_dark": "#347f47",   # Monika green
        "select": "#ffe0ef", "success": "#f06ea9", "warn": "#dd8420", "danger": "#d6455d",
        "heading": "#bb5599", "outline": "#bb5599", "menu_fg": "#ffffff", "menu_hover": "#ffaacc",
        "menu_disabled": "#f3e6ed", "menu_disabled_outline": "#d9bfcd",
        "sidebar": "#ffe6f4", "sidebar_line": "#ffffff", "sidebar_edge": "#ffbde1",
        "logo_fill": "#ffffff", "logo_ring": "#ffbde1", "logo_heart": "#bfe3ff", "logo_outline": "#ffffff",
        "logo_shadow": "#f2c4dc", "paper": "#fffdf5", "paper_edge": "#e9d9c9", "paper_fg": "#4b3a44",
        # Like the DDLC logo: light pink, light blue, light purple, light red, light green only.
        #                D          R          O          P          M          I          N          E          R
        "logo_letters": ["#ff9fcf", "#86c9f5", "#c3a0f5", "#ff8e8e", "#92dd8c", "#ff9fcf", "#86c9f5",
                         "#ff8e8e", "#c3a0f5"],
        "strip": "Drop Club, {login}!",
    },
    "monika": {
        "bg": "#0d0716", "pattern": "stars",
        "stars": ["#ffffff", "#e8d9ff", "#ffc9e6", "#b9f5c8", "#8c7aa6", "#6a5a82"],
        "panel": "#1a1226", "panel_border": "#4a2f5c", "field": "#221733",
        "fg": "#f5ecf3", "muted": "#a996b5", "trough": "#2c1f3b",
        "button": "#2a1d3a", "button_hover": "#36264b", "button_active": "#44305d",
        "accent": "#5fd07a", "accent_hover": "#4fbd6a", "accent_dark": "#3fa35a",
        "select": "#35244a", "success": "#ff8fc5", "warn": "#f0a53a", "danger": "#e0556d",
        "heading": "#ff9ed2", "outline": "#2f8a4a", "menu_fg": "#ffffff", "menu_hover": "#b9f5c8",
        "menu_disabled": "#5d4d6d", "menu_disabled_outline": "#2c1f3b",
        "sidebar": "#140c1f", "sidebar_line": "#241834", "sidebar_edge": "#5fd07a",
        "logo_fill": "#1a1226", "logo_ring": "#5fd07a", "logo_heart": "#3a2455", "logo_outline": "#0d0716",
        "logo_shadow": "#05030a", "paper": "#241836", "paper_edge": "#4a2f5c", "paper_fg": "#b9f5c8",
        "logo_letters": ["#5fd07a", "#ff9ed2", "#5fd07a", "#ff9ed2", "#5fd07a", "#ff9ed2", "#5fd07a",
                         "#ff9ed2", "#5fd07a"],
        "strip": "Just Monika.",
    },
}


# --------------------------------------------------------------------------
# Fonts
# --------------------------------------------------------------------------
_loaded_fonts = set()


def _font_files():
    """Font files next to the app (dev copy) and downloaded into %APPDATA%."""
    return glob.glob(os.path.join(ASSETS, "fonts", "*.ttf")) + glob.glob(os.path.join(FONT_DIR, "*.ttf"))


def load_private_fonts():
    """Make the fonts available to this process only. Safe to call again after a download."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        for path in _font_files():
            if path not in _loaded_fonts:
                ctypes.windll.gdi32.AddFontResourceExW(path, 0x10, 0)  # FR_PRIVATE
                _loaded_fonts.add(path)
    except Exception:
        pass


def missing_fonts():
    have = {os.path.basename(p) for p in _font_files()}
    return [(url, files) for url, files in FONT_SOURCES
            if any(f.endswith(".ttf") and f not in have for f in files)]


def download_fonts():
    """First launch: fetch the fonts from their official download pages. Returns True if all present."""
    os.makedirs(FONT_DIR, exist_ok=True)
    for url, files in missing_fonts():
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        data = urllib.request.urlopen(req, timeout=60).read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            members = {os.path.basename(n): n for n in zf.namelist()}
            for name in files:
                if name in members:
                    with open(os.path.join(FONT_DIR, name), "wb") as out:
                        out.write(zf.read(members[name]))
    return not missing_fonts()


# role -> (typeface, size, weight). "riffic"/"aller" fall back to Windows fonts until downloaded.
FONT_SPECS = {
    "body": ("aller", 10, "normal"), "body_b": ("aller", 10, "bold"), "small": ("aller", 9, "normal"),
    "small_b": ("aller", 9, "bold"), "mono": ("Consolas", 9, "normal"),
    # badge: Riffic's pointed capital A gets squashed by Windows' rasteriser below 10pt
    "panel": ("riffic", 12, "normal"), "card_title": ("riffic", 11, "normal"), "tab": ("riffic", 10, "normal"),
    "badge": ("riffic", 10, "normal"), "button": ("riffic", 12, "normal"), "button_big": ("riffic", 16, "normal"),
    "menu": ("riffic", 19, "normal"), "logo": ("riffic", 26, "normal"), "title": ("riffic", 12, "normal"),
    "paper": ("hand", 10, "bold"),
}


def _families(root):
    families = set(tkfont.families(root))
    riffic = RIFFIC if RIFFIC in families else "Segoe UI Black"
    aller = ALLER if ALLER in families else "Segoe UI"
    hand = "Segoe Print" if "Segoe Print" in families else aller
    return {"riffic": riffic, "aller": aller, "hand": hand}


def make_fonts(root):
    fam = _families(root)
    return {role: tkfont.Font(root=root, family=fam.get(face, face), size=size, weight=weight)
            for role, (face, size, weight) in FONT_SPECS.items()}


def refresh_fonts(root, fonts):
    """Switch existing named fonts to Riffic/Aller once they're available (widgets update live)."""
    load_private_fonts()
    fam = _families(root)
    for role, (face, _size, _weight) in FONT_SPECS.items():
        fonts[role].configure(family=fam.get(face, face))
    return fam["riffic"] == RIFFIC and fam["aller"] == ALLER


# --------------------------------------------------------------------------
# Drawing helpers
# --------------------------------------------------------------------------
def _outline_offsets(width):
    return [(dx, dy) for dx in range(-width, width + 1) for dy in range(-width, width + 1)
            if (dx or dy) and dx * dx + dy * dy <= width * width + 1]


def draw_outlined(canvas, x, y, text, font, fill, outline, width=2, anchor="nw", angle=0, tags=()):
    """Text with a solid outline, like DDLC's menu and logo lettering."""
    tags = tuple(tags)
    for dx, dy in _outline_offsets(width):
        canvas.create_text(x + dx, y + dy, text=text, font=font, fill=outline, anchor=anchor,
                           angle=angle, tags=tags + ("outline",))
    return canvas.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor, angle=angle,
                              tags=tags + ("fill",))


def _mix(i, j):
    h = (i * 374761393 + j * 668265263) & 0xFFFFFFFF
    h = ((h ^ (h >> 13)) * 1274126177) & 0xFFFFFFFF
    return h ^ (h >> 16)


def draw_pattern(canvas, p, ox, oy, x0, y0, x1, y1, tag="pattern"):
    """Background pattern for the local area (x0,y0)-(x1,y1); (ox,oy) = canvas offset in the
    window, so every canvas draws the same global pattern and edges line up."""
    if p["pattern"] == "dots":
        spacing, r = 96, 30
        for j in range(math.floor((oy + y0 - r) / spacing), math.floor((oy + y1 + r) / spacing) + 1):
            for i in range(math.floor((ox + x0 - r) / spacing), math.floor((ox + x1 + r) / spacing) + 1):
                if (i + j) % 2:
                    continue
                cx, cy = i * spacing - ox, j * spacing - oy
                canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=p["dots"], outline="", tags=tag)
    else:
        cell = 44
        colors = p["stars"]
        for j in range(math.floor((oy + y0) / cell) - 1, math.floor((oy + y1) / cell) + 1):
            for i in range(math.floor((ox + x0) / cell) - 1, math.floor((ox + x1) / cell) + 1):
                h = _mix(i, j)
                if h % 100 >= 42:
                    continue
                sx = i * cell + (h >> 8) % cell - ox
                sy = j * cell + (h >> 16) % cell - oy
                k = (h >> 24) % 24
                r = 2.2 if k == 0 else 1.5 if k < 6 else 0.9
                color = colors[0 if k == 0 else (h >> 4) % len(colors)]
                canvas.create_oval(sx - r, sy - r, sx + r, sy + r, fill=color, outline="", tags=tag)


def heart_points(cx, cy, size, steps=40):
    pts = []
    for n in range(steps):
        t = 2 * math.pi * n / steps
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts += [cx + x * size / 32, cy - y * size / 32]
    return pts


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------
class DotCanvas(tk.Canvas):
    """A canvas with the theme background; with mascot=True Monika sits in its bottom-right corner.
    With scroll=True the pattern covers the scroll region (it scrolls with the content)."""

    def __init__(self, master, app, mascot=False, scroll=False, **kw):
        super().__init__(master, highlightthickness=0, bd=0, **kw)
        self.app = app
        self.mascot = mascot
        self.scroll = scroll
        self.content_height = 0
        self.bind("<Configure>", lambda e: self.redraw(), add="+")
        app.dot_canvases.append(self)

    def redraw(self):
        p = self.app.p
        self.configure(bg=p["bg"])
        self.delete("pattern")
        self.delete("mascot")
        w, h = self.winfo_width(), self.winfo_height()
        if w <= 1 or h <= 1:
            return
        top = self.winfo_toplevel()
        ox = self.winfo_rootx() - top.winfo_rootx()
        oy = self.winfo_rooty() - top.winfo_rooty()
        draw_pattern(self, p, ox, oy, 0, 0, w, max(h, self.content_height) if self.scroll else h)
        if self.mascot and self.app.mascot_img is not None:
            self.create_image(w, h, image=self.app.mascot_img, anchor="se", tags="mascot")
            self.tag_lower("mascot")
        self.tag_lower("pattern")


class MenuButton(tk.Canvas):
    """DDLC main-menu style button: white Riffic text with a coloured outline."""

    def __init__(self, master, app, text, command, size="button", bg_key="panel", pad=(6, 3)):
        super().__init__(master, highlightthickness=0, bd=0, cursor="hand2")
        self.app, self.text, self.command = app, text, command
        self.size, self.bg_key, self.pad = size, bg_key, pad
        self.state, self.hover = "normal", False
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonRelease-1>", self._click)
        app.menu_buttons.append(self)
        self.redraw()

    def set(self, text=None, state=None):
        if text is not None:
            self.text = text
        if state is not None:
            self.state = state
            self.configure(cursor="hand2" if state == "normal" else "arrow")
        self.redraw()

    def _set_hover(self, hover):
        self.hover = hover
        self.redraw()

    def _click(self, event):
        if self.state == "normal" and 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height():
            self.command()

    def redraw(self):
        p = self.app.p
        font = self.app.fonts[self.size]
        px, py = self.pad
        width = 3 if self.size == "button_big" else 2
        self.configure(width=font.measure(self.text) + 2 * px + 2 * width,
                       height=font.metrics("linespace") + 2 * py, bg=p[self.bg_key])
        self.delete("all")
        if self.state != "normal":
            fill, outline = p["menu_disabled"], p["menu_disabled_outline"]
        else:
            fill, outline = (p["menu_hover"] if self.hover else p["menu_fg"]), p["outline"]
        draw_outlined(self, px + width, py, self.text, font, fill, outline, width=width)


class Sidebar(tk.Canvas):
    """The pink main-menu panel: circular logo, page menu and a status footer."""

    WIDTH = 250
    ITEMS = [("mining", "Mining"), ("campaigns", "Campaigns"), ("settings", "Settings"), ("quit", "Quit")]

    def __init__(self, master, app, on_select):
        super().__init__(master, width=self.WIDTH, highlightthickness=0, bd=0)
        self.app = app
        self.on_select = on_select
        self.current = "mining"
        self.status = ""
        self.claimed = 0
        self.login = ""
        self.bind("<Configure>", lambda e: self.redraw())
        # Bound once: tag bindings outlive the items, and tkinter never frees a tag_bind's callback
        # (re-binding on every redraw slowly leaked memory).
        for key, _label in self.ITEMS:
            tag = f"item_{key}"
            self.tag_bind(tag, "<Enter>", lambda e, k=key: self._hover(k, True))
            self.tag_bind(tag, "<Leave>", lambda e, k=key: self._hover(k, False))
            self.tag_bind(tag, "<ButtonRelease-1>", lambda e, k=key: self.on_select(k))

    def set_status(self, text):
        self.status = text
        self.itemconfigure("status", text=text)

    def set_claimed(self, n):
        self.claimed = n
        self.itemconfigure("claimed", text=f"Claimed this session: {n}  ♥")

    def select(self, key):
        self.current = key
        self.redraw()

    def redraw(self):
        self.delete("all")
        p, fonts = self.app.p, self.app.fonts
        w, h = self.WIDTH, max(self.winfo_height(), 600)
        self.create_rectangle(0, 0, w, h, fill=p["sidebar"], outline="")
        self.create_rectangle(w - 12, 0, w - 5, h, fill=p["sidebar_line"], outline="")
        self.create_rectangle(w - 5, 0, w, h, fill=p["sidebar_edge"], outline="")
        self._logo(w / 2 - 4, 122)

        y = 292
        for key, label in self.ITEMS:
            tag = f"item_{key}"
            self.create_rectangle(14, y - 22, w - 18, y + 22, fill=p["sidebar"], outline="", tags=(tag,))
            selected = key == self.current
            if selected:
                self.create_polygon(heart_points(30, y + 1, 18), fill=p["outline"], smooth=True, tags=(tag,))
            draw_outlined(self, 46, y, label, fonts["menu"], p["menu_hover"] if selected else p["menu_fg"],
                          p["outline"], width=3, anchor="w", tags=(tag,))
            y += 48

        self.create_text(22, h - 74, anchor="nw", text=self.status, width=w - 44, font=fonts["small"],
                         fill=p["muted"], tags=("status",))
        self.create_text(22, h - 22, anchor="w", text=f"Claimed this session: {self.claimed}  ♥",
                         font=fonts["small_b"], fill=p["heading"], tags=("claimed",))
        self.configure(cursor="")

    def _hover(self, key, on):
        p = self.app.p
        selected = key == self.current
        self.itemconfigure(f"item_{key}&&fill", fill=p["menu_hover"] if (on or selected) else p["menu_fg"])
        self.configure(cursor="hand2" if on else "")

    def _paper(self, cx, cy, text, angle, font):
        p = self.app.p
        tw, th = font.measure(text) + 22, font.metrics("linespace") + 8
        a = math.radians(angle)
        corners = [(-tw / 2, -th / 2), (tw / 2, -th / 2), (tw / 2, th / 2), (-tw / 2, th / 2)]
        pts = []
        for x, y in corners:
            pts += [cx + x * math.cos(a) + y * math.sin(a), cy - x * math.sin(a) + y * math.cos(a)]
        self.create_polygon([v + 2 for v in pts], fill=p["logo_shadow"], outline="")
        self.create_polygon(pts, fill=p["paper"], outline=p["paper_edge"])
        self.create_text(cx, cy, text=text, font=font, fill=p["paper_fg"], angle=angle)

    def _logo(self, cx, cy):
        p, fonts = self.app.p, self.app.fonts
        r = 104
        self.create_oval(cx - r, cy - r, cx + r, cy + r, fill=p["logo_fill"], outline=p["logo_ring"], width=10)
        self.create_polygon(heart_points(cx + 6, cy + 4, 150), fill=p["logo_heart"], smooth=True, outline="")
        self._paper(cx - 10, cy - 76, "Welcome to:", 4, fonts["paper"])
        colors = p["logo_letters"]
        tilts = [-7, 5, -4, 8, -6, 3, -7, 6, -3]
        for word, y, start in (("DROP", cy - 26, 0), ("MINER", cy + 24, 4)):
            font = fonts["logo"]
            widths = [font.measure(ch) + (6 if ch == "I" else 1) for ch in word]
            x = cx - sum(widths) / 2
            for n, ch in enumerate(word):
                k = start + n
                lx = x + widths[n] / 2
                self.create_text(lx + 2, y + 3, text=ch, font=font, fill=p["logo_shadow"], angle=tilts[k])
                draw_outlined(self, lx, y, ch, font, colors[k % len(colors)], p["logo_outline"], width=3,
                              anchor="center", angle=tilts[k])
                x += widths[n]
        strip = p["strip"].format(login=self.login or "Player")
        self._paper(cx + 8, cy + 78, strip, -3, fonts["paper"])
