# BOSS 搜索结果页 DOM 合同

> SOP: `../../README.md` · 平台: `../README.md`
> 数据基准：[`20260422T065914Z.json`](./20260422T065914Z.json)（3 张真实 card，landed URL `/web/geek/jobs?query=Agent开发实习生`）

## 卡片外层

| 用途 | selector | 说明 |
|---|---|---|
| 卡片容器 | `ul.job-list-box > li.job-card-box` | 2026-04-22 观察。`_default_job_card_selectors` 保留若干历史候选，命中第一个就停。 |

## 字段合同

| 字段 | selector | DOM 证据 | 注意 |
|---|---|---|---|
| 岗位名 | `.job-name` | `<a class="job-name">Agent开发实习生</a>`（card #1/2 均）、`可转正&包三餐-后端和Agent开发-base可选`（card #3） | **禁用** `[class*='title']`：`.job-title clearfix` 会被它命中，但内层嵌套的 `.job-name` 才是纯文本节点。 |
| 薪资 | `.job-salary` | `<span class="job-salary">-元/天</span>`（3/3 均为脱敏占位） | 未登录 / 部分账号下搜索列表薪资被 BOSS **脱敏为 `"-元/天"`**。真实薪资要靠 greet 路径从详情页再抓一次。不要把这个字段当真。 |
| 公司 | `.boss-name` | `<span class="boss-name">上海觅深科技有限公司</span>` / `上海简文` / `字节跳动` | ★ 历史炸弹：老代码 `[class*='company']` 会命中**地址节点** `.company-location`（字面含 "company"），导致 26/26 greet record 公司字段全是地址。见 `trace_fe19c3ab1e43`。 |
| 地址 | `.company-location` | `<span class="company-location">上海·徐汇区·漕河泾</span>` 等 | **独立字段**，不要和公司混。形态：2~5 段 `·` 分隔 —— 作为 `_looks_like_address` 启发式的锚点。 |
| 详情链接 | `a.job-name[href*='/job_detail']` | `href="/job_detail/d9b5615bbc6e0f330nd53dW6ElJU.html"` | `a.boss-info` 指向公司主页 `/gongsi/`，**不要**用它做岗位链接。 |
| 公司主页链接 | `a.boss-info[href*='/gongsi/']` | `href="/gongsi/af335ad67302988903B909y6EVs~.html?from=top-card"` | 可用作公司身份锚点（比关键字更稳），未来若要做公司级去重或"大厂"分类可从这里取 |
| 工作模式 tag | `ul.tag-list > li` 文本 | `3天/周` / `3个月` / `本科` | 纯文本 list，顺序不稳定；按关键字匹配（"/周"、"/天"、"本科/硕士"）解析 |

## 已知陷阱

| 陷阱 | 形态 | 防御 |
|---|---|---|
| `[class*='company']` 误命中 `.company-location` | 26/26 真实 greet 公司字段全是"上海·XX·XX" | selector 改走精确 `.boss-name`；代码里加 `_looks_like_address` 作为 defense-in-depth |
| `_guess_company(url)` 把域名当公司 | 返回 `www.zhipin.com` 当公司名 | 已移除，现在返回空字符串让 matcher 感知缺失 |
| 薪资脱敏 `-元/天` | 3/3 card 都是这个值 | search-list 层**不要**用 salary 做硬匹配；依赖 greet 详情页再抓 |

## 与代码/测试的对应

- 查询 JS 实现：`src/pulse/mcp_servers/_boss_platform_runtime.py::_extract_jobs_from_page` 内的 `page.eval_on_selector_all(...)`
- row 组装与形态守卫：同文件中 `_looks_like_address` / `_guess_company` / 循环组装处
- 形态测试：`tests/pulse/mcp_servers/test_boss_helpers.py`（真实样本覆盖本文档 `company` 与地址两列）
