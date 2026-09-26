"""
Campaign discovery and the mining loop for Twitch Drop Miner.

CampaignStore  - finds every live drop campaign on Twitch, remembers streamer-
                 specific campaigns (and their channels) while those streamers
                 are offline, tracks progress, claims finished drops and
                 publishes snapshots to the GUI.
Miner          - picks what to watch (drop queue first, then picked games with
                 streamer drops ahead of general drops), reports watch time
                 every ~59 s, and switches back up the queue as soon as a
                 higher-priority streamer comes online.
"""

import copy
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape

from twitch_api import (
    APP_DIR, DROPS_PAGE_URL, SETTINGS_FILE, WEB_USER_AGENT, TwitchError, game_slug, load_json,
    parse_ts, save_json, slugify, utcnow,
)

CACHE_FILE = os.path.join(APP_DIR, "campaign_cache.json")

WATCH_INTERVAL = 59          # seconds between "minute-watched" reports
INVENTORY_EVERY = 3          # watch ticks between inventory syncs (claims + progress)
PREEMPT_EVERY = 2            # watch ticks between "is something higher in the queue live?" checks
STREAM_CHECK_EVERY = 5       # watch ticks between live / game checks of the watched channel
STALL_TICKS = 8              # minutes without any progress before a game is skipped
SCAN_CHANNELS = 3            # channels per category checked for campaigns
SCAN_CHANNELS_DEEP = 30      # ... for picked / queued games (finds every streamer campaign)
SCAN_WORKERS = 8
MAX_LOGIN_CHECKS = 25        # live checks per preemption pass

FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)
STATE_ORDER = ["Mining", "Queued", "Available", "Offline", "Not started", "Not progressing",
               "Account not linked", "Upcoming", "Completed", "Ended"]


def fmt_minutes(minutes):
    minutes = max(0, int(minutes))
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


# --------------------------------------------------------------------------
# Streamer-drop pages (for games that publish which streamers give which drop)
# --------------------------------------------------------------------------
def fetch_facepunch():
    """Rust streamer drops from twitch.facepunch.com -> [{"item", "logins", "minutes"}]."""
    req = urllib.request.Request("https://twitch.facepunch.com/", headers={"User-Agent": WEB_USER_AGENT})
    page = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "replace")
    parts = re.split(r'<div class="drop-box((?: [\w-]+)*)"\s*>', page)
    boxes = []
    for i in range(1, len(parts), 2):
        body = parts[i + 1]
        logins = list(dict.fromkeys(re.findall(
            r'href="https://www\.twitch\.tv/([A-Za-z0-9_]+)" class="streamer-info"', body)))
        items = re.findall(r'<span class="drop-type">\s*([^<]+?)\s*</span>', body)
        if not logins or not items:
            continue  # general drops link to the category, not to streamers
        m = re.search(r'class="drop-time">.*?<span>\s*([\d.]+)\s*(hour|minute)', body, re.S | re.I)
        minutes = int(float(m.group(1)) * (60 if m.group(2).lower() == "hour" else 1)) if m else 60
        boxes.append({"item": unescape(items[0]), "logins": logins, "minutes": minutes})
    return boxes


STREAMER_PAGES = {"rust": fetch_facepunch}  # game slug -> fetcher


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
class Drop:
    def __init__(self, data):
        self.id = data.get("id")
        self.name = data.get("name") or "?"
        self.required = int(data.get("requiredMinutesWatched") or 0)
        self.required_subs = int(data.get("requiredSubs") or 0)
        self.start = parse_ts(data.get("startAt"))
        self.end = parse_ts(data.get("endAt"))
        benefits = [e.get("benefit") or {} for e in data.get("benefitEdges") or []]
        self.benefit_ids = [b.get("id") for b in benefits if b.get("id")]
        self.rewards = ", ".join(b.get("name", "") for b in benefits if b.get("name")) or self.name
        self.current = 0
        self.claimed = False
        self.instance_id = None
        self.preconditions = True
        self.apply_progress(data.get("self") or {})

    def apply_progress(self, me):
        if not me:
            return
        self.current = int(me.get("currentMinutesWatched") or 0)
        self.claimed = bool(me.get("isClaimed"))
        self.instance_id = me.get("dropInstanceID")
        self.preconditions = me.get("hasPreconditionsMet", True) is not False

    @property
    def watchable(self):
        """Earned by watching (not by subscribing)."""
        return self.required > 0 and self.required_subs == 0

    @property
    def active(self):
        now = utcnow()
        return (self.start is None or self.start <= now) and (self.end is None or now < self.end)

    @property
    def minable(self):
        return not self.claimed and self.watchable and self.active

    @property
    def claimable(self):
        return (not self.claimed and self.instance_id and self.preconditions
                and self.current >= self.required)


