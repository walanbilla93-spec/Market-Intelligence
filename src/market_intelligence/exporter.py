from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .util import iso_utc, stable_id, utc_now


def build_manifest(export_dir: Path) -> dict:
    watermark=iso_utc(utc_now());files=[]
    for file in sorted(export_dir.glob("*.jsonl")):
        size=file.stat().st_size;digest=hashlib.sha256();rows=0
        with file.open("rb") as handle:
            remaining=size
            while remaining:
                chunk=handle.read(min(65536,remaining))
                if not chunk:break
                remaining-=len(chunk);digest.update(chunk);rows+=chunk.count(b"\n")
        files.append({"path":file.name,"bytes":size,"rows":rows,"sha256":digest.hexdigest()})
    snapshot_id=stable_id("mis",watermark,[(x["path"],x["bytes"],x["sha256"]) for x in files])
    manifest={"schema_version":"MARKET_INTELLIGENCE_MANIFEST_V1","snapshot_id":snapshot_id,
      "watermark_utc":watermark,"file_count":len(files),"row_count":sum(x["rows"] for x in files),"files":files}
    target=export_dir/f"manifest-{watermark[:13].replace(':','').replace('T','-')}.json"
    target.write_text(json.dumps(manifest,indent=2,sort_keys=True)+"\n","utf-8")
    return manifest

