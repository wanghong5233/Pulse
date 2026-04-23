"""BOSS 直聘 rendered-DOM 采集器 (selector 设计 & 改版回归的事实源).

## 定位

这个脚本是 ``docs/dom-specs/boss/`` 的**唯一事实来源上游**. 不要把它
当成运行日志, 也不要把它当成一次性脚本 — 每次 BOSS 改版 / 怀疑
selector 错位, 都应重新跑一次对应 ``page_type``, 把新产物归档进
``docs/dom-specs/boss/<page_type>/``, 让代码和 spec 可追溯对齐.

## 工作流程 (见 ``docs/dom-specs/README.md``)

    [1] dump 真实 DOM     ← 本脚本
    [2] 归纳 selector 合同   ← docs/dom-specs/boss/<page_type>/README.md
    [3] 把合同落到代码       ← src/pulse/mcp_servers/_boss_platform_runtime.py
    [4] 真实 trace 回放       ← tests/pulse/mcp_servers/ (纯函数形态守卫)
                              + 线上 audit (boss_mcp_actions.jsonl)

## 为什么不能用 F12 view-source

BOSS 是 Vue SPA, 右键"查看源代码"只拿到 `<div id='app'>加载中</div>`
的壳. 实际 DOM 由 JS 渲染. 且 BOSS 内置反爬 — 一旦打开 DevTools
会触发 ``debugger;`` 循环或 ``outerWidth`` 差异检测直接重定向走.

Playwright 走 CDP 远控, DevTools UI 不打开, 反爬看不见; 且复用 Pulse
生产环境同一个 ``~/.pulse/boss_browser_profile`` (patchright stealth
+ 登录态), 与线上 ``scan_jobs`` / ``greet_jobs`` 走同一条渲染路径,
dump 结果即线上 DOM.

## 支持的 page_type

* ``search-list``  — 搜索结果页 job card 列表
                     典型用途: scan/greet 的 ``_extract_jobs_from_page``
* ``chat-list``    — 会话列表页左栏
                     典型用途: 托管模式监听"未读"/"新招呼" tab 新消息
* ``chat-detail``  — 单条会话右侧消息流 + HR 卡片 + 底部工具栏
                     典型用途: 自动回复 — 识别 HR 卡片"同意/拒绝",
                     或主动点"发简历" / "换简历"

## Usage

    python scripts/dump_boss_dom.py search-list "Agent开发实习生"
    python scripts/dump_boss_dom.py chat-list
    python scripts/dump_boss_dom.py chat-detail         # 默认 30s 让你手动选会话
    python scripts/dump_boss_dom.py chat-detail 45      # 自定义倒计时

## 产物

    docs/dom-specs/boss/<page_type>/<ts>.html     # 人读 (class tree + outerHTML)
    docs/dom-specs/boss/<page_type>/<ts>.json     # 机读 (下游可作 fixture)

## 前置

* ``~/.pulse/boss_browser_profile`` 已登录 BOSS
* Pulse backend 正在 scan 的话会抢 SingletonLock, 建议先停
"""
from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from pulse.mcp_servers._boss_platform_runtime import (  # noqa: E402
    _browser_timeout_ms,
    _build_search_url_candidates,
    _default_job_card_selectors,
    _ensure_browser_page,
)


_DOM_SPECS_ROOT = ROOT / "docs" / "dom-specs" / "boss"

# 深度 6 够覆盖 BOSS 所有业务节点 (card/会话 item/消息气泡都是浅树).
# text 前 40 字让 "class → 真实内容" 的对应一目了然, 避免翻 outerHTML.
_EXTRACT_JS = r"""
nodes => {
  const walkTree = (el, depth) => {
    if (!el || depth > 6) return null;
    const children = Array.from(el.children || [])
      .map(c => walkTree(c, depth + 1))
      .filter(Boolean);
    return {
      tag: (el.tagName || '').toLowerCase(),
      class: el.className || '',
      text: ((el.innerText || el.textContent || '')
        .replace(/\s+/g, ' ').trim()).slice(0, 40),
      children,
    };
  };
  return nodes.slice(0, 6).map(card => ({
    outer_html: card.outerHTML,
    text: ((card.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 400),
    tree: walkTree(card, 0),
  }));
}
"""


