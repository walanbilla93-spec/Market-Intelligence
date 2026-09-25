from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from market_intelligence.exporter import build_manifest
from market_intelligence.gemini import GeminiObserver
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

    def test_gemini_queue_is_bounded_and_disabled_without_key(self) -> None:
        old=os.environ.pop("GEMINI_API_KEY",None)
        try:
            observer=GeminiObserver();self.assertFalse(observer.submit({"x":1}))
            result=asyncio.run(observer.observe({"x":1}))
            self.assertFalse(result["authoritative"])
        finally:
            if old is not None:os.environ["GEMINI_API_KEY"]=old


if __name__=="__main__":unittest.main()

