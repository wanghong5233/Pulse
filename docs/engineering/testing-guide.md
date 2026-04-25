# LLM 应用测试工程化指南

> 定位：Pulse 测试策略的**工程化基线**，面向 LLM + 浏览器自动化 + 多模块 Agent 系统。
> 关联文档：`../code-review-checklist.md`（工程宪法 · §2 测试）、`../adr/ADR-001-ToolUseContract.md`（三契约）

---

## 1. 现状陈述

Pulse 在跑四类测试，覆盖四个不同的失败面：

| 类别 | 跑在 | 目的 | 失败信号 |
|---|---|---|---|
| 单元测试 | 纯函数 / 有限状态机 | 断言**算子本身**在输入域内行为正确 | 算子写错 |
| 集成测试 | 模块 A + 模块 B 真实拼接 | 断言**调用链上装配**正确 | 接线错 |
| 合同测试 | 跨层 / 跨进程 / 跨真实 DOM | 断言**跨边界约定**未被破坏 | 契约漂移 |
| 反回归测试 | 某条已复盘的生产 bug 路径 | 钉住**这条 bug 不再复活** | 旧 bug 回来了 |

单元和集成是通用概念，本文只展开合同测试与反回归测试——LLM + 浏览器自动化这种"系统边界很多、子系统自己会变"的场景，真正拦 bug 的是这两类。

---

## 2. 合同测试（Contract Test）

### 2.1 定义与由来

合同测试断言的是**跨边界的不变式**，不是某段代码算得对不对。

术语源于 Bertrand Meyer 的 *Design by Contract*：一个模块对外暴露三件东西——前置条件（调用方必须满足）、后置条件（返回时必须满足）、不变式（无论怎么调都成立）。合同测试就是把这三件事写成可执行断言。

边界的三种形态：

| 边界 | 合同双方 | Pulse 实例 |
|---|---|---|
| 进程内分层 | 上层 ↔ 下层 | `JobChatService` ↔ `_boss_platform_runtime`（MCP） |
| 跨进程/网络 | Client ↔ Server | MCP RPC、LLM provider、HTTP API |
| 代码 ↔ 外部系统 | 选择器 ↔ 真实 DOM | CSS selector ↔ BOSS 页面 |

### 2.2 与单元测试的区别

| 维度 | 单元测试 | 合同测试 |
|---|---|---|
| 断言对象 | 一个函数的输入→输出 | 一个模块对外的**承诺** |
| 失败意味着 | 这段代码算错了 | 这个承诺被破坏了（可能是上游改了、可能是下游改了、可能是两边都改了） |
| Mock 策略 | 尽量少 mock | 用 fake 精确复现对方合法/非法的返回形态 |
| 写作姿态 | 覆盖算子输入域 | 覆盖**接口承诺的每一条**，一条一个 test |

### 2.3 四种形态

#### 形态 A — 行为契约（Behavioral Contract）

**用途**：断言"当下游返回形态 X 时，上层必须做出反应 Y"。

**做法**：用 fake 精确复现下游的返回形态（包括失败态），断言上层的可观察反应。

**签名骨架**：

```python
async def test_<上层>_on_<下游状态>_must_<上层反应>():
    fake_downstream = Fake(return_value={...精确复现...})
    upstream = Upstream(downstream=fake_downstream)
    result = await upstream.do_something()
    assert result.ok is <预期>
    assert result.status == "<预期 status>"
```

**Pulse 实例**（`tests/pulse/modules/job/chat/test_service_stage_events.py`）：

```python
async def test_logged_only_status_is_not_laundered_into_sent_for_send_resume():
    """
    合同: MCP 在 dry-run 模式下返回 status='logged_only'/ok=True,
    service 层禁止把它洗成 status='sent',必须保持 ok=False 透传.
    """
    fake_connector = _fake_mcp(return_value={
        "ok": True,
        "status": "logged_only",
        "card_hit": True,
    })
    service = JobChatService(connector=fake_connector)
    result = await service._execute_send_resume(ctx=..., payload=...)
    assert result.ok is False          # 关键: 不能被洗成 True
    assert result.status == "logged_only"  # 关键: 原始 status 必须透传
```

