# OpenClaw 部署复盘与踩坑手册（OfferPilot）

> 更新时间：2026-03-14  
> 目的：用于后续回顾、快速重装、以及面试复述。

---

## 1) 当前结论：部署阶段是否完成？

**结论：完成（对应实施方案“阶段 0”）。**

当前已确认状态：

- OpenClaw 可运行，模型配置生效
- 默认模型：`qwen-portal/qwen3-max`
- 回退模型：`qwen-portal/qwen-plus`
- Skills：`job-monitor`、`resume-tailor`、`application-tracker`、`email-reader`、`company-intel`、`interview-prep`（阶段 6 起步）
- 后端 API：`http://127.0.0.1:8010/health` 正常
- PostgreSQL：`localhost:15433` 正常
- OpenClaw -> Skill -> FastAPI 链路已跑通
- 前端看板工程已创建（Next.js + Tailwind），可对接真实后端接口
- 阶段 2 MVP 已落地：材料生成 + 人工审批（approve/reject/regenerate）

---

## 2) 当前环境快照（便于面试/复现）

- 系统：Windows + WSL2 Ubuntu
- Node：22.x（通过 `nvm use 22`）
- OpenClaw：`2026.3.13`
- 后端：FastAPI + LangGraph（已替换掉 mock）
- 数据库：PostgreSQL（Docker Compose，宿主机端口 `15433`）
- 模型接入：DashScope OpenAI 兼容模式

---

## 3) 可复现部署流程（从零到可用）

## Step A. 进入 WSL 并加载 Node 环境

```bash
wsl -d Ubuntu
source /root/.nvm/nvm.sh
nvm use 22
```

## Step B. 配置 OpenClaw 模型（阿里 Key）

```bash
read -rsp "DASHSCOPE_API_KEY: " DASHSCOPE_API_KEY; echo
[ -n "$DASHSCOPE_API_KEY" ] || { echo "DASHSCOPE_API_KEY 为空"; exit 1; }

openclaw onboard --non-interactive --accept-risk \
  --auth-choice custom-api-key \
  --custom-provider-id qwen \
  --custom-base-url https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --custom-model-id qwen3-max \
  --custom-compatibility openai \
  --custom-api-key "$DASHSCOPE_API_KEY" \
  --skip-channels --skip-skills --skip-ui --skip-daemon --skip-health --mode local

# 质量优先配置
openclaw models set qwen-portal/qwen3-max
openclaw models fallbacks clear
openclaw models fallbacks add qwen-portal/qwen-plus

openclaw models list
openclaw models status --json
```

## Step C. 启动数据库

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/infra
docker compose up -d
```

## Step D. 启动后端

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/backend
source /root/.venvs/offerpilot/bin/activate
export DASHSCOPE_API_KEY="你的真实key"
export CORS_ORIGINS="http://127.0.0.1:3000,http://localhost:3000"
uvicorn app.main:app --reload --host 127.0.0.1 --port 8010
```

## Step E. 验证链路

```bash
curl -sS http://127.0.0.1:8010/health

# 先把项目内 skills 同步到 OpenClaw workspace
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot
chmod +x scripts/sync_skills_to_openclaw_workspace.sh
./scripts/sync_skills_to_openclaw_workspace.sh

openclaw skills list
openclaw agent --local --session-id offerpilot-test \
  --message "Use job-monitor to analyze this JD: We need Python, LangGraph, RAG, MCP, Playwright." \
  --json

# 发布前检查（可选）
./scripts/skills_release_prep.sh

# ClawHub 发布（会自动处理 slug 冲突）
./scripts/clawhub_sync.sh dry-run
./scripts/clawhub_sync.sh publish
```

## Step F. 启动前端看板（可选）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/frontend
export NEXT_PUBLIC_API_BASE_URL="http://127.0.0.1:8010"

# 开发模式（可能受 /mnt/e I/O 影响较慢）
npm run dev -- --hostname 127.0.0.1 --port 3000
```

可在看板直接演示阶段 2：
- 在“最近岗位”里点“一键生成材料”
- 在“材料生成与审批”区块执行 `approve / reject / regenerate`
- 对应后端接口：`/api/material/generate`、`/api/material/review`、`/api/material/pending`

## Step G. 启动 browser-use MCP Server（阶段 3）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/backend
source /root/.venvs/offerpilot/bin/activate
python -m mcp_servers.browser_use_server
```

该服务提供最小工具：`navigate / click / extract_text / screenshot / wait / current_url`。

