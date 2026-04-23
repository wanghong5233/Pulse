from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

import uvicorn

from .core.config import get_settings


def _build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="pulse", description="Pulse command line interface")
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start Pulse API server")
    start.add_argument("--host", default=settings.host, help="Bind host")
    start.add_argument("--port", type=int, default=settings.port, help="Bind port")
    start.add_argument(
        "--reload",
        action="store_true",
        default=settings.reload,
        help="Enable auto-reload in development mode",
    )
    start.add_argument(
        "--log-level",
        default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Uvicorn log level",
    )

    # ── profile: Domain Profile yaml ↔ memory 运维入口 ──
    profile = subparsers.add_parser(
        "profile",
        help="Manage Domain Profile yaml ↔ memory (job / mail / ...)",
    )
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)

    for name, help_text in [
        ("load", "Load <domain>.yaml into memory (全量替换 domain 前缀)"),
        ("dump", "Print domain memory 当前状态为 JSON (不落盘)"),
        ("export", "强制把 domain memory 当前状态写回 yaml"),
        ("reset", "清空 domain memory 并重置 yaml 为空"),
    ]:
        sp = profile_sub.add_parser(name, help=help_text)
        sp.add_argument(
            "--domain",
            default="",
            help="指定单个 domain (如 job); 缺省时作用到全部已注册 domain",
        )

    _attach_job_subcommands(subparsers)

    return parser


def _attach_job_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """``pulse job ...`` — JobMemory v2 的开发态 CRUD 工具。

    三块存储各给一组子命令; 绕过 Pipeline 审计直接操作 JobMemory, 适合
    快速预置 / 检查 / 清理, 不代替 Brain 通过 IntentSpec 的正式写入路径。
    """
    job = subparsers.add_parser(
        "job",
        help="Job domain memory CLI (item / hc / resume / snapshot / reset)",
    )
    job_sub = job.add_subparsers(dest="job_command", required=True)

    snap = job_sub.add_parser("snapshot", help="打印 JobMemory 当前 snapshot (JSON)")
    snap.add_argument(
        "--md", action="store_true",
        help="改为输出 to_prompt_section() markdown (给 prompt 用的那份)",
    )

    reset = job_sub.add_parser(
        "reset",
        help="清空所有 job.* facts + 重置 job.yaml + 删除 resume.md (开发期 wipe)",
    )
    reset.add_argument(
        "--yes", action="store_true",
        help="跳过交互确认",
    )

    # ── item ──
    item = job_sub.add_parser("item", help="Memory Item (job.item:<uuid>) CRUD")
    item_sub = item.add_subparsers(dest="item_command", required=True)

    item_add = item_sub.add_parser("add", help="追加一条 memory item")
    item_add.add_argument("--type", required=True, help="item 类型 (推荐 enum; 未知值回落 'other')")
    item_add.add_argument("--content", required=True, help="人类可读的一行事实")
    item_add.add_argument("--target", default=None, help="作用目标 (如公司名); 可选")
    item_add.add_argument("--raw-text", default="", help="原始用户话语; 缺省用 content")
    item_add.add_argument(
        "--valid-until", default=None,
        help="过期时间 ISO-8601 (e.g. 2026-12-31T00:00:00Z); 缺省永久",
    )

    item_list = item_sub.add_parser("list", help="列出 items")
    item_list.add_argument("--type", default=None, help="按 type 过滤")
    item_list.add_argument("--target", default=None, help="按 target 过滤")
    item_list.add_argument(
        "--include-expired", action="store_true",
        help="包含已过期 / 已被取代的 items",
    )

    item_retire = item_sub.add_parser("retire", help="把一条 item 置为过期 (valid_until=now)")
    item_retire.add_argument("--id", required=True, help="item uuid")

    # ── hc (Hard Constraints) ──
    hc = job_sub.add_parser("hc", help="Hard Constraints (job.hc.*) CRUD")
    hc_sub = hc.add_subparsers(dest="hc_command", required=True)

    hc_set = hc_sub.add_parser("set", help="设置一个 hard constraint")
    hc_set.add_argument("field", help="字段名 (preferred_location/salary_floor_monthly/target_roles/experience_level)")
    hc_set.add_argument(
        "values", nargs="+",
        help="一个或多个值; list 字段传多个, int 字段传一个数字",
    )

    hc_unset = hc_sub.add_parser("unset", help="移除一个 hard constraint")
    hc_unset.add_argument("field", help="字段名")

    # ── resume ──
    res = job_sub.add_parser("resume", help="Resume (job.doc:resume) 查看 / 从文件重载")
    res_sub = res.add_subparsers(dest="resume_command", required=True)
    res_sub.add_parser("show", help="打印 memory 里保存的 resume raw_text")
    res_load = res_sub.add_parser(
        "load", help="从 resume.md 文件重新灌入 memory (会清空 parsed cache)",
    )
    res_load.add_argument(
        "--path", default=None,
        help="自定义路径; 缺省用 config 里 profile_resume_md_path",
    )


