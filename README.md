# Media Downloader

Containerized yt-dlp media downloader that can route traffic through a Tailscale VPN exit node (optional). Features a web UI with authentication, first-run setup wizard, real-time download progress, video streaming/playback, and VPN health monitoring when VPN is enabled.

Designed for use with Headscale (self-hosted Tailscale control plane). Published as multi-arch Docker images (amd64/arm64).

## Features

- **Web UI**: Mobile-first PWA with dark theme, real-time progress via SSE
- **VPN Routing (optional)**: When enabled, downloads use a Tailscale exit node via SOCKS5 (`localhost:1055`); you can skip VPN and use direct egress instead
- **Setup Wizard**: Configure credentials, then either connect to Headscale and pick an exit node, or skip VPN for a direct connection
- **Video Playback**: Stream downloaded videos directly in the browser
- **Format Selection**: Choose quality/format before downloading
- **Multi-arch**: Works on AMD64 and ARM64 (Apple Silicon)

## Quick Start

```bash
# Using Docker Hub
docker pull zuptalo/tailscaled-yt-dlp:latest

# Or using GHCR
docker pull ghcr.io/zuptalo/tailscaled-yt-dlp:latest

# Run with compose
docker compose up -d

# Open web UI — first run shows setup wizard
open http://localhost:8080
```

## Docker Compose

```yaml
services:
  tailscaled-yt-dlp:
    image: zuptalo/tailscaled-yt-dlp:latest
    container_name: tailscaled-yt-dlp
    hostname: tailscaled-yt-dlp
    restart: always
    ports:
      - "8080:8080"
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun
    volumes:
      - ./downloads:/downloads
      - ./data:/data
      - ./tailscale-state:/var/lib/tailscale
```

## Setup

On first run, the web UI displays a setup wizard:

1. **Credentials** — Choose username and password for the web UI
2. **Headscale** — Enter your Headscale server URL and pre-authorized auth key, then connect; **or** use **Skip VPN (direct connection)** to finish without Tailscale
3. **Exit node** — If you connected to Headscale, select from discovered exit nodes

You can change **Use VPN for downloads** later under Settings → General. Configuration persists across container restarts.

## Directory Structure

```
./downloads/     # Downloaded media files
./data/          # Config, database, cookies
./tailscale-state/  # Tailscale identity (persists across restarts)
```

## CLI Usage

```bash
# Shell into running container
docker exec -it tailscaled-yt-dlp bash

# Download a video (after setup wizard is complete)
y "https://youtube.com/watch?v=..."

# The 'y' wrapper automatically applies cookies, user-agent, and VPN proxy
```

