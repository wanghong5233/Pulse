"""Dump JobMemory + DomainMemory state from core_memory.json for audit."""
import json
import sys
from pathlib import Path

p = Path(sys.argv[1] if len(sys.argv) > 1 else "/root/.pulse/core_memory.json")
data = json.loads(p.read_text(encoding="utf-8"))

print(f"=== top-level keys ({p}) ===")
for key in sorted(data.keys()):
    print(f"  {key}")

print()
for key in ("job_memory", "domain_memory", "domains", "preferences", "profile"):
    if key in data:
        print(f"=== {key} ===")
        print(json.dumps(data[key], ensure_ascii=False, indent=2)[:4000])
        print()
