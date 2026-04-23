# JD 匹配偏差分析与方案设计

> 背景：OfferPilot Agent 在生产环境主动打招呼时，将"大模型算法实习生"（预训练/底座方向）误判为匹配岗位（目标：大模型应用/Agent 方向垂直实习），触发错误打招呼。本文档从根因分析出发，调研业界成熟方案，给出架构改进建议。

---

## 1. 问题复现与根因诊断

### 1.1 现象

| 岗位 | LLM 评分 | should_apply | 实际方向 | 预期结果 |
|------|---------|-------------|---------|---------|
| 大模型算法实习生 | 78 | true | 预训练/底座 | **应拦截** |
| AI Agent 开发实习生 | 82 | true | Agent 应用 | 应放行 ✅ |
| AI Agent 研发实习生 | 82 | true | Agent 应用 | 应放行 ✅ |

### 1.2 根因链

```
搜索结果 → 薪资过滤(OK) → LLM评分(问题在这) → 打招呼
```

**直接原因：LLM 给"大模型算法"岗位打了 78 分（高于 60 分阈值）且 `should_apply=true`。**

**深层原因（三层）：**

1. **JD 信息不足（信噪比问题）**
   - `greet_matching_jobs` 中传入 LLM 的 `jd_text` 只有 `title + company + salary + snippet`（搜索卡片摘要，约 400 字符）
   - **缺少完整 JD 正文**。搜索卡片里"大模型算法实习生"和"大模型 Agent 实习生"的摘要可能高度相似（都提到 Python、LLM、Transformer）
   - LLM 在信息不足时倾向给出保守偏高的分数（"看起来相关就给 70+"）

2. **评分模式本身不可靠（LLM-as-Judge 的已知缺陷）**
   - 研究表明：通用 LLM 做 Job Matching 的 ROC AUC 仅 0.77（Eightfold AI, 2025）
   - LLM 的数值评分天然存在**校准偏差**：RLHF 训练使模型倾向"讨好用户"，在不确定时偏向高分
   - 0-100 的连续评分区间对于"投不投"这种**二元决策**来说信息过剩且噪声大

3. **规则与 LLM 的职责混淆**
   - 当前架构让 LLM 同时做**信息提取**（从 JD 中提取技能/公司）+ **方向判断** + **数值评分** + **决策建议**
   - 一个 prompt 承载四个任务，违反了"单一职责"原则
   - 业界共识："LLM 是概率文本生成器，不是确定性业务逻辑引擎"（Iterathon, 2026）

---

## 2. 业界成熟方案调研

### 2.1 方案光谱

| 方案 | 核心思路 | 代表 | 优点 | 缺点 |
|------|---------|------|------|------|
| **A. 纯规则** | 关键词白名单/黑名单 | 传统 ATS | 100% 可控、零延迟、零成本 | 无法处理语义模糊（"AI 工程师"可能是算法也可能是应用） |
| **B. 语义 Embedding** | 向量相似度排序 | LinkedIn、Indeed | 捕捉语义关系 | 无法理解用户意图的细粒度区分（"Agent 应用" vs "算法"在 embedding 空间可能很近） |
| **C. LLM 评分** | 让 LLM 给 0-100 分 | 当前系统 | 理解复杂上下文 | 校准差、不可复现、成本高 |
| **D. LLM 二元判断** | 让 LLM 只回答 yes/no + 理由 | Tom Ron (2026) | 决策边界清晰、prompt 简单 | 仍依赖 LLM，但可靠性远高于连续评分 |
| **E. 规则前置 + LLM 后置** | 规则做硬性过滤，LLM 做最终确认 | Eightfold AI, OpenClaw Guardrails | 结合两者优势 | 需要精心设计分工边界 |
| **F. Multi-Agent** | 拆分提取/评估/决策为独立 Agent | 学术系统 (arXiv 2504.02870) | 模块化、可追溯 | 工程复杂度高、延迟翻倍 |

### 2.2 关键论文/实践总结

**Tom Ron (2026)** — *What I Learned Building a Job-Matching System*
- LLM 用于**结构化提取**（用 Pydantic schema 约束输出），不用于评分
- 匹配用 **RAG + 向量相似度 + 规则标准化**
- 核心观点：JD 的语言零标准化（同一个岗位可以写出完全不同的 JD），LLM 的价值在于**理解和归一化**，而非评判

