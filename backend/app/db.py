import asyncio
import pathlib
import sqlite3
from collections import deque

DB_PATH = pathlib.Path(__file__).parent.parent / "data" / "radar.db"

_SQL_INSERT = (
    "INSERT INTO track_samples"
    " (ts_ms, track_id, bearing, range_u, heading, rel_speed_u, altitude_m, confidence)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _to_row(s: dict) -> tuple:
    return (
        s.get("ts_ms"),
        s.get("track_id"),
        s.get("bearing"),
        s.get("range_u"),
        s.get("heading"),
        s.get("rel_speed_u"),
        s.get("altitude_m"),
        s.get("confidence"),
    )


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS track_samples (
                ts_ms       INTEGER NOT NULL,
                track_id    INTEGER NOT NULL,
                bearing     REAL,
                range_u     REAL,
                heading     REAL,
                rel_speed_u REAL,
                altitude_m  REAL,
                confidence  REAL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_track_samples_ts_ms
            ON track_samples (ts_ms)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS zones (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                shape       TEXT NOT NULL,
                coords      TEXT NOT NULL,
                created_ms  INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id     INTEGER REFERENCES zones(id) ON DELETE CASCADE,
                ts_ms       INTEGER NOT NULL,
                event_type  TEXT NOT NULL,
                track_id    INTEGER,
                detail      TEXT
            )
        """)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_ts_ms
            ON events (ts_ms)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_zone_id
            ON events (zone_id)
        """)


class TrackSampleWriter:
    _FLUSH_ROWS = 500
    _FLUSH_INTERVAL = 0.2  # seconds
    _MAX_ROWS = 5000

    def __init__(self) -> None:
        self._buf: deque[dict] = deque()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._flush_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
        self._flush()  # drain whatever remains

    def add_sample(self, sample: dict) -> None:
        if len(self._buf) >= self._MAX_ROWS:
            self._buf.popleft()  # drop oldest to make room
        self._buf.append(sample)
        if len(self._buf) >= self._FLUSH_ROWS:
            self._flush()

    def _flush(self) -> None:
        if not self._buf:
            return
        rows = list(self._buf)
        self._buf.clear()
        with get_connection() as conn:
            conn.executemany(_SQL_INSERT, [_to_row(s) for s in rows])

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._FLUSH_INTERVAL)
            self._flush()
