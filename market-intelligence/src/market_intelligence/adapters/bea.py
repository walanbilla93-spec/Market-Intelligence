from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from ..http import BoundedHttpClient
from ..models import Event
from ..util import iso_utc, utc_now


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__();self.parts=[]
    def handle_data(self,data: str) -> None:
        value=" ".join(data.split())
        if value:self.parts.append(value)


class BeaScheduleAdapter:
    name="bea_schedule"
    url="https://www.bea.gov/news/schedule"

    def __init__(self,http: BoundedHttpClient,timezone_name: str="America/New_York") -> None:
        self.http=http;self.timezone=ZoneInfo(timezone_name)

    async def collect(self) -> list[Event]:
        response=await self.http.get(self.url,{"Accept":"text/html"});parser=_Text();parser.feed(response.body.decode("utf-8","replace"))
        text=" ".join(parser.parts);now_dt=utc_now();now=iso_utc(now_dt);year=now_dt.astimezone(self.timezone).year
        pattern=re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\s+(\d{1,2}:\d{2})\s+(AM|PM)\s+(?:News|Data)\s+(.+?)(?=(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\s+\d{1,2}:\d{2}\s+(?:AM|PM)|To Be Announced|$)")
        events=[]
        for month,day,clock,ampm,title in pattern.findall(text):
            title=" ".join(title.split())
            if "Personal Income and Outlays" not in title:continue
            local=datetime.strptime(f"{year} {month} {day} {clock} {ampm}","%Y %B %d %I:%M %p").replace(tzinfo=self.timezone)
            events.append(Event(source=self.name,event_type="PCE",title=title,publisher="U.S. Bureau of Economic Analysis",
              source_url=self.url,scheduled_at_utc=iso_utc(local),observed_at_utc=now,
              available_to_system_at_utc=now,first_seen_at_utc=now,payload={"timezone":"America/New_York"}))
        return events

