from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .models import Event, Observation
from .util import canonical_json, iso_utc, sha256_text, stable_id


class Storage:
    def __init__(self, data_dir: Path, boot_id: str, migrations_dir: Path, read_only: bool = False) -> None:
        self.data_dir = data_dir
        self.read_only = read_only
        if not read_only:self.data_dir.mkdir(parents=True, exist_ok=True)
        self.boot_id = boot_id
        self.db_path = data_dir / "market_intelligence.sqlite3"
        self.export_dir = data_dir / "exports"
        if not read_only:self.export_dir.mkdir(exist_ok=True)
        self._lock = threading.Lock()
        if read_only:
            self.db = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro",uri=True,timeout=10,check_same_thread=False)
        else:self.db = sqlite3.connect(self.db_path, timeout=10, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if read_only:
            self.db.execute("PRAGMA query_only=ON")
            return
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        for migration in sorted(migrations_dir.glob("*.sql")):
            self.db.executescript(migration.read_text("utf-8"))
        self.db.execute("INSERT OR IGNORE INTO boots VALUES (?,?,?)", (boot_id, iso_utc(), __version__))
        self.db.commit()

    def close(self) -> None:
        if not self.read_only:self.db.commit()
        self.db.close()

    def _append(self, kind: str, value: dict[str, Any], at: str) -> None:
        hour = at[:13].replace("T", "-").replace(":", "")
        target = self.export_dir / f"{kind}-{hour}.jsonl"
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_json({"boot_id": self.boot_id, **value}) + "\n")

    def upsert_event(self, event: Event) -> str:
        payload = event.as_dict()
        # Retrieval timestamps change on every poll and are not publisher revisions.
        # Hash only substantive source facts so unchanged events do not retrigger briefings.
        content_hash = sha256_text(canonical_json({
          "source":event.source,"event_type":event.event_type,"title":event.title,
          "scheduled_at_utc":event.scheduled_at_utc,"publisher_time_utc":event.publisher_time_utc,
          "verification_status":event.verification_status,"publisher":event.publisher,
          "source_url":event.source_url,"payload":event.payload,
        }))
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
            neutral={"DISABLED","BUDGET_LIMIT","STALE_INPUT","QUEUE_FULL"}
            failures=0 if status=="OK" else int(old["consecutive_failures"] if old else 0) if status in neutral else int(old["consecutive_failures"] if old else 0)+1
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

    def build_briefing_snapshot(self, now: datetime | None = None) -> dict[str, Any]:
        """Build a compact, causal snapshot from data already committed by collectors."""
        now = now or datetime.now(timezone.utc)
        captured = iso_utc(now)
        recent_news_after = iso_utc(now - timedelta(hours=24))
        upcoming_before = iso_utc(now + timedelta(days=14))
        with self._lock:
            calendar_rows = self.db.execute("""SELECT event_id,event_type,title,scheduled_at_utc,
                available_to_system_at_utc,verification_status,publisher,source_url,payload_json
              FROM events WHERE scheduled_at_utc>=? AND scheduled_at_utc<=?
                AND verification_status LIKE 'VERIFIED%'
              ORDER BY scheduled_at_utc LIMIT 24""", (captured, upcoming_before)).fetchall()
            news_rows = self.db.execute("""SELECT event_id,event_type,title,publisher_time_utc,
                available_to_system_at_utc,verification_status,publisher,source_url
              FROM events WHERE event_type='UNSCHEDULED_NEWS' AND available_to_system_at_utc>=?
                AND verification_status='VERIFIED_TRUSTED_FEED'
              ORDER BY available_to_system_at_utc DESC LIMIT 20""", (recent_news_after,)).fetchall()
            observation_rows = self.db.execute("""SELECT observation_id,source,metric,instrument,value_num,unit,
                status,observed_at_utc,available_to_system_at_utc,source_url,payload_json
              FROM observations ORDER BY available_to_system_at_utc DESC LIMIT 200""").fetchall()
            health_rows = self.db.execute("""SELECT source,status,last_attempt_at_utc,last_success_at_utc,
                consecutive_failures,latency_ms,detail FROM source_health ORDER BY source""").fetchall()

        latest: dict[tuple[str, str | None], sqlite3.Row] = {}
        wanted = {
            ("linear_breadth", None), ("funding_rate", "BTCUSDT"),
            ("open_interest", "BTCUSDT"), ("mark_index_basis", "BTCUSDT"),
            ("return_24h", "BTCUSDT"), ("funding_rate", "ETHUSDT"),
            ("open_interest", "ETHUSDT"), ("mark_index_basis", "ETHUSDT"),
            ("return_24h", "ETHUSDT"), ("eth_btc_relative_price", "ETH/BTC"),
        }
        for row in observation_rows:
            key = (row["metric"], row["instrument"])
            if key in wanted and key not in latest:
                latest[key] = row

        observations=[];missing=[]
        for metric, instrument in sorted(wanted, key=lambda item: (item[0], item[1] or "")):
            row=latest.get((metric,instrument))
            field=f"{instrument or 'market'}:{metric}"
            if row is None:
                missing.append(field);continue
            age=max(0.0,(now-_parse_utc(row["available_to_system_at_utc"])).total_seconds())
            observations.append({
                "evidence_id":row["observation_id"],"source":row["source"],"metric":metric,
                "instrument":instrument,"value":row["value_num"],"unit":row["unit"],
                "status":row["status"],"observed_at_utc":row["observed_at_utc"],
                "available_to_system_at_utc":row["available_to_system_at_utc"],
                "age_seconds":round(age,3),"source_url":row["source_url"],
            })

        calendar=[]
        for row in calendar_rows:
            payload=json.loads(row["payload_json"])
            assumed=row["verification_status"]=="VERIFIED_DATE_STANDARD_TIME_ASSUMPTION"
            calendar.append({
                "evidence_id":row["event_id"],"event_type":row["event_type"],"title":row["title"],
                "scheduled_at_utc":row["scheduled_at_utc"],"available_to_system_at_utc":row["available_to_system_at_utc"],
                "publisher":row["publisher"],"source_url":row["source_url"],
                "quality":"OFFICIAL_DATE_CLOCK_ASSUMED_NOT_CONFIRMED" if assumed else "VERIFIED_OFFICIAL_SOURCE",
                "clock_assumption":payload.get("local_time_assumption") if assumed else None,
            })
        news=[{
            "evidence_id":row["event_id"],"event_type":row["event_type"],"title":row["title"],
            "publisher_time_utc":row["publisher_time_utc"],
            "available_to_system_at_utc":row["available_to_system_at_utc"],
            "publisher":row["publisher"],"source_url":row["source_url"],
            "quality":row["verification_status"],
        } for row in news_rows]
        health=[dict(row) for row in health_rows]
        watermark=max(
            [captured]+[x["available_to_system_at_utc"] for x in observations]
            +[x["available_to_system_at_utc"] for x in calendar]
            +[x["available_to_system_at_utc"] for x in news]
        )
        return {
            "snapshot_schema":"MARKET_BRIEFING_INPUT_V1","captured_at_utc":captured,
            "watermark_utc":watermark,"calendar":calendar,"verified_news":news,
            "market_observations":observations,"source_health":health,
            "quality":{"missing_fields":missing,"news_coverage":"CONFIGURED_TRUSTED_FEEDS_ONLY",
              "calendar_completeness":"CONFIGURED_OFFICIAL_SOURCES_ONLY",
              "fomc_clock_note":"Meeting dates are official; stored decision/press clock times are assumptions until independently verified."},
        }

    def latest_briefing_request(self) -> sqlite3.Row | None:
        with self._lock:
            return self.db.execute("SELECT * FROM briefings ORDER BY created_at_utc DESC LIMIT 1").fetchone()

    def count_briefing_requests_since(self, since_utc: str) -> int:
        with self._lock:
            return int(self.db.execute("SELECT count(*) FROM briefings WHERE created_at_utc>=?",(since_utc,)).fetchone()[0])

    def has_meaningful_event_since(self, since_utc: str | None) -> bool:
        if not since_utc:
            snapshot=self.build_briefing_snapshot()
            return bool(snapshot["calendar"] or snapshot["verified_news"])
        with self._lock:
            row=self.db.execute("""SELECT 1 FROM events WHERE first_seen_at_utc>?
              AND verification_status LIKE 'VERIFIED%'
              AND (event_type='UNSCHEDULED_NEWS' OR scheduled_at_utc IS NOT NULL) LIMIT 1""",(since_utc,)).fetchone()
            revision=self.db.execute("""SELECT 1 FROM schedule_revisions r JOIN events e ON e.event_id=r.event_id
              WHERE r.retrieved_at_utc>? AND e.verification_status LIKE 'VERIFIED%' LIMIT 1""",(since_utc,)).fetchone()
        return row is not None or revision is not None

    def create_briefing(self, snapshot: dict[str, Any], trigger_reason: str, model: str,
                        prompt_version: str, schema_version: str, status: str = "QUEUED",
                        error_type: str | None = None, error_detail: str | None = None) -> str | None:
        snapshot_json=canonical_json(snapshot);snapshot_hash=sha256_text(snapshot_json);now=iso_utc()
        briefing_id=stable_id("brf",snapshot_hash,prompt_version,model)
        with self._lock:
            cursor=self.db.execute("""INSERT OR IGNORE INTO briefings
              (briefing_id,trigger_reason,status,authoritative,prompt_version,schema_version,model,
               input_snapshot_hash,input_watermark_utc,input_snapshot_json,first_seen_at_utc,
               requested_at_utc,attempt_count,error_type,error_detail,boot_id,created_at_utc,updated_at_utc)
              VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (briefing_id,trigger_reason,status,0,prompt_version,schema_version,model,snapshot_hash,
               snapshot["watermark_utc"],snapshot_json,now,now if status=="QUEUED" else None,0,
               error_type,(error_detail or "")[:500] or None,self.boot_id,now,now))
            self.db.commit()
            if cursor.rowcount:
                self._append("briefings",{"briefing_id":briefing_id,"status":status,"authoritative":False,
                  "trigger_reason":trigger_reason,"prompt_version":prompt_version,"schema_version":schema_version,
                  "model":model,"input_snapshot_hash":snapshot_hash,"input_watermark_utc":snapshot["watermark_utc"],
                  "input_snapshot":snapshot,
                  "first_seen_at_utc":now,"requested_at_utc":now if status=="QUEUED" else None,
                  "error_type":error_type,"error_detail":error_detail},now)
        return briefing_id if cursor.rowcount else None

    def pending_briefings(self, limit: int) -> list[str]:
        with self._lock:
            rows=self.db.execute("SELECT briefing_id FROM briefings WHERE status IN ('QUEUED','RUNNING') ORDER BY created_at_utc LIMIT ?",(limit,)).fetchall()
        return [row["briefing_id"] for row in rows]

    def briefing_input(self, briefing_id: str) -> dict[str, Any] | None:
        with self._lock:
            row=self.db.execute("SELECT input_snapshot_json FROM briefings WHERE briefing_id=?",(briefing_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def mark_briefing_running(self, briefing_id: str, attempt_count: int) -> None:
        with self._lock:
            self.db.execute("UPDATE briefings SET status='RUNNING',attempt_count=?,updated_at_utc=? WHERE briefing_id=?",
              (attempt_count,iso_utc(),briefing_id));self.db.commit()

    def complete_briefing(self, briefing_id: str, result: dict[str, Any]) -> None:
        now=iso_utc();output=result.get("output");output_json=canonical_json(output) if output is not None else None
        content_hash=sha256_text(output_json) if output_json else None
        usage=result.get("usage") or {}
        diagnostics=result.get("diagnostics") or {}
        with self._lock:
            row=self.db.execute("SELECT trigger_reason,prompt_version,schema_version,model,input_snapshot_hash,input_watermark_utc,first_seen_at_utc,requested_at_utc,attempt_count FROM briefings WHERE briefing_id=?",(briefing_id,)).fetchone()
            if row is None:return
            self.db.execute("""UPDATE briefings SET status=?,completed_at_utc=?,available_to_system_at_utc=?,
              latency_ms=?,input_tokens=?,output_tokens=?,total_tokens=?,estimated_cost_usd=?,error_type=?,
              error_detail=?,output_json=?,content_hash=?,updated_at_utc=? WHERE briefing_id=?""",
              (result["status"],now,now,result.get("latency_ms"),usage.get("promptTokenCount"),
               usage.get("candidatesTokenCount"),usage.get("totalTokenCount"),None,result.get("error_type"),
               (result.get("error_detail") or "")[:500] or None,output_json,content_hash,now,briefing_id))
            self.db.execute("""INSERT INTO briefing_diagnostics
              (briefing_id,finish_reason,usage_json,diagnostics_json,updated_at_utc)
              VALUES (?,?,?,?,?) ON CONFLICT(briefing_id) DO UPDATE SET
              finish_reason=excluded.finish_reason,usage_json=excluded.usage_json,
              diagnostics_json=excluded.diagnostics_json,updated_at_utc=excluded.updated_at_utc""",
              (briefing_id,result.get("finish_reason"),canonical_json(usage),canonical_json(diagnostics),now))
            self.db.commit()
            self._append("briefings",{"briefing_id":briefing_id,"status":result["status"],"authoritative":False,
              "trigger_reason":row["trigger_reason"],"prompt_version":row["prompt_version"],
              "schema_version":row["schema_version"],"model":row["model"],
              "input_snapshot_hash":row["input_snapshot_hash"],"input_watermark_utc":row["input_watermark_utc"],
              "first_seen_at_utc":row["first_seen_at_utc"],"requested_at_utc":row["requested_at_utc"],
              "completed_at_utc":now,"available_to_system_at_utc":now,"latency_ms":result.get("latency_ms"),
              "usage":usage,"estimated_cost_usd":None,"attempt_count":row["attempt_count"],
              "finish_reason":result.get("finish_reason"),"response_diagnostics":diagnostics,
              "error_type":result.get("error_type"),"error_detail":result.get("error_detail"),
              "content_hash":content_hash,"output":output},now)

    def service_status(self) -> dict[str, Any]:
        with self._lock:
            counts={table:int(self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
              for table in ("events","observations","briefings")}
            last_event=self.db.execute("SELECT max(available_to_system_at_utc) FROM events").fetchone()[0]
            last_observation=self.db.execute("SELECT max(available_to_system_at_utc) FROM observations").fetchone()[0]
            latest=self.db.execute("SELECT briefing_id,status,model,requested_at_utc,available_to_system_at_utc,error_type FROM briefings ORDER BY created_at_utc DESC LIMIT 1").fetchone()
            health=[dict(row) for row in self.db.execute("SELECT * FROM source_health ORDER BY source").fetchall()]
        return {"database":str(self.db_path),"counts":counts,"latest_event_at_utc":last_event,
          "latest_observation_at_utc":last_observation,"latest_briefing":dict(latest) if latest else None,
          "source_health":health}

    def list_briefings(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._lock:
            rows=self.db.execute("""SELECT b.briefing_id,b.status,b.trigger_reason,b.model,b.prompt_version,
              b.schema_version,b.input_watermark_utc,b.requested_at_utc,b.available_to_system_at_utc,
              b.latency_ms,b.input_tokens,b.output_tokens,b.total_tokens,b.error_type,b.error_detail,b.output_json,
              d.finish_reason,d.usage_json,d.diagnostics_json
              FROM briefings b LEFT JOIN briefing_diagnostics d ON d.briefing_id=b.briefing_id
              ORDER BY b.created_at_utc DESC LIMIT ?""",(max(1,min(limit,50)),)).fetchall()
        out=[]
        for row in rows:
            item=dict(row);raw=item.pop("output_json");usage=item.pop("usage_json")
            diagnostics=item.pop("diagnostics_json");item["output"]=json.loads(raw) if raw else None
            item["usage"]=json.loads(usage) if usage else {};item["response_diagnostics"]=json.loads(diagnostics) if diagnostics else {}
            out.append(item)
        return out


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z","+00:00"))

