"""
Twitch access for Twitch Drop Miner: settings, saved accounts and the API client.

Only the Python standard library is used.
"""

import base64
import http.client
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import deque
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Settings. Twitch changes the GQL hashes from time to time; if requests start
# failing with "PersistedQueryNotFound", update them in settings.json.
# --------------------------------------------------------------------------
APP_VERSION = "1.0.7"

DEFAULTS = {
    "client_id": "ue6666qo983tsx6so1t0vnawi233wa",  # Twitch Android TV app (supports device login)
    "user_agent": (
        "Dalvik/2.1.0 (Linux; U; Android 16; SM-S911B Build/TP1A.220624.014) "
        "tv.twitch.android.app/25.3.0/2503006"
    ),
    "hashes": {
        "Inventory": "8337eb8541b314040b0edde0c09c5c7a2783ba1960aa9edfbf3bac16d0fec404",
        "DropsPage_ClaimDropRewards": "a455deea71bdc9015b78eb49f4acfbce8baa7ccbedd28e549bb025bd0f751930",
        "DropsHighlightService_AvailableDrops": "782dad0f032942260171d2d80a654f88bdd0c5a9dddc392e9bc92218a0f42d20",
        "DropCurrentSessionContext": "4d06b702d25d652afb9ef835d2a550031f1cf762b193523a92166f40ea3d142b",
        "VideoPlayerStreamInfoOverlayChannel": "198492e0857f6aedead9665c81c5a06d67b25b58034649687124083ff288597d",
        "DirectoryPage_Game": "86bcceb4e8b1a51256ff8eed8bd8aae4acacf80d737efe904f84f3aeadf8cafd",
        "BrowsePage_AllDirectories": "2f67f71ba89f3c0ed26a141ec00da1defecb2303595f5cda4298169549783d9e",
        "PlaybackAccessToken": "ed230aa1e33e07eebb8928504583da78a5173989fadfb1ac94be06a04f3cdbe9",
    },
    "priority_games": [],        # games picked in the Campaigns tab, mined top to bottom
    "game_slugs": {},            # picked game name -> Twitch category slug
    "drop_queue": [],            # specific drops to mine first, in order
    "mining_disabled": [],       # account logins unticked in the Accounts table (all others mine)
    "autoclaim_disabled": [],    # account logins whose finished drops are left for you to claim
    "prefer_streamer_drops": True,  # within a game, streamer-specific drops before general ones
    "mine_unselected": False,    # mine other campaigns when the picked games have nothing
    "mine_unselected_confirmed": False,  # set when the user OKs the "mine games I haven't picked" prompt
    "refresh_minutes": 10,       # how often to rescan Twitch for campaigns
    "scan_categories": 300,      # how many of Twitch's top categories to check for drops
    "auto_mine_on_launch": True, # start mining as soon as the app opens (if logged in)
    "start_minimized": False,    # when launched by Windows at startup
    "dark_mode": False,
}

WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
CLIENT_URL = "https://www.twitch.tv"
GQL_URL = "https://gql.twitch.tv/gql"
DEVICE_URL = "https://id.twitch.tv/oauth2/device"
TOKEN_URL = "https://id.twitch.tv/oauth2/token"
VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
DROPS_PAGE_URL = "https://www.twitch.tv/drops/campaigns"
SPADE_PATTERN = r'"spade_?url": ?"(https://[.\w\-/]+)"'
SETTINGS_JS_PATTERN = r'src="(https://[\w.]+/config/settings\.[0-9a-f]{32}\.js)"'

APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TwitchDropClaimer")
AUTH_FILE = os.path.join(APP_DIR, "auth.json")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")


