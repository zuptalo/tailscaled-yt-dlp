import hashlib
import json
import os
import secrets
import time

from fastapi import Header, HTTPException, Request

from app.config import CONFIG_FILE, TOKENS_FILE

_active_tokens: set[str] = set()

# Share access tokens: token -> expiry timestamp
_share_access_tokens: dict[str, float] = {}


# --- Token persistence ---

def _load_tokens():
    """Load tokens from disk on startup."""
    global _active_tokens
    if os.path.isfile(TOKENS_FILE):
        try:
            with open(TOKENS_FILE) as f:
                data = json.load(f)
                _active_tokens = set(data.get("tokens", []))
        except (json.JSONDecodeError, IOError):
            _active_tokens = set()


def _save_tokens():
    """Persist tokens to disk."""
    try:
        os.makedirs(os.path.dirname(TOKENS_FILE), exist_ok=True)
        with open(TOKENS_FILE, "w") as f:
            json.dump({"tokens": list(_active_tokens)}, f)
        os.chmod(TOKENS_FILE, 0o600)
    except IOError:
        pass


# Load tokens on module import
_load_tokens()


# --- Password hashing ---

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(32)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return hashed, salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    computed, _ = hash_password(password, salt)
    return secrets.compare_digest(computed, password_hash)


# --- Token management ---

def create_token() -> str:
    token = secrets.token_urlsafe(32)
    _active_tokens.add(token)
    _save_tokens()
    return token


def validate_token(token: str) -> bool:
    return token in _active_tokens


def revoke_token(token: str):
    _active_tokens.discard(token)
    _save_tokens()


# --- Share access tokens ---

def create_share_access_token(ttl: int = 300) -> str:
    token = secrets.token_urlsafe(32)
    _share_access_tokens[token] = time.time() + ttl
    return token


def validate_share_access_token(token: str) -> bool:
    expiry = _share_access_tokens.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        _share_access_tokens.pop(token, None)
        return False
    return True


# --- Config file I/O ---

def load_config() -> dict | None:
    if not os.path.isfile(CONFIG_FILE):
        return None
    with open(CONFIG_FILE) as f:
        return json.load(f)


def save_config(config: dict):
    existing = load_config() or {}
    existing.update(config)
    with open(CONFIG_FILE, "w") as f:
        json.dump(existing, f, indent=2)
    os.chmod(CONFIG_FILE, 0o600)


def is_setup_complete() -> bool:
    config = load_config()
    return config is not None and "username" in config


# --- FastAPI auth dependency ---

async def require_auth(request: Request, authorization: str | None = Header(None)):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not token:
        token = request.query_params.get("token")
    if not token or not validate_token(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
