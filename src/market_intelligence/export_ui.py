from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any

from . import __version__
from .util import iso_utc, redact_sensitive_text


_SENSITIVE_KEYS = re.compile(
    r"^(?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|password|passwd|secret|cookie|set-cookie)$",
    re.IGNORECASE,
)
_COOKIE_NAME = "__Host-mi_export_session"
_KINDS = {"bundle", "events", "observations", "briefings"}
_RANGES = {"24h": 24, "7d": 24 * 7, "all": None}


class ExportError(RuntimeError):
    pass


class ExportLimitError(ExportError):
    pass


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(left.encode("utf-8")).digest(),
        hashlib.sha256(right.encode("utf-8")).digest(),
    )


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    return max(minimum, min(value, maximum))


@dataclass(frozen=True)
class ExportSettings:
    enabled: bool
    host: str
    port: int
    token: str
    require_https: bool
    session_seconds: int
    request_timeout_seconds: int
    max_input_bytes: int
    max_zip_bytes: int
    tmp_quota_bytes: int
    tmp_dir: Path
    max_http_threads: int

    @classmethod
    def from_env(cls) -> "ExportSettings":
        mib = 1024 * 1024
        return cls(
            enabled=_env_bool("MI_EXPORT_UI_ENABLED", False),
            host="0.0.0.0",
            port=_bounded_int("MI_EXPORT_UI_PORT", 8080, 1, 65535),
            token=os.getenv("MI_EXPORT_TOKEN", ""),
            require_https=_env_bool("MI_EXPORT_REQUIRE_HTTPS", True),
            session_seconds=_bounded_int("MI_EXPORT_SESSION_SECONDS", 3600, 300, 86400),
            request_timeout_seconds=_bounded_int("MI_EXPORT_REQUEST_TIMEOUT_SECONDS", 120, 10, 900),
            max_input_bytes=_bounded_int("MI_EXPORT_MAX_INPUT_MIB", 192, 8, 2048) * mib,
            max_zip_bytes=_bounded_int("MI_EXPORT_MAX_ZIP_MIB", 192, 8, 2048) * mib,
            tmp_quota_bytes=_bounded_int("MI_EXPORT_TMP_QUOTA_MIB", 384, 32, 4096) * mib,
            tmp_dir=Path(os.getenv("MI_EXPORT_TMP_DIR", tempfile.gettempdir())).resolve(),
            max_http_threads=_bounded_int("MI_EXPORT_MAX_HTTP_THREADS", 8, 2, 32),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if len(self.token.encode("utf-8")) < 32:
            raise ValueError("MI_EXPORT_TOKEN must contain at least 32 UTF-8 bytes")
        if self.max_zip_bytes > self.tmp_quota_bytes:
            raise ValueError("MI_EXPORT_MAX_ZIP_MIB cannot exceed MI_EXPORT_TMP_QUOTA_MIB")
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


class SessionStore:
    def __init__(self, ttl_seconds: int, capacity: int = 64) -> None:
        self.ttl_seconds = ttl_seconds
        self.capacity = capacity
        self._sessions: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("ascii")).hexdigest()

    def create(self) -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= self.capacity:
                oldest = min(self._sessions, key=lambda key: self._sessions[key][0])
                self._sessions.pop(oldest, None)
            self._sessions[self._digest(raw)] = (now + self.ttl_seconds, csrf)
        return raw, csrf

    def get(self, raw: str | None) -> str | None:
        if not raw:
            return None
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            value = self._sessions.get(self._digest(raw))
        return value[1] if value and value[0] > now else None

    def remove(self, raw: str | None) -> None:
        if raw:
            with self._lock:
                self._sessions.pop(self._digest(raw), None)

    def _prune(self, now: float) -> None:
        for key, (expires, _) in list(self._sessions.items()):
            if expires <= now:
                self._sessions.pop(key, None)


