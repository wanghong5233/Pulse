"""ONE-SHOT forensic probe: dump BOSS 搜索结果页 job-card 真实 DOM.

> 用完即删 — 按 `docs/code-review-checklist.md` "测试宪法·不留僵尸".

## 为什么需要这个脚本

BOSS 搜索页是 Vue SPA, 右键"查看网页源代码"只拿到 empty shell
(`<div id='app'>加载中</div>`). 真实 card DOM 要等 JS 渲染完才出现,
普通 F12 又会被 BOSS 的反爬检测 (debugger; / outerWidth 差异) 直接
跳转重定向.

Playwright 走 CDP 远程控制, 不经过 DevTools UI, 反爬看不见它 ——
而且 Pulse 本来每天 `scan_jobs` 就在用同一条路径正常抓字段, 说明这
条路径经过验证.

## 做什么

1. 复用 ``_ensure_browser_page()`` (同 ``~/.pulse/boss_browser_profile``,
   保留登录态 + patchright stealth, 和 prod scan 完全一致).
2. 导航到搜索结果页, 等 card 渲染.
3. 抓前 3 张 card 的:
   * ``outer_html`` — 完整 HTML 用于 selector 设计
   * ``text`` — innerText 快速确认命中的是哪个卡片
   * ``tree`` — class tree (深度≤6, 每个元素 40 字 innerText preview),
     便于肉眼扫视 "company" 这个 class 挂在公司节点还是地址节点.
4. dump 到 ``logs/boss_card_dump_<ts>.html`` (人读) + ``.json`` (机器).

## 前置条件

- Pulse backend 最好先关 (避免和 ``~/.pulse/boss_browser_profile`` 的
  SingletonLock 撞) — 但如果 backend 已经没在 scan, 一般也能共存.
- 需要已在这个 profile 登录过 BOSS (未登录 BOSS 会显示登录弹窗,
  dump 出来没有 card).

## Usage

    python3 Pulse/scripts/dump_boss_card_html.py "Agent开发实习生"

完成后:

    rm Pulse/scripts/dump_boss_card_html.py
"""
from __future__ import annotations

import json
import sys
import time
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


# JS: 抓 3 张 card 的 outerHTML + 可读 class tree.
# 深度≤6 够覆盖 BOSS card 的全部业务节点 (job-name / company-name / job-area).
# text 前 40 字是为了让 class 到真实内容的对应关系一目了然.
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
  return nodes.slice(0, 3).map(card => ({
    outer_html: card.outerHTML,
    text: ((card.innerText || '').replace(/\s+/g, ' ').trim()).slice(0, 300),
    tree: walkTree(card, 0),
  }));
}
"""


def _now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _render_tree(node: dict, depth: int = 0) -> list[str]:
    """Pretty-print class tree into a shell-readable outline."""
    if node is None:
        return []
    indent = "  " * depth
    cls = node.get("class") or ""
    cls_suffix = f".{cls.replace(' ', '.')}" if cls else ""
    text = node.get("text") or ""
    text_suffix = f"  # {text!r}" if text else ""
    line = f"{indent}<{node.get('tag','?')}{cls_suffix}>{text_suffix}"
    out = [line]
    for child in node.get("children") or []:
        out.extend(_render_tree(child, depth + 1))
    return out


def main(argv: list[str]) -> int:
    keyword = (argv[1] if len(argv) > 1 else "Agent开发实习生").strip()
    print(f"[dump] keyword={keyword!r}")

    page = _ensure_browser_page()
    timeout_ms = _browser_timeout_ms()
    urls = _build_search_url_candidates(keyword=keyword, page=1, city=None)
    print(f"[dump] trying {len(urls)} url candidate(s): {urls}")

    nav_url = ""
    last_err: str | None = None
    for url in urls:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            nav_url = url
            break
        except Exception as exc:
            last_err = f"goto {url}: {exc!s:.200s}"
            continue
    if not nav_url:
        print(f"[dump][FATAL] navigation failed: {last_err}", file=sys.stderr)
        return 2
    print(f"[dump] landed on {page.url!r}")

    for delay in (800, 1500, 2500):
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 5000))
        except Exception:
            pass
        time.sleep(delay / 1000)
        try:
            page.mouse.wheel(0, 400)
        except Exception:
            pass

    cards: list[dict] = []
    hit_selector: str | None = None
    for selector in _default_job_card_selectors():
        try:
            result = page.eval_on_selector_all(selector, _EXTRACT_JS)
        except Exception as exc:
            print(f"[dump] selector {selector!r} eval failed: {exc!s:.200s}")
            continue
        if isinstance(result, list) and result:
            cards = [row for row in result if isinstance(row, dict)]
            hit_selector = selector
            break

    if not cards:
        print(
            "[dump][FATAL] no job cards matched any selector in "
            f"{_default_job_card_selectors()}.\n"
            f"  current_url={page.url!r}\n"
            f"  page_title={page.title()!r}\n"
            "  可能是 (a) 需要登录 — 去浏览器里 profile 手动登一次; "
            "(b) 搜索返回 0 结果; (c) BOSS 改版了最外层 card class.",
            file=sys.stderr,
        )
        return 3

    ts = _now_ts()
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    html_path = logs_dir / f"boss_card_dump_{ts}.html"
    json_path = logs_dir / f"boss_card_dump_{ts}.json"

    html_parts = [
        "<!doctype html><meta charset='utf-8'><title>boss_card_dump</title>",
        "<style>body{font-family:sans-serif;max-width:1200px;margin:2em auto;}"
        "pre{background:#f4f4f4;padding:1em;overflow:auto;white-space:pre-wrap;}"
        "h2{border-top:1px solid #ccc;padding-top:1em;}</style>",
        f"<h1>BOSS card dump @ {ts}</h1>",
        f"<p><b>keyword:</b> {keyword}<br>"
        f"<b>landed_url:</b> {page.url}<br>"
        f"<b>hit_selector:</b> <code>{hit_selector}</code><br>"
        f"<b>card_count:</b> {len(cards)}</p>",
    ]
    for i, card in enumerate(cards):
        html_parts.append(f"<h2>Card #{i} — innerText preview</h2>")
        html_parts.append(f"<pre>{card.get('text','')!r}</pre>")
        html_parts.append(f"<h2>Card #{i} — class tree</h2>")
        html_parts.append("<pre>" + "\n".join(_render_tree(card.get("tree"))) + "</pre>")
        html_parts.append(f"<h2>Card #{i} — outerHTML (raw)</h2>")
        html_parts.append("<details open><summary>expand</summary><pre>"
                          + _escape_for_pre(card.get("outer_html", ""))
                          + "</pre></details>")

    html_path.write_text("\n".join(html_parts), encoding="utf-8")
    json_path.write_text(json.dumps({
        "ts": ts,
        "keyword": keyword,
        "landed_url": page.url,
        "hit_selector": hit_selector,
        "cards": cards,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[dump] wrote {html_path}")
    print(f"[dump] wrote {json_path}")
    print("[dump] done. remember: 'rm Pulse/scripts/dump_boss_card_html.py'")
    return 0


def _escape_for_pre(html_text: str) -> str:
    return (
        html_text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv))
