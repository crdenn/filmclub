"""Portable, validated application backup and restore support.

A Film Club backup is a small ZIP archive containing an online SQLite snapshot,
the data-at-rest key needed to decrypt saved settings and Plex member tokens,
and a checksummed manifest. Restore never extracts arbitrary paths: it reads the
three expected entries directly, validates a temporary database, re-encrypts
secrets for the destination installation, and only then replaces the live file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from . import config, db, migrations
from . import settings as app_settings

log = logging.getLogger("filmclub.backups")

FORMAT_NAME = "filmclub-backup"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "filmclub.db"
DATA_KEY_NAME = "data.key"
EXPECTED_ENTRIES = {MANIFEST_NAME, DATABASE_NAME, DATA_KEY_NAME}
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
MAX_KEY_BYTES = 8 * 1024
RESTORE_CONFIRMATION = "RESTORE"


class BackupError(ValueError):
    """A backup could not be created or safely restored."""


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}{suffix}"
    number = 1
    while candidate.exists():
        candidate = directory / f"{stem}-{number}{suffix}"
        number += 1
    return candidate


def _online_copy(source: Path, destination: Path) -> None:
    """Copy committed SQLite state without requiring the application to stop."""
    src = sqlite3.connect(source, timeout=30)
    dest = sqlite3.connect(destination)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if not row:
            return 0
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        return int(version or 0)
    finally:
        conn.close()


def _zip_entry(name: str, data: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, date_time=datetime.now().timetuple()[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info, data


def _store_effective_settings(path: Path) -> None:
    """Make environment-backed runtime settings portable in the snapshot.

    Normal UI-managed settings are already in SQLite. Legacy/automated installs
    may override them through the environment, so the backup snapshot stores the
    effective values as encrypted database settings without changing the live DB.
    A destination environment override still wins when the restored app loads.
    """
    encryptor = _fernet(config.DATA_KEY, "filmclub-app-settings")
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN")
        for key, meta in app_settings.FIELDS.items():
            raw = getattr(config, key, "")
            value = "" if raw is None else str(raw).strip()
            stored = encryptor.encrypt(value.encode()).decode("ascii") \
                if meta["secret"] and value else value
            conn.execute(
                """INSERT INTO app_settings (key, value, encrypted, updated_at)
                   VALUES (?, ?, ?, datetime('now'))
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                     encrypted=excluded.encrypted, updated_at=excluded.updated_at""",
                (key, stored, 1 if meta["secret"] and value else 0),
            )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_archive() -> tuple[bytes, str]:
    """Return a portable backup archive and its suggested download filename."""
    if not config.DB_PATH.exists():
        raise BackupError("The Film Club database does not exist")
    with tempfile.TemporaryDirectory(prefix="filmclub-backup-", dir=config.DATA_DIR) as tmp:
        snapshot = Path(tmp) / DATABASE_NAME
        _online_copy(config.DB_PATH, snapshot)
        _store_effective_settings(snapshot)
        database = snapshot.read_bytes()

    if len(database) > MAX_DATABASE_BYTES:
        raise BackupError("The database is too large for an in-browser backup")
    data_key = config.DATA_KEY.encode("utf-8")
    manifest = {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_version": config.APP_VERSION,
        "schema_version": _schema_version_from_bytes(database),
        "database_sha256": hashlib.sha256(database).hexdigest(),
        "data_key_sha256": hashlib.sha256(data_key).hexdigest(),
    }
    manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(*_zip_entry(MANIFEST_NAME, manifest_bytes))
        archive.writestr(*_zip_entry(DATABASE_NAME, database))
        archive.writestr(*_zip_entry(DATA_KEY_NAME, data_key))
    payload = output.getvalue()
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise BackupError("The backup archive is too large to download in the browser")
    return payload, f"filmclub-backup-{_timestamp()}.filmclub-backup"


def _schema_version_from_bytes(database: bytes) -> int:
    with tempfile.TemporaryDirectory(prefix="filmclub-backup-check-", dir=config.DATA_DIR) as tmp:
        path = Path(tmp) / DATABASE_NAME
        path.write_bytes(database)
        return _schema_version(path)


def _read_archive(payload: bytes) -> tuple[dict, bytes, str]:
    if not payload:
        raise BackupError("Choose a Film Club backup file")
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise BackupError("The backup file is too large")
    try:
        with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != len(set(names)) or set(names) != EXPECTED_ENTRIES:
                raise BackupError("This is not a recognized Film Club backup")
            sizes = {entry.filename: entry.file_size for entry in entries}
            if sizes[MANIFEST_NAME] > MAX_MANIFEST_BYTES:
                raise BackupError("The backup manifest is too large")
            if sizes[DATABASE_NAME] > MAX_DATABASE_BYTES:
                raise BackupError("The database in this backup is too large")
            if sizes[DATA_KEY_NAME] > MAX_KEY_BYTES:
                raise BackupError("The data key in this backup is invalid")
            bad_entry = archive.testzip()
            if bad_entry:
                raise BackupError(f"The backup is corrupt ({bad_entry})")
            manifest_bytes = archive.read(MANIFEST_NAME)
            database = archive.read(DATABASE_NAME)
            key_bytes = archive.read(DATA_KEY_NAME)
    except BackupError:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError) as exc:
        raise BackupError("This is not a valid Film Club backup") from exc

    try:
        manifest = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupError("The backup manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT_NAME:
        raise BackupError("This is not a recognized Film Club backup")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError("This backup format is not supported by this version of Film Club")
    if not hmac.compare_digest(
        str(manifest.get("database_sha256", "")), hashlib.sha256(database).hexdigest()
    ):
        raise BackupError("The database checksum does not match; the backup may be corrupt")
    if not hmac.compare_digest(
        str(manifest.get("data_key_sha256", "")), hashlib.sha256(key_bytes).hexdigest()
    ):
        raise BackupError("The data-key checksum does not match; the backup may be corrupt")
    try:
        data_key = key_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise BackupError("The data key in this backup is invalid") from exc
    if not data_key or data_key != data_key.strip():
        raise BackupError("The data key in this backup is invalid")
    return manifest, database, data_key


def _fernet(data_key: str, purpose: str) -> Fernet:
    digest = hashlib.sha256(f"{purpose}:{data_key}".encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _rekey_database(path: Path, source_key: str, destination_key: str) -> None:
    """Validate and re-encrypt every key-protected value in a restored DB."""
    settings_old = _fernet(source_key, "filmclub-app-settings")
    settings_new = _fernet(destination_key, "filmclub-app-settings")
    plex_old = _fernet(source_key, "filmclub-plex-token")
    plex_new = _fernet(destination_key, "filmclub-plex-token")
    conn = sqlite3.connect(path)
    try:
        conn.execute("BEGIN")
        for key, value in conn.execute(
            "SELECT key, value FROM app_settings WHERE encrypted = 1 AND value <> ''"
        ).fetchall():
            try:
                plaintext = settings_old.decrypt(value.encode("ascii"))
            except (InvalidToken, ValueError, UnicodeError) as exc:
                raise BackupError(f"Saved setting {key} could not be decrypted") from exc
            conn.execute(
                "UPDATE app_settings SET value = ? WHERE key = ?",
                (settings_new.encrypt(plaintext).decode("ascii"), key),
            )
        for member_id, value in conn.execute(
            "SELECT id, plex_token_encrypted FROM members "
            "WHERE plex_token_encrypted IS NOT NULL AND plex_token_encrypted <> ''"
        ).fetchall():
            try:
                plaintext = plex_old.decrypt(value.encode("ascii"))
            except (InvalidToken, ValueError, UnicodeError) as exc:
                raise BackupError(
                    f"A saved Plex member credential (member {member_id}) could not be decrypted"
                ) from exc
            conn.execute(
                "UPDATE members SET plex_token_encrypted = ? WHERE id = ?",
                (plex_new.encrypt(plaintext).decode("ascii"), member_id),
            )
        # Session tokens are intentionally not restored. A restore is a security
        # boundary and may replace the entire member set, so everyone signs in again.
        conn.execute("DELETE FROM sessions")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA journal_mode=DELETE")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _validate_database(path: Path) -> int:
    try:
        conn = sqlite3.connect(path)
        try:
            integrity = conn.execute("PRAGMA quick_check").fetchone()
            if not integrity or integrity[0] != "ok":
                raise BackupError("The backup database failed its integrity check")
            required = {"members", "movies", "ratings", "votes", "prior_views"}
            tables = {
                row[0] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if not required.issubset(tables):
                raise BackupError("The backup database is missing required Film Club data")
        finally:
            conn.close()
        version = _schema_version(path)
    except BackupError:
        raise
    except sqlite3.DatabaseError as exc:
        raise BackupError("The backup does not contain a valid SQLite database") from exc
    if version > migrations.latest_version():
        raise BackupError(
            "This backup was made by a newer Film Club version; update the app before restoring it"
        )
    return version


def _remove_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def restore_archive(payload: bytes) -> dict:
    """Validate and atomically restore an uploaded portable backup archive."""
    manifest, database, source_key = _read_archive(payload)
    with tempfile.TemporaryDirectory(prefix="filmclub-restore-", dir=config.DATA_DIR) as tmp:
        candidate = Path(tmp) / DATABASE_NAME
        candidate.write_bytes(database)
        actual_version = _validate_database(candidate)
        if manifest.get("schema_version") != actual_version:
            raise BackupError("The backup schema metadata does not match its database")

        # Bring an older same-format backup forward before re-keying its secrets.
        migrations.run(candidate)
        _rekey_database(candidate, source_key, config.DATA_KEY)
        restored_version = _validate_database(candidate)
        conn = sqlite3.connect(candidate)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise BackupError("The backup database contains broken relationships")
        finally:
            conn.close()

        safety = _unique_path(
            config.DATA_DIR / "backups", f"filmclub-{_timestamp()}-pre-restore", ".db"
        )
        _online_copy(config.DB_PATH, safety)

        # The candidate and live database share a filesystem, so os.replace is
        # atomic. If initialization unexpectedly fails, put the safety copy back.
        rollback = Path(tmp) / "rollback.db"
        shutil.copy2(safety, rollback)
        try:
            _remove_sidecars(config.DB_PATH)
            os.replace(candidate, config.DB_PATH)
            db.init_db()
        except Exception:
            _remove_sidecars(config.DB_PATH)
            os.replace(rollback, config.DB_PATH)
            db.init_db()
            raise

    log.warning(
        "Restored Film Club backup created at %s; safety copy: %s",
        manifest.get("created_at", "unknown time"), safety,
    )
    return {
        "created_at": manifest.get("created_at"),
        "schema_version": restored_version,
        "safety_backup": safety.name,
        "sessions_revoked": True,
    }
