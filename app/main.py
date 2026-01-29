import asyncio
import json
import logging
import os
import secrets
import ssl
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.auth import (
    create_share_access_token,
    create_token,
    hash_password,
    is_setup_complete,
    load_config,
    require_auth,
    revoke_token,
    save_config,
    validate_share_access_token,
    validate_token,
    verify_password,
)
from app.config import DOWNLOADS_DIR, THUMBNAILS_DIR, USER_AGENT
from app.database import (
    delete_category as db_delete_category,
    delete_download,
    delete_share_link as db_delete_share_link,
    delete_share_links_for_download,
    get_category as db_get_category,
    get_download,
    get_share_link as db_get_share_link,
    get_share_link_by_token,
    init_db,
    insert_category as db_insert_category,
    insert_share_link as db_insert_share_link,
    list_categories as db_list_categories,
    list_downloads,
    list_share_links as db_list_share_links,
    update_category as db_update_category,
    update_download,
)
from app.downloader import DownloadManager
from app.models import (
    CategoryCreate,
    CategoryUpdate,
    CredentialsUpdate,
    DownloadPatch,
    DownloadRequest,
    DownloadStatus,
    SettingsUpdate,
    ShareLinkCreate,
    ShareLinkInfo,
    VPNSettingsUpdate,
)
from app.vpn import VPNMonitor, connect, disconnect, list_exit_nodes, set_exit_node

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# SSE subscriber queues
_subscribers: list[asyncio.Queue] = []


def broadcast(event: str, data: dict):
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


download_manager = DownloadManager()
vpn_monitor = VPNMonitor()

# SSL context that doesn't verify certificates (for fetching thumbnails from various CDNs)
_thumbnail_ssl_ctx = ssl.create_default_context()
_thumbnail_ssl_ctx.check_hostname = False
_thumbnail_ssl_ctx.verify_mode = ssl.CERT_NONE


def _fetch_thumbnail(url: str, timeout: int = 10) -> bytes | None:
    """Fetch thumbnail from URL, handling SSL issues gracefully."""
    import urllib.request
    import urllib.error

    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout, context=_thumbnail_ssl_ctx) as resp:
            return resp.read()
    except Exception as e:
        logger.debug(f"_fetch_thumbnail failed for {url}: {type(e).__name__}: {e}")
        return None


async def _precache_thumbnails():
    """Pre-cache thumbnails for existing downloads on startup."""
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    downloads = await list_downloads()
    cached = 0
    for d in downloads:
        if d.get("status") != "completed":
            continue
        thumbnail_url = d.get("thumbnail_url")
        if not thumbnail_url:
            continue

        download_id = d["id"]

        # Determine extension
        ext = ".jpg"
        if ".png" in thumbnail_url.lower():
            ext = ".png"
        elif ".webp" in thumbnail_url.lower():
            ext = ".webp"

        cached_path = os.path.join(THUMBNAILS_DIR, f"{download_id}{ext}")

        # Skip if already cached
        if os.path.isfile(cached_path):
            continue

        # Fetch and cache using helper (handles SSL issues)
        data = _fetch_thumbnail(thumbnail_url)
        if data:
            with open(cached_path, "wb") as f:
                f.write(data)
            cached += 1

    if cached > 0:
        logger.info(f"Pre-cached {cached} thumbnails")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    loop = asyncio.get_running_loop()
    download_manager.set_event_loop(loop)
    download_manager.set_broadcast(broadcast)
    vpn_monitor.set_broadcast(broadcast)

    # Connect VPN from saved config if setup is complete
    if is_setup_complete():
        await vpn_monitor.connect_from_config()

    await vpn_monitor.start()

    # Pre-cache thumbnails in background
    asyncio.create_task(_precache_thumbnails())

    logger.info("Application started")
    yield
    await vpn_monitor.stop()
    download_manager.shutdown()
    logger.info("Application stopped")


app = FastAPI(lifespan=lifespan)