**Keshav Jha (2026)** — *从 78% 到 97% 准确率*
- 核心方法：**让 LLM 的角色限于模式匹配和工具选择，把计算和逻辑放到确定性函数里**
- 68% 的 LLM 错误来自"参数值不正确"（对应我们的场景：评分数值不准确）
- 解决方案：structured output + 双 LLM 验证 + 确定性逻辑兜底

**Eightfold AI (2025)** — *LLMs in Hiring Decisions*
- 通用 LLM 做 Job Matching：ROC AUC = 0.77
- 专用模型：ROC AUC = 0.85
- **通用 LLM 的 false positive 率显著高于专用模型**
- 关键结论：不要让通用 LLM 独自做高风险决策

**OpenClaw Guardrails (2026)** — *Pre-Response Enforcement Hooks*
- 两阶段协议：Phase 1 规则门控 → Phase 2 LLM 生成
- "硬门"（hard gates）在 LLM 输出之前执行，不可被 LLM 绕过
- 对应我们的场景：**方向门控应该在 LLM 评分之前，作为不可绕过的硬规则**

---

## 3. 核心洞察：规则与 LLM 的角色分工

### 3.1 原则

> **规则负责"绝对不行"，LLM 负责"可能可以"。**

| 维度 | 规则引擎（确定性） | LLM（概率性） |
|------|-------------------|--------------|
| **角色** | 守门员（Gatekeeper） | 顾问（Advisor） |
| **擅长** | 硬性条件、黑白分明的过滤 | 语义理解、模糊场景的综合判断 |
| **执行位置** | 前置（在 LLM 之前） | 后置（在规则放行之后） |
| **失败模式** | 过度保守（漏掉好机会） | 过度宽松（放行坏匹配） |
| **成本** | ≈0 | Token 消耗 + 延迟 |
| **可解释性** | 100%（命中了哪条规则） | 低（"模型觉得 78 分"） |

### 3.2 架构模式：漏斗（Funnel）

```
搜索结果（~15 条）
    │
    ├── [Layer 1] 硬规则过滤（成本=0，延迟=0）
    │     ├── 薪资格式：排除非实习
    │     ├── 方向门控：三层信号（Strong Accept / Accept / Reject）
    │     │     └── 关键词从 skills/jd-filter/SKILL.md 热加载
    │     └── 黑名单：排除已打过招呼的
    │
    └── [Layer 2] 详情页完整JD + LLM 二元判断（每条~2s + ~500 token）
          ├── 导航到岗位详情页，提取完整JD正文（工作职责+任职资格）
          ├── 输入：候选人画像 + 完整JD（数百~数千字）
          ├── LLM prompt 规则从 SKILL.md 动态组装
          ├── 输出：should_greet (bool) + reason (str)
          ├── 通过 → 同一详情页直接点击「立即沟通」
          └── 拒绝 → 跳过，继续下一个
```

### 3.3 为什么二元判断优于数值评分

| 维度 | 数值评分 (0-100) | 二元判断 (yes/no) |
|------|-----------------|-----------------|
| **阈值选择** | 60? 70? 75? 永远不确定 | 不需要阈值 |
| **校准困难** | 不同 JD 的 78 分含义不同 | 每次独立判断，不需要跨 JD 可比性 |
| **prompt 复杂度** | 需要详细评分标准 + 权重 | 只需要清晰的判断标准 |
| **LLM 擅长度** | 不擅长（数值预测是 LLM 的弱项） | 擅长（分类/推理是 LLM 的强项） |
| **可调试性** | "为什么是 78 不是 65？" | "为什么是 yes？因为 XXX" |

---

## 4. 当前系统偏差的具体诊断

### 4.1 prompt 过度复杂化

当前 `_JD_MATCH_PROMPT` 要求 LLM 同时输出 **8 个字段**：
```
title, company, skills[], match_score, should_apply,
strengths[], gaps[], gap_analysis, one_line_reason
```

问题：
- **信息提取** (title/company/skills) 和**匹配判断** (score/should_apply) 混在一个 prompt 里
- 过多字段分散 LLM 的注意力，导致核心判断（should_apply）的质量下降
- `match_score` 和 `should_apply` 之间存在逻辑耦合（prompt 说 ">=60 且方向一致 → true"），但 LLM 的数值感知不可靠

