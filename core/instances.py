# -*- coding: utf-8 -*-
"""实例化：每个平台适配器（Banyan / Sui / …）一份独立配置与数据根。

设计
----
AstrBot 的插件配置是**全局单文件**（`star_manager` 启动时读一次），
但一个 AstrBot 进程里可以挂多个平台适配器，每个适配器背后是一个 QQ 号、
一套人群、一套记忆。这个模块把「实例」这一维度补进插件层：

    instances/<实例 id>/
        config.json      # 该实例的完整配置快照（143 个 key，schema 自动补键）
        personas.json    # 该实例可用的人格 id 列表（授权表）
        memory/          # memory.db + graph.db
        proactive/       # 主动消息调度状态

实例 id = UMO 的第一段（`Banyan:GroupMessage:123` -> `Banyan`）。

本模块不 import 任何 chatcore 内部逻辑，只依赖 paths 与框架的 AstrBotConfig，
方便单测。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from . import paths

# 新实例的「最小可用」预设：只留人格 + 记忆 + 基础回复。
# 其余全部显式关掉 —— 新号刚接进来时不该自动插话、分段、主动找人。
MINIMAL_PRESET: dict[str, Any] = {
    "enabled": True,
    "enable_memory": True,
    "enable_quote": True,
    "enable_admin_page": True,
    "enable_attention": False,
    "enable_segmentation": False,
    "enable_markers": False,
    "enable_proactive": False,
    "enable_tts": False,
    "enable_streaming": False,
    "enable_no_reply_judge": False,
    "enable_intent": False,
    "enable_graph_retrieval": False,
    "costar_enabled": False,
    "char_mention_enabled": False,
    "enable_affection": False,
    "enable_lewdness": False,
    "enable_impression": False,
    "enable_nsfw_impression": False,
    "poke_enabled": False,
    "merge_pending_messages": False,
    # 新实例不自动认主：必须手工填
    "privilege_uid": "",
}


class InstanceStore:
    """实例发现 / 配置加载 / 目录创建。全部惰性、幂等。"""

    def __init__(self, astrbot_config: Any = None,
                 root: Path | None = None,
                 schema_path: Path | None = None) -> None:
        self.astrbot_config = astrbot_config
        self.root = Path(root) if root else paths.INSTANCES_DIR
        self.schema_path = Path(schema_path) if schema_path else (
            paths.PLUGIN_DIR / "_conf_schema.json")
        self._cfgs: dict[str, Any] = {}
        self._meta: dict[str, dict] = {}
        self._default: str = ""

    # ------------------------------------------------------------ 发现
    def discover(self) -> list[dict]:
        """从 AstrBot 配置里的 `platform` 数组取实例（含禁用项）。"""
        out: list[dict] = []
        try:
            plats = self.astrbot_config.get("platform", []) or []
        except Exception:
            plats = []
        if not isinstance(plats, list):
            plats = []
        for p in plats:
            if not isinstance(p, dict):
                continue
            iid = str(p.get("id") or "").strip()
            if not iid:
                continue
            out.append({
                "id": iid,
                "type": str(p.get("type") or ""),
                "enabled": bool(p.get("enable", True)),
            })
        return out

    def loaded_ids(self) -> list[str]:
        """磁盘上已存在 config.json 的实例。"""
        if not self.root.is_dir():
            return []
        return sorted(p.parent.name for p in self.root.glob("*/config.json"))

    def default_id(self) -> str:
        if self._default:
            return self._default
        # 已有配置的实例优先（迁移后 Banyan 一定在）
        loaded = self.loaded_ids()
        if loaded:
            self._default = loaded[0]
            return self._default
        live = [d["id"] for d in self.discover() if d["enabled"]]
        self._default = live[0] if live else ""
        return self._default

    # ------------------------------------------------------------ 目录 / 配置
    def _schema(self) -> Any:
        try:
            return json.loads(self.schema_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return {}

    def cfg_path(self, inst: str) -> Path:
        return self.root / inst / "config.json"

    def load_cfg(self, inst: str) -> Any:
        """加载（并缓存）某实例的 AstrBotConfig；文件不存在返回 None。"""
        if inst in self._cfgs:
            return self._cfgs[inst]
        p = self.cfg_path(inst)
        if not p.exists():
            return None
        from astrbot.api import AstrBotConfig
        try:
            cfg = AstrBotConfig(config_path=str(p), schema=self._schema())
        except Exception as e:  # 损坏时不要拖垮整个插件
            logger.error(f"[chatcore_neo] 实例 {inst} 配置加载失败：{e!r}")
            return None
        self._cfgs[inst] = cfg
        return cfg

    def ensure(self, inst: str, template: Any = None,
               preset: dict | None = None) -> Any:
        """确保实例目录 + 配置存在；已存在则不动。

        返回该实例的 AstrBotConfig。
        """
        d = self.root / inst
        (d / "memory").mkdir(parents=True, exist_ok=True)
        (d / "proactive").mkdir(parents=True, exist_ok=True)
        p = d / "config.json"
        if not p.exists():
            data: dict = {}
            try:
                data = dict(template or {})
            except Exception:
                data = {}
            data.update(preset or {})
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            logger.info(f"[chatcore_neo] 已为实例 {inst} 创建配置：{p}")
        return self.load_cfg(inst)

    def ensure_all(self, template: Any = None,
                   use_preset: bool = True) -> dict[str, Any]:
        """发现的所有**启用**实例都补齐目录；返回 {id: cfg}。

        `template` 就是**框架面板里的那份插件配置**——它只当模板：
        已存在的实例文件一律不动，所以改模板不会影响现有实例。
        新建时：第一次装机用完整模板；之后新增的按 `use_preset` 决定是否
        再叠一层「最小可用」预设（关注意力/分段/主动消息等）。
        """
        out: dict[str, Any] = {}
        had_any = bool(self.loaded_ids())
        for meta in self.discover():
            iid = meta["id"]
            if not (meta["enabled"] or self.cfg_path(iid).exists()):
                continue
            exists = self.cfg_path(iid).exists()
            preset = None
            if use_preset and (had_any or exists):
                preset = MINIMAL_PRESET
            cfg = self.ensure(iid, template=template, preset=preset)
            if cfg is not None:
                out[iid] = cfg
            had_any = True
        if not out:
            # 一个都没发现（比如平台还没配）时，用模板兜底，保证插件可用
            iid = "default"
            cfg = self.ensure(iid, template=template, preset=None)
            if cfg is not None:
                out[iid] = cfg
        return out

    # ------------------------------------------------------------ 运行时信息
    def meta(self, inst: str) -> dict:
        if inst in self._meta:
            return self._meta[inst]
        info = next((d for d in self.discover() if d["id"] == inst), {})
        m = {
            "id": inst,
            "enabled": bool(info.get("enabled", True)),
            "type": info.get("type", ""),
            "known": bool(info),
            "has_config": self.cfg_path(inst).exists(),
        }
        self._meta[inst] = m
        return m

    def invalidate(self) -> None:
        self._cfgs.clear()
        self._meta.clear()

    # ------------------------------------------------------------ 备份 / 删除
    def backup(self, inst: str) -> Path | None:
        src = self.root / inst
        if not src.is_dir():
            return None
        dst = self.root / f"_backup_{inst}_{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(src, dst)
        return dst
