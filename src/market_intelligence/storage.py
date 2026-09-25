from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .models import Event, Observation
from .util import canonical_json, iso_utc, sha256_text, stable_id


class Storage:
    def __init__(self, data_dir: Path, boot_id: str, migrations_dir: Path) -> None:
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.boot_id = boot_id
        self.db_path = data_dir / "market_intelligence.sqlite3"
        self.export_dir = data_dir / "exports"
        self.export_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for migration in sorted(migrations_dir.glob("*.sql")):
            self.db.executescript(migration.read_text("utf-8"))
        self.db.execute("INSERT OR IGNORE INTO boots VALUES (?,?,?)", (boot_id, iso_utc(), "0.1.0"))
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def _append(self, kind: str, value: dict[str, Any], at: str) -> None:
        hour = at[:13].replace("T", "-").replace(":", "")
        target = self.export_dir / f"{kind}-{hour}.jsonl"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json({"boot_id": self.boot_id, **value}) + "\n")

    def upsert_event(self, event: Event) -> str:
        payload = event.as_dict()
        content_hash = sha256_text(canonical_json(payload))
        # Scheduled/publisher times are revisionable facts, never identity. Prefer an official
        # UID where supplied; otherwise the source/type/title tuple is the stable series key.
        source_identity = event.payload.get("uid") or event.payload.get("source_event_id") or event.title
        event_id = stable_id("evt", event.source, event.event_type, source_identity)
        now = iso_utc()
        first_seen = event.first_seen_at_utc or event.observed_at_utc
        with self._lock:
            previous = self.db.execute("SELECT scheduled_at_utc,content_hash FROM events WHERE event_id=?", (event_id,)).fetchone()
            self.db.execute("""INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(event_id) DO UPDATE SET scheduled_at_utc=excluded.scheduled_at_utc,
              publisher_time_utc=excluded.publisher_time_utc,observed_at_utc=excluded.observed_at_utc,
              available_to_system_at_utc=excluded.available_to_system_at_utc,
              verification_status=excluded.verification_status,source_url=excluded.source_url,
              content_hash=excluded.content_hash,payload_json=excluded.payload_json,
              boot_id=excluded.boot_id,updated_at_utc=excluded.updated_at_utc""",
              (event_id,event.source,event.event_type,event.title,event.scheduled_at_utc,event.publisher_time_utc,
               first_seen,event.observed_at_utc,event.available_to_system_at_utc,event.verification_status,
               event.publisher,event.source_url,content_hash,canonical_json(payload),self.boot_id,now,now))
            if previous is None or previous["scheduled_at_utc"] != event.scheduled_at_utc or previous["content_hash"] != content_hash:
                revision_id=stable_id("rev",event_id,event.observed_at_utc,content_hash)
                self.db.execute("INSERT OR IGNORE INTO schedule_revisions VALUES (?,?,?,?,?,?,?)",
                  (revision_id,event_id,event.observed_at_utc,event.scheduled_at_utc,content_hash,event.source_url,canonical_json(payload)))
            self.db.commit()
            self._append("events",{"event_id":event_id,"content_hash":content_hash,**payload},event.observed_at_utc)
        return event_id

    def add_observation(self, observation: Observation) -> str:
        payload=observation.as_dict();content_hash=sha256_text(canonical_json(payload))
        observation_id=stable_id("obs",observation.source,observation.metric,observation.instrument,
          observation.observed_at_utc,content_hash)
        with self._lock:
            self.db.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (observation_id,observation.source,observation.observed_at_utc,observation.available_to_system_at_utc,
               observation.metric,observation.instrument,observation.value_num,observation.unit,observation.status,
               observation.source_url,content_hash,canonical_json(payload),self.boot_id))
            self.db.commit();self._append("observations",{"observation_id":observation_id,"content_hash":content_hash,**payload},observation.observed_at_utc)
        return observation_id

    def health(self, source: str, status: str, attempted_at: str, latency_ms: int,
               detail: str | None = None) -> None:
        with self._lock:
            old=self.db.execute("SELECT consecutive_failures,last_success_at_utc FROM source_health WHERE source=?",(source,)).fetchone()
            failures=0 if status=="OK" else int(old["consecutive_failures"] if old else 0)+1
            success=attempted_at if status=="OK" else (old["last_success_at_utc"] if old else None)
            self.db.execute("""INSERT INTO source_health VALUES (?,?,?,?,?,?,?,?)
              ON CONFLICT(source) DO UPDATE SET last_attempt_at_utc=excluded.last_attempt_at_utc,
              last_success_at_utc=excluded.last_success_at_utc,status=excluded.status,
              consecutive_failures=excluded.consecutive_failures,latency_ms=excluded.latency_ms,
              detail=excluded.detail,boot_id=excluded.boot_id""",
              (source,attempted_at,success,status,failures,latency_ms,(detail or "")[:500],self.boot_id))
            self.db.commit()

    def import_candidate(self, row: dict[str, Any]) -> bool:
        candidate_id=str(row.get("candidateId") or "")
        decision=row.get("decisionAt") or row.get("at")
        if not candidate_id or not isinstance(decision,(int,float)):
            return False
        from datetime import datetime, timezone
        decision_at=datetime.fromtimestamp(decision/1000,tz=timezone.utc).isoformat().replace("+00:00","Z")
        with self._lock:
            cursor=self.db.execute("INSERT OR IGNORE INTO candidate_imports VALUES (?,?,?,?,?,?,?)",
              (candidate_id,row.get("candidateKey"),row.get("episodeId"),decision_at,row.get("configHash"),iso_utc(),canonical_json(row)))
            self.db.commit()
        return cursor.rowcount==1

