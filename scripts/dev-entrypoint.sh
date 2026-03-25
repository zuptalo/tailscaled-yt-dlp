#!/bin/bash
set -e

echo "=== Development mode (with VPN) ==="

# --- Start Tailscale daemon (same as production) ---
echo "Starting tailscaled..."
/usr/sbin/tailscaled --state=/var/lib/tailscale/tailscaled.state --tun=userspace-networking --socks5-server=localhost:1055 &

# Wait for tailscaled socket (up to 30s)
echo "Waiting for tailscaled to be ready..."
attempts=0
max_attempts=30
until [ -S /var/run/tailscale/tailscaled.sock ]; do
  attempts=$((attempts + 1))
  if [ "$attempts" -ge "$max_attempts" ]; then
    echo "ERROR: tailscaled did not become ready within ${max_attempts}s"
    exit 1
  fi
  sleep 1
done
echo "tailscaled is ready."

# --- Cookies check (informational) ---
COOKIES_FILE="${COOKIES_FILE:-/data/cookies.txt}"
if [ -f "$COOKIES_FILE" ]; then
  echo "Cookies file found at $COOKIES_FILE."
else
  echo "No cookies.txt found at $COOKIES_FILE. Some downloads may require it."
fi

# --- User agent ---
USER_AGENT="${USER_AGENT:-Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36}"

# --- Create CLI wrapper (backward compat) ---
cat > /usr/local/bin/y << WRAPPER
#!/bin/bash
exec yt-dlp \\
  --cookies "$COOKIES_FILE" \\
  --user-agent "$USER_AGENT" \\
  --js-runtimes node \\
  --proxy "socks5://localhost:1055" \\
  "\$@"
WRAPPER
chmod +x /usr/local/bin/y
echo "CLI wrapper 'y' installed."

# --- Launch web UI with live reload ---
echo "Starting dev server on port 8080 (uvicorn --reload)..."
echo "Source code is mounted; changes are picked up automatically."
exec uvicorn app.main:app \
  --host 0.0.0.0 \
  --port 8080 \
  --reload \
  --reload-dir /app \
  --reload-dir /static \
  --app-dir /
