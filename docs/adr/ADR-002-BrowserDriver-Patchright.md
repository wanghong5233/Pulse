# ADR-002: BrowserDriver = patchright

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-04-21 |
| 作用域 | `src/pulse/mcp_servers/_boss_platform_runtime.py`、`scripts/boss_login.py`、`pyproject.toml[browser]`、`docs/Pulse-MCP优先实施方案.md` |
| 关联 | `docs/archive/debug-boss-antibot-postmortem.md`、`docs/archive/browser-agent-architecture-decision.md`、`ADR-001-ToolUseContract.md` |

---

## 1. 现状

BOSS 平台 MCP gateway (`boss_platform_gateway`) 的浏览器子系统只使用 `patchright`（Playwright fork，在 Chromium 二进制层移除 CDP 可检测特征），不再使用原生 `playwright` 及任何 JS 层反检测 workaround。

```text
boss_platform_gateway  ──POST /call scan_jobs──▶  _boss_platform_runtime
                                                        │
                                                        ▼
                                          patchright.sync_api.sync_playwright
                                                        │
                                                        ▼
                                   launch_persistent_context(user_data_dir=...)
                                                        │
                                                        ▼
                                         BOSS (zhipin.com) — 无 about:blank 重定向
```

---

## 2. 分层职责

| 层 | 负责 | 不负责 |
|---|---|---|
| `patchright` Chromium 二进制补丁 | 移除 CDP 指纹（`navigator.webdriver`、`Runtime.enable` 痕迹、Target.attachedToTarget 等） | 业务逻辑、profile 管理、审计 |
| `_boss_platform_runtime._ensure_browser_page` | persistent profile 单例、headed 开关、legacy env fail-loud | 反检测、指纹伪造 |
| `scripts/boss_login.py` | 交互式扫码登录 + 写回 profile cookie | MCP runtime 生命周期 |
| `pyproject.toml[browser] extra` | 声明 `patchright>=1.48` 为浏览器能力的唯一依赖 | 运行时安装 Chromium 二进制（`patchright install chromium` 手动） |

---

## 3. 第一性原理

| 维度 | 分析 | 结论 |
|---|---|---|
| 反爬检测层级 | BOSS 在 CDP 协议层检测：JS-API 拦截、UA 伪装、`ignore_default_args` 均无效；原生 Google Chrome + Playwright 同样被识别（postmortem §二） | 只有二进制层 patch 可过，patchright 是这一层的唯一成熟方案 |
| 依赖体量 | `patchright` 本体 ≈ `playwright` (46 MB wheel) + 一份 Chromium bundle (~170 MB)；无需额外 `playwright_stealth`/`puppeteer-extra` 系列 | 单库替换，复杂度不增反减 |
| API 兼容性 | patchright 仅改 Chromium 二进制与少量 runtime 初始化；`sync_api` / `async_api` 签名与 Playwright 1.x 100% 兼容 | 迁移成本 = 改 import 一行 + 清理历史 workaround |
| 维护风险 | patchright 跟随 Playwright 版本发布（通常滞后 1~2 周）；BOSS 若升级到 TLS 指纹或行为分析，patchright 不够 | 可接受：现阶段 CDP 是主要检测手段；升级后再评估 |
| 回退成本 | 所有反检测 workaround（`KILL_ZHIPIN_FRAME_JS`、`iframe-core` 拦截、`playwright_stealth`）已删，env 开关 fail-loud | 防止重构无意中把 `from patchright` 改回 `from playwright`，有专门 regression test 兜底 |

---

## 4. 接口契约

### 4.1 import 路径（唯一）

```python
from patchright.sync_api import sync_playwright
```

**不变式**：
- `src/pulse/mcp_servers/_boss_platform_runtime.py` 与 `scripts/boss_login.py` 是仅有的两个合法引用点
- 任何 `from playwright.sync_api import ...` 在 BOSS 相关路径出现即违反本 ADR
- 违反由 `tests/pulse/core/test_boss_platform_runtime.py::test_runtime_uses_patchright_not_playwright` regression test 自动拦截

### 4.2 launch 参数

```python
launch_persistent_context(
    user_data_dir=_browser_profile_dir(),  # ~/.pulse/boss_browser_profile
    headless=_browser_headless(),          # 默认 False
    no_viewport=True,
    args=[
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
    ],
    # user_agent 显式留空 → 由 Chromium 自身输出, 避免 UA 版本与内核不一致
)
```

**不变式**：
- 不传 `ignore_default_args`（会干扰 patchright 的默认 patch flag）
- 不传固定 `user_agent`（`_DEFAULT_BROWSER_UA` 仅供 `health()` 展示；生产路径走 Chromium 默认 UA）
- `--no-sandbox` 必须保留（WSL/Docker 下非 sandbox 用户）

### 4.3 legacy env fail-loud

| 环境变量 | 语义 | 运行时行为 |
|---|---|---|
| `PULSE_BOSS_BROWSER_STEALTH_ENABLED` | 已弃用 | 设为非空 `true/1/yes/on` 以外的值 → `_ensure_browser_page` 抛 `RuntimeError` |
| `PULSE_BOSS_BROWSER_BLOCK_IFRAME_CORE` | 已弃用 | 设为 `true/1/yes/on` → `_ensure_browser_page` 抛 `RuntimeError`（拦截 `iframe-core.js` 会卡住 BOSS SPA） |

---

## 5. 可逆性 / 重评触发条件

1. patchright 连续 30 天落后 Playwright 主线 ≥ 2 个 minor 版本
2. BOSS 反爬升级，`patchright` 也被 about:blank 重定向（以 `boss_login.py` 连续 3 次扫码均被反爬页取代为信号）
3. 出现比 patchright 更低维护负担的替代方案（如官方 Playwright 集成 CDP hide）

触发任一项后重评 ADR，**指标以运行时为准，不在无数据时提前决策**。

---

## 6. 历史背景（只列指针，不复述）

- 根因分析与 20 轮误判复盘：`docs/archive/debug-boss-antibot-postmortem.md`
- 反爬层级模型（L1 navigator.webdriver → L5 服务端指纹）：同上 §五.教训 2
- 早期架构选型（原生 Chrome 登录 → patchright 统一）：`docs/archive/browser-agent-architecture-decision.md`