### 4.2 JD 信息不足

```python
jd_text = "\n".join([item.title, item.company, item.salary or "", item.snippet or ""])
```

`snippet` 只是搜索卡片的 400 字摘要，对于区分"大模型算法"和"大模型应用"来说**严重不足**——两者的 snippet 可能都包含 "Python"、"大模型"、"Transformer"。

### 4.3 评分阈值的永恒困境

- 设 60 → 算法岗的 78 分通过
- 改 70 → 算法岗的 78 分仍通过
- 改 80 → 正确的 Agent 岗 78 分被误杀
- **没有一个阈值能同时兼顾精确率和召回率**

这不是调参能解决的，是评分模式本身的结构性缺陷。

---

## 5. 推荐方案

### 5.1 近期优化（当前已实施）

**规则硬门控**（Layer 1）—— 已上线：
- 标题含"算法"且不含"应用/开发/工程化" → 拦截
- snippet 含预训练/底座关键词且无 Agent/应用信号 → 拦截
- 无任何 Agent/应用方向信号 → 拦截
- 配置化（`BOSS_GREET_DIRECTION_MODE=strict/auto/off`）

**保留 LLM 双重门控**（Layer 2）—— 已上线：
- `match_score >= 70` **且** `should_apply == true` 才放行
- 两道门任意一道拦截即不打招呼

### 5.2 中期优化（已实施）

#### 5.2.1 LLM 判断改为纯二元模式（已上线）

将当前的"一个 prompt 做所有事"拆分：

**打招呼流程使用独立的二元判断 prompt**（`run_greet_decision`）：
```
输入: 候选人画像（从DB加载）+ 完整JD正文（从详情页提取）
输出: {should_greet: bool, reason: str, confidence: "high"|"medium"|"low"}
```

prompt 规则从 `skills/jd-filter/SKILL.md` 动态组装（Reject Rules / Accept Rules / Principle）。

#### 5.2.2 详情页完整 JD 提取（已上线）

不再依赖搜索卡片的 snippet 摘要，而是：
- 对通过规则层的每个岗位，直接导航到详情页
- 提取完整工作职责+任职资格文本（`_extract_detail_text`，支持多段拼接 + JS fallback）
- 基于完整 JD 做 LLM 二元判断
- 通过后在**同一详情页**直接点击「立即沟通」，无需二次导航

### 5.3 长期方向（技术储备）

| 方案 | 实现思路 | 适用场景 |
|------|---------|---------|
| **Embedding 预筛** | 用 SBERT 对 profile 和 JD 做向量相似度，低于阈值直接排除 | 日处理量 >100 时降低 LLM 调用成本 |
| **微调专用模型** | 收集 50+ 标注数据（JD + 是否匹配），微调 Qwen2.5-7B 做二分类 | 追求极致精确率 |
| **反馈学习** | 记录每次打招呼的 HR 回复率，作为隐式标注反哺模型 | 长期运营数据积累后 |

---

## 6. 面试深挖准备

### 6.1 可能的面试追问

**Q: 为什么不完全用规则，要引入 LLM？**
> 规则只能处理"白与黑"——标题写了"算法"就拦截，标题写了"Agent"就放行。但现实中大量岗位处于灰色地带：标题写"AI 工程师"，JD 里一半是 Agent 开发一半是模型训练。规则无法理解语义，而 LLM 可以。所以架构设计是：规则做硬性过滤（守门员），LLM 做软性判断（顾问）。

**Q: 为什么不完全用 LLM，要加规则？**
> 三个原因：
> 1. **可靠性**：LLM 是概率系统，同一个 JD 跑两次可能得到不同结果。规则是确定性的。
> 2. **成本与延迟**：规则过滤是 O(1)，LLM 调用需要 2-5 秒且消耗 token。先用规则砍掉明显不匹配的，再让 LLM 判断剩余的。
> 3. **安全边际**：生产环境中，打错招呼的代价远大于漏掉一个机会。规则提供不可被 LLM "说服绕过"的硬底线（hard gate）。

