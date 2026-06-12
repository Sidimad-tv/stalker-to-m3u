"""
api/convert.py  —  Vercel serverless function.

POST /api/convert
  Body (JSON):
    portal      : str   required  http://HOST:PORT
    mac         : str   required  00:1A:79:XX:XX:XX
    types       : list  optional  ["live","vod","series"]  default ["live"]
    maxPages    : int   optional  default 50
    epgUrl      : str   optional
    format      : str   optional  "m3u" | "json"           default "m3u"
    skipKnown   : str   optional  existing M3U text; channels whose URLs already
                                  appear in it will be omitted (diff / recheck mode)

Response modes
  format=m3u  (default) — buffered M3U file download, backward-compatible.
  format=json           — NDJSON stream; one JSON object per line.

NDJSON event types (format=json)
  {"event":"meta",     "portal":…, "types":…, "maxPages":…, "epgUrl":…, "knownUrls":N}
  {"event":"profile",  "profile":{…}}
  {"event":"channel",  "count":N, "channel":{name,logo,group,number,stream_url,
                                             epg_id,raw_cmd,uncheckable,stream_type,media_type}}
  {"event":"progress", "scope":"live"|"vod"|"series", "page":N,
                        "count":N, "typeCount":N, "estimatedTotal":N,
                        "done":true|false}
  {"event":"error",    "scope":…, "message":…, "page":N}
  {"event":"done",     "total":N, "errors":[…], "profile":{…}, "epgUrl":"…"}
"""

from http.server import BaseHTTPRequestHandler
import json, hashlib, re, time, threading
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode


# ── Stream-type classification ─────────────────────────────────────────────────
STREAM_TYPE_MAP = {
    '.m3u8': 'video', '.m3u': 'video', '.ts': 'video', '.mp4': 'video',
    '.avi': 'video', '.mkv': 'video', '.flv': 'video',
    '.mp3': 'audio', '.aac': 'audio', '.pls': 'audio', '.ogg': 'audio',
    '/stream': 'audio', '/radio/': 'audio',
}

def classify_stream_type(url: str) -> str:
    low = url.lower().split('?')[0]
    for pat, typ in STREAM_TYPE_MAP.items():
        if pat in low:
            return typ
    return 'video'


def sanitize_url(url: str) -> str:
    """Strip ad-injected query params from .m3u8 URLs."""
    try:
        idx = url.find('.m3u8?')
        if idx != -1:
            qs = url[idx + 6:].lower()
            if any(kw in qs for kw in ['ads.', 'ad=', 'adv=', 'vast=', 'ima=']):
                return url[:idx + 6].rstrip('?')
    except Exception:
        pass
    return url


def fix_localhost_url(url: str, base: str) -> str:
    """Convert localhost/loopback URLs to absolute URLs using the base portal host."""
    if not url or not base:
        return url
    parsed_base = urllib.parse.urlparse(base)
    host = parsed_base.netloc.split(':')[0]
    if "localhost" in url.lower():
        url = url.replace("localhost", host)
    elif "127.0.0.1" in url:
        url = url.replace("127.0.0.1", host)
    elif "0.0.0.0" in url:
        url = url.replace("0.0.0.0", host)
    if url.startswith("/"):
        url = f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
    return url


def is_uncheckable(url: str) -> bool:
    if len(url) > 250:
        return True
    low = url.lower()
    # play_token means the stream has self-contained auth, NOT external required
    if 'play_token' in low:
        return False
    UNCHECKABLE_KEYWORDS = ['auth', 'login', 'key', 'signature', 'drm']
    return any(kw in low for kw in UNCHECKABLE_KEYWORDS)


# ── M3U helpers ────────────────────────────────────────────────────────────────

def extract_known_urls(m3u_text: str) -> set:
    if not m3u_text:
        return set()
    return {line.strip() for line in m3u_text.splitlines()
            if line.strip() and not line.strip().startswith('#')}


# ── Stalker portal helpers ─────────────────────────────────────────────────────

def mac_to_serial(mac):
    return hashlib.md5(mac.replace(":", "").upper().encode()).hexdigest()[:13].upper()

def mac_to_device_id(mac):
    return hashlib.sha256(mac.replace(":", "").upper().encode()).hexdigest()[:64].upper()

def mac_to_signature(mac):
    return hashlib.sha256((mac.replace(":", "").upper() + "stalker").encode()).hexdigest()[:64].upper()

