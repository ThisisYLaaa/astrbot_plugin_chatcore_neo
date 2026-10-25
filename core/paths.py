# -*- coding: utf-8 -*-
"""chatcore_neo 路径解析。

所有路径从插件自身位置推导，不依赖 ASTRBOT_ROOT 环境变量，
也不依赖任何其他插件的存在（旧 lili_state 的反射式耦合就此消除）。

目录关系：
    <data>/plugins/astrbot_plugin_chatcore_neo/core/paths.py
    <data>/plugins/astrbot_plugin_chatcore_neo/          <- PLUGIN_DIR
    <data>/plugins/                                      <- PLUGINS_DIR
    <data>/                                              <- DATA_DIR
    <data>/plugin_data/chatcore_neo/                     <- ROOT   （本插件数据根）
    <data>/skills/                                       <- SKILLS_DIR（AstrBot 技能树，只读）
    <data>/config/astrbot_plugin_chatcore_neo_config.json
"""
from __future__ import annotations

from pathlib import Path

PLUGIN_DIR: Path = Path(__file__).resolve().parent.parent
PLUGINS_DIR: Path = PLUGIN_DIR.parent
DATA_DIR: Path = PLUGINS_DIR.parent

# ---- 本插件数据根 ----
ROOT: Path = DATA_DIR / "plugin_data" / "chatcore_neo"

PERSONAS_DIR: Path = ROOT / "personas"      # 人格池（**不在 skills/ 下，因而不进 AstrBot 技能索引**）
STATE_DIR: Path = ROOT / "state"            # 状态数据（umo 键，跨实例共享）
MEMORY_DIR: Path = ROOT / "memory"          # 旧版单库（迁移后仅作备份，不再写入）
PROACTIVE_DIR: Path = ROOT / "proactive"    # 旧版单目录（同上）

# ---- 实例化：每个平台适配器一份配置 / 记忆 / 人格授权 ----
INSTANCES_DIR: Path = ROOT / "instances"


def inst_root(inst: str) -> Path:
    """某实例的数据根：`instances/<id>/`。"""
    return INSTANCES_DIR / str(inst)


def inst_memory_dir(inst: str) -> Path:
    """某实例的记忆目录（memory.db + graph.db 都放这里）。"""
    return inst_root(inst) / "memory"


def inst_memory_db(inst: str) -> Path:
    return inst_memory_dir(inst) / "memory.db"


def inst_proactive_dir(inst: str) -> Path:
    return inst_root(inst) / "proactive"


def inst_config_json(inst: str) -> Path:
    return inst_root(inst) / "config.json"


def inst_personas_json(inst: str) -> Path:
    return inst_root(inst) / "personas.json"

# ---- 共享资源（AstrBot 技能树，只读）----
SKILLS_DIR: Path = DATA_DIR / "skills"
FRAMEWORK_DIR: Path = SKILLS_DIR / "_pfm_framework"
WORLDS_DIR: Path = SKILLS_DIR / "_worlds"
TEMPLATE_DIR: Path = SKILLS_DIR / "_pfm_template"

# ---- 单文件 ----
GROUPS_JSON: Path = ROOT / "groups.json"                  # 原 chatcore_lite
CHARACTERS_JSON: Path = ROOT / "characters.json"          # 角色名/外号库（旧 strinova_news 的 roles.json 同构）
EVENT_TEMPLATES: Path = ROOT / "storage" / "event_templates.json"
MIGRATION_REPORT: Path = ROOT / "migration_report.json"
DOCTOR_REPORT: Path = ROOT / "doctor_report.json"

# 旧环境版本标记：迁移脚本据此识别「尚未迁移」的旧布局
SENTINEL_JSON: Path = ROOT / ".chatcore_neo"

# ---- 旧环境（四插件合并之前的那套数据根）----
#
# 由本插件的位置反推，不写死绝对路径：
#   <...>/Astrbot_Env/.new_env/data/plugins/<plugin>  ->  DATA_DIR
#   <...>/Astrbot_Env/.astrbot/data                   ->  LEGACY_DATA_DIR
#
# 只用来做**一次性**的昵称/群名补缺：老 lili_state 的 `_name_cache.json`
# 里躺着 15 个群名 + 5805 条成员昵称，新环境凭空造不出来（平台的
# `get_group_list` 只给群名，成员昵称还得逐群拉）。文件不在就静默跳过。
LEGACY_DATA_DIR: Path = DATA_DIR.parent.parent / ".astrbot" / "data"
LEGACY_NAME_CACHE: Path = (LEGACY_DATA_DIR / "plugin_data" / "lili_state"
                           / "_name_cache.json")


def ensure_dirs() -> None:
    """建立数据根骨架（幂等）。"""
    for d in (ROOT, PERSONAS_DIR, STATE_DIR, MEMORY_DIR, PROACTIVE_DIR,
              INSTANCES_DIR, ROOT / "storage", ROOT / "logs"):
        d.mkdir(parents=True, exist_ok=True)


def is_migrated() -> bool:
    return SENTINEL_JSON.exists()
