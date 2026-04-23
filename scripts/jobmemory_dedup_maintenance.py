"""One-shot JobMemory maintenance scripts.

Two subcommands:
- ``probe``: verify P1-C dedup actually short-circuits on real JobMemory.
- ``dedup-migrate``: one-time cleanup — keep the earliest active item per
  ``(type, target, content)`` key, ``retire`` the rest. Safe to re-run (retired
  items are skipped). Records an audit line for each retirement.

Not a unit test. Runs against the real workspace so we can audit current state.
Usage (from Pulse/):
    python scripts/_probe_dedup_live.py probe
    python scripts/_probe_dedup_live.py dedup-migrate [--dry-run] [--workspace job.default]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from pulse.core.storage.engine import DatabaseEngine
from pulse.modules.job.memory import JobMemory, MemoryItem


def _build_mem(workspace_id: str, source: str) -> JobMemory:
    engine = DatabaseEngine()
    return JobMemory.from_engine(engine, workspace_id=workspace_id, source=source)


def _cmd_probe(args: argparse.Namespace) -> int:
    mem = _build_mem(args.workspace, "dedup-live-probe")

    before = mem.list_items(type="constraint_note", include_expired=False)
    print(f"BEFORE: {len(before)} active constraint_note items")
    for it in before:
        print(f"  - id={it.id[:8]}... content={it.content!r} target={it.target!r}")

    print()
    print("--- probing: record same content twice ---")
    r1 = mem.record_item(
        {"type": "constraint_note", "target": None, "content": "已经联系过的不要重复投递"}
    )
    print(f"  attempt 1 -> id={r1.id[:8]}... created_at={r1.created_at}")
    r2 = mem.record_item(
        {"type": "constraint_note", "target": None, "content": "已经联系过的不要重复投递"}
    )
    print(f"  attempt 2 -> id={r2.id[:8]}... created_at={r2.created_at}")
    print(f"  SAME ID (dedup hit)? {r1.id == r2.id}")

    print()
    after = mem.list_items(type="constraint_note", include_expired=False)
    print(f"AFTER: {len(after)} active constraint_note items")
    print(f"  delta = {len(after) - len(before)} (0 = dedup works, >0 = dedup FAILED)")

    return 0 if r1.id == r2.id and len(after) == len(before) else 1


def _cmd_dedup_migrate(args: argparse.Namespace) -> int:
    mem = _build_mem(args.workspace, "dedup-migrate")

    all_active = mem.list_items(include_expired=False)
    groups: dict[tuple[str, str, str], list[MemoryItem]] = defaultdict(list)
    for it in all_active:
        key = (it.type, it.target or "", it.content)
        groups[key].append(it)

    duplicates = [(k, v) for k, v in groups.items() if len(v) > 1]
    if not duplicates:
        print("No duplicate active items; nothing to migrate.")
        return 0

    print(f"Found {len(duplicates)} duplicate group(s). Plan:")
    retire_plan: list[tuple[str, str, str]] = []
    for (type_, target, content), items in duplicates:
        items_sorted = sorted(items, key=lambda i: i.created_at or "")
        keeper = items_sorted[0]
        losers = items_sorted[1:]
        display_content = content if len(content) <= 30 else content[:30] + "..."
        print(
            f"  group key=({type_}, target={target!r}, content={display_content!r})"
        )
        print(
            f"    KEEP    id={keeper.id[:8]}... created_at={keeper.created_at}"
        )
        for loser in losers:
            print(
                f"    RETIRE  id={loser.id[:8]}... created_at={loser.created_at}"
            )
            retire_plan.append((loser.id, type_, content))

    if args.dry_run:
        print(f"\n[DRY RUN] would retire {len(retire_plan)} item(s). Re-run without --dry-run.")
        return 0

    print(f"\nApplying: retire {len(retire_plan)} item(s)...")
    for item_id, type_, content in retire_plan:
        ok = mem.retire_item(item_id)
        status = "OK" if ok else "SKIPPED (not found)"
        print(f"  retire id={item_id[:8]}... type={type_} -> {status}")

    remaining = mem.list_items(include_expired=False)
    print(f"\nRetired. {len(all_active)} -> {len(remaining)} active items.")
    print(
        "Note: config/profile/job.yaml will be refreshed on the next Brain "
        "mutation (sync_to_yaml is a side-effect hook). LLM snapshot reads "
        "directly from memory, so prompt injection is already clean."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_probe = sub.add_parser("probe", help="live dedup smoke test")
    p_probe.add_argument("--workspace", default="job.default")
    p_probe.set_defaults(func=_cmd_probe)

    p_mig = sub.add_parser(
        "dedup-migrate",
        help="one-time cleanup: retire duplicates, keep earliest active",
    )
    p_mig.add_argument("--workspace", default="job.default")
    p_mig.add_argument("--dry-run", action="store_true")
    p_mig.set_defaults(func=_cmd_dedup_migrate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
