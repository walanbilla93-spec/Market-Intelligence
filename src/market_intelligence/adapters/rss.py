from __future__ import annotations

import email.utils
import xml.etree.ElementTree as ET
from datetime import timezone

from ..http import BoundedHttpClient
from ..models import Event
from ..util import iso_utc, utc_now


class RssAdapter:
    def __init__(self,http: BoundedHttpClient,name: str,url: str,publisher: str,trusted: bool) -> None:
        self.http=http;self.name=name;self.url=url;self.publisher=publisher;self.trusted=trusted

    async def collect(self) -> list[Event]:
        response=await self.http.get(self.url,{"Accept":"application/rss+xml, application/atom+xml"})
        root=ET.fromstring(response.body);now=iso_utc(utc_now());events=[]
        items=root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        for item in items[:100]:
            def value(*names: str) -> str:
                for name in names:
                    node=item.find(name)
                    if node is not None and node.text:return node.text.strip()
                return ""
            title=value("title","{http://www.w3.org/2005/Atom}title")
            link=value("link")
            atom_link=item.find("{http://www.w3.org/2005/Atom}link")
            if not link and atom_link is not None:link=atom_link.attrib.get("href","")
            raw_time=value("pubDate","published","updated","{http://www.w3.org/2005/Atom}published","{http://www.w3.org/2005/Atom}updated")
            publisher_time=None
            try:publisher_time=iso_utc(email.utils.parsedate_to_datetime(raw_time).astimezone(timezone.utc))
            except (TypeError,ValueError,OverflowError):pass
            if not title or not link:continue
            events.append(Event(source=self.name,event_type="UNSCHEDULED_NEWS",title=title,publisher=self.publisher,
              source_url=link,publisher_time_utc=publisher_time,first_seen_at_utc=now,observed_at_utc=now,
              available_to_system_at_utc=now,verification_status="VERIFIED_TRUSTED_FEED" if self.trusted else "UNVERIFIED_FEED",
              payload={"feed_url":self.url,"causality_note":"available_to_system_at is local first observation; publisher_time is not used as availability"}))
        return events

