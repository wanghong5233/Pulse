# BOSS 直聘浏览器自动化：技术方案调研与架构决策

> 日期：2026-03-14 ~ 2026-03-16  
> 状态：**两条 JD 提取路径均已实现并通过验证**  
> 上下文：OfferPilot 需要对 BOSS 直聘实现多条自动化路径（搜索浏览 JD、从对话获取 JD、打招呼、自动回复），涉及复杂的页面导航和多标签页交互。  
> 核心问题：**用 LLM 视觉 Agent 驱动浏览器，还是用 Playwright 硬编码选择器？**

---

## 一、BOSS 直聘关键页面流分析

在做技术选型之前，必须先理解要自动化的**具体页面流转**。

### 路径 1：搜索浏览 → JD 详情

```
首页搜索框输入关键词
    ↓
搜索结果列表页 (zhipin.com/web/geek/jobs?query=...)
    ↓  提取 job-card-wrapper 列表
    ↓
点击某个职位卡片 → 新标签页
    ↓
JD 详情页 (zhipin.com/job_detail/...)
    ↓  提取 .job-sec-text 完整 JD 文本
```

特点：页面结构稳定，CSS 选择器可靠，标准的列表→详情模式。

### 路径 2：对话列表 → 查看职位 → JD 详情（更复杂）

```
消息页 (zhipin.com/web/geek/chat)
    ↓  左侧是会话列表（li[role="listitem"]）
    ↓
点击某个会话 → 右侧面板加载对话内容（SPA 局部刷新，URL 不变）
    ↓
对话面板顶部固定栏显示 [职位名称 + 薪资 + 城市] + [查看职位]
    ↓  ⚠️「查看职位」是 <SPAN> 元素，不是 <a> 标签！
    ↓  点击后浏览器新开标签页 → JD 详情页 (zhipin.com/job_detail/...)
    ↓  需要 context.expect_page() 捕获新标签页
    ↓  提取完整 JD → 关闭标签页 → 回到对话
```

特点：涉及 **SPA 局部刷新 + 多标签页跳转**，但流程是确定性的。

> **实战关键发现**（2026-03-16）：「查看职位」不在聊天消息流里，而是对话面板**顶部固定栏**的一部分。它渲染为 `<SPAN>` 而非 `<a>` ，通过 Vue 的 `@click` 事件触发 `window.open()` 跳转。这意味着只搜索 `<a>` 标签的 CSS 选择器（`a[href*='job_detail']` 等）永远无法找到它。

### 路径 3：打招呼 / 自动回复

```
消息页 → 点击会话 → 右侧对话面板
    ↓
定位输入框 ([contenteditable=true] / textarea)
    ↓
输入文本 → 点击发送按钮
```

特点：输入框选择器可能变化，但交互模式固定。

### 关键观察

> **所有路径的页面导航流程都是确定性的。** 不存在"需要 LLM 理解页面语义才能决定下一步操作"的场景。每一步该点什么、跳转到哪里、提取什么内容，在开发时就已经完全确定。

---

## 二、备选方案调研

### 方案 A：Playwright 硬编码选择器（当前方案）

手动分析页面 DOM 结构，用 CSS 选择器 / XPath 定位元素，用 Playwright API 执行点击、输入、导航。

```python
# 示例：从对话获取 JD（已验证通过 2026-03-16）
# 1. 点击左侧会话
page.locator('li[role="listitem"]').nth(idx).click()
page.wait_for_timeout(random.randint(2000, 4000))

# 2. 用文本选择器定位「查看职位」（它是 SPAN，不是 A 标签）
view_job = page.locator("text=查看职位").first
if view_job.count() > 0:
    with context.expect_page(timeout=10000) as new_page_info:
        view_job.click()
    new_page = new_page_info.value
    new_page.wait_for_load_state("domcontentloaded")
    jd_text = new_page.locator(".job-sec-text").inner_text()
    source_url = new_page.url
    new_page.close()
```

