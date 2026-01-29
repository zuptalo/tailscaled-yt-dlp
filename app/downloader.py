import asyncio
import glob
import logging
import os
import re
import shutil
import signal
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yt_dlp

from app.config import (
    COOKIES_FILE,
    DOWNLOADS_DIR,
    MAX_CONCURRENT_DOWNLOADS,
    THUMBNAILS_DIR,
    USER_AGENT,
)
from app.database import get_download, insert_download, update_download
from app.models import FormatInfo, VideoInfo

logger = logging.getLogger(__name__)


def _get_ffmpeg_path() -> str | None:
    """Get ffmpeg path - prefer system ffmpeg, fall back to imageio-ffmpeg bundle."""
    # Check if system ffmpeg is available
    if shutil.which("ffmpeg"):
        return None  # yt-dlp will find it in PATH

    # Fall back to imageio-ffmpeg bundle (for local dev without system ffmpeg)
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


def _cookies_file_valid() -> bool:
    """Check if cookies file exists and has valid content (non-empty)."""
    if not os.path.isfile(COOKIES_FILE):
        return False
    try:
        with open(COOKIES_FILE, "r") as f:
            content = f.read(100)  # Read first 100 chars
            # Must have some content (not just whitespace)
            return bool(content.strip())
    except Exception:
        return False


def _build_quality_label(f: dict) -> str:
    height = f.get("height")
    fps = f.get("fps")
    if height:
        label = f"{height}p"
        if fps and fps > 30:
            label += f"{int(fps)}"
        return label
    abr = f.get("abr")
    if abr:
        return f"{int(abr)}kbps"
    return f.get("format_note", f.get("format_id", "unknown"))


def _find_best_audio(formats: list[dict], max_abr: float = 128.0) -> dict | None:
    audio_fmts = [
        f for f in formats
        if f.get("acodec", "none") != "none"
        and f.get("vcodec", "none") in ("none", None)
    ]
    if not audio_fmts:
        return None

    under = [f for f in audio_fmts if (f.get("abr") or 0) <= max_abr]
    over = [f for f in audio_fmts if (f.get("abr") or 0) > max_abr]

    if under:
        return max(under, key=lambda f: f.get("abr") or 0)
    if over:
        return min(over, key=lambda f: f.get("abr") or 0)
    return audio_fmts[0]


_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub('', s)


def _next_vid_number() -> int:
    existing = glob.glob(os.path.join(DOWNLOADS_DIR, "vid[0-9]*.*"))
    nums = []
    for path in existing:
        basename = os.path.basename(path)
        m = re.match(r"vid(\d+)", basename)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _base_yt_dlp_opts() -> dict:
    opts = {
        "user_agent": USER_AGENT,
        "js_runtimes": {"node": {}},
        "compat_opts": {"allow-unsafe-ext"},
        "merge_output_format": "mp4",
        "paths": {"home": DOWNLOADS_DIR},
        "postprocessors": [
            {"key": "FFmpegMetadata"},
        ],
        "postprocessor_args": {
            "Merger": ["-movflags", "+faststart"],
        },
        "writethumbnail": False,  # We cache thumbnails separately
        # Prefer H.264 formats for iOS compatibility
        "format_sort": ["vcodec:h264", "acodec:aac"],
        # YouTube extractor args - use mweb (mobile web) client which often works without PO tokens
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "tv"],
            }
        },
        # Route traffic through Tailscale SOCKS5 proxy (for VPN exit node routing)
        "proxy": "socks5://localhost:1055",
    }
    if _cookies_file_valid():
        opts["cookiefile"] = COOKIES_FILE
    # Use bundled ffmpeg if system ffmpeg not available (for local dev)
    ffmpeg_path = _get_ffmpeg_path()
    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
    return opts


