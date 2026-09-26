"""
Twitch access for Twitch Drop Miner: settings, saved accounts and the API client.

Only the Python standard library is used.
"""

import base64
import gzip
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Settings. Twitch changes the GQL hashes from time to time; if requests start
# failing with "PersistedQueryNotFound", update them in settings.json.
# --------------------------------------------------------------------------
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
    "prefer_streamer_drops": True,  # within a game, streamer-specific drops before general ones
    "mine_unselected": False,    # mine other campaigns when the picked games have nothing
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


class TwitchError(Exception):
    pass


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
        self.watch_mode = "spade"  # falls back to "gql" if the tracking host is blocked
        self._spade_retry_at = 0
        self._hls = None           # (login, media playlist url, fetched_at)

    # -- HTTP helpers ------------------------------------------------------
    def _base_headers(self):
        return {
            "Client-Id": self.s["client_id"],
            "User-Agent": self.s["user_agent"],
            "X-Device-Id": self.device_id,
            "Origin": CLIENT_URL,
            "Referer": CLIENT_URL,
        }

    def _request(self, url, data=None, headers=None, form=False, raw=False):
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
    def gql(self, op_name, variables):
        payload = {
            "operationName": op_name,
            "variables": variables,
            "extensions": {"persistedQuery": {"version": 1, "sha256Hash": self.s["hashes"][op_name]}},
        }
        headers = {"Client-Session-Id": self.session_id}
        if self.token:
            headers["Authorization"] = f"OAuth {self.token}"
        status, data = self._request(GQL_URL, payload, headers=headers)
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
        data = self.gql(
            "DropsPage_ClaimDropRewards", {"input": {"dropInstanceID": drop_instance_id}}
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
        """Report one minute watched on `channel`. Returns True on success.

        Twitch counts watch time through its tracking endpoint (spade.twitch.tv).
        If that host is unreachable (commonly blocked by VPN / ad-block DNS) the
        event is relayed through the GQL API instead, which Twitch accepts but
        does not appear to count - so the caller warns the user about it.
        """
        event = self._watch_event(channel)
        ok = False
        if self.watch_mode == "gql" and time.time() >= self._spade_retry_at:
            self.watch_mode = "spade"  # periodically re-check whether it's been unblocked
        if self.watch_mode == "spade":
            try:
                if self.spade_url is None:
                    self.spade_url = self._get_spade_url(channel["login"])
                encoded = base64.b64encode(json.dumps(event, separators=(",", ":")).encode()).decode()
                status, _ = self._request(
                    self.spade_url, {"data": encoded}, headers={"User-Agent": WEB_USER_AGENT},
                    form=True, raw=True,
                )
                ok = status == 204
            except TwitchError:
                self.watch_mode = "gql"  # tracking host blocked/unreachable
                self._spade_retry_at = time.time() + 300
        if not ok:
            ok = self._send_watch_gql(event)
        self._touch_stream(channel)
        return ok

    def _send_watch_gql(self, event):
        data = base64.b64encode(gzip.compress(json.dumps(event, separators=(",", ":")).encode())).decode()
        query = ("\n mutation SendEvents($input: SendSpadeEventsInput!) "
                 "{\n sendSpadeEvents(input: $input) {\n statusCode\n}\n}\n")
        status, resp = self._request(
            GQL_URL, {"query": query, "variables": {"input": {"data": data}}},
            headers={"Authorization": f"OAuth {self.token}", "Client-Session-Id": self.session_id},
        )
        result = ((resp.get("data") or {}).get("sendSpadeEvents") or {}) if isinstance(resp, dict) else {}
        return status == 200 and result.get("statusCode") == 204

    def _touch_stream(self, channel):
        """Best effort: fetch the live playlist and HEAD the newest video chunk."""
        try:
            cached = self._hls
            if not cached or cached[0] != channel["login"] or time.time() - cached[2] > 600:
                data = self.gql("PlaybackAccessToken", {
                    "isLive": True, "isVod": False, "login": channel["login"],
                    "platform": "web", "playerType": "site", "vodID": "",
                })
                tok = data.get("streamPlaybackAccessToken") or {}
                if not tok:
                    return
                url = (f"https://usher.ttvnw.net/api/channel/hls/{channel['login']}.m3u8?"
                       + urllib.parse.urlencode({"sig": tok["signature"], "token": tok["value"]}))
                status, master = self._request(url, raw=True)
                variants = [l for l in master.strip().split("\n") if l and not l.startswith("#")]
                if status != 200 or not variants:
                    return
                cached = self._hls = (channel["login"], variants[-1], time.time())
            status, media = self._request(cached[1], raw=True, headers={"Connection": "close"})
            chunks = [l for l in media.strip().split("\n") if l and not l.startswith("#")]
            if status != 200 or not chunks:
                self._hls = None
                return
            urllib.request.urlopen(urllib.request.Request(chunks[-1], method="HEAD"), timeout=20).close()
        except Exception:
            self._hls = None

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