| 维度 | 评估 |
|------|------|
| **延迟** | 毫秒级（原生 DOM 操作） |
| **成本** | 零 LLM 调用费用 |
| **可靠性** | 高（选择器命中即成功，命中率可通过多候选选择器提高） |
| **维护成本** | BOSS 改版时需更新选择器（但可通过多选择器降级策略缓解） |
| **安全性** | 好控制（精确知道每一步做了什么） |
| **适用场景** | 页面结构已知、交互流程确定 |

### 方案 B：LLM 视觉 Agent（browser-use）

[browser-use](https://github.com/browser-use/browser-use)（80k+ stars）是当前最流行的 LLM 驱动浏览器 Agent 框架。工作循环：截图 → LLM 视觉识别 → 决定操作 → 执行 → 截图 → 循环。

```python
from browser_use import Agent
agent = Agent(
    task="打开BOSS直聘消息页面，点击第一个未读对话，找到查看职位链接并提取JD内容",
    llm=ChatOpenAI(model="gpt-4o"),
)
result = await agent.run()
```

| 维度 | 评估 |
|------|------|
| **延迟** | 秒级（每步操作 2-5 秒等待 LLM 响应） |
| **成本** | 高（每步操作消耗 vision token，一个完整流程可能 5-10 次 LLM 调用，约 $0.05-0.15/次） |
| **可靠性** | 中（LLM 可能误判元素、点错位置、陷入循环） |
| **维护成本** | 低（页面改版不需要改代码，LLM 自适应） |
| **安全性** | 风险较高（LLM 可能执行意外操作，如点错按钮发送不当消息） |
| **适用场景** | 页面结构未知或频繁变化、一次性探索任务 |

### 方案 C：Playwright MCP（微软官方）

[Playwright MCP](https://github.com/microsoft/playwright-mcp) 将 Playwright 暴露为 MCP Server，LLM 通过**无障碍树快照**（Accessibility Tree）而非截图来理解页面，然后发出操作指令。

```
LLM 收到的是结构化数据：
- button "查看职位" [ref=42]
- textbox "请输入..." [ref=55]
- listitem "黄女士 快商通 招聘经理" [ref=67]

LLM 返回操作：
browser_click(ref=42)
```

| 维度 | 评估 |
|------|------|
| **延迟** | 秒级（但比视觉方案快，不需要传输截图） |
| **成本** | 中（无障碍树文本 token 比图像 token 便宜，但仍需每步调用 LLM） |
| **可靠性** | 中高（结构化数据比截图更准确，但复杂 SPA 的无障碍树可能不完整） |
| **维护成本** | 低 |
| **安全性** | 中（操作可审计，但仍依赖 LLM 决策） |
| **适用场景** | 通用网页 Agent、复杂未知页面 |

### 方案 D：逆向 API 直接调用（boss-cli 方案）

[boss-cli](https://github.com/jackwener/boss-cli)（443 stars）通过逆向工程 BOSS 直聘的内部 HTTP API，绕过浏览器直接调用后端接口。

| 维度 | 评估 |
|------|------|
| **延迟** | 最快（HTTP 请求，无浏览器开销） |
| **成本** | 零 |
| **可靠性** | 高（API 比 DOM 稳定），但 API 变更时完全失效 |
| **维护成本** | 高（需要持续跟踪 API 变更、Token 刷新机制、反爬 `__zp_stoken__` 动态获取） |
| **安全性** | 风险（逆向 API 可能违反 ToS，且 token 机制复杂） |
| **适用场景** | 高频批量操作 |

---

## 三、决策分析

### 核心论点：对于确定性流程，LLM Agent 是过度设计

这是本项目最关键的架构判断。详细论证如下：

#### 1. 流程确定性 vs LLM 不确定性

BOSS 直聘的自动化操作本质上是**有限状态机**：

```
状态 S1（消息列表）→ 动作 A1（点击会话）→ 状态 S2（对话面板）
状态 S2 → 动作 A2（点击查看职位）→ 状态 S3（JD 详情新标签页）
状态 S3 → 动作 A3（提取文本）→ 状态 S4（关闭标签页）
```

每个状态转移的**前置条件、动作、后置状态**在开发时就已经完全确定。引入 LLM 来做这个决策，等于用一个概率性系统（LLM）去执行一个确定性任务——不仅没有收益，还引入了不确定性风险。

#### 2. 成本分析

按照每天处理 50 条 JD 的频率估算：

| 方案 | 单次 JD 提取成本 | 日均成本 | 月成本 |
|------|-----------------|---------|--------|
| Playwright 选择器 | ¥0 | ¥0 | ¥0 |
| browser-use (GPT-4o vision) | ~¥0.5（5-8 步 × ¥0.07/步） | ¥25 | ¥750 |
| Playwright MCP (text) | ~¥0.15（5-8 步 × ¥0.02/步） | ¥7.5 | ¥225 |
| boss-cli API | ¥0 | ¥0 | ¥0 |

对于一个实习生求职助手，每月 ¥225-750 的 LLM 调用费用于执行本可零成本完成的浏览器导航是不合理的。

#### 3. 安全性与可控性

求职场景对操作准确性要求极高：

- 发错消息给 HR → 影响求职印象，不可逆
- 点错按钮（如"不感兴趣"）→ 错过机会
- LLM 幻觉导致提取错误 JD 信息 → 匹配决策失误

Playwright 硬编码选择器每一步都是确定性的，可以精确预测和测试；LLM Agent 的每一步都有概率出错，且错误模式难以预测和复现。

#### 4. 反检测考量

LLM Agent 的操作模式（截图→等待→操作→截图→等待）产生的行为节奏和正常人差异很大（规律性的长停顿），反而更容易被反爬系统识别。Playwright 硬编码选择器可以精确控制随机延迟分布，更容易模拟真人行为模式。

### 那什么场景适合用 LLM Agent？

LLM 视觉 Agent 真正的价值在于：

1. **页面结构未知**：通用网页爬虫、面对任意网站
2. **流程不确定**：用户的自然语言任务描述无法预编码
3. **一次性探索**：不会重复执行的任务

OfferPilot 不符合以上任何一条。BOSS 直聘是唯一目标网站，流程完全确定，且会高频重复执行。

---

## 四、最终方案

**Playwright 硬编码选择器 + LLM 仅用于决策层**

```
┌─────────────────────────────────────────────────────┐
│  决策层（需要 LLM）                                   │
│  · JD 匹配评分：这个岗位是否适合我？                   │
│  · 意图分类：HR 在问什么？                             │
│  · 回复生成：该怎么回复？                              │
│  · 打招呼决策：要不要主动联系？                        │
└────────────────────┬────────────────────────────────┘
                     │ 决策结果
                     ▼
┌─────────────────────────────────────────────────────┐
│  执行层（不需要 LLM，纯 Playwright）                  │
│  · 导航到搜索页/消息页                                │
│  · 点击职位卡片/会话/查看职位                          │
│  · 捕获新标签页、提取 JD 文本                         │
│  · 在输入框中键入文本、点击发送                        │
│  · 截图、Cookie 管理                                  │
└─────────────────────────────────────────────────────┘
```

这个分层设计的核心原则：

- **LLM 做它擅长的事**：理解自然语言、语义分析、策略决策
- **Playwright 做它擅长的事**：精确的 DOM 操作、可靠的页面导航、可控的交互执行
- **不让 LLM 做它不擅长的事**：精确的 UI 元素定位、确定性的流程控制

### 与 OpenClaw 的关系

OpenClaw 的 browser-automation skill 本质上是 Playwright + stealth 的封装层，加了自然语言触发接口。其「智能」来源是把 LLM 放在操作循环中做决策。

OfferPilot 的选择是：**不使用 OpenClaw 的 browser-automation skill，而是直接使用 Playwright**。原因：
1. OpenClaw 作为 Agent Runtime 负责调度和 Skill 桥接，这是它的核心价值
2. 浏览器操作层直接用 Playwright，避免通过 OpenClaw 的抽象层增加不必要的间接性
3. BOSS 直聘的反爬对抗（参见 [antibot postmortem](./debug-boss-antibot-postmortem.md)）需要精细控制浏览器行为，中间抽象层会妨碍调试

### 选择器降级策略

为应对 BOSS 页面改版，对每个提取目标维护**多候选选择器**，按优先级依次尝试：

```python
JOB_TITLE_SELECTORS = [".job-name", ".job-title", "a"]
COMPANY_SELECTORS = [".company-name", ".company-text", ".company"]
JD_TEXT_SELECTORS = [".job-sec-text", ".job-detail-section .text", ".job-detail"]
```

当主选择器失效时自动降级，同时触发告警通知开发者更新选择器。这种维护成本远低于维护一个 LLM Agent 的 prompt 和 error handling。

---

## 五、实战踩坑全记录（2026-03-14 ~ 2026-03-16）

从对话获取 JD 的功能已完整实现并验证通过。以下是开发过程中遇到的全部关键问题、根因分析和解决方案。

### 坑 1：CDP 协议检测 —— Playwright + stealth 不够用

**现象**：使用 `playwright` + `playwright_stealth` 访问 BOSS 直聘任何页面，均被重定向到 `about:blank` 或登录页。

**根因**：BOSS 直聘的反爬不是简单的 `navigator.webdriver` 检测，而是在 **Chrome DevTools Protocol（CDP）** 层面进行检测。Playwright 通过 CDP 协议与浏览器通信，而 BOSS 直聘的 `iframe-core.js` 可以检测到这个协议连接的存在。`playwright_stealth` 只能修改 JavaScript 层面的指纹，无法掩盖底层 CDP 连接。

**解决方案**：迁移到 [patchright](https://github.com/AidoP/patchright)，它是 Playwright 的 fork，在 **Chromium 二进制层面** 打补丁，移除了 CDP 的可检测特征。安装后直接替换 import：

```python
# Before: from playwright.sync_api import sync_playwright
from patchright.sync_api import sync_playwright
```

**验证**：patchright 成功打开 BOSS 直聘所有页面（搜索、消息、职位详情），无 `about:blank` 重定向。

> 详见 [debug-boss-antibot-postmortem.md](./debug-boss-antibot-postmortem.md)

### 坑 2：SPA 异步认证竞态条件

**现象**：`page.goto("https://www.zhipin.com/web/geek/chat")` 返回后立刻检查登录状态显示"已登录"，但 3-5 秒后页面被 SPA 路由重定向到登录页。

**根因**：BOSS 直聘是 Vue SPA，`page.goto()` 在 HTML 文档 `load` 事件触发后返回，但此时 Vue 应用的异步认证逻辑还在执行（检查 token、调用后端验证接口）。如果 token 过期或会话失效，SPA 会在 2-5 秒后才触发路由跳转到 `/web/user/login`。

**解决方案**：实现 `_navigate_and_check_auth()` 辅助函数，导航后**等待 SPA 稳定**再判断：

```python
async def _navigate_and_check_auth(page, url, *, operation):
    page.goto(url, wait_until="domcontentloaded")
    # 等待 SPA 异步认证完成（最多 6 秒）
    for _ in range(6):
        page.wait_for_timeout(1000)
        current = page.url
        if "/user/login" in current or "/user/" in current:
            raise NeedLoginError(f"{operation}: SPA 认证失败，重定向到 {current}")
    # 认证通过，页面稳定
```

### 坑 3：浏览器会话反复失效

**现象**：每次 API 调用都能启动浏览器、加载 Cookie，但 BOSS 直聘仍然要求重新登录。

**根因**：每次 API 调用 `with sync_playwright() as pw:` 会 **启动→关闭** 浏览器进程。虽然用了 persistent context 保存了 Cookie 到磁盘，但反复启动/关闭浏览器的模式本身就是 bot 特征，BOSS 服务端检测到后主动使 session 失效。

**解决方案**：实现 **浏览器单例会话池**，一个 patchright 浏览器实例在整个后端生命周期内保持运行：

```python
_browser_lock = threading.Lock()
_browser_pw = None
_browser_context = None

def _get_browser_context():
    global _browser_pw, _browser_context
    with _browser_lock:
        if _browser_context is None:
            _browser_pw = sync_playwright().start()
            _browser_context = _browser_pw.chromium.launch_persistent_context(...)
        return _browser_context
```

配合 `POST /api/boss/login` 端点让用户通过持久浏览器交互式登录，登录后的 session 在后续所有 API 调用中自动复用。

### 坑 4：「查看职位」是 SPAN 不是 A 标签（最关键的坑）

**现象**：代码用多种 CSS 选择器搜索 `<a>` 标签（`a:has-text("查看职位")`、`a[href*='job_detail']`、`a[ka='job-detail']`），全部返回 0 匹配。debug 日志遍历页面全部 35 个 `<a>` 标签，没有一个文本包含"查看职位"。

**根因**：BOSS 直聘是 Vue SPA，「查看职位」渲染为 **`<SPAN>` 元素**，不是 `<a>` 标签。点击事件通过 Vue 的 `@click` 绑定处理，在 JavaScript 中调用 `window.open()` 打开新标签页。这是现代 SPA 的标准做法——可点击元素不一定是 `<a>` 标签。

```
实际 DOM 结构（简化）：
┌──────────────────────────────────────────────┐
│ 对话面板顶部固定栏                              │
│ <div class="chat-header">                    │
│   <span>NLP大模型训练实习 200-300元/天 厦门</span> │
│   <span @click="openJob()">查看职位</span>    │  ← 不是 <a>！
│ </div>                                       │
└──────────────────────────────────────────────┘
```

**解决方案**：使用 Playwright 的**文本选择器** `text=查看职位`，它匹配**任意元素类型**，不限于 `<a>`：

```python
_VIEW_JOB_SELECTORS = [
    "text=查看职位",          # 匹配任意包含此文本的元素（SPAN、DIV、A 均可）
    ':has-text("查看职位")',  # 备选：匹配包含此文本的任意元素
    "a[ka='job-detail']",    # 降级：传统 a 标签选择器
    "a[href*='job_detail']",
]
```

**验证日志**：
```
[ENRICH_DEBUG] 'text=查看职位' matches: 1
[ENRICH_DEBUG]   tag=SPAN, class=, text=查看职位
[JD_EXTRACT] Found via: text=查看职位, tag=SPAN, href=None
→ 成功打开新标签页，提取到完整 JD 文本
```

### 坑 5：对话列表选择器不准确

**现象**：`_extract_chat_items()` 提取到的"会话"实际上是导航栏的 `<li>` 元素（"首页"、"职位"、"公司"等），而非真正的聊天会话。

**根因**：初始选择器 `.chat-list li` 过于宽泛，同时命中了导航栏和会话列表。

**解决方案**：通过 DOM 分析确定正确选择器 `li[role="listitem"]`，这是 BOSS 聊天列表的 ARIA 角色标注。

### 经验总结：SPA 自动化的通用原则

| 原则 | 说明 |
|------|------|
| **文本选择器优先** | `text=查看职位` > `a:has-text("查看职位")`，因为 SPA 中可点击元素不一定是 `<a>` |
| **等待 SPA 稳定** | `page.goto()` 返回不等于页面就绪，需要额外等待异步路由和认证逻辑 |
| **保持浏览器常驻** | 反复启动/关闭浏览器是 bot 特征，会话池模式更接近真人使用习惯 |
| **ARIA 属性可靠** | `[role="listitem"]` 等 ARIA 属性比 class name 更稳定（框架不改 ARIA 语义） |
| **多选择器降级** | 为每个目标维护优先级选择器列表，主选择器失效时自动降级 |
| **debug 要精确** | 出问题时先 dump DOM 确认元素类型和属性，而非猜测选择器 |

---

## 六、面试要点总结

### 问题 1："为什么不用 LLM 视觉 Agent 驱动浏览器？"

核心论点：

1. **确定性流程不需要概率性系统**：BOSS 直聘的操作流程是有限状态机，每步转移在开发时就已确定，用 LLM 做导航决策是引入了不必要的不确定性
2. **成本效率**：实习生求职场景对成本敏感，每月 ¥225-750 的 LLM 调用费用于零成本可完成的 DOM 操作不合理
3. **安全性**：求职操作不可逆（发错消息、点错按钮），需要确定性执行而非概率性决策
4. **LLM 用在该用的地方**：JD 语义匹配、意图分类、回复生成才是 LLM 的价值所在
5. **分层设计**：决策层（LLM）和执行层（Playwright）清晰分离，各司其职

**反面论证**（体现思辨深度）：如果目标是一个通用的多平台求职 Agent（BOSS + 拉勾 + 猎聘 + 脉脉），每个平台的页面结构完全不同，那么用 LLM 视觉 Agent 来适配未知页面就是合理的。但 OfferPilot 当前阶段聚焦 BOSS 直聘单一平台，硬编码选择器是更务实的选择。

### 问题 2："SPA 自动化最大的坑是什么？"

用真实案例回答（展示工程实践深度）：

1. **元素类型假设错误**：「查看职位」渲染为 `<SPAN>` 而非 `<a>`，所有基于 `<a>` 标签的选择器全部失效。教训：SPA 框架中可点击元素不一定是传统 HTML 语义标签，必须用 `text=` 文本选择器或先 dump DOM 确认元素类型。

2. **异步认证竞态**：`page.goto()` 返回时 Vue 的异步认证逻辑还没跑完，导致"已登录"的判断是假阳性。3-5 秒后 SPA 路由才执行重定向。教训：SPA 自动化必须在 `goto()` 之后加等待窗口让框架稳定。

3. **会话池 vs 反复启动**：每次 API 调用都 open/close 浏览器会被反爬系统识别为 bot，导致 session 失效。教训：浏览器实例必须长驻复用，模拟人类"打开浏览器→使用一段时间→偶尔刷新"的行为模式。

### 问题 3："为什么用 patchright 而不是原生 Playwright？"

BOSS 直聘的反爬在 CDP（Chrome DevTools Protocol）协议层面检测自动化。原生 Playwright 通过 CDP 与浏览器通信，这个连接本身可被 JavaScript 检测到。`playwright_stealth` 只能修改 JS 层指纹（如 `navigator.webdriver`），无法掩盖 CDP 连接。patchright 在 Chromium 二进制层面打补丁移除 CDP 可检测特征，是目前对抗 CDP 检测的成熟方案。API 与 Playwright 100% 兼容，仅需修改 import 路径。

---

## 七、验证结果记录

### 路径 1：搜索扫描获取 JD ✅

```
POST /api/boss/scan
→ 成功提取 3 个职位卡片（job_title, company, salary, jd_text）
```

### 路径 2：对话中点击「查看职位」获取 JD ✅

```
POST /api/boss/chat/pull  {"max_conversations": 1, "fetch_jd": true}
→ source_url: https://www.zhipin.com/job_detail/6048c6b0af13510d03x50tm0GFVQ.html?...
→ jd_text: "岗位职责：\n1.智能体应用设计：协助设计智能体（Agent）的交互流程..."
→ 完整提取到岗位职责 + 任职要求
```

关键 debug 日志证据：
```
[ENRICH_DEBUG] 'text=查看职位' matches: 1
[ENRICH_DEBUG]   tag=SPAN, class=, text=查看职位
[JD_EXTRACT] Found via: text=查看职位, tag=SPAN, href=None
→ expect_page() 成功捕获新标签页
→ 提取 .job-sec-text 完整内容
```

---

## 八、参考资料

| 项目 | 简介 | 链接 |
|------|------|------|
| browser-use | LLM + 截图视觉驱动浏览器，80k+ stars | https://github.com/browser-use/browser-use |
| Playwright MCP | 微软官方，LLM + 无障碍树结构化数据 | https://github.com/microsoft/playwright-mcp |
| patchright | Playwright fork，二进制级 CDP 反检测 | https://github.com/AidoP/patchright |
| boss-cli | BOSS 直聘逆向 API CLI，443 stars | https://github.com/jackwener/boss-cli |
| OpenClaw browser-automation | Playwright + stealth 封装，自然语言触发 | https://openclaws.io/skills/browser-automation/ |
| BOSS 反爬复盘 | OfferPilot 自有文档，CDP 协议检测分析 | [debug-boss-antibot-postmortem.md](./debug-boss-antibot-postmortem.md) |