class FailureLimiter:
    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        with self._lock:
            items = [stamp for stamp in self._failures.get(address, []) if now - stamp < 900]
            self._failures[address] = items
            if len(items) < 5:
                return True
            delay = min(300, 2 ** min(len(items) - 5, 8))
            return now - items[-1] >= delay

    def fail(self, address: str) -> None:
        now = time.monotonic()
        with self._lock:
            items = [stamp for stamp in self._failures.get(address, []) if now - stamp < 900]
            items.append(now)
            self._failures[address] = items[-32:]

    def success(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


@dataclass
class ExportArtifact:
    path: Path
    filename: str
    temp_root: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


def _cutoff(range_name: str, now: datetime) -> str | None:
    hours = _RANGES[range_name]
    return iso_utc(now - timedelta(hours=hours)) if hours is not None else None


def _safe_json(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): ("[REDACTED]" if _SENSITIVE_KEYS.match(str(key)) else _safe_json(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_json(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    return value


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            (_SENSITIVE_KEYS.match(str(key)) and str(item) not in {"[REDACTED]", "REDACTED", ""})
            or _contains_sensitive_key(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_sensitive_key(item) for item in value)
    if isinstance(value, str):
        return redact_sensitive_text(value) != value
    return False


def _safe_child(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    if candidate.is_symlink():
        raise ExportError("symbolic links are not exportable")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ExportError("export path escapes its allowed root") from error
    if resolved.parent != root or not resolved.is_file():
        raise ExportError("only regular top-level export files are allowed")
    return resolved


def _hash_file(path: Path, deadline: float) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("export request timed out")
            chunk = handle.read(65536)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _temp_usage(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


def _enforce_temp_quota(root: Path, quota: int) -> None:
    if _temp_usage(root) > quota:
        raise ExportLimitError("export exceeded its temporary disk quota")


def _add_to_zip(archive: zipfile.ZipFile, source: Path, deadline: float, zip_path: Path,
                max_zip_bytes: int, base_temp_bytes: int, tmp_quota_bytes: int) -> None:
    with source.open("rb") as input_handle, archive.open(source.name, "w", force_zip64=True) as output_handle:
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("export request timed out")
            chunk = input_handle.read(65536)
            if not chunk:
                break
            output_handle.write(chunk)
            if zip_path.stat().st_size > max_zip_bytes:
                raise ExportLimitError("ZIP exceeds the configured maximum size")
            if base_temp_bytes + zip_path.stat().st_size > tmp_quota_bytes:
                raise ExportLimitError("export exceeded its temporary disk quota")


def _database_backup(db_path: Path, target: Path, deadline: float) -> None:
    source = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    destination = sqlite3.connect(target)
    try:
        def progress(_: int, __: int, ___: int) -> None:
            if time.monotonic() > deadline:
                raise TimeoutError("database backup timed out")
        source.backup(destination, pages=128, progress=progress, sleep=0.01)
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
    finally:
        destination.close()
        source.close()


def _audit_database(db_path: Path) -> None:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    try:
        columns = {
            "events": ("payload_json",), "observations": ("payload_json",),
            "candidate_imports": ("payload_json",), "observer_outputs": ("payload_json",),
            "briefings": ("input_snapshot_json", "output_json"),
            "briefing_diagnostics": ("usage_json", "diagnostics_json"),
        }
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table, names in columns.items():
            if table not in tables:
                continue
            for name in names:
                for (raw,) in connection.execute(f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'):
                    try:
                        parsed = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if _contains_sensitive_key(parsed):
                        raise ExportError("database contains a credential-like JSON field; full backup refused")
        for table in tables:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
                continue
            text_columns = [row[1] for row in connection.execute(f'PRAGMA table_info("{table}")') if str(row[2]).upper() == "TEXT"]
            for name in text_columns:
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                    continue
                for (raw,) in connection.execute(f'SELECT "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'):
                    if isinstance(raw, str) and redact_sensitive_text(raw) != raw:
                        raise ExportError("database contains credential-like text; full backup refused")
    finally:
        connection.close()


def _write_rows(connection: sqlite3.Connection, target: Path, table: str, cutoff: str | None,
                deadline: float, max_bytes: int) -> dict[str, Any]:
    time_columns = {
        "events": "available_to_system_at_utc",
        "observations": "available_to_system_at_utc",
        "briefings": "COALESCE(available_to_system_at_utc, first_seen_at_utc)",
    }
    where = f" WHERE {time_columns[table]} >= ?" if cutoff else ""
    query = f'SELECT * FROM "{table}"{where} ORDER BY {time_columns[table]}'
    cursor = connection.execute(query, (cutoff,) if cutoff else ())
    digest = hashlib.sha256();rows = 0;written = 0
    with target.open("wb") as handle:
        for record in cursor:
            if time.monotonic() > deadline:
                raise TimeoutError("export request timed out")
            item = _safe_json(dict(record))
            for key, value in list(item.items()):
                if key.endswith("_json") and isinstance(value, str):
                    try:
                        item[key[:-5]] = _safe_json(json.loads(value));del item[key]
                    except json.JSONDecodeError:
                        pass
            encoded = (json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            written += len(encoded)
            if written > max_bytes:
                raise ExportLimitError("selected export exceeds the configured input limit")
            handle.write(encoded);digest.update(encoded);rows += 1
    return {"path": target.name, "bytes": written, "rows": rows, "sha256": digest.hexdigest()}


def _selected_hourly_files(export_dir: Path, cutoff: str | None) -> list[Path]:
    selected = []
    cutoff_hour = cutoff[:13].replace("T", "-").replace(":", "") if cutoff else None
    for candidate in sorted(export_dir.glob("*.jsonl")):
        safe = _safe_child(export_dir, candidate)
        match = re.match(r"^[a-z]+-(\d{4}-\d{2}-\d{2}-\d{2})\.jsonl$", safe.name)
        if match and (cutoff_hour is None or match.group(1) >= cutoff_hour):
            selected.append(safe)
    return selected


def _copy_sanitized_jsonl(source: Path, target: Path, deadline: float, remaining: int) -> dict[str, Any]:
    digest = hashlib.sha256();rows = 0;written = 0;fixed_size = source.stat().st_size
    with source.open("rb") as input_handle, target.open("wb") as output_handle:
        while input_handle.tell() < fixed_size:
            if time.monotonic() > deadline:
                raise TimeoutError("export request timed out")
            raw = input_handle.readline(min(1024 * 1024, fixed_size - input_handle.tell()))
            if not raw:
                break
            if not raw.endswith(b"\n"):
                # An appender may have been between write() calls when the fixed
                # snapshot size was captured. Ignore only that incomplete tail.
                if input_handle.tell() >= fixed_size:
                    break
                raise ExportError(f"JSONL line exceeds the 1 MiB safety limit in {source.name}")
            try:
                encoded = (json.dumps(_safe_json(json.loads(raw)), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ExportError(f"invalid JSONL encountered in {source.name}") from error
            written += len(encoded)
            if written > remaining:
                raise ExportLimitError("selected export exceeds the configured input limit")
            output_handle.write(encoded);digest.update(encoded);rows += 1
    return {"path": target.name, "bytes": written, "rows": rows, "sha256": digest.hexdigest()}


def _schema_provenance(migrations_dir: Path, deadline: float) -> list[dict[str, Any]]:
    result = []
    for candidate in sorted(migrations_dir.glob("*.sql")):
        safe = _safe_child(migrations_dir, candidate);size, digest = _hash_file(safe, deadline)
        result.append({"name": safe.name, "bytes": size, "sha256": digest})
    return result


def _database_summary(db_path: Path) -> tuple[dict[str, int], list[dict[str, Any]], str | None]:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        wanted = ("events", "observations", "briefings", "schedule_revisions", "candidate_imports")
        counts = {name: int(connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]) for name in wanted if name in tables}
        health = []
        if "source_health" in tables:
            health = [dict(row) for row in connection.execute(
                "SELECT source,status,last_attempt_at_utc,last_success_at_utc,consecutive_failures,latency_ms FROM source_health ORDER BY source")]
        candidates = []
        for table, column in (("events", "available_to_system_at_utc"), ("observations", "available_to_system_at_utc"), ("briefings", "available_to_system_at_utc")):
            if table in tables:
                candidates.append(connection.execute(f'SELECT max("{column}") FROM "{table}"').fetchone()[0])
        return counts, health, max((value for value in candidates if value), default=None)
    finally:
        connection.close()


def create_export(db_path: Path, export_dir: Path, migrations_dir: Path, settings: ExportSettings,
                  kind: str, range_name: str, now: datetime | None = None) -> ExportArtifact:
    if kind not in _KINDS or range_name not in _RANGES:
        raise ExportError("invalid export selection")
    now = now or datetime.now(timezone.utc);deadline = time.monotonic() + settings.request_timeout_seconds
    temp_root = Path(tempfile.mkdtemp(prefix="mi-export-", dir=settings.tmp_dir));stage = temp_root / "stage";stage.mkdir()
    filename = f"market-intelligence-{kind}-{range_name}-{now:%Y%m%dT%H%M%SZ}.zip"
    try:
        if shutil.disk_usage(settings.tmp_dir).free < settings.tmp_quota_bytes:
            raise ExportLimitError("temporary disk free space is below the configured safety quota")
        snapshot = temp_root / "snapshot.sqlite3";_database_backup(db_path, snapshot, deadline);_audit_database(snapshot)
        if snapshot.stat().st_size > settings.max_input_bytes:
            raise ExportLimitError("database snapshot exceeds the configured input limit")
        _enforce_temp_quota(temp_root, settings.tmp_quota_bytes)
        counts, health, source_watermark = _database_summary(snapshot);cutoff = _cutoff(range_name, now)
        files: list[dict[str, Any]] = [];consumed = 0
        if kind == "bundle":
            if range_name == "all":
                database_target = stage / "market_intelligence.sqlite3";shutil.move(snapshot, database_target)
                size, digest = _hash_file(database_target, deadline);consumed += size
                files.append({"path": database_target.name, "bytes": size, "rows": None, "sha256": digest})
                if consumed > settings.max_input_bytes:
                    raise ExportLimitError("database backup exceeds the configured input limit")
            for source in _selected_hourly_files(export_dir, cutoff):
                item = _copy_sanitized_jsonl(source, stage / source.name, deadline, settings.max_input_bytes - consumed)
                consumed += item["bytes"];files.append(item);_enforce_temp_quota(temp_root, settings.tmp_quota_bytes)
        else:
            connection = sqlite3.connect(f"file:{snapshot.as_posix()}?mode=ro", uri=True, timeout=10);connection.row_factory = sqlite3.Row
            try:
                item = _write_rows(connection, stage / f"{kind}-{range_name}.jsonl", kind, cutoff, deadline, settings.max_input_bytes)
            finally:
                connection.close()
            consumed += item["bytes"];files.append(item)
            _enforce_temp_quota(temp_root, settings.tmp_quota_bytes)
        if snapshot.exists():
            snapshot.unlink()
        manifest = {
            "schema_version": "MARKET_INTELLIGENCE_EXPORT_MANIFEST_V1", "service_version": __version__,
            "created_at_utc": iso_utc(now), "source_watermark_utc": source_watermark,
            "selection": {"kind": kind, "range": range_name, "cutoff_utc": cutoff},
            "database_row_counts": counts, "source_health": health,
            "schema_provenance": _schema_provenance(migrations_dir, deadline),
            "credential_audit": "passed; credential-like JSON fields are redacted from JSONL and block database backup",
            "files": files, "total_uncompressed_bytes": consumed,
        }
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
        zip_path = temp_root / filename
        base_temp_bytes = _temp_usage(temp_root)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for candidate in sorted(stage.iterdir()):
                safe = _safe_child(stage, candidate)
                _add_to_zip(archive, safe, deadline, zip_path, settings.max_zip_bytes, base_temp_bytes, settings.tmp_quota_bytes)
        if zip_path.stat().st_size > settings.max_zip_bytes:
            raise ExportLimitError("ZIP exceeds the configured maximum size")
        return ExportArtifact(zip_path, filename, temp_root)
    except Exception:
        shutil.rmtree(temp_root, ignore_errors=True);raise


def export_preview(db_path: Path, export_dir: Path) -> dict[str, Any]:
    counts, health, watermark = _database_summary(db_path)
    db_bytes = db_path.stat().st_size if db_path.exists() else 0
    now = datetime.now(timezone.utc)
    files = [path for path in export_dir.glob("*.jsonl") if path.is_file() and not path.is_symlink()]
    def size_since(hours: int | None) -> int:
        cutoff_hour = (now - timedelta(hours=hours)).strftime("%Y-%m-%d-%H") if hours else None
        total = 0
        for path in files:
            match = re.match(r"^[a-z]+-(\d{4}-\d{2}-\d{2}-\d{2})\.jsonl$", path.name)
            if match and (cutoff_hour is None or match.group(1) >= cutoff_hour):
                total += path.stat().st_size
        return total
    range_bytes = {"24h": size_since(24), "7d": size_since(24 * 7), "all": size_since(None)}
    return {"counts": counts, "health": health, "watermark": watermark,
            "database_bytes": db_bytes, "jsonl_bytes": range_bytes["all"], "range_bytes": range_bytes}


class BoundedThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler: type[BaseHTTPRequestHandler], max_threads: int) -> None:
        self._slots = threading.BoundedSemaphore(max_threads);super().__init__(server_address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release();raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class ExportServer(BoundedThreadingHTTPServer):
    def __init__(self, settings: ExportSettings, db_path: Path, export_dir: Path, migrations_dir: Path) -> None:
        self.settings = settings;self.db_path = db_path;self.export_dir = export_dir;self.migrations_dir = migrations_dir
        self.sessions = SessionStore(settings.session_seconds);self.failures = FailureLimiter();self.export_lock = threading.Lock()
        super().__init__((settings.host, settings.port), ExportHandler, settings.max_http_threads)


class ExportHandler(BaseHTTPRequestHandler):
    server: ExportServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write(f"market-intelligence export-ui response={args[1] if len(args) > 1 else '-'}\n")

    def _security_headers(self, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        if length is not None:self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store, max-age=0");self.send_header("Pragma", "no-cache");self.send_header("Expires", "0")
        self.send_header("X-Content-Type-Options", "nosniff");self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer");self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(), payment=(), usb=()")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")

    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status);self._security_headers(content_type, len(body));self.end_headers()
        if self.command != "HEAD":self.wfile.write(body)

    def _redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER);self._security_headers("text/plain; charset=utf-8", 0);self.send_header("Location", location)
        if cookie:self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def _secure_request(self) -> bool:
        if not self.server.settings.require_https:return True
        proto = self.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
        return proto == "https" or "proto=https" in self.headers.get("Forwarded", "").lower()

    def _cookie_value(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            key, separator, value = part.strip().partition("=")
            if separator and key == _COOKIE_NAME:return value
        return None

    def _bearer_ok(self) -> bool:
        prefix = "Bearer ";value = self.headers.get("Authorization", "")
        return value.startswith(prefix) and _constant_time_equal(value[len(prefix):], self.server.settings.token)

    def _auth(self) -> tuple[bool, str | None, str | None]:
        if self._bearer_ok():return True, None, None
        raw = self._cookie_value();csrf = self.server.sessions.get(raw)
        return csrf is not None, raw, csrf

    def _read_form(self) -> dict[str, str]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/x-www-form-urlencoded":
            raise ValueError("unsupported form type")
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 8192:raise ValueError("form too large")
        parsed = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8", "strict"), keep_blank_values=True, max_num_fields=12)
        return {key: values[-1] for key, values in parsed.items()}

    def _login_page(self, message: str = "") -> bytes:
        note = f'<p class="error">{html.escape(message)}</p>' if message else ""
        return _page("Market Intelligence", f"""<main><h1>Market Intelligence</h1><p>Enter the private export token.</p>{note}
          <form method="post" action="/login"><label>Export token<input name="token" type="password" autocomplete="current-password" required></label>
          <button type="submit">Unlock downloads</button></form></main>""")

    def _dashboard(self, csrf: str) -> bytes:
        preview = export_preview(self.server.db_path, self.server.export_dir);counts = preview["counts"]
        health_ok = sum(1 for item in preview["health"] if item["status"] == "OK");health_total = len(preview["health"])
        approximate = preview["database_bytes"] + preview["jsonl_bytes"]
        options = "".join(f'<option value="{name}">{label}</option>' for name, label in (("bundle", "Bundle (database + hourly JSONL)"), ("observations", "Observations JSONL"), ("events", "Events JSONL"), ("briefings", "Briefings JSONL")))
        ranges = "".join(f'<option value="{name}">{label}</option>' for name, label in (("24h", "Latest 24 hours"), ("7d", "Latest 7 days"), ("all", "Everything")))
        return _page("Market Intelligence exports", f"""<main><h1>Market Intelligence exports</h1>
          <section class="status"><strong>{counts.get('observations', 0):,}</strong> observations · <strong>{counts.get('events', 0):,}</strong> events · <strong>{counts.get('briefings', 0):,}</strong> briefings<br>
          Freshest data: {html.escape(preview['watermark'] or 'No data yet')}<br>Sources OK: {health_ok}/{health_total}<br>
          Hourly JSONL preview: 24h {_human_bytes(preview['range_bytes']['24h'])} · 7d {_human_bytes(preview['range_bytes']['7d'])} · all {_human_bytes(preview['range_bytes']['all'])}<br>
          Full “Everything” bundle: about {_human_bytes(approximate)} before compression.</section>
          <form method="post" action="/download"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><label>Download contents<select name="kind">{options}</select></label>
          <label>Date range<select name="range">{ranges}</select></label><button type="submit">Download ZIP</button></form>
          <p class="hint">“Everything” bundle includes a consistent SQLite backup. Smaller choices are phone-friendly JSONL ZIPs. Only one export is built at a time.</p>
          <form method="post" action="/logout" class="logout"><input type="hidden" name="csrf" value="{html.escape(csrf)}"><button type="submit">Lock</button></form></main>""")

    def do_HEAD(self) -> None:self.do_GET()

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/healthz":self._send(HTTPStatus.OK, b"ok\n");return
        if not self._secure_request():self._send(HTTPStatus.BAD_REQUEST, b"HTTPS required\n");return
        if path == "/robots.txt":self._send(HTTPStatus.OK, b"User-agent: *\nDisallow: /\n");return
        if path != "/":self._send(HTTPStatus.NOT_FOUND, b"Not found\n");return
        authenticated, _, csrf = self._auth();body = self._dashboard(csrf or "") if authenticated else self._login_page()
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")

    def do_POST(self) -> None:
        if not self._secure_request():self._send(HTTPStatus.BAD_REQUEST, b"HTTPS required\n");return
        path = urllib.parse.urlsplit(self.path).path
        try:form = self._read_form()
        except (ValueError, UnicodeDecodeError):self._send(HTTPStatus.BAD_REQUEST, b"Invalid request\n");return
        if path == "/login":self._login(form)
        elif path == "/logout":self._logout(form)
        elif path == "/download":self._download(form)
        else:self._send(HTTPStatus.NOT_FOUND, b"Not found\n")

    def _login(self, form: dict[str, str]) -> None:
        address = self.client_address[0]
        if not self.server.failures.allowed(address):
            self._send(HTTPStatus.TOO_MANY_REQUESTS, self._login_page("Too many attempts. Please wait."), "text/html; charset=utf-8");return
        if not _constant_time_equal(form.get("token", ""), self.server.settings.token):
            self.server.failures.fail(address);self._send(HTTPStatus.UNAUTHORIZED, self._login_page("Token not accepted."), "text/html; charset=utf-8");return
        self.server.failures.success(address);raw, _ = self.server.sessions.create()
        self._redirect("/", f"{_COOKIE_NAME}={raw}; Path=/; Max-Age={self.server.settings.session_seconds}; HttpOnly; Secure; SameSite=Strict")

    def _valid_session_form(self, form: dict[str, str]) -> tuple[bool, str | None]:
        authenticated, raw, csrf = self._auth()
        return authenticated and raw is not None and csrf is not None and _constant_time_equal(form.get("csrf", ""), csrf), raw

    def _logout(self, form: dict[str, str]) -> None:
        valid, raw = self._valid_session_form(form)
        if not valid:self._send(HTTPStatus.FORBIDDEN, b"Forbidden\n");return
        self.server.sessions.remove(raw);self._redirect("/", f"{_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")

    def _download(self, form: dict[str, str]) -> None:
        bearer = self._bearer_ok();valid, _ = self._valid_session_form(form)
        address = self.client_address[0]
        if not bearer and not valid:
            if self.headers.get("Authorization"):
                if not self.server.failures.allowed(address):
                    self._send(HTTPStatus.TOO_MANY_REQUESTS, b"Too many attempts\n");return
                self.server.failures.fail(address)
            self._send(HTTPStatus.FORBIDDEN, b"Forbidden\n");return
        self.server.failures.success(address)
        kind = form.get("kind", "");range_name = form.get("range", "")
        if kind not in _KINDS or range_name not in _RANGES:self._send(HTTPStatus.BAD_REQUEST, b"Invalid export selection\n");return
        if not self.server.export_lock.acquire(blocking=False):self._send(HTTPStatus.TOO_MANY_REQUESTS, b"Another export is being prepared\n");return
        artifact: ExportArtifact | None = None;response_started = False
        try:
            artifact = create_export(self.server.db_path, self.server.export_dir, self.server.migrations_dir, self.server.settings, kind, range_name)
            size = artifact.path.stat().st_size;self.send_response(HTTPStatus.OK);self._security_headers("application/zip", size)
            self.send_header("Content-Disposition", f'attachment; filename="{artifact.filename}"');self.end_headers()
            response_started = True
            deadline = time.monotonic() + self.server.settings.request_timeout_seconds
            with artifact.path.open("rb") as handle:
                while time.monotonic() <= deadline:
                    chunk = handle.read(65536)
                    if not chunk:break
                    self.wfile.write(chunk)
        except (ExportError, TimeoutError, OSError, sqlite3.Error, zipfile.BadZipFile):
            if response_started:
                self.close_connection = True
            else:
                try:self._send(HTTPStatus.SERVICE_UNAVAILABLE, b"Export could not be prepared within safety limits\n")
                except (BrokenPipeError, ConnectionError):pass
        finally:
            if artifact:artifact.cleanup()
            self.server.export_lock.release()


def _human_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if number < 1024 or unit == "GiB":return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} GiB"


def _page(title: str, content: str) -> bytes:
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title><style>
    :root{{color-scheme:dark;--bg:#08111d;--card:#111f30;--ink:#eef6ff;--muted:#9bb0c7;--accent:#49d3a5;--danger:#ff9b9b}}*{{box-sizing:border-box}}
    body{{margin:0;background:linear-gradient(160deg,#07101b,#10273a);color:var(--ink);font:16px/1.5 system-ui,-apple-system,sans-serif;min-height:100vh}}
    main{{max-width:620px;margin:auto;padding:clamp(24px,7vw,64px) 18px}}h1{{font-size:clamp(28px,8vw,44px);line-height:1.05;margin:0 0 18px}}
    form,.status{{background:var(--card);border:1px solid #29415b;border-radius:18px;padding:18px;margin:18px 0;box-shadow:0 18px 50px #0005}}label{{display:block;font-weight:700;margin:0 0 16px}}
    input,select,button{{display:block;width:100%;font:inherit;border-radius:12px;padding:14px;margin-top:7px}}input,select{{color:var(--ink);background:#081421;border:1px solid #3b5877}}
    button{{border:0;background:var(--accent);color:#052319;font-weight:850;cursor:pointer}}.hint{{color:var(--muted)}}.error{{color:var(--danger);font-weight:700}}.logout{{background:transparent;border:0;box-shadow:none;padding:0}}.logout button{{background:#26394d;color:var(--ink)}}
    </style></head><body>{content}</body></html>""".encode("utf-8")


def start_export_ui(db_path: Path, export_dir: Path, migrations_dir: Path) -> tuple[ExportServer, threading.Thread] | None:
    settings = ExportSettings.from_env()
    if not settings.enabled:return None
    settings.validate();server = ExportServer(settings, db_path, export_dir, migrations_dir)
    thread = threading.Thread(target=server.serve_forever, name="market-intelligence-export-ui", daemon=True);thread.start()
    return server, thread

