# Pulse 发布前检查清单（M7 收尾）

> 目标：在正式发布前，以最小成本完成可回滚、可审计、可验证的上线准备。  
> 适用范围：`M7 进化引擎` 全量能力（治理、审计、版本化、导出、观测）。

---

## 1. 预发布冻结

- 代码冻结：除阻断缺陷外，不再新增功能。
- 配置冻结：锁定 `config/evolution_rules.json` 与生产 `.env`。
- 数据备份：备份 `~/.pulse/` 下关键文件（记忆、审计、规则版本）。
- 回滚预案：确认至少一个可回滚规则版本存在。

---

## 2. 自动化验收（必须通过）

在仓库根目录执行：

```bash
pytest tests/pulse -q
```

通过标准：

- 用例全绿（当前基线：`96 passed`）。
- 不允许出现 flaky（同一提交连续两次结果不一致）。

---

## 3. 核心 API 冒烟清单

### 3.1 治理与进化

- `GET /api/evolution/status`
- `GET /api/evolution/audits`
- `GET /api/evolution/audits/stats`
- `GET /api/evolution/audits/export?format=json`
- `GET /api/evolution/audits/export?format=csv`
- `GET /api/evolution/dashboard`
- `POST /api/evolution/reflect`
- `POST /api/evolution/rollback`

### 3.2 治理规则管理

- `GET /api/evolution/governance/mode`
- `POST /api/evolution/governance/mode`
- `POST /api/evolution/governance/reload`
- `GET /api/evolution/governance/versions`
- `GET /api/evolution/governance/versions/diff`
- `POST /api/evolution/governance/versions/rollback`

### 3.3 记忆与训练对

- `GET /api/memory/archival/recent`
- `POST /api/memory/archival/query`
- `GET /api/learning/dpo/status`
- `GET /api/learning/dpo/recent`
- `POST /api/learning/dpo/collect`

---

## 4. 关键配置检查（生产）

至少确认以下配置有效：

- `PULSE_EVOLUTION_RULES_PATH`
- `PULSE_GOVERNANCE_AUDIT_PATH`
- `PULSE_GOVERNANCE_RULES_VERSIONS_PATH`
- `PULSE_ARCHIVAL_MEMORY_PATH`
- `PULSE_DPO_PAIRS_PATH`
- `PULSE_DPO_AUTO_COLLECT`

建议同时检查：

- `PULSE_EVOLUTION_DEFAULT_MODE`
- `PULSE_EVOLUTION_PREFS_MODE`
- `PULSE_EVOLUTION_SOUL_MODE`
- `PULSE_EVOLUTION_BELIEF_MODE`

> 注意：若上述模式环境变量显式设置，会覆盖规则文件同名项。

---

## 5. 回滚演练（上线前必做）

### 演练 A：治理模式回滚

1. 调整一个模式（`POST /api/evolution/governance/mode`，`persist=true`）。
2. 记录返回的 `new_version_id`。
3. 调用 `GET /api/evolution/governance/versions/diff`，确认规则差异可见。
4. 用 `POST /api/evolution/governance/versions/rollback` 回滚到旧版本。
5. 再次 `GET /api/evolution/governance/mode`，确认模式恢复。

通过标准：模式可前进、可回退、规则文件可持久化。

### 演练 B：审计导出追溯

1. `GET /api/evolution/audits/export?format=json&limit=1`，检查 `next_cursor`。
2. 用 `cursor=next_cursor` 拉取下一页。
3. 使用 `start_at/end_at` 做时间范围过滤。
4. 导出 CSV，检查响应头包含 `X-Next-Cursor`。

通过标准：分页、过滤、导出均可用且字段完整。

---

## 6. 观测面板检查

`GET /api/evolution/dashboard` 必须包含：

- `audits.stats`（聚合统计）
- `audits.trends.hourly` / `audits.trends.daily`（趋势序列）
- `audits.alerts`（异常告警）
- `memory.archival_total` / `memory.dpo_pairs_total`

上线前重点关注：

- `pending_approval` 是否积压
- `blocked_by_gate` 是否异常激增
- `rejected` 是否持续上升

---

## 7. 发布通过标准（Go/No-Go）

满足以下全部条件才允许发布：

- 自动化测试通过
- API 冒烟通过
- 规则回滚演练通过
- 审计导出演练通过
- dashboard 指标可观测且无阻断告警

若任一项失败：

- 立即停止发布
- 记录缺陷与复现步骤
- 修复后重新执行本清单

---

## 8. 发布后 24 小时观察

- 每 2-4 小时检查一次 `dashboard.alerts`
- 重点追踪 `pending/gated/rejected` 三类波动
- 若异常持续，优先执行治理规则版本回滚

> 建议在发布窗口内保留人工值守，避免无人值守放大风险。