# Resolve static directory: works in Docker (/static) and locally (./static)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATIC_DIR = os.path.join(_PROJECT_ROOT, "static")

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


@app.get("/manifest.json")
async def manifest():
    return FileResponse(os.path.join(_STATIC_DIR, "manifest.json"))


@app.get("/sw.js")
async def service_worker():
    return FileResponse(os.path.join(_STATIC_DIR, "sw.js"), media_type="application/javascript")


@app.get("/favicon.ico")
async def favicon():
    return FileResponse(os.path.join(_STATIC_DIR, "icon.svg"), media_type="image/svg+xml")


# =====================================================================
# Setup endpoints (no auth required — only functional before setup)
# =====================================================================

@app.get("/api/setup/status")
async def setup_status():
    return {"setup_complete": is_setup_complete()}


class SetupConnectRequest(BaseModel):
    headscale_url: str
    headscale_authkey: str


@app.post("/api/setup/connect")
async def setup_connect(req: SetupConnectRequest):
    if is_setup_complete():
        raise HTTPException(status_code=403, detail="Setup already complete")

    success = await connect(req.headscale_url, req.headscale_authkey)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to connect to Headscale. Check URL and auth key.")

    # Wait briefly for peer list to populate
    await asyncio.sleep(2)

    nodes = await list_exit_nodes()
    return {"success": True, "exit_nodes": nodes}


class SetupCompleteRequest(BaseModel):
    username: str
    password: str
    headscale_url: str
    headscale_authkey: str
    exit_node: str


@app.post("/api/setup/complete")
async def setup_complete(req: SetupCompleteRequest):
    if is_setup_complete():
        raise HTTPException(status_code=403, detail="Setup already complete")

    if len(req.username) < 1:
        raise HTTPException(status_code=400, detail="Username is required")
    if len(req.password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    # Configure exit node
    success = await connect(req.headscale_url, req.headscale_authkey, req.exit_node)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to set exit node")

    # Hash password and save config
    password_hash, salt = hash_password(req.password)
    save_config({
        "username": req.username,
        "password_hash": password_hash,
        "password_salt": salt,
        "headscale_url": req.headscale_url,
        "headscale_authkey": req.headscale_authkey,
        "exit_node": req.exit_node,
    })

    vpn_monitor.exit_node = req.exit_node

    # Create auth token
    token = create_token()
    return {"token": token}


# =====================================================================
# Auth endpoints (no auth required for login)
# =====================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    config = load_config()
    if not config:
        raise HTTPException(status_code=403, detail="Setup not complete")

    if req.username != config.get("username"):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not verify_password(req.password, config["password_hash"], config["password_salt"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token()
    return {"token": token}


@app.get("/api/auth/validate")
async def validate_auth(request: Request):
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token")
    if not token or not validate_token(token):
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"valid": True}


@app.post("/api/auth/logout", dependencies=[Depends(require_auth)])
async def logout(request: Request):
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        revoke_token(auth_header[7:])
    return {"ok": True}


# =====================================================================
# Protected API endpoints (auth required)
# =====================================================================

@app.get("/api/formats", dependencies=[Depends(require_auth)])
async def get_formats(url: str):
    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, download_manager.fetch_formats, url)
        return info.model_dump()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/proxy-thumbnail", dependencies=[Depends(require_auth)])
async def proxy_thumbnail(url: str):
    """Proxy an external thumbnail URL to avoid CORS issues."""
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")

    content = _fetch_thumbnail(url)
    if not content:
        raise HTTPException(status_code=502, detail="Failed to fetch thumbnail")

    # Determine content type from URL
    content_type = "image/jpeg"
    if ".png" in url.lower():
        content_type = "image/png"
    elif ".webp" in url.lower():
        content_type = "image/webp"

    return Response(content=content, media_type=content_type)


@app.post("/api/downloads", dependencies=[Depends(require_auth)])
async def create_download(req: DownloadRequest):
    download_id = await download_manager.start_download(req.url, req.format_id, req.category_id)
    return {"id": download_id, "status": "queued"}


@app.get("/api/downloads", dependencies=[Depends(require_auth)])
async def get_downloads():
    rows = await list_downloads()
    return [DownloadStatus(**r).model_dump() for r in rows]


@app.delete("/api/downloads/{download_id}", dependencies=[Depends(require_auth)])
async def remove_download(download_id: str):
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")

    # Delete the video file
    if row.get("filename"):
        filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
        if os.path.isfile(filepath):
            os.remove(filepath)

    # Delete cached thumbnail (could be .jpg, .png, or .webp)
    for ext in [".jpg", ".png", ".webp"]:
        thumb_path = os.path.join(THUMBNAILS_DIR, f"{download_id}{ext}")
        if os.path.isfile(thumb_path):
            os.remove(thumb_path)

    # Cascade delete share links
    await delete_share_links_for_download(download_id)
    await delete_download(download_id)
    return {"ok": True}


@app.post("/api/downloads/{download_id}/retry", dependencies=[Depends(require_auth)])
async def retry_download(download_id: str):
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    if row["status"] != "failed":
        raise HTTPException(status_code=400, detail="Only failed downloads can be retried")

    await delete_share_links_for_download(download_id)
    await delete_download(download_id)
    new_id = await download_manager.start_download(row["url"], row.get("format_id"), row.get("category_id"))
    return {"id": new_id, "status": "queued"}


# --- File Download ---

@app.get("/api/downloads/{download_id}/file")
async def download_file(download_id: str, request: Request):
    # Auth via header or query param
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token")
    if not token or not validate_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    if row["status"] != "completed" or not row.get("filename"):
        raise HTTPException(status_code=400, detail="File not available")

    filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(filepath, filename=row["filename"], media_type="application/octet-stream")


# --- Video streaming ---

_MIME_MAP = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4",
    ".mp3": "audio/mpeg",
    ".ts": "video/mp2t",
    ".ogg": "audio/ogg",
    ".opus": "audio/opus",
    ".flac": "audio/flac",
    ".wav": "audio/wav",
}