**拦的是什么**：service 层原先只看 `ok` 字段，看到 `True` 就当作"真的投出去了"。这个测试把"`status` 必须进入 ok 判定"这条承诺写成 hard 断言。下次谁再写 `ok = mcp_result["ok"]`，这条测试会直接红。

**代价**：20 行左右，跑 < 10 ms。

---

#### 形态 B — 结构契约（Structural / Source Invariant）

**用途**：断言"某个函数的执行路径**必须**包含某个关键步骤"，即使这个步骤难以在行为层可靠触发。

**做法**：读被测函数的源码（`inspect.getsource`），对源码文本做正则/子串断言。

**什么时候用**：

- 关键步骤在行为层难以制造（比如"调用完 send 后必须 poll DOM"，行为测试需要真实浏览器）
- 想防止有人"重构时顺手删掉"关键保护步骤（日志打点、兜底校验、post-send verify）
- 算术复杂度：一个 selector/断言点在 100 行函数里重构多次，用结构契约钉住它的存在比搭 fake 便宜

**签名骨架**：

```python
def test_<函数>_must_call_<关键步骤>():
    src = inspect.getsource(<函数>)
    assert "<关键函数名>" in src, "<函数> 必须包含 <关键步骤> 才能保证 <承诺>"
```

**Pulse 实例**（`tests/pulse/mcp_servers/test_boss_send_resume_selectors.py`）：

```python
def test_reply_executor_waits_for_dom_delta_after_send_click():
    """
    合同: _execute_browser_reply 在点击 send 后必须 poll DOM delta,
    不允许看到 click ok 就直接 return success.
    """
    src = inspect.getsource(_execute_browser_reply)
    assert "_snapshot_resume_send_state" in src
    assert "_wait_direct_resume_send_effect" in src
    assert "verify_failed" in src   # 失败分支必须存在
```

**注意事项**：

- 结构契约是**弱于**行为契约的，只证明"代码里写了"，不证明"运行时一定会走到"。所以它通常与行为测试配对出现。
- 断言的粒度要大（函数名、字符串常量），不要断到空格、参数顺序、具体行号——否则变成反重构负资产。
- 首选行为测试；只有当行为层造不出场景时才回退到结构契约。

---

#### 形态 C — 选择器契约（Selector Contract / External Contract）

**用途**：断言"代码里的 CSS selector / XPath / API 字段路径在真实外部系统里**真的指向那个东西**"。

**做法**：把真实外部响应（DOM 快照、API JSON、LLM 输出）作为 fixture 存盘，测试加载 fixture，让代码的 selector 跑一遍，断言命中数 > 0 且内容符合预期。

**为什么必须有**：外部系统（BOSS 前端、LLM provider schema）会在你不知情的情况下变。没有合同测试，selector 漂移只在**生产出 bug 后**才被发现。

**签名骨架**：

```python
def test_<selector>_reachable_on_real_<page>():
    html = Path(FIXTURE_DIR / "<page_dump>.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    matched = soup.select("<selector 表达式>")
    assert matched, "selector 漂移: <selector> 在真实 <page> 上未命中"
    assert <内容检查>
```

**Pulse 实例**（`tests/pulse/mcp_servers/test_boss_send_resume_selectors.py`）：

```python
def test_resume_button_reachable_under_chat_controls():
    """
    合同: _RESUME_BUTTON_SELECTORS 必须能在 chat-detail 真实 DOM dump 里命中.
    """
    html = (FIXTURE_DIR / "chat-detail" / "20260422T073442Z.html").read_text("utf-8")
    soup = BeautifulSoup(html, "html.parser")
    for selector in _RESUME_BUTTON_SELECTORS:
        if soup.select(selector):
            return
    raise AssertionError(f"selector drift: none of {_RESUME_BUTTON_SELECTORS} hit real DOM")
```