def load_json(path, default):
    try:
        # utf-8-sig: also accepts files saved with a BOM (Notepad, Windows PowerShell)
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_settings():
    settings = json.loads(json.dumps(DEFAULTS))
    saved = load_json(SETTINGS_FILE, None)
    if saved is None or not isinstance(saved, dict):
        if os.path.exists(SETTINGS_FILE):
            # Unreadable (e.g. hand-edited): keep a copy instead of silently overwriting it with defaults.
            stamp = time.strftime("%Y%m%d-%H%M%S")
            try:
                os.replace(SETTINGS_FILE, SETTINGS_FILE.replace(".json", f".unreadable-{stamp}.json"))
            except OSError:
                pass
        saved = {}
    if saved.get("games") and not saved.get("priority_games"):
        saved["priority_games"] = saved["games"]  # migrate the old typed-in games list
    for key, value in saved.items():
        if key == "hashes" and isinstance(value, dict):
            settings["hashes"].update(value)
        elif key in settings:
            settings[key] = value
    if settings["mine_unselected"] and not settings["mine_unselected_confirmed"]:
        # Before v1.0.1 this was a single unconfirmed checkbox that was easy to tick by accident,
        # which made the miner wander off to games nobody picked. Keep it only once confirmed.
        settings["mine_unselected"] = False
    return settings


def parse_ts(value):
    if not value:
        return None
    try:
        value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def utcnow():
    return datetime.now(timezone.utc)


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower().replace("'", "")).strip("-")


def game_slug(settings, name):
    """Twitch category slug for a picked game (remembered when it was picked)."""
    return (settings.get("game_slugs") or {}).get(name) or slugify(name)


# (patterns in the error text, short cause, what to do) - shown on the red banner and in the log
VIDEO_ERROR_HELP = [
    (("getaddrinfo", "11001", "11002", "11004", "name or service not known", "nodename nor servname"),
     "your DNS can't find Twitch's video servers",
     "A DNS blocker is the usual cause: Pi-hole, AdGuard, NextDNS, router parental controls, or a VPN's "
     "ad / tracker blocking (Mullvad, Proton NetShield, Nord Threat Protection). Allow-list *.ttvnw.net or "
     "turn the blocker off."),
    (("certificate_verify_failed", "certificate verify failed", "sslcertverificationerror", "self-signed",
      "unable to get local issuer"),
     "something is intercepting secure connections",
     "Usually an antivirus 'HTTPS scanning' / 'web shield' (Avast, AVG, Kaspersky, ESET, Bitdefender) or a "
     "school / work network. Turn HTTPS scanning off or add an exception for DokiTwitchMiner.exe."),
    (("10013", "forbidden by its access permissions", "permissionerror"),
     "a firewall or antivirus is blocking this app's internet access",
     "Allow DokiTwitchMiner.exe in Windows Firewall and in your antivirus."),
    (("timed out", "10060", "timeouterror"),
     "connections to Twitch's video servers are timing out",
     "A VPN, firewall or your network may be dropping them - try without the VPN and check the firewall."),
    (("10061", "connection refused", "connectionrefusederror"),
     "connections to Twitch's video servers are being refused",
     "A proxy, firewall or VPN is in the way - try without them."),
    (("10054", "10053", "connectionreset", "connectionaborted", "remotedisconnected"),
     "the connection to Twitch's video servers keeps getting cut",
     "Usually a VPN, an antivirus 'web shield' / HTTPS scanning, or unstable Wi-Fi. Try without the VPN, pause "
     "the web shield (or add an exception for DokiTwitchMiner.exe), restart the router or use Ethernet."),
]


def explain_video_error(error):
    """(cause, fix) for a video-server error text, in plain words."""
    text = (error or "").lower()
    for patterns, cause, fix in VIDEO_ERROR_HELP:
        if any(p in text for p in patterns):
            return cause, fix
    return ("Twitch's video servers can't be reached",
            "Check your internet connection, VPN, firewall / antivirus and any ad-block DNS.")


class TwitchError(Exception):
    pass


VIDEO_HISTORY = 15  # watch reports remembered for the banner (~5 min at one every 20 s)


def is_connection_cut(error):
    """True when a connection was reset / closed part-way (flaky VPN, Wi-Fi, antivirus) - worth one quick
    retry. DNS failures, timeouts, refused connections and certificate errors are not."""
    if isinstance(error, TwitchError):
        error = error.__cause__
    if isinstance(error, urllib.error.URLError):
        error = error.reason
    return (isinstance(error, (ConnectionError, http.client.HTTPException, ssl.SSLEOFError))
            and not isinstance(error, ConnectionRefusedError))