@app.get("/api/downloads/{download_id}/stream")
async def stream_file(download_id: str, request: Request):
    # Auth via header or query param
    token = None
    auth_header = request.headers.get("authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token")
    if not token or not validate_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    if row["status"] != "completed" or not row.get("filename"):
        raise HTTPException(status_code=400, detail="File not available")

    filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk")

    ext = os.path.splitext(row["filename"])[1].lower()
    mime = _MIME_MAP.get(ext, "application/octet-stream")

    return FileResponse(filepath, media_type=mime)


# --- Thumbnail serving with caching ---

@app.get("/api/downloads/{download_id}/thumbnail")
async def get_thumbnail(download_id: str):
    """Serve cached thumbnail, fetching from external URL if needed."""
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")

    thumbnail_url = row.get("thumbnail_url")
    if not thumbnail_url:
        raise HTTPException(status_code=404, detail="No thumbnail available")

    # Ensure thumbnails directory exists
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    # Determine file extension from URL or default to jpg
    ext = ".jpg"
    if ".png" in thumbnail_url.lower():
        ext = ".png"
    elif ".webp" in thumbnail_url.lower():
        ext = ".webp"

    cached_path = os.path.join(THUMBNAILS_DIR, f"{download_id}{ext}")

    # If not cached, fetch and save using helper (handles SSL issues)
    if not os.path.isfile(cached_path):
        data = _fetch_thumbnail(thumbnail_url)
        if data:
            with open(cached_path, "wb") as f:
                f.write(data)
        else:
            logger.warning(f"Failed to fetch thumbnail for {download_id}")
            raise HTTPException(status_code=502, detail="Failed to fetch thumbnail")

    if not os.path.isfile(cached_path):
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    mime_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime = mime_types.get(ext, "image/jpeg")

    return FileResponse(cached_path, media_type=mime)