def _cmd_start(args: argparse.Namespace) -> int:
    uvicorn.run(
        "pulse.core.server:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )
    return 0


def _build_profile_coordinator():
    """构造一个 ProfileCoordinator + 注册所有 module 的 manager。

    与服务器启动路径独立: CLI 不起 FastAPI, 只需要 ModuleRegistry + 每个
    module 自建的 manager (manager 自带 WorkspaceMemory 到 DB 的连接)。
    """
    from .core.module import ModuleRegistry
    from .core.profile import ProfileCoordinator

    registry = ModuleRegistry()
    registry.discover("pulse.modules")
    coord = ProfileCoordinator()
    for mod in registry.modules:
        try:
            pm = mod.get_profile_manager()
        except Exception as exc:
            print(f"[warn] module {mod.name} get_profile_manager failed: {exc}", file=sys.stderr)
            continue
        if pm is not None:
            try:
                coord.register(pm)
            except ValueError as exc:
                print(f"[warn] profile register rejected for {mod.name}: {exc}", file=sys.stderr)
    return coord


def _filter_domains(coord, requested: str) -> list[str]:
    requested = str(requested or "").strip()
    if not requested:
        if not coord.domains:
            print("[warn] no profile-enabled module registered", file=sys.stderr)
        return list(coord.domains)
    if requested not in coord.domains:
        print(
            f"[error] unknown profile domain: {requested} "
            f"(available: {', '.join(coord.domains) or '(none)'})",
            file=sys.stderr,
        )
        return []
    return [requested]


def _cmd_profile(args: argparse.Namespace) -> int:
    coord = _build_profile_coordinator()
    domains = _filter_domains(coord, args.domain)
    if not domains:
        return 2

    sub = args.profile_command
    if sub == "load":
        report: dict[str, str] = {}
        for d in domains:
            mgr = coord.get(d)
            assert mgr is not None
            try:
                mgr.load()
                report[d] = "loaded"
            except Exception as exc:
                report[d] = f"error: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(v == "loaded" for v in report.values()) else 1

    if sub == "dump":
        out: dict[str, object] = {}
        for d in domains:
            mgr = coord.get(d)
            assert mgr is not None
            try:
                out[d] = mgr.dump_current()
            except Exception as exc:
                out[d] = {"error": str(exc)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if sub == "export":
        report = {}
        for d in domains:
            mgr = coord.get(d)
            assert mgr is not None
            try:
                mgr.sync_to_yaml()
                report[d] = f"exported to {mgr.yaml_path}"
            except Exception as exc:
                report[d] = f"error: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all("error" not in v for v in report.values()) else 1

    if sub == "reset":
        report = {}
        for d in domains:
            mgr = coord.get(d)
            assert mgr is not None
            try:
                mgr.reset()
                report[d] = "reset"
            except Exception as exc:
                report[d] = f"error: {exc}"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if all(v == "reset" for v in report.values()) else 1

    print(f"[error] unknown profile subcommand: {sub}", file=sys.stderr)
    return 2


def _require_job_manager(coord):
    """从 coordinator 拿 JobProfileManager 并保证类型正确。"""
    from .modules.job.profile.manager import JobProfileManager

    mgr = coord.get("job")
    if mgr is None:
        print(
            "[error] job profile manager not registered; is pulse.modules.job loaded?",
            file=sys.stderr,
        )
        return None
    if not isinstance(mgr, JobProfileManager):
        print(
            f"[error] registered job manager is {type(mgr).__name__}, expected JobProfileManager",
            file=sys.stderr,
        )
        return None
    return mgr


def _parse_hc_value(field: str, raw_values: list[str]) -> Any:
    """把 CLI 原始字符串映射到 JobMemory.set_hard_constraint 期望的类型。"""
    f = (field or "").strip()
    if f in {"preferred_location", "target_roles"}:
        return [v for v in (x.strip() for x in raw_values) if v]
    if f == "salary_floor_monthly":
        if len(raw_values) != 1:
            raise ValueError(f"field {f!r} expects a single integer value")
        try:
            return int(raw_values[0])
        except ValueError as exc:
            raise ValueError(f"field {f!r} expects integer, got {raw_values[0]!r}") from exc
    if f == "experience_level":
        if len(raw_values) != 1:
            raise ValueError(f"field {f!r} expects a single string value")
        return raw_values[0].strip()
    # 未知字段, 先当 str 处理让 set_hard_constraint 抛白名单错误
    return raw_values[0] if len(raw_values) == 1 else list(raw_values)


def _cmd_job(args: argparse.Namespace) -> int:
    coord = _build_profile_coordinator()
    mgr = _require_job_manager(coord)
    if mgr is None:
        return 2
    mem = mgr.memory()

    sub = args.job_command

    if sub == "snapshot":
        snap = mem.snapshot()
        if getattr(args, "md", False):
            print(snap.to_prompt_section())
            return 0
        print(json.dumps(snap.to_dict(), ensure_ascii=False, indent=2))
        return 0

    if sub == "reset":
        if not args.yes:
            prompt = (
                f"[pulse] 将清空所有 job.* facts 并删除:\n"
                f"  - {mgr.yaml_path}\n"
                f"  - {mgr.resume_md_path}\n"
                f"确认? (输入 'yes' 继续) > "
            )
            try:
                answer = input(prompt)
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                print("[pulse] aborted")
                return 1
        mgr.reset()
        print(json.dumps({"ok": True, "reset": "job"}, ensure_ascii=False, indent=2))
        return 0

    if sub == "item":
        return _cmd_job_item(args, mem)

    if sub == "hc":
        return _cmd_job_hc(args, mem)

    if sub == "resume":
        return _cmd_job_resume(args, mgr, mem)

    print(f"[error] unknown job subcommand: {sub}", file=sys.stderr)
    return 2


def _cmd_job_item(args: argparse.Namespace, mem) -> int:
    sub = args.item_command

    if sub == "add":
        try:
            item = mem.record_item({
                "type": args.type,
                "target": args.target,
                "content": args.content,
                "raw_text": args.raw_text or args.content,
                "valid_until": args.valid_until,
            })
        except Exception as exc:   # noqa: BLE001
            print(f"[error] record_item failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(
            {"ok": True, "item": item.to_dict()}, ensure_ascii=False, indent=2,
        ))
        return 0

    if sub == "list":
        items = mem.list_items(
            type=args.type, target=args.target,
            include_expired=args.include_expired,
        )
        payload = {
            "count": len(items),
            "items": [it.to_dict() for it in items],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if sub == "retire":
        removed = mem.retire_item(args.id)
        print(json.dumps(
            {"ok": True, "id": args.id, "retired": removed},
            ensure_ascii=False, indent=2,
        ))
        return 0 if removed else 1

    print(f"[error] unknown job item subcommand: {sub}", file=sys.stderr)
    return 2


def _cmd_job_hc(args: argparse.Namespace, mem) -> int:
    sub = args.hc_command

    if sub == "set":
        try:
            value = _parse_hc_value(args.field, list(args.values or []))
            mem.set_hard_constraint(args.field, value)
        except ValueError as exc:
            print(f"[error] set_hard_constraint failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(
            {"ok": True, "field": args.field, "value": value},
            ensure_ascii=False, indent=2,
        ))
        return 0

    if sub == "unset":
        try:
            removed = mem.unset_hard_constraint(args.field)
        except ValueError as exc:
            print(f"[error] unset_hard_constraint failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(
            {"ok": True, "field": args.field, "removed": removed},
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"[error] unknown job hc subcommand: {sub}", file=sys.stderr)
    return 2


def _cmd_job_resume(args: argparse.Namespace, mgr, mem) -> int:
    from pathlib import Path

    sub = args.resume_command

    if sub == "show":
        resume = mem.get_resume()
        if resume is None or not resume.raw_text:
            print("(no resume stored in memory)")
            return 0
        print(resume.raw_text)
        return 0

    if sub == "load":
        path = Path(args.path) if getattr(args, "path", None) else mgr.resume_md_path
        if not path.is_file():
            print(f"[error] resume file not found: {path}", file=sys.stderr)
            return 1
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            print(f"[error] cannot read {path}: {exc}", file=sys.stderr)
            return 1
        # 跟 manager._strip_md_header 行为对齐
        if text.lstrip().startswith("<!--"):
            end = text.find("-->")
            if end >= 0:
                text = text[end + len("-->"):]
        text = text.strip()
        if not text:
            print(f"[error] resume file is empty after stripping header: {path}", file=sys.stderr)
            return 1
        mem.update_resume(text)
        print(json.dumps(
            {"ok": True, "loaded_from": str(path), "chars": len(text)},
            ensure_ascii=False, indent=2,
        ))
        return 0

    print(f"[error] unknown job resume subcommand: {sub}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "start":
        return _cmd_start(args)
    if args.command == "profile":
        return _cmd_profile(args)
    if args.command == "job":
        return _cmd_job(args)
    parser.error(f"unsupported command: {args.command}")
    return 2
