import asyncio
import glob
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import yt_dlp
from yt_dlp.utils import DownloadError

from app.auth import config_use_vpn, load_config
from app.config import (
    COOKIES_FILE,
    DOWNLOADS_DIR,
    MAX_CONCURRENT_DOWNLOADS,
    THUMBNAILS_DIR,
    USER_AGENT,
)
from app.database import get_download, insert_download, update_download
from app.models import FormatInfo, VideoInfo
from app.vpn import has_active_exit_node_sync, set_exit_node_sync

logger = logging.getLogger(__name__)


def _normalized_exit_override(exit_node: str | None) -> str | None:
    s = (exit_node or "").strip()
    return s or None


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


def _approx_stream_bytes(f: dict | None, duration_sec: float | None) -> int | None:
    """Bytes for one stream: prefer metadata; else duration * bitrate from yt-dlp format fields."""
    if not f:
        return None
    for key in ("filesize", "filesize_approx"):
        v = f.get(key)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    if not duration_sec or duration_sec <= 0:
        return None
    try:
        tbr = f.get("tbr")
        if tbr is not None and float(tbr) > 0:
            return int(float(tbr) * 1000 * duration_sec / 8)
    except (TypeError, ValueError):
        pass
    try:
        kbps = 0.0
        vbr = f.get("vbr")
        abr = f.get("abr")
        if vbr is not None:
            kbps += float(vbr)
        if abr is not None:
            kbps += float(abr)
        if kbps > 0:
            return int(kbps * 1000 * duration_sec / 8)
    except (TypeError, ValueError):
        pass
    return None


def _estimated_merged_size_bytes(formats: list[dict], video_row: dict, duration_sec: float | None) -> int | None:
    """Final file size estimate for a row we would download as video + default audio (video-only DASH)."""
    v = _approx_stream_bytes(video_row, duration_sec)
    if v is None:
        return None
    has_video = video_row.get("vcodec", "none") not in ("none", None)
    has_audio = video_row.get("acodec", "none") not in ("none", None)
    if not has_video or has_audio:
        return v
    audio = _find_best_audio(formats)
    if not audio:
        return v
    a = _approx_stream_bytes(audio, duration_sec)
    if a:
        return v + a
    return v


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


def _format_speed_bps(speed: float) -> str:
    """Format yt-dlp numeric speed (bytes/s) like yt-dlp's MiB/s strings."""
    if speed <= 0:
        return ""
    s = float(speed)
    for label, div in (("GiB/s", 1073741824), ("MiB/s", 1048576), ("KiB/s", 1024)):
        if s >= div:
            return f"{s / div:.2f}{label}"
    return f"{int(s)}B/s"


def _format_eta_seconds(eta: float) -> str:
    if eta < 0:
        return ""
    sec = int(eta)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:d}:{s:02d}"


def _format_row_matches_id(f: dict, format_id: str | None) -> bool:
    if not format_id:
        return False
    fid = f.get("format_id")
    if fid is None:
        return False
    return str(fid).strip() == str(format_id).strip()


def _merge_format_ids(format_str: str | None) -> list[str]:
    if not format_str:
        return []
    head = format_str.split("/")[0].strip()
    return [p.strip() for p in head.split("+") if p.strip()]


def _initial_filesize_estimate(info: dict, format_id: str | None, format_str: str | None) -> int | None:
    """Best-effort total size from metadata before per-fragment hooks run.

    YouTube (and similar) often set top-level info['filesize_approx'] to a rough maximum across
    *all* formats, not the chosen stream — so never prefer that when the user picked a format.

    Video-only selections are downloaded with a separate audio stream; include that size (metadata
    or bitrate*duration) so the estimate matches the final merged file.
    """
    formats = info.get("formats") or []
    duration = info.get("duration")
    fid = str(format_id).strip() if format_id else None
    want_specific = bool(fid or (format_str and str(format_str).strip()))

    parts = _merge_format_ids(format_str)
    if len(parts) > 1:
        by_id = {str(f.get("format_id")): f for f in formats if f.get("format_id") is not None}
        total = 0
        found_any = False
        for pid in parts:
            row = by_id.get(pid)
            b = _approx_stream_bytes(row, duration)
            if b:
                total += b
                found_any = True
        if found_any and total > 0:
            return total

    target_id = fid
    if not target_id and len(parts) == 1:
        target_id = parts[0]

    if target_id:
        selected = next((f for f in formats if _format_row_matches_id(f, target_id)), None)
        if selected:
            merged = _estimated_merged_size_bytes(formats, selected, duration)
            if merged:
                return merged
        if want_specific:
            return None

    if want_specific:
        return None

    for key in ("filesize", "filesize_approx"):
        v = info.get(key)
        if v:
            try:
                return int(v)
            except (TypeError, ValueError):
                pass
    return None


