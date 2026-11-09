import os
import re
import json
import time
import uuid
import asyncio
from pathlib import Path
from urllib.parse import urljoin, urlparse, quote, unquote

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Дениско пиписко и Миланка милашка")

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

rooms = {}


def get_or_create_room(room_id: str):
    if room_id not in rooms:
        rooms[room_id] = {
            "id": room_id,
            "url": None,
            "media": [],
            "selected": None,
            "state": {"playing": False, "time": 0.0, "updated": time.time()},
            "clients": set(),
            "chat": [],
        }
    return rooms[room_id]


def effective_position(room):
    """Authoritative playhead: stored position + time elapsed while playing.

    The server is the single source of truth (like Rave): clients continuously
    adjust to this value instead of trusting their own local clocks.
    """
    s = room["state"]
    t = s["time"]
    if s["playing"]:
        t += time.time() - s["updated"]
    return s["playing"], t


def serialize_room(room):
    playing, t = effective_position(room)
    return {
        "room_id": room["id"],
        "url": room["url"],
        "media": room["media"],
        "selected": room["selected"],
        "state": {"playing": playing, "time": t, "updated": time.time()},
        "viewers": len(room["clients"]),
    }


UABROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

MEDIA_EXT = (".mp4", ".webm", ".m3u8", ".ogg", ".mov")

# players that usually expose a direct media link in their embed page
KINOBOX_ORDER = ["Collaps", "Turbo", "Gencit", "Veoveo", "Videoseed", "Kodik", "Alloha"]


def _find_media_links(html: str):
    """Return direct media urls found in an embed page."""
    txt = html.replace("\\/", "/")
    found = []
    pats = [
        r'https?://[^\s"\'<>\\]+\.m3u8[^\s"\'<>\\]*',
        r'https?://[^\s"\'<>\\]+\.mp4[^\s"\'<>\\]*',
        r'https?://[^\s"\'<>\\]+\.m3u8\?[^\s"\'<>\\]*',
    ]
    for pat in pats:
        for m in re.finditer(pat, txt):
            u = m.group(0)
            if u not in found:
                found.append(u)
    return found


