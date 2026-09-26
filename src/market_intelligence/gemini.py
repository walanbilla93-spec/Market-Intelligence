from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .storage import Storage
from .util import canonical_json, iso_utc


PROMPT_VERSION = "MARKET_BRIEFING_PROMPT_V3"
SCHEMA_VERSION = "MARKET_BRIEFING_V2"
FIXED_MODEL = "gemini-3.8-flash"
RETRYABLE_HTTP_CODES = {500, 502, 503, 504}
USAGE_FIELDS = {"promptTokenCount", "cachedContentTokenCount", "candidatesTokenCount",
                "toolUsePromptTokenCount", "thoughtsTokenCount", "totalTokenCount"}


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0.0


class ObserverError(Exception):
    def __init__(self, error_type: str, detail: str, retryable: bool = False, *,
                 usage: dict[str, int] | None = None, finish_reason: str | None = None,
                 diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail
        self.retryable = retryable
        self.usage = usage or {}
        self.finish_reason = finish_reason
        self.diagnostics = diagnostics or {}


class GeminiObserver:
    """Scheduled, shadow-only observer. No output is consumed by any trading process."""

    def __init__(self, storage: Storage, enabled: bool = False, interval_seconds: int = 1800,
                 event_trigger: bool = True) -> None:
        self.storage = storage
        self.enabled = enabled
        self.interval_seconds = max(300, interval_seconds)
        self.event_trigger = event_trigger
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        requested_model = os.getenv("GEMINI_MODEL", FIXED_MODEL)
        self.model = FIXED_MODEL
        self.model_config_valid = requested_model == FIXED_MODEL
        self.timeout = max(2.0, min(float(os.getenv("GEMINI_TIMEOUT_SECONDS", "30")), 30.0))
        self.total_timeout = max(self.timeout,
                                 min(float(os.getenv("GEMINI_TOTAL_TIMEOUT_SECONDS", "45")), 45.0))
        self.threshold = max(1, min(int(os.getenv("GEMINI_FAILURE_THRESHOLD", "3")), 10))
        self.open_seconds = max(60, min(int(os.getenv("GEMINI_CIRCUIT_OPEN_SECONDS", "900")), 3600))
        self.max_attempts = max(1, min(int(os.getenv("GEMINI_MAX_ATTEMPTS", "2")), 2))
        self.max_output_tokens = max(4096, min(int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "8192")), 16384))
        self.routine_thinking_level = os.getenv("GEMINI_ROUTINE_THINKING_LEVEL", "medium").lower()
        self.event_thinking_level = os.getenv("GEMINI_EVENT_THINKING_LEVEL", "high").lower()
        self.thinking_config_valid = all(level in {"low", "medium", "high"} for level in
                                         (self.routine_thinking_level, self.event_thinking_level))
        self.max_input_chars = max(8000, min(int(os.getenv("GEMINI_MAX_INPUT_CHARS", "24000")), 48000))
        self.max_request_bytes = max(20000, min(int(os.getenv("GEMINI_MAX_REQUEST_BYTES", "65536")), 100000))
        self.max_input_age = max(300, min(int(os.getenv("GEMINI_MAX_INPUT_AGE_SECONDS", "1200")), 7200))
        queue_size = max(1, min(int(os.getenv("GEMINI_MAX_QUEUE", "2")), 2))
        self.queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=queue_size)
        self.circuit = Circuit()
        self._queued_ids: set[str] = set()

    def recover_pending(self) -> int:
        recovered = 0
        for briefing_id in self.storage.pending_briefings(self.queue.maxsize):
            if briefing_id not in self._queued_ids and not self.queue.full():
                self.queue.put_nowait(briefing_id)
                self._queued_ids.add(briefing_id)
                recovered += 1
        return recovered

    def schedule_after_collection(self) -> str | None:
        attempted = iso_utc()
        started = time.monotonic()
        if not self.enabled:
            self.storage.health("gemini_observer", "DISABLED", attempted, 0,
                                "MI_GEMINI_ENABLED is false; zero-cost default")
            return None
        latest = self.storage.latest_briefing_request()
        now = datetime.now(timezone.utc)
        latest_created = latest["created_at_utc"] if latest else None
        interval_due = not latest_created or _parse_utc(latest_created) <= now - timedelta(seconds=self.interval_seconds)
        event_due = self.event_trigger and self.storage.has_meaningful_event_since(latest_created)
        if not (interval_due or event_due):
            return None
        if self.storage.count_briefing_requests_since(iso_utc(now - timedelta(minutes=30))) >= 1:
            self.storage.health("gemini_observer", "BUDGET_LIMIT", attempted, 0,
                                "maximum 1 briefing request per 30 minutes")
            return None
        snapshot = self.storage.build_briefing_snapshot(now)
        trigger = "VERIFIED_NEW_EVENT" if event_due else "SCHEDULED_INTERVAL"
        observations = snapshot["market_observations"]
        stale = (not observations or any(item["age_seconds"] > self.max_input_age for item in observations)
                 or bool(snapshot["quality"]["missing_fields"]))
        if stale:
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "ABSTAIN_STALE_INPUT", "STALE_OR_MISSING_INPUT",
                "Fresh complete Bybit core snapshot is required")
            self.storage.health("gemini_observer", "STALE_INPUT", attempted,
                int((time.monotonic() - started) * 1000), "briefing abstained before API request")
            return briefing_id
        if len(canonical_json(snapshot)) > self.max_input_chars:
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "ABSTAIN_INPUT_TOO_LARGE", "INPUT_BUDGET",
                "compact input exceeded configured character budget")
            self.storage.health("gemini_observer", "BUDGET_LIMIT", attempted, 0,
                                "input character budget exceeded")
            return briefing_id
        if not self.model_config_valid:
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "DISABLED_MODEL_MISMATCH", "MODEL_MISMATCH",
                f"GEMINI_MODEL must be {FIXED_MODEL}")
            self.storage.health("gemini_observer", "DISABLED", attempted, 0, "fixed model mismatch")
            return briefing_id
        if not self.thinking_config_valid:
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "DISABLED_THINKING_CONFIG", "THINKING_CONFIG",
                "thinking level must be low, medium, or high")
            self.storage.health("gemini_observer", "DISABLED", attempted, 0, "invalid thinking config")
            return briefing_id
        if not self.api_key:
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "DISABLED_NO_KEY", "NO_API_KEY", "GEMINI_API_KEY is not configured")
            self.storage.health("gemini_observer", "DISABLED", attempted, 0,
                                "enabled but GEMINI_API_KEY is absent")
            return briefing_id
        if self.queue.full():
            briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION,
                SCHEMA_VERSION, "DROPPED_QUEUE_FULL", "QUEUE_FULL", "bounded Gemini queue is full")
            self.storage.health("gemini_observer", "QUEUE_FULL", attempted, 0, "request not sent")
            return briefing_id
        briefing_id = self.storage.create_briefing(snapshot, trigger, self.model, PROMPT_VERSION, SCHEMA_VERSION)
        if briefing_id:
            self.queue.put_nowait(briefing_id)
            self._queued_ids.add(briefing_id)
        return briefing_id

    async def run(self) -> None:
        self.recover_pending()
        while True:
            briefing_id = await self.queue.get()
            try:
                if briefing_id is None:
                    return
                self._queued_ids.discard(briefing_id)
                await self._process(briefing_id)
            finally:
                self.queue.task_done()

    async def close(self) -> None:
        if not self.queue.full():
            self.queue.put_nowait(None)

    async def _process(self, briefing_id: str) -> None:
        snapshot = self.storage.briefing_input(briefing_id)
        if snapshot is None:
            return
        trigger = self.storage.briefing_trigger_reason(briefing_id)
        thinking_level = (self.event_thinking_level if trigger == "VERIFIED_NEW_EVENT"
                          else self.routine_thinking_level)
        result = await self.observe(snapshot, briefing_id, thinking_level)
        self.storage.complete_briefing(briefing_id, result)
        self.storage.health("gemini_observer", result["status"], iso_utc(),
                            int(result.get("latency_ms") or 0), result.get("error_detail"))

    async def observe(self, payload: dict[str, Any], briefing_id: str = "direct",
                      thinking_level: str | None = None) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        thinking_level = thinking_level or self.routine_thinking_level
        if loop.time() < self.circuit.open_until:
            return _failure("CIRCUIT_OPEN", "circuit breaker is open", started)
        deadline = loop.time() + self.total_timeout
        last_error: ObserverError | None = None
        attempts = 0
        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                last_error = ObserverError("TIMEOUT", f"total hard deadline of {self.total_timeout:g}s exceeded",
                                           diagnostics={"deadline_scope": "total"})
                break
            attempts = attempt
            if briefing_id != "direct":
                self.storage.mark_briefing_running(briefing_id, attempt)
            request_timeout = min(self.timeout, remaining)
            try:
                response = await asyncio.wait_for(
                    asyncio.to_thread(self._request, payload, request_timeout, thinking_level),
                    timeout=min(remaining, request_timeout + 0.5))
                self.circuit.failures = 0
                diagnostics = dict(response.get("diagnostics") or {})
                diagnostics["attempts"] = attempts
                diagnostics["thinking_level"] = thinking_level
                diagnostics["max_output_tokens"] = self.max_output_tokens
                return {"status": "OK", "authoritative": False, "output": response["output"],
                        "usage": response.get("usage") or {}, "finish_reason": response.get("finish_reason"),
                        "diagnostics": diagnostics, "latency_ms": int((time.monotonic() - started) * 1000)}
            except asyncio.TimeoutError:
                last_error = ObserverError("TIMEOUT", f"attempt deadline of {request_timeout:g}s exceeded",
                    diagnostics={"deadline_scope": "attempt", "retry_suppressed": "possible request still active"})
            except ObserverError as error:
                last_error = error
            except Exception as error:
                last_error = ObserverError("NETWORK_ERROR", type(error).__name__, True)
            if not last_error.retryable or attempt >= self.max_attempts:
                break
            backoff = min(0.25 * attempt, 0.5)
            if deadline - loop.time() <= backoff:
                last_error = ObserverError("TIMEOUT",
                    f"total hard deadline of {self.total_timeout:g}s exhausted before retry",
                    diagnostics={"deadline_scope": "total"})
                break
            await asyncio.sleep(backoff)
        self.circuit.failures += 1
        if self.circuit.failures >= self.threshold:
            self.circuit.open_until = loop.time() + self.open_seconds
        assert last_error is not None
        diagnostics = dict(last_error.diagnostics)
        diagnostics["attempts"] = attempts
        diagnostics["thinking_level"] = thinking_level
        diagnostics["max_output_tokens"] = self.max_output_tokens
        return _failure(last_error.error_type, last_error.detail, started, usage=last_error.usage,
                        finish_reason=last_error.finish_reason, diagnostics=diagnostics)

    def _request(self, payload: dict[str, Any], request_timeout: float | None = None,
                 thinking_level: str | None = None) -> dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        prompt = {"prompt_version": PROMPT_VERSION,
          "task": "Produce one concise, non-authoritative market briefing as schema-valid JSON.",
          "rules": ["Use only supplied evidence and cite evidence_id values.",
            "Separate facts from interpretations; abstain if evidence is insufficient.",
            "Never invent an event, value, source, or clock time.",
            "Treat FOMC clock-assumption labels as unverified clock times.",
            "Do not forecast prices, recommend trades, control trading, or propose rule learning.",
            "Stay within every schema length and item limit."],
          "required_output_schema": SCHEMA_VERSION, "input": payload}
        thinking_level = thinking_level or self.routine_thinking_level
        body = json.dumps({"contents": [{"parts": [{"text": canonical_json(prompt)}]}],
          "generationConfig": {"temperature": 0, "candidateCount": 1,
            "maxOutputTokens": self.max_output_tokens,
            "thinkingConfig": {"thinkingLevel": thinking_level},
            "responseFormat": {"text": {"mimeType": "APPLICATION_JSON", "schema": _response_schema()}}}},
          separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(body) > self.max_request_bytes:
            raise ObserverError("REQUEST_TOO_LARGE",
                f"encoded request exceeded {self.max_request_bytes} bytes",
                diagnostics={"request_bytes": len(body), "request_limit_bytes": self.max_request_bytes})
        request = urllib.request.Request(url, data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key}, method="POST")
        timeout = request_timeout if request_timeout is not None else self.timeout
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(1_000_001)
        except urllib.error.HTTPError as error:
            kind, detail = _classify_http_error(error)
            raise ObserverError(kind, detail, error.code in RETRYABLE_HTTP_CODES) from error
        except urllib.error.URLError as error:
            if isinstance(error.reason, (TimeoutError, socket.timeout)):
                raise ObserverError("TIMEOUT", "HTTP transport timed out", True) from error
            raise ObserverError("NETWORK_ERROR", type(error.reason).__name__, True) from error
        except (TimeoutError, socket.timeout) as error:
            raise ObserverError("TIMEOUT", "HTTP transport timed out", True) from error
        if len(raw) > 1_000_000:
            raise ObserverError("RESPONSE_TOO_LARGE", "response exceeded 1,000,000 bytes")
        return _parse_generate_content_response(raw)


