# Media Downloader

Containerized yt-dlp media downloader that routes all traffic through a Tailscale VPN exit node. Features a web UI with authentication, first-run setup wizard, real-time download progress, video streaming/playback, and VPN health monitoring.

Designed for use with Headscale (self-hosted Tailscale control plane). Published as multi-arch Docker images (amd64/arm64).

## Features

- **Web UI**: Mobile-first PWA with dark theme, real-time progress via SSE
- **VPN Routing**: All downloads route through Tailscale exit node via SOCKS5 proxy
- **Setup Wizard**: Configure credentials, Headscale connection, and exit node on first run
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

On first run, the web UI displays a 3-step setup wizard:

1. **Credentials** — Choose username and password for the web UI
2. **Headscale** — Enter your Headscale server URL and pre-authorized auth key
3. **Exit node** — Select from discovered exit nodes on your network

Configuration persists across container restarts.

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

For sites requiring authentication, place a `cookies.txt` file (Netscape format) at `/data/cookies.txt`:

```bash
# Copy cookies into the data volume
cp cookies.txt ./data/cookies.txt
```

Export cookies from your browser using extensions like "Get cookies.txt LOCALLY".

## Architecture

The container combines yt-dlp + Tailscale + FastAPI in a single Alpine-based image:

1. `tailscaled` runs with userspace networking and SOCKS5 proxy on `localhost:1055`
2. All yt-dlp traffic routes through the SOCKS5 proxy to the exit node
3. FastAPI serves the web UI on port 8080
4. SQLite database tracks download history

### API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/` | No | Serve web UI |
| GET | `/api/setup/status` | No | Check if setup is complete |
| POST | `/api/setup/connect` | No | Connect to Headscale |
| POST | `/api/setup/complete` | No | Save config, get auth token |
| POST | `/api/auth/login` | No | Authenticate |
| GET | `/api/formats?url=` | Yes | Extract available formats |
| POST | `/api/downloads` | Yes | Start download |
| GET | `/api/downloads` | Yes | List downloads |
| DELETE | `/api/downloads/{id}` | Yes | Delete download |
| GET | `/api/downloads/{id}/stream` | Yes | Stream video |
| GET | `/api/events?token=` | Yes | SSE event stream |
| GET | `/api/vpn/status` | Yes | VPN status |
| GET | `/api/health` | No | Health check |

## Building Locally

```bash
# Build the image
docker build -t tailscaled-yt-dlp .

# Run with compose
docker compose up -d
```

### Local Development

```bash
# Install dependencies and start dev server
make dev
```

Requires Python 3.11+ and ffmpeg.

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
- Auth tokens are held in memory and invalidated on container restart
- The container requires `NET_ADMIN` capability for VPN functionality

## License

This project combines:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) — Public domain
- [Tailscale](https://tailscale.com/) — BSD-3-Clause license
