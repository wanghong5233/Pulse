# DOM 解析 SOP

> 作用范围：所有"必须和外部 Web UI 打交道"的 Pulse module（当前：BOSS 直聘；未来可能：其他招聘平台 / 游戏签到页 / 日常网站自动化）。
>
> 本文只约定**方法论**与**目录约定**，不约定具体代码形态。每个平台的 selector 合同、dump 工具、异常形态，放在 `docs/dom-specs/<platform>/` 之下。当第二个平台真的接入后，再讨论"抽象出通用 dump 协议"是否有价值——在那之前不要预设抽象。

## 为什么需要这套 SOP

真实事故：`trace_fe19c3ab1e43`（见 `logs/boss_mcp_actions.jsonl` 共 26 条记录），BOSS 搜索结果 card 的 `company` 字段在 26/26 条投递里都被写成了地址（`杭州·余杭区·仓前` / `上海·浦东新区·陆家嘴` …）。根因：老 CSS selector `[class*='company']` 宽松匹配 → 精确命中了 BOSS 里**字面含 "company" 字样**的**地址节点** `.company-location`，真正的公司名节点 `.boss-name` 从没被查询过。

**没有真实 DOM 做基准 → selector 只能脑补 → selector 脑补错了 → 下游所有 matcher / reply 都跟着错**。这是"类型 A 屎山：伪造结果"的典型反面教材（`docs/code-review-checklist.md` §类型 A）。

SOP 的目标就是让"selector 错"变成**肉眼可立刻发现**：dump 产物交代码库托管，selector 合同写成 markdown 表，代码、测试、spec 三者必须能一一对应。

## 四步工作流

```
  [1] 采数据                [2] 归纳合同             [3] 落到代码           [4] 回放验证
  dump_*_dom.py   →   <page>/README.md  →   _*_platform_runtime.py  →   tests + audit log
```

| 步骤 | 产物 | 约束 |
|---|---|---|
| **1 采** | `<platform>/<page_type>/<ts>.html` + `.json`（rendered DOM 快照，含 outerHTML + class tree） | 产物**进版本控制**，不是运行日志。至少保留最新一份，**改版前的旧版也不要随手删**——它是定位"从什么时候开始错"的证据。 |
| **2 归** | `<platform>/<page_type>/README.md` 里的 **selector 合同表**（字段 → selector → 字面含义 → 已知陷阱） | 每列必须引用 `<ts>.json` 里哪个 node 作为证据，禁止写"推测是这样"。 |
| **3 写** | `src/pulse/mcp_servers/_<platform>_platform_runtime.py` 里的查询逻辑 | 代码注释必须回链到 `<page>/README.md` 对应条目。**禁止**使用 `[class*='xxx']` 这类宽松 substring 匹配（见下文"反模式")。 |
| **4 验** | `tests/pulse/mcp_servers/test_<platform>_helpers.py` 里的形态守卫（纯函数、真实样本）+ 线上 `audit.jsonl` 的 trace 复核 | 测试样本必须直接从 `<ts>.json` / audit log 抽取——不是脑补的合成字符串。 |

## 何时触发一次完整流转

- **首次接入一个 page**：走 1→2→3→4。
- **上游报字段错误/缺失**（如用户反馈"公司名不对"）：
  1. 先看 `audit.jsonl` 是否复现 → 定位到受影响 page_type
  2. **重新跑**一次对应 dump，**对比新旧 `<ts>.json`** 是否结构变化
     - 变了 → 说明平台改版，走 2→3→4 修 spec + 代码 + test
     - 没变 → 说明是 selector 一开始就写错了（跟 `trace_fe19c3ab1e43` 一样），直接走 3→4
- **周期性保健**（可选）：每季度跑一次所有 page_type 的 dump，diff 旧 json，提前发现悄悄改版。

## 目录约定

```
Pulse/
├── scripts/
│   └── dump_<platform>_dom.py            # 每个平台一个采集器; 共享 SOP 但不强求共享代码
├── docs/
│   └── dom-specs/
│       ├── README.md                     # 本文件 (方法论)
│       └── <platform>/
│           ├── README.md                 # 平台级索引 + 反爬注意事项
│           └── <page_type>/
│               ├── README.md             # selector 合同表 + 陷阱清单
│               ├── <ts>.html             # 人读 dump
│               └── <ts>.json             # 机读 dump (可作 test fixture)
├── src/pulse/mcp_servers/
│   └── _<platform>_platform_runtime.py   # 查询逻辑; 注释回链 page_type README
└── tests/pulse/mcp_servers/
    └── test_<platform>_helpers.py        # 形态守卫 (纯函数, 真实样本)
```

## 反模式（禁用列表）

1. **宽松属性子串匹配**：`[class*='company']`、`[class*='title']`、`[class*='salary']` 等。这是 `trace_fe19c3ab1e43` 的元凶——平台的 class 命名里带"company"的不一定就是公司，Pulse 不能押在"语义 = 字面"的假设上。selector 必须精确到已经在 dump 里亲眼看到过的 class 名。
2. **`_guess_*` 的 URL/domain fallback**：历史上 `_guess_company` 拿不到公司就返回 URL host（`www.zhipin.com`），直接伪造结果。规则：**拿不到就返回空字符串**，让下游 matcher / ActionReport 把"字段缺失"作为 `concerns` 显式传给用户，不能假造。
3. **selector 合同只写在代码注释里**：代码改一次，spec 就永远落后。合同必须以 markdown 表为源头，代码是它的实现。
4. **test 里用合成字符串**：`"测试公司1" / "Foo Co Ltd"` 这种合成样本骗过所有单测也不能说明 selector 对。测试输入必须从真实 dump / 真实 audit log 抽样。

## 反爬对抗三原则

1. **永远复用 Pulse 生产的 Playwright profile**（`~/.pulse/boss_browser_profile` 等），不要另起浏览器——登录态和 stealth 指纹都在这里。
2. **不要在页面里打开 DevTools**。BOSS 会用 `debugger;` 循环或窗口尺寸差异检测并强制重定向。CDP 远控不触发这套机制。
3. **所有 dump 节点的 innerText 做一次 `replace(/\s+/g, " ").trim()`**，否则 Vue SSR 的空白字符会让字符串比较奇奇怪怪失败。

## 平台索引

- [BOSS 直聘](./boss/README.md)