class DownloadManager:
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DOWNLOADS)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._broadcast = None  # set externally by main.py
        self._active_processes: dict[str, subprocess.Popen] = {}

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def set_broadcast(self, broadcast_fn):
        self._broadcast = broadcast_fn

    def _emit(self, event: str, data: dict):
        if self._broadcast and self._loop:
            self._loop.call_soon_threadsafe(self._broadcast, event, data)

    def fetch_formats(self, url: str) -> VideoInfo:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "user_agent": USER_AGENT,
            "js_runtimes": {"node": {}},
            "proxy": "socks5://localhost:1055",  # Route through Tailscale
        }
        if _cookies_file_valid():
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = []
        for f in info.get("formats", []):
            has_video = f.get("vcodec", "none") not in ("none", None)
            has_audio = f.get("acodec", "none") not in ("none", None)
            formats.append(FormatInfo(
                format_id=f.get("format_id", ""),
                ext=f.get("ext", ""),
                resolution=f.get("resolution"),
                fps=f.get("fps"),
                vcodec=f.get("vcodec"),
                acodec=f.get("acodec"),
                abr=f.get("abr"),
                filesize=f.get("filesize"),
                filesize_approx=f.get("filesize_approx"),
                quality_label=_build_quality_label(f),
                has_video=has_video,
                has_audio=has_audio,
            ))

        return VideoInfo(
            title=info.get("title"),
            thumbnail=info.get("thumbnail"),
            duration=info.get("duration"),
            formats=formats,
        )

    async def start_download(self, url: str, format_id: str | None = None, category_id: str | None = None) -> str:
        download_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        await insert_download({
            "id": download_id,
            "url": url,
            "title": None,
            "status": "queued",
            "progress": 0.0,
            "speed": None,
            "eta": None,
            "filesize": None,
            "downloaded_bytes": None,
            "filename": None,
            "format_id": format_id,
            "quality_label": None,
            "error_message": None,
            "thumbnail_url": None,
            "created_at": now,
            "updated_at": now,
            "category_id": category_id,
            "is_live": 0,
            "duration": None,
        })

        self._emit("download_queued", {"id": download_id, "url": url, "status": "queued"})
        self._pool.submit(self._run_download, download_id, url, format_id)
        return download_id

    def _run_download(self, download_id: str, url: str, format_id: str | None):
        try:
            self._do_download(download_id, url, format_id)
        except Exception as e:
            logger.exception("Download %s failed", download_id)
            self._sync_update(download_id, {
                "status": "failed",
                "error_message": str(e),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._emit("download_failed", {
                "id": download_id,
                "error_message": str(e),
                "status": "failed",
            })

    def _do_download(self, download_id: str, url: str, format_id: str | None):
        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        # Fetch info first
        self._sync_update(download_id, {"status": "fetching_info", "updated_at": now_iso()})

        extract_opts = {
            "quiet": True,
            "no_warnings": True,
            "user_agent": USER_AGENT,
            "js_runtimes": {"node": {}},
            "proxy": "socks5://localhost:1055",  # Route through Tailscale
        }
        if _cookies_file_valid():
            extract_opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title") or f"vid{_next_vid_number()}"
        thumbnail_url = info.get("thumbnail")
        is_live = bool(info.get("is_live"))

        # Determine format string
        format_str = None
        quality_label = None
        if format_id:
            all_formats = info.get("formats", [])
            selected = next((f for f in all_formats if f.get("format_id") == format_id), None)
            if selected:
                has_video = selected.get("vcodec", "none") not in ("none", None)
                has_audio = selected.get("acodec", "none") not in ("none", None)
                quality_label = _build_quality_label(selected)

                if has_video and not has_audio:
                    audio = _find_best_audio(all_formats)
                    if audio:
                        format_str = f"{format_id}+{audio['format_id']}"
                    else:
                        format_str = format_id
                else:
                    format_str = format_id

        duration = info.get("duration")

        update_fields = {
            "title": title,
            "thumbnail_url": thumbnail_url,
            "quality_label": quality_label,
            "format_id": format_id,
            "status": "downloading",
            "duration": duration,
            "updated_at": now_iso(),
        }
        if is_live:
            update_fields["is_live"] = 1

        self._sync_update(download_id, update_fields)

        self._emit("download_progress", {
            "id": download_id,
            "title": title,
            "thumbnail_url": thumbnail_url,
            "progress": 0.0,
            "status": "downloading",
            "is_live": is_live,
        })

        if is_live:
            self._do_live_download(download_id, url, title, format_str, thumbnail_url)
        else:
            self._do_regular_download(download_id, url, title, format_str, thumbnail_url)

    def _do_regular_download(self, download_id: str, url: str, title: str, format_str: str | None, thumbnail_url: str | None):
        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        # Use download_id (UUID) as filename
        output_filename = f"{download_id}.%(ext)s"

        # Progress hook
        def progress_hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                progress = (downloaded / total * 100) if total else 0.0
                speed = _strip_ansi(d.get("_speed_str", "")).strip()
                eta = _strip_ansi(d.get("_eta_str", "")).strip()

                self._sync_update(download_id, {
                    "progress": round(progress, 1),
                    "speed": speed or None,
                    "eta": eta or None,
                    "filesize": total,
                    "downloaded_bytes": downloaded,
                    "status": "downloading",
                    "updated_at": now_iso(),
                })
                self._emit("download_progress", {
                    "id": download_id,
                    "progress": round(progress, 1),
                    "speed": speed or None,
                    "eta": eta or None,
                    "filesize": total,
                    "downloaded_bytes": downloaded,
                    "status": "downloading",
                })
            elif d.get("status") == "finished":
                self._sync_update(download_id, {
                    "progress": 100.0,
                    "status": "post_processing",
                    "updated_at": now_iso(),
                })
                self._emit("download_progress", {
                    "id": download_id,
                    "progress": 100.0,
                    "status": "post_processing",
                })

        # Postprocessor hook
        def postprocessor_hook(d):
            if d.get("status") == "finished":
                filepath = d.get("info_dict", {}).get("filepath")
                if filepath:
                    self._sync_update(download_id, {
                        "filename": os.path.basename(filepath),
                        "updated_at": now_iso(),
                    })

        # Download
        dl_opts = _base_yt_dlp_opts()
        dl_opts["outtmpl"] = {"default": output_filename}
        dl_opts["progress_hooks"] = [progress_hook]
        dl_opts["postprocessor_hooks"] = [postprocessor_hook]
        if format_str:
            # Use format with fallback to best available
            dl_opts["format"] = f"{format_str}/bestvideo+bestaudio/best"
        else:
            dl_opts["format"] = "bestvideo+bestaudio/best"

        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            ydl.download([url])

        # Final — get the actual filename (should be UUID.ext)
        row = self._sync_get(download_id)
        filename = row.get("filename") if row else None
        if not filename:
            filename = f"{download_id}.mp4"

        filepath = os.path.join(DOWNLOADS_DIR, filename)

        # Re-encode to H.264/AAC for iOS compatibility if needed
        if os.path.isfile(filepath) and filepath.endswith(".mp4"):
            self._ensure_ios_compatible(download_id, filepath)

        # Cache thumbnail to thumbnails folder
        if thumbnail_url:
            self._cache_thumbnail(download_id, thumbnail_url)
        elif os.path.isfile(filepath):
            # Extract a frame as thumbnail if no URL provided
            self._extract_thumbnail_from_video(download_id, filepath)

        # Get final filesize and duration
        final_filesize = os.path.getsize(filepath) if os.path.isfile(filepath) else None
        row = self._sync_get(download_id)
        final_duration = row.get("duration") if row else None

        self._sync_update(download_id, {
            "status": "completed",
            "progress": 100.0,
            "filename": filename,
            "filesize": final_filesize,
            "updated_at": now_iso(),
        })
        self._emit("download_complete", {
            "id": download_id,
            "filename": filename,
            "status": "completed",
            "filesize": final_filesize,
            "duration": final_duration,
        })

    def _do_live_download(self, download_id: str, url: str, title: str, format_str: str | None, thumbnail_url: str | None):
        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        # Pre-cache thumbnail if available
        if thumbnail_url:
            self._cache_thumbnail(download_id, thumbnail_url)

        # Use download_id (UUID) as filename
        output_path = os.path.join(DOWNLOADS_DIR, f"{download_id}.%(ext)s")
        cmd = ["yt-dlp", "--newline", "--no-colors", "-o", output_path]
        cmd.extend(["--user-agent", USER_AGENT])
        cmd.extend(["--merge-output-format", "mp4"])
        cmd.extend(["--proxy", "socks5://localhost:1055"])  # Route through Tailscale

        # Use bundled ffmpeg if needed
        ffmpeg_path = _get_ffmpeg_path()
        if ffmpeg_path:
            cmd.extend(["--ffmpeg-location", ffmpeg_path])

        if _cookies_file_valid():
            cmd.extend(["--cookies", COOKIES_FILE])
        if format_str:
            cmd.extend(["-f", format_str])

        cmd.append(url)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self._active_processes[download_id] = proc

        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue

                # Parse yt-dlp progress lines like: [download]  45.2% of ~50.00MiB at 1.23MiB/s ETA 00:30
                m = re.search(r'\[download\]\s+([\d.]+)%', line)
                if m:
                    progress = float(m.group(1))

                    speed_m = re.search(r'at\s+(\S+)', line)
                    eta_m = re.search(r'ETA\s+(\S+)', line)
                    size_m = re.search(r'of\s+~?([\d.]+)(\w+)', line)

                    downloaded_bytes = None
                    filesize = None
                    if size_m:
                        size_val = float(size_m.group(1))
                        size_unit = size_m.group(2).upper()
                        multiplier = {"B": 1, "KIB": 1024, "MIB": 1048576, "GIB": 1073741824,
                                      "KB": 1000, "MB": 1000000, "GB": 1000000000}.get(size_unit, 1)
                        filesize = int(size_val * multiplier)
                        downloaded_bytes = int(filesize * progress / 100)

                    update_fields = {
                        "progress": round(progress, 1),
                        "speed": speed_m.group(1) if speed_m else None,
                        "eta": eta_m.group(1) if eta_m else None,
                        "status": "downloading",
                        "updated_at": now_iso(),
                    }
                    if downloaded_bytes is not None:
                        update_fields["downloaded_bytes"] = downloaded_bytes
                    if filesize is not None:
                        update_fields["filesize"] = filesize

                    self._sync_update(download_id, update_fields)
                    self._emit("download_progress", {
                        "id": download_id,
                        "progress": round(progress, 1),
                        "speed": speed_m.group(1) if speed_m else None,
                        "eta": eta_m.group(1) if eta_m else None,
                        "downloaded_bytes": downloaded_bytes,
                        "filesize": filesize,
                        "status": "downloading",
                        "is_live": True,
                    })

                # Detect destination filename
                dest_m = re.search(r'\[(?:download|Merger)\]\s+(?:Destination:\s+)?(.+\.(?:mp4|mkv|webm|ts|m4a|mp3))', line)
                if dest_m:
                    self._sync_update(download_id, {
                        "filename": os.path.basename(dest_m.group(1)),
                        "updated_at": now_iso(),
                    })

            proc.wait()
        finally:
            self._active_processes.pop(download_id, None)

        # Get final filename (should be UUID.ext)
        row = self._sync_get(download_id)
        filename = row.get("filename") if row else None
        if not filename:
            filename = f"{download_id}.mp4"

        # Determine filesize
        filepath = os.path.join(DOWNLOADS_DIR, filename)
        filesize = None
        if os.path.isfile(filepath):
            filesize = os.path.getsize(filepath)

            # Extract thumbnail if we don't have one cached
            thumb_path = os.path.join(THUMBNAILS_DIR, f"{download_id}.jpg")
            if not os.path.isfile(thumb_path):
                self._extract_thumbnail_from_video(download_id, filepath)

        self._sync_update(download_id, {
            "status": "completed",
            "progress": 100.0,
            "filename": filename,
            "filesize": filesize,
            "updated_at": now_iso(),
        })
        self._emit("download_complete", {
            "id": download_id,
            "filename": filename,
            "status": "completed",
            "filesize": filesize,
            "duration": None,
        })

    def stop_live_download(self, download_id: str) -> bool:
        proc = self._active_processes.get(download_id)
        if not proc:
            return False
        try:
            proc.send_signal(signal.SIGINT)
            return True
        except Exception:
            logger.exception("Failed to stop live download %s", download_id)
            return False

    def _sync_update(self, download_id: str, fields: dict):
        import asyncio as _aio
        loop = _aio.new_event_loop()
        try:
            loop.run_until_complete(update_download(download_id, fields))
        finally:
            loop.close()

    def _sync_get(self, download_id: str) -> dict | None:
        import asyncio as _aio
        loop = _aio.new_event_loop()
        try:
            return loop.run_until_complete(get_download(download_id))
        finally:
            loop.close()

    def _ensure_ios_compatible(self, download_id: str, filepath: str):
        """Re-encode video to H.264/AAC if not iOS compatible (VP9, AV1, etc.)."""
        try:
            # Check current video codec
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=30
            )
            vcodec = result.stdout.strip().lower()

            # iOS doesn't support VP9 or AV1 - need to re-encode to H.264
            if vcodec in ("vp9", "vp8", "av1"):
                logger.info(f"Re-encoding {vcodec} to H.264 for iOS compatibility: {download_id}")
                self._sync_update(download_id, {"status": "post_processing"})
                self._emit("download_progress", {
                    "id": download_id,
                    "status": "post_processing",
                    "progress": 100.0,
                })

                tmp_path = filepath + ".h264.mp4"
                ffmpeg_cmd = [
                    "ffmpeg", "-y", "-i", filepath,
                    "-c:v", "libx264",
                    "-preset", "fast",
                    "-crf", "23",
                    "-c:a", "aac",
                    "-b:a", "128k",
                    "-movflags", "+faststart",
                    "-pix_fmt", "yuv420p",
                    tmp_path
                ]

                proc = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=600)
                if proc.returncode == 0 and os.path.isfile(tmp_path):
                    os.replace(tmp_path, filepath)
                    logger.info(f"Re-encoded to H.264: {download_id}")
                else:
                    logger.warning(f"Re-encode failed for {download_id}: {proc.stderr.decode()[:200]}")
                    if os.path.isfile(tmp_path):
                        os.remove(tmp_path)
        except Exception as e:
            logger.warning(f"iOS compatibility check failed for {download_id}: {e}")

    def _cache_thumbnail(self, download_id: str, thumbnail_url: str):
        """Pre-cache thumbnail for a download."""
        try:
            import ssl
            import urllib.request

            os.makedirs(THUMBNAILS_DIR, exist_ok=True)
            thumb_path = os.path.join(THUMBNAILS_DIR, f"{download_id}.jpg")

            # Skip if already cached
            if os.path.isfile(thumb_path):
                return

            # Use unverified SSL context for thumbnail fetching (handles expired certs)
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(thumbnail_url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
                with open(thumb_path, "wb") as f:
                    f.write(resp.read())
            logger.debug("Cached thumbnail for %s", download_id)
        except Exception as e:
            logger.debug("Failed to cache thumbnail for %s: %s", download_id, e)

    def _extract_thumbnail_from_video(self, download_id: str, filepath: str):
        """Extract a frame from video as thumbnail."""
        try:
            os.makedirs(THUMBNAILS_DIR, exist_ok=True)
            thumb_path = os.path.join(THUMBNAILS_DIR, f"{download_id}.jpg")

            # Skip if already exists
            if os.path.isfile(thumb_path):
                return

            # Extract frame at 10 seconds (or start if video is shorter)
            subprocess.run(
                ["ffmpeg", "-y", "-i", filepath, "-ss", "10", "-vframes", "1",
                 "-vf", "scale=480:-1", thumb_path],
                capture_output=True, timeout=30,
            )
            if os.path.isfile(thumb_path):
                logger.debug("Extracted thumbnail from video for %s", download_id)
            else:
                logger.debug("Failed to extract thumbnail for %s", download_id)
        except Exception as e:
            logger.debug("Failed to extract thumbnail for %s: %s", download_id, e)

    def shutdown(self):
        # Stop any active live stream processes
        for proc in self._active_processes.values():
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                pass
        self._pool.shutdown(wait=False)
