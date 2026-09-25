from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ..http import BoundedHttpClient
from ..models import Event
from ..util import iso_utc, utc_now


class BlsCalendarAdapter:
    name = "bls_calendar"
    url = "https://www.bls.gov/schedule/news_release/bls.ics"
    wanted = ("Consumer Price Index", "Producer Price Index", "Employment Situation")

    def __init__(self, http: BoundedHttpClient, timezone_name: str = "America/New_York") -> None:
        self.http = http
        self.timezone = ZoneInfo(timezone_name)

    async def collect(self) -> list[Event]:
        response = await self.http.get(self.url, {"Accept": "text/calendar"})
        text = response.body.decode("utf-8", "replace").replace("\r\n ", "")
        now = iso_utc(utc_now())
        events=[]
        for block in text.split("BEGIN:VEVENT")[1:]:
            fields={}
            for line in block.splitlines():
                if ":" in line:
                    key,value=line.split(":",1);fields[key.split(";",1)[0]]=value.strip()
            title=fields.get("SUMMARY","").replace("\\,",",")
            if not any(term.lower() in title.lower() for term in self.wanted):
                continue
            raw=fields.get("DTSTART","")
            try:
                local=datetime.strptime(raw[:15],"%Y%m%dT%H%M%S").replace(tzinfo=self.timezone)
            except ValueError:
                continue
            kind="NFP" if "Employment Situation" in title else "CPI" if "Consumer Price" in title else "PPI"
            events.append(Event(source=self.name,event_type=kind,title=title,publisher="U.S. Bureau of Labor Statistics",
              source_url=self.url,scheduled_at_utc=iso_utc(local),observed_at_utc=now,
              available_to_system_at_utc=now,first_seen_at_utc=now,payload={"uid":fields.get("UID"),"timezone":"America/New_York"}))
        return events