@dataclass
class ProbeSpec:
    """一个 page_type 的采集合同.

    ``selectors`` 是**候选列表**, 命中第一个就停 — 兼容 BOSS 历史上
    多次改版的外层 class (e.g. ``.job-list-box .job-card-wrapper`` ↔
    ``ul .job-card-box``), 避免写死单一 selector 被一次改版直接打穷.

    ``manual_staging_seconds`` (新增): 导航完成后, 在 dump 之前停顿的
    秒数. > 0 时脚本会在 stdout 倒计时, 期间可以在 Pulse headed 浏览器
    里手动切 tab / 点开目标会话 — 因为每次脚本运行都是新 Playwright
    page (from ``_ensure_browser_page()``), 没有"上一次手动操作"的
    持久状态可继承, 必须在**单次运行内部**给出手动介入窗口.
    """

    name: str
    summary: str
    needs_navigation: bool
    target_url: str | None
    wait_selectors: tuple[str, ...]
    selectors: tuple[str, ...]
    manual_staging_seconds: int = 0


def _search_list_spec(query: str) -> ProbeSpec:
    urls = _build_search_url_candidates(keyword=query, page=1, city=None)
    return ProbeSpec(
        name="search-list",
        summary=f"搜索结果页 job card (query={query!r})",
        needs_navigation=True,
        target_url=urls[0],
        wait_selectors=_default_job_card_selectors(),
        selectors=_default_job_card_selectors(),
    )


def _chat_list_spec() -> ProbeSpec:
    return ProbeSpec(
        name="chat-list",
        summary="会话列表页 (/web/geek/chat) 左栏; 脚本不自动切 tab, "
                "跑之前请自己在浏览器里把 tab 切到你要观察的那个 "
                "(比如'未读(3)'或'新招呼')",
        needs_navigation=True,
        target_url="https://www.zhipin.com/web/geek/chat",
        # 候选覆盖两代改版 — 一旦都没命中, 终端会把所有候选列出来帮助排查.
        wait_selectors=(
            ".user-list",
            ".chat-list",
            "[class*='conversation-list']",
            "[class*='user-list']",
        ),
        selectors=(
            ".user-list li",
            ".chat-list li",
            "[class*='conversation-list'] [class*='conversation-item']",
            "[class*='user-list'] li",
        ),
    )


def _chat_detail_spec(wait_seconds: int) -> ProbeSpec:
    return ProbeSpec(
        name="chat-detail",
        summary="单条会话右栏 (消息流 + HR 卡片 + 底部工具栏); "
                f"脚本导航到 /chat 后停留 {wait_seconds}s, 期间你在 Pulse "
                "浏览器里手动切 tab + 点开目标会话, 倒计时到 0 再 dump",
        needs_navigation=True,
        target_url="https://www.zhipin.com/web/geek/chat",
        wait_selectors=(
            ".chat-record",
            ".chat-conversation",
            "[class*='chat-conversation']",
            "[class*='message-list']",
        ),
        # 多路候选 selector — 命中第一个即 dump 那一路; 合同该抓什么
        # 之后再说. 第一轮目标: 先摸清"有 HR 卡片+消息流+底部工具栏"
        # 页面里哪些 class 存在.
        selectors=(
            ".chat-conversation",
            "[class*='chat-conversation']",
            ".chat-record",
            "[class*='message-list']",
        ),
        manual_staging_seconds=wait_seconds,
    )


