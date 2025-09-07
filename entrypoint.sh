#!/bin/bash
set -e

# Check required vars
if [ -z "$HEADSCALE_AUTHKEY" ]; then
  echo "❌ Missing HEADSCALE_AUTHKEY env var"
  exit 1
fi
if [ -z "$EXIT_NODE" ]; then
  echo "❌ Missing EXIT_NODE env var"
  exit 1
fi

# Start Tailscale daemon
echo "🔌 Starting Tailscale..."
/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=userspace-networking &

sleep 2

# Connect to Headscale
tailscale up \
  --login-server "$HEADSCALE_URL" \
  --auth-key "$HEADSCALE_AUTHKEY" \
  --exit-node "$EXIT_NODE" \
  --exit-node-allow-lan-access=false \
  --accept-routes

echo "✅ Connected to Headscale, using exit node: $EXIT_NODE"

# Cookies check
if [ ! -f "/downloads/cookies.txt" ]; then
  echo "❌ No cookies.txt found in /downloads"
  exit 1
fi

echo "🚀 Container ready! You can now run yt-dlp commands."
echo "Usage: y [URL] [options]"
echo "Cookies and user-agent are automatically configured."

# Create yt-dlp wrapper function that automatically includes cookies and user-agent
cat > /usr/local/bin/y << 'EOF'
#!/bin/bash
exec /usr/local/bin/yt-dlp \
  --cookies /downloads/cookies.txt \
  --user-agent "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36" \
  "$@"
EOF
chmod +x /usr/local/bin/y

# Keep container running with bash shell
exec /bin/bash
