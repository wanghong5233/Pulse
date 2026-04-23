"""JobProfileManager 端到端验证: yaml ↔ JobMemory roundtrip。

验证 memory 是单一事实源、yaml 是实时投影的一致性契约:
  - 编辑 yaml → load → memory 反映 yaml
  - mutate memory → sync_to_yaml → yaml 反映 memory
  - reset → memory 空 + yaml 空
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pulse.core.memory.workspace_memory import WorkspaceMemory
from pulse.core.profile.base import DomainProfileError
from pulse.modules.job.memory import JobMemory
from pulse.modules.job.profile.manager import JobProfileManager


class _FakeWorkspaceDB:
    """仅支持 WorkspaceMemory 用到的 SQL 形状的内存 DB。"""

    def __init__(self) -> None:
        # rows: list[dict[str, Any]]
        self.rows: list[dict[str, Any]] = []

    def execute(self, sql, params=None, *, fetch="none", commit=True):  # noqa: ANN001
        _ = commit
        norm = " ".join(str(sql).lower().split())
        p = tuple(params or ())

        if norm.startswith("create table") or norm.startswith("create index"):
            return None

        if norm.startswith("select value from workspace_facts where workspace_id"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (row["value"],)
            return None

        if norm.startswith("select id from workspace_facts where workspace_id"):
            ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    return (id(row),)
            return None

        if norm.startswith("update workspace_facts set value"):
            value, source, updated_at, ws, key = p
            for row in self.rows:
                if row["workspace_id"] == ws and row["key"] == key:
                    row["value"] = value
                    row["source"] = source
                    row["updated_at"] = updated_at
                    return None
            return None

        if norm.startswith("insert into workspace_facts"):
            workspace_id, key, value, source, created_at, updated_at = p
            self.rows.append({
                "workspace_id": workspace_id,
                "key": key,
                "value": value,
                "source": source,
                "created_at": created_at,
                "updated_at": updated_at,
            })
            return None

        if norm.startswith("delete from workspace_facts where workspace_id = %s and key = %s"):
            ws, key = p
            self.rows = [r for r in self.rows if not (r["workspace_id"] == ws and r["key"] == key)]
            return None

        if norm.startswith("select key, value, source, updated_at from workspace_facts where workspace_id"):
            ws, prefix_pattern = p
            prefix = prefix_pattern.rstrip("%")
            out = []
            for row in sorted(self.rows, key=lambda r: r["key"]):
                if row["workspace_id"] == ws and (not prefix or row["key"].startswith(prefix)):
                    out.append((
                        row["key"], row["value"], row["source"], row["updated_at"],
                    ))
            return out

        if norm.startswith("select count(*) from workspace_facts where workspace_id = %s and key like"):
            ws, pat = p
            prefix = pat.rstrip("%")
            cnt = sum(1 for r in self.rows if r["workspace_id"] == ws and r["key"].startswith(prefix))
            return (cnt,)

        if norm.startswith("delete from workspace_facts where workspace_id = %s and key like"):
            ws, pat = p
            prefix = pat.rstrip("%")
            self.rows = [
                r for r in self.rows
                if not (r["workspace_id"] == ws and r["key"].startswith(prefix))
            ]
            return None

        # summary / essentials / 其它 SQL 暂不测试
        raise AssertionError(f"unexpected SQL in fake DB: {sql}")


@pytest.fixture()
def manager(tmp_path: Path) -> JobProfileManager:
    ws = WorkspaceMemory(db_engine=_FakeWorkspaceDB())
    yaml_path = tmp_path / "job.yaml"
    resume_md_path = tmp_path / "resume.md"
    return JobProfileManager(
        workspace_memory=ws,
        workspace_id="job.test",
        yaml_path=yaml_path,
        resume_md_path=resume_md_path,
    )


def test_load_missing_yaml_is_noop(manager: JobProfileManager) -> None:
    assert not manager.yaml_path.exists()
    manager.load()
    dumped = manager.dump_current()
    assert dumped["target_roles"] == []
    assert dumped["blocked_companies"] == []


def test_load_seeds_memory_from_yaml(manager: JobProfileManager) -> None:
    manager.yaml_path.write_text(
        """
