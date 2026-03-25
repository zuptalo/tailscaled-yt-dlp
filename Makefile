.PHONY: help install venv dev clean check check-deps check-ytdlp check-ytdlp-dry print-build-info \
	docker-build docker-up docker-down docker-rebuild docker-buildx build test

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PORT := 8080
# Must match compose.yaml image: tag
IMAGE := tailscaled-yt-dlp:local
COMPOSE := docker compose

help:
	@echo "Local Python:  make install | make dev | make check"
	@echo "  (after git pull, run make install to sync requirements.txt)"
	@echo "Docker:        make docker-build | make docker-up | make docker-down | make docker-rebuild"
	@echo "CI parity:     make docker-buildx | make check-ytdlp | make print-build-info"
	@echo "Legacy:        make build  (same as docker-rebuild)"

# --- Local development ---

install: $(VENV)/bin/activate

venv: install

$(VENV)/bin/activate: requirements.txt
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)/bin/activate

dev: check-deps install
	@mkdir -p data downloads
	@echo "Starting dev server on http://0.0.0.0:$(PORT) ..."
	@if [ "$$(uname -s)" = "Darwin" ]; then ( sleep 1 && open "http://localhost:$(PORT)" ) & fi
	DATA_DIR=./data DOWNLOADS_DIR=./downloads \
		$(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) --reload

check-deps:
	@command -v ffmpeg >/dev/null 2>&1 || \
		echo "Note: System ffmpeg not found, using bundled version from imageio-ffmpeg"

check:
	@echo "Syntax check (py_compile) app/*.py"
	@python3 -m compileall -q -f app && echo OK

# No test suite yet; placeholder for CI
test:
	@echo "No tests defined. Run: make check"
	@$(MAKE) check

check-ytdlp:
	@./scripts/check-ytdlp-version.sh

check-ytdlp-dry:
	@./scripts/check-ytdlp-version.sh --dry-run

print-build-info:
	@echo "date_tag=$$(date +%Y%m%d)"
	@echo "build_date=$$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
	@echo "git_sha=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

# --- Docker (tag matches compose.yaml: image) ---

docker-build:
	docker build -t $(IMAGE) .

docker-up:
	$(COMPOSE) up -d

docker-down:
	$(COMPOSE) down

docker-rebuild: docker-build
	$(COMPOSE) up -d --force-recreate

# Single-arch load; CI builds linux/amd64 + linux/arm64 with push (see README)
docker-buildx:
	docker buildx build --platform linux/amd64 -t $(IMAGE) --load .

# Backward-compatible: was docker build + compose up
build: docker-rebuild

clean:
	rm -rf $(VENV)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