**Q: 数值评分 vs 二元判断，你怎么选的？**
> 一开始用的 0-100 评分 + 阈值，但实践中发现三个问题：
> 1. 阈值永远调不好——设 60 则算法岗 78 分通过，设 80 则好岗位 78 分被误杀
> 2. LLM 的数值校准天然不可靠——论文显示 RLHF 训练后的模型"过度自信"
> 3. 我们的场景本质是二分类（打不打招呼），不需要连续分数
>
> 改为让 LLM 只输出 yes/no + 一句话理由后，prompt 更聚焦、结果更可控、可调试性大幅提升。

**Q: 这个方向门控会不会误杀好的岗位？**
> 会。设计原则是"宁缺毋滥"——误杀的代价是少一个机会（可以下轮补回），误放的代价是给不相关的 HR 发消息（浪费双方时间且损害账号信誉）。在求职 Agent 场景下，precision 优先于 recall。同时门控可配置化（strict/auto/off），用户可以根据自己的风险偏好调整。

### 6.2 技术亮点总结

1. **漏斗架构**：规则前置（零成本硬过滤）→ LLM 后置（语义软判断），体现了"把确定性留给代码、把不确定性留给模型"的工程原则
2. **从连续评分到二元判断的演进**：不是拍脑袋改的，而是从生产 Bug 出发 → 诊断根因 → 调研业界方案（Eightfold AI ROC AUC 对比、Tom Ron 实战经验、LLM-as-Judge 校准论文）→ 做出架构决策
3. **配置化门控（OpenClaw Skills 驱动）**：匹配策略不硬编码在 Python 代码里，而是声明在 `skills/jd-filter/SKILL.md` 中——遵循 OpenClaw Skills 规范（YAML frontmatter + Markdown body）。后端通过 `skill_loader.py` 在运行时热加载该文件，自动编译为正则表达式和 LLM prompt 片段。用户只需编辑 Markdown 即可调整：
   - **Direction Keywords**（Accept/Reject 方向关键词）→ 编译为方向门控正则
   - **Title Block / Title Require App**（标题拦截/解除词）→ 驱动标题级硬过滤
   - **LLM Decision Rules**（Reject/Accept/Principle）→ 注入 LLM 二元判断 prompt
   - **Parameters**（batch_size / daily_limit / direction_mode）→ 运行参数

   这个设计的优势：
   - **热加载**：修改 SKILL.md 后下一次调用自动生效，无需重启后端
   - **声明式**：非技术用户也能看懂 Markdown，不需要理解正则语法
   - **OpenClaw 生态对齐**：与 OpenClaw 的 Skill 加载机制（workspace `/skills` > managed `~/.openclaw/skills` > bundled）完全一致，面试时可以深入讲解这个设计决策
   - **优先级链**：`.env` 环境变量 > SKILL.md 参数 > 代码 fallback 默认值
4. **可观测性**：每个过滤层都有日志（`reason=title_contains_algorithm`），可追溯每一条 JD 被拦截/放行的完整链路

---

## 7. 参考文献

1. Tom Ron (2026). *What I Learned Building a Job-Matching System in Hebrew*. Towards AI.
2. Tom Ron (2026). *I Built a Job-Matching Algorithm. Now I Understand Why LinkedIn Struggles*. Towards AI.
3. Keshav Jha (2026). *How I Took My AI Agent from 78% to 97% Accuracy*. Medium.
4. Eightfold AI (2025). *Evaluating the Promise and Pitfalls of LLMs in Hiring Decisions*. arXiv.
5. arXiv 2504.02870 (2025). *AI Hiring with LLMs: A Context-Aware and Explainable Multi-Agent Framework for Resume Screening*.
6. arXiv 2601.13284 (2026). *Balancing Classification and Calibration Performance in Decision-Making LLMs via Calibration Aware Reinforcement Learning*.
7. Iterathon (2026). *Advanced Function Calling Tool Composition Production Agents*.
8. OpenClaw (2026). *Pre-response Enforcement Hooks (Hard Gates)*. GitHub Issue #13583.
9. Shailesh Chaudhary (2026). *Building a Smart Resume-Screening System with RAG*. Medium.
10. Tushar (2026). *From Keyword Matching to Semantic Ranking: Building a Deep Learning Resume-Job Description Matcher*. Medium.
