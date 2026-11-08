# -*- coding: utf-8 -*-
"""管理页与插件页桥的约定测试。

背景（我犯过的错）：页面用裸 `fetch("/api/plug/...")` 调自己的接口，
浏览器只报 "Failed to fetch"。原因是插件页被 Dashboard 以 iframe 方式加载，
**鉴权 token 在父窗口**，裸 fetch 带不上凭证，在浏览器网络层就失败了。

AstrBot 的正确姿势是 `window.AstrBotPluginPage.apiGet/apiPost`
（`astrbot/dashboard/plugin_page_bridge.js`），由父窗口代为带鉴权请求。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import webadmin  # noqa: E402

PAGE = (Path(__file__).resolve().parent.parent
        / "pages" / "admin" / "index.html")


def _page_code() -> str:
    """取出内联 script 并**去掉注释**（注释里会提到 fetch 做说明）。"""
    s = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>",
                        s, re.DOTALL)
    assert blocks, "管理页应有内联 script"
    code = "\n".join(blocks)
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.DOTALL)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
    return code


def test_page_exists_and_is_selfcontained():
    assert PAGE.is_file(), "管理页入口 pages/admin/index.html 必须存在"
    s = PAGE.read_text(encoding="utf-8")
    # 单文件、无外部依赖：不能有外链 js/css
    ext = re.findall(r'<(?:script|link)[^>]*\b(?:src|href)="(http[^"]*)"', s)
    assert not ext, f"管理页不应依赖外部资源: {ext}"


def test_page_uses_bridge_not_raw_fetch():
    """**关键约束**：必须走插件页桥，不能裸 fetch。"""
    code = _page_code()
    assert not re.search(r"\bfetch\s*\(", code), (
        "管理页不得使用裸 fetch —— 插件页在 iframe 里、token 在父窗口，"
        "裸 fetch 必然 'Failed to fetch'。请用 window.AstrBotPluginPage.apiGet/apiPost")
    assert "apiGet(" in code and "apiPost(" in code, "应通过桥的 apiGet/apiPost 调用"


def test_page_hardcodes_no_api_prefix():
    """页面不能硬编码 /api/plug 或 /api/v1 —— 前缀由桥决定。"""
    code = _page_code()
    assert "/api/plug/" not in code, "不要硬编码 /api/plug 前缀"
    assert "/api/v1/" not in code, "不要硬编码 /api/v1 前缀"


def test_page_waits_for_bridge_and_has_timeout():
    """桥脚本注入在 </body> 之前（晚于本页脚本），必须轮询等待。

    且 bridge 的 `ready()` 只在父窗口发来 context 消息时才 resolve
    （plugin_page_bridge.js:139），不在 iframe 里会永远挂着 ——
    所以等待与请求都必须有超时，否则页面静默卡死。
    """
    code = _page_code()
    assert "bridgeReady" in code, "必须等待桥出现（桥脚本晚于本页脚本注入）"
    assert "withTimeout" in code, "桥的 ready()/请求都必须有超时保护"
    assert re.search(r"withTimeout\(\s*B\.ready\(\)", code), \
        "ready() 必须有超时：它只在父窗口发 context 时 resolve"


def test_page_endpoints_are_all_registered():
    """页面调用的每个 endpoint 都必须在 webadmin 里注册过。"""
    code = _page_code()
    called = set()
    for m in re.finditer(r'(?:get|post)\(\s*"([^"]+)"', code):
        ep = m.group(1).split("?")[0].lstrip("/")
        called.add(ep)
    assert called, "页面应至少调用一个接口"

    # 从 webadmin.register 的 add() 调用里抠出已注册路径
    src = (Path(__file__).resolve().parent.parent
           / "core" / "webadmin.py").read_text(encoding="utf-8")
    registered = {p.lstrip("/") for p in
                  re.findall(r'add\(\s*"([^"]+)"', src)}
    missing = sorted(called - registered)
    assert not missing, f"页面调用了未注册的接口: {missing}"


def test_registered_prefix_matches_bridge_contract():
    """注册路径必须包含插件名（桥传的是 `<plugin>/<endpoint>`）。

    桥把 endpoint 拼成 `/api/v1/plugins/extensions/<plugin>/<endpoint>`，
    后端据此取 `plugin_path = "<plugin>/<endpoint>"` 再去匹配注册表。
    所以注册时必须以插件名为前缀，只写 `/personas` 会匹配不上。
    """
    assert webadmin.API_PREFIX == "/astrbot_plugin_chatcore_neo", \
        "API_PREFIX 应与插件目录名一致，否则桥拼出的路径匹配不到"
    src = (Path(__file__).resolve().parent.parent
           / "core" / "webadmin.py").read_text(encoding="utf-8")
    assert 'reg(f"{P}{path}"' in src, "注册时应拼上 API_PREFIX"


def test_error_envelope_reports_status_for_bridge():
    """错误信封必须带 status:"error"，桥据此 reject 并回传 message。

    桥的父窗口逻辑：`if (r.data?.status === "error") throw new Error(r.data.message)`。
    只给 `{ok:false}` 的话不会抛错，页面拿不到失败原因。
    """
    ok = webadmin._ok({"x": 1})
    err = webadmin._err("炸了")
    assert ok.get("status") == "ok"
    assert err.get("status") == "error", "错误信封必须带 status=error"
    assert err.get("message"), "错误信封必须带 message（桥用它抛错）"
    assert err.get("ok") is False


def test_page_endpoints_use_relative_paths():
    """桥要求 endpoint 是相对路径，不能含 "?" / "#" / "://"。"""
    code = _page_code()
    for m in re.finditer(r'(?:get|post)\(\s*"([^"]+)"', code):
        ep = m.group(1)
        q = ep.find("?")
        clean = ep[:q] if q >= 0 else ep
        assert not clean.startswith("http"), f"{ep} 应是相对路径"
        assert "#" not in clean and "://" not in clean, f"{ep} 不合法"
        assert not clean.startswith("//"), f"{ep} 不合法"
