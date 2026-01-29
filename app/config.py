import os

DATA_DIR = os.environ.get("DATA_DIR", "/data")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
THUMBNAILS_DIR = os.path.join(DATA_DIR, "thumbnails")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")
TOKENS_FILE = os.path.join(DATA_DIR, "tokens.json")
COOKIES_FILE = os.environ.get("COOKIES_FILE", os.path.join(DATA_DIR, "cookies.txt"))
DB_PATH = os.path.join(DATA_DIR, "downloads.db")
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
)
