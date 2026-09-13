import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    last_request_at TEXT
);
CREATE TABLE IF NOT EXISTS configurations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origin_station_id TEXT,
    service TEXT,
    direction TEXT,
    destination_station_id TEXT,
    destination_walk_minutes INTEGER
);
CREATE TABLE IF NOT EXISTS device_configurations (
    device_id TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    configuration_id INTEGER NOT NULL REFERENCES configurations(id) ON DELETE CASCADE,
    PRIMARY KEY (device_id, configuration_id)
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_service(service: str) -> str:
    return service.strip().upper()


DIRECTIONS = ("uptown", "downtown")


def normalize_direction(direction: str) -> str:
    normalized = direction.strip().lower()
    if normalized not in DIRECTIONS:
        raise ValueError(f"invalid direction: {direction!r} (expected one of {DIRECTIONS})")
    return normalized


class DeviceExistsError(Exception):
    pass


class DuplicateServiceError(Exception):
    """(device_id, service) must be unique together."""

    def __init__(self, service: str):
        self.service = service
        super().__init__(f"duplicate service for device: {service}")


@dataclass
class Device:
    id: str
    created_at: str
    last_request_at: str | None


@dataclass
class Configuration:
    id: int
    origin_station_id: str | None
    service: str | None
    direction: str | None  # "uptown" or "downtown"
    destination_station_id: str | None
    destination_walk_minutes: int | None

    @property
    def is_complete(self) -> bool:
        return bool(self.origin_station_id and self.service and self.direction)


@dataclass
class ConfigurationUpdate:
    """Fields of a configuration as submitted for saving (no id: rows are
    replaced wholesale on save)."""

    origin_station_id: str
    service: str
    direction: str
    destination_station_id: str | None = None
    destination_walk_minutes: int | None = None


class Database:
    """SQLite-backed store. A fresh connection per operation keeps this
    thread-safe without any pooling; at LAN scale the overhead is nothing."""

    def __init__(self, path: Path):
        self._path = path
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- devices ---------------------------------------------------------

    def enroll_device(self, device_id: str) -> Device:
        """Store the new device row plus one blank default configuration row
        pointed at it, per the enrollment interaction."""
        device_id = device_id.strip()
        if not device_id:
            raise ValueError("device id must not be empty")
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM devices WHERE id = ?", (device_id,)
            ).fetchone():
                raise DeviceExistsError(device_id)
            conn.execute(
                "INSERT INTO devices (id, created_at) VALUES (?, ?)",
                (device_id, _now_iso()),
            )
            cursor = conn.execute(
                "INSERT INTO configurations (origin_station_id) VALUES (NULL)"
            )
            conn.execute(
                "INSERT INTO device_configurations (device_id, configuration_id)"
                " VALUES (?, ?)",
                (device_id, cursor.lastrowid),
            )
        return self.get_device(device_id)

    def get_device(self, device_id: str) -> Device | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM devices WHERE id = ?", (device_id,)
            ).fetchone()
        return Device(**dict(row)) if row else None

    def list_devices(self) -> list[Device]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        return [Device(**dict(row)) for row in rows]

    def touch_device(self, device_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE devices SET last_request_at = ? WHERE id = ?",
                (_now_iso(), device_id),
            )

    # -- configurations --------------------------------------------------

    def get_configurations(self, device_id: str) -> list[Configuration]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT c.* FROM configurations c
                JOIN device_configurations dc ON dc.configuration_id = c.id
                WHERE dc.device_id = ?
                ORDER BY c.id
                """,
                (device_id,),
            ).fetchall()
        return [Configuration(**dict(row)) for row in rows]

    def replace_configurations(
        self, device_id: str, configs: list[ConfigurationUpdate]
    ) -> list[Configuration]:
        """Swap the device's whole configuration set for the submitted one
        (the configure page edits every row in-page and saves them together).
        Configurations no longer linked to ANY device are deleted."""
        seen: set[str] = set()
        for config in configs:
            service = normalize_service(config.service)
            if service in seen:
                raise DuplicateServiceError(service)
            seen.add(service)

        with self._connect() as conn:
            conn.execute(
                "DELETE FROM device_configurations WHERE device_id = ?", (device_id,)
            )
            for config in configs:
                cursor = conn.execute(
                    """
                    INSERT INTO configurations
                        (origin_station_id, service, direction,
                         destination_station_id, destination_walk_minutes)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        config.origin_station_id,
                        normalize_service(config.service),
                        normalize_direction(config.direction),
                        config.destination_station_id or None,
                        config.destination_walk_minutes,
                    ),
                )
                conn.execute(
                    "INSERT INTO device_configurations (device_id, configuration_id)"
                    " VALUES (?, ?)",
                    (device_id, cursor.lastrowid),
                )
            # Orphaned configuration rows (no device links left) are garbage.
            conn.execute(
                "DELETE FROM configurations WHERE id NOT IN"
                " (SELECT configuration_id FROM device_configurations)"
            )
        return self.get_configurations(device_id)
