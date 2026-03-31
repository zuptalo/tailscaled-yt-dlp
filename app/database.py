from datetime import datetime, timezone

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
    "ALTER TABLE downloads ADD COLUMN exit_node TEXT",
    "ALTER TABLE categories ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
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
        for sql in MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass
        await db.commit()

        # Seed default categories on first run
        cursor = await db.execute("SELECT COUNT(*) FROM categories")
        (count,) = await cursor.fetchone()
        if count == 0:
            import uuid
            from datetime import datetime, timezone

            now = datetime.now(timezone.utc).isoformat()
            seeds = ["YouTube", "Instagram", "X", "Facebook"]
            for i, name in enumerate(seeds):
                await db.execute(
                    "INSERT INTO categories (id, name, sort_order, created_at) VALUES (?, ?, ?, ?)",
                    [str(uuid.uuid4()), name, i, now],
                )
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


async def mark_interrupted_active_downloads() -> int:
    """Mark non-terminal jobs as failed after process restart (workers are gone).

    UI shows Retry for failed downloads instead of a no-op Cancel on ghost in-progress rows.
    """
    now = datetime.now(timezone.utc).isoformat()
    msg = "Interrupted when the server restarted. Use Retry to resume."
    db = await get_db()
    try:
        await db.execute(
            """UPDATE downloads SET status = 'failed', error_message = ?, updated_at = ?,
                   is_live = 0
               WHERE status IN ('queued', 'fetching_info', 'downloading', 'post_processing')""",
            [msg, now],
        )
        await db.commit()
        cur = await db.execute("SELECT changes()")
        row = await cur.fetchone()
        return int(row[0]) if row and row[0] is not None else 0
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
        cursor = await db.execute("SELECT * FROM categories ORDER BY sort_order, name")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def reorder_categories(ordered_ids: list[str]):
    db = await get_db()
    try:
        for i, cat_id in enumerate(ordered_ids):
            await db.execute(
                "UPDATE categories SET sort_order = ? WHERE id = ?", [i, cat_id]
            )
        await db.commit()
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
