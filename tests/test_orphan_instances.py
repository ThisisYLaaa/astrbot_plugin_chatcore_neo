# -*- coding: utf-8 -*-
"""「已移除实例」的盘点与清理（用户删了平台实例，插件这边还留着数据）。

需求（2026-10-30）：用户在 AstrBot 里删掉了实例 `Sui`，但插件仍然
在配置页/会话设置/记忆组里显示 Sui 的配置和会话数据。

这里的方案：
  * `orphan_ids()` 找出"磁盘上有 config.json、但 AstrBot 的 platform
    数组里已经没有"的实例；
  * 正常页面（实例清单 / 人格分配 / 会话列表 / 主动消息）**不再显示**
    这些实例的数据；
  * 总览页给一张「已移除的实例」卡，一键**归档**（默认，可恢复）或
    彻底删除，同时清掉共享映射里指向它的键。

本模块用真实的小对象（StateStore / MemoryGroups / CoStarStore / JsonMap）
在临时目录里跑，不去碰线上 plugin_data。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import webadmin  # noqa: E402
from core.costars import CoStarStore  # noqa: E402
from core.groups import MemoryGroups  # noqa: E402
from core.instances import InstanceStore  # noqa: E402
from core.sessionmap import JsonMap  # noqa: E402
from core.state.store import StateStore, safe_segment  # noqa: E402


class _Cfg(dict):
    """AstrBot 配置桩：只有 `platform` 有用。"""


class _FakePlugin:
    def __init__(self, root: Path, platforms: list[dict]):
        self.root = root
        cfg = _Cfg({"platform": platforms})
        self._inst_store = InstanceStore(cfg, root=root / "instances")
        self.state = StateStore(root / "state")
        self.groups = MemoryGroups(root / "groups.json")
        self.costars = CoStarStore(root / "_session_costars.json")
        self._session_persona = self.state.session_persona
        self._session_params = JsonMap(root / "state" / "_session_params.json")
        self._runtimes = {}
        self._inst_cfgs = {}
        self._alive = set()
        # build_instances / build_session_params 需要的少量属性
        self.config = {"instance_auto_create": False}
        self._default_inst = "Banyan"
        self._persona_pool = type("P", (), {
            "grants": staticmethod(lambda inst: []),
        })()
        self._user_names = None
        self._group_names = None

    def _cfg(self, key, default=None, inst=None):
        return default

    def mem(self, inst=""):
        return None


def _mk(platforms=None) -> _FakePlugin:
    root = Path(tempfile.mkdtemp())
    p = _FakePlugin(root, platforms if platforms is not None else [
        {"id": "Banyan", "enable": True, "type": "aiocqhttp"},
    ])
    for inst in ("Banyan", "Sui"):
        d = p._inst_store.root / inst
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.json").write_text("{}", encoding="utf-8")
        (d / "memory").mkdir(exist_ok=True)
        (d / "memory" / "memory.db").write_bytes(b"x" * 64)
    return p


def test_orphan_ids_means_disk_minus_platform():
    """Sui 在磁盘上、但 platform 里没有 → 它是已移除实例。"""
    p = _mk()
    assert p._inst_store.live_ids() == ["Banyan"]
    assert p._inst_store.orphan_ids() == ["Sui"]


def test_build_orphans_counts_sessions_groups_and_size():
    p = _mk()
    umo = "Sui:GroupMessage:123"
    (p.state.root / safe_segment(umo)).mkdir(parents=True, exist_ok=True)
    p.groups.assign(umo, "G1")
    d = webadmin.build_orphans(p)
    assert [o["id"] for o in d["orphans"]] == ["Sui"], d
    o = d["orphans"][0]
    assert o["sessions"] == 1, o
    assert o["groups"] == 1, o
    assert o["bytes"] > 0 and o["has_memory"] is True, o


def test_purge_archives_dir_and_clears_references():
    """归档：目录搬到 _archive，共享映射/记忆组/副人格里的 Sui 键被清掉。"""
    p = _mk()
    umo = "Sui:GroupMessage:123"
    other = "Banyan:GroupMessage:9"
    (p.state.root / safe_segment(umo)).mkdir(parents=True, exist_ok=True)
    (p.state.root / safe_segment(other)).mkdir(parents=True, exist_ok=True)
    p.groups.assign(umo, "G1")
    p._session_persona.set(umo, "arke_si")
    p._session_params.set(umo, {"mode": 2})
    p.costars.set(umo, ["arke_si"])

    r = webadmin.purge_orphan_instance(p, "Sui")
    assert r["ok"] and r["deleted"] is False
    assert r["archived_to"] and Path(r["archived_to"]).is_dir(), r
    assert not (p._inst_store.root / "Sui").exists(), "原目录应已移走"
    # 会话目录只剩 Banyan 的
    assert p.state.sessions() == [safe_segment(other)], p.state.sessions()
    # 共享映射 / 记忆组 / 副人格都清干净了
    assert umo not in p._session_persona, "会话人格映射仍残留 Sui 键"
    assert umo not in p._session_params, "发言参数仍残留 Sui 键"
    assert p.groups.group_of(umo) == umo, "记忆组成员未清理"
    assert umo not in p.costars.all(), "副人格仍残留 Sui 键"
    # 另一个实例的数据一点没动
    assert other in p._session_persona or p.state.sessions() == [safe_segment(other)]


def test_purge_delete_removes_dir_without_archive():
    p = _mk()
    r = webadmin.purge_orphan_instance(p, "Sui", delete=True)
    assert r["deleted"] is True and not r["archived_to"]
    assert not (p._inst_store.root / "Sui").exists()
    assert not any(p.root.glob("_archive/instances/*"))


def test_purge_refuses_live_instance():
    """还在 AstrBot 里的实例不许被清 —— 否则一次误点就删掉线上数据。"""
    p = _mk()
    try:
        webadmin.purge_orphan_instance(p, "Banyan")
    except ValueError as e:
        assert "仍然是" in str(e), e
    else:
        raise AssertionError("live 实例必须被拒绝")


def test_purge_rejects_path_traversal():
    p = _mk()
    for bad in ("../etc", "a/b", "..", ""):
        try:
            webadmin.purge_orphan_instance(p, bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"应当拒绝非法 id：{bad!r}")


def test_instances_and_persona_pool_hide_orphans():
    """正常页面不该再列出已移除实例（实例清单 / 人格分配矩阵）。"""
    p = _mk()
    ids = [m["id"] for m in webadmin.build_instances(p)["instances"]]
    assert "Sui" not in ids and "Banyan" in ids, ids


def test_session_params_rows_skip_orphan_sessions():
    """会话设置的行来自 build_sessions，已移除实例的会话必须整条消失。"""
    p = _mk()
    (p.state.root / safe_segment("Sui:GroupMessage:123")).mkdir(parents=True, exist_ok=True)
    (p.state.root / safe_segment("Banyan:GroupMessage:9")).mkdir(parents=True, exist_ok=True)
    p.personas = type("X", (), {"all": staticmethod(lambda: [])})()
    d = webadmin.build_sessions(p, alive=None)
    umos = [s["umo"] for s in d["sessions"]]
    assert "Banyan:GroupMessage:9" in umos, umos
    assert not [u for u in umos if u.startswith("Sui:")], umos
    assert not [s for s in d.get("stale_sessions", [])
                if str(s.get("umo", "")).startswith("Sui:")], \
        "已移除实例的会话不该出现在残留清单里（那会被'一键清理残留'误删）"

def test_default_instance_is_not_an_orphan():
    """`default` 是插件的兜底实例目录，不能被当成"用户删除的实例"去清理。"""
    p = _mk()
    d = p._inst_store.root / "default"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.json").write_text("{}", encoding="utf-8")
    assert p._inst_store.orphan_ids() == ["Sui"], p._inst_store.orphan_ids()

