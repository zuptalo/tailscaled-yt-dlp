FROM alpine:3.21

# Install system packages
RUN apk add --no-cache \
    python3 \
    py3-pip \
    ffmpeg \
    nodejs \
    tailscale \
    iptables \
    bash \
    curl

# Install Python dependencies (with build deps for curl_cffi)
COPY requirements.txt /tmp/requirements.txt
RUN apk add --no-cache --virtual .build-deps \
        gcc \
        musl-dev \
        libffi-dev \
        python3-dev \
    && pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt \
    && apk del .build-deps \
    && rm /tmp/requirements.txt

# Create directories
RUN mkdir -p /downloads /data

# Copy application
COPY app/ /app/
COPY static/ /static/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /downloads

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