async def resolve_kinobox(url: str, kinopoisk: str):
    """Query kinobox API, then try to extract a direct media link from the player.

    Returns (media_list, error).
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    hdrs = {
        "User-Agent": UABROWSER,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": url,
        "Origin": origin,
    }
    players = []
    try:
        async with httpx.AsyncClient(http2=True, timeout=25.0, follow_redirects=True) as client:
            r = await client.get(
                f"https://fbphdplay.top/api/players?kinopoisk={kinopoisk}", headers=hdrs
            )
            data = r.json()
            players = [p for p in (data.get("data") or []) if p and p.get("iframeUrl")]
    except Exception as e:
        return [], f"Kinobox API недоступен: {e}"

    if not players:
        return [], "Kinobox: плееры не найдены"

    def sort_key(p):
        t = p.get("type") or ""
        return KINOBOX_ORDER.index(t) if t in KINOBOX_ORDER else len(KINOBOX_ORDER)

    players.sort(key=sort_key)

    # 1) try to get a direct media link (gives full sync control)
    async with httpx.AsyncClient(http2=True, timeout=25.0, follow_redirects=True) as client:
        for p in players[:4]:
            src = p.get("iframeUrl")
            if not src:
                continue
            try:
                pr = await client.get(src, headers={
                    "User-Agent": UABROWSER,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9",
                    "Referer": url,
                })
                links = _find_media_links(pr.text)
            except Exception:
                links = []
            if links:
                return [{
                    "type": "video",
                    "src": links[0],
                    "label": f"Прямая ссылка ({p.get('type')})",
                }], None

    # 2) fallback: embed only the player window (not the whole site page)
    first = players[0]
    return [{
        "type": "iframe",
        "src": first.get("iframeUrl"),
        "label": first.get("type") or "Плеер",
    }], None


async def extract_media(url: str):
    media = []
    err = None
    parsed0 = urlparse(url)
    if any(parsed0.path.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")):
        return [{"type": "video", "src": url, "label": "direct"}], None

    host0 = parsed0.netloc.lower()
    # YouTube: build embed without fetching the page (avoids 429/bot blocks)
    if "youtube.com" in host0 or "youtu.be" in host0:
        vid = None
        if "youtu.be" in host0:
            vid = parsed0.path.strip("/").split("/")[0]
        else:
            from urllib.parse import parse_qs
            vid = parse_qs(parsed0.query).get("v", [None])[0]
        if vid:
            return [{"type": "iframe", "src": f"https://www.youtube.com/embed/{vid}", "label": "YouTube"}], None
    # Rutube
    if "rutube.ru" in host0:
        m = re.search(r"/video/([a-zA-Z0-9]+)", parsed0.path)
        if m:
            return [{"type": "iframe", "src": f"https://rutube.ru/play/embed/{m.group(1)}", "label": "Rutube"}], None

    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru,en;q=0.9",
            })
            html = r.text
            # Some SPA sites return 404/403 but still serve the app shell
            if r.status_code >= 400 and "<html" not in html.lower() and "<!doctype" not in html.lower():
                return media, f"Не удалось загрузить страницу: HTTP {r.status_code}"
    except Exception as e:
        return media, f"Не удалось загрузить страницу: {e}"

    soup = BeautifulSoup(html, "html.parser")

    # Kinobox (kinogo-like) sites: resolve a direct media link server-side
    kp_candidates = []
    kp_tag = soup.find(attrs={"data-kinopoisk": True})
    if kp_tag and kp_tag.get("data-kinopoisk"):
        v = str(kp_tag.get("data-kinopoisk")).strip()
        if v.isdigit():
            kp_candidates.append(v)
    m = re.search(r"/(?:film|movie|title)/(\d{3,10})/?", parsed0.path)
    if m:
        kp_candidates.append(m.group(1))
    m = re.search(r"(?:kinopoisk|kp)=(\d{3,10})", parsed0.query)
    if m:
        kp_candidates.append(m.group(1))

    seen_kp = set()
    for kid in kp_candidates:
        if kid in seen_kp:
            continue
        seen_kp.add(kid)
        kmedia, kerr = await resolve_kinobox(url, kid)
        if kmedia:
            return kmedia, None
        err = kerr

    # Special handling for known hosts
    parsed_host = urlparse(url)
    host = parsed_host.netloc.lower()
    if "youtube.com" in host or "youtu.be" in host:
        vid = None
        if "youtu.be" in host:
            vid = parsed_host.path.strip("/").split("/")[0]
        else:
            from urllib.parse import parse_qs
            vid = parse_qs(parsed_host.query).get("v", [None])[0]
        if vid:
            media.append({"type": "iframe", "src": f"https://www.youtube.com/embed/{vid}", "label": "YouTube"})
    if "rutube.ru" in host:
        m = re.search(r"/video/([a-zA-Z0-9]+)", parsed_host.path)
        if m:
            media.append({"type": "iframe", "src": f"https://rutube.ru/play/embed/{m.group(1)}", "label": "Rutube"})

    # <video> and <source>
    for v in soup.find_all("video"):
        if v.get("src"):
            media.append({"type": "video", "src": v["src"], "label": "video"})
        for s in v.find_all("source"):
            if s.get("src"):
                media.append({"type": "video", "src": s["src"], "label": s.get("type") or "source"})

    # iframes
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src")
        if src and not any(bad in src for bad in ("ServiceLogin", "signin", "/@", "consent")):
            media.append({"type": "iframe", "src": src, "label": iframe.get("title") or "iframe"})

    # meta og:video / twitter:player (only url-like props)
    for meta in soup.find_all("meta"):
        prop = (meta.get("property") or meta.get("name") or "").lower()
        if not any(k in prop for k in ("video", "player", "stream")):
            continue
        if any(bad in prop for bad in ("width", "height", "type", "tag", "duration", "image")):
            continue
        content = meta.get("content")
        if content and (content.startswith("http") or content.startswith("/")):
            t = "video" if any(content.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")) else "iframe"
            media.append({"type": t, "src": content, "label": prop})

    # JSON-LD
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except Exception:
            continue

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k in ("contentUrl", "embedUrl", "videoUrl") and isinstance(v, str):
                        t = "video" if any(v.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")) else "iframe"
                        media.append({"type": t, "src": v, "label": k})
                    elif k == "url" and isinstance(v, str) and ("embed" in v or any(v.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov"))):
                        t = "video" if any(v.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")) else "iframe"
                        media.append({"type": t, "src": v, "label": k})
                    else:
                        walk(v)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(data)

    # scripts with media urls
    for script in soup.find_all("script"):
        txt = script.string or ""
        for m in re.finditer(r'https?://[^\s"\'<>]+\.(?:mp4|webm|m3u8|ogg|mov)(?:\?[^\s"\'<>]*)?', txt):
            media.append({"type": "video", "src": m.group(0), "label": "script"})
        # HLS in quoted strings
        for m in re.finditer(r'"(https?://[^"]+\.m3u8[^"]*)"', txt):
            media.append({"type": "video", "src": m.group(1), "label": "hls"})

    # resolve relative urls + dedup
    seen = set()
    uniq = []
    for m in media:
        src = urljoin(url, m["src"])
        if src not in seen:
            seen.add(src)
            m["src"] = src
            uniq.append(m)

    # If nothing found but url itself is media
    parsed = urlparse(url)
    if not uniq and any(parsed.path.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")):
        uniq.append({"type": "video", "src": url, "label": "direct"})

    # Fallback: embed the original page (works for SPA sites, but no sync control)
    if not uniq:
        label = "Страница (без синхронизации)"
        if err:
            label += " — " + str(err)
        uniq.append({"type": "iframe", "src": url, "label": label})

    return uniq, None


async def broadcast(room, msg, exclude=None):
    dead = []
    for ws in list(room["clients"]):
        if ws is exclude:
            continue
        try:
            await ws.send_json(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        room["clients"].discard(ws)


@app.get("/", response_class=HTMLResponse)
async def index():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.post("/api/rooms")
async def create_room(payload: dict):
    url = payload.get("url")
    room_id = payload.get("room_id") or uuid.uuid4().hex[:8]
    room = get_or_create_room(room_id)
    if url:
        room["url"] = url
        media, err = await extract_media(url)
        room["media"] = media
        room["selected"] = 0 if media else None
        room["state"] = {"playing": False, "time": 0.0, "updated": time.time()}
    return {"room_id": room_id, "media": room["media"], "error": None, "state": room["state"], "selected": room["selected"]}


@app.get("/api/extract")
async def extract(url: str = Query(...)):
    media, err = await extract_media(url)
    return {"media": media, "error": err}


HLS_PROXY = "/api/hls?url="


def _rewrite_playlist(text: str, base_url: str) -> str:
    """Point every URI of an HLS playlist back at our proxy."""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            out.append("")
            continue
        if s.startswith("#"):
            def repl(m):
                u = str(httpx.URL(base_url).join(m.group(1)))
                return 'URI="' + HLS_PROXY + quote(u, safe="") + '"'
            out.append(re.sub(r'URI="([^"]+)"', repl, line))
        else:
            u = str(httpx.URL(base_url).join(s))
            out.append(HLS_PROXY + quote(u, safe=""))
    return "\n".join(out) + "\n"


@app.get("/api/hls")
async def hls_proxy(url: str = Query(...)):
    """Proxy HLS playlists/segments.

    Some CDNs block unusual user agents (Electron, etc.) and cross-origin
    playback, so the browser always fetches media through us.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "bad url")
    headers = {
        "User-Agent": UABROWSER,
        "Accept": "*/*",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        "Referer": url,
    }
    try:
        async with httpx.AsyncClient(http2=True, timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url, headers=headers)
    except Exception as e:
        raise HTTPException(502, f"upstream error: {e}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, "upstream error")

    ctype = r.headers.get("content-type", "")
    is_playlist = ".m3u8" in urlparse(str(r.url)).path or r.text.lstrip().startswith("#EXTM3U")
    if is_playlist:
        body = _rewrite_playlist(r.text, str(r.url)).encode("utf-8")
        return Response(content=body, media_type="application/vnd.apple.mpegurl")
    return Response(
        content=r.content,
        media_type=ctype or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "room not found")
    return serialize_room(room)


@app.on_event("startup")
async def start_sync_loop():
    asyncio.create_task(sync_loop())


async def sync_loop():
    """Periodically broadcast the authoritative playhead to every room.

    This is the Rave-style central clock: clients that buffered, stalled or
    drifted re-align themselves to the server position.
    """
    while True:
        await asyncio.sleep(2.5)
        for room in list(rooms.values()):
            if not room["clients"]:
                continue
            playing, tm = effective_position(room)
            await broadcast(room, {"type": "sync", "time": tm, "playing": playing})


@app.websocket("/ws/{room_id}")
async def ws_endpoint(websocket: WebSocket, room_id: str):
    room = get_or_create_room(room_id)
    await websocket.accept()
    room["clients"].add(websocket)
    await websocket.send_json({"type": "state", "room": serialize_room(room)})
    try:
        while True:
            data = await websocket.receive_json()
            t = data.get("type")
            if t == "load":
                url = data.get("url")
                room["url"] = url
                media, err = await extract_media(url)
                room["media"] = media
                room["selected"] = 0 if media else None
                room["state"] = {"playing": False, "time": 0.0, "updated": time.time()}
                await broadcast(room, {
                    "type": "loaded",
                    "url": url,
                    "media": media,
                    "selected": room["selected"],
                    "state": room["state"],
                    "error": err,
                })
            elif t == "select":
                idx = data.get("index")
                if idx is None or idx < 0 or idx >= len(room["media"]):
                    continue
                room["selected"] = idx
                await broadcast(room, {"type": "selected", "index": idx})
            elif t == "control":
                action = data.get("action")
                tm = data.get("time", room["state"]["time"])
                try:
                    tm = float(tm)
                except (TypeError, ValueError):
                    tm = room["state"]["time"]
                if action == "play":
                    room["state"]["playing"] = True
                    room["state"]["time"] = tm
                elif action == "pause":
                    room["state"]["playing"] = False
                    room["state"]["time"] = tm
                elif action == "seek":
                    room["state"]["time"] = tm
                room["state"]["updated"] = time.time()
                # do not echo back to the sender: it would re-trigger the
                # sender's local guard and swallow its next control event
                await broadcast(room, {
                    "type": "control",
                    "action": action,
                    "time": room["state"]["time"],
                    "playing": room["state"]["playing"],
                    "updated": room["state"]["updated"],
                }, exclude=websocket)
            elif t == "sync_request":
                playing, tm = effective_position(room)
                try:
                    await websocket.send_json({"type": "sync", "time": tm, "playing": playing})
                except Exception:
                    pass
            elif t == "chat":
                msg = {"type": "chat", "name": data.get("name", "anon"), "text": data.get("text", "")}
                room["chat"].append(msg)
                await broadcast(room, msg)
            elif t == "ping":
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        room["clients"].discard(websocket)
        await broadcast(room, {"type": "presence", "viewers": len(room["clients"])})
    except Exception as e:
        room["clients"].discard(websocket)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=False)