def _build_spec(argv: list[str]) -> ProbeSpec:
    if len(argv) < 2:
        print(__doc__)
        sys.exit(2)
    page_type = argv[1].strip()
    if page_type == "search-list":
        query = (argv[2] if len(argv) > 2 else "Agent开发实习生").strip()
        return _search_list_spec(query)
    if page_type == "chat-list":
        return _chat_list_spec()
    if page_type == "chat-detail":
        wait = 30
        if len(argv) > 2:
            try:
                wait = max(0, int(argv[2]))
            except ValueError:
                print(f"[dump][FATAL] chat-detail second argument must be "
                      f"int seconds, got {argv[2]!r}", file=sys.stderr)
                sys.exit(2)
        return _chat_detail_spec(wait_seconds=wait)
    print(f"[dump][FATAL] unknown page_type {page_type!r}. "
          f"支持: search-list | chat-list | chat-detail", file=sys.stderr)
    sys.exit(2)


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _render_tree(node: dict | None, depth: int = 0) -> list[str]:
    if node is None:
        return []
    indent = "  " * depth
    cls = node.get("class") or ""
    cls_suffix = f".{cls.replace(' ', '.')}" if cls else ""
    text = node.get("text") or ""
    text_suffix = f"  # {text!r}" if text else ""
    out = [f"{indent}<{node.get('tag', '?')}{cls_suffix}>{text_suffix}"]
    for child in node.get("children") or []:
        out.extend(_render_tree(child, depth + 1))
    return out