## Step H. 启动 web-search MCP Server（阶段 6 起步，可选）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/backend
source /root/.venvs/offerpilot/bin/activate
python -m mcp_servers.web_search_server
```

该服务提供工具：`search(query, max_results)`、`scrape_page(url, max_chars)`。

## Step I. 对接 OpenClaw Heartbeat（阶段 5 收口）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot
chmod +x scripts/openclaw_heartbeat_setup.sh

# 默认安装：
# 1) 每天 09:00 的邮件巡检任务
# 2) 每小时一次的心跳状态测试任务
./scripts/openclaw_heartbeat_setup.sh install

# 查看状态
./scripts/openclaw_heartbeat_setup.sh status

# 手动触发一次（验证联动链路）
./scripts/openclaw_heartbeat_setup.sh run-now offerpilot-email-daily-heartbeat
```

## Step J. 一键演示脚本（阶段 6 收口）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/backend
source /root/.venvs/offerpilot/bin/activate
# 默认 API_TIMEOUT_SEC=90（对 qwen3-max 更稳）
python demo_walkthrough.py
# 慢网络可手动放宽超时
# API_TIMEOUT_SEC=120 python demo_walkthrough.py
```

脚本会按顺序验证：`resume -> jd -> material -> company-intel -> interview-prep -> security -> timeline/metrics`。

## Step K. Fullstack Docker Compose（阶段 6 可选）

```bash
cd /mnt/e/0找工作/0大模型全栈知识库/OfferPilot/infra

# 仅数据库
docker compose up -d

# 全栈（postgres + backend + frontend）
docker compose --profile fullstack up -d --build
```

---

## 4) 关键踩坑复盘（最重要）

| 坑点 | 现象 | 根因 | 修复 |
|---|---|---|---|
| `anthropic` provider 报错 | `Unknown provider "anthropic"` | 当前 OpenClaw 未加载 anthropic provider | 改用 `custom-api-key` 路径（DashScope/DeepSeek） |
| Key 看似配置成功但调用 401 | `models status` 正常，调用失败 | 写入了错误 key 或无效 key | 用真实 key 重新 onboard，并做真实 API 调用验证 |
| 空变量污染参数 | key 位置被写成 `--skip-channels` | shell 变量为空时命令被错误吸收 | 先做 `[ -n "$KEY" ]` 校验再执行 |
| 后端端口冲突 | 8000 无法启动 | 本机已有服务占用 | 改为 `8010` |
| PostgreSQL 端口冲突 | 5432/5433 绑定失败 | Windows excluded port range | 改为宿主机 `15433` |
| `pip install` 卡死很久 | WSL 下安装巨慢 | venv 在 `/mnt/e` 挂载盘，I/O 慢 | venv 放到 `/root/.venvs/offerpilot` |
| Skill 改了不生效 | 项目文件改了但行为没变 | OpenClaw 实际读的是 `~/.openclaw/workspace/skills/...` | 执行 `scripts/sync_skills_to_openclaw_workspace.sh` 一键同步 |
| `openclaw agent --message` 看起来“听不懂” | 模型反复要你补上下文 | 命令引号错误，消息只传了首词 | 使用正确单引号包裹完整 message |
| qwen3-max 推理偶发超时 | 部分请求 >45s | 推理模型延迟更高 | 对重推理场景提高 timeout，或 fallback 到 plus |
| Next.js 在 WSL 挂载盘启动慢/卡住 | `next dev/start` 长时间无日志、端口不监听 | `/mnt/e` 上 I/O 波动导致 Node 进程进入 `D` 状态 | 优先保证 `npm run build` 成功；开发时可重试，或迁移高频运行目录到 WSL Linux 文件系统 |
| Playwright 启动报 `libnspr4.so` 缺失 | `/api/boss/scan` 返回 503 | WSL 缺少 Chromium 运行时依赖 | 先执行 `playwright install-deps chromium`，再 `playwright install chromium` |
| ClawHub 发布 `Slug is already taken` / `slug locked` | `clawhub sync/publish` 中途失败 | 公共 slug 已被其他账号占用或锁定 | 使用 `scripts/clawhub_sync.sh`，自动回退到 `<handle>-offerpilot-<skill>` 命名 |
| ClawHub `max 5 new skills per hour` | 最后 1 个新技能发布被限流 | 平台对“新建技能”有小时级限额 | 等待限流窗口后重跑 `./scripts/clawhub_sync.sh publish`（已发布项会自动 `SKIP`） |

---

## 5) 面试高频问法（可直接背）

## Q1：为什么用 OpenClaw + LangGraph 两层，不冲突吗？

**答：**

- OpenClaw 负责消息入口、Skill 路由、Heartbeat 调度
- LangGraph 负责复杂业务状态机（解析、评分、审批、恢复）
- 两者通过 HTTP 桥接，职责清晰，便于多入口复用（Web + Channel）

## Q2：为什么模型选 `qwen3-max` 主、`qwen-plus` 备？

**答：**

- 项目目标是求职演示，质量优先，因此主模型选 `qwen3-max`
- `qwen-plus` 成本和延迟更友好，适合作为超时/限流 fallback
- 实际做了工具调用与结构化输出 A/B 验证，不是拍脑袋选型

## Q3：如何保证“自动化”不越界？

**答：**

- 关键操作（提交申请、发消息）保留人工确认
- 强调“辅助系统”而非“全自动投递机器人”
- 保留动作日志，支持审计回放

## Q4：你们怎么定位线上问题？

**答：**

- 先分层排查：模型鉴权 -> API 健康 -> Skill 生效路径 -> DB 落库
- 用固定命令快速定位（见下节“排障命令”）

---

## 6) 快速排障命令清单

```bash
# 模型配置
openclaw models list
openclaw models status --json

