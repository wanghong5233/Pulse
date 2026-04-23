# BOSS 直聘反爬对抗：about:blank 跳转问题复盘

> 耗时：~3 小时诊断 + ~4 小时迁移到最终方案  
> 日期：2026-03-14 ~ 2026-03-16  
> 影响：`boss_scan.py` 全部浏览器操作  
> 最终方案：**patchright（Playwright fork，二进制级 CDP 反检测）+ 浏览器单例会话池**

---

## 一、现象

使用 Playwright `launch_persistent_context` 打开 BOSS 直聘登录页后，页面在 **闪现登录界面约 2-3 秒后**，自动跳转到 `about:blank`，用户无法完成扫码登录。

---

## 二、真正的根因（结论先行）

### BOSS 直聘检测的是 Playwright 的 CDP（Chrome DevTools Protocol）连接本身

BOSS 直聘的反爬系统通过 `app.js` 中嵌入的检测代码，结合 `iframe-core.js` 和 `zhipinFrame` 隐藏 iframe，对浏览器进行**底层协议级别的自动化检测**。这个检测发生在比 JavaScript API 更深的层面——它检测的是浏览器是否被 CDP 协议控制。

**关键证据链：**

| 实验 | 结果 | 说明 |
|------|------|------|
| Playwright + Chromium for Testing | ❌ 跳转 | 初始状态 |
| Playwright + 所有 stealth 补丁 + 真实 UA | ❌ 跳转 | JS 层伪装无效 |
| Playwright + 真正的 Google Chrome (`channel="chrome"`) | ❌ 跳转 | **不是浏览器品牌问题** |
| Playwright + 拦截所有 JS location 方法 (init_script) | ❌ 跳转，且拦截器**未触发** | 跳转不走 JS API |
| Playwright + 阻止 iframe-core.js | ✅ URL 稳定 / ❌ 页面卡"加载中" | 阻断了检测，但也破坏了页面 |
| **原生 Chrome 直接启动（无 Playwright）** | **✅ 完全正常，QR 码正常显示** | **CDP 连接是唯一触发条件** |

**结论：任何通过 Playwright（或 Puppeteer 等 CDP 工具）控制的浏览器，无论是 Chromium for Testing 还是真正的 Google Chrome，都会被检测并重定向到 about:blank。唯一的解法是登录时不使用 CDP 控制。**

### 检测机制详细分析

```
加载链路：
  app.js → 动态创建 <iframe name="zhipinFrame" src="about:blank">
         → 加载 iframe-core.js（107KB Vue Router SPA 包）
         → iframe-core.js 中包含 CDP 协议检测逻辑
         → 检测到 CDP → 通过非 JS-API 路径（无法被 JS 拦截）重定向主帧到 about:blank

关键证据：
  - 拦截 location.replace / assign / href setter / document.open / document.write
    → 全部未触发，说明跳转不经过任何可拦截的 JS API
  - 跳转由 iframe-core.js 触发（阻止该脚本可阻止跳转）
  - 但阻止 iframe-core.js 会导致页面无法初始化（它也是登录页 SPA 的路由框架）
  - 即使用真正的 Google Chrome（非 Chromium for Testing）+ Playwright 控制，仍然被检测
  - 只有完全脱离 CDP 控制的原生 Chrome 才能通过检测
```

---

## 三、方案演进（从临时到最终）

### 阶段 1 临时方案：原生 Chrome 登录 + Playwright 自动化（已废弃）

最初的思路是把登录和自动化分开：用原生 Chrome（无 CDP）手动登录，再用 Playwright 复用 Cookie 做自动化。

```bash
# 旧方案：boss-login.sh（已废弃）
google-chrome --user-data-dir="$PROFILE_DIR" "$LOGIN_URL"
```

**问题**：Playwright 即使复用了登录后的 Cookie，在已登录页面仍然被 CDP 检测。BOSS 直聘对**所有页面**都做了 CDP 检测，不仅限于登录页。需要两套浏览器（原生 Chrome + Playwright Chromium）共享 profile 目录，版本不兼容风险高，且每次 API 调用都启动/关闭 Playwright 会话导致 session 被服务端识别为 bot 并失效。