class Campaign:
    """Built from a channel's AvailableDrops entry or an inventory entry (same shape)."""

    def __init__(self, data, slug=None, game_name=None):
        game = data.get("game") or {}
        self.id = data.get("id")
        self.name = (data.get("name") or "?").strip()
        self.game_id = str(game.get("id") or "")
        self.game_name = game_name or game.get("displayName") or game.get("name") or "?"
        self.slug = slug or game.get("slug") or slugify(self.game_name)
        self.details_url = data.get("detailsURL") or ""
        self.start = parse_ts(data.get("startAt"))
        self.end = parse_ts(data.get("endAt"))
        me = data.get("self") or {}
        self.linked = me.get("isAccountConnected", True) is not False
        self.drops = [Drop(d) for d in data.get("timeBasedDrops") or []]
        self.channels = []       # channels seen live (and carrying this campaign) recently
        self.known_logins = []   # every channel known to carry it (streamer drops only)
        self.general = None      # True: any stream of the game; False: specific streamers only
        self.kind_rank = 0       # how sure we are about `general` (2 = told by Twitch / game page)
        self.placeholder = False # known from a streamer-drop page, not seen on Twitch yet
        self.not_started = False # placeholder whose streamer is live without the drop on Twitch
        allow = data.get("allow")
        if isinstance(allow, dict):
            channels = allow.get("channels")
            if channels is None:
                self.set_kind(True, 2)
            else:
                self.set_kind(False, 2)
                self.add_logins([c.get("name") or c.get("login") for c in channels])
        if self.start is None and self.drops:
            starts = [d.start for d in self.drops if d.start]
            self.start = min(starts) if starts else None
        if self.end is None and self.drops:
            ends = [d.end for d in self.drops if d.end]
            self.end = max(ends) if ends else None

    def set_kind(self, general, rank):
        if general is not None and rank >= self.kind_rank:
            self.general, self.kind_rank = general, rank

    def add_logins(self, logins):
        have = {l.lower() for l in self.known_logins}
        for login in logins:
            if login and login.lower() not in have:
                self.known_logins.append(login)
                have.add(login.lower())

    def add_channel(self, ch):
        if all(c["login"].lower() != ch["login"].lower() for c in self.channels):
            self.channels.append(ch)

    def merge(self, other):
        """Fill in what another source knows (inventory knows progress, link state, allow list)."""
        self.start = self.start or other.start
        self.end = self.end or other.end
        self.linked = self.linked and other.linked
        self.game_id = self.game_id or other.game_id
        self.details_url = self.details_url or other.details_url
        self.set_kind(other.general, other.kind_rank)
        self.add_logins(other.known_logins)
        known = {d.id for d in self.drops}
        self.drops += [d for d in other.drops if d.id not in known]
        for ch in other.channels:
            self.add_channel(ch)

    @property
    def streamer_logins(self):
        """Channels to check for a streamer-specific campaign."""
        return [c["login"] for c in self.channels] + [
            l for l in self.known_logins if l.lower() not in {c["login"].lower() for c in self.channels}]

    @property
    def active(self):
        now = utcnow()
        return (self.start is None or self.start <= now) and (self.end is None or now < self.end)

    @property
    def watchable(self):
        return any(d.watchable for d in self.drops)

    @property
    def remaining(self):
        return [d for d in self.drops if d.minable]

    @property
    def earnable(self):
        return self.linked and self.active and bool(self.remaining) and not self.not_started


def placeholder_campaign(item, logins, minutes, game_id, slug, game_name):
    pid = "fp:" + slugify(item)
    camp = Campaign({
        "id": pid, "name": f"{item} (streamer drop)", "game": {"id": game_id},
        "timeBasedDrops": [{"id": pid, "name": item, "requiredMinutesWatched": minutes,
                            "benefitEdges": [{"benefit": {"name": item}}]}],
    }, slug=slug, game_name=game_name)
    camp.placeholder = True
    camp.set_kind(False, 2)
    camp.add_logins(logins)
    return camp


def apply_inventory(campaigns, inventory):
    """Merge watch progress / claim state from the inventory into campaigns."""
    progress = {}
    for camp in inventory.get("dropCampaignsInProgress") or []:
        for d in camp.get("timeBasedDrops") or []:
            progress[d.get("id")] = d.get("self") or {}
    awarded = {}
    for b in inventory.get("gameEventDrops") or []:
        awarded[b.get("id")] = parse_ts(b.get("lastAwardedAt"))
    for camp in campaigns:
        for drop in camp.drops:
            if drop.id in progress:
                drop.apply_progress(progress[drop.id])
            elif drop.benefit_ids and all(
                awarded.get(bid) and (drop.start is None or awarded[bid] >= drop.start)
                for bid in drop.benefit_ids
            ):
                drop.claimed = True  # reward already received earlier


def queue_entry(camp, drop):
    return {"drop_id": drop.id, "campaign_id": camp.id, "game": camp.slug, "game_name": camp.game_name,
            "label": f"{camp.game_name} · {drop.rewards}",
            "logins": camp.known_logins[:4] if camp.general is False else []}


