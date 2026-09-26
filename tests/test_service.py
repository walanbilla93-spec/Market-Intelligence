from __future__ import annotations

import asyncio
import io
import json
import os
import tempfile
import time
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

from market_intelligence.exporter import build_manifest
from market_intelligence.gemini import GeminiObserver, ObserverError, PROMPT_VERSION, SCHEMA_VERSION, FIXED_MODEL
from market_intelligence.linkage import import_candidates
from market_intelligence.models import Event, Observation
from market_intelligence.storage import Storage
from market_intelligence.util import iso_utc


ROOT=Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp=tempfile.TemporaryDirectory();self.data=Path(self.temp.name)
        self.store=Storage(self.data,"boot-test",ROOT/"migrations")

    def tearDown(self) -> None:
        self.store.close();self.temp.cleanup()

    def test_event_causality_and_schedule_revision(self) -> None:
        first="2026-09-25T12:00:00Z"
        base=dict(source="test",event_type="CPI",title="CPI",publisher="Official",source_url="https://example.test/cpi",
          observed_at_utc=first,available_to_system_at_utc=first,first_seen_at_utc=first)
        event_id=self.store.upsert_event(Event(scheduled_at_utc="2026-10-01T12:30:00Z",**base))
        self.store.upsert_event(Event(scheduled_at_utc="2026-10-02T12:30:00Z",**{**base,"observed_at_utc":"2026-09-26T12:00:00Z","available_to_system_at_utc":"2026-09-26T12:00:00Z"}))
        event=self.store.db.execute("SELECT * FROM events WHERE event_id=?",(event_id,)).fetchone()
        self.assertEqual(event["first_seen_at_utc"],first)
        self.assertEqual(event["scheduled_at_utc"],"2026-10-02T12:30:00Z")
        revisions=self.store.db.execute("SELECT count(*) FROM schedule_revisions WHERE event_id=?",(event_id,)).fetchone()[0]
        self.assertEqual(revisions,2)
        self.store.upsert_event(Event(scheduled_at_utc="2026-10-02T12:30:00Z",**{**base,
          "observed_at_utc":"2026-09-27T12:00:00Z","available_to_system_at_utc":"2026-09-27T12:00:00Z"}))
        revisions=self.store.db.execute("SELECT count(*) FROM schedule_revisions WHERE event_id=?",(event_id,)).fetchone()[0]
        self.assertEqual(revisions,2)

    def test_restart_dedupe(self) -> None:
        event=Event(source="test",event_type="NEWS",title="Same",publisher="Official",source_url="https://example.test/a",
          observed_at_utc="2026-09-25T12:00:00Z",available_to_system_at_utc="2026-09-25T12:00:00Z")
        first=self.store.upsert_event(event);self.store.close()
        self.store=Storage(self.data,"boot-test-2",ROOT/"migrations")
        second=self.store.upsert_event(event)
        self.assertEqual(first,second)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM events").fetchone()[0],1)

    def test_manifest_hashes_fixed_jsonl(self) -> None:
        self.store.add_observation(Observation(source="test",metric="x",observed_at_utc=iso_utc(),
          available_to_system_at_utc=iso_utc(),source_url="https://example.test",value_num=1.0))
        manifest=build_manifest(self.store.export_dir)
        self.assertEqual(manifest["schema_version"],"MARKET_INTELLIGENCE_MANIFEST_V1")
        self.assertGreaterEqual(manifest["row_count"],1)
        self.assertTrue(all(len(item["sha256"])==64 for item in manifest["files"]))

    def test_candidate_import_is_one_way_and_idempotent(self) -> None:
        source=self.data/"births.jsonl";row={"kind":"candidate_birth","candidateId":"c1","candidateKey":"k1",
          "episodeId":"e1","decisionAt":1790352000000,"configHash":"abc"}
        source.write_text(json.dumps(row)+"\n","utf-8")
        self.assertEqual(import_candidates(source,self.store)["inserted"],1)
        self.assertEqual(import_candidates(source,self.store)["inserted"],0)

    def _fresh_market(self) -> None:
        now=iso_utc()
        rows=[
          ("linear_breadth",None,12.0,"percent_net_up_minus_down"),
          ("funding_rate","BTCUSDT",0.0001,"ratio"),("open_interest","BTCUSDT",100.0,"contracts"),
          ("mark_index_basis","BTCUSDT",0.01,"percent"),("return_24h","BTCUSDT",0.02,"ratio"),
          ("funding_rate","ETHUSDT",0.0002,"ratio"),("open_interest","ETHUSDT",200.0,"contracts"),
          ("mark_index_basis","ETHUSDT",0.02,"percent"),("return_24h","ETHUSDT",0.03,"ratio"),
          ("eth_btc_relative_price","ETH/BTC",0.03,"ratio"),
        ]
        for metric,instrument,value,unit in rows:
            self.store.add_observation(Observation(source="bybit_context",metric=metric,instrument=instrument,
              value_num=value,unit=unit,observed_at_utc=now,available_to_system_at_utc=now,
              source_url="https://api.bybit.test"))

    def _valid_output(self) -> dict:
        return {"briefing_schema_version":SCHEMA_VERSION,"summary":"Shadow briefing","abstain":False,
          "trading_authority":"NONE","facts":[],"interpretations":[],"upcoming_verified_events":[],
          "market_stress_risk_flags":[],"stale_or_missing_fields":[],"uncertainties":[]}

    def test_gemini_disabled_default_persists_source_health(self) -> None:
        observer=GeminiObserver(self.store,enabled=False)
        self.assertIsNone(observer.schedule_after_collection())
        row=self.store.db.execute("SELECT status FROM source_health WHERE source='gemini_observer'").fetchone()
        self.assertEqual(row[0],"DISABLED")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM briefings").fetchone()[0],0)

    def test_gemini_enabled_without_key_is_durable_and_sends_nothing(self) -> None:
        self._fresh_market();old=os.environ.pop("GEMINI_API_KEY",None)
        try:
            observer=GeminiObserver(self.store,enabled=True);briefing_id=observer.schedule_after_collection()
            row=self.store.db.execute("SELECT status,error_type FROM briefings WHERE briefing_id=?",(briefing_id,)).fetchone()
            self.assertEqual(tuple(row),("DISABLED_NO_KEY","NO_API_KEY"));self.assertTrue(observer.queue.empty())
        finally:
            if old is not None:os.environ["GEMINI_API_KEY"]=old

    def test_gemini_success_is_validated_and_persisted(self) -> None:
        self._fresh_market();snapshot=self.store.build_briefing_snapshot()
        briefing_id=self.store.create_briefing(snapshot,"TEST",FIXED_MODEL,PROMPT_VERSION,SCHEMA_VERSION)
        observer=GeminiObserver(self.store,enabled=True)
        observer._request=lambda payload,timeout=None:{"output":self._valid_output(),"usage":{"promptTokenCount":10,"candidatesTokenCount":5,"totalTokenCount":15},"finish_reason":"STOP","diagnostics":{"response_bytes":100}}
        asyncio.run(observer._process(briefing_id))
        row=self.store.db.execute("SELECT status,total_tokens,authoritative,output_json FROM briefings WHERE briefing_id=?",(briefing_id,)).fetchone()
        self.assertEqual(row["status"],"OK");self.assertEqual(row["total_tokens"],15);self.assertEqual(row["authoritative"],0)
        self.assertEqual(json.loads(row["output_json"])["trading_authority"],"NONE")

    def test_gemini_rest_shape_and_key_header(self) -> None:
        output=self._valid_output();captured={}
        class FakeResponse:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,limit):
                return json.dumps({"candidates":[{"content":{"parts":[{"text":json.dumps(output)}]}}]}).encode()
        def fake_urlopen(request,timeout):
            captured["url"]=request.full_url;captured["headers"]=dict(request.header_items())
            captured["body"]=json.loads(request.data);return FakeResponse()
        observer=GeminiObserver(self.store,enabled=True);observer.api_key="secret-test-key"
        with patch("market_intelligence.gemini.urllib.request.urlopen",fake_urlopen):result=observer._request({"x":1})
        self.assertEqual(result["output"]["trading_authority"],"NONE")
        self.assertNotIn("secret-test-key",captured["url"])
        self.assertEqual(captured["headers"]["X-goog-api-key"],"secret-test-key")
        schema=captured["body"]["generationConfig"]["responseFormat"]["text"]["schema"]
        self.assertEqual(schema["type"],"object")
        self.assertEqual(captured["body"]["generationConfig"]["responseFormat"]["text"]["mimeType"],"APPLICATION_JSON")
        self.assertNotIn("responseMimeType",captured["body"]["generationConfig"])
        self.assertNotIn("responseSchema",captured["body"]["generationConfig"])
        self.assertEqual(captured["body"]["generationConfig"]["candidateCount"],1)
        self.assertEqual(schema["properties"]["facts"]["maxItems"],3)

    def test_gemini_http_429_is_classified(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.api_key="secret-test-key"
        error=urllib.error.HTTPError("https://example.test",429,"quota",{},io.BytesIO(b'{"error":"quota"}'))
        with patch("market_intelligence.gemini.urllib.request.urlopen",side_effect=error):
            with self.assertRaises(ObserverError) as caught:observer._request({"x":1})
        self.assertEqual(caught.exception.error_type,"QUOTA_429");self.assertFalse(caught.exception.retryable)

    def test_gemini_malformed_response(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.max_attempts=1
        observer._request=lambda payload,timeout=None:(_ for _ in ()).throw(ObserverError("MALFORMED_RESPONSE","bad schema"))
        result=asyncio.run(observer.observe({"x":1}));self.assertEqual(result["status"],"MALFORMED_RESPONSE")

    def test_gemini_quota_429_is_not_retried(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);calls=[]
        def quota(payload,timeout=None):calls.append(1);raise ObserverError("QUOTA_429","quota",False)
        observer._request=quota;result=asyncio.run(observer.observe({"x":1}))
        self.assertEqual(result["status"],"QUOTA_429");self.assertEqual(len(calls),1)

    def test_gemini_http_503_has_one_bounded_retry(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);calls=[]
        def unavailable(payload,timeout=None):calls.append(1);raise ObserverError("API_HTTP_503","high demand",True)
        observer._request=unavailable;result=asyncio.run(observer.observe({"x":1}))
        self.assertEqual(result["status"],"API_HTTP_503");self.assertEqual(len(calls),2)

    def test_gemini_transport_timeout_has_one_bounded_retry(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);calls=[]
        def timeout(payload,request_timeout=None):calls.append(1);raise ObserverError("TIMEOUT","transport timeout",True)
        observer._request=timeout;result=asyncio.run(observer.observe({"x":1}))
        self.assertEqual(result["status"],"TIMEOUT");self.assertEqual(len(calls),2)

    def test_gemini_hard_timeout(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.timeout=0.01;observer.max_attempts=1
        observer.total_timeout=0.03
        def slow(payload,timeout=None):time.sleep(0.05);return {"output":self._valid_output(),"usage":{},"diagnostics":{}}
        observer._request=slow;result=asyncio.run(observer.observe({"x":1}))
        self.assertEqual(result["status"],"TIMEOUT")
        self.assertLess(result["latency_ms"],100)

    def test_gemini_unavailable_network_and_circuit_breaker(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.max_attempts=1;observer.threshold=1
        observer._request=lambda payload,timeout=None:(_ for _ in ()).throw(ObserverError("NETWORK_ERROR","offline",True))
        first=asyncio.run(observer.observe({"x":1}));second=asyncio.run(observer.observe({"x":1}))
        self.assertEqual(first["status"],"NETWORK_ERROR");self.assertEqual(second["status"],"CIRCUIT_OPEN")

    def test_gemini_truncated_json_captures_finish_reason_and_usage_without_raw_text(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.api_key="secret-test-key"
        raw={"candidates":[{"finishReason":"MAX_TOKENS","content":{"parts":[{"text":"{\"summary\":\"secret fragment"}]}}],
          "usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":2048,"thoughtsTokenCount":7,"totalTokenCount":2155}}
        class FakeResponse:
            def __enter__(self):return self
            def __exit__(self,*args):return False
            def read(self,limit):return json.dumps(raw).encode()
        with patch("market_intelligence.gemini.urllib.request.urlopen",return_value=FakeResponse()):
            with self.assertRaises(ObserverError) as caught:observer._request({"x":1})
        error=caught.exception
        self.assertEqual(error.error_type,"OUTPUT_TRUNCATED");self.assertEqual(error.finish_reason,"MAX_TOKENS")
        self.assertEqual(error.usage["candidatesTokenCount"],2048)
        self.assertIn("candidate_json_error",error.diagnostics)
        self.assertNotIn("secret fragment",json.dumps(error.diagnostics))
        self.assertNotIn("secret fragment",error.detail)

    def test_gemini_malformed_candidate_preserves_safe_diagnostics_in_storage(self) -> None:
        self._fresh_market();snapshot=self.store.build_briefing_snapshot()
        briefing_id=self.store.create_briefing(snapshot,"TEST",FIXED_MODEL,PROMPT_VERSION,SCHEMA_VERSION)
        observer=GeminiObserver(self.store,enabled=True);observer.max_attempts=1
        observer._request=lambda payload,timeout=None:(_ for _ in ()).throw(ObserverError(
          "MALFORMED_RESPONSE","candidate JSON invalid at line 3 column 14",
          usage={"promptTokenCount":90,"candidatesTokenCount":1200,"totalTokenCount":1290},finish_reason="STOP",
          diagnostics={"candidate_text_chars":4800,"candidate_json_error":"Unterminated string at line 3 column 14 char 66"}))
        asyncio.run(observer._process(briefing_id))
        self.store.close();self.store=Storage(self.data,"boot-diagnostics-restart",ROOT/"migrations")
        item=self.store.list_briefings(1)[0]
        self.assertEqual(item["finish_reason"],"STOP");self.assertEqual(item["usage"]["totalTokenCount"],1290)
        self.assertEqual(item["response_diagnostics"]["candidate_text_chars"],4800)
        self.assertIsNone(item["output"])

    def test_gemini_encoded_request_is_bounded(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.api_key="secret-test-key";observer.max_request_bytes=20000
        with self.assertRaises(ObserverError) as caught:observer._request({"x":"z"*30000})
        self.assertEqual(caught.exception.error_type,"REQUEST_TOO_LARGE")

    def test_stale_input_abstains_without_request(self) -> None:
        observer=GeminiObserver(self.store,enabled=True);observer.api_key="not-used"
        briefing_id=observer.schedule_after_collection()
        row=self.store.db.execute("SELECT status FROM briefings WHERE briefing_id=?",(briefing_id,)).fetchone()
        self.assertEqual(row[0],"ABSTAIN_STALE_INPUT");self.assertTrue(observer.queue.empty())

    def test_restart_idempotency_and_queue_bound(self) -> None:
        self._fresh_market();snapshot=self.store.build_briefing_snapshot()
        first=self.store.create_briefing(snapshot,"TEST",FIXED_MODEL,PROMPT_VERSION,SCHEMA_VERSION)
        second=self.store.create_briefing(snapshot,"TEST",FIXED_MODEL,PROMPT_VERSION,SCHEMA_VERSION)
        self.assertIsNotNone(first);self.assertIsNone(second)
        old=os.environ.get("GEMINI_MAX_QUEUE");os.environ["GEMINI_MAX_QUEUE"]="99"
        try:self.assertEqual(GeminiObserver(self.store).queue.maxsize,2)
        finally:
            if old is None:os.environ.pop("GEMINI_MAX_QUEUE",None)
            else:os.environ["GEMINI_MAX_QUEUE"]=old

    def test_status_and_briefings_are_read_only(self) -> None:
        before=self.store.db.execute("SELECT count(*) FROM boots").fetchone()[0]
        status=self.store.service_status();items=self.store.list_briefings()
        self.assertIn("source_health",status);self.assertEqual(items,[])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM boots").fetchone()[0],before)
        self.store.close();readonly=Storage(self.data,"not-recorded",ROOT/"migrations",read_only=True)
        self.assertEqual(readonly.db.execute("SELECT count(*) FROM boots").fetchone()[0],before)
        readonly.close();self.store=Storage(self.data,"boot-after-read",ROOT/"migrations")


if __name__=="__main__":unittest.main()

