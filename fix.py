"""
fix.py — Standalone Stalker → M3U converter.
Always resolves URLs through create_link to get the real stream URL with play_token.
"""
import json, hashlib, re, time, urllib.request, urllib.parse, sys

def mac_to_serial(mac):
    return hashlib.md5(mac.replace(":", "").upper().encode()).hexdigest()[:13].upper()

def mac_to_device_id(mac):
    return hashlib.sha256(mac.replace(":", "").upper().encode()).hexdigest()[:64].upper()

def mac_to_signature(mac):
    return hashlib.sha256((mac.replace(":", "").upper() + "stalker").encode()).hexdigest()[:64].upper()

def build_headers(mac, token=""):
    h = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C) AppleWebKit/533.3 (KHTML, like Gecko) MAG200 stbapp ver: 2 rev: 250 Safari/533.3",
        "X-User-Agent": "Model: MAG200; Link: Ethernet",
        "Cookie": f"mac={mac}; stb_lang=en; timezone=Europe/London",
        "Accept": "*/*",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h

def http_get(url, headers, timeout=20):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))

def handshake(base, mac):
    data = http_get(f"{base.rstrip('/')}/portal.php?action=handshake&type=stb&prehash=0", build_headers(mac))
    token = data.get("js", {}).get("token") or data.get("token")
    if not token:
        raise RuntimeError("No token in handshake response")
    return token

def create_link(base, mac, token, cmd):
    try:
        url = f"{base.rstrip('/')}/portal.php?type=itv&action=create_link&cmd={urllib.parse.quote(cmd, safe='')}&series=&forced_storage=undefined&disable_ad=0&download=0&JsHttpRequest={int(time.time() * 1000)}-xml"
        data = http_get(url, build_headers(mac, token))
        raw = data.get("js", {}).get("cmd", "")
        if not raw:
            return ""
        m = re.match(r'^(?:ffmpeg|auto)\s+(https?://\S+|rtsp://\S+)', raw.strip())
        if m:
            link = m.group(1)
        else:
            link = raw.strip()
        if re.search(r'stream=(?:&|$)', link):
            m2 = re.search(r'(\d+)', cmd)
            if m2:
                link = re.sub(r'stream=(?:&|$)', f'stream={m2.group(1)}&', link)
                link = link.rstrip('&')
        return link
    except Exception:
        return ""

def fetch_page(base, mac, token, media_type, page):
    t = {"live": "itv", "vod": "vod", "series": "series"}.get(media_type, "itv")
    url = f"{base.rstrip('/')}/portal.php?action=get_ordered_list&type={t}&genre=*&force_ch_link_check=0&fav=0&sortby=number&hd=0&p={page}&JsHttpRequest={int(time.time() * 1000)}-xml"
    js = http_get(url, build_headers(mac, token)).get("js", {})
    if isinstance(js, list):
        return js, len(js)
    return js.get("data") or [], int(js.get("total_items") or js.get("total") or 0)

def fetch_genres(base, mac, token, media_type):
    action = "get_genres" if media_type == "live" else "get_categories"
    t = {"live": "itv", "vod": "vod", "series": "series"}.get(media_type, "itv")
    try:
        data = http_get(f"{base.rstrip('/')}/portal.php?action={action}&type={t}", build_headers(mac, token))
        js = data.get("js") or []
        if isinstance(js, dict):
            js = list(js.values())
        return {str(g.get("id", "")): (g.get("title") or g.get("name", "")).strip() for g in js if g.get("id")}
    except:
        return {}

def fix_localhost(url, base):
    if not url or not base:
        return url
    parsed = urllib.parse.urlparse(base)
    host = parsed.netloc.split(':')[0]
    if "localhost" in url.lower():
        url = url.replace("localhost", host)
    elif "127.0.0.1" in url:
        url = url.replace("127.0.0.1", host)
    elif "0.0.0.0" in url:
        url = url.replace("0.0.0.0", host)
    if url.startswith("/"):
        url = f"{parsed.scheme}://{parsed.netloc}{url}"
    return url

def main(portal, mac, types, max_pages=5):
    base = portal.rstrip("/")
    print(f"Handshaking...", file=sys.stderr)
    token = handshake(base, mac)
    print(f"Token: {token[:16]}...", file=sys.stderr)

    print("#EXTM3U")
    for media_type in types:
        print(f"\n# Fetching {media_type}...", file=sys.stderr)
        genres = fetch_genres(base, mac, token, media_type)
        seen = set()
        for page in range(1, max_pages + 1):
            items, total = fetch_page(base, mac, token, media_type, page)
            if not items:
                break
            for ch in items:
                cid = str(ch.get("id", "") or ch.get("cmd", ""))
                if cid in seen:
                    continue
                seen.add(cid)
                raw_cmd = ch.get("cmd") or ""
                # Resolve through create_link (standard Stalker API)
                stream = create_link(base, mac, token, raw_cmd)
                if not stream:
                    # Fallback: try extracting URL directly from cmd
                    m = re.match(r'^(?:ffmpeg|auto)\s+(https?://\S+)', raw_cmd.strip())
                    if m:
                        stream = m.group(1)
                    elif raw_cmd.strip().startswith(("http://", "https://", "rtsp://")):
                        stream = raw_cmd.strip()
                if stream:
                    stream = fix_localhost(stream, base)
                if stream and token and "token=" not in stream.lower():
                    sep = "&" if "?" in stream else "?"
                    stream = f"{stream}{sep}token={token}"

                name = (ch.get("name") or ch.get("title") or "Unknown").strip()
                logo = ch.get("logo") or ch.get("screenshot_uri") or ""
                genre_id = str(ch.get("tv_genre_id") or ch.get("category_id") or "")
                group = genres.get(genre_id, "Uncategorized")
                number = ch.get("number") or ch.get("ch_number") or ""

                attrs = f'tvg-name="{name}"'
                if logo:
                    attrs += f' tvg-logo="{logo}"'
                attrs += f' group-title="{group}"'
                if number:
                    attrs += f' tvg-chno="{number}"'

                url = stream or raw_cmd or ""
                if url:
                    print(f'#EXTINF:-1 {attrs},{name}')
                    print(url)

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python fix.py <portal_url> <mac_address> [types] [max_pages]", file=sys.stderr)
        print("Example: python fix.py http://ghaouti1.com:80 00:1A:79:B5:35:53 live 5", file=sys.stderr)
        sys.exit(1)
    portal = sys.argv[1]
    mac = sys.argv[2]
    types = sys.argv[3].split(",") if len(sys.argv) > 3 else ["live"]
    max_pages = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    main(portal, mac, types, max_pages)