def build_headers(mac, token=""):
    h = {
        "User-Agent": ("Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 "
                       "(KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3"),
        "X-User-Agent": "Model: MAG200; Link: Ethernet",
        "Cookie": f"mac={mac}; stb_lang=en; timezone=Europe/London",
        "Accept": "*/*",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def portal_url(base, action, **params):
    params["action"] = action
    return f"{base.rstrip('/')}/portal.php?{urlencode(params)}"

def http_get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def handshake(base, mac):
    data = http_get(portal_url(base, "handshake", type="stb", prehash=0), build_headers(mac))
    token = data.get("js", {}).get("token") or data.get("token")
    if not token:
        raise RuntimeError("No token in handshake response")
    return token

def get_profile(base, mac, token):
    url = portal_url(base, "get_profile",
        hd=1, ver="ImageDescription: 0.2.18-r14-pub-250;",
        num_banks=2, sn=mac_to_serial(mac), stb_type="MAG200",
        image_version=218, video_out="hdmi",
        device_id=mac_to_device_id(mac), device_id2=mac_to_device_id(mac),
        signature=mac_to_signature(mac), auth_second_step=1,
        hw_version="1.7-BD-00", not_valid_token=0,
        client_type="STB", hw_version_2=mac_to_serial(mac))
    return http_get(url, build_headers(mac, token)).get("js", {})

def fetch_genres(base, mac, token, media_type):
    action = "get_genres" if media_type == "live" else "get_categories"
    t = {"live": "itv", "vod": "vod", "series": "series"}.get(media_type, "itv")
    try:
        data = http_get(portal_url(base, action, type=t), build_headers(mac, token))
        js = data.get("js") or []
        if isinstance(js, dict):
            js = list(js.values())
        return {str(g.get("id", "")): (g.get("title") or g.get("name", "")).strip()
                for g in js if g.get("id")}
    except Exception:
        return {}

def clean_cmd(cmd, base=""):
    """Strip ffmpeg/auto prefix; return a plain URL or empty string."""
    if not cmd:
        return ""
    cmd = cmd.strip()
    if re.match(r'^https?://', cmd) or cmd.startswith("rtsp://"):
        url = sanitize_url(cmd)
        return fix_localhost_url(url, base) if base else url
    m = re.match(r'^(?:ffmpeg|auto)\s+(https?://\S+|rtsp://\S+)', cmd)
    if m:
        url = sanitize_url(m.group(1))
        return fix_localhost_url(url, base) if base else url
    # Check if cmd contains localhost/127.0.0.1 URL
    if re.search(r'(localhost|127\.0\.0\.1|0\.0\.0\.0)', cmd.lower()):
        m = re.search(r'([a-zA-Z]+://[^\s]+)', cmd)
        if m:
            url = sanitize_url(m.group(1))
            return fix_localhost_url(url, base) if base else url
    if base:
        return fix_localhost_url(cmd, base)
    return cmd

def fetch_page(base, mac, token, media_type, page):
    t = {"live": "itv", "vod": "vod", "series": "series"}.get(media_type, "itv")
    url = portal_url(base, "get_ordered_list",
        type=t, genre="*",
        force_ch_link_check=0, fav=0, sortby="number", hd=0,
        p=page, JsHttpRequest=f"{int(time.time() * 1000)}-xml")
    js = http_get(url, build_headers(mac, token)).get("js", {})
    if isinstance(js, list):
        return js, len(js)
    data  = js.get("data") or []
    total = int(js.get("total_items") or js.get("total") or 0)
    return data, total

def create_link(base, mac, token, cmd, media_type="live"):
    try:
        # Normalize: extract stream ID from channel reference URLs
        # e.g. "http://server/ch/2052844_" or "2052844_" -> "2052844"
        simple = re.match(r'^(\d+)_?$', cmd.strip())
        if simple:
            cmd = simple.group(1)
        else:
            m = re.search(r'/ch/(\d+)_?', cmd)
            if m:
                cmd = m.group(1)
        t = {"live": "itv", "vod": "vod", "series": "series"}.get(media_type, "itv")
        url = portal_url(base, "create_link", type=t,
                         cmd=urllib.parse.quote(cmd, safe=""),
                         JsHttpRequest=f"{int(time.time() * 1000)}-xml")
        raw = http_get(url, build_headers(mac, token)).get("js", {}).get("cmd", "")
        link = clean_cmd(raw, base)
        if link:
            if re.search(r'stream=(?:&|$)', link):
                m = re.search(r'(\d+)', cmd)
                if m:
                    link = re.sub(r'stream=(?:&|$)', f'stream={m.group(1)}&', link)
                    link = link.rstrip('&')
            if token and "token=" not in link.lower():
                separator = "&" if "?" in link else "?"
                link = f"{link}{separator}token={token}"
        return link
    except Exception:
        return ""

def is_channel_ref(cmd):
    """True if cmd is a channel reference needing create_link API resolution.
    False if it looks like a direct playable URL (skip the HTTP request).
    """
    if not cmd:
        return False
    cmd = cmd.strip()
    if re.match(r'^\d+_?$', cmd):
        return True
    if re.search(r'(localhost|127\.0\.0\.1|0\.0\.0\.0)', cmd.lower()):
        return True
    if re.search(r'/ch/\d+_?', cmd):
        return True
    if re.search(r'\.(ts|m3u8?|mp4|flv|mkv|avi|mpeg|mp3|aac)(\?|$)', cmd.lower()):
        return False
    if re.search(r'/(?:play|stream|live|hls)/', cmd.lower()):
        return False
    if not re.match(r'^[a-zA-Z]+://', cmd):
        return True
    return True

def resolve_stream_url(raw_cmd, media_type, base, mac, token):
    stream = ""
    if is_channel_ref(raw_cmd):
        stream = create_link(base, mac, token, raw_cmd, media_type)
    if not stream:
        stream = clean_cmd(raw_cmd, base)
    if stream and base:
        stream = fix_localhost_url(stream, base)
    if stream and token and "token=" not in stream.lower():
        sep = "&" if "?" in stream else "?"
        stream = f"{stream}{sep}token={token}"
    return stream

def build_channel(ch, genres, media_type, base, mac, token, known_urls, fallback_number, stream_url=None):
    """Normalize one raw portal channel dict into our schema.
    If stream_url is provided, use it directly (skip URL resolution).
    Returns None if the channel should be skipped (already in known_urls).
    """
    genre_id = str(ch.get("tv_genre_id") or ch.get("category_id") or "")
    raw_cmd  = ch.get("cmd") or ""

    stream = stream_url
    if stream is None:
        stream = resolve_stream_url(raw_cmd, media_type, base, mac, token)

    if stream and stream in known_urls:
        return None

    return {
        "name":        (ch.get("name") or ch.get("title") or "Unknown").strip(),
        "logo":        ch.get("logo") or ch.get("screenshot_uri") or "",
        "group":       genres.get(genre_id, "Uncategorized"),
        "number":      ch.get("number") or ch.get("ch_number") or fallback_number,
        "stream_url":  stream,
        "epg_id":      ch.get("xmltv_id") or ch.get("tvg_id") or "",
        "raw_cmd":     raw_cmd,
        "uncheckable": is_uncheckable(stream) if stream else False,
        "stream_type": classify_stream_type(stream or raw_cmd or ""),
        "media_type":  media_type,
    }

def fetch_all(base, mac, token, media_type, max_pages=50, known_urls=None):
    """Buffered fetch — used by format=m3u path."""
    genres = fetch_genres(base, mac, token, media_type)
    raw_channels, seen, total = [], set(), None

    for page in range(1, max_pages + 1):
        try:
            items, total_items = fetch_page(base, mac, token, media_type, page)
        except Exception:
            break
        if total is None and total_items:
            total = total_items
        if not items:
            break
        for ch in items:
            cid = str(ch.get("id", "") or ch.get("cmd", ""))
            if cid in seen:
                continue
            seen.add(cid)
            raw_channels.append(ch)
        if total and len(raw_channels) >= total:
            break

    with ThreadPoolExecutor(max_workers=40) as pool:
        resolved = list(pool.map(lambda ch: resolve_stream_url(
            ch.get("cmd", ""), media_type, base, mac, token), raw_channels))

    known_urls = known_urls or set()
    channels = []
    for i, ch in enumerate(raw_channels):
        built = build_channel(ch, genres, media_type, base, mac, token,
                              known_urls, len(channels) + 1, stream_url=resolved[i])
        if built and built.get("stream_url"):
            if built["stream_url"] not in known_urls:
                known_urls.add(built["stream_url"])
                channels.append(built)
        elif built:
            channels.append(built)

    return channels

def build_m3u(channels, epg_url="", base=""):
    lines = [f'#EXTM3U url-tvg="{epg_url}"' if epg_url else "#EXTM3U"]
    for ch in channels:
        url = ch.get("stream_url") or ch.get("raw_cmd") or ""
        if not url:
            continue
        
        # Fix localhost URLs in the final output
        if base and "localhost" in url.lower():
            parsed_base = urllib.parse.urlparse(base)
            host = parsed_base.netloc.split(':')[0]
            url = url.replace("localhost", host)
        
        name  = ch["name"].replace('"', '&quot;').replace(',', '&#44;')
        logo  = ch.get("logo", "")
        group = ch.get("group", "Uncategorized").replace('"', '&quot;')
        if ch.get("uncheckable"):
            group = "⚠ Auth Required"
        epg_id = ch.get("epg_id", "")
        number = ch.get("number", "")
        
        # Build attributes - only include non-empty ones for better compatibility
        attrs = []
        if epg_id:
            attrs.append(f'tvg-id="{epg_id}"')
        attrs.append(f'tvg-name="{name}"')
        if logo:
            attrs.append(f'tvg-logo="{logo}"')
        attrs.append(f'group-title="{group}"')
        if number:
            attrs.append(f'tvg-chno="{number}"')
        
        lines.append(f'#EXTINF:-1 {",".join(attrs)},{name}')
        lines.append(url)
    return "\n".join(lines) + "\n"


# ── Vercel handler ─────────────────────────────────────────────────────────────

class handler(BaseHTTPRequestHandler):

    def log_message(self, *a): pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def send_m3u(self, content, base=""):
        # Final safety net: replace all localhost URLs with actual host
        if base and "localhost" in content.lower():
            parsed_base = urllib.parse.urlparse(base)
            host = parsed_base.netloc.split(':')[0]
            content = content.replace("localhost", host)
        
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-mpegurl; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="playlist.m3u"')
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    # ── NDJSON streaming helpers ───────────────────────────────────────────────

    def start_ndjson(self):
        """Send streaming response headers — no Content-Length."""
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")  # disable nginx proxy buffering
        self._cors()
        self.end_headers()

    def emit(self, payload: dict):
        """Write one NDJSON line and flush immediately."""
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.wfile.write(line)
        self.wfile.flush()

    # ── HTTP verbs ─────────────────────────────────────────────────────────────

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        try:
            length  = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except Exception:
            return self.send_json(400, {"error": "Invalid JSON body"})

        portal     = (payload.get("portal")    or "").strip().rstrip("/")
        mac        = (payload.get("mac")       or "").strip()
        types      = payload.get("types")      or ["live"]
        max_pgs    = int(payload.get("maxPages") or 50)
        epg_url    = (payload.get("epgUrl")    or "").strip()
        fmt        = (payload.get("format")    or "m3u").lower()
        skip_known = (payload.get("skipKnown") or "").strip()

        if not portal or not mac:
            return self.send_json(400, {"error": "Missing required fields: portal, mac"})
        if not re.match(r'^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$', mac):
            return self.send_json(400, {"error": f"Invalid MAC: {mac}"})

        known_urls = extract_known_urls(skip_known)

        try:
            token = handshake(portal, mac)
        except Exception as e:
            return self.send_json(502, {"error": f"Handshake failed: {e}"})

        profile = {}
        try:
            profile = get_profile(portal, mac, token)
        except Exception:
            pass

        # ── format=m3u  (buffered, backward-compatible) ────────────────────────
        if fmt != "json":
            all_channels, errors = [], []
            for t in types:
                try:
                    all_channels.extend(fetch_all(portal, mac, token, t, max_pgs, known_urls))
                except Exception as e:
                    errors.append(f"{t}: {e}")
            if not all_channels and errors:
                return self.send_json(502, {"error": "No channels fetched", "details": errors})
            return self.send_m3u(build_m3u(all_channels, epg_url, portal), portal)

        # ── format=json  (NDJSON streaming) ───────────────────────────────────
        self.start_ndjson()
        self.emit({
            "event":     "meta",
            "portal":    portal,
            "types":     types,
            "maxPages":  max_pgs,
            "epgUrl":    epg_url,
            "knownUrls": len(known_urls),
        })
        self.emit({"event": "profile", "profile": profile})

        sent   = 0
        errors = []
        # Seed estimate: types × maxPages × ~20 channels/page
        estimated_total = max(len(types) * max_pgs * 20, 20)

        for media_type in types:
            genres    = fetch_genres(portal, mac, token, media_type)
            seen      = set()
            type_sent = 0

            for page in range(1, max_pgs + 1):
                try:
                    items, total_items = fetch_page(portal, mac, token, media_type, page)
                except Exception as e:
                    err_msg = str(e)
                    errors.append(f"{media_type} p{page}: {err_msg}")
                    self.emit({"event": "error", "scope": media_type,
                               "message": err_msg, "page": page})
                    break

                # Refine estimate from real portal total
                if total_items:
                    estimated_total = max(estimated_total, sent + total_items)

                if not items:
                    break

                for ch in items:
                    cid = str(ch.get("id", "") or ch.get("cmd", ""))
                    if cid in seen:
                        continue
                    seen.add(cid)
                    built = build_channel(ch, genres, media_type,
                                          portal, mac, token, known_urls, sent + 1)
                    if not built:
                        continue
                    sent      += 1
                    type_sent += 1
                    self.emit({"event": "channel", "count": sent, "channel": built})

                self.emit({
                    "event":          "progress",
                    "scope":          media_type,
                    "page":           page,
                    "count":          sent,
                    "typeCount":      type_sent,
                    "estimatedTotal": estimated_total,
                    "done":           False,
                })

            # Signal end-of-type
            self.emit({
                "event":          "progress",
                "scope":          media_type,
                "count":          sent,
                "typeCount":      type_sent,
                "estimatedTotal": estimated_total,
                "done":           True,
            })

        self.emit({
            "event":   "done",
            "total":   sent,
            "errors":  errors,
            "profile": profile,
            "epgUrl":  epg_url,
        })
