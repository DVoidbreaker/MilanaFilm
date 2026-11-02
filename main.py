import os
import re
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).parent
app = FastAPI(title="Дениско пиписко и Милака милашка")

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


def serialize_room(room):
    return {
        "room_id": room["id"],
        "url": room["url"],
        "media": room["media"],
        "selected": room["selected"],
        "state": room["state"],
        "viewers": len(room["clients"]),
    }


async def extract_media(url: str):
    media = []
    err = None
    parsed0 = urlparse(url)
    if any(parsed0.path.lower().endswith(x) for x in (".mp4", ".webm", ".m3u8", ".ogg", ".mov")):
        return [{"type": "video", "src": url, "label": "direct"}], None
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=25.0) as client:
            r = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru,en;q=0.9",
            })
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return media, f"Не удалось загрузить страницу: {e}"

    soup = BeautifulSoup(html, "html.parser")

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

    return uniq, None


async def broadcast(room, msg):
    dead = []
    for ws in list(room["clients"]):
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


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str):
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "room not found")
    return serialize_room(room)


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
                await broadcast(room, {
                    "type": "control",
                    "action": action,
                    "time": room["state"]["time"],
                    "playing": room["state"]["playing"],
                    "updated": room["state"]["updated"],
                })
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