def _next_vid_number() -> int:
    existing = glob.glob(os.path.join(DOWNLOADS_DIR, "vid[0-9]*.*"))
    nums = []
    for path in existing:
        basename = os.path.basename(path)
        m = re.match(r"vid(\d+)", basename)
        if m:
            nums.append(int(m.group(1)))
    return max(nums, default=0) + 1


def _vpn_proxy_url() -> str | None:
    """SOCKS URL when VPN is enabled AND an exit node is actively set; otherwise direct."""
    if not config_use_vpn():
        return None
    if not has_active_exit_node_sync():
        return None
    return "socks5://localhost:1055"


def _merge_proxy(opts: dict) -> None:
    proxy = _vpn_proxy_url()
    if proxy:
        opts["proxy"] = proxy
    else:
        opts.pop("proxy", None)


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
        # Do not force youtube player_client (e.g. mweb/tv): without PO tokens yt-dlp skips
        # most https formats and the UI can show a single low-res option only.
    }
    _merge_proxy(opts)
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
        self._lock = threading.Lock()
        self._cancelled: set[str] = set()
        self._active_ydl: dict[str, object] = {}
        # Serialize tailscale set --exit-node only (brief); yt-dlp may run concurrently
        self._vpn_run_lock = threading.Lock()

    def _is_cancelled(self, download_id: str) -> bool:
        with self._lock:
            return download_id in self._cancelled

    def _register_ydl(self, download_id: str, ydl: "yt_dlp.YoutubeDL") -> None:
        with self._lock:
            self._active_ydl[download_id] = ydl

    def _unregister_ydl(self, download_id: str) -> None:
        with self._lock:
            self._active_ydl.pop(download_id, None)

    def cancel_download(self, download_id: str) -> None:
        """Signal cancellation: cooperative yt-dlp interrupt, live subprocess SIGINT, or no-op if queued."""
        with self._lock:
            self._cancelled.add(download_id)
            ydl = self._active_ydl.get(download_id)
        if ydl:
            try:
                ydl.close()
            except Exception:
                logger.debug("ydl.close() during cancel", exc_info=True)
        proc = self._active_processes.get(download_id)
        if proc:
            try:
                proc.send_signal(signal.SIGINT)
            except Exception:
                logger.debug("SIGINT to live download during cancel", exc_info=True)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def set_broadcast(self, broadcast_fn):
        self._broadcast = broadcast_fn

    def _emit(self, event: str, data: dict):
        if self._broadcast and self._loop:
            self._loop.call_soon_threadsafe(self._broadcast, event, data)

    def _ensure_exit_for_request(self, exit_node_override: str | None) -> None:
        """Switch Tailscale exit node only when a per-download override is specified.

        Otherwise, respect the user's current routing choice (direct or exit node).
        """
        if not config_use_vpn():
            return
        ov = _normalized_exit_override(exit_node_override)
        if not ov:
            return
        with self._vpn_run_lock:
            set_exit_node_sync(ov)

    def fetch_formats(self, url: str, exit_node: str | None = None) -> VideoInfo:
        return self._fetch_formats_inner(url, exit_node)

    def _fetch_formats_inner(self, url: str, exit_node: str | None = None) -> VideoInfo:
        self._ensure_exit_for_request(exit_node)
        opts = {
            "quiet": True,
            "no_warnings": True,
            "user_agent": USER_AGENT,
            "js_runtimes": {"node": {}},
        }
        _merge_proxy(opts)
        if _cookies_file_valid():
            opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        raw_formats = info.get("formats", [])
        duration = info.get("duration")
        formats = []
        for f in raw_formats:
            has_video = f.get("vcodec", "none") not in ("none", None)
            has_audio = f.get("acodec", "none") not in ("none", None)
            _fid = f.get("format_id")
            # Do not use `or ""` — numeric id 0 is valid; falsy or would drop it and collapse the UI list
            format_id_str = "" if _fid is None else str(_fid)
            est_total = None
            if has_video and not has_audio:
                est_total = _estimated_merged_size_bytes(raw_formats, f, duration)
            formats.append(FormatInfo(
                format_id=format_id_str,
                ext=f.get("ext", ""),
                resolution=f.get("resolution"),
                fps=f.get("fps"),
                vcodec=f.get("vcodec"),
                acodec=f.get("acodec"),
                abr=f.get("abr"),
                filesize=f.get("filesize"),
                filesize_approx=f.get("filesize_approx"),
                estimated_total_bytes=est_total,
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

    async def start_download(
        self,
        url: str,
        format_id: str | None = None,
        category_id: str | None = None,
        exit_node: str | None = None,
        quality_label: str | None = None,
    ) -> str:
        download_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        ql = (quality_label or "").strip() or None
        fid = str(format_id).strip() if format_id else None

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
            "format_id": fid,
            "quality_label": ql,
            "error_message": None,
            "thumbnail_url": None,
            "created_at": now,
            "updated_at": now,
            "category_id": category_id,
            "is_live": 0,
            "duration": None,
            "exit_node": exit_node,
        })

        self._emit("download_queued", {
            "id": download_id,
            "url": url,
            "status": "queued",
            "quality_label": ql,
            "format_id": fid,
        })
        self._pool.submit(self._run_download, download_id, url, fid)
        return download_id

    def _run_download(self, download_id: str, url: str, format_id: str | None):
        try:
            if self._is_cancelled(download_id):
                return
            self._do_download(download_id, url, format_id)
        except DownloadError as e:
            if self._is_cancelled(download_id) or str(e).strip() == "Cancelled":
                logger.info("Download %s cancelled", download_id)
                return
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
        except Exception as e:
            if self._is_cancelled(download_id):
                logger.info("Download %s cancelled", download_id)
                return
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
        finally:
            with self._lock:
                self._cancelled.discard(download_id)
            self._unregister_ydl(download_id)

    def _ensure_exit_from_row(self, download_id: str) -> None:
        row = self._sync_get(download_id)
        override = (row or {}).get("exit_node")
        self._ensure_exit_for_request(override if override else None)

    def _do_download(self, download_id: str, url: str, format_id: str | None):
        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        if self._is_cancelled(download_id):
            return

        self._ensure_exit_from_row(download_id)

        if self._is_cancelled(download_id):
            return

        # Fetch info first
        self._sync_update(download_id, {"status": "fetching_info", "updated_at": now_iso()})

        extract_opts = {
            "quiet": True,
            "no_warnings": True,
            "user_agent": USER_AGENT,
            "js_runtimes": {"node": {}},
        }
        _merge_proxy(extract_opts)
        if _cookies_file_valid():
            extract_opts["cookiefile"] = COOKIES_FILE

        with yt_dlp.YoutubeDL(extract_opts) as ydl:
            self._register_ydl(download_id, ydl)
            try:
                if self._is_cancelled(download_id):
                    return
                info = ydl.extract_info(url, download=False)
            finally:
                self._unregister_ydl(download_id)

        if self._is_cancelled(download_id):
            return

        title = info.get("title") or f"vid{_next_vid_number()}"
        thumbnail_url = info.get("thumbnail")
        is_live = bool(info.get("is_live"))

        # Determine format string
        format_str = None
        quality_label = None
        if format_id:
            all_formats = info.get("formats", [])
            selected = next((f for f in all_formats if _format_row_matches_id(f, format_id)), None)
            if selected:
                has_video = selected.get("vcodec", "none") not in ("none", None)
                has_audio = selected.get("acodec", "none") not in ("none", None)
                quality_label = _build_quality_label(selected)

                if has_video and not has_audio:
                    audio = _find_best_audio(all_formats)
                    if audio:
                        format_str = f"{format_id}+{str(audio.get('format_id'))}"
                    else:
                        format_str = format_id
                else:
                    format_str = format_id
            else:
                logger.warning(
                    "No format matching id %r for download %s; falling back to default format",
                    format_id,
                    download_id,
                )

        duration = info.get("duration")
        size_est = _initial_filesize_estimate(info, format_id, format_str)

        row_pre = self._sync_get(download_id) or {}
        if not quality_label:
            quality_label = row_pre.get("quality_label")

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
        if size_est:
            update_fields["filesize"] = size_est

        self._sync_update(download_id, update_fields)

        emit_start = {
            "id": download_id,
            "title": title,
            "thumbnail_url": thumbnail_url,
            "progress": 0.0,
            "status": "downloading",
            "is_live": is_live,
        }
        if size_est:
            emit_start["filesize"] = size_est
        self._emit("download_progress", emit_start)

        if is_live:
            self._do_live_download(download_id, url, title, format_str, thumbnail_url)
        else:
            self._do_regular_download(
                download_id, url, title, format_str, thumbnail_url, initial_filesize=size_est,
            )

    def _do_regular_download(
        self,
        download_id: str,
        url: str,
        title: str,
        format_str: str | None,
        thumbnail_url: str | None,
        initial_filesize: int | None = None,
    ):
        now_iso = lambda: datetime.now(timezone.utc).isoformat()

        # Use download_id (UUID) as filename
        output_filename = f"{download_id}.%(ext)s"

        # yt-dlp often reports no total during fragments/merges; avoid wiping DB/UI with nulls.
        best_total: list[int | None] = [initial_filesize]
        peak_progress: list[float] = [0.0]
        ema_bps: list[float | None] = [None]
        ema_eta: list[float | None] = [None]
        # Video+audio (DASH): yt-dlp downloads streams sequentially and resets counters between
        # them — accumulate so the bar does not snap back to 0 and totals stay coherent.
        cum_bytes: list[int] = [0]
        phase_peak: list[int] = [0]
        last_raw_dl: list[int] = [0]

        # Progress hook
        def progress_hook(d):
            if self._is_cancelled(download_id):
                raise DownloadError("Cancelled")
            if d.get("status") == "downloading":
                raw_dl = int(d.get("downloaded_bytes") or 0)
                raw_total = d.get("total_bytes") or d.get("total_bytes_estimate")

                prev_dl = last_raw_dl[0]
                if prev_dl > 256 * 1024 and raw_dl < prev_dl // 3:
                    cum_bytes[0] += phase_peak[0]
                    phase_peak[0] = 0

                last_raw_dl[0] = raw_dl
                phase_peak[0] = max(phase_peak[0], raw_dl)
                downloaded = cum_bytes[0] + raw_dl

                if raw_total:
                    try:
                        rt = int(raw_total)
                        bt = best_total[0]
                        phase_dl = raw_dl
                        combined_ceil = cum_bytes[0] + rt
                        if bt is None or bt <= 0:
                            best_total[0] = combined_ceil
                        elif combined_ceil > bt:
                            best_total[0] = combined_ceil
                        elif (
                            cum_bytes[0] == 0
                            and bt > max(rt, phase_dl, 1) * 4
                            and phase_dl > 0
                            and rt > 0
                            and phase_dl >= rt * 0.88
                        ):
                            # Inflated metadata (e.g. wrong top-level approx); trust hook + bytes
                            best_total[0] = max(rt, phase_dl, downloaded)
                        else:
                            best_total[0] = max(bt, combined_ceil)
                    except (TypeError, ValueError):
                        pass
                total = best_total[0]

                if total and total > 0:
                    pct = min(99.9, 100.0 * downloaded / total)
                    peak_progress[0] = max(peak_progress[0], pct)
                else:
                    pct = peak_progress[0]

                bps = d.get("speed")
                if bps is not None:
                    try:
                        bf = float(bps)
                        if bf > 0:
                            prev = ema_bps[0]
                            ema_bps[0] = bf if prev is None else (0.28 * bf + 0.72 * prev)
                    except (TypeError, ValueError):
                        pass
                speed = _format_speed_bps(ema_bps[0]) if ema_bps[0] else ""
                if not speed:
                    speed = _strip_ansi(d.get("_speed_str", "")).strip()

                eta_sec = d.get("eta")
                eta = ""
                if eta_sec is not None:
                    try:
                        es = float(eta_sec)
                        if es >= 0:
                            prev_e = ema_eta[0]
                            ema_eta[0] = es if prev_e is None else (0.22 * es + 0.78 * prev_e)
                            eta = _format_eta_seconds(ema_eta[0])
                    except (TypeError, ValueError):
                        pass
                if not eta:
                    eta = _strip_ansi(d.get("_eta_str", "")).strip()

                fields: dict = {
                    "progress": round(pct, 1),
                    "downloaded_bytes": downloaded,
                    "status": "downloading",
                    "updated_at": now_iso(),
                }
                if speed:
                    fields["speed"] = speed
                if eta:
                    fields["eta"] = eta
                if total is not None and total > 0:
                    fields["filesize"] = total

                self._sync_update(download_id, fields)
                ev = {"id": download_id, **{k: v for k, v in fields.items() if k != "updated_at"}}
                self._emit("download_progress", ev)
            # Ignore per-stream "finished" from yt-dlp (video then audio); it is not job done.

        # Postprocessor hook
        def postprocessor_hook(d):
            if self._is_cancelled(download_id):
                raise DownloadError("Cancelled")
            st = d.get("status")
            pp = d.get("postprocessor") or ""
            if st == "started" and pp == "Merger":
                merge_pct = min(99.5, max(peak_progress[0], 99.0))
                self._sync_update(download_id, {
                    "status": "post_processing",
                    "progress": merge_pct,
                    "speed": None,
                    "eta": None,
                    "updated_at": now_iso(),
                })
                self._emit("download_progress", {
                    "id": download_id,
                    "status": "post_processing",
                    "progress": merge_pct,
                    "speed": None,
                    "eta": None,
                })
            if st == "finished":
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
            # Exact selection only — `/bestvideo+bestaudio` was upgrading every job to the same "best" stream
            dl_opts["format"] = format_str
            dl_opts.pop("format_sort", None)
        else:
            dl_opts["format"] = "bestvideo+bestaudio/best"

        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            self._register_ydl(download_id, ydl)
            try:
                if self._is_cancelled(download_id):
                    return
                ydl.download([url])
            finally:
                self._unregister_ydl(download_id)

        if self._is_cancelled(download_id):
            return

        # Final — get the actual filename (should be UUID.ext)
        row = self._sync_get(download_id)
        filename = row.get("filename") if row else None
        if not filename:
            filename = f"{download_id}.mp4"

        filepath = os.path.join(DOWNLOADS_DIR, filename)

        if self._is_cancelled(download_id):
            return

        # Re-encode to H.264/AAC for iOS compatibility if needed
        if os.path.isfile(filepath) and filepath.endswith(".mp4"):
            self._ensure_ios_compatible(download_id, filepath)

        if self._is_cancelled(download_id):
            return

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

        if self._is_cancelled(download_id):
            return

        # Pre-cache thumbnail if available
        if thumbnail_url:
            self._cache_thumbnail(download_id, thumbnail_url)

        if self._is_cancelled(download_id):
            return

        # Use download_id (UUID) as filename
        output_path = os.path.join(DOWNLOADS_DIR, f"{download_id}.%(ext)s")
        cmd = ["yt-dlp", "--newline", "--no-colors", "-o", output_path]
        cmd.extend(["--user-agent", USER_AGENT])
        cmd.extend(["--merge-output-format", "mp4"])
        cmd.extend(["--extractor-args", "youtube:player_client=mweb,tv"])
        proxy = _vpn_proxy_url()
        if proxy:
            cmd.extend(["--proxy", proxy])

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
                if self._is_cancelled(download_id):
                    try:
                        proc.send_signal(signal.SIGINT)
                    except Exception:
                        pass
                    break
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

        if self._is_cancelled(download_id):
            return

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

        if self._is_cancelled(download_id):
            return

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
            if self._is_cancelled(download_id):
                return
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