## Configuration

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATA_DIR` | `/data` | Application data directory |
| `DOWNLOADS_DIR` | `/downloads` | Download output directory |
| `COOKIES_FILE` | `/data/cookies.txt` | Path to cookies file |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Max parallel downloads |
| `USER_AGENT` | Chrome 140 | Browser user-agent string |

### Cookies

For sites requiring authentication, provide a single **Netscape-format** `cookies.txt` (one file can list multiple domains). You can upload or clear it from **Settings → General**, or place it on disk at `/data/cookies.txt` (or the path in `COOKIES_FILE`):

```bash
# Copy cookies into the data volume
cp cookies.txt ./data/cookies.txt
```

Export cookies from your browser using extensions like "Get cookies.txt LOCALLY".

### VPN modes and exit nodes

- **VPN off**: yt-dlp runs without the SOCKS proxy; traffic uses the container’s normal network path.
- **VPN on**: Traffic goes through Tailscale’s SOCKS proxy to the **currently active** exit node. Tailscale only allows **one active exit at a time**, so the app **serializes** work that switches exits (format fetch and downloads share a lock). Per-download exit selection changes the global exit before that job runs; do not expect different exits for concurrent jobs.

## Architecture

The container combines yt-dlp + Tailscale + FastAPI in a single Alpine-based image:

1. `tailscaled` runs with userspace networking and SOCKS5 proxy on `localhost:1055`
2. All yt-dlp traffic routes through the SOCKS5 proxy to the exit node
3. FastAPI serves the web UI on port 8080
4. SQLite database tracks download history

For a diagram, component breakdown, and feature-level map of the web app, see **[ARCHITECTURE.md](ARCHITECTURE.md)**.

### API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Serve web UI |
| GET | `/api/setup/status` | No | Check if setup is complete |
| POST | `/api/setup/connect` | No | Connect to Headscale |
| POST | `/api/setup/complete` | No | Save config, get auth token |
| POST | `/api/auth/login` | No | Authenticate |
| GET | `/api/auth/validate` | Yes | Validate Bearer or query token |
| POST | `/api/auth/logout` | Yes | Revoke session token |
| GET | `/api/formats?url=` | Yes | Extract available formats (optional `exit_node` when using VPN) |
| POST | `/api/downloads` | Yes | Start download (optional `exit_node` in JSON body) |
| GET | `/api/downloads` | Yes | List downloads |
| PATCH | `/api/downloads/{id}` | Yes | Update download (e.g. category) |
| DELETE | `/api/downloads/{id}` | Yes | Delete download |
| POST | `/api/downloads/{id}/retry` | Yes | Retry failed download |
| POST | `/api/downloads/{id}/stop` | Yes | Stop live stream download |
| GET | `/api/downloads/{id}/file` | Yes | Download file (Bearer or `?token=`) |
| GET | `/api/downloads/{id}/stream` | Yes | Stream media (Bearer or `?token=`) |
| GET | `/api/downloads/{id}/thumbnail` | Yes | Cached thumbnail |
| GET | `/api/proxy-thumbnail?url=` | Yes | Proxy thumbnail (CORS) |
| POST | `/api/downloads/{id}/share` | Yes | Create share link |
| GET | `/api/downloads/{id}/shares` | Yes | List share links |
| DELETE | `/api/shares/{id}` | Yes | Delete share link |
| GET | `/s/{token}` | No | Public share page (HTML) |
| GET | `/api/events?token=` | Yes | SSE event stream |
| GET | `/api/categories` | Yes | List categories |
| POST | `/api/categories` | Yes | Create category |
| PUT | `/api/categories/{id}` | Yes | Rename category |
| DELETE | `/api/categories/{id}` | Yes | Delete category |
| GET | `/api/settings` | Yes | Get settings (`public_url`, `use_vpn`) |
| PUT | `/api/settings` | Yes | Update settings (`public_url`, `use_vpn`) |
| GET | `/api/settings/cookies` | Yes | Cookies file presence and size |
| POST | `/api/settings/cookies` | Yes | Upload Netscape `cookies.txt` (`multipart/form-data`, field `file`) |
| DELETE | `/api/settings/cookies` | Yes | Remove cookies file |
| PUT | `/api/settings/credentials` | Yes | Change username/password |
| GET | `/api/settings/vpn` | Yes | VPN settings + exit node list |
| PUT | `/api/settings/vpn` | Yes | Update VPN (reconnect) |
| PUT | `/api/vpn/exit-node` | Yes | Change exit node only |
| POST | `/api/vpn/disconnect` | Yes | Disconnect Tailscale |
| GET | `/api/vpn/ip` | Yes | External IP via SOCKS5 |
| GET | `/api/vpn/status` | Yes | VPN status |
| GET | `/api/health` | No | Health check |

## Building Locally

### Makefile (unified with CI checks)

| Target | Purpose |
|--------|---------|
| `make help` | List common targets |
| `make install` | Create `.venv` and install `requirements.txt` |
| `make dev` | Run FastAPI with reload (`DATA_DIR`/`DOWNLOADS_DIR` under `./data`, `./downloads`) |
| `make check` | `python3 -m compileall` on `app/` (syntax only; same as CI) |
| `make test` | Alias for `make check` until a test suite exists |
| `make check-ytdlp` | Compare PyPI yt-dlp with `yt-dlp-version.txt` (updates file if drift; uses `scripts/check-ytdlp-version.sh`) |
| `make check-ytdlp-dry` | Same comparison without writing `yt-dlp-version.txt` |
| `make print-build-info` | Print date tag, UTC time, short git SHA (mirrors CI “build info” vars) |
| `make docker-build` | `docker build -t tailscaled-yt-dlp:local .` (matches `compose.yaml` `image:`) |
| `make docker-up` / `make docker-down` | `docker compose up -d` / `down` |
| `make docker-rebuild` | Rebuild image and recreate containers |
| `make docker-buildx` | Buildx for `linux/amd64` with `--load` (local smoke; CI builds amd64+arm64 and pushes) |
| `make build` | Same as `make docker-rebuild` (legacy) |

Requires **curl** and **jq** for `check-ytdlp` scripts.

### Docker Compose

```bash
make docker-build
make docker-up
# or: make docker-rebuild
```

### Local Development (no Docker)

```bash
make dev
```

Requires Python 3.11+ and ffmpeg (`make check-deps` prints a note if ffmpeg is missing).

### CI vs local

[`.github/workflows/build.yml`](.github/workflows/build.yml) runs the same yt-dlp check as [`scripts/check-ytdlp-version.sh`](scripts/check-ytdlp-version.sh), then `make check`, then multi-arch Docker Buildx with registry login, `docker/metadata-action` tags, and GHA cache—those pieces stay in Actions. Pushing images and creating releases still require GitHub secrets and are not duplicated in the Makefile.

## Troubleshooting

### Container won't start
- Ensure `/dev/net/tun` exists on your host
- Verify `NET_ADMIN` capability is available

### Tailscale connection fails
- Verify Headscale URL is correct and accessible
- Check that auth key is valid and not expired/already used
- Generate a new pre-authorized key if needed

### Downloads fail
- Check that cookies.txt is present and valid (cookies expire)
- Verify the video URL is accessible from your exit node location
- Check container logs: `docker compose logs -f`

### VPN not routing traffic
- The external IP shown in the header should match your exit node's IP
- If showing your real IP, check that the exit node is online and selected

## Security Notes

- Cookies contain authentication tokens — keep them secure
- Config file is stored with mode 0600
- Session tokens are stored under `/data` and survive container restarts; revoke via logout or delete the tokens file if needed
- The container requires `NET_ADMIN` capability for VPN functionality

## License

This project combines:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Public domain
- [Tailscale](https://tailscale.com/) — BSD-3-Clause license
