FROM ghcr.io/jauderho/yt-dlp:latest

# Install Tailscale and bash
RUN apk add --no-cache tailscale iptables bash

# Create downloads directory
RUN mkdir -p /downloads

# Set working directory
WORKDIR /downloads

# Wrapper entrypoint script
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
