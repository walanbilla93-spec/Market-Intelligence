from __future__ import annotations

import json
from pathlib import Path

from .storage import Storage


def import_candidates(path: Path, storage: Storage) -> dict[str, int]:
    result={"read":0,"candidate_births":0,"inserted":0,"invalid":0}
    with path.open("r",encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():continue
            result["read"]+=1
            try:row=json.loads(line)
            except json.JSONDecodeError:result["invalid"]+=1;continue
            if row.get("kind")!="candidate_birth":continue
            result["candidate_births"]+=1
            if storage.import_candidate(row):result["inserted"]+=1
    return result