def _failure(status: str, detail: str, started: float, *, usage: dict[str, int] | None = None,
             finish_reason: str | None = None, diagnostics: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"status": status, "authoritative": False, "error_type": status,
            "error_detail": detail[:300], "latency_ms": int((time.monotonic() - started) * 1000),
            "usage": usage or {}, "finish_reason": finish_reason, "diagnostics": diagnostics or {}}


def _parse_generate_content_response(raw: bytes) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {"response_bytes": len(raw)}
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        diagnostics["envelope_error"] = _json_error_summary(error)
        raise ObserverError("MALFORMED_RESPONSE", "response envelope was not valid JSON",
                            diagnostics=diagnostics) from error
    if not isinstance(data, dict):
        raise ObserverError("MALFORMED_RESPONSE", "response envelope must be an object",
                            diagnostics=diagnostics)
    usage = _safe_usage(data.get("usageMetadata"))
    candidates = data.get("candidates")
    diagnostics["candidate_count"] = len(candidates) if isinstance(candidates, list) else 0
    if not isinstance(candidates, list) or not candidates:
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and isinstance(feedback.get("blockReason"), str):
            diagnostics["prompt_block_reason"] = feedback["blockReason"]
        raise ObserverError("NO_CANDIDATE", "GenerateContent returned no candidate",
                            usage=usage, diagnostics=diagnostics)
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        raise ObserverError("MALFORMED_RESPONSE", "candidate must be an object",
                            usage=usage, diagnostics=diagnostics)
    finish_reason = candidate.get("finishReason") if isinstance(candidate.get("finishReason"), str) else None
    diagnostics["finish_message_present"] = bool(candidate.get("finishMessage"))
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text_parts = [part["text"] for part in parts or []
                  if isinstance(part, dict) and isinstance(part.get("text"), str)]
    text = "".join(text_parts)
    diagnostics["text_part_count"] = len(text_parts)
    diagnostics["candidate_text_chars"] = len(text)
    if finish_reason == "MAX_TOKENS":
        try:
            json.loads(text)
        except json.JSONDecodeError as error:
            diagnostics["candidate_json_error"] = _json_error_summary(error)
        raise ObserverError("OUTPUT_TRUNCATED", "finishReason=MAX_TOKENS; candidate rejected",
                            usage=usage, finish_reason=finish_reason, diagnostics=diagnostics)
    if finish_reason not in {None, "STOP"}:
        safe_reason = "".join(ch for ch in finish_reason if ch.isalnum() or ch == "_")[:80] or "UNKNOWN"
        raise ObserverError(f"FINISH_{safe_reason}", f"candidate stopped with finishReason={safe_reason}",
                            usage=usage, finish_reason=finish_reason, diagnostics=diagnostics)
    if not text_parts:
        raise ObserverError("MALFORMED_RESPONSE", "candidate contained no text parts", usage=usage,
                            finish_reason=finish_reason, diagnostics=diagnostics)
    try:
        output = json.loads(text)
        _validate_output(output)
    except json.JSONDecodeError as error:
        diagnostics["candidate_json_error"] = _json_error_summary(error)
        raise ObserverError("MALFORMED_RESPONSE",
            f"candidate JSON invalid at line {error.lineno} column {error.colno}", usage=usage,
            finish_reason=finish_reason, diagnostics=diagnostics) from error
    except (TypeError, ValueError) as error:
        diagnostics["validation_error"] = str(error)[:160]
        raise ObserverError("MALFORMED_RESPONSE",
            f"candidate schema validation failed: {str(error)[:160]}", usage=usage,
            finish_reason=finish_reason, diagnostics=diagnostics) from error
    return {"output": output, "usage": usage, "finish_reason": finish_reason,
            "diagnostics": diagnostics}


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: int(item) for key, item in value.items()
            if key in USAGE_FIELDS and isinstance(item, int) and not isinstance(item, bool) and item >= 0}


