# Tailscaled yt-dlp

A containerized YouTube/video downloader (yt-dlp) that routes all traffic through a Tailscale VPN connection. Perfect for downloading geo-restricted content or maintaining privacy by routing through a specific exit node.

## Features

- **yt-dlp with Tailscale VPN**: All downloads route through your Tailscale network
- **Persistent container**: Long-running container you can connect to and run commands
- **Automatic cookie/user-agent**: Cookies and Chrome user-agent automatically applied
- **Persistent state**: Tailscale identity persists across container restarts
- **Multi-arch support**: Works on AMD64 and ARM64 (Apple Silicon)

## Prerequisites

### Required Files
1. **`cookies.txt`** - Browser cookies for authentication with video platforms
   - Export from your browser using extensions like "Get cookies.txt LOCALLY"
   - Must be in Netscape cookie file format
   - Place in the project root directory

2. **Tailscale/Headscale setup**:
   - A running Headscale server (or Tailscale account)
   - An authentication key for your network
   - An exit node configured in your network

### Required Environment Variables
Set these in your `docker-compose.yaml`:

- `HEADSCALE_URL` - Your Headscale server URL (e.g., `https://hs.example.com`)
- `HEADSCALE_AUTHKEY` - Authentication key for your Headscale network
- `EXIT_NODE` - IP address of the exit node to route traffic through (e.g., `100.64.0.12`)

## Quick Start

1. **Clone and configure**:
   ```bash
   git clone <this-repo>
   cd tailscaled-yt-dlp
   
   # Add your cookies.txt file
   cp /path/to/your/cookies.txt ./cookies.txt
   
   # Update docker-compose.yaml with your Headscale details
   ```

2. **Start the container**:
   ```bash
   docker-compose up -d
   ```

3. **Connect to the container**:
   ```bash
   docker exec -it tailscaled-yt-dlp bash
   ```

4. **Download videos**:
   ```bash
   # Simple download
   y "https://youtube.com/watch?v=..."
   
   # With options
   y "https://youtube.com/watch?v=..." --format best --output "%(title)s.%(ext)s"
   
   # Download playlist
   y "https://youtube.com/playlist?list=..." --format best
   ```

## Configuration

### docker-compose.yaml

```yaml
services:
  tailscaled-yt-dlp:
    image: zuptalo/tailscaled-yt-dlp:latest
    container_name: tailscaled-yt-dlp
    hostname: tailscaled-yt-dlp
    restart: always
    cap_add:
      - NET_ADMIN
    devices:
      - /dev/net/tun
    volumes:
      - ./downloads:/downloads
      - ./cookies.txt:/downloads/cookies.txt
      - ./tailscale-state:/var/lib/tailscale
    environment:
      - HEADSCALE_URL=https://hs.example.com
      - HEADSCALE_AUTHKEY=your-auth-key-here
      - EXIT_NODE=100.64.0.12
    tty: true
    stdin_open: true
```

### Getting Cookies

1. **Browser Extension Method**:
   - Install "Get cookies.txt LOCALLY" extension
   - Visit the video platform you want to download from
   - Click the extension and export cookies
   - Save as `cookies.txt` in the project directory

2. **Manual Browser Method**:
   - Open Developer Tools (F12)
   - Go to Application/Storage tab
   - Copy cookies manually to Netscape format

## Usage

### Basic Commands

```bash
# Connect to running container
docker exec -it tailscaled-yt-dlp bash

# Download single video
y "https://youtube.com/watch?v=dQw4w9WgXcQ"

# Download with specific format
y "https://youtube.com/watch?v=dQw4w9WgXcQ" --format "best[height<=720]"

# Download audio only
y "https://youtube.com/watch?v=dQw4w9WgXcQ" --extract-audio --audio-format mp3

# Download playlist
y "https://youtube.com/playlist?list=PLrAXtmRdnEQy6nuLMt3JXXqiTFuqIgMmp"

# List available formats
y "https://youtube.com/watch?v=dQw4w9WgXcQ" --list-formats
```

### Directory Structure

```
tailscaled-yt-dlp/
├── downloads/           # Downloaded files appear here
├── tailscale-state/     # Persistent Tailscale state
├── cookies.txt          # Your browser cookies
├── docker-compose.yaml  # Container configuration
└── README.md           # This file
```

## Troubleshooting

### Container won't start
- Ensure `/dev/net/tun` exists on your host
- Check that your user can access TUN/TAP devices
- Verify `NET_ADMIN` capability is available

### Tailscale connection fails
- Verify `HEADSCALE_URL` is correct and accessible
- Check that `HEADSCALE_AUTHKEY` is valid and not expired
- Ensure the `EXIT_NODE` IP is correct and online

### Downloads fail
- Check that `cookies.txt` is present and valid
- Try updating your cookies (they expire)
- Verify the video URL is accessible from your exit node location

### Permission issues
```bash
# Fix downloads directory permissions
sudo chown -R $USER:$USER downloads/
```

## Security Notes

- Cookies contain authentication tokens - keep `cookies.txt` secure
- Auth keys in environment variables are visible in container inspect
- Consider using Docker secrets for production deployments
- The container runs with `NET_ADMIN` capabilities for VPN functionality

## Building Locally

```bash
# Build the image
docker build -t tailscaled-yt-dlp .

# Update docker-compose.yaml to use local image
# Change: image: zuptalo/tailscaled-yt-dlp:latest
# To: build: .
```

## License

This project combines:
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) - Public domain
- [Tailscale](https://tailscale.com/) - BSD-3-Clause license

## Support

For issues related to:
- **yt-dlp functionality**: Check [yt-dlp documentation](https://github.com/yt-dlp/yt-dlp)
- **Tailscale/Headscale**: Check [Tailscale docs](https://tailscale.com/kb/) or [Headscale docs](https://headscale.net/)
- **This container**: Open an issue in this repository