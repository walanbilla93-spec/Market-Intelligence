from __future__ import annotations

import json
from statistics import median

from ..http import BoundedHttpClient
from ..models import Observation
from ..util import iso_utc, utc_now


class BybitContextAdapter:
    name="bybit_context"
    url="https://api.bybit.com/v5/market/tickers?category=linear"

    def __init__(self,http: BoundedHttpClient,symbols: list[str] | None=None) -> None:
        self.http=http;self.symbols=set(symbols or ["BTCUSDT","ETHUSDT"])

    async def collect(self) -> list[Observation]:
        response=await self.http.get(self.url,{"Accept":"application/json"});body=json.loads(response.body)
        if body.get("retCode")!=0:raise ValueError(f"Bybit retCode {body.get('retCode')}: {body.get('retMsg')}")
        rows=body.get("result",{}).get("list",[]);now=iso_utc(utc_now());out=[]
        changes=[]
        for row in rows:
            try:changes.append(float(row.get("price24hPcnt")))
            except (TypeError,ValueError):pass
        if changes:
            breadth=100*(sum(x>0 for x in changes)-sum(x<0 for x in changes))/len(changes)
            out.append(Observation(source=self.name,metric="linear_breadth",value_num=breadth,unit="percent_net_up_minus_down",
              observed_at_utc=now,available_to_system_at_utc=now,source_url=self.url,
              payload={"instrument_count":len(changes),"median_24h_return":median(changes)}))
        for row in rows:
            symbol=row.get("symbol")
            if symbol not in self.symbols:continue
            mark=_float(row.get("markPrice"));index=_float(row.get("indexPrice"));basis=(100*(mark/index-1) if mark and index else None)
            metrics={"funding_rate":(_float(row.get("fundingRate")),"ratio"),"open_interest":(_float(row.get("openInterest")),"contracts"),
              "mark_index_basis":(basis,"percent"),"return_24h":(_float(row.get("price24hPcnt")),"ratio")}
            for metric,(value,unit) in metrics.items():
                out.append(Observation(source=self.name,metric=metric,instrument=symbol,value_num=value,unit=unit,
                  status="OK" if value is not None else "NOT_AVAILABLE",observed_at_utc=now,
                  available_to_system_at_utc=now,source_url=self.url,payload={"next_funding_time":row.get("nextFundingTime")}))
        btc=next((x for x in rows if x.get("symbol")=="BTCUSDT"),None);eth=next((x for x in rows if x.get("symbol")=="ETHUSDT"),None)
        btc_last=_float(btc.get("lastPrice")) if btc else None;eth_last=_float(eth.get("lastPrice")) if eth else None
        if btc_last and eth_last:out.append(Observation(source=self.name,metric="eth_btc_relative_price",instrument="ETH/BTC",
          value_num=eth_last/btc_last,unit="ratio",observed_at_utc=now,available_to_system_at_utc=now,source_url=self.url))
        return out


def _float(value: object) -> float | None:
    try:return float(value) if value not in (None,"") else None
    except (TypeError,ValueError):return None