def _classify_http_error(error: urllib.error.HTTPError) -> tuple[str, str]:
    raw = error.read(4096)
    kind = "QUOTA_429" if error.code == 429 else f"API_HTTP_{error.code}"
    try:
        payload = json.loads(raw)
        item = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(item, dict):
            status = str(item.get("status") or "")[:80]
            message = str(item.get("message") or "")[:180].replace("\n", " ")
            detail_text = canonical_json(item.get("details") or [])[:600].lower()
            billing_text = f"{status} {message} {detail_text}".lower()
            if error.code == 403 and any(marker in billing_text for marker in
                                         ("billing", "service_disabled", "service disabled",
                                          "api has not been used", "has been disabled")):
                kind = "BILLING_DISABLED"
            return kind, f"HTTP {error.code} {status}: {message}".strip()
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return kind, f"HTTP {error.code} {error.reason}"[:300]


def _json_error_summary(error: Exception) -> str:
    if isinstance(error, json.JSONDecodeError):
        return f"{error.msg} at line {error.lineno} column {error.colno} char {error.pos}"[:160]
    return type(error).__name__


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _string_schema(max_length: int) -> dict[str, Any]:
    return {"type": "string", "maxLength": max_length}


def _response_schema() -> dict[str, Any]:
    evidence_ref = _string_schema(96)
    evidence_item = {"type": "object", "additionalProperties": False,
      "properties": {"statement": _string_schema(180),
        "evidence_refs": {"type": "array", "maxItems": 2, "items": evidence_ref}},
      "required": ["statement", "evidence_refs"]}
    return {"type": "object", "additionalProperties": False, "properties": {
      "briefing_schema_version": {"type": "string", "enum": [SCHEMA_VERSION]},
      "summary": _string_schema(300), "abstain": {"type": "boolean"},
      "trading_authority": {"type": "string", "enum": ["NONE"]},
      "facts": {"type": "array", "maxItems": 3, "items": evidence_item},
      "interpretations": {"type": "array", "maxItems": 3, "items": evidence_item},
      "upcoming_verified_events": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"event_type": _string_schema(64), "scheduled_at_utc": _string_schema(40),
          "timing_quality": _string_schema(80), "evidence_ref": evidence_ref},
        "required": ["event_type", "scheduled_at_utc", "timing_quality", "evidence_ref"]}},
      "market_stress_risk_flags": {"type": "array", "maxItems": 3, "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"flag": _string_schema(120),
          "severity": {"type": "string", "enum": ["LOW", "MEDIUM", "HIGH"]},
          "evidence_refs": {"type": "array", "maxItems": 2, "items": evidence_ref}},
        "required": ["flag", "severity", "evidence_refs"]}},
      "stale_or_missing_fields": {"type": "array", "maxItems": 6, "items": _string_schema(80)},
      "uncertainties": {"type": "array", "maxItems": 5, "items": _string_schema(120)}},
      "required": ["briefing_schema_version", "summary", "abstain", "trading_authority", "facts",
        "interpretations", "upcoming_verified_events", "market_stress_risk_flags",
        "stale_or_missing_fields", "uncertainties"]}


