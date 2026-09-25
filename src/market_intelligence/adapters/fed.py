from __future__ import annotations

import re
from datetime import datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from ..http import BoundedHttpClient
from ..models import Event
from ..util import iso_utc, utc_now


class _Meetings(HTMLParser):
    def __init__(self) -> None:
        super().__init__();self.capture=None;self.buffer=[];self.year=None;self.pending_month=None;self.meetings=[]
    def handle_starttag(self,tag: str,attrs) -> None:
        classes=dict(attrs).get("class","").split()
        if "panel-heading" in classes:self.capture="heading";self.buffer=[]
        elif "fomc-meeting__month" in classes:self.capture="month";self.buffer=[]
        elif "fomc-meeting__date" in classes:self.capture="date";self.buffer=[]
    def handle_data(self,data: str) -> None:
        if self.capture:self.buffer.append(data)
    def handle_endtag(self,tag: str) -> None:
        if not self.capture:return
        text=" ".join("".join(self.buffer).split())
        if self.capture=="heading":
            match=re.search(r"\b(20\d{2})\b",text)
            if match:self.year=int(match.group(1))
        elif self.capture=="month":self.pending_month=text
        elif self.capture=="date" and self.year and self.pending_month:
            clean=text.replace("*","").strip()
            if re.fullmatch(r"\d{1,2}(?:-\d{1,2})?",clean):self.meetings.append((self.year,self.pending_month,clean))
            self.pending_month=None
        self.capture=None;self.buffer=[]


class FedFomcAdapter:
    name="federal_reserve_fomc"
    url="https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
    months="January|February|March|April|May|June|July|August|September|October|November|December"

    def __init__(self,http: BoundedHttpClient,timezone_name: str="America/New_York") -> None:
        self.http=http;self.timezone=ZoneInfo(timezone_name)

    async def collect(self) -> list[Event]:
        response=await self.http.get(self.url,{"Accept":"text/html"});parser=_Meetings();parser.feed(response.body.decode("utf-8","replace"))
        now_dt=utc_now();now=iso_utc(now_dt);events=[]
        for year,month,day_text in parser.meetings:
            if year not in (now_dt.year,now_dt.year+1):continue
            first,*last=day_text.split("-");last_day=last[0] if last else first;day=int(last_day)
            try:decision=datetime.strptime(f"{year} {month} {day} 2:00 PM","%Y %B %d %I:%M %p").replace(tzinfo=self.timezone)
            except ValueError:continue
            common=dict(source=self.name,publisher="Federal Reserve Board",source_url=self.url,
              observed_at_utc=now,available_to_system_at_utc=now,first_seen_at_utc=now,
              verification_status="VERIFIED_DATE_STANDARD_TIME_ASSUMPTION")
            source_event_id=f"fomc-{year}-{month.lower()}"
            events.append(Event(event_type="FOMC_DECISION",title=f"FOMC decision — {month} {day_text}, {year}",
              scheduled_at_utc=iso_utc(decision),payload={"source_event_id":source_event_id,"local_time_assumption":"14:00 America/New_York"},**common))
            press=decision.replace(hour=14,minute=30)
            events.append(Event(event_type="FOMC_PRESS_CONFERENCE",title=f"FOMC press conference — {month} {day}, {year}",
              scheduled_at_utc=iso_utc(press),payload={"source_event_id":source_event_id+"-press","local_time_assumption":"14:30 America/New_York"},**common))
        return events

