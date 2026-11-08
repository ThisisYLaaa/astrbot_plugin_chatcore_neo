# -*- coding: utf-8 -*-
"""冒烟：**真正实例化插件**（不是只 import）。

为什么必须有这个测试
--------------------
`main.py` 里 `self._num(...)` 用了 10 次，而 `_num` 从未被定义。
后果是**插件完全加载不了**（AstrBot 日志：`AttributeError:
'ChatCoreNeoPlugin' object has no attribute '_num'`）。

为什么之前的检查全都没抓到：
  * `ast.parse` / `py_compile` —— 语法没错
  * `import main` —— **import 不执行方法体**
  * `test_no_undefined_names` —— 它只收集模块级定义，
    看不到"实例属性在某个方法里被使用但从未定义"
  * `test_plugin_wiring` —— 只查 `self.X.Y()` 形式，
    `self._num(...)` 是 `self.<方法>()`，被当成"方法调用"放过了

只有**真的 `ChatCoreNeoPlugin(context, config)` 一次**才能发现。

注意：本测试会真实读写 `plugin_data`（与 AstrBot 加载插件时一致）。
它只做只读或幂等操作，不写业务数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN))
sys.path.insert(0, str(PLUGIN.parent))
import _env  # noqa: E402
_APP = _env.app()
if _APP.is_dir():
    sys.path.insert(0, str(_APP))

import importlib  # noqa: E402

try:
    plugin_main = importlib.import_module(f"{PLUGIN.name}.main")
except Exception as e:  # pragma: no cover
    from run_tests import Skip
    raise Skip(f"无法导入插件入口：{e!r}")

Neo = plugin_main.ChatCoreNeoPlugin

CFG = _env.plugin_config()


class _Ctx:
    def __init__(self):
        self.registered: list[str] = []

    def register_web_api(self, route, handler, methods, desc):
        self.registered.append(route)

    def get_all_providers(self):
        return []

    def get_all_embedding_providers(self):
        return []

    def get_all_tts_providers(self):
        return []

    def get_using_provider(self, umo=None):
        return None

    def get_using_tts_provider(self, umo=None):
        return None

    def get_provider_by_id(self, pid):
        return None

    def get_event_queue(self):
        return None


class _Cfg(dict):
    def get(self, k, d=None):
        return dict.get(self, k, d)

    def save_config(self):
        return None


def _real_cfg() -> _Cfg:
    if CFG.is_file():
        return _Cfg(json.loads(CFG.read_text(encoding="utf-8-sig")))
    return _Cfg({"enable_admin_page": True})


def test_plugin_instantiates():
    """**核心断言**：用真实配置实例化插件不能抛异常。

    这就是 AstrBot 加载插件时做的事。
    """
    ctx = _Ctx()
    p = Neo(ctx, _real_cfg())
    assert p is not None
    assert len(ctx.registered) >= 20, \
        f"管理接口注册太少：{len(ctx.registered)}"
    assert len(p.personas) > 0, "人格没加载"


def test_every_self_method_used_exists():
    """静态查：`self.<name>()` 调用的名字必须是类上真实存在的方法/属性。

    这是能抓到 `self._num` 那类问题的检查 —— `test_plugin_wiring` 只覆盖
    `self.X.Y()`，覆盖不到 `self.X()`。
    """
    import ast
    tree = ast.parse((PLUGIN / "main.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef)
               and n.name == "ChatCoreNeoPlugin")
    defined = {n.name for n in cls.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    # __init__ 里 self.x = ... 赋值的实例属性
    for node in ast.walk(cls):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (isinstance(t, ast.Attribute)
                        and isinstance(t.value, ast.Name)
                        and t.value.id == "self"):
                    defined.add(t.attr)
        if isinstance(node, ast.AnnAssign):
            t = node.target
            if (isinstance(t, ast.Attribute)
                    and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                defined.add(t.attr)
    missing = []
    for node in ast.walk(cls):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name)
                and f.value.id == "self"):
            if f.attr not in defined:
                missing.append(f"main.py:{node.lineno} self.{f.attr}() 未定义")
    assert not missing, (
        "以下 self.<name>() 在类上不存在（运行时会 AttributeError）:\n  "
        + "\n  ".join(sorted(set(missing))))


def test_key_runtime_methods_callable():
    """实例化后跑几个关键方法，确认没有别的"只在运行期才炸"的名字。"""
    p = Neo(_Ctx(), _real_cfg())
    docs = p.personas.all()
    if not docs:
        from run_tests import Skip
        raise Skip("没有人格数据")
    st = p.state.load("Sui:GroupMessage:__smoke__", docs[0].id, "1")
    assert p._thought(st, "测试") , "_thought 返回空"
    assert isinstance(p._unrestricted(), set)
    assert isinstance(p._default_affection(is_private=True, uid=""), int)
    ok, why = p.scheduler.should_emit("Sui:GroupMessage:__smoke__")
    assert isinstance(ok, bool) and isinstance(why, str)
    from core import webadmin  # noqa: E402
    assert webadmin.build_stats(p), "build_stats 返回空"


def test_proactive_state_persisted_on_disk():
    """主动消息状态要真的落盘，而且是**每实例一份**（2026-10-30 起）。

    旧实现只有一个全局 `proactive/state.json`：四个实例共用一份 overrides，
    Banyan 里配的会话在 Sui 的调度器里也读得到（用户报的"记忆组/配置串实例"）。
    现在路径是 `instances/<inst>/proactive/state.json`，这里同时钉住两点：
      * 全局那份不该再出现（否则又变回共享）；
      * 每个文件里只能有自己的实例前缀，不能混进别人的会话。
    """
    root = _env.plugin_root()
    files = sorted((root / "instances").glob("*/proactive/state.json"))
    if not files:
        from run_tests import Skip
        raise Skip("尚未产生主动消息状态（插件还没记录过用户消息）")
    legacy = root / "proactive" / "state.json"
    assert not legacy.is_file(), \
        "不该再有全局 proactive/state.json（会让所有实例共享一份覆盖）"
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        assert "sessions" in d and "overrides" in d, f
        inst = f.parent.parent.name
        for umo in list((d.get("overrides") or {})) + list(d.get("sessions") or {}):
            assert umo.split(":", 1)[0] == inst, \
                f"{f} 混进了别的实例的会话：{umo}"


def test_session_persona_map_is_shared_with_state_store():
    """会话人格表只能有一份内存对象。

    main 与 StateStore 若各自 new 一个 JsonMap 指向同一个文件，
    /persona 切换写的是 main 那份，而 terminate() 保存的是 StateStore
    启动时的旧快照 —— 插件一重载就整表回滚（切换过的人格全丢）。
    """
    p = Neo(_Ctx(), _real_cfg())
    assert p._session_persona is p.state.session_persona, \
        "main._session_persona 与 state.session_persona 不是同一对象，重载会回滚人格切换"
    assert p._initialized is p.state.session_initialized


def test_initialize_starts_proactive_scheduler():
    """initialize() 必须把主动消息循环跑起来，且不再有实例级总开关。

    旧实现只在 __init__ 里 new 了 ProactiveScheduler 却从不 start()，
    主循环从未运行（state.json 里 ticks 恒为 0）。

    2026-10-30 语义变更：主动消息改成**独立功能** —— 实例配置里的
    `enable_proactive` 与 4 个 `proactive_*` 已从 schema 删除，循环无条件
    启动；谁开口只由「主动消息 → 分会话配置」的 opt-in 决定。
    """
    import asyncio
    p = Neo(_Ctx(), _real_cfg())

    async def _run():
        await p.initialize()
        assert p.scheduler.running, "initialize() 后调度循环未运行"
        inst = getattr(p, "_default_inst", "") or "Banyan"
        assert p.scheduler.in_scope(inst + ":GroupMessage:1") is True, \
            "调度器应只管自己实例的会话"
        assert p.scheduler.in_scope("Other:GroupMessage:1") is False, \
            "别的实例的会话不该落进这条循环"
        await p.scheduler.stop()

    asyncio.run(_run())


def test_scheduler_known_predicate_rejects_fake_sessions():
    """`known` 判据必须接到真实状态目录上。

    账本只增不减：测试脚本打进来的假会话（webchat e2e / Sui:GroupMessage:1）
    也会被 `note_user_message` 记下来。判据接错（比如恒 True）就会让它们
    一直留在管理页的「各会话活跃度」里，还会被当成候选真的去开口。
    """
    from run_tests import Skip
    p = Neo(_Ctx(), _real_cfg())
    assert p.scheduler._known is not None, "调度器没接上 _session_known"

    fake = "webchat:FriendMessage:webchat!astrbot!e2e-not-a-real-session"
    assert p.scheduler.is_known(fake) is False, "不存在的会话被判成了真会话"

    dirs = p.state.sessions()
    if not dirs:
        raise Skip("本机还没有任何会话状态目录")
    from core.state import safe_segment
    for d in dirs:
        if ":" not in d:
            continue
        umo = d.replace("_", ":", 2)
        if safe_segment(umo) == d:
            assert p.scheduler.is_known(umo) is True, f"真会话被判成了假会话: {umo}"