def _validate_output(value: Any) -> None:
    required = {"briefing_schema_version", "summary", "abstain", "trading_authority", "facts",
                "interpretations", "upcoming_verified_events", "market_stress_risk_flags",
                "stale_or_missing_fields", "uncertainties"}
    if not isinstance(value, dict):
        raise ValueError("briefing must be an object")
    if set(value) != required:
        raise ValueError(f"keys differ: missing={sorted(required-set(value))} extra={sorted(set(value)-required)}")
    if value["briefing_schema_version"] != SCHEMA_VERSION:
        raise ValueError("schema version mismatch")
    if value["trading_authority"] != "NONE":
        raise ValueError("trading_authority must be NONE")
    _require_string(value["summary"], "summary", 300)
    if not isinstance(value["abstain"], bool):
        raise ValueError("abstain must be a boolean")
    for name in ("facts", "interpretations"):
        _require_list(value[name], name, 3)
        for item in value[name]:
            if not isinstance(item, dict) or set(item) != {"statement", "evidence_refs"}:
                raise ValueError(f"invalid {name} item")
            _require_string(item["statement"], f"{name}.statement", 180)
            _require_string_list(item["evidence_refs"], f"{name}.evidence_refs", 2, 96, False)
    _require_list(value["upcoming_verified_events"], "upcoming_verified_events", 3)
    for item in value["upcoming_verified_events"]:
        keys = {"event_type", "scheduled_at_utc", "timing_quality", "evidence_ref"}
        if not isinstance(item, dict) or set(item) != keys:
            raise ValueError("invalid upcoming_verified_events item")
        for key, limit in (("event_type", 64), ("scheduled_at_utc", 40),
                           ("timing_quality", 80), ("evidence_ref", 96)):
            _require_string(item[key], f"upcoming_verified_events.{key}", limit)
    _require_list(value["market_stress_risk_flags"], "market_stress_risk_flags", 3)
    for item in value["market_stress_risk_flags"]:
        if not isinstance(item, dict) or set(item) != {"flag", "severity", "evidence_refs"}:
            raise ValueError("invalid market_stress_risk_flags item")
        _require_string(item["flag"], "market_stress_risk_flags.flag", 120)
        if item["severity"] not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("invalid market_stress_risk_flags.severity")
        _require_string_list(item["evidence_refs"], "market_stress_risk_flags.evidence_refs", 2, 96, False)
    _require_string_list(value["stale_or_missing_fields"], "stale_or_missing_fields", 6, 80)
    _require_string_list(value["uncertainties"], "uncertainties", 5, 120)


def _require_list(value: Any, name: str, max_items: int) -> None:
    if not isinstance(value, list) or len(value) > max_items:
        raise ValueError(f"{name} must be a list with at most {max_items} items")


def _require_string(value: Any, name: str, max_length: int) -> None:
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{name} must be a string no longer than {max_length} characters")


def _require_string_list(value: Any, name: str, max_items: int, max_length: int,
                         allow_empty: bool = True) -> None:
    _require_list(value, name, max_items)
    if not allow_empty and not value:
        raise ValueError(f"{name} must not be empty")
    for item in value:
        _require_string(item, name, max_length)
