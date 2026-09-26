from __future__ import annotations

import asyncio
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .storage import Storage
from .util import canonical_json, iso_utc


PROMPT_VERSION="MARKET_BRIEFING_PROMPT_V1"
SCHEMA_VERSION="MARKET_BRIEFING_V1"
FIXED_MODEL="gemini-3.8-flash"


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0.0


class ObserverError(Exception):
    def __init__(self, error_type: str, detail: str, retryable: bool = False) -> None:
        super().__init__(detail);self.error_type=error_type;self.detail=detail;self.retryable=retryable


class GeminiObserver:
    """Scheduled, shadow-only observer. No output is consumed by any trading process."""

    def __init__(self, storage: Storage, enabled: bool = False, interval_seconds: int = 1800,
                 event_trigger: bool = True) -> None:
        self.storage=storage;self.enabled=enabled;self.interval_seconds=max(300,interval_seconds)
        self.event_trigger=event_trigger;self.api_key=os.getenv("GEMINI_API_KEY","")
        requested_model=os.getenv("GEMINI_MODEL",FIXED_MODEL)
        self.model=FIXED_MODEL;self.model_config_valid=requested_model==FIXED_MODEL
        self.timeout=max(2.0,min(float(os.getenv("GEMINI_TIMEOUT_SECONDS","8")),30.0))
        self.threshold=max(1,min(int(os.getenv("GEMINI_FAILURE_THRESHOLD","3")),10))
        self.open_seconds=max(60,min(int(os.getenv("GEMINI_CIRCUIT_OPEN_SECONDS","900")),3600))
        self.max_attempts=max(1,min(int(os.getenv("GEMINI_MAX_ATTEMPTS","2")),2))
        self.max_output_tokens=max(256,min(int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS","1200")),2048))
        self.max_input_chars=max(10000,min(int(os.getenv("GEMINI_MAX_INPUT_CHARS","60000")),100000))
        self.max_input_age=max(300,min(int(os.getenv("GEMINI_MAX_INPUT_AGE_SECONDS","1200")),7200))
        queue_size=max(1,min(int(os.getenv("GEMINI_MAX_QUEUE","2")),2))
        self.queue: asyncio.Queue[str | None]=asyncio.Queue(maxsize=queue_size);self.circuit=Circuit()
        self._queued_ids:set[str]=set()

    def recover_pending(self) -> int:
        recovered=0
        for briefing_id in self.storage.pending_briefings(self.queue.maxsize):
            if briefing_id not in self._queued_ids and not self.queue.full():
                self.queue.put_nowait(briefing_id);self._queued_ids.add(briefing_id);recovered+=1
        return recovered

    def schedule_after_collection(self) -> str | None:
        attempted=iso_utc();started=time.monotonic()
        if not self.enabled:
            self.storage.health("gemini_observer","DISABLED",attempted,0,"MI_GEMINI_ENABLED is false; zero-cost default")
            return None
        latest=self.storage.latest_briefing_request();now=datetime.now(timezone.utc)
        latest_created=latest["created_at_utc"] if latest else None
        interval_due=not latest_created or _parse_utc(latest_created)<=now-timedelta(seconds=self.interval_seconds)
        event_due=self.event_trigger and self.storage.has_meaningful_event_since(latest_created)
        if not (interval_due or event_due):return None
        if self.storage.count_briefing_requests_since(iso_utc(now-timedelta(minutes=30)))>=2:
            self.storage.health("gemini_observer","BUDGET_LIMIT",attempted,0,"maximum 2 briefing records per 30 minutes")
            return None
        snapshot=self.storage.build_briefing_snapshot(now)
        trigger="VERIFIED_NEW_EVENT" if event_due else "SCHEDULED_INTERVAL"
        observations=snapshot["market_observations"]
        stale=(not observations or any(item["age_seconds"]>self.max_input_age for item in observations)
               or bool(snapshot["quality"]["missing_fields"]))
        if stale:
            briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION,
              "ABSTAIN_STALE_INPUT","STALE_OR_MISSING_INPUT","Fresh complete Bybit core snapshot is required")
            self.storage.health("gemini_observer","STALE_INPUT",attempted,int((time.monotonic()-started)*1000),
              "briefing abstained before API request")
            return briefing_id
        if len(canonical_json(snapshot))>self.max_input_chars:
            briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION,
              "ABSTAIN_INPUT_TOO_LARGE","INPUT_BUDGET","compact input exceeded configured character budget")
            self.storage.health("gemini_observer","BUDGET_LIMIT",attempted,0,"input character budget exceeded")
            return briefing_id
        if not self.model_config_valid:
            briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION,
              "DISABLED_MODEL_MISMATCH","MODEL_MISMATCH",f"GEMINI_MODEL must be {FIXED_MODEL}")
            self.storage.health("gemini_observer","DISABLED",attempted,0,"fixed model mismatch")
            return briefing_id
        if not self.api_key:
            briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION,
              "DISABLED_NO_KEY","NO_API_KEY","GEMINI_API_KEY is not configured")
            self.storage.health("gemini_observer","DISABLED",attempted,0,"enabled but GEMINI_API_KEY is absent")
            return briefing_id
        if self.queue.full():
            briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION,
              "DROPPED_QUEUE_FULL","QUEUE_FULL","bounded Gemini queue is full")
            self.storage.health("gemini_observer","QUEUE_FULL",attempted,0,"request not sent")
            return briefing_id
        briefing_id=self.storage.create_briefing(snapshot,trigger,self.model,PROMPT_VERSION,SCHEMA_VERSION)
        if briefing_id:
            self.queue.put_nowait(briefing_id);self._queued_ids.add(briefing_id)
        return briefing_id

    async def run(self) -> None:
        self.recover_pending()
        while True:
            briefing_id=await self.queue.get()
            try:
                if briefing_id is None:return
                self._queued_ids.discard(briefing_id)
                await self._process(briefing_id)
            finally:self.queue.task_done()

    async def close(self) -> None:
        if not self.queue.full():self.queue.put_nowait(None)

    async def _process(self, briefing_id: str) -> None:
        snapshot=self.storage.briefing_input(briefing_id)
        if snapshot is None:return
        result=await self.observe(snapshot,briefing_id)
        self.storage.complete_briefing(briefing_id,result)
        status="OK" if result["status"]=="OK" else result["status"]
        self.storage.health("gemini_observer",status,iso_utc(),int(result.get("latency_ms") or 0),
          result.get("error_detail"))

    async def observe(self, payload: dict[str,Any], briefing_id: str = "direct") -> dict[str,Any]:
        loop=asyncio.get_running_loop();started=time.monotonic()
        if loop.time()<self.circuit.open_until:
            return _failure("CIRCUIT_OPEN","CIRCUIT_OPEN","circuit breaker is open",started)
        last_error: ObserverError | None=None
        for attempt in range(1,self.max_attempts+1):
            if briefing_id!="direct":self.storage.mark_briefing_running(briefing_id,attempt)
            try:
                response=await asyncio.wait_for(asyncio.to_thread(self._request,payload),timeout=self.timeout)
                self.circuit.failures=0
                return {"status":"OK","authoritative":False,"output":response["output"],
                  "usage":response.get("usage") or {},"latency_ms":int((time.monotonic()-started)*1000)}
            except asyncio.TimeoutError:
                last_error=ObserverError("TIMEOUT",f"hard deadline of {self.timeout:g}s exceeded",True)
            except ObserverError as error:last_error=error
            except Exception as error:last_error=ObserverError("NETWORK_ERROR",str(error)[:300],True)
            if not last_error.retryable or attempt>=self.max_attempts:break
            await asyncio.sleep(min(0.25*attempt,0.5))
        self.circuit.failures+=1
        if self.circuit.failures>=self.threshold:self.circuit.open_until=loop.time()+self.open_seconds
        assert last_error is not None
        return _failure(last_error.error_type,last_error.error_type,last_error.detail,started)

    def _request(self,payload: dict[str,Any]) -> dict[str,Any]:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        prompt={
          "prompt_version":PROMPT_VERSION,"role":"shadow_only_non_authoritative_market_briefing",
          "rules":[
            "Use only supplied evidence. Never claim live or real-time knowledge beyond its timestamps.",
            "Separate facts from interpretations and cite supplied evidence_id values.",
            "Abstain when inputs are missing or stale. Do not invent an event, value, source, or clock time.",
            "FOMC entries marked clock assumed have official dates but unverified clock times; say so.",
            "Do not forecast prices, recommend trades, control trading, or propose automatic rule learning.",
          ],
          "required_output_schema":SCHEMA_VERSION,"input":payload,
        }
        body=json.dumps({
          "contents":[{"parts":[{"text":canonical_json(prompt)}]}],
          "generationConfig":{"temperature":0,"maxOutputTokens":self.max_output_tokens,
            "responseFormat":{"text":{"mimeType":"APPLICATION_JSON","schema":_response_schema()}}},
        }).encode("utf-8")
        request=urllib.request.Request(url,data=body,headers={"Content-Type":"application/json",
          "x-goog-api-key":self.api_key},method="POST")
        try:
            with urllib.request.urlopen(request,timeout=self.timeout) as response:
                raw=response.read(1_000_001)
        except urllib.error.HTTPError as error:
            detail=error.read(4096).decode("utf-8","replace")
            kind="QUOTA_429" if error.code==429 else f"API_HTTP_{error.code}"
            raise ObserverError(kind,detail[:300],error.code in {429,500,502,503,504}) from error
        except urllib.error.URLError as error:
            raise ObserverError("NETWORK_ERROR",str(error.reason)[:300],True) from error
        if len(raw)>1_000_000:raise ObserverError("RESPONSE_TOO_LARGE","response exceeded 1,000,000 bytes")
        try:
            data=json.loads(raw);text=data["candidates"][0]["content"]["parts"][0]["text"]
            output=json.loads(text);_validate_output(output)
        except (KeyError,IndexError,TypeError,ValueError,json.JSONDecodeError) as error:
            raise ObserverError("MALFORMED_RESPONSE",str(error)[:300]) from error
        return {"output":output,"usage":data.get("usageMetadata") or {}}