# --------------------------------------------------------------------------
# Saved accounts
# --------------------------------------------------------------------------
class AccountStore:
    """Every Twitch account the user has logged in with, plus the active one."""

    def __init__(self):
        data = load_json(AUTH_FILE, {})
        self.device_id = data.get("device_id") or uuid.uuid4().hex
        self.accounts = [a for a in data.get("accounts") or [] if a.get("token")]
        self.active = data.get("active")
        if data.get("access_token") and not self.accounts:
            # Older single-account file: login gets filled in after validation.
            self.accounts = [{"login": None, "user_id": None, "token": data["access_token"]}]

    def save(self):
        save_json(AUTH_FILE, {"device_id": self.device_id, "accounts": self.accounts, "active": self.active})

    def logins(self):
        return [a["login"] for a in self.accounts if a.get("login")]

    def get(self, login):
        return next((a for a in self.accounts if a.get("login") == login), None)

    def upsert(self, login, user_id, token):
        self.accounts = [a for a in self.accounts if a.get("login") not in (login, None)]
        self.accounts.append({"login": login, "user_id": user_id, "token": token})
        self.active = login
        self.save()

    def remove(self, login):
        self.accounts = [a for a in self.accounts if a.get("login") != login]
        if self.active == login:
            self.active = self.accounts[0]["login"] if self.accounts else None
        self.save()


