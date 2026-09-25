from __future__ import annotations

import argparse
import asyncio
import os
import time
import uuid
from pathlib import Path

from .adapters import BeaScheduleAdapter, BlsCalendarAdapter, BybitContextAdapter, FedFomcAdapter, FredAdapter, RssAdapter
from .config import Config
from .exporter import build_manifest
from .http import BoundedHttpClient
from .linkage import import_candidates
from .models import Event, Observation
from .storage import Storage
from .util import iso_utc, utc_now


def migrations_dir() -> Path:
    configured=os.getenv("MI_MIGRATIONS_DIR")
    if configured:return Path(configured)
    candidates=[Path.cwd()/"migrations",Path(__file__).resolve().parents[2]/"migrations"]
    for candidate in candidates:
        if candidate.exists():return candidate
    raise FileNotFoundError("migrations directory not found; set MI_MIGRATIONS_DIR")


def make_adapters(config: Config,http: BoundedHttpClient):
    enabled=config.raw.get("sources",{});adapters=[]
    if enabled.get("bls_calendar",True):adapters.append(BlsCalendarAdapter(http,config.timezone))
    if enabled.get("bea_schedule",True):adapters.append(BeaScheduleAdapter(http,config.timezone))
    if enabled.get("federal_reserve",True):adapters.append(FedFomcAdapter(http,config.timezone))
    if enabled.get("bybit_context",True):adapters.append(BybitContextAdapter(http,config.raw.get("bybit_symbols")))
    if enabled.get("fred_cross_market",False):adapters.append(FredAdapter(http,os.getenv("FRED_API_KEY",""),config.raw.get("fred_series",[])))
    for item in config.raw.get("rss",[]):
        adapters.append(RssAdapter(http,str(item["name"]),str(item["url"]),str(item["publisher"]),bool(item.get("trusted",False))))
    return adapters


async def collect_once(config: Config,storage: Storage) -> dict[str,int]:
    http=BoundedHttpClient(config.http_timeout,config.max_response_bytes);adapters=make_adapters(config,http)
    semaphore=asyncio.Semaphore(3);result={"sources":len(adapters),"success":0,"failed":0,"events":0,"observations":0}
    async def run(adapter):
        attempted=iso_utc(utc_now());started=time.monotonic()
        try:
            async with semaphore:rows=await adapter.collect()
            for row in rows:
                if isinstance(row,Event):storage.upsert_event(row);result["events"]+=1
                elif isinstance(row,Observation):storage.add_observation(row);result["observations"]+=1
            storage.health(adapter.name,"OK",attempted,int((time.monotonic()-started)*1000));result["success"]+=1
        except Exception as error:
            storage.health(adapter.name,"ERROR",attempted,int((time.monotonic()-started)*1000),str(error));result["failed"]+=1
    await asyncio.gather(*(run(adapter) for adapter in adapters));build_manifest(storage.export_dir);return result


async def run_forever(config: Config,storage: Storage) -> None:
    while True:
        await collect_once(config,storage)
        await asyncio.sleep(config.poll_seconds)


def main() -> None:
    parser=argparse.ArgumentParser(description="Independent New Orayan market intelligence service")
    parser.add_argument("command",choices=["run","once","migrate","export","import-candidates"])
    parser.add_argument("path",nargs="?");args=parser.parse_args();config=Config.load();boot_id=str(uuid.uuid4())
    storage=Storage(config.data_dir,boot_id,migrations_dir())
    try:
        if args.command=="migrate":print({"ok":True,"database":str(storage.db_path)})
        elif args.command=="export":print(build_manifest(storage.export_dir))
        elif args.command=="import-candidates":
            if not args.path:parser.error("import-candidates requires a JSONL path")
            print(import_candidates(Path(args.path),storage))
        elif args.command=="once":print(asyncio.run(collect_once(config,storage)))
        else:asyncio.run(run_forever(config,storage))
    finally:storage.close()


if __name__=="__main__":main()