def _failure(status: str,error_type: str,detail: str,started: float) -> dict[str,Any]:
    return {"status":status,"authoritative":False,"error_type":error_type,
      "error_detail":detail[:300],"latency_ms":int((time.monotonic()-started)*1000),"usage":{}}


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z","+00:00"))


def _response_schema() -> dict[str,Any]:
    evidence_item={"type":"object","properties":{"statement":{"type":"string"},
      "evidence_refs":{"type":"array","items":{"type":"string"}}},"required":["statement","evidence_refs"]}
    return {"type":"object","properties":{
      "briefing_schema_version":{"type":"string"},"summary":{"type":"string"},
      "abstain":{"type":"boolean"},"trading_authority":{"type":"string","enum":["NONE"]},
      "facts":{"type":"array","items":evidence_item},
      "interpretations":{"type":"array","items":evidence_item},
      "upcoming_verified_events":{"type":"array","items":{"type":"object","properties":{
        "event_type":{"type":"string"},"scheduled_at_utc":{"type":"string"},
        "timing_quality":{"type":"string"},"evidence_ref":{"type":"string"}},
        "required":["event_type","scheduled_at_utc","timing_quality","evidence_ref"]}},
      "market_stress_risk_flags":{"type":"array","items":{"type":"object","properties":{
        "flag":{"type":"string"},"severity":{"type":"string"},
        "evidence_refs":{"type":"array","items":{"type":"string"}}},
        "required":["flag","severity","evidence_refs"]}},
      "stale_or_missing_fields":{"type":"array","items":{"type":"string"}},
      "uncertainties":{"type":"array","items":{"type":"string"}},
    },"required":["briefing_schema_version","summary","abstain","trading_authority","facts",
      "interpretations","upcoming_verified_events","market_stress_risk_flags",
      "stale_or_missing_fields","uncertainties"]}