# --- Download management (PATCH + Stop) ---

@app.patch("/api/downloads/{download_id}", dependencies=[Depends(require_auth)])
async def patch_download(download_id: str, patch: DownloadPatch):
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")

    fields = {"updated_at": datetime.now(timezone.utc).isoformat()}
    if patch.category_id is not None:
        fields["category_id"] = patch.category_id if patch.category_id != "" else None
    else:
        fields["category_id"] = None

    await update_download(download_id, fields)
    return {"ok": True}


@app.post("/api/downloads/{download_id}/stop", dependencies=[Depends(require_auth)])
async def stop_download(download_id: str):
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    if not row.get("is_live"):
        raise HTTPException(status_code=400, detail="Not a live stream download")

    success = download_manager.stop_live_download(download_id)
    if not success:
        raise HTTPException(status_code=400, detail="No active process to stop")
    return {"ok": True}


# --- SSE (auth via query param since EventSource can't set headers) ---

@app.get("/api/events")
async def sse(request: Request):
    # Auth check: require token as query param
    token = request.query_params.get("token")
    if not token or not validate_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    _subscribers.append(queue)

    async def event_stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield msg
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
        finally:
            if queue in _subscribers:
                _subscribers.remove(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/vpn/status", dependencies=[Depends(require_auth)])
async def vpn_status():
    return vpn_monitor.status_dict()


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "vpn_connected": vpn_monitor.connected,
        "vpn_exit_node_online": vpn_monitor.exit_node_online,
    }


# =====================================================================
# Categories
# =====================================================================

@app.get("/api/categories", dependencies=[Depends(require_auth)])
async def list_categories():
    rows = await db_list_categories()
    return rows


@app.post("/api/categories", dependencies=[Depends(require_auth)])
async def create_category(req: CategoryCreate):
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    cat_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        await db_insert_category({"id": cat_id, "name": name, "created_at": now})
    except Exception:
        raise HTTPException(status_code=409, detail="Category name already exists")

    return {"id": cat_id, "name": name, "created_at": now}


@app.put("/api/categories/{category_id}", dependencies=[Depends(require_auth)])
async def rename_category(category_id: str, req: CategoryUpdate):
    cat = await db_get_category(category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    try:
        await db_update_category(category_id, {"name": name})
    except Exception:
        raise HTTPException(status_code=409, detail="Category name already exists")

    return {"id": category_id, "name": name}


@app.delete("/api/categories/{category_id}", dependencies=[Depends(require_auth)])
async def remove_category(category_id: str):
    cat = await db_get_category(category_id)
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")

    await db_delete_category(category_id)
    return {"ok": True}


# =====================================================================
# Share Links
# =====================================================================

@app.post("/api/downloads/{download_id}/share", dependencies=[Depends(require_auth)])
async def create_share_link(download_id: str, req: ShareLinkCreate):
    row = await get_download(download_id)
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")
    if row["status"] != "completed":
        raise HTTPException(status_code=400, detail="Only completed downloads can be shared")

    link_id = str(uuid.uuid4())
    link_token = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc).isoformat()

    password_hash = None
    password_salt = None
    if req.password:
        password_hash, password_salt = hash_password(req.password)

    await db_insert_share_link({
        "id": link_id,
        "download_id": download_id,
        "token": link_token,
        "password_hash": password_hash,
        "password_salt": password_salt,
        "expires_at": req.expires_at,
        "created_at": now,
    })

    # Build share URL
    config = load_config() or {}
    public_url = config.get("public_url", "").rstrip("/")
    url = f"{public_url}/s/{link_token}" if public_url else f"/s/{link_token}"

    return ShareLinkInfo(
        id=link_id,
        download_id=download_id,
        token=link_token,
        has_password=password_hash is not None,
        expires_at=req.expires_at,
        created_at=now,
        url=url,
    ).model_dump()