**Fixture 管理纪律**：

- Fixture 存 `docs/dom-specs/<platform>/<page>/<timestamp>.html|.json`，文件名带 UTC 时间戳
- 每条 fixture 配 `.json` 元信息（URL、抓取时机、页面状态）
- 过期 fixture 移进 `docs/dom-specs/<platform>/<page>/archive/`，不要删
- 不要手搓 HTML 当 fixture——必须是真实 dump

**成本**：单测 < 50 ms；fixture 平均生命周期 2~4 周（视外部页面改版频率）。

---

#### 形态 D — 反回归测试（Anti-Regression / Characterization Test）

**用途**：钉住一条已在生产发生、已复盘、已修复的 bug，保证它不再复活。

**做法**：把 bug 的"最小触发条件"固化成一个测试，失败信息里写清楚**这条 bug 的 ID / 日期 / 现象**。

**与其他三类的区别**：前三类都是正向承诺；反回归测试是"负向禁区"——这条路径**曾经踩过，不许再踩**。

**签名骨架**：

```python
def test_<功能>_does_not_regress_on_<bug 标识>():
    """
    Regression guard for <bug>:
    <日期> 生产观察: <现象>
    根因: <一句话>
    修复: <commit / PR>
    """
    # 复现 bug 的最小触发输入
    ...
    # 断言修复后的正确行为
    ...
```

**Pulse 实例**（节选）：

```python
async def test_auto_execute_event_surfaces_nested_send_resume_selector_error():
    """
    Regression: 2026-04-21 生产观察:
    _execute_browser_send_resume 在 selector 找不到按钮时抛的 error
    被吞在 nested result["card"]["error"] 里, top-level ok=True,
    service 层把它当成成功上报给用户.
    修复: _lift_error 把 nested error 抬到顶层.
    """
    ...
    assert event.payload["ok"] is False
    assert "selector" in event.payload["error"].lower()
```

**维护纪律**：

- 每修一个 P0/P1 生产 bug，必须配一条反回归测试——**这是结项的一部分**
- test docstring 必须含日期 + 一句话现象 + 一句话根因；将来读的人要能从 test 反推当时出了什么事
- 这类 test 数量只增不减（除非 bug 所在功能整体下线）

---

### 2.4 四种形态的选择矩阵

出现以下信号 → 写哪种：

| 信号 | 选 | 原因 |
|---|---|---|
| "下游返回 X，上游应该反应 Y"（两者都是你自己的代码） | A 行为契约 | 双方在控制下，fake 最精准 |
| "这个函数无论怎么改，都必须做 Z 步" | B 结构契约 | Z 步在行为层难制造或代价大 |
| selector / API path / 字段名指向**你不控制的外部系统** | C 选择器契约 | 外部会偷偷变，fixture 是唯一锚点 |
| 这条 bug 今天刚修 | D 反回归 | 强制把根因钉死 |

**一次合入通常配 1 条行为契约 + 1 条反回归，有 selector/source 改动再加 B/C。**

---

## 3. 大模型应用特有的测试点

LLM + Agent 系统相比普通后端多出三类测试需求：

### 3.1 Prompt 契约测试

**断言对象**：prompt 注入的变量**必须在最终文本里出现**。

**为什么**：LLM 应用里常见的 bug 是"变量忘拼进 prompt 了"——单元测试过了（函数返回了字符串），但字符串里少了关键上下文，LLM 回答稀烂。

**做法**：生成 prompt 后，对字符串做子串/正则断言。

```python
def test_reply_prompt_contains_hr_message_and_user_profile():
    prompt = build_reply_prompt(hr_msg="你好", user_profile={"name": "Xiao"})
    assert "你好" in prompt
    assert "Xiao" in prompt
    assert "<ROLE>" in prompt  # 结构锚点
```

**不做的事**：断言 prompt 里每个字都对——prompt 还在迭代，过细的断言只会变成反重构负资产。