def _validate_output(value: Any) -> None:
    if not isinstance(value,dict):raise ValueError("briefing must be an object")
    required={"briefing_schema_version","summary","abstain","trading_authority","facts","interpretations",
      "upcoming_verified_events","market_stress_risk_flags","stale_or_missing_fields","uncertainties"}
    if not required.issubset(value):raise ValueError(f"missing keys: {sorted(required-set(value))}")
    if value["briefing_schema_version"]!=SCHEMA_VERSION:raise ValueError("schema version mismatch")
    if value["trading_authority"]!="NONE":raise ValueError("trading_authority must be NONE")
    if not isinstance(value["summary"],str) or not isinstance(value["abstain"],bool):raise ValueError("invalid summary/abstain")
    for name in ("facts","interpretations"):
        if not isinstance(value[name],list):raise ValueError(f"{name} must be a list")
        for item in value[name]:
            if not isinstance(item,dict) or not isinstance(item.get("statement"),str) or not _string_list(item.get("evidence_refs")):
                raise ValueError(f"invalid {name} item")
    for name in ("upcoming_verified_events","market_stress_risk_flags"):
        if not isinstance(value[name],list) or not all(isinstance(item,dict) for item in value[name]):
            raise ValueError(f"invalid {name}")
    for item in value["upcoming_verified_events"]:
        if not all(isinstance(item.get(key),str) for key in ("event_type","scheduled_at_utc","timing_quality","evidence_ref")):
            raise ValueError("invalid upcoming_verified_events item")
    for item in value["market_stress_risk_flags"]:
        if not isinstance(item.get("flag"),str) or not isinstance(item.get("severity"),str) or not _string_list(item.get("evidence_refs")):
            raise ValueError("invalid market_stress_risk_flags item")
    for name in ("stale_or_missing_fields","uncertainties"):
        if not _string_list(value[name],allow_empty=True):raise ValueError(f"invalid {name}")


def _string_list(value: Any, allow_empty: bool = False) -> bool:
    return isinstance(value,list) and (allow_empty or bool(value)) and all(isinstance(item,str) for item in value)
