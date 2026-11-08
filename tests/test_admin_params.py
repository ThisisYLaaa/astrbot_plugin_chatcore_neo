# -*- coding: utf-8 -*-
"""参数传递的集成测试：**按 AstrBot 真实的调用方式**调 handler。

事故复盘
--------
我之前的管理接口测试是**直接调 builder 函数**的，所以完全没发现：
AstrBot 调用插件 Web API 时只传 **path 参数**：

    asgi_runtime._call_view(view_func, path_params)
      → view_func(**path_params)

查询串和 JSON body **都不会**进函数签名。结果：
  * `async def handler(pid: str = "")` 里 pid 永远是 `""`
    → `/persona?pid=xxx` 报"找不到人格"
  * `async def handler(payload=None)` 里 payload 永远是 `None`
    → 所有 POST 保存实际上什么都没改

所以这里**必须模拟真实分发**：只传 path 参数（通常为空），
把查询串/body 放进 AstrBot 的请求上下文里，再调 handler。
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))

# AstrBot 的 app 目录（用于拿 api.web 的请求上下文）
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

from test_admin_api import FakePlugin, _plugin, _seed  # noqa: E402

from core import webadmin  # noqa: E402


class _Query:
    """最小化的 request.query（PluginMultiDict 的 get 语义）。"""

    def __init__(self, pairs: dict):
        self._p = dict(pairs)

    def get(self, key, default=None, type=None):
        v = self._p.get(key, None)
        if v is None:
            return default
        if type is not None:
            try:
                return type(v)
            except (TypeError, ValueError):
                return default
        return v


class _FakeRequest:
    def __init__(self, query=None, body=None):
        self.query = _Query(query or {})
        self._body = body or {}

    async def json(self, default=None):
        return self._b if hasattr(self, "_b") else self._body


def _collect(plugin) -> dict:
    """注册接口并返回 {path: handler}。"""
    seen: dict[str, object] = {}

    class Ctx:
        def register_web_api(self, route, handler, methods, desc):
            seen[route.split("/")[-1] if False else route] = handler

    plugin.context = Ctx()
    webadmin.register(plugin)
    return seen


def _invoke(handler, query=None, body=None):
    """按 AstrBot 的方式调：不传任何 kwargs，参数放请求上下文。"""
    try:
        from astrbot.api import web as astrbot_web
    except Exception:
        from run_tests import Skip
        raise Skip("拿不到 astrbot.api.web，跳过集成测试")

    req = _FakeRequest(query=query, body=body)
    token = astrbot_web._request_var.set(req)
    try:
        r = asyncio.run(handler())
    finally:
        astrbot_web._request_var.reset(token)
    # handler 返回的是信封 {"ok","status","data"}；
    # 插件页桥会替前端拆掉这一层，这里也照做，断言才写在真正的数据上。
    if isinstance(r, dict) and "ok" in r and "data" in r:
        return r["data"]
    return r


def _handler(routes: dict, suffix: str):
    for route, h in routes.items():
        if route.endswith(suffix):
            return h
    raise AssertionError(f"没注册 {suffix}")


# ---------------------------------------------------------------- GET

def test_persona_detail_receives_pid():
    """**用户报的那个 bug**：点「查看」不该报"找不到人格"。"""
    p = _plugin()
    lst = webadmin.build_personas(p)
    if not lst["personas"]:
        from run_tests import Skip
        raise Skip("没有人格数据")
    # 故意取一个不是第一个的人格，确保真的是 pid 生效而不是"回落到第一个"
    want = lst["personas"][-1]["id"]
    routes = _collect(p)
    d = _invoke(_handler(routes, "/persona"), {"pid": want})
    assert d.get("id") == want, f"pid 没传进去，拿到的是 {d.get('id')!r}"


def test_build_persona_via_handler_matches_direct_call():
    p = _plugin()
    lst = webadmin.build_personas(p)
    if not lst["personas"]:
        from run_tests import Skip
        raise Skip("没有人格数据")
    pid = lst["personas"][0]["id"]
    direct = webadmin.build_persona(p, pid)
    via = _invoke(_handler(_collect(p), "/persona"), {"pid": pid})
    assert direct.get("id") == via.get("id")


def test_state_get_receives_query_params():
    p = _plugin()
    umo = _seed(p)
    routes = _collect(p)
    d = _invoke(_handler(routes, "/state"),
                {"umo": umo, "persona": "p1", "uid": "42"})
    assert d["state"], d
    # uid 传没传进去看 tier：特权名单里没有 "42" → friend；
    # uid 为空时 build_state 返回的是 intimate。所以这里必须是 friend。
    assert d["tier"] == "friend", \
        f"uid 没传进去（空 uid 会得到 intimate）: {d.get('tier')}"
    assert d["log_count"] >= 1, "应读到该成员的状态日志"


def test_graph_node_receives_key():
    p = _plugin()
    _seed(p)
    key = next(iter(p.memory.graph.nodes), "")
    if not key:
        from run_tests import Skip
        raise Skip("图谱为空")
    d = _invoke(_handler(_collect(p), "/graph/node"), {"key": key})
    assert d["node"]["id"] == key, d


def test_graph_search_receives_q():
    p = _plugin()
    _seed(p)
    d = _invoke(_handler(_collect(p), "/graph/search"), {"q": "卡拉彼丘"})
    assert d.get("query") == "卡拉彼丘", d


def test_memory_receives_umo():
    p = _plugin()
    umo = _seed(p)
    d = _invoke(_handler(_collect(p), "/memory"), {"umo": umo})
    assert d.get("key"), d
    assert d.get("atoms"), "umo 没传进去会拿到空结果"


def test_preview_receives_all_params():
    p = _plugin()
    lst = webadmin.build_personas(p)
    if not lst["personas"]:
        from run_tests import Skip
        raise Skip("没有人格数据")
    want = lst["personas"][-1]["id"]
    d = _invoke(_handler(_collect(p), "/preview"),
                {"persona": want, "is_private": "1", "msg_mode": "2"})
    # 稳定段应随人格不同而不同 —— 用它证明 persona 生效了
    other = webadmin.build_preview(p, lst["personas"][0]["id"])
    if len(lst["personas"]) > 1:
        assert d.get("stable_text") != other.get("stable_text"), \
            "persona 参数没生效（跟第一个人格的预览一样）"


def test_impression_receives_uid():
    p = _plugin()
    webadmin.set_impression(p, "42", sfw="自动总结内容")
    d = _invoke(_handler(_collect(p), "/impression"), {"uid": "42"})
    assert d.get("uid") == "42"
    assert d["impression"].get("sfw") == "自动总结内容", d


# ---------------------------------------------------------------- POST

def test_state_set_receives_json_body():
    """**POST body 也不会进签名** —— 必须主动读。"""
    p = _plugin()
    umo = _seed(p)
    routes = _collect(p)
    d = _invoke(_handler(routes, "/state/set"), None,
                {"umo": umo, "persona": "p1", "uid": "42",
                 "values": {"affection": 91}})
    assert d["state"]["affection"] == 91, d
    # 真的落盘了吗
    assert webadmin.build_state(p, umo, "p1", "42")["state"]["affection"] == 91


def test_inject_set_receives_json_body():
    p = _plugin()
    umo = _seed(p)
    d = _invoke(_handler(_collect(p), "/inject/set"), None,
                {"umo": umo, "persona": "p1", "uid": "42",
                 "text": "你暗恋他"})
    assert d.get("inject") == "你暗恋他", d


def test_groups_assign_receives_json_body():
    p = _plugin()
    umo = _seed(p)
    d = _invoke(_handler(_collect(p), "/groups/assign"), None,
                {"umo": umo, "group": "G1"})
    # body 里只有一个 umo（不是 umos 列表）时 handler 也要认：added 是回执。
    assert d.get("added") == [umo], d.get("added")
    # 组值在 v2 从「成员列表」变成了「组配置对象」
    # （persona/costars/instance/members），前端按 groups[g].members 渲染。
    assert umo in ((d["groups"].get("G1") or {}).get("members") or []), \
        d["groups"].get("G1")


def test_memory_recall_receives_json_body():
    p = _plugin()
    umo = _seed(p)
    d = _invoke(_handler(_collect(p), "/memory/recall"), None,
                {"umo": umo, "query": "卡拉彼丘", "top_k": 3})
    assert d.get("query") == "卡拉彼丘", d
    assert "fused" in d


def test_segment_test_receives_json_body():
    p = _plugin()
    d = _invoke(_handler(_collect(p), "/segment/test"), None,
                {"text": "第一句话。第二句话。", "is_private": True})
    assert d.get("input") == "第一句话。第二句话。", d


def test_clear_log_receives_json_body():
    p = _plugin()
    umo = _seed(p)
    d = _invoke(_handler(_collect(p), "/state/clear-log"), None,
                {"umo": umo, "persona": "p1", "uid": "42"})
    assert d.get("cleared", 0) >= 1, d


# ---------------------------------------------------------------- 助手

def test_query_helpers_read_from_context():
    """_q/_qi/_qb 必须能从请求上下文取值。"""
    try:
        from astrbot.api import web as astrbot_web
    except Exception:
        from run_tests import Skip
        raise Skip("拿不到 astrbot.api.web")

    req = _FakeRequest(query={"s": "abc", "n": "7", "b": "true"})
    token = astrbot_web._request_var.set(req)
    try:
        assert webadmin._q("s") == "abc"
        assert webadmin._q("missing", "dflt") == "dflt"
        assert webadmin._qi("n", 0) == 7
        assert webadmin._qi("missing", 5) == 5
        assert webadmin._qb("b") is True
        assert webadmin._qb("missing") is False
    finally:
        astrbot_web._request_var.reset(token)


def test_query_helpers_are_safe_outside_handler():
    """不在 handler 里调用时返回默认值，不能抛异常。"""
    # 测试进程里通常没有绑定请求上下文
    try:
        from astrbot.api import web as astrbot_web
        astrbot_web._request_var.set(None)  # type: ignore[arg-type]
    except Exception:
        pass
    assert webadmin._q("x", "d") in ("d", "")
    assert webadmin._qb("x") in (True, False)
    assert webadmin._qi("x", 3) in (3, 0)