### 3.2 Schema 合同测试（LLM 输出结构）

**断言对象**：LLM 返回的 JSON/结构化输出**能被下游 parser 接住**。

**做法**：

- 正路径：真实 provider 响应切片 → parser → 断言字段齐
- 毒路径：mock LLM 返回畸形 JSON / 缺字段 / 多字段 → 断言 parser 正确 fail-loud

```python
def test_llm_job_match_schema_rejects_missing_required_field():
    bad = {"match_score": 0.8}  # 缺 reason
    with pytest.raises(ValidationError):
        JobMatchResult.model_validate(bad)
```

### 3.3 工具调用契约（三契约审计）

Pulse 内已有 ADR-001 规定：描述（A）/ 调用（B）/ 执行验证（C）必须一致。对应到测试：

| 契约 | 测试形式 |
|---|---|
| A 描述 | 读 tool manifest，断言 schema 与实际 handler 签名一致 |
| B 调用 | 行为契约：给定合法/非法参数，handler 返回 `ActionReport` 结构正确 |
| C 执行验证 | 反回归 + selector 契约：真实 DOM 观察到 side-effect 才算 `ok=True` |

---

## 4. 成本预算

每一条合同测试都要通过这道门槛：

| 指标 | 阈值 | 超过就 |
|---|---|---|
| 代码行数 | ≤ 40 行（含 docstring） | 拆或删 |
| 运行时间 | ≤ 50 ms | 移到集成层 |
| 失败时的可诊断性 | 看消息就能定位根因 | 重写断言消息 |
| 半年内失败次数 | ≥ 1 | 保留，这是它在干活 |
| 半年内失败次数 | = 0 且多次改动都不影响 | 候选删除（可能是同源复读） |

---

## 5. 反模式（命中即删）

| 反模式 | 特征 | 原因 |
|---|---|---|
| 同源复读 | `assert _MAX == 8`（测常量等于自己） | 不拦任何 regression |
| 过度 mock | 被测函数所有依赖都 mock，断言退化成"调用次数" | 测的是调用图，不是行为 |
| 手搓 fixture | 手工写 HTML/JSON 当 fixture，字段形态与真实响应不一致 | 形态造假，生产会在 fixture 覆盖不到的字段上爆 |
| 测 stdlib | 断言 `pydantic` / `fastapi` / `sqlalchemy` 已知行为 | 不是你的职责 |
| per-field unit | 一个 dataclass 每个 getter 一个 test | 聚合成一条整体契约 |
| 绿色但无声 | 一条 test 永远绿，删了没人发现异常 | 无区分力，删 |
| 结构契约断到空格 | `assert "  return" in src` | 反重构负资产 |

---

## 6. 与三契约架构的关系

Pulse 的三契约（ADR-001）规定工具要同时提供描述 / 调用 / 执行验证。本文档给出的四种合同测试形态**就是把三契约从"设计规范"落成"可执行门禁"**的手段：

| 三契约 | 落到测试上 |
|---|---|
| A 描述 | Schema 合同测试（§3.2） |
| B 调用 | 行为契约（§2.3 形态 A） |
| C 执行验证 | 结构契约（§2.3 形态 B） + 选择器契约（§2.3 形态 C） |
| 生产 bug 回路 | 反回归测试（§2.3 形态 D） |

没有这层测试，ADR-001 就只是 markdown；有了，它才是 runtime 真正遵守的约束。

---

## 7. 参考

- Bertrand Meyer, *Object-Oriented Software Construction*（Design by Contract 原典）
- Martin Fowler, *Contract Test* — https://martinfowler.com/bliki/ContractTest.html
- Kent Beck — "Test desired behavior, not implementation"
- Kent C. Dodds — *Testing Trophy*（集成 > 单元金字塔）
- Pact（consumer-driven contract testing 框架）
- 本仓工程约束：`docs/code-review-checklist.md` §2（测试）与 §4（系统形态）