def _escape_for_pre(html_text: str) -> str:
    return (
        html_text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _wait_render(page, timeout_ms: int) -> None:
    # 三次循环: 每次 (networkidle + sleep + 滚轮触发懒加载). BOSS 会在
    # 视口滚动时才填充 `boss-logo` 头像和部分 card 元数据, 不滚导致
    # 字段空字符串 (影响 text preview, 但不影响 class tree).
    for delay_ms in (800, 1500, 2500):
        try:
            page.wait_for_load_state("networkidle",
                                     timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        time.sleep(delay_ms / 1000)
        try:
            page.mouse.wheel(0, 400)
        except Exception:
            pass


def _wait_for_first_present(page, selectors: tuple[str, ...],
                            timeout_ms: int) -> str | None:
    for sel in selectors:
        try:
            page.wait_for_selector(sel, timeout=min(timeout_ms, 4000),
                                   state="attached")
            return sel
        except Exception:
            continue
    return None


def _collect(page, selectors: tuple[str, ...]
             ) -> tuple[list[dict], str | None]:
    for sel in selectors:
        try:
            result = page.eval_on_selector_all(sel, _EXTRACT_JS)
        except Exception as exc:
            print(f"[dump] selector {sel!r} eval failed: {exc!s:.200s}")
            continue
        if isinstance(result, list) and result:
            rows = [r for r in result if isinstance(r, dict)]
            if rows:
                return rows, sel
    return [], None


def _write_outputs(spec: ProbeSpec, landed_url: str, hit_selector: str | None,
                   nodes: list[dict], ts: str) -> tuple[Path, Path]:
    out_dir = _DOM_SPECS_ROOT / spec.name
    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / f"{ts}.html"
    json_path = out_dir / f"{ts}.json"

    html_parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>boss {spec.name} @ {ts}</title>",
        "<style>body{font-family:sans-serif;max-width:1200px;margin:2em auto;}"
        "pre{background:#f4f4f4;padding:1em;overflow:auto;white-space:pre-wrap;}"
        "h2{border-top:1px solid #ccc;padding-top:1em;}</style>",
        f"<h1>BOSS {spec.name} dump @ {ts}</h1>",
        "<p><b>summary:</b> " + spec.summary + "<br>"
        f"<b>landed_url:</b> {landed_url}<br>"
        f"<b>hit_selector:</b> <code>{hit_selector}</code><br>"
        f"<b>node_count:</b> {len(nodes)}</p>",
    ]
    for i, node in enumerate(nodes):
        html_parts += [
            f"<h2>Node #{i} — innerText preview</h2>",
            f"<pre>{node.get('text', '')!r}</pre>",
            f"<h2>Node #{i} — class tree</h2>",
            "<pre>" + "\n".join(_render_tree(node.get("tree"))) + "</pre>",
            f"<h2>Node #{i} — outerHTML (raw)</h2>",
            "<details open><summary>expand</summary><pre>"
            + _escape_for_pre(node.get("outer_html", ""))
            + "</pre></details>",
        ]
    html_path.write_text("\n".join(html_parts), encoding="utf-8")
    json_path.write_text(json.dumps({
        "ts": ts,
        "page_type": spec.name,
        "summary": spec.summary,
        "landed_url": landed_url,
        "hit_selector": hit_selector,
        "nodes": nodes,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return html_path, json_path


def main(argv: list[str]) -> int:
    spec = _build_spec(argv)
    print(f"[dump] page_type={spec.name} — {spec.summary}")

    page = _ensure_browser_page()
    timeout_ms = _browser_timeout_ms()

    if spec.needs_navigation and spec.target_url:
        try:
            page.goto(spec.target_url,
                      wait_until="domcontentloaded",
                      timeout=timeout_ms)
        except Exception as exc:
            print(f"[dump][FATAL] goto {spec.target_url!r} failed: "
                  f"{exc!s:.200s}", file=sys.stderr)
            return 2
    print(f"[dump] landed on {page.url!r}")

    if spec.manual_staging_seconds > 0:
        total = spec.manual_staging_seconds
        print(f"[dump] ==== MANUAL STAGING {total}s ====")
        print("[dump] 在 Pulse 的 headed 浏览器窗口里:")
        print("[dump]   1. 把左栏 tab 切到你想观察的那个")
        print("[dump]   2. 点开一条目标会话 (chat-detail 建议选带"
              "'同意/拒绝'卡片的那种)")
        print("[dump]   3. 等右栏消息流 + 底部工具栏都渲染出来")
        print("[dump] 倒计时到 0 自动 dump 当前 page DOM.")
        for remaining in range(total, 0, -1):
            print(f"[dump]   {remaining:3d}s...", end="\r", flush=True)
            time.sleep(1)
        print("[dump]        ")  # 覆盖最后的倒计时行
        print(f"[dump] staging over. current url: {page.url!r}")

    _wait_render(page, timeout_ms)
    found_wait = _wait_for_first_present(page, spec.wait_selectors, timeout_ms)
    if found_wait:
        print(f"[dump] wait_selector matched: {found_wait!r}")
    else:
        print(f"[dump][WARN] none of wait_selectors attached; "
              f"continuing anyway — page may be blank or logged out. "
              f"candidates={list(spec.wait_selectors)}")

    nodes, hit_selector = _collect(page, spec.selectors)
    if not nodes:
        print(
            f"[dump][FATAL] no node matched any selector.\n"
            f"  current_url={page.url!r}\n"
            f"  page_title={page.title()!r}\n"
            f"  selectors_tried={list(spec.selectors)}\n"
            "  可能原因: (a) 需要登录 profile; (b) tab 不对或还没渲染; "
            "(c) BOSS 改版外层 class — 在浏览器 Elements 面板手动找到 "
            "目标节点 class 后把它加进对应 ProbeSpec.selectors.",
            file=sys.stderr,
        )
        return 3

    ts = _now_ts()
    html_path, json_path = _write_outputs(
        spec, page.url, hit_selector, nodes, ts)
    print(f"[dump] hit_selector={hit_selector!r}  node_count={len(nodes)}")
    print(f"[dump] wrote {html_path}")
    print(f"[dump] wrote {json_path}")
    print(f"[dump] next: 编辑 docs/dom-specs/boss/{spec.name}/README.md "
          f"写下这一轮观察到的 selector 合同.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
