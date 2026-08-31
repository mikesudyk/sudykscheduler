import csv
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data")))
DB_PATH = DATA_DIR / "family.db"
UPLOAD_DIR = DATA_DIR / "uploads"

DEFAULT_COLORS = [
    "#2F6F4E",
    "#1F4E79",
    "#B4532A",
    "#6B3FA0",
    "#0F766E",
    "#9F1239",
    "#854D0E",
    "#1E3A5F",
]


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _connect() -> sqlite3.Connection:
    ensure_data_dirs()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kids (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                short_name TEXT,
                color TEXT NOT NULL DEFAULT '#2F6F4E',
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS uploads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL,
                stored_name TEXT NOT NULL,
                original_name TEXT NOT NULL,
                mime TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                extraction_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (kid_id) REFERENCES kids(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kid_id INTEGER NOT NULL,
                upload_id INTEGER,
                uid TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                event_type TEXT NOT NULL DEFAULT 'event',
                start_iso TEXT NOT NULL,
                end_iso TEXT,
                all_day INTEGER NOT NULL DEFAULT 0,
                location TEXT,
                opponent TEXT,
                notes TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (kid_id) REFERENCES kids(id) ON DELETE CASCADE,
                FOREIGN KEY (upload_id) REFERENCES uploads(id) ON DELETE SET NULL
            );
            """
        )
        cols = {row[1] for row in conn.execute("PRAGMA table_info(kids)")}
        if "parent" not in cols:
            conn.execute("ALTER TABLE kids ADD COLUMN parent TEXT")


PARENT_ORDER = ["Mike", "Laura", "Jen", "Dave"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_kids() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            """SELECT * FROM kids
               ORDER BY CASE parent
                    WHEN 'Mike' THEN 1
                    WHEN 'Laura' THEN 2
                    WHEN 'Jen' THEN 3
                    WHEN 'Dave' THEN 4
                    ELSE 9 END,
                    sort_order, name"""
        ).fetchall()
    return [dict(r) for r in rows]


def get_kid(kid_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM kids WHERE id = ?", (kid_id,)).fetchone()
    return dict(row) if row else None


def add_kid(
    name: str,
    color: str,
    short_name: str | None = None,
    parent: str | None = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO kids (name, short_name, color, parent, sort_order, created_at)
               VALUES (?, ?, ?, ?, COALESCE((SELECT MAX(sort_order)+1 FROM kids), 1), ?)""",
            (
                name.strip(),
                (short_name or "").strip() or None,
                color,
                (parent or "").strip() or None,
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def find_kid_by_name(name: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM kids WHERE lower(name) = lower(?)",
            (name.strip(),),
        ).fetchone()
    return dict(row) if row else None


def import_kids_csv(text: str) -> dict:
    """Load grandkids from a spreadsheet export.

    Accepted headers (case-insensitive): name or first_name or grandkid,
    short_name or nickname, color or hex.
    Extra columns are ignored so you can include parents, ages, teams, etc.
    """
    sample = text.lstrip("\ufeff")
    lines = sample.splitlines()
    header_i = 0
    for i, line in enumerate(lines):
        lower = line.lower()
        if "name" in lower and ("parent" in lower or "color" in lower or "nickname" in lower):
            header_i = i
            break
    trimmed = "\n".join(lines[header_i:])
    reader = csv.DictReader(io.StringIO(trimmed))
    if not reader.fieldnames:
        return {"added": 0, "skipped": 0, "errors": ["CSV has no header row"]}

    def pick(row: dict, *keys: str) -> str:
        lower = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
        for key in keys:
            if lower.get(key):
                return lower[key]
        return ""

    added = skipped = 0
    errors: list[str] = []

    with db() as conn:
        existing_names = {
            r["name"].lower()
            for r in conn.execute("SELECT name FROM kids")
        }
        color_i = len(existing_names)
        max_sort = conn.execute("SELECT COALESCE(MAX(sort_order), 0) FROM kids").fetchone()[0]

        for row in reader:
            name = pick(row, "name", "first_name", "firstname", "grandkid", "child", "kid")
            if not name:
                first = pick(row, "first")
                last = pick(row, "last", "last_name", "lastname")
                name = " ".join(p for p in (first, last) if p)
            if not name or name.lower() in {"name", "grandkid", "child"}:
                continue
            if name.lower() in existing_names:
                skipped += 1
                continue
            short_name = pick(row, "short_name", "short", "nickname", "nick") or None
            parent = pick(row, "parent", "household", "family") or None
            color = pick(row, "color", "hex", "colour")
            if color and not color.startswith("#"):
                color = "#" + color
            if not color or len(color) not in (4, 7):
                color = DEFAULT_COLORS[color_i % len(DEFAULT_COLORS)]
            max_sort += 1
            conn.execute(
                """INSERT INTO kids (name, short_name, color, parent, sort_order, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (name, short_name, color, parent, max_sort, now_iso()),
            )
            existing_names.add(name.lower())
            color_i += 1
            added += 1

    return {"added": added, "skipped": skipped, "errors": errors}


def delete_kid(kid_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM kids WHERE id = ?", (kid_id,))


def add_upload(kid_id: int, stored_name: str, original_name: str, mime: str) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO uploads (kid_id, stored_name, original_name, mime, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (kid_id, stored_name, original_name, mime, now_iso()),
        )
        return int(cur.lastrowid)


def get_upload(upload_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM uploads WHERE id = ?", (upload_id,)).fetchone()
    return dict(row) if row else None


def save_extraction(upload_id: int, payload: dict, status: str = "reviewed") -> None:
    with db() as conn:
        conn.execute(
            "UPDATE uploads SET extraction_json = ?, status = ? WHERE id = ?",
            (json.dumps(payload), status, upload_id),
        )


def list_events(kid_id: int | None = None, upcoming_only: bool = False) -> list[dict]:
    sql = """
        SELECT e.*, k.name AS kid_name, k.color AS kid_color, k.short_name AS kid_short,
               k.parent AS parent_name
        FROM events e
        JOIN kids k ON k.id = e.kid_id
    """
    params: list = []
    clauses = []
    if kid_id:
        clauses.append("e.kid_id = ?")
        params.append(kid_id)
    if upcoming_only:
        clauses.append("e.start_iso >= date('now', '-1 day')")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY e.start_iso"
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def add_event(event: dict) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO events (
                kid_id, upload_id, uid, title, event_type, start_iso, end_iso,
                all_day, location, opponent, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event["kid_id"],
                event.get("upload_id"),
                event["uid"],
                event["title"],
                event.get("event_type") or "event",
                event["start_iso"],
                event.get("end_iso"),
                1 if event.get("all_day") else 0,
                event.get("location"),
                event.get("opponent"),
                event.get("notes"),
                now_iso(),
            ),
        )
        return int(cur.lastrowid)


def delete_event(event_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))


def get_event(event_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(
            """SELECT e.*, k.name AS kid_name, k.color AS kid_color, k.short_name AS kid_short,
                      k.parent AS parent_name
               FROM events e JOIN kids k ON k.id = e.kid_id
               WHERE e.id = ?""",
            (event_id,),
        ).fetchone()
    return dict(row) if row else None


def seed_roster_if_empty(csv_path: Path) -> None:
    if list_kids():
        return
    if csv_path.exists():
        import_kids_csv(csv_path.read_text(encoding="utf-8"))
