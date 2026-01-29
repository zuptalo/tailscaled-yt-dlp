from pydantic import BaseModel


class FormatInfo(BaseModel):
    format_id: str
    ext: str
    resolution: str | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    abr: float | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    quality_label: str | None = None
    has_video: bool = False
    has_audio: bool = False


class VideoInfo(BaseModel):
    title: str | None = None
    thumbnail: str | None = None
    duration: float | None = None
    formats: list[FormatInfo] = []


class DownloadRequest(BaseModel):
    url: str
    format_id: str | None = None
    category_id: str | None = None


class DownloadStatus(BaseModel):
    id: str
    url: str
    title: str | None = None
    status: str
    progress: float = 0.0
    speed: str | None = None
    eta: str | None = None
    filesize: int | None = None
    downloaded_bytes: int | None = None
    filename: str | None = None
    format_id: str | None = None
    quality_label: str | None = None
    error_message: str | None = None
    thumbnail_url: str | None = None
    created_at: str
    updated_at: str
    category_id: str | None = None
    is_live: bool = False
    duration: float | None = None


class VPNStatus(BaseModel):
    connected: bool = False
    exit_node: str | None = None
    exit_node_online: bool = False


class CategoryCreate(BaseModel):
    name: str


class CategoryUpdate(BaseModel):
    name: str


class Category(BaseModel):
    id: str
    name: str
    created_at: str


class ShareLinkCreate(BaseModel):
    password: str | None = None
    expires_at: str | None = None


class ShareLinkInfo(BaseModel):
    id: str
    download_id: str
    token: str
    has_password: bool
    expires_at: str | None = None
    created_at: str
    url: str


class DownloadPatch(BaseModel):
    category_id: str | None = None


class SettingsUpdate(BaseModel):
    public_url: str


class CredentialsUpdate(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None


class VPNSettingsUpdate(BaseModel):
    headscale_url: str | None = None
    headscale_authkey: str | None = None
    exit_node: str | None = None
