.PHONY: dev build clean check-deps

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
UVICORN := $(VENV)/bin/uvicorn
PORT := 8080

# --- Local development ---

dev: check-deps $(VENV)/bin/activate
	@mkdir -p data downloads
	@echo "Starting dev server on http://0.0.0.0:$(PORT) ..."
	@( sleep 1 && open http://localhost:$(PORT) ) &
	DATA_DIR=./data DOWNLOADS_DIR=./downloads \
		$(UVICORN) app.main:app --host 0.0.0.0 --port $(PORT) --reload

check-deps:
	@command -v ffmpeg >/dev/null 2>&1 || \
		echo "Note: System ffmpeg not found, using bundled version from imageio-ffmpeg"

$(VENV)/bin/activate: requirements.txt
	@test -d $(VENV) || python3 -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt
	@touch $(VENV)/bin/activate

# --- Docker build ---

build:
	docker build -t tailscaled-yt-dlp .
	docker compose up -d --force-recreate

clean:
	rm -rf $(VENV)
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
