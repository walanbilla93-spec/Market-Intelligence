from __future__ import annotations

import json
import urllib.parse

from ..http import BoundedHttpClient
from ..models import Observation
from ..util import iso_utc, utc_now


class FredAdapter:
    name="fred_cross_market"
    base="https://api.stlouisfed.org/fred/series/observations"

    def __init__(self,http: BoundedHttpClient,api_key: str,series: list[str]) -> None:
        self.http=http;self.api_key=api_key;self.series=series

    async def collect(self) -> list[Observation]:
        if not self.api_key:raise RuntimeError("FRED_API_KEY is not configured")
        out=[];available=iso_utc(utc_now())
        for series in self.series:
            query=urllib.parse.urlencode({"series_id":series,"api_key":self.api_key,"file_type":"json","sort_order":"desc","limit":1})
            url=f"{self.base}?{query}";response=await self.http.get(url,{"Accept":"application/json"});body=json.loads(response.body)
            row=(body.get("observations") or [{}])[0];value=None
            try:value=float(row.get("value"))
            except (TypeError,ValueError):pass
            out.append(Observation(source=self.name,metric=series,instrument=series,value_num=value,unit="source_native",
              status="OK" if value is not None else "NOT_AVAILABLE",observed_at_utc=f"{row.get('date')}T00:00:00Z" if row.get("date") else available,
              available_to_system_at_utc=available,source_url=url.split("api_key=")[0]+"api_key=REDACTED",
              payload={"realtime_start":row.get("realtime_start"),"realtime_end":row.get("realtime_end")}))
        return out

