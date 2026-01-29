import aiosqlite
from app.config import DB_PATH

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS downloads (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0.0,
    speed TEXT,
    eta TEXT,
    filesize INTEGER,
    downloaded_bytes INTEGER,
    filename TEXT,
    format_id TEXT,
    quality_label TEXT,
    error_message TEXT,
    thumbnail_url TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    category_id TEXT,
    is_live INTEGER NOT NULL DEFAULT 0,
    duration REAL
)
"""

CREATE_CATEGORIES_TABLE = """
CREATE TABLE IF NOT EXISTS categories (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
)
"""

CREATE_SHARE_LINKS_TABLE = """
CREATE TABLE IF NOT EXISTS share_links (
    id TEXT PRIMARY KEY,
    download_id TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    password_hash TEXT,
    password_salt TEXT,
    expires_at TEXT,
    created_at TEXT NOT NULL
)
"""

MIGRATIONS = [
    "ALTER TABLE downloads ADD COLUMN category_id TEXT",
    "ALTER TABLE downloads ADD COLUMN is_live INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE downloads ADD COLUMN duration REAL",
]


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.execute(CREATE_TABLE)
        await db.execute(CREATE_CATEGORIES_TABLE)
        await db.execute(CREATE_SHARE_LINKS_TABLE)
        # Run migrations for existing databases
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # Column already exists
        await db.commit()
    finally:
        await db.close()


# --- Downloads CRUD ---

async def insert_download(row: dict):
    db = await get_db()
    try:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        await db.execute(
            f"INSERT INTO downloads ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        await db.commit()
    finally:
        await db.close()


async def update_download(download_id: str, fields: dict):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [download_id]
        await db.execute(f"UPDATE downloads SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def get_download(download_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM downloads WHERE id = ?", [download_id])
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def list_downloads() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM downloads ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def delete_download(download_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM downloads WHERE id = ?", [download_id])
        await db.commit()
    finally:
        await db.close()


# --- Categories CRUD ---

async def list_categories() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories ORDER BY name")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_category(category_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM categories WHERE id = ?", [category_id])
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def insert_category(row: dict):
    db = await get_db()
    try:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        await db.execute(
            f"INSERT INTO categories ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        await db.commit()
    finally:
        await db.close()


async def update_category(category_id: str, fields: dict):
    db = await get_db()
    try:
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [category_id]
        await db.execute(f"UPDATE categories SET {sets} WHERE id = ?", vals)
        await db.commit()
    finally:
        await db.close()


async def delete_category(category_id: str):
    db = await get_db()
    try:
        # Nullify category_id on downloads that reference this category
        await db.execute("UPDATE downloads SET category_id = NULL WHERE category_id = ?", [category_id])
        await db.execute("DELETE FROM categories WHERE id = ?", [category_id])
        await db.commit()
    finally:
        await db.close()


# --- Share Links CRUD ---

async def insert_share_link(row: dict):
    db = await get_db()
    try:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(["?"] * len(row))
        await db.execute(
            f"INSERT INTO share_links ({cols}) VALUES ({placeholders})",
            list(row.values()),
        )
        await db.commit()
    finally:
        await db.close()


async def list_share_links(download_id: str) -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM share_links WHERE download_id = ? ORDER BY created_at DESC",
            [download_id],
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_share_link_by_token(token: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM share_links WHERE token = ?", [token])
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def get_share_link(link_id: str) -> dict | None:
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM share_links WHERE id = ?", [link_id])
        row = await cursor.fetchone()
        return dict(row) if row else None
    finally:
        await db.close()


async def delete_share_link(link_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM share_links WHERE id = ?", [link_id])
        await db.commit()
    finally:
        await db.close()


async def delete_share_links_for_download(download_id: str):
    db = await get_db()
    try:
        await db.execute("DELETE FROM share_links WHERE download_id = ?", [download_id])
        await db.commit()
    finally:
        await db.close()
