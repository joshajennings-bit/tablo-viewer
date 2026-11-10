"""
Minimal client for the Tablo 4th Gen (cloud "Lighthouse") API.

This API is undocumented. The endpoints, headers and request-signing scheme
below come from two open-source community projects:
  * tablo-api   (Python, MIT)  https://github.com/trevor-viljoen/tablo-api
  * tablo2plex  (Node.js)      https://github.com/hearhellacopters/tablo2plex
Tablo/Nuvyyo can change any of this at any time, which would break the app.

Only depends on `requests`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

import requests

log = logging.getLogger("tablo")

CLOUD_HOST = os.environ.get("TABLO_CLOUD_HOST", "https://lighthousetv.ewscloud.com")

_CLOUD_UA = "Tablo-FAST/2.0.0 (Mobile; iPhone; iOS 16.6)"
_LOCAL_UA = "Tablo-FAST/1.7.0 (Mobile; iPhone; iOS 18.4)"

# HMAC-MD5 keys used by the official Tablo apps to sign requests sent
# straight to the box on your home network (published by the projects above).
_HASH_KEY = "6l8jU5N43cEilqItmT3U2M2PFM3qPziilXqau9ys"
_DEVICE_KEY = "ljpg6ZkwShVv8aI12E2LP55Ep8vq1uYDPvX0DdTB"


class TabloError(Exception):
    """Something went wrong talking to Tablo."""


class TabloAuthError(TabloError):
    """Login failed or the session expired."""


@dataclass
class Device:
    sid: str
    name: str
    url: str  # e.g. http://192.168.1.50:8887


@dataclass(order=True)
class Channel:
    sort_key: tuple = field(repr=False)
    identifier: str = field(compare=False)
    call_sign: str = field(compare=False)
    major: int = field(compare=False, default=0)
    minor: int = field(compare=False, default=0)
    network: str = field(compare=False, default="")
    kind: str = field(compare=False, default="ota")  # "ota" (antenna) or "ott" (free streaming)

    @property
    def number(self) -> str:
        return f"{self.major}.{self.minor}" if self.major else ""


@dataclass
class Airing:
    title: str
    start: datetime  # timezone-aware
    duration: int  # seconds
    episode: str = ""
    description: str = ""

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)


def make_device_auth(method: str, path: str, body: str = "") -> tuple[str, str]:
    """Return (Authorization, Date) headers for a request to the Tablo box."""
    date_hdr = format_datetime(datetime.now(timezone.utc), usegmt=True)
    body_md5 = hashlib.md5(body.encode()).hexdigest() if body else ""
    payload = f"{method}\n{path}\n{body_md5}\n{date_hdr}"
    sig = hmac.new(_HASH_KEY.encode(), payload.encode(), hashlib.md5).hexdigest()
    return f"tablo:{_DEVICE_KEY}:{sig}", date_hdr


def _parse_time(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    v = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class TabloSession:
    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers["User-Agent"] = _CLOUD_UA
        self.auth_header: str | None = None
        self.lighthouse: str | None = None
        self.device: Device | None = None
        self.client_id = str(uuid.uuid4())
        self._profile_id: str | None = None
        self._devices: list[Device] = []

    # ---------------- cloud: sign-in ----------------

    def login(self, email: str, password: str) -> list[Device]:
        """Sign in to the Tablo cloud and return the Tablo boxes on the account."""
        try:
            r = self.http.post(f"{CLOUD_HOST}/api/v2/login/",
                               json={"email": email, "password": password},
                               timeout=self.timeout)
        except requests.RequestException as e:
            raise TabloError(f"Couldn't reach the Tablo service: {e}") from e
        if r.status_code in (400, 401, 403):
            raise TabloAuthError("Tablo rejected that email or password.")
        if not r.ok:
            raise TabloError(f"Sign-in failed (HTTP {r.status_code}).")
        data = r.json()
        self.auth_header = f"{data.get('token_type', 'Bearer')} {data['access_token']}"

        r = self.http.get(f"{CLOUD_HOST}/api/v2/account/",
                          headers={"Authorization": self.auth_header},
                          timeout=self.timeout)
        self._check(r, "reading your Tablo account")
        acct = r.json()
        profiles = acct.get("profiles") or []
        if not profiles:
            raise TabloError("No profiles found on this Tablo account.")
        self._profile_id = profiles[0]["identifier"]
        self._devices = [Device(sid=d["serverId"], name=d.get("name") or d["serverId"], url=d["url"])
                         for d in (acct.get("devices") or [])]
        if not self._devices:
            raise TabloError("No Tablo boxes are linked to this account. "
                             "Set the box up with the Tablo phone or TV app first.")
        return list(self._devices)

    def select_device(self, device: Device) -> None:
        r = self.http.post(f"{CLOUD_HOST}/api/v2/account/select/",
                           json={"pid": self._profile_id, "sid": device.sid},
                           headers={"Authorization": self.auth_header},
                           timeout=self.timeout)
        self._check(r, "selecting your Tablo")
        self.lighthouse = r.json()["token"]
        self.device = device
        log.info("Selected Tablo %s at %s", device.sid, device.url)

    # ---------------- cloud: guide ----------------

    def _cloud_headers(self) -> dict:
        return {"Authorization": self.auth_header, "Lighthouse": self.lighthouse,
                "Accept": "*/*", "Content-Type": "application/json"}

    def channels(self, include_streaming: bool = False) -> list[Channel]:
        r = self.http.get(f"{CLOUD_HOST}/api/v2/account/{self.lighthouse}/guide/channels/",
                          headers=self._cloud_headers(), timeout=self.timeout)
        self._check(r, "loading channels")
        out: list[Channel] = []
        for raw in r.json():
            kind = raw.get("kind") or "ota"
            if kind == "ott" and not include_streaming:
                continue
            info = raw.get("ota") or raw.get("ott") or {}
            call = info.get("callSign") or raw.get("name") or raw["identifier"]
            major = int(info.get("major") or 0)
            minor = int(info.get("minor") or 0)
            key = (0, major, minor, "") if major else (1, 0, 0, call.lower())
            out.append(Channel(sort_key=key, identifier=raw["identifier"], call_sign=call,
                               major=major, minor=minor, network=info.get("network") or "", kind=kind))
        return sorted(out)

    def airings(self, channel_id: str, day: date) -> list[Airing]:
        r = self.http.get(
            f"{CLOUD_HOST}/api/v2/account/guide/channels/{channel_id}/airings/{day.isoformat()}/",
            headers=self._cloud_headers(), timeout=self.timeout)
        if r.status_code == 404:
            return []
        self._check(r, "loading the guide")
        out = []
        for a in r.json() or []:
            start = _parse_time(a.get("datetime"))
            if not start:
                continue
            show = a.get("show") or {}
            title = show.get("title") or a.get("title") or "Unknown"
            ep = a.get("episode") or {}
            ep_txt = ""
            if isinstance(ep, dict):
                s, e, t = ep.get("season"), ep.get("episodeNumber") or ep.get("number"), ep.get("title")
                if isinstance(s, dict):
                    s = s.get("number")
                parts = []
                if s and e:
                    parts.append(f"S{s}E{e}")
                if t:
                    parts.append(t)
                ep_txt = " · ".join(str(p) for p in parts)
            if not ep_txt and a.get("title") and a.get("title") != title:
                ep_txt = a["title"]
            out.append(Airing(title=title, start=start, duration=int(a.get("duration") or 0),
                              episode=ep_txt, description=a.get("description") or ""))
        return sorted(out, key=lambda x: x.start)

    def now_and_next(self, channel_id: str) -> tuple[Airing | None, Airing | None]:
        now = datetime.now(timezone.utc)
        today = datetime.now().date()
        items: list[Airing] = []
        for d in (today - timedelta(days=1), today):
            try:
                items += self.airings(channel_id, d)
            except TabloError:
                pass
        items.sort(key=lambda x: x.start)
        current = nxt = None
        for a in items:
            if a.start <= now < a.end:
                current = a
            elif a.start > now:
                nxt = a
                break
        return current, nxt

    # ---------------- local box: streaming ----------------

    def ping(self) -> str:
        """Unauthenticated check that the box is reachable on the LAN."""
        if not self.device:
            raise TabloError("No Tablo selected.")
        url = self.device.url.rstrip("/") + "/ping"
        t0 = time.monotonic()
        try:
            r = requests.get(url, timeout=(5, 10))
        except requests.RequestException as e:
            log.warning("ping %s failed after %.1fs: %s", url, time.monotonic() - t0, e)
            raise TabloError(f"Can't reach your Tablo at {self.device.url}\n\n{type(e).__name__}: {e}") from e
        log.info("ping %s -> %s in %.1fs: %s", url, r.status_code, time.monotonic() - t0, r.text[:200])
        return f"HTTP {r.status_code} in {time.monotonic() - t0:.1f}s: {r.text[:200]}"

    def watch(self, channel_id: str) -> str:
        """Ask the Tablo box to tune a channel. Returns an HLS playlist URL."""
        if not self.device:
            raise TabloError("No Tablo selected.")
        path = f"/guide/channels/{channel_id}/watch"
        body = json.dumps({
            "bandwidth": None,
            "extra": {
                "limitedAdTracking": 1, "deviceOSVersion": "16.6", "lang": "en_US",
                "height": 1080, "width": 1920,
                "deviceId": "00000000-0000-0000-0000-000000000000",
                "deviceModel": "iPhone10,1", "deviceMake": "Apple", "deviceOS": "iOS",
            },
            "device_id": self.client_id,
            "platform": "ios",
        })
        auth, date_hdr = make_device_auth("POST", path, body)
        url = self.device.url.rstrip("/") + path
        log.info("watch POST %s?lh", url)
        t0 = time.monotonic()
        try:
            r = requests.post(url, data=body.encode(), params="lh",
                              headers={"Authorization": auth, "Date": date_hdr,
                                       "Content-Type": "application/x-www-form-urlencoded",
                                       "Accept": "*/*", "User-Agent": _LOCAL_UA},
                              timeout=(5, 30))
        except requests.RequestException as e:
            log.warning("watch failed after %.1fs: %r", time.monotonic() - t0, e)
            raise TabloError(
                f"Couldn't reach your Tablo at {self.device.url}.\n\n"
                f"{type(e).__name__}\n\n"
                "Make sure this PC is on the same home network as the Tablo "
                "(not a VPN or guest Wi-Fi).") from e
        log.info("watch -> HTTP %s in %.1fs: %s", r.status_code, time.monotonic() - t0, r.text[:300])
        if r.status_code == 401:
            raise TabloAuthError("The Tablo box rejected the request (401). Try signing in again.")
        if r.status_code in (409, 503):
            raise TabloError("All of your Tablo's tuners are busy (watching or recording). "
                             "Close another stream and try again.")
        if not r.ok:
            raise TabloError(f"The Tablo couldn't start that channel (HTTP {r.status_code}): {r.text[:200]}")
        url = (r.json() or {}).get("playlist_url")
        if not url:
            raise TabloError("The Tablo didn't return a stream for that channel.")
        return url

    # ---------------- helpers ----------------

    @staticmethod
    def _check(r: requests.Response, doing: str) -> None:
        if r.status_code in (401, 403):
            raise TabloAuthError(f"Your Tablo session expired while {doing}.")
        if not r.ok:
            raise TabloError(f"Tablo error while {doing} (HTTP {r.status_code}).")