# Skill 状态
openclaw skills list | sed -n '/job-monitor/p'
openclaw skills list | sed -n '/email-reader/p'

# 后端健康
curl -sS http://127.0.0.1:8010/health

# OpenClaw 调度状态
openclaw cron list --all --json
openclaw system heartbeat last --json

# 数据库连通性（在 backend venv 里）
python - <<'PY'
import psycopg
conn=psycopg.connect('postgresql://postgres:offerpilot@localhost:15433/offerpilot')
cur=conn.cursor()
cur.execute('select count(*) from jobs')
print('jobs=',cur.fetchone()[0])
cur.close(); conn.close()
PY
```

---

## 7) 你现在要补的“本人参与度”（非常关键）

为了防止面试时“会用不会讲”，建议你亲手完成下面 3 个动作（30-60 分钟）：

1. **亲手重跑一次模型配置**  
   从 `read -rsp DASHSCOPE_API_KEY` 到 `openclaw models status --json`

2. **亲手改一次 Skill 并验证**  
   在 `job-monitor` 输出里加一行固定文本，运行一次 `openclaw agent --local` 看变化

3. **亲手排一次故障演练**  
   临时停掉后端，再执行 agent 命令，观察报错；然后恢复并验证正常

做到这三步，你面试时就不是“听说过”，而是“做过、修过、讲得清楚”。

---

## 8) 当前阶段后续开发入口

部署阶段完成后，当前优先是阶段 6 收口与 Demo 打磨：

- 持续完善 LangGraph 节点（解析/评分/Gap）✅
- 接入 ChromaDB 的历史 JD 相似检索（`jd_history`）✅
- 前端看板展示真实分析结果与历史记录 ✅（已接入分析、列表、简历入库、评分依据片段）
- 阶段 2 MVP：材料生成与审批 ✅（已接入后端接口 + 前端操作）
- 阶段 2 审批持久化：LangGraph checkpoint + PostgreSQL `material_threads` ✅
- 阶段 2 收尾：导出 PDF/TXT + 复制话术 + `resume-tailor` 首版 ✅
- 阶段 3 起步：`/api/boss/scan`（Playwright 关键词扫描 + 截图 + 入库）✅
- 阶段 3 MVP：`browser_use_server.py` + BossScanner 子图 + Stealth/限速 ✅
- 阶段 3 增强：BOSS 多页扫描（`max_pages`）+ 详情页 JD 提取 ✅
- 阶段 4 起步：`/api/form/autofill/preview` + `/api/form/autofill/preview-url` + `/api/form/autofill/fill-url` ✅
- 阶段 4 进阶：`/api/form/fill/start` + `/api/form/fill/review`（LangGraph HITL + 持久化）✅
- 阶段 5 起步：`/api/email/ingest` + `/api/email/recent`（邮件分类 + 岗位状态同步）✅
- 阶段 5 起步：`/api/email/fetch`（IMAP 未读拉取入口）✅
- 阶段 5 进阶：`/api/email/heartbeat/*`（定时巡检 + 手动触发 + 状态观测）✅
- 阶段 5 进阶：webhook 通道通知（飞书机器人可接入）✅
- 阶段 5 收口：OpenClaw Heartbeat 与后端巡检统一（脚本化配置 + run-now 验证）✅
- 阶段 6 起步：`/api/company/intel` + `/api/interview/prep` + `/api/security/*`（令牌与预算）✅
- 阶段 6 起步：看板新增“公司情报 + 面试题库 + 安全治理”联动区块 ✅
- 阶段 6 收口：`backend/demo_walkthrough.py` 一键演示链路 ✅
- 阶段 6 收口：`docker compose --profile fullstack` 全栈启动能力 ✅
- 下一步：阶段 6 最后收尾（Demo 视频录制、真实简历数据回放与讲述打磨）

> 记住：先保证“稳定可演示”，再追求“大而全”。
