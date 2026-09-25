from __future__ import annotations

import unittest

from market_intelligence.adapters.bls import BlsCalendarAdapter
from market_intelligence.adapters.rss import RssAdapter
from market_intelligence.adapters.fed import FedFomcAdapter
from market_intelligence.http import Response


class FakeHttp:
    def __init__(self,body: str,content_type: str="text/plain") -> None:self.body=body;self.content_type=content_type
    async def get(self,url: str,headers=None) -> Response:return Response(url,200,self.body.encode(),self.content_type)


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_bls_ics_timezone_and_filter(self) -> None:
        ics="""BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:cpi\nDTSTART;TZID=America/New_York:20261013T083000\nSUMMARY:Consumer Price Index for September 2026\nEND:VEVENT\nBEGIN:VEVENT\nUID:other\nDTSTART;TZID=America/New_York:20261014T100000\nSUMMARY:Other Release\nEND:VEVENT\nEND:VCALENDAR\n"""
        rows=await BlsCalendarAdapter(FakeHttp(ics)).collect()
        self.assertEqual(len(rows),1);self.assertEqual(rows[0].scheduled_at_utc,"2026-10-13T12:30:00Z")

    async def test_rss_never_backdates_availability(self) -> None:
        rss="""<rss><channel><item><title>Incident</title><link>https://example.test/i</link><pubDate>Thu, 24 Sep 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        row=(await RssAdapter(FakeHttp(rss),"trusted","https://example.test/rss","Publisher",True).collect())[0]
        self.assertEqual(row.publisher_time_utc,"2026-09-24T10:00:00Z")
        self.assertNotEqual(row.available_to_system_at_utc,row.publisher_time_utc)
        self.assertEqual(row.verification_status,"VERIFIED_TRUSTED_FEED")

    async def test_fomc_parser_ignores_minutes_release_dates(self) -> None:
        html="""<div class='panel-heading'>2026 FOMC Meetings</div>
        <div class='fomc-meeting__month'>October</div><div class='fomc-meeting__date'>27-28</div>
        <p>Minutes released November 18, 2026</p>"""
        rows=await FedFomcAdapter(FakeHttp(html)).collect()
        self.assertEqual(len(rows),2)
        self.assertTrue(all("October" in row.title for row in rows))


if __name__=="__main__":unittest.main()