# --------------------------------------------------------------------------
# Discovery: which campaigns exist and where they run. Account-independent, so
# one scan serves every account (each account keeps its own progress/claims).
# --------------------------------------------------------------------------
class Discovery:
    def __init__(self, settings, emit):
        self.settings = settings
        self.emit = emit             # app-wide log/status (not tied to an account)
        self.found = {}              # id -> Campaign, as of the latest scan
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.last_scan = 0
        self.version = 0             # bumps on every scan; stores re-adopt when it changes
        self.live = {}               # login (lower) -> True/False from the latest checks
        self.cache = load_json(CACHE_FILE, {})  # campaign id -> what we learned about it
        self._page_warned = set()

    def selected_slugs(self):
        return [game_slug(self.settings, g) for g in self.settings["priority_games"]]

    def deep_slugs(self):
        """Games scanned thoroughly: picked games and games with queued drops."""
        return set(self.selected_slugs()) | {e.get("game") for e in self.settings.get("drop_queue", [])}

    def is_live(self, login):
        return self.live.get(login.lower(), False)

    def note_live(self, login, live):
        self.live[login.lower()] = live

    def snapshot(self):
        """A private copy of the latest scan for one account's store."""
        with self.lock:
            return copy.deepcopy(self.found), self.version

    # -- Scanning ------------------------------------------------------------
    def ensure_fresh(self, max_age, client):
        """Rescan (using `client`) if the last scan is older than max_age seconds (0 = always).
        When several accounts ask at once, only one scan runs; the others reuse it."""
        with self.scan_lock:
            if max_age and time.time() - self.last_scan < max_age:
                return False
            self._scan(client)
            return True

    def _scan(self, client):
        self.emit("status", "Scanning Twitch for drop campaigns...")
        started = time.time()
        cats = client.get_top_categories(int(self.settings.get("scan_categories", 300)))
        known = {c["slug"] for c in cats}
        deep = self.deep_slugs()
        for slug in deep - known:  # always include picked games, even small ones
            cats.append({"id": None, "slug": slug, "name": slug})

        found = {}
        live = {}
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            jobs = [pool.submit(self.scan_game, client, c["slug"],
                                SCAN_CHANNELS_DEEP if c["slug"] in deep else SCAN_CHANNELS)
                    for c in cats]
            for job in jobs:
                try:
                    camps = job.result()
                except Exception:
                    continue
                for camp in camps:
                    for ch in camp.channels:
                        live[ch["login"].lower()] = True
                    if camp.id in found:
                        found[camp.id].merge(camp)
                    else:
                        found[camp.id] = camp
        self.live = live
        with self.lock:
            self._apply_cache(found)
        self._apply_streamer_pages(found, deep, client)
        self._refresh_live(found, deep, client)

        with self.lock:
            first_scan = not self.last_scan
            new = [c for cid, c in found.items() if cid not in self.found and not c.placeholder]
            self.found = found
            self.last_scan = time.time()
            self.version += 1
            self._save_cache()
        if not first_scan:
            for c in new:
                self.emit("log", f"New campaign: {c.game_name} - {c.name}")
        games = len({c.slug for c in found.values()})
        self.emit("log", f"Found {len(found)} campaign(s) across {games} game(s) "
                         f"in {time.time() - started:.0f}s")

    def scan_game(self, client, slug, channels_to_check):
        """Campaigns running on a game's live drops-enabled channels."""
        game, channels = client.get_game_streams(slug, limit=max(10, channels_to_check))
        if not game or not channels:
            return []
        game_id = str(game.get("id"))
        game_name = game.get("displayName") or game.get("name")
        camps, checked = {}, 0
        for ch in channels[:channels_to_check]:
            try:
                raw_list = client.get_channel_campaigns(ch["id"])
            except TwitchError:
                continue
            checked += 1
            for raw in raw_list:
                if str((raw.get("game") or {}).get("id")) != game_id or not raw.get("id"):
                    continue  # sitewide / other-game campaigns
                camp = camps.get(raw["id"])
                if camp is None:
                    camp = camps[raw["id"]] = Campaign(raw, slug=slug, game_name=game_name)
                camp.add_channel(ch)
        # General campaigns run on (almost) every channel; streamer drops on one or two.
        for camp in camps.values():
            seen = len(camp.channels)
            if checked >= 5 and seen >= max(3, checked // 2):
                camp.set_kind(True, 1)
            elif checked >= 5 and seen <= max(2, checked // 5):
                camp.set_kind(False, 1)
            if camp.general is False:
                camp.add_logins([c["login"] for c in camp.channels])
        return [c for c in camps.values() if c.watchable]

    # -- Remembering streamer campaigns while their channels are offline --------
    def remember(self, camp):
        with self.lock:
            self._remember(camp)

    def _remember(self, camp):
        if camp.placeholder:
            return
        raw = {"id": camp.id, "name": camp.name, "detailsURL": camp.details_url,
               "game": {"id": camp.game_id},
               "endAt": camp.end.isoformat() if camp.end else None,
               "timeBasedDrops": [{"id": d.id, "name": d.name, "requiredMinutesWatched": d.required,
                                   "requiredSubs": d.required_subs,
                                   "startAt": d.start.isoformat() if d.start else None,
                                   "endAt": d.end.isoformat() if d.end else None,
                                   "benefitEdges": [{"benefit": {"id": bid, "name": d.rewards}}
                                                    for bid in d.benefit_ids] or
                                                   [{"benefit": {"name": d.rewards}}]}
                                  for d in camp.drops]}
        entry = self.cache.get(camp.id) or {}
        logins = list(dict.fromkeys((entry.get("logins") or []) + (
            camp.known_logins if camp.general is False else [])))
        rank = entry.get("rank", 0)
        general = entry.get("general")
        if camp.general is not None and camp.kind_rank >= rank:
            general, rank = camp.general, camp.kind_rank
        self.cache[camp.id] = {"raw": raw, "slug": camp.slug, "game_name": camp.game_name,
                               "logins": logins, "general": general, "rank": rank}

    def _apply_cache(self, found):
        now = utcnow()
        for cid in list(self.cache):
            entry = self.cache[cid]
            end = parse_ts((entry.get("raw") or {}).get("endAt"))
            if end and end < now:
                del self.cache[cid]  # campaign over
                continue
            camp = found.get(cid)
            if camp is None:
                if entry.get("general") is not False:
                    continue  # only streamer campaigns are worth showing while offline
                camp = found[cid] = Campaign(entry["raw"], slug=entry.get("slug"),
                                             game_name=entry.get("game_name"))
            camp.set_kind(entry.get("general"), entry.get("rank", 0))
            if camp.general is False:
                camp.add_logins(entry.get("logins") or [])
        for camp in found.values():
            self._remember(camp)

    def _apply_streamer_pages(self, found, deep, client):
        for slug, fetch in STREAMER_PAGES.items():
            game_camps = [c for c in found.values() if c.slug == slug]
            if slug not in deep and not game_camps:
                continue
            try:
                boxes = fetch()
            except Exception as e:
                if slug not in self._page_warned:
                    self._page_warned.add(slug)
                    self.emit("log", f"Couldn't read the {slug} streamer-drop page: {e}")
                continue
            game_id = next((c.game_id for c in game_camps if c.game_id), "")
            game_name = next((c.game_name for c in game_camps), slug.title())
            if not game_id:
                try:
                    game, _ = client.get_game_streams(slug, limit=1)
                    game_id = str((game or {}).get("id") or "")
                    game_name = (game or {}).get("displayName") or game_name
                except TwitchError:
                    pass
            for box in boxes:
                logins = {l.lower() for l in box["logins"]}
                matches = [c for c in game_camps if c.general is not True
                           and logins & {l.lower() for l in c.streamer_logins}]
                if matches:
                    for camp in matches:
                        camp.set_kind(False, 2)
                        camp.add_logins(box["logins"])
                        self.remember(camp)
                    self.rewrite_queue("fp:" + slugify(box["item"]), matches[0])
                else:
                    ph = placeholder_campaign(box["item"], box["logins"], box["minutes"],
                                              game_id, slug, game_name)
                    found.setdefault(ph.id, ph)

    def _refresh_live(self, found, deep, client):
        """Live-check streamer drops' channels the scan didn't see, attach live channels,
        and turn page-only placeholders into real Twitch campaigns where possible."""
        by_login = {}
        for camp in found.values():
            for ch in camp.channels:
                by_login.setdefault(ch["login"].lower(), ch)
        todo, games = {}, {}
        for camp in found.values():
            if camp.general is False and (camp.slug in deep or camp.placeholder) and camp.remaining:
                for login in camp.known_logins:
                    if login.lower() not in self.live:
                        todo.setdefault(login.lower(), login)
                        games.setdefault(login.lower(), set()).add(camp.game_id)
        if todo:
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                infos = dict(zip(todo, pool.map(lambda l: self._safe_stream_info(client, l), todo.values())))
            for key, info in infos.items():
                # "Live" = live AND streaming the game the drop is for.
                self.live[key] = bool(info) and (info["game_id"] in games[key] or "" in games[key])
                if self.live[key]:
                    by_login[key] = info
        for camp in list(found.values()):
            if camp.general is not False or camp.id not in found:
                continue
            live = [by_login[l.lower()] for l in camp.known_logins if l.lower() in by_login
                    and (not camp.game_id or by_login[l.lower()]["game_id"] == camp.game_id)]
            if not live:
                continue
            if camp.placeholder:
                if not self.resolve_placeholder(client, camp, live[0], found):
                    camp.not_started = True  # streamer is live but Twitch has no drop for them yet
                continue
            for ch in live:
                camp.add_channel(ch)

    @staticmethod
    def _safe_stream_info(client, login):
        try:
            return client.get_stream_info(login)
        except TwitchError:
            return None

    def resolve_placeholder(self, client, ph, channel, campaigns):
        """A placeholder's streamer is live: find the real Twitch campaign on their channel.
        `campaigns` is the dict to update (the scan result, or one account's store)."""
        try:
            raw_list = client.get_channel_campaigns(channel["id"])
        except TwitchError:
            return None
        reals = []
        with self.lock:
            for raw in raw_list:
                if str((raw.get("game") or {}).get("id")) != ph.game_id or not raw.get("id"):
                    continue
                camp = campaigns.get(raw["id"]) or Campaign(raw, slug=ph.slug, game_name=ph.game_name)
                if camp.general is True or not camp.watchable:
                    continue
                reals.append(camp)
            if not reals:
                return None
            for camp in reals:
                camp.set_kind(False, 2)
                camp.add_logins(ph.known_logins)
                camp.add_channel(channel)
                campaigns[camp.id] = camp
                self._remember(camp)
            campaigns.pop(ph.id, None)
            self._save_cache()
        self.rewrite_queue(ph.drops[0].id, reals[0])
        self.emit("log", f"{ph.drops[0].rewards}: found {channel['display']}'s campaign "
                         f"'{reals[0].name}' on Twitch")
        return reals[0]

    def _save_cache(self):
        try:
            save_json(CACHE_FILE, self.cache)
        except OSError:
            pass

    def rewrite_queue(self, old_drop_id, camp):
        """Point queue entries at a placeholder to the real campaign's drop(s)."""
        queue = self.settings.get("drop_queue", [])
        if not any(e.get("drop_id") == old_drop_id for e in queue):
            return
        new = []
        for e in queue:
            if e.get("drop_id") == old_drop_id:
                new += [queue_entry(camp, d) for d in camp.drops if d.watchable]
            else:
                new.append(e)
        seen, deduped = set(), []
        for e in new:
            if e["drop_id"] not in seen:
                seen.add(e["drop_id"])
                deduped.append(e)
        self.settings["drop_queue"] = deduped
        save_json(SETTINGS_FILE, self.settings)


# --------------------------------------------------------------------------
# Campaign store: one per account (its progress, claims, target, stalled games)
# --------------------------------------------------------------------------
class CampaignStore:
    def __init__(self, client, settings, emit, discovery):
        self.client = client
        self.settings = settings
        self.emit = emit
        self.discovery = discovery
        self.campaigns = {}          # id -> Campaign (this account's copy)
        self.lock = threading.RLock()
        self.version = -1            # discovery version adopted
        self.target_id = None
        self.target_drop_id = None
        self.stalled = set()         # game slugs that got no progress (usually: account not linked)
        self.claimed_total = 0

    @property
    def last_scan(self):
        return self.discovery.last_scan if self.version >= 0 else 0

    def selected_slugs(self):
        return self.discovery.selected_slugs()

    def find_drop(self, drop_id):
        with self.lock:
            for camp in self.campaigns.values():
                for drop in camp.drops:
                    if drop.id == drop_id:
                        return camp, drop
        return None, None

    def is_live(self, login):
        return self.discovery.is_live(login)

    def note_live(self, login, live):
        self.discovery.note_live(login, live)

    def ensure_fresh(self, max_age):
        """Make sure campaigns are at most max_age seconds old (0 = force a rescan).
        The scan itself is shared; this account then syncs its own inventory."""
        scanned = self.discovery.ensure_fresh(max_age, self.client)
        if self.discovery.version != self.version:
            found, version = self.discovery.snapshot()
            with self.lock:
                self.campaigns, self.version = found, version
            self.sync_inventory()
        return scanned

    def rescan_game(self, slug):
        """Refresh one game's campaigns/channels (e.g. when its channels went offline)."""
        camps = self.discovery.scan_game(self.client, slug, SCAN_CHANNELS_DEEP)
        with self.lock:
            for camp in camps:
                for ch in camp.channels:
                    self.note_live(ch["login"], True)
                old = self.campaigns.get(camp.id)
                if old:
                    old.channels = camp.channels
                    old.set_kind(camp.general, camp.kind_rank)
                    old.add_logins(camp.known_logins)
                else:
                    self.campaigns[camp.id] = camp
        return camps

    def resolve_placeholder(self, ph, channel):
        with self.lock:
            return self.discovery.resolve_placeholder(self.client, ph, channel, self.campaigns)

    # -- Inventory -------------------------------------------------------------
    def sync_inventory(self):
        """Claim every finished drop, then merge progress into the campaigns."""
        inventory = self.client.get_inventory()
        claimed_any = False
        for raw in inventory.get("dropCampaignsInProgress") or []:
            game = (raw.get("game") or {})
            game_name = game.get("displayName") or game.get("name") or "?"
            for d in raw.get("timeBasedDrops") or []:
                drop = Drop(d)
                if not drop.claimable:
                    continue
                try:
                    result = self.client.claim(drop.instance_id)
                    claimed_any = True
                    self.claimed_total += 1
                    self.emit("log", f"CLAIMED: {drop.rewards} ({game_name}) - {result}")
                except TwitchError as e:
                    self.emit("log", f"Failed to claim {drop.rewards}: {e}")
        if claimed_any:
            inventory = self.client.get_inventory()
        with self.lock:
            for raw in inventory.get("dropCampaignsInProgress") or []:
                inv_camp = Campaign(raw)
                if inv_camp.id in self.campaigns:
                    self.campaigns[inv_camp.id].merge(inv_camp)
                    self.discovery.remember(self.campaigns[inv_camp.id])
                elif inv_camp.watchable:
                    self.campaigns[inv_camp.id] = inv_camp  # in progress but no live channel now
            apply_inventory(self.campaigns.values(), inventory)
        self.publish()

    # -- Snapshots for the GUI ---------------------------------------------------
    def has_live_channel(self, c):
        if c.general is False or c.placeholder:
            return any(self.is_live(l) for l in c.streamer_logins)
        return bool(c.channels)

    def state_of(self, c, selected, queued_camps):
        if c.id == self.target_id:
            return "Mining"
        if not c.linked:
            return "Account not linked"
        if not c.active:
            return "Upcoming" if c.start and c.start > utcnow() else "Ended"
        if not c.remaining:
            return "Completed"
        if c.not_started:
            return "Not started"
        if c.slug in self.stalled:
            return "Not progressing"
        if not self.has_live_channel(c):
            return "Offline"
        return "Queued" if c.slug in selected or c.id in queued_camps else "Available"

    def publish(self):
        selected = self.selected_slugs()
        queue = list(self.settings.get("drop_queue", []))
        queue_pos = {e["drop_id"]: i + 1 for i, e in enumerate(queue)}
        cards = []
        with self.lock:
            queued_camps = {c.id for c in self.campaigns.values()
                            if any(d.id in queue_pos for d in c.drops)}
            for c in self.campaigns.values():
                if not c.watchable:
                    continue
                state = self.state_of(c, selected, queued_camps)
                drops = []
                for d in c.drops:
                    if not d.watchable:
                        continue
                    drops.append({"id": d.id, "name": d.rewards, "cur": min(d.current, d.required),
                                  "req": d.required, "claimed": d.claimed, "queued": queue_pos.get(d.id, 0),
                                  "status": ("claimed" if d.claimed else "active" if d.active
                                             else "upcoming" if d.start and d.start > utcnow() else "expired")})
                streamers = [(l, self.is_live(l)) for l in c.streamer_logins] if c.general is False else []
                kind = "Streamer drop" if c.general is False else "General drop" if c.general else ""
                prio = selected.index(c.slug) if c.slug in selected else len(selected)
                qpos = min((queue_pos[d.id] for d in c.drops if d.id in queue_pos), default=999)
                cards.append({
                    "id": c.id, "game": c.game_name, "slug": c.slug, "name": c.name, "state": state,
                    "selected": c.slug in selected, "kind": kind, "streamers": streamers,
                    "ends": c.end.astimezone().strftime("%b %d, %H:%M") if c.end else "",
                    "drops": drops,
                    "sort": (STATE_ORDER.index(state), qpos, prio, c.general is not False,
                             c.end or FAR_FUTURE, c.game_name.lower(), c.name.lower()),
                })
            rows = []
            for e in queue:
                camp, drop = self.find_drop(e["drop_id"])
                if not drop:
                    status = "Not found"
                elif drop.claimed:
                    status = "Claimed"
                elif camp.id == self.target_id and (self.target_drop_id in (None, drop.id)):
                    status = "Mining"
                elif not drop.minable or not camp.active:
                    status = "Expired"
                elif not camp.linked:
                    status = "Not linked"
                elif camp.not_started:
                    status = "Not started"
                else:
                    status = "Live" if self.has_live_channel(camp) else "Offline"
                logins = camp.streamer_logins[:3] if camp and camp.general is False else e.get("logins", [])
                label = e.get("label", "?") + (f"  ({', '.join(logins)})" if logins else "")
                if drop and not drop.claimed and drop.required:
                    label += f"  {min(drop.current, drop.required)}/{drop.required}m"
                rows.append({"drop_id": e["drop_id"], "label": label, "status": status})
        cards.sort(key=lambda x: x["sort"])
        self.emit("campaigns", cards, self.claimed_total, self.last_scan, rows)


# --------------------------------------------------------------------------
# Miner (runs in a background thread)
# --------------------------------------------------------------------------
class Miner:
    def __init__(self, store):
        self.store = store
        self.client = store.client
        self.settings = store.settings
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.channel = None
        self.blocked = False
        self.blocked_warned = False
        self.cooldown = {}  # campaign id -> time before which it isn't retried

    def emit(self, kind, *payload):
        # After Stop, a tick that was mid-request must not repaint the UI.
        if not self.stop_event.is_set() or kind in ("log", "stopped"):
            self.store.emit(kind, *payload)

    def log(self, msg):
        self.emit("log", msg)

    def status(self, msg):
        self.emit("status", msg)

    def sleep(self, seconds):
        """Wait, returning early on stop or a wake-up (refresh / selection change)."""
        self.wake.wait(seconds)
        self.wake.clear()
        return not self.stop_event.is_set()

    # -- What to watch, in order ------------------------------------------------
    def candidates(self):
        """[(campaign, queued drop or None)] from most to least wanted."""
        store = self.store
        selected = store.selected_slugs()
        prefer = self.settings.get("prefer_streamer_drops", True)

        def usable(c):
            return c.earnable and c.slug not in store.stalled

        def kind_rank(c):
            return (0 if c.general is False else 1 if c.general is None else 2) if prefer else 0

        out, used = [], set()
        for entry in self.settings.get("drop_queue", []):          # 1. the drop queue, in order
            camp, drop = store.find_drop(entry.get("drop_id"))
            if camp and drop and drop.minable and usable(camp) and (camp.id, drop.id) not in used:
                out.append((camp, drop))
                used.add((camp.id, drop.id))
        queued_camps = {cid for cid, _ in used}
        with store.lock:
            camps = [c for c in store.campaigns.values() if usable(c) and c.id not in queued_camps]
        by_game = sorted((c for c in camps if c.slug in selected),  # 2. picked games
                         key=lambda c: (selected.index(c.slug), kind_rank(c), c.end or FAR_FUTURE))
        out += [(c, None) for c in by_game]
        if self.settings.get("mine_unselected"):                     # 3. anything else
            rest = sorted((c for c in camps if c.slug not in selected),
                          key=lambda c: (kind_rank(c), c.end or FAR_FUTURE))
            out += [(c, None) for c in rest]
        return out

    def _logins_for(self, camp):
        if camp.general is False or camp.placeholder:
            return camp.streamer_logins
        return [c["login"] for c in camp.channels]

    def _live_channel(self, camp, budget=MAX_LOGIN_CHECKS):
        current = (self.channel or {}).get("login", "").lower()
        logins = sorted(self._logins_for(camp), key=lambda l: l.lower() != current)
        for login in logins[:budget]:
            if self.stop_event.is_set():
                return None
            info = self.client.get_stream_info(login)
            ok = bool(info) and (not camp.game_id or info["game_id"] == camp.game_id)
            self.store.note_live(login, ok)
            if ok:
                return info
        return None

    def pick_target(self):
        for camp, drop in self.candidates():
            if self.stop_event.is_set():
                break
            if self.cooldown.get(camp.id, 0) > time.time():
                continue
            what = f"{camp.game_name} · {drop.rewards}" if drop else camp.game_name
            self.status(f"Looking for a live channel for {what}...")
            channel = self._live_channel(camp)
            if not channel and camp.general is not False and not camp.placeholder:
                self.store.rescan_game(camp.slug)  # channels from the last scan went offline
                camp = self.store.campaigns.get(camp.id) or camp
                channel = self._live_channel(camp)
            if not channel:
                continue
            if camp.placeholder:
                real = self.store.resolve_placeholder(camp, channel)
                if not real or not real.earnable:
                    # Streamer is live but their stream doesn't carry the drop (yet).
                    self.cooldown[camp.id] = time.time() + 600
                    continue
                camp = real
                drop = next((d for d in real.drops if d.minable), None)
            return camp, drop, channel
        return None, None, None

    def higher_priority_live(self, camp, drop):
        """Is something ranked above the current target watchable right now?"""
        budget = MAX_LOGIN_CHECKS
        for cand, cand_drop in self.candidates():
            if cand.id == camp.id and (cand_drop is None or drop is None or cand_drop.id == drop.id):
                return None  # reached the current target: nothing better is live
            if self.cooldown.get(cand.id, 0) > time.time():
                continue
            logins = self._logins_for(cand)[:max(1, min(4, budget))]
            budget -= len(logins)
            for login in logins:
                if self.stop_event.is_set():
                    return None
                info = self.client.get_stream_info(login)
                ok = bool(info) and (not cand.game_id or info["game_id"] == cand.game_id)
                self.store.note_live(login, ok)
                if ok:
                    return cand, cand_drop, info
            if budget <= 0:
                return None
        return None

    # -- Main loop -------------------------------------------------------------
    def run(self):
        try:
            self._run()
        except Exception as e:  # keep the GUI informed about anything unexpected
            self.log(f"Miner crashed: {e!r}")
        finally:
            self.store.target_id = self.store.target_drop_id = None
            self.store.publish()
            self.emit("stopped", self)

    def _run(self):
        while not self.stop_event.is_set():
            refresh_every = max(2, int(self.settings.get("refresh_minutes", 10))) * 60
            try:
                self.store.ensure_fresh(refresh_every)
                self.store.sync_inventory()
                camp, drop, channel = self.pick_target()
            except TwitchError as e:
                if self._fatal(e):
                    return
                if not self.sleep(60):
                    return
                continue
            if self.stop_event.is_set():
                return

            if not camp:
                self.channel = None
                self.store.target_id = self.store.target_drop_id = None
                self.store.publish()
                self.emit("progress", None)
                self.emit("channels", [], None)
                if not (self.settings["priority_games"] or self.settings.get("drop_queue")
                        or self.settings.get("mine_unselected")):
                    self.status("Queue drops or tick \"Mine\" on campaigns in the Campaigns tab")
                else:
                    self.status("Nothing to mine right now - waiting for a queued streamer / campaign to go live")
                if not self.sleep(min(refresh_every, 300)):
                    return
                continue

            what = f"{drop.rewards} ({camp.name})" if drop else f"'{camp.name}'"
            if not self.channel or self.channel["login"] != channel["login"]:
                self.log(f"Watching {channel['display']} ({channel['game_name']}) for {what}")
            self.channel = channel
            self.store.target_id, self.store.target_drop_id = camp.id, (drop.id if drop else None)
            self.store.publish()
            chans = list(camp.channels)
            if all(c["login"].lower() != channel["login"].lower() for c in chans):
                chans.insert(0, channel)
            self.emit("channels", chans, channel["login"])
            if not self.watch(camp, drop, refresh_every):
                return

    def _fatal(self, error):
        self.log(str(error))
        self.status("Error - see console")
        if "log in again" in str(error):
            self.emit("session_expired")
            return True
        return False

    def _progress(self, camp, ch, target_drop=None, session_drop=None):
        drops = [d for d in camp.drops if d.watchable]
        total = sum(d.required for d in drops) or 1
        done = sum(d.required if d.claimed else min(d.current, d.required) for d in drops)
        remaining = [d for d in drops if d.minable]
        if target_drop in remaining:
            drop = target_drop
        elif session_drop in remaining:
            drop = session_drop
        else:
            drop = min(remaining, key=lambda d: d.required - d.current, default=None)
        self.emit("progress", {
            "channel": ch,
            "game": camp.game_name,
            "campaign": camp.name,
            "kind": "Streamer drop" if camp.general is False else "General drop" if camp.general else "",
            "camp_pct": 100 * done / total,
            "camp_left": max((d.required - d.current for d in remaining), default=0),
            "claimed": sum(1 for d in drops if d.claimed),
            "count": len(drops),
            "drop": {"name": drop.rewards, "cur": drop.current, "req": drop.required} if drop else None,
        })

    def watch(self, camp, drop, refresh_every):
        """Watch self.channel until something needs re-evaluation. False = stop."""
        started = time.time()
        tick = 0
        fails = 0
        baseline = {d.id: (d.current, d.claimed) for d in camp.drops}
        progress_seen = False
        self._progress(camp, self.channel, drop)
        while not self.stop_event.is_set():
            ch = self.channel
            try:
                ok = self.client.send_watch(ch)
                self._report_blocking()
                if ok:
                    fails = 0
                else:
                    fails += 1
                    self.log(f"Watch report rejected ({fails}/3)")
                    self.client.spade_url = None
                    if fails >= 3:
                        return True

                session = self.client.get_current_session(ch["id"])
                _, session_drop = self.store.find_drop((session or {}).get("dropID"))
                if session and session_drop:
                    cur = int(session.get("currentMinutesWatched") or 0)
                    if cur > 0:
                        progress_seen = True
                    session_drop.current = max(session_drop.current, cur)

                tick += 1
                if tick % INVENTORY_EVERY == 0:
                    self.store.sync_inventory()
                    camp = self.store.campaigns.get(camp.id) or camp
                    if drop is not None:
                        drop = next((d for d in camp.drops if d.id == drop.id), drop)
                    if any((d.current, d.claimed) != baseline.get(d.id, (0, False)) for d in camp.drops):
                        progress_seen = True
                self._progress(camp, ch, drop, session_drop)

                if not progress_seen and tick >= STALL_TICKS and self.client.watch_mode == "spade":
                    self.store.stalled.add(camp.slug)
                    self.log(f"No progress on {camp.game_name} after {STALL_TICKS} minutes - usually the "
                             f"game account isn't linked ({DROPS_PAGE_URL}). Skipping it until the next "
                             "'Refresh campaigns'.")
                    self.store.target_id = self.store.target_drop_id = None
                    self.store.publish()
                    return True
                if drop is not None and not drop.minable:
                    self.log(f"{drop.rewards} done - moving to the next drop in the queue")
                    return True
                if not camp.remaining:
                    self.log(f"Campaign '{camp.name}' finished")
                    return True
                if tick % PREEMPT_EVERY == 0:
                    better = self.higher_priority_live(camp, drop)
                    if better:
                        b_camp, b_drop, b_ch = better
                        what = b_drop.rewards if b_drop else b_camp.name
                        self.log(f"{b_ch['display']} is live for higher-priority {what} - switching")
                        return True
                if tick % STREAM_CHECK_EVERY == 0:
                    info = self.client.get_stream_info(ch["login"])
                    if not info or (camp.game_id and info["game_id"] != camp.game_id):
                        self.store.note_live(ch["login"], False)
                        self.log(f"{ch['display']} went offline or changed game - switching channel")
                        self.channel = None
                        return True
                    ch.update(broadcast_id=info["broadcast_id"], viewers=info["viewers"])
                if time.time() - started >= refresh_every:
                    return True  # rescan: picks up new / higher-priority campaigns
            except TwitchError as e:
                if self._fatal(e):
                    return False

            next_at = datetime.fromtimestamp(time.time() + WATCH_INTERVAL).strftime("%H:%M:%S")
            self.status(f"Mining {camp.game_name} - next watch report at {next_at}")
            self.wake.wait(WATCH_INTERVAL)
            if self.wake.is_set():  # refresh / queue changed: re-pick right away
                self.wake.clear()
                return not self.stop_event.is_set()
        return False

    def _report_blocking(self):
        blocked = self.client.watch_mode != "spade"
        if blocked != self.blocked:
            self.blocked = blocked
            self.emit("blocked", blocked)
        if not blocked and self.blocked_warned:
            self.blocked_warned = False
            self.log("spade.twitch.tv is reachable again - watch time is being counted")
        elif blocked and not self.blocked_warned:
            self.blocked_warned = True
            self.log("WARNING: spade.twitch.tv (where Twitch counts watch time) can't be reached. "
                     "It's blocked by your DNS - e.g. Mullvad's Ads or Trackers content blockers, Pi-hole "
                     "or AdGuard. Drops will NOT progress until it's unblocked.")