# --------------------------------------------------------------------------
# API client (one per account)
# --------------------------------------------------------------------------
class TwitchClient:
    def __init__(self, settings, device_id, account=None):
        self.s = settings
        self.device_id = device_id
        account = account or {}
        self.token = account.get("token")
        self.login = account.get("login")
        self.user_id = account.get("user_id")
        self.session_id = uuid.uuid4().hex[:16]
        self.spade_url = None
        self._last_spade = 0
        self._hls = None           # (login, media playlist url, fetched_at)
        self.video_error = None    # last error reaching Twitch's video servers (kept for the banner)
        self.video_failures = 0    # watch reports in a row that failed with a network error
        self.video_history = deque(maxlen=VIDEO_HISTORY)  # recent watch reports: True = got through

    # -- HTTP helpers ------------------------------------------------------
    def _base_headers(self):
        return {
            "Client-Id": self.s["client_id"],
            "User-Agent": self.s["user_agent"],
            "X-Device-Id": self.device_id,
            "Origin": CLIENT_URL,
            "Referer": CLIENT_URL,
        }

    def _request(self, url, data=None, headers=None, form=False, raw=False, retry=False):
        """(status, body). retry=True: a connection cut part-way (flaky VPN / Wi-Fi / antivirus) is
        retried once right away - only for requests that are safe to repeat."""
        body = None
        hdrs = self._base_headers()
        hdrs.update(headers or {})
        if data is not None:
            if form:
                body = urllib.parse.urlencode(data).encode()
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                body = json.dumps(data).encode()
                hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=hdrs)
        try:
            return self._open(req, raw)
        except TwitchError as e:
            if not (retry and is_connection_cut(e)):
                raise
            return self._open(req, raw)

    def _open(self, req, raw):
        url = req.full_url
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
                return resp.status, (text if raw else json.loads(text or "{}"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if raw:
                return e.code, text
            try:
                return e.code, json.loads(text or "{}")
            except ValueError:
                return e.code, {}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise TwitchError(f"Network error: {getattr(e, 'reason', e)}") from e
        except (http.client.HTTPException, OSError, ValueError) as e:
            # e.g. a maintenance / captive-portal page instead of JSON, or a download cut off half-way.
            # Must surface as TwitchError so callers retry instead of crashing.
            raise TwitchError(f"Unexpected response from {urllib.parse.urlsplit(url).netloc}: {e!r}") from e

    # -- Auth --------------------------------------------------------------
    def validate(self):
        """Return True if the token is valid (and fill in login / user_id)."""
        if not self.token:
            return False
        status, data = self._request(VALIDATE_URL, headers={"Authorization": f"OAuth {self.token}"})
        if status == 200:
            self.login = data.get("login")
            self.user_id = data.get("user_id")
            return True
        return False

    def start_device_login(self):
        status, data = self._request(
            DEVICE_URL, {"client_id": self.s["client_id"], "scopes": ""}, form=True
        )
        if status != 200:
            raise TwitchError(f"Device login failed ({status}): {data}")
        return data  # device_code, user_code, verification_uri, expires_in, interval

    def poll_device_login(self, device, cancel_event):
        """Wait for the user to approve the code. Returns True once logged in."""
        deadline = time.time() + int(device.get("expires_in", 1800))
        interval = int(device.get("interval", 5))
        while time.time() < deadline and not cancel_event.is_set():
            status, data = self._request(
                TOKEN_URL,
                {
                    "client_id": self.s["client_id"],
                    "device_code": device["device_code"],
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                form=True,
            )
            if status == 200 and "access_token" in data:
                self.token = data["access_token"]
                return self.validate()
            if data.get("message") not in (None, "authorization_pending"):
                if data.get("message") == "slow_down":
                    interval += 5
                else:
                    raise TwitchError(f"Login failed: {data.get('message')}")
            cancel_event.wait(interval)
        return False

    # -- GQL ---------------------------------------------------------------
    def gql(self, op_name, variables, retry=True):
        payload = {
            "operationName": op_name,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.s["hashes"][op_name]}},
        }
        headers = {"Client-Session-Id": self.session_id}
        if self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        status, data = self._request(GQL_URL, payload, headers=headers, retry=retry)
        if status == 401:
            raise TwitchError("Token expired or revoked. Please log in again.")
        if status != 200:
            raise TwitchError(f"GQL {op_name} HTTP {status}: {data}")
        if isinstance(data, dict) and data.get("errors"):
            msgs = ", ".join(e.get("message", "?") for e in data["errors"])
            if "PersistedQueryNotFound" in msgs:
                msgs += f" (update the hash for {op_name} in {SETTINGS_FILE})"
            raise TwitchError(f"GQL {op_name} error: {msgs}")
        return data.get("data") or {}

    def get_inventory(self):
        data = self.gql("Inventory", {"fetchRewardCampaigns": False})
        return ((data.get("currentUser") or {}).get("inventory")) or {}

    def get_top_categories(self, count):
        """Twitch's categories ordered by current viewers."""
        cats, cursor = [], None
        while len(cats) < count:
            variables = {
                "limit": min(100, count - len(cats)),
                "options": {"recommendationsContext": {"platform": "web"}, "requestID": "JIRA-VXP-2397",
                            "sort": "VIEWER_COUNT", "tags": []},
            }
            if cursor:
                variables["cursor"] = cursor
            conn = self.gql("BrowsePage_AllDirectories", variables).get("directoriesWithTags") or {}
            edges = conn.get("edges") or []
            for edge in edges:
                node = edge.get("node") or {}
                if node.get("slug"):
                    cats.append({"id": str(node.get("id")), "slug": node["slug"],
                                 "name": node.get("displayName") or node.get("name") or node["slug"]})
            if not edges or not (conn.get("pageInfo") or {}).get("hasNextPage"):
                break
            cursor = edges[-1].get("cursor")
        return cats

    def get_channel_campaigns(self, channel_id):
        """Drop campaigns that can currently be progressed on a channel."""
        data = self.gql("DropsHighlightService_AvailableDrops", {"channelID": str(channel_id)})
        return ((data.get("channel") or {}).get("viewerDropCampaigns")) or []

    def get_current_session(self, channel_id):
        data = self.gql("DropCurrentSessionContext", {"channelID": str(channel_id), "channelLogin": ""})
        return ((data.get("currentUser") or {}).get("dropCurrentSession")) or None

    def get_stream_info(self, login):
        """Return a channel dict if `login` is live, else None."""
        data = self.gql("VideoPlayerStreamInfoOverlayChannel", {"channel": login})
        user = data.get("user") or {}
        stream = user.get("stream")
        if not stream:
            return None
        game = (user.get("broadcastSettings") or {}).get("game") or {}
        return {
            "id": user.get("id"),
            "login": user.get("login") or login,
            "display": user.get("displayName") or login,
            "broadcast_id": stream.get("id"),
            "viewers": stream.get("viewersCount") or 0,
            "game_id": str(game.get("id") or ""),
            "game_name": game.get("displayName") or game.get("name") or "",
        }

    def get_game_streams(self, slug, limit=30):
        """(game, channels): a game's live drops-enabled streams, most relevant first.
        game is None when Twitch doesn't know the slug."""
        data = self.gql("DirectoryPage_Game", {
            "limit": limit,
            "slug": slug,
            "imageWidth": 50,
            "includeCostreaming": False,
            "options": {
                "broadcasterLanguages": [],
                "freeformTags": None,
                "includeRestricted": ["SUB_ONLY_LIVE"],
                "recommendationsContext": {"platform": "web"},
                "sort": "RELEVANCE",
                "systemFilters": ["DROPS_ENABLED"],
                "tags": [],
                "requestID": "JIRA-VXP-2397",
            },
            "sortTypeIsRecency": False,
        })
        game = data.get("game")
        if not game:
            return None, []
        channels = []
        for edge in (game.get("streams") or {}).get("edges") or []:
            node = edge.get("node") or {}
            b = node.get("broadcaster") or {}
            g = node.get("game") or game
            if not b.get("login"):
                continue
            channels.append({
                "id": b.get("id"),
                "login": b.get("login"),
                "display": b.get("displayName") or b.get("login"),
                "broadcast_id": node.get("id"),
                "viewers": node.get("viewersCount") or 0,
                "game_id": str(g.get("id") or ""),
                "game_name": g.get("displayName") or g.get("name") or "",
            })
        return game, channels

    def claim(self, drop_instance_id):
        data = self.gql(  # no automatic retry: a claim isn't repeated blindly
            "DropsPage_ClaimDropRewards", {"input": {"dropInstanceID": drop_instance_id}}, retry=False
        )
        result = data.get("claimDropRewards") or {}
        return result.get("status", "OK")

    # -- Watching ------------------------------------------------------------
    def _get_spade_url(self, login):
        headers = {"User-Agent": WEB_USER_AGENT}
        status, html = self._request(f"{CLIENT_URL}/{login}", headers=headers, raw=True)
        match = re.search(SPADE_PATTERN, html, re.I)
        if not match:
            js = re.search(SETTINGS_JS_PATTERN, html, re.I)
            if not js:
                raise TwitchError("Could not find Twitch's watch-tracking URL (step 1)")
            status, settings_js = self._request(js.group(1), headers=headers, raw=True)
            match = re.search(SPADE_PATTERN, settings_js, re.I)
            if not match:
                raise TwitchError("Could not find Twitch's watch-tracking URL (step 2)")
        return match.group(1)

    def send_watch(self, channel):
        """Report watching `channel` (call every ~20 s). True if Twitch's video servers answered for
        every segment, False if the stream / Twitch said no, None on a network problem (that's not the
        channel's fault; it's counted in video_failures / video_history for the banner instead).

        Since early Oct 2026 Twitch only counts watch time when the stream's video is actually
        requested; the spade "minute-watched" event alone no longer counts. So, like
        TwitchDropsMiner (commit f693b0c), fetch the live playlist and HEAD every video segment
        in it - HEAD only asks for the segment, nothing is downloaded. The spade event is still
        sent once a minute, best effort.
        """
        ok = self._watch_playlist(channel)
        if time.time() - self._last_spade >= 55:
            self._last_spade = time.time()
            self._send_spade(channel)
        return ok

    def _send_spade(self, channel):
        try:
            if self.spade_url is None:
                self.spade_url = self._get_spade_url(channel["login"])
            event = self._watch_event(channel)
            encoded = base64.b64encode(json.dumps(event, separators=(",", ":")).encode()).decode()
            self._request(self.spade_url, {"data": encoded}, headers={"User-Agent": WEB_USER_AGENT},
                          form=True, raw=True)
        except TwitchError:
            self.spade_url = None  # often blocked by ad-block DNS; not needed for progress any more

    def _playlist_url(self, channel):
        """The stream's media playlist URL (cached ~10 min; the access token is short-lived)."""
        cached = self._hls
        if cached and cached[0] == channel["login"] and time.time() - cached[2] < 600:
            return cached[1]
        data = self.gql("PlaybackAccessToken", {
            "isLive": True, "isVod": False, "login": channel["login"],
            "platform": "web", "playerType": "site", "vodID": "",
        })
        tok = data.get("streamPlaybackAccessToken") or {}
        if not tok:
            return None
        url = (f"https://usher.ttvnw.net/api/channel/hls/{channel['login']}.m3u8?"
               + urllib.parse.urlencode({"sig": tok["signature"], "token": tok["value"]}))
        status, master = self._request(url, raw=True, retry=True)
        variants = [l.strip() for l in master.splitlines() if l.startswith("http")]
        if status != 200 or not variants:
            return None  # offline (usher answers 404) or not watchable
        self._hls = (channel["login"], variants[-1], time.time())  # last = smallest quality
        return variants[-1]

    def _watch_playlist(self, channel):
        try:
            url = self._playlist_url(channel)
            if not url:
                return False
            status, media = self._request(url, raw=True, retry=True)
            segments = [l.strip() for l in media.splitlines() if l.startswith("http")]
            if status != 200 or not segments:
                self._hls = None  # stream ended or token expired: get a fresh playlist next time
                return False
            ok = self._head_all(segments)
        except TwitchError as e:
            self.video_error = str(e)
            self.video_failures += 1
            self.video_history.append(False)
            if self.video_failures >= 2:
                self._hls = None  # keeps failing: fetch a fresh playlist (maybe from another server)
            return None  # one-off: keep the playlist, so the next report is 1 request instead of 3
        self.video_failures = 0
        self.video_history.append(True)
        return ok

    def _head_all(self, urls):
        """HEAD each URL, reusing one connection per server. True if all answered 200.
        Video servers may close a reused connection at any time, and flaky networks cut new ones: a
        reused connection that fails, or a new one that's cut, is retried once on a fresh connection
        instead of being reported as "can't be reached"."""
        conns = {}

        def head(netloc, path, retried=False):
            conn = conns.get(netloc)
            reused = conn is not None
            if conn is None:
                conn = conns[netloc] = http.client.HTTPSConnection(netloc, timeout=20)
            try:
                conn.request("HEAD", path, headers={"User-Agent": WEB_USER_AGENT})
                resp = conn.getresponse()
                resp.read()
                return resp.status
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                del conns[netloc]
                if retried or not (reused or is_connection_cut(e)):
                    raise
                return head(netloc, path, retried=True)  # once more, on a fresh connection

        try:
            for url in urls:
                parts = urllib.parse.urlsplit(url)
                if head(parts.netloc, parts.path + (f"?{parts.query}" if parts.query else "")) != 200:
                    return False
            return True
        except (OSError, http.client.HTTPException) as e:
            raise TwitchError(f"Network error reaching Twitch's video servers: {e!r}") from e
        finally:
            for conn in conns.values():
                conn.close()

    def _watch_event(self, channel):
        return [{
            "event": "minute-watched",
            "properties": {
                "broadcast_id": str(channel["broadcast_id"]),
                "channel_id": str(channel["id"]),
                "channel": channel["login"],
                "client_time": utcnow().isoformat(timespec="seconds").replace("+00:00", "Z"),
                "game": channel.get("game_name", ""),
                "game_id": str(channel.get("game_id") or ""),
                "hidden": False,
                "is_live": True,
                "live": True,
                "logged_in": True,
                "minutes_logged": 1,
                "muted": False,
                "user_id": str(self.user_id),
            },
        }]
