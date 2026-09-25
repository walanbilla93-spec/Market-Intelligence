from __future__ import annotations

import asyncio
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from .util import canonical_json, iso_utc, sha256_text, stable_id, utc_now


@dataclass
class Circuit:
    failures: int = 0
    open_until: float = 0.0


class GeminiObserver:
    """Optional, bounded, non-authoritative observer. It is never called by New Orayan."""

    def __init__(self) -> None:
        self.api_key=os.getenv("GEMINI_API_KEY","");self.model=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
        self.timeout=float(os.getenv("GEMINI_TIMEOUT_SECONDS","8"));self.threshold=int(os.getenv("GEMINI_FAILURE_THRESHOLD","3"))
        self.open_seconds=int(os.getenv("GEMINI_CIRCUIT_OPEN_SECONDS","900"));self.queue=asyncio.Queue(maxsize=int(os.getenv("GEMINI_MAX_QUEUE","8")))
        self.circuit=Circuit()

    def submit(self,payload: dict[str,Any]) -> bool:
        if not self.api_key or self.queue.full():return False
        self.queue.put_nowait(payload);return True

    async def observe(self,payload: dict[str,Any]) -> dict[str,Any]:
        loop=asyncio.get_running_loop()
        if loop.time()<self.circuit.open_until:return {"status":"CIRCUIT_OPEN","authoritative":False}
        try:
            result=await asyncio.wait_for(asyncio.to_thread(self._request,payload),timeout=self.timeout)
            self.circuit.failures=0;return result
        except Exception as error:
            self.circuit.failures+=1
            if self.circuit.failures>=self.threshold:self.circuit.open_until=loop.time()+self.open_seconds
            return {"status":"ERROR","authoritative":False,"error":str(error)[:200]}

    def _request(self,payload: dict[str,Any]) -> dict[str,Any]:
        url=f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        prompt={"role":"non_authoritative_market_observer","required_output":{"summary":"string","risk_tags":["string"],
          "confidence_0_to_1":"number","evidence_urls":["string"]},"input":payload}
        body=json.dumps({"contents":[{"parts":[{"text":canonical_json(prompt)}]}],
          "generationConfig":{"responseMimeType":"application/json","temperature":0}}).encode()
        request=urllib.request.Request(url,data=body,headers={"Content-Type":"application/json"},method="POST")
        with urllib.request.urlopen(request,timeout=self.timeout) as response:data=json.loads(response.read(1000000))
        text=data["candidates"][0]["content"]["parts"][0]["text"];parsed=json.loads(text)
        if not isinstance(parsed.get("risk_tags"),list) or not isinstance(parsed.get("summary"),str):raise ValueError("observer schema mismatch")
        observed=iso_utc(utc_now());return {"output_id":stable_id("gem",observed,sha256_text(text)),"status":"OK",
          "authoritative":False,"model":self.model,"observed_at_utc":observed,"available_to_system_at_utc":observed,"output":parsed}