### 阶段 2 最终方案：patchright + 浏览器单例会话池（当前使用）

[patchright](https://github.com/AidoP/patchright) 是 Playwright 的 fork，在 **Chromium 二进制层面**打补丁，移除 CDP 的可检测特征。API 与 Playwright 100% 兼容，仅需修改 import 路径：

```python
# 一行 import 变更解决 CDP 检测
from patchright.sync_api import sync_playwright  # 替代 playwright.sync_api
```

配合**浏览器单例会话池**，一个 patchright 浏览器实例在后端整个生命周期内保持运行：

```python
_browser_lock = threading.Lock()
_browser_pw = None
_browser_context = None

def _get_browser_context():
    """获取或创建持久浏览器上下文（单例模式）"""
    global _browser_pw, _browser_context
    with _browser_lock:
        if _browser_context is None:
            _browser_pw = sync_playwright().start()
            _browser_context = _browser_pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="chrome",
                no_viewport=True,
                args=["--disable-blink-features=AutomationControlled",
                      "--no-sandbox", "--no-first-run"],
            )
        return _browser_context
```

用户通过 `POST /api/boss/login` 在持久浏览器中交互式登录，session 自动复用于后续所有 API 调用。

**验证**：patchright 成功打开 BOSS 直聘所有页面（登录、搜索、消息、职位详情），无 `about:blank` 重定向。不再需要 `playwright_stealth`、`KILL_ZHIPIN_FRAME_JS`、`iframe-core.js` 阻止等任何辅助措施。

**关键改进对比**：

| 维度 | 旧方案（原生 Chrome + Playwright） | 新方案（patchright + 会话池） |
|------|----------------------------------|----------------------------|
| 登录 | 手动启动原生 Chrome，CLI 脚本 | API 端点 `POST /api/boss/login`，浏览器弹出 |
| CDP 检测 | 登录绕过，但自动化页面仍有风险 | 完全绕过，所有页面均可访问 |
| 会话管理 | 每次 API 调用启动/关闭浏览器 | 单例长驻，模拟真人使用模式 |
| 依赖 | 原生 Chrome + Playwright + stealth | 仅 patchright（一个库） |
| Profile 兼容 | 两个浏览器共享 profile，版本冲突风险 | 单一浏览器，无兼容问题 |

---

## 四、错误的排查路径（教训）

按时间顺序记录每一轮错误假设，**这些才是最值得反思的**。

### 第一阶段：盲目猜测（轮次 1-3）

| 轮次 | 假设 | 行动 | 结果 | 为什么错 |
|------|------|------|------|---------|
| 1 | Session restore 恢复旧页面 | 清理 Sessions 文件、锁文件 | ❌ | 跳转在导航成功后 2-3 秒，不是 restore |
| 2 | 多 tab 焦点争抢 | 新建 tab → 关闭其他 → bring_to_front | ❌ 更糟 | 关闭 tab 触发副作用 |
| 3 | 脚本逻辑太复杂 | 回归最简 3 行代码 | ❌ 同样跳转 | 排除了脚本 bug，但没深究 |

### 第二阶段：JS 层反检测（轮次 4-8）

| 轮次 | 假设 | 行动 | 结果 | 为什么错 |
|------|------|------|------|---------|
| 4 | `navigator.webdriver` 被检测 | ignore_default_args + init_script 覆盖 | ❌ | 不是唯一检测手段 |
| 5 | stealth 库没装 | 安装 playwright-stealth 全套 | ❌ | 反爬不依赖这些指纹 |
| 6 | UA 暴露 Chromium for Testing | 设置真实 Chrome/131 UA | ❌ | UA 不是触发条件 |
| 7 | chrome.runtime 缺失 | Stealth 开启 chrome_runtime=True | ❌ | 也不是触发条件 |
| 8 | 持久化 profile 旧数据 | 全新空 tmpdir 测试 | ❌ | 排除 profile 因素 |

### 第三阶段：脚本级分析（轮次 9-12）

| 轮次 | 假设 | 行动 | 结果 | 为什么错 |
|------|------|------|------|---------|
| 9 | browser-check.min.js 是检测脚本 | page.route 阻止加载 | ❌ | 只是兼容性检查 |
| 10 | warlockdata/patas 是反爬 SDK | 阻止 3 个外部脚本 | ❌ | 检测不在这些脚本里 |
| 11 | 内联 `<script>` 有检测代码 | 抓取 HTML 分析 12 个内联脚本 | ❌ | 无检测代码 |
| 12 | 修改 HTML 去掉 iframe | route.fetch 替换响应 | ❌ | iframe 是 JS 动态创建的 |

### 第四阶段：iframe 级分析（轮次 13-16）— 接近真相但走了弯路

| 轮次 | 假设 | 行动 | 结果 | 为什么错 |
|------|------|------|------|---------|
| 13 | 阻止 iframe-core.js + MutationObserver | 双管齐下 | ✅ URL 稳定 | 但页面卡"加载中"！ |
| 14 | MutationObserver 单独够用 | 只用 MO 不阻止脚本 | ❌ | MO 是异步的，来不及 |
| 15 | sandbox iframe 阻止脚本执行 | MO 添加 sandbox 属性 | ❌ | 同样来不及 |
| 16 | 修改 app.js 不创建 iframe | patch appendChild 调用 | ❌ | iframe 不是跳转源头！ |

### 第五阶段：定位到 CDP 协议检测（轮次 17-20）

| 轮次 | 假设 | 行动 | 结果 | 关键发现 |
|------|------|------|------|---------|
| 17 | iframe-core.js 里有 about:blank | 扫描脚本内容 | 0 个匹配 | 跳转 URL 是动态构造的 |
| 18 | 跳转走 location.replace/assign | init_script 全面拦截 | ❌ 拦截器未触发 | **跳转不走任何 JS API** |
| 19 | Chromium for Testing 被识别 | 安装真 Chrome + channel="chrome" | ❌ | **浏览器品牌不是问题** |
| **20** | **Playwright CDP 连接被检测** | **原生 Chrome 直接启动** | **✅ 完美运行** | **CDP 协议是唯一触发条件** |

---

## 五、核心教训

### 教训 1：先做精确诊断，再做修复

> **错误做法**：猜原因 → 改代码 → 试运行 → 猜错 → 换假设。20 轮循环。  
> **正确做法**：先搭建可重复的诊断环境，用数据逐步缩小范围。

最终定位根因的关键诊断步骤：
1. `framenavigated` 事件监听 → 发现 `zhipinFrame` 关联
2. 网络请求日志 → 识别 `iframe-core.js`
3. JS API 全面拦截 → 证明跳转不走 JS 层
4. 真 Chrome 对比测试 → 证明是 CDP 协议问题
5. 原生 Chrome 无 CDP → 最终验证

### 教训 2：反爬对抗有不可逾越的层级

```
L1: navigator.webdriver              → stealth 可绕过
L2: 浏览器指纹 (UA/plugins/WebGL)     → stealth 可绕过
L3: Chrome 品牌/版本检测              → channel="chrome" 可绕过
L4: CDP 协议特征检测                   → ⚠️ 无法在 JS 层绕过
L5: 服务端 TLS 指纹/IP 风控           → 需要代理/真实环境
```

当尝试绕过 L1-L3 全部失败时，应该**立即怀疑 L4（协议层检测）**，而不是继续在 L1-L3 层面找原因。我们在 L1-L3 浪费了至少 10 轮。

### 教训 3：「阻止脚本使 URL 稳定」≠「问题解决」

轮次 13 中，阻止 iframe-core.js 让 URL 稳定了 30 秒，我们以为"找到了"。但页面卡在"加载中"——**URL 稳定但功能不可用等于没解决**。当时应该立即意识到：阻止核心 SPA 脚本不可能是正确的最终方案。

### 教训 4：最简单的方案往往是最好的

20 轮复杂的 JS 注入、脚本拦截、DOM 操作、响应修改……最终的解决方案就一行：

```bash
google-chrome --user-data-dir="$PROFILE_DIR" "$LOGIN_URL"
```

**不使用 Playwright 就不会被检测到 Playwright。** 这个思路应该更早被考虑。

### 教训 5：「登录」和「自动化」的反爬强度不同——但不能依赖这个假设

最初以为 BOSS 只在登录页检测 CDP，已登录页面不检测。但实测发现 Playwright 在已登录页面也会被检测到并导致 session 失效。**反爬策略可能随时升级，不要假设某个页面"不会检测"。**

正确做法：从协议层面根治（patchright），而不是针对个别页面打补丁。

### 教训 6：浏览器实例的生命周期管理同样重要

即使绕过了 CDP 检测，如果每次 API 调用都启动/关闭浏览器，这种行为模式本身就是 bot 特征。BOSS 服务端可以通过 session 活跃模式判断——正常人不会每隔几秒就"启动浏览器→操作→关闭浏览器"。

浏览器单例会话池不仅提升了性能（省去重复启动开销），更重要的是它模拟了真人的浏览器使用模式。

### 教训 7：寻找社区已有解决方案

在 Playwright + stealth 上浪费了大量时间后，最终解决问题的 patchright 是一个已有的社区项目。**当问题在 JS 层面无解时，应该更早搜索"playwright CDP detection bypass"等关键词，而不是继续在同一层面重复尝试。**

---

## 六、影响的文件（最终状态）

| 文件 | 修改内容 |
|------|---------|
| `backend/app/boss_scan.py` | import 从 `playwright` → `patchright`；移除 stealth / MutationObserver / iframe-core 阻止；新增 `_get_browser_context()` 会话池、`_navigate_and_check_auth()` SPA 竞态处理、`boss_login_via_pool()` 交互式登录 |
| `backend/app/main.py` | 新增 `POST /api/boss/login` 端点；新增 `@app.on_event("shutdown")` 调用 `shutdown_browser()` |
| `scripts/boss-login.sh` | **已废弃**（被 API 端点取代） |
| `scripts/_boss_login_impl.py` | **已废弃** |

前置依赖：
```bash
pip install patchright
patchright install chromium  # 安装 patchright 的补丁版 Chromium
```

---

## 七、后续风险与关注点

1. **Cookie 过期**：登录 Cookie 有时效性，过期后需要通过 `POST /api/boss/login` 重新扫码登录。已实现飞书告警自动通知。
2. **patchright 版本滞后**：patchright 是 Playwright 的 fork，可能在 Playwright 大版本更新后有延迟。需要关注 patchright 仓库的更新频率。
3. **BOSS 反爬再升级**：如果 BOSS 从 CDP 检测升级到 TLS 指纹或服务端行为分析，patchright 可能不够。但目前 CDP 检测是主要手段，patchright 有效。
4. **浏览器会话池内存占用**：长驻浏览器实例会持续占用内存（约 200-400MB）。在资源受限环境需要关注，可通过 `shutdown_browser()` 主动释放。

---

## 八、调试工具箱（备忘）

以后遇到类似反爬问题的标准排查流程：

```python
# 第一步：事件级诊断（最重要）
page.on("framenavigated", lambda f: print(f"[NAV] {f.name or 'MAIN'} => {f.url}"))
page.on("request", lambda r: print(f"[REQ] {r.resource_type} {r.url}")
    if r.resource_type in ("document", "script") else None)

# 第二步：定时验证 JS 层真实 URL
for t in range(15):
    time.sleep(2)
    print(f"t+{(t+1)*2}s url={page.evaluate('window.location.href')}")

# 第三步：如果上面没找到原因，测试 CDP vs 非 CDP
# 用原生浏览器直接打开同一 URL，对比行为差异
# 如果原生浏览器正常 → 问题在 CDP 协议层，JS 层方案无解

# 第四步：扫描所有脚本内容
def scan(route):
    resp = route.fetch()
    body = resp.text()
    for pat in ["about:blank", "top.location", "parent.location"]:
        if pat in body:
            print(f"[FOUND] {pat} in {route.request.url}")
    route.fulfill(response=resp, body=body)
ctx.route("**/*.js*", scan)
```