target_roles:
  - Agent Engineer
preferred_location:
  - 杭州
salary_floor_daily: 800
remote_ok: true
blocked_companies:
  - name: 拼多多
    reason: 笔试挂了
blocked_keywords:
  - keyword: 外包
    reason: ""
""",
        encoding="utf-8",
    )
    manager.load()
    dumped = manager.dump_current()
    assert dumped["target_roles"] == ["Agent Engineer"]
    assert dumped["preferred_location"] == ["杭州"]
    assert dumped["salary_floor_daily"] == 800
    assert dumped["remote_ok"] is True
    assert dumped["blocked_companies"] == [{"name": "拼多多", "reason": "笔试挂了"}]
    assert dumped["blocked_keywords"] == [{"keyword": "外包", "reason": ""}]


def test_load_invalid_schema_raises(manager: JobProfileManager) -> None:
    manager.yaml_path.write_text(
        "unknown_top_field: 42\n",
        encoding="utf-8",
    )
    with pytest.raises(DomainProfileError):
        manager.load()


def test_sync_to_yaml_reflects_memory_mutation(manager: JobProfileManager) -> None:
    mem = JobMemory(
        workspace_memory=manager._ws,  # type: ignore[attr-defined]
        workspace_id="job.test",
    )
    mem.block_company("字节跳动", reason="加班太多")
    mem.set_preference("preferred_location", ["上海"])

    manager.sync_to_yaml()
    text = manager.yaml_path.read_text(encoding="utf-8")
    import yaml
    parsed = yaml.safe_load(text)
    assert parsed["preferred_location"] == ["上海"]
    assert parsed["blocked_companies"] == [{"name": "字节跳动", "reason": "加班太多"}]


def test_load_replaces_memory_not_merges(manager: JobProfileManager) -> None:
    """关键契约: yaml load 是全量替换, 不是合并。memory 里没写进 yaml 的 job.* 会被清掉。"""
    mem = JobMemory(
        workspace_memory=manager._ws,  # type: ignore[attr-defined]
        workspace_id="job.test",
    )
    mem.block_company("旧公司", reason="load 前存在")
    mem.set_preference("preferred_location", ["北京"])

    manager.yaml_path.write_text(
        "preferred_location:\n  - 杭州\n",
        encoding="utf-8",
    )
    manager.load()

    dumped = manager.dump_current()
    assert dumped["preferred_location"] == ["杭州"]
    assert dumped["blocked_companies"] == []  # "旧公司" 被清


def test_reset_empties_memory_and_yaml(manager: JobProfileManager) -> None:
    mem = JobMemory(
        workspace_memory=manager._ws,  # type: ignore[attr-defined]
        workspace_id="job.test",
    )
    mem.block_company("拼多多", reason="")
    manager.sync_to_yaml()
    assert manager.yaml_path.exists()

    manager.reset()
    dumped = manager.dump_current()
    assert dumped["blocked_companies"] == []
    import yaml
    parsed = yaml.safe_load(manager.yaml_path.read_text(encoding="utf-8"))
    assert parsed["blocked_companies"] == []


def test_auto_sync_roundtrip_keeps_memory_and_yaml_equal(manager: JobProfileManager) -> None:
    """模拟 after_tool_use 场景: 每次 mutation 后立即 sync_to_yaml, 验证两者一致。"""
    mem = JobMemory(
        workspace_memory=manager._ws,  # type: ignore[attr-defined]
        workspace_id="job.test",
    )

    mem.block_company("A", reason="r1")
    manager.sync_to_yaml()
    mem.block_keyword("外包", reason="")
    manager.sync_to_yaml()
    mem.set_preference("preferred_location", ["上海", "杭州"])
    manager.sync_to_yaml()

    memory_view = json.dumps(manager.dump_current(), ensure_ascii=False, sort_keys=True)

    # 重新从 yaml load (模拟重启 / reload), 应得到完全一致的状态
    manager.load()
    after_load_view = json.dumps(manager.dump_current(), ensure_ascii=False, sort_keys=True)
    assert memory_view == after_load_view