@app.get("/api/downloads/{download_id}/shares", dependencies=[Depends(require_auth)])
async def get_share_links(download_id: str):
    links = await db_list_share_links(download_id)
    config = load_config() or {}
    public_url = config.get("public_url", "").rstrip("/")

    result = []
    for link in links:
        url = f"{public_url}/s/{link['token']}" if public_url else f"/s/{link['token']}"
        result.append(ShareLinkInfo(
            id=link["id"],
            download_id=link["download_id"],
            token=link["token"],
            has_password=link.get("password_hash") is not None,
            expires_at=link.get("expires_at"),
            created_at=link["created_at"],
            url=url,
        ).model_dump())
    return result


@app.delete("/api/shares/{link_id}", dependencies=[Depends(require_auth)])
async def remove_share_link(link_id: str):
    link = await db_get_share_link(link_id)
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")
    await db_delete_share_link(link_id)
    return {"ok": True}


# =====================================================================
# Public share page & download (no auth)
# =====================================================================

SHARE_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>{title} - Shared File</title>
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: #0f0f0f; color: #e5e5e5; min-height: 100vh;
  display: flex; justify-content: center; align-items: center;
}}
.card {{
  background: #1a1a1a; border: 1px solid #2a2a2a;
  border-radius: 12px; padding: 24px; max-width: 480px; width: 100%; margin: 16px;
}}
.card-title {{ font-size: 18px; font-weight: 600; margin-bottom: 8px; word-break: break-word; }}
.card-meta {{ font-size: 13px; color: #737373; margin-bottom: 16px; }}
.thumb-wrap {{
  position: relative; width: 100%; aspect-ratio: 16/9; border-radius: 10px;
  overflow: hidden; background: #2a2a2a; margin-bottom: 16px; cursor: pointer;
}}
.thumb-wrap img {{ width: 100%; height: 100%; object-fit: cover; }}
.thumb-play {{
  position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  background: rgba(0,0,0,0.4); transition: background 0.2s;
}}
.thumb-wrap:hover .thumb-play {{ background: rgba(0,0,0,0.6); }}
.thumb-play svg {{ width: 64px; height: 64px; fill: #fff; filter: drop-shadow(0 2px 8px rgba(0,0,0,0.5)); }}
.btn {{
  display: block; width: 100%; padding: 12px; border-radius: 8px; border: none;
  font-size: 14px; font-weight: 500; cursor: pointer; text-align: center;
  text-decoration: none; transition: all 0.2s; margin-bottom: 8px;
}}
.btn:last-child {{ margin-bottom: 0; }}
.btn-primary {{ background: #3b82f6; color: #fff; }}
.btn-primary:hover {{ background: #2563eb; }}
.btn-ghost {{ background: transparent; color: #a3a3a3; border: 1px solid #2a2a2a; }}
.btn-ghost:hover {{ background: #2a2a2a; color: #e5e5e5; }}
.form-input {{
  width: 100%; padding: 10px 14px; border-radius: 8px;
  border: 1px solid #2a2a2a; background: #0f0f0f; color: #e5e5e5;
  font-size: 16px; outline: none; margin-bottom: 12px;
}}
.form-input:focus {{ border-color: #3b82f6; }}
.error {{ color: #ef4444; font-size: 13px; margin-bottom: 12px; display: none; }}
.expired {{ text-align: center; color: #ef4444; font-size: 14px; }}
</style>
</head>
<body>
<div class="card">
{content}
</div>
{script}
</body>
</html>"""


@app.get("/s/{token}", response_class=HTMLResponse)
async def share_page(token: str):
    link = await get_share_link_by_token(token)
    if not link:
        return HTMLResponse(SHARE_PAGE_TEMPLATE.format(
            title="Not Found",
            content='<div class="expired">This share link does not exist.</div>',
            script="",
        ), status_code=404)

    # Check expiration
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires:
            return HTMLResponse(SHARE_PAGE_TEMPLATE.format(
                title="Expired",
                content='<div class="expired">This share link has expired.</div>',
                script="",
            ))

    row = await get_download(link["download_id"])
    if not row or row["status"] != "completed" or not row.get("filename"):
        return HTMLResponse(SHARE_PAGE_TEMPLATE.format(
            title="Unavailable",
            content='<div class="expired">This file is no longer available.</div>',
            script="",
        ))

    title = row.get("title") or row.get("filename") or "File"
    filesize = row.get("filesize")
    size_str = ""
    if filesize:
        units = ["B", "KB", "MB", "GB"]
        val = filesize
        i = 0
        while val >= 1024 and i < len(units) - 1:
            val /= 1024
            i += 1
        size_str = f"{val:.1f} {units[i]}" if i > 0 else f"{val} {units[i]}"

    has_password = link.get("password_hash") is not None

    if has_password:
        thumbnail_url = f"/s/{token}/thumbnail" if row.get("thumbnail_url") else ""
        thumb_html = f'''<div class="thumb-wrap" id="thumbWrap" style="display:none" onclick="playVideo()">
<img src="{thumbnail_url}" alt="" onerror="this.style.display='none'">
<div class="thumb-play"><svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"/></svg></div>
</div>''' if thumbnail_url else ""

        content = f'''{thumb_html}<div class="card-title">{_html_escape(title)}</div>
<div class="card-meta">{size_str}</div>
<div class="error" id="error">Incorrect password</div>
<div id="passwordSection">
<input class="form-input" type="password" id="password" placeholder="Enter password" autocomplete="off" autocapitalize="off">
<button class="btn btn-primary" onclick="verifyPassword()">Unlock</button>
</div>
<div id="actionSection" style="display:none">
<button class="btn btn-primary" onclick="playVideo()">Play</button>
<a class="btn btn-ghost" id="downloadLink" href="#">Download</a>
</div>'''
        script = f'''<script>
let accessToken = null;
async function verifyPassword() {{
  const pw = document.getElementById('password').value;
  if (!pw) return;
  try {{
    const resp = await fetch('/s/{token}/verify', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{password: pw}}),
    }});
    if (!resp.ok) {{
      document.getElementById('error').style.display = 'block';
      return;
    }}
    const data = await resp.json();
    accessToken = data.access_token;
    document.getElementById('passwordSection').style.display = 'none';
    document.getElementById('actionSection').style.display = 'block';
    document.getElementById('downloadLink').href = '/s/{token}/download?access=' + accessToken;
    const thumb = document.getElementById('thumbWrap');
    if (thumb) thumb.style.display = 'block';
  }} catch (e) {{
    document.getElementById('error').style.display = 'block';
  }}
}}
function playVideo() {{
  if (!accessToken) return;
  window.location.href = '/s/{token}/stream?access=' + accessToken;
}}
document.getElementById('password').addEventListener('keydown', e => {{
  if (e.key === 'Enter') verifyPassword();
}});
</script>'''
    else:
        thumbnail_url = f"/s/{token}/thumbnail" if row.get("thumbnail_url") else ""
        stream_url = f"/s/{token}/stream"
        thumb_html = f'''<div class="thumb-wrap" onclick="playVideo()">
<img src="{thumbnail_url}" alt="" onerror="this.style.display='none'">
<div class="thumb-play"><svg viewBox="0 0 24 24"><polygon points="5,3 19,12 5,21"/></svg></div>
</div>''' if thumbnail_url else ""

        content = f'''{thumb_html}<div class="card-title">{_html_escape(title)}</div>
<div class="card-meta">{size_str}</div>
<button class="btn btn-primary" onclick="playVideo()">Play</button>
<a class="btn btn-ghost" href="/s/{token}/download">Download</a>'''
        script = f'''<script>
function playVideo() {{
  window.location.href = '{stream_url}';
}}
</script>'''

    return HTMLResponse(SHARE_PAGE_TEMPLATE.format(
        title=_html_escape(title),
        content=content,
        script=script,
    ))


@app.post("/s/{token}/verify")
async def share_verify(token: str, request: Request):
    link = await get_share_link_by_token(token)
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    # Check expiration
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=410, detail="Share link expired")

    if not link.get("password_hash"):
        raise HTTPException(status_code=400, detail="No password required")

    body = await request.json()
    password = body.get("password", "")

    if not verify_password(password, link["password_hash"], link["password_salt"]):
        raise HTTPException(status_code=401, detail="Incorrect password")

    access_token = create_share_access_token(ttl=300)
    return {"access_token": access_token}


@app.get("/s/{token}/download")
async def share_download(token: str, request: Request):
    link = await get_share_link_by_token(token)
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    # Check expiration
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=410, detail="Share link expired")

    # Password-protected links require access token
    if link.get("password_hash"):
        access = request.query_params.get("access")
        if not access or not validate_share_access_token(access):
            raise HTTPException(status_code=401, detail="Access denied")

    row = await get_download(link["download_id"])
    if not row or row["status"] != "completed" or not row.get("filename"):
        raise HTTPException(status_code=404, detail="File not available")

    filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk")

    return FileResponse(filepath, filename=row["filename"], media_type="application/octet-stream")


@app.get("/s/{token}/stream")
async def share_stream(token: str, request: Request):
    """Stream shared file for in-browser playback."""
    link = await get_share_link_by_token(token)
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    # Check expiration
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=410, detail="Share link expired")

    # Password-protected links require access token
    if link.get("password_hash"):
        access = request.query_params.get("access")
        if not access or not validate_share_access_token(access):
            raise HTTPException(status_code=401, detail="Access denied")

    row = await get_download(link["download_id"])
    if not row or row["status"] != "completed" or not row.get("filename"):
        raise HTTPException(status_code=404, detail="File not available")

    filepath = os.path.join(DOWNLOADS_DIR, row["filename"])
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="File not found on disk")

    ext = os.path.splitext(row["filename"])[1].lower()
    mime = _MIME_MAP.get(ext, "application/octet-stream")

    return FileResponse(filepath, media_type=mime)


@app.get("/s/{token}/thumbnail")
async def share_thumbnail(token: str):
    """Serve thumbnail for shared file."""
    link = await get_share_link_by_token(token)
    if not link:
        raise HTTPException(status_code=404, detail="Share link not found")

    # Check expiration
    if link.get("expires_at"):
        expires = datetime.fromisoformat(link["expires_at"])
        if datetime.now(timezone.utc) > expires:
            raise HTTPException(status_code=410, detail="Share link expired")

    row = await get_download(link["download_id"])
    if not row:
        raise HTTPException(status_code=404, detail="Download not found")

    thumbnail_url = row.get("thumbnail_url")
    if not thumbnail_url:
        raise HTTPException(status_code=404, detail="No thumbnail available")

    # Use same caching logic as authenticated thumbnail endpoint
    os.makedirs(THUMBNAILS_DIR, exist_ok=True)

    ext = ".jpg"
    if ".png" in thumbnail_url.lower():
        ext = ".png"
    elif ".webp" in thumbnail_url.lower():
        ext = ".webp"

    download_id = link["download_id"]
    cached_path = os.path.join(THUMBNAILS_DIR, f"{download_id}{ext}")

    # Fetch and cache using helper (handles SSL issues)
    if not os.path.isfile(cached_path):
        data = _fetch_thumbnail(thumbnail_url)
        if data:
            with open(cached_path, "wb") as f:
                f.write(data)
        else:
            logger.warning(f"Failed to fetch thumbnail for shared {download_id}")
            raise HTTPException(status_code=502, detail="Failed to fetch thumbnail")

    if not os.path.isfile(cached_path):
        raise HTTPException(status_code=404, detail="Thumbnail not available")

    mime_types = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
    mime = mime_types.get(ext, "image/jpeg")

    return FileResponse(cached_path, media_type=mime)


# =====================================================================
# Settings
# =====================================================================

@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    config = load_config() or {}
    return {"public_url": config.get("public_url", "")}


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def update_settings(req: SettingsUpdate):
    save_config({"public_url": req.public_url.strip().rstrip("/")})
    return {"ok": True}


@app.put("/api/settings/credentials", dependencies=[Depends(require_auth)])
async def update_credentials(req: CredentialsUpdate):
    config = load_config()
    if not config:
        raise HTTPException(status_code=400, detail="No config found")

    if not verify_password(req.current_password, config["password_hash"], config["password_salt"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")

    updates = {}
    if req.new_username and req.new_username.strip():
        updates["username"] = req.new_username.strip()
    if req.new_password:
        if len(req.new_password) < 4:
            raise HTTPException(status_code=400, detail="Password must be at least 4 characters")
        password_hash, salt = hash_password(req.new_password)
        updates["password_hash"] = password_hash
        updates["password_salt"] = salt

    if not updates:
        raise HTTPException(status_code=400, detail="No changes specified")

    save_config(updates)
    return {"ok": True}


@app.get("/api/settings/vpn", dependencies=[Depends(require_auth)])
async def get_vpn_settings():
    config = load_config() or {}
    nodes = await list_exit_nodes()
    return {
        "headscale_url": config.get("headscale_url", ""),
        "exit_node": config.get("exit_node", ""),
        "exit_nodes": nodes,
    }


@app.put("/api/settings/vpn", dependencies=[Depends(require_auth)])
async def update_vpn_settings(req: VPNSettingsUpdate):
    # Use existing config values if not provided
    config = load_config() or {}
    headscale_url = req.headscale_url or config.get("headscale_url", "")
    headscale_authkey = req.headscale_authkey or config.get("headscale_authkey", "")
    exit_node = req.exit_node or config.get("exit_node")

    if not headscale_url or not headscale_authkey:
        raise HTTPException(status_code=400, detail="Headscale URL and auth key are required")

    success = await connect(headscale_url, headscale_authkey, exit_node)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to connect with new VPN settings")

    updates = {
        "headscale_url": headscale_url,
        "headscale_authkey": headscale_authkey,
    }
    if exit_node:
        updates["exit_node"] = exit_node
        vpn_monitor.exit_node = exit_node

    save_config(updates)
    return {"ok": True}


@app.put("/api/vpn/exit-node", dependencies=[Depends(require_auth)])
async def update_exit_node(request: Request):
    """Change exit node without re-authenticating."""
    body = await request.json()
    exit_node = body.get("exit_node")

    success = await set_exit_node(exit_node)
    if not success:
        raise HTTPException(status_code=502, detail="Failed to change exit node")

    if exit_node:
        save_config({"exit_node": exit_node})
        vpn_monitor.exit_node = exit_node
    else:
        save_config({"exit_node": ""})
        vpn_monitor.exit_node = None

    return {"ok": True}


@app.post("/api/vpn/disconnect", dependencies=[Depends(require_auth)])
async def disconnect_vpn():
    """Disconnect from Tailscale."""
    success = await disconnect()
    if not success:
        raise HTTPException(status_code=502, detail="Failed to disconnect VPN")
    vpn_monitor.connected = False
    vpn_monitor.exit_node_online = False
    return {"ok": True}


@app.get("/api/vpn/ip", dependencies=[Depends(require_auth)])
async def get_external_ip():
    """Get external IP address via Tailscale SOCKS5 proxy."""
    import subprocess
    try:
        # Use curl through Tailscale SOCKS5 proxy to get exit node's external IP
        result = subprocess.run(
            ["curl", "-s", "--max-time", "5", "--socks5", "localhost:1055", "https://api.ipify.org"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"ip": result.stdout.strip()}
        return {"ip": "Unknown"}
    except Exception as e:
        logger.debug(f"Failed to get external IP: {e}")
        return {"ip": "Unknown"}


# =====================================================================
# Helpers
# =====================================================================

def _html_escape(s: str) -> str:
    return (s
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;"))
