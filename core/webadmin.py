# -*- coding: utf-8 -*-
"""WebUI 管理接口。

注册路径经 AstrBot 插件 WebAPI 约定暴露，前端页面通过**插件页桥**
（`window.AstrBotPluginPage.apiGet/apiPost`）调用 —— 不能用裸 fetch，
插件页在 iframe 里、鉴权 token 在父窗口。

设计原则
--------
* **只暴露已经存在的运行时状态**，不在这里额外计算或缓存 ——
  管理页看到的必须和 bot 实际用的是同一份数据。
* 写操作都走 `StateStore` / `GraphStore` / `MemoryGroups` 等既有对象，
  与聊天命令共用同一条路径，避免"页面改了但运行中没生效"。
* 服务端只返回 JSON；渲染全在前端。
* 数据可能有几万条的（图谱条目），一律**限量 + 按度排序**再返回。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from .state.store import safe_segment

API_PREFIX = "/astrbot_plugin_chatcore_neo"


# 信封同时带 `ok` 与 `status`：
#   * `status` 是 AstrBot 插件页桥的约定 —— 父窗口看到 status=="error" 会直接
#     reject 并把 message 抛给页面，页面就能显示真正的错误原因。
#   * `ok` 是本页自己的判据，也兼容直接看接口返回的场景。
def _ok(data: Any) -> dict:
    return {"ok": True, "status": "ok", "data": data}


def _err(msg: str) -> dict:
    return {"ok": False, "status": "error", "message": str(msg),
            "error": str(msg)}


# ---------------------------------------------------------------- 请求参数
#
# **必须这样读参数。** AstrBot 的插件 Web API 只把 **path 参数**传给处理函数
# （`asgi_runtime._call_view` → `view_func(**path_params)`），
# 查询串和 JSON body 都**不会**出现在函数签名里。
# 直接写 `def handler(pid: str = "")` 会永远拿到默认值 ——
# 表现为"接口没报错但参数全空"，非常难查。

def _q(name: str, default: str = "") -> str:
    """读查询串参数（读不到时返回 default）。"""
    try:
        from astrbot.api.web import request
        v = request.query.get(name, None)
    except Exception:
        return default
    return default if v is None else str(v)


def _qb(name: str, default: bool = False) -> bool:
    """读布尔查询参数（1/true/yes/on 为真）。"""
    return _q(name, "1" if default else "0").strip().lower() in (
        "1", "true", "yes", "on")


def _qi(name: str, default: int = 0) -> int:
    """读整数查询参数。"""
    try:
        return int(_q(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


async def _body() -> dict:
    """读 POST 的 JSON body（读不到返回空 dict）。"""
    try:
        from astrbot.api.web import request
        data = await request.json({})
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- 人格

# ---------------------------------------------------------------- 实例与人格池

def _mem_stats(plugin: Any, inst: str) -> dict:
    try:
        from . import paths as _p
        db = _p.inst_memory_db(inst)
        return {"exists": db.exists(),
                "bytes": db.stat().st_size if db.exists() else 0}
    except Exception:
        return {}


def build_instances(plugin: Any) -> dict:
    """实例清单：已知平台 + 已有目录 + 授权数 + 记忆库大小。

    打开管理页时**自动补齐**缺失实例的目录与默认配置（用户要求的行为）：
    新平台接进来后，第一次打开 WebUI 就会看到它，且是「最小可用」配置。
    """
    store = plugin._inst_store
    pool = plugin._persona_pool
    if bool(plugin.config.get("instance_auto_create", True)):
        try:
            store.ensure_all(
                template=plugin.config,
                use_preset=bool(plugin.config.get("instance_new_preset", True)))
        except Exception:
            pass
    loaded = store.loaded_ids()
    rows = []
    for i in loaded:
        m = dict(store.meta(i))
        m["grants"] = pool.grants(i)
        m["memory"] = _mem_stats(plugin, i)
        m["privilege_uid"] = str(plugin._cfg("privilege_uid", "", inst=i) or "")
        rows.append(m)
    for meta in store.discover():
        if meta["id"] in loaded:
            continue
        rows.append({**store.meta(meta["id"]), "grants": [],
                     "memory": {}, "privilege_uid": ""})
    rows.sort(key=lambda r: (not r.get("enabled", True), r["id"]))
    return {"instances": rows, "default": plugin._default_inst}


def persona_pool_matrix(plugin: Any) -> dict:
    store = plugin._inst_store
    insts = [dict(m) for m in store.discover()]
    have = {m["id"] for m in insts}
    for i in store.loaded_ids():
        if i not in have:
            insts.append({"id": i, "enabled": True, "type": "",
                          "known": False})
    for m in insts:
        m.setdefault("known", True)
    out = plugin._persona_pool.matrix(plugin.personas.all(), insts)
    out["default"] = plugin._default_inst
    out["defaults"] = {
        m["id"]: str(plugin._cfg("persona_current", "", inst=m["id"]) or "")
        for m in insts
    }
    return out


def _check_inst(plugin: Any, inst: str) -> str:
    inst = str(inst or "").strip()
    if not inst:
        raise ValueError("inst 不能为空")
    known = {m["id"] for m in plugin._inst_store.discover()}
    if inst not in known and inst not in plugin._inst_store.loaded_ids():
        raise ValueError(f"未知实例：{inst}")
    return inst


def persona_pool_grant(plugin: Any, inst: str, pid: str) -> dict:
    inst = _check_inst(plugin, inst)
    pid = str(pid or "").strip()
    if not pid or pid not in plugin.personas:
        raise ValueError(f"未知人格：{pid}")
    plugin._persona_pool.grant(inst, pid)
    return persona_pool_matrix(plugin)


def persona_pool_revoke(plugin: Any, inst: str, pid: str) -> dict:
    inst = _check_inst(plugin, inst)
    plugin._persona_pool.revoke(inst, str(pid or "").strip())
    return persona_pool_matrix(plugin)


def persona_pool_bulk(plugin: Any, inst: str, pids: Any) -> dict:
    inst = _check_inst(plugin, inst)
    valid = {d.id for d in plugin.personas.all()}
    keep = [str(p).strip() for p in (pids or []) if str(p).strip() in valid]
    plugin._persona_pool.set_grants(inst, keep)
    return persona_pool_matrix(plugin)


def persona_pool_default(plugin: Any, inst: str, pid: str) -> dict:
    inst = _check_inst(plugin, inst)
    pid = str(pid or "").strip()
    if pid and not plugin._persona_pool.is_available(inst, pid):
        raise ValueError("该人格尚未授权给此实例")
    cfg = plugin.cfg_of(inst)
    cfg["persona_current"] = pid
    try:
        cfg.save_config()
    except Exception as e:
        raise ValueError(f"保存失败：{e!r}")
    return persona_pool_matrix(plugin)


def build_personas(plugin: Any) -> dict:
    out = []
    for d in plugin.personas.all():
        out.append({
            "id": d.id, "display_name": d.display_name,
            "display_name_en": d.display_name_en,
            "world": d.world, "world_role": d.world_role,
            "aliases": list(d.aliases),
            "legacy_dir": d.legacy_dir,
            "sections": len(d.outline.all_nodes()) if d.outline else 0,
            "lore_extracted": bool(d.lore and d.lore.extracted),
            "tiers": sorted(d.tiers.keys()),
        })
    return {"personas": out, "current": _current_id(plugin)}


def _current_id(plugin: Any) -> str:
    try:
        docs = plugin.personas.all()
        g = str(plugin._cfg("persona_current", "") or "").strip()
        doc = plugin.personas.resolve(g) if g else None
        if doc is None and docs:
            doc = docs[0]
        return doc.id if doc else ""
    except Exception:
        return ""


def build_persona(plugin: Any, pid: str) -> dict:
    """单个人格的详情：章节树、文件、体积、注入策略。"""
    doc = plugin.personas.resolve(pid) if pid else None
    if doc is None:
        return {"error": f"找不到人格 {pid}"}

    nodes = []
    try:
        for n in doc.outline.all_nodes():
            # SectionSpec 的字段是 `default_inject` / `description`；
            # OutlineNode 没有 `size`。旧写法用 getattr(..., "") 静默取空，
            # 于是页面上"注入策略"一列永远是空的。
            spec = getattr(n, "spec", None)
            nodes.append({
                "title": n.title, "level": n.level,
                "inject": (getattr(spec, "default_inject", "")
                           if spec is not None else ""),
                "desc": getattr(spec, "description", "") if spec is not None else "",
                "bytes": max(0, int(getattr(n, "body_end", 0) or 0)
                             - int(getattr(n, "body_start", 0) or 0)),
            })
    except Exception:
        pass

    # PersonaDoc 的字段是 `dir` / `content`(dict) / `skill_body`，
    # **没有** `skill_path` / `content_dir` —— 旧写法被 try/except 静默吞掉，
    # 于是页面永远显示"无文件"。
    files = []
    base = getattr(doc, "dir", None)
    if base is not None and Path(base).is_dir():
        for f in sorted(Path(base).rglob("*")):
            if f.is_file():
                try:
                    files.append({"name": str(f.relative_to(base)),
                                  "bytes": f.stat().st_size})
                except Exception:
                    continue

    return {
        "id": doc.id, "display_name": doc.display_name,
        "display_name_en": doc.display_name_en,
        "world": doc.world, "world_role": doc.world_role,
        "aliases": list(doc.aliases), "legacy_dir": doc.legacy_dir,
        "tiers": sorted(doc.tiers.keys()),
        "sections": nodes, "files": files,
        "lore_extracted": bool(doc.lore and doc.lore.extracted),
        "effective_inject": _effective_inject(doc),
    }


def _effective_inject(doc: Any) -> dict:
    """每个章节的最终注入策略（registry 优先，再回落到文档本身）。"""
    out = {}
    try:
        from .persona.outline import render_selection  # noqa: F401
    except Exception:
        pass
    for n in doc.outline.all_nodes():
        try:
            spec = getattr(n, "spec", None)
            if spec is not None:
                out[n.title] = {
                    "inject": getattr(spec, "default_inject", "") or "on_demand",
                    "desc": getattr(spec, "description", "") or "",
                }
            else:
                out[n.title] = {"inject": "on_demand", "desc": ""}
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 会话/状态

def _safe_to_real(plugin: Any) -> dict:
    """状态目录名（safe 名，冒号被换成下划线）→ 真实 umo。

    真实 umo 只有几个地方记着（会话人格表 / 注入覆盖表 的键）。旧实现还
    把 `st.sessions()` 的**目录名**也塞进映射（`out.setdefault(real, real)`），
    等于把 safe 名当成了它自己 —— 于是还原不出来的会话就以 `BotA_..._
    10006` 这种下划线名直接出现在页面上，看起来就是「没规格化的残留」。
    现在只接受含 `:` 的真实 umo 作为还原源。
    """
    try:
        from .state.model import safe_segment
    except Exception:
        def safe_segment(s):
            return re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", str(s))
    out = {}
    try:
        for real in list(plugin.state.session_persona.as_dict()):
            out[safe_segment(real)] = real
    except Exception:
        pass
    try:
        for real in list(plugin.state.inject_overrides.as_dict()):
            out.setdefault(safe_segment(real), real)
    except Exception:
        pass
    try:
        for real in plugin.state.sessions():
            if ":" in str(real):            # 目录里若已是真实名（异常情形）也接受
                out.setdefault(real, real)
    except Exception:
        pass
    return out


# 规格化 UMO：`实例:消息类型:id`。safe 目录名的位次识别用同一组类型。
SPEC_MSG_TYPES = ("GroupMessage", "FriendMessage", "StrangerMessage",
                  "GroupNotice", "FriendNotice", "GroupAdminEvent",
                  "GroupMemberIncrease", "GroupMemberDecrease", "RequestEvent")
_SPEC_RE = re.compile(r"^[^:\s]+:[^:\s]+:.+$")
# 会话目录名反推：`平台_消息类型_id`（safe 化是把 冒号/斜线 换成下划线），
# 只接受中间段是已知消息类型的严格匹配 —— 与"从人格目录名嗅探系列/世界"
# 完全是两码事，这里还原的是 UMO 的**结构**而非任何语义。
_DIR_UMO_RE = re.compile(
    r"^([^_]+)_(" + "|".join(re.escape(t) for t in SPEC_MSG_TYPES) + r")_(.+)$")
# 私聊形态的历史印象文件名：`实例_FriendMessage_对方QQ`。
# 旧代码 `uid or umo` 在私聊里（uid 恒为空）把印象写成了**以 umo 命名**的文件，
# 例如 `BotA_FriendMessage_10001.json`。它不是"垃圾用户"，而是
# **那个真人自己**的计数 —— 所以按"私聊会话 = 对方"归到他名下，
# 既不显示成待清理的残留，也不删任何东西。
_DIR_PRIV_RE = re.compile(r"^([^_]+)_(FriendMessage|StrangerMessage)_(.+)$")


def is_spec_umo(umo: str) -> bool:
    """是否为规格化 UMO（至少 `实例:类型:id` 三段，每段非空）。"""
    return bool(umo and _SPEC_RE.match(str(umo)))


def _resolve_umo(plugin: Any, dirname: str) -> tuple[str, bool]:
    r"""目录名 → (umo, 是否规格化)。

    先查 `_safe_to_real` 的还原表；查不到再按 `实例_类型_id` 反推：
    safe 化只把 `:`/`/`/`\` 换成 `_`，而第二段必须是已知消息类型，
    因此反推是安全的。反推不出来才算真正的残留。
    """
    s2r = _safe_to_real(plugin)
    r = s2r.get(dirname)
    if r and ":" in str(r):
        return str(r), is_spec_umo(str(r))
    m = _DIR_UMO_RE.match(str(dirname))
    if m:
        umo = m.group(1) + ":" + m.group(2) + ":" + m.group(3)
        return umo, is_spec_umo(umo)
    return str(dirname), is_spec_umo(str(dirname))


def _is_spec_uid(uid: str) -> bool:
    """用户 id 规格化：纯数字（QQ/昵称位 id）或以冒号分隔的平台 id。

    垃圾实例是 `BotA_FriendMessage_10001` 这种把 umo safe 化后
    当 uid 的历史文件 —— 含下划线，会被排除。
    """
    u = str(uid)
    if not u:
        return False
    if u.isdigit():
        return True
    return ":" in u and is_spec_umo(u)


def _nick(names: dict, uid: str) -> str:
    """取昵称。平台把 QQ 号当昵称返回时等于没有昵称 —— 别显示成「123（123）」。"""
    n = str((names or {}).get(str(uid), "") or "").strip()
    return "" if n == str(uid) else n


def _user_names(plugin: Any) -> dict[str, str]:
    """uid → 昵称（main 在每条消息里顺手记录，见 `_user_names.json`）。"""
    un = getattr(plugin, "_user_names", None)
    if un is None:
        return {}
    try:
        return {str(k): str(v) for k, v in un.as_dict().items()}
    except Exception:
        return {}


def _group_names(plugin: Any) -> dict[str, str]:
    """群 umo → 群名（main 从消息体里白捡 / 管理页调 API 补齐）。"""
    gn = getattr(plugin, "_group_names", None)
    if gn is None:
        return {}
    try:
        return {str(k): str(v) for k, v in gn.as_dict().items()}
    except Exception:
        return {}


def session_label_parts(umo: str, members: dict | None = None,
                        names: dict | None = None,
                        group_names: dict | None = None) -> dict:
    """把 UMO 拆成 `实例 / 群聊|私聊 / 名字` 三段。

    字段顺序按用户要求定为 **实例 → 类型 → 昵称**：前两段短且取值有限，
    排在最前面，下拉里后面的名字就能对齐（前端再按最长实例名补空格）。
    旧格式把名字放最前、后面挂一长串括号，看着就是一团。

    `name` 取不到时退回 id 并**明说**没取到 —— 旧写法把「小明、小红 等 3 人」
    摆在名字位置上，页面上看着就像群名就叫这个，是错的。

    数据来源：
      * 群名 = 消息体里的 `group_name`（`main._note_group_name`）
        / 管理页「补齐群名与昵称」/ 旧环境 `_name_cache.json`；
      * 昵称 = `_user_names`（消息里白捡 + 上面两条补缺）。
    """
    out = {"umo": str(umo), "instance": "", "kind": "",
           "sid": "", "name": "", "label": str(umo)}
    names = names or {}
    group_names = group_names or {}
    parts = str(umo).split(":")
    if len(parts) >= 3 and parts[1] in ("GroupMessage", "FriendMessage",
                                        "StrangerMessage"):
        instance = parts[0] or "?"
        is_group = parts[1] == "GroupMessage"
        sid = parts[2] if len(parts) == 3 else ":".join(parts[2:])
        kind = "群聊" if is_group else "私聊"
        if is_group:
            gname = str(group_names.get(str(umo), "") or "").strip()
            name = gname or f"群 {sid}（未取到群名）"
        else:
            name = _nick(names, sid) or sid
        out.update({"instance": instance, "kind": kind, "sid": sid,
                    "name": name})
        out["label"] = f"{instance} · {kind} · {name}"
    return out


def _session_label(umo: str, members: dict, names: dict,
                   group_names: dict | None = None) -> str:
    """ `{实例} · {群聊|私聊} · {名字}`（见 `session_label_parts`）。"""
    return session_label_parts(umo, members, names, group_names)["label"]


# 印象文件里字段名 → 是否有正文的判据（自动总结 + 人工覆写都算）
_IMP_TEXT_KEYS = ("sfw", "nsfw", "sfw_custom", "nsfw_custom")


def impression_has_text(imp: Any) -> bool:
    """这份印象里有没有真内容（空壳不算）。"""
    if not isinstance(imp, dict):
        return False
    return bool("".join(str(imp.get(k) or "") for k in _IMP_TEXT_KEYS).strip())


def _impression_index(plugin: Any) -> tuple[dict[str, dict], list[str]]:
    """扫一遍 `_impressions/` → (uid 索引, 真正无法归属的文件名列表)。

    能收进索引的两种文件名：
      * **规格化 uid**：纯数字 QQ / `平台:类型:id`；
      * **私聊形态的 umo 名**（`实例_FriendMessage_对方QQ`）：这是旧代码把
        私聊印象写成 umo 名留下的，归到**对方 QQ** 名下 —— 那本来就是他自己的
        数据（老插件留下的 `10001.json` 是真印象，这两份是同一个人的）。

    收不进去的（例如 `实例_GroupMessage_群号`，群号不是用户）才列成残留候选，
    交给「残留用户」清理 —— 不能因为不进索引就从页面上消失，那样用户
    永远看不到、也删不掉它们。

    同一个 uid 有多份文件时合并：`msg_count` 取较大值（同一份数据的副本，
    不该相加），`updated` 取较新，`has_impression` 任一有正文即为真。
    """
    idx: dict[str, dict] = {}
    junk: list[str] = []
    root = getattr(plugin.state, "root", None)
    d = (Path(root) / "_impressions") if root is not None else None
    if d is None or not d.is_dir():
        return idx, junk
    for f in d.glob("*.json"):
        stem = f.stem
        uid = stem
        if not _is_spec_uid(uid):
            m = _DIR_PRIV_RE.match(stem)
            if not m or not _is_spec_uid(m.group(3)):
                junk.append(stem)
                continue
            uid = m.group(3)          # 私聊 = 对方本人
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(j, dict):
            continue
        try:
            cur = idx.setdefault(uid, {"msg_count": 0, "updated": 0,
                                       "has_impression": False})
            cur["msg_count"] = max(cur["msg_count"],
                                   int(j.get("msg_count", 0) or 0))
            cur["updated"] = max(cur["updated"],
                                 int(j.get("updated", 0) or 0))
            cur["has_impression"] = (cur["has_impression"]
                                     or impression_has_text(j))
        except Exception:
            continue
    return idx, junk


def build_sessions(plugin: Any, alive: set[str] | None = None) -> dict:
    st = plugin.state
    names = _user_names(plugin)
    gnames = _group_names(plugin)
    imp_idx, imp_junk = _impression_index(plugin)
    sessions = []
    users: dict[str, str] = {}
    labels: dict[str, str] = {}
    stale_sessions: list[dict] = []
    for dirname in st.sessions():
        umo, spec = _resolve_umo(plugin, dirname)
        personas = st.personas_in(dirname)
        members = {p: st.members_in(dirname, p) for p in personas}
        lp = session_label_parts(umo, members, names, gnames)
        label = lp["label"]
        if not spec:
            # 还原不出来的目录：视为残留，不进会话下拉，单独列给清理。
            stale_sessions.append({"dir": dirname, "umo": umo, "label": label,
                                   "personas": personas})
            continue
        if alive is not None and safe_segment(umo) not in alive:
            # AstrBot 侧已删掉的「孤儿会话」：能还原出 UMO，但 DB 里已没有
            # 该会话的对话记录（用户在 AstrBot 删了它）。与还原不出的残留
            # 同列给清理，但标注原因 —— 别混进真会话下拉，也别让主动消息
            # 继续当它是活会话（`_session_known` 用同一判据）。
            # `alive` 是 safe 段格式（与 state 目录名同构），故用 safe_segment。
            stale_sessions.append({"dir": dirname, "umo": umo,
                                   "label": label + "（已从 AstrBot 删除）",
                                   "personas": personas, "orphan": True})
            continue
        sessions.append({"umo": umo, "dir": dirname,
                         "personas": personas, "members": members,
                         "label": label,
                         # 拆开的字段让前端能按「最长实例名」补空格做对齐
                         "instance": lp["instance"], "kind": lp["kind"],
                         "sid": lp["sid"], "name": lp["name"],
                         "group_name": str(gnames.get(umo, "") or "")})
        labels[umo] = label
        for uids in members.values():
            for u in uids:
                users.setdefault(u, names.get(u, ""))
    # 有过印象的用户也算「已知用户」；昵称表里的一并并入
    for uid in imp_idx:
        users.setdefault(uid, names.get(uid, ""))
    for uid, nm in names.items():
        users.setdefault(uid, nm)
    # 用户同样只留规格化 id（纯数字 QQ / 冒号平台 id）；像
    # `BotA_FriendMessage_10001.json` 这种把 umo 当 uid 的历史文件
    # （含下划线）归入残留，不再出现在用户下拉。
    clean_users: list[dict] = []
    stale_users: list[dict] = []
    for uid in list(users) + [j for j in imp_junk if j not in users]:
        if not _is_spec_uid(uid):
            stale_users.append({"uid": uid, "name": "", "msg_count": 0,
                                "updated": 0, "has_impression": False})
            continue
        info = imp_idx.get(uid) or {}
        clean_users.append({
            "uid": uid,
            # 平台把 QQ 号当昵称返回时等于没有昵称
            "name": _nick(names, uid),
            "msg_count": int(info.get("msg_count", 0) or 0),
            "updated": int(info.get("updated", 0) or 0),
            "has_impression": bool(info.get("has_impression")),
        })
    # 有名字的排在前面、按名字排；没名字的按 QQ 排。全是纯数字 QQ 时
    # 这个顺序等价于按 QQ 排，和旧行为一致。
    clean_users.sort(key=lambda u: (not u["name"], u["name"] or u["uid"]))
    stale_users.sort(key=lambda u: u["uid"])
    # 印象页下拉默认只列这一份：**真的有印象**的用户，按更新时间降序。
    # 314 个用户里只有 43 个有正文，把空壳也铺进下拉就是让人挑花眼。
    with_imp = sorted((u["uid"] for u in clean_users if u["has_impression"]),
                      key=lambda x: -int((imp_idx.get(x) or {}).get("updated", 0)))
    return {"sessions": sessions, "stats": st.stats(),
            "session_persona": st.session_persona.as_dict(),
            "inject_overrides": st.inject_overrides.as_dict(),
            "user_names": names,
            "group_names": gnames,
            "labels": labels,
            "users": clean_users,
            "users_with_impression": with_imp,
            "stale_sessions": stale_sessions,
            "stale_users": stale_users}


# ---------------------------------------------------------------- 补名

# 单次平台 API 调用的超时；`get_group_member_list` 在大群上会明显慢
_API_TIMEOUT = 10.0
# 一次补齐最多打多少个平台请求，防止把页面请求拖死
_API_BUDGET = 160
# 整个补齐过程的总时间预算。必须明显小于 AstrBot 插件页桥的 60s 超时，
# 否则一个卡住的平台会让页面直接报"请求超时"、连已拿到的部分都看不到。
_API_DEADLINE = 45.0


async def refresh_names(plugin: Any, *, groups: bool = True,
                        members: bool = True, friends: bool = True,
                        force: bool = False) -> dict:
    """调平台 API 把「群名」「用户昵称」补进本地映射表。

    平时这两样是**跟着消息白捡**的（`main._note_identity`），但 OneBot 的
    群消息事件不一定带 `group_name`（框架拿不到就填 `"N/A"`），而迁移过来
    的历史用户更是从来没记过昵称 —— 于是管理页里只剩一串光秃秃的 QQ 号。

    调用顺序经过挑选，尽量少打请求：
      1. `get_group_list` —— **一次**拿回 bot 所在全部群的 `group_name`，
         覆盖绝大多数会话（比逐群 `get_group_info` 省 N 倍请求）；
      2. 仍然没名字的群才补 `get_group_info`；
      3. `get_group_member_list`（逐群）拿成员群名片/昵称；
      4. `get_friend_list` 拿好友备注/昵称，补私聊会话。

    昵称一律**只补缺**（`force=True` 才按平台现状覆盖）：群名片常是玩笑话，
    覆盖已有的账号昵称只会让下拉变难看。

    只写 `_group_names.json` / `_user_names.json`，**不碰任何聊天状态**；
    取不到就跳过并如实列进 `errors`，不影响已经拿到的部分。

    适配器取法与 `astrbot_plugin_qqadmin/group_info_cache.py` 一致
    （`inst.get_client()` → `call_action(...)`），那是本机验证过可用的路径。
    """
    ctx = getattr(plugin, "context", None)
    if ctx is None:
        raise ValueError("插件上下文不可用，无法调用平台 API")
    get_inst = getattr(ctx, "get_platform_inst", None)
    if not callable(get_inst):
        raise ValueError("当前 AstrBot 版本不支持 get_platform_inst")

    gm = getattr(plugin, "_group_names", None)
    un = getattr(plugin, "_user_names", None)
    out: dict = {"groups": 0, "group_names": 0, "users": 0, "errors": [],
                 "skipped": [], "budget_exhausted": False,
                 "deadline_hit": False}
    budget = _API_BUDGET

    # 按平台实例分组：get_group_list / get_friend_list 一个实例只需一次
    by_inst: dict[str, list[tuple[str, int]]] = {}
    for dirname in plugin.state.sessions():
        umo, spec = _resolve_umo(plugin, dirname)
        if not spec:
            continue
        parts = umo.split(":")
        if len(parts) < 3 or parts[1] != "GroupMessage":
            continue
        try:
            gid = int(parts[2])
        except (TypeError, ValueError):
            out["skipped"].append(f"{umo}：群号不是数字，跳过")
            continue
        by_inst.setdefault(parts[0], []).append((umo, gid))

    gm_up: dict[str, str] = {}
    un_up: dict[str, str] = {}
    # `groups` 是"打算扫几个群"，与后面调用成功与否无关 ——
    # 适配器不给调 API 时也要让页面看到到底涉及多少会话。
    out["groups"] = sum(len(v) for v in by_inst.values())
    deadline = time.monotonic() + _API_DEADLINE

    def _can_call() -> bool:
        """还能不能再打一次平台 API（次数 + 总时间双重预算）。"""
        nonlocal budget
        if budget <= 0:
            out["budget_exhausted"] = True
            return False
        if time.monotonic() >= deadline:
            out["deadline_hit"] = True
            return False
        budget -= 1
        return True

    def _note_user(uid: Any, nm: Any) -> None:
        u, n = str(uid or "").strip(), str(nm or "").strip()
        # 平台把 QQ 号当昵称返回时等于没有昵称，别写进去
        if not u or not n or n == u or un is None:
            return
        cur = un.get(u)
        if cur == n:
            return
        # **默认只补缺**：群名片经常是玩笑话（「群主已和你达成情侣关系…」），
        # 拿它去覆盖已有的账号昵称只会让页面变难看。想强制按平台现状刷一遍
        # 就传 force=True。日常的"名字变了"由 live 采集在 on_any_message 里更新。
        if cur and not force:
            return
        un_up[u] = n

    def _name_of(info: Any) -> str:
        nm = str((info or {}).get("group_name") or "").strip()
        return "" if (not nm or nm.upper() == "N/A") else nm

    for inst_id, items in sorted(by_inst.items()):
        inst = None
        try:
            inst = get_inst(inst_id)
        except Exception as e:
            out["errors"].append(f"{inst_id}：取平台实例失败 {e!r}")
        client = None
        for getter in ("get_client", "bot"):
            obj = getattr(inst, getter, None)
            try:
                obj = obj() if callable(obj) and getter == "get_client" else obj
            except Exception:
                obj = None
            if callable(getattr(obj, "call_action", None)):
                client = obj
                break
        if client is None:
            out["skipped"].append(
                f"{inst_id}：该适配器不允许直接调平台 API，已跳过")
            continue
        call = client.call_action
        # 有些 OneBot 实现根本不提供这几个接口（会抛 `ApiNotAvailable`）。
        # 那是"这个平台办不到"，不是"出错了" —— 记一条 skipped 就短路掉
        # 本实例的其余调用，否则 15 个群会各报一次同样的失败，把页面刷满。
        unsupported = False
        # 有些 OneBot 实现根本不提供这几个接口，aiocqhttp 会抛 `ApiNotAvailable`。
        # 用**真类型**判定（不是比对类名字符串）；`core/*` 必须能脱离框架
        # import，所以惰性导入，拿不到就退化成一个普通错误。
        try:
            from aiocqhttp.exceptions import ApiNotAvailable as _ApiNA
        except Exception:
            _ApiNA = None

        async def _ask(action: str, **kw) -> Any:
            nonlocal unsupported
            if unsupported or not _can_call():
                return None
            try:
                return await asyncio.wait_for(call(action, **kw), _API_TIMEOUT)
            except Exception as e:
                if _ApiNA is not None and isinstance(e, _ApiNA):
                    unsupported = True
                    out["skipped"].append(
                        f"{inst_id}：该平台实例不支持 {action}"
                        f"（ApiNotAvailable），本实例其余调用已跳过")
                else:
                    out["errors"].append(
                        f"{inst_id}/{kw.get('group_id', '')}：{action} 失败 {e!r}")
                return None

        # ① 一次拿全部群名
        listed: dict[int, str] = {}
        if groups:
            for g in (await _ask("get_group_list") or []):
                if not isinstance(g, dict):
                    continue
                try:
                    gid = int(g.get("group_id"))
                except (TypeError, ValueError):
                    continue
                nm = _name_of(g)
                if nm:
                    listed[gid] = nm

        for umo, gid in items:
            need_name = bool(groups) and (force or not gm.get(umo))
            if need_name and listed.get(gid):
                gm_up[umo] = listed[gid]        # ① 已经拿到了，零额外请求
            elif need_name:
                # ② 群列表里没有（权限/分页）→ 单独问一次
                nm = _name_of(await _ask("get_group_info", group_id=gid))
                if nm:
                    gm_up[umo] = nm
            if members:
                # ③ 成员昵称只能逐群取
                ms = await _ask("get_group_member_list", group_id=gid)
                for m in (ms or []):
                    if not isinstance(m, dict):
                        continue
                    # 与框架适配器一致：群名片优先，其次账号昵称
                    _note_user(m.get("user_id"),
                               m.get("card") or m.get("nickname"))
        if friends:
            # ④ 好友备注/昵称：补私聊会话
            for f in (await _ask("get_friend_list") or []):
                if not isinstance(f, dict):
                    continue
                _note_user(f.get("user_id"),
                           f.get("remark") or f.get("nickname"))

    if gm_up and gm is not None:
        out["group_names"] = gm.update_many(gm_up)
    if un_up and un is not None:
        out["users"] = un.update_many(un_up)
    return out


def build_state(plugin: Any, umo: str, persona: str, uid: str = "") -> dict:
    m = plugin.state.load(umo, persona, uid)
    d = m.to_dict()
    d.pop("conversation_log", None)
    return {"state": d, "snapshot": m.snapshot(),
            "log_count": len(m.conversation_log),
            "log": list(m.conversation_log)[-20:],
            "inject": m.inject,
            "tier": plugin.privilege.tier(uid, scope=umo) if uid else "intimate"}


def set_state(plugin: Any, umo: str, persona: str, uid: str,
              values: dict) -> dict:
    m = plugin.state.load(umo, persona, uid)
    for k in ("affection", "lewdness", "depravity", "emotion"):
        if k in values and values[k] is not None:
            try:
                v = int(values[k])
            except (TypeError, ValueError):
                continue
            if k == "emotion":
                m.emotion = max(0, min(100, v))
            else:
                setattr(m, k, max(0, min(100, v)))
    if "stutter_done" in values:
        m.stutter_done = bool(values["stutter_done"])
    plugin.state.save(umo, persona, uid, m)
    return build_state(plugin, umo, persona, uid)


def clear_log(plugin: Any, umo: str, persona: str, uid: str = "") -> dict:
    m = plugin.state.load(umo, persona, uid)
    n = len(m.conversation_log)
    m.conversation_log = []
    m.emotion = 100
    m.stutter_done = False
    plugin.state.save(umo, persona, uid, m)
    return {"cleared": n}


def set_inject(plugin: Any, umo: str, persona: str, uid: str,
               text: str) -> dict:
    plugin.state.set_inject(umo, persona, uid, text or "")
    return {"inject": text or ""}


# ---------------------------------------------------------------- 印象

def build_impression(plugin: Any, uid: str) -> dict:
    return {"uid": uid, "impression": plugin.state.load_impression(uid),
            "effective": plugin.state.impression_text(uid)}


def set_impression(plugin: Any, uid: str, *, sfw: str | None = None,
                   nsfw: str | None = None, sfw_custom: str | None = None,
                   nsfw_custom: str | None = None,
                   reset_count: bool = False) -> dict:
    imp = plugin.state.load_impression(uid)
    for k in ("sfw", "nsfw", "sfw_custom", "nsfw_custom"):
        imp.setdefault(k, "")
    imp.setdefault("updated", 0)
    imp.setdefault("msg_count", 0)
    for k, v in (("sfw", sfw), ("nsfw", nsfw), ("sfw_custom", sfw_custom),
                 ("nsfw_custom", nsfw_custom)):
        if v is not None:
            imp[k] = v
    if reset_count:
        imp["msg_count"] = 0
    plugin.state.save_impression(uid, imp)
    return build_impression(plugin, uid)


# ---------------------------------------------------------------- 总览

def build_stats(plugin: Any, inst: str = "") -> dict:
    """运行统计 —— **按实例**。

    实例化之后每个子对象（注意力/判定/分段/TTS/调度器/记忆库）都是各实例
    一套，所以这里必须先切上下文再取值，否则读到的永远是默认实例。
    """
    from . import world as world_mod
    i = inst or getattr(plugin, "_default_inst", "")
    tok = plugin.use_inst(i)
    try:
        mem = {}
        try:
            mem = plugin.memory.stats()
        except Exception:
            pass
        seg = {}
        try:
            seg = plugin.segmenter.report()
        except Exception:
            pass
        stream = {}
        try:
            stream = dict(plugin._stream_stats)
        except Exception:
            pass
        grp = {}
        try:
            allg = plugin.groups.all_groups()
            mine = [m for m in allg.values()
                    if (m.get("instance") or "") in ("", i)]
            grp = {"groups": len(mine),
                   "members": sum(len(m.get("members") or []) for m in mine),
                   "path": str(getattr(plugin.groups, "path", "")),
                   "total_groups": len(allg)}
        except Exception:
            pass
        out = {
            "plugin": dict(plugin._stats),
            "last_error": getattr(plugin, "_last_error", ""),
            "personas": len(plugin.personas),
            "worlds": world_mod.stats().__dict__,
            "attention": plugin.attention.stats(),
            "judge": plugin.judge.report(),
            "impressions": plugin.impressions.report(),
            "memory": mem,
            "proactive": plugin.scheduler.report(),
            "poke": plugin.poke.report(),
            "merge": plugin.merge.report(),
            "tts": plugin.tts.report(),
            "templates": plugin.templates.stats(),
            "segment": seg,
            "stream": stream,
            "groups": grp,
            "features": build_features(plugin),
            "inst": i,
        }
        try:
            out["instances"] = [m["id"] for m in plugin._inst_store.discover()]
        except Exception:
            out["instances"] = [i] if i else []
        return out
    finally:
        plugin.end_inst(tok)


# ---------------------------------------------------------------- 功能开关

def _flag(plugin: Any, key: str, default: bool) -> bool:
    try:
        return bool(plugin._cfg(key, default))
    except Exception:
        return bool(default)


def _obj_flag(obj: Any, default: bool = False) -> bool:
    """取子系统对象自报的 `enabled`（实现里真正开门的那一个值）。"""
    try:
        return bool(getattr(obj, "enabled", default))
    except Exception:
        return bool(default)


def _stream_detail(plugin: Any) -> str:
    """流式传输那一行的补充说明：开了之后**谁在发**、已经发了多少。

    这一行最容易误解成"开着就有流式" —— 实际还要看平台给不给流式结果
    （`turn_off` 策略下框架会整段交付）、以及 `send_streaming` 有没有被
    成功接管（没接管就退回框架自己的硬切发送）。所以把退回次数也报出来。
    """
    try:
        st = dict(plugin._stream_stats)
    except Exception:
        st = {}
    turns = int(st.get("turns", 0) or 0)
    parts = ([f"{turns} 轮 / {int(st.get('segments', 0) or 0)} 段"]
             if turns else ["尚无流式轮次"])
    if st.get("last_turn_ms"):
        parts.append(f"上轮 {int(st['last_turn_ms'])}ms")
    if _flag(plugin, "enable_streaming", False):
        parts.append("分段子代理已禁用")
    if int(st.get("degraded", 0) or 0):
        parts.append(f"{int(st['degraded'])} 次退回框架发送")
    if st.get("last_error"):
        parts.append(f"最近错误 {str(st['last_error'])[:60]}")
    return " · ".join(parts)


def _proactive_detail(plugin: Any) -> str:
    """主动消息那一行的补充说明：放行 / 循环 / 已开启会话数 分开讲。

    只说"启用/关闭"是不够的：全局开关是**放行**，它开着也不代表会有会话开口
    —— 会话是 opt-in 的。用户最需要一眼看到的是"到底有几个会话开着"，
    以及"全局没放行时这些开启是不是白配了"。
    """
    s = plugin.scheduler
    try:
        ovs = list(s.state.overrides.values())
    except Exception:
        ovs = []
    on = sum(1 for ov in ovs if ov.get("enabled") is True)
    off = sum(1 for ov in ovs if ov.get("enabled") is False)
    parts = ["循环运行中" if s.running else "循环未启动"]
    parts.append(f"{on} 个会话已开启" if on else "尚无会话开启")
    if off:
        parts.append(f"{off} 个会话已禁用")
    if not s.enabled and on:
        parts.append("全局未放行，已开启的会话也不生效")
    return " · ".join(parts)


def build_features(plugin: Any) -> list[dict]:
    """总览页「子系统状态」的**功能开关**列数据源。

    为什么必须单独建一张表：这一列以前的写法是"顺手从 /stats 里挑一个字段
    当布尔用"，挑到了**计数**就去显示是否启用 ——
    `用户印象` 读 `updated`、`长期记忆` 读 `atoms`、`知识图谱` 读 `nodes`、
    `注意力插话` 读 `sessions`。于是功能明明开着，只要还没有产出数据
    （或插件刚重载、进程内计数归零）就显示"关闭"；`语音合成` 更离谱，
    读的是 provider 可用性而不是 `enable_tts`。

    这里的每一条都对齐**实现里真正用来开门的那一个值**：
      * 子系统对象自报的 `enabled`（poke/merge/tts/memory/scheduler）；
      * 其余没有对象属性可读的，读它对应的配置键（与主流程里的闸门同源）；
      * `主动消息` 额外带出循环状态与"几个会话已开启" —— 全局开关只是**放行**，
        会话级才是 opt-in 的实际开关，只报全局会让页面看起来"开了却没有会话"。
    """
    rows: list[dict] = []

    def add(name: str, enabled: Any, gate: str, detail: str = "") -> None:
        rows.append({"name": name, "enabled": bool(enabled),
                     "gate": gate, "detail": detail})

    # ① 各子系统对象自报的门（最贴近实现）
    try:
        add("主动消息", _obj_flag(plugin.scheduler),
            "enable_proactive",
            _proactive_detail(plugin))
    except Exception:
        add("主动消息", False, "enable_proactive", "（读取失败）")
    add("戳一戳", _obj_flag(plugin.poke), "poke_enabled")
    add("连发合并", _obj_flag(plugin.merge), "merge_pending_messages")
    add("语音合成", _obj_flag(plugin.tts), "enable_tts",
        "provider 可用" if bool(getattr(plugin.tts, "available", False))
        else "provider 不可用")
    try:
        add("长期记忆", _obj_flag(plugin.memory), "enable_memory")
    except Exception:
        add("长期记忆", False, "enable_memory", "（读取失败）")
    # ② 闸门在配置里的，读与之同源的键
    add("用户印象", _flag(plugin, "enable_impression", True),
        "enable_impression")
    add("注意力插话", _flag(plugin, "enable_attention", True),
        "enable_attention")
    add("不回复判定", _flag(plugin, "enable_no_reply_judge", True),
        "enable_no_reply_judge")
    add("撤回取消", _flag(plugin, "enable_recall_cancel", True),
        "enable_recall_cancel")
    add("消息分段", _flag(plugin, "enable_segmentation", True),
        "enable_segmentation")
    add("流式传输", _flag(plugin, "enable_streaming", False),
        "enable_streaming", _stream_detail(plugin))
    add("多人格同台", _flag(plugin, "costar_enabled", True), "costar_enabled",
        "仅剧情模式下生效")
    # 知识图谱：挂在长期记忆下面，两个都开才算开
    add("知识图谱",
        _obj_flag(plugin.memory) and _flag(plugin, "enable_graph_retrieval", True),
        "enable_memory + enable_graph_retrieval")
    # ③ 没有开关的子系统：常开，写清楚免得被当成"没配"
    add("事件模板", True, "（无开关，常开）")
    add("记忆组", True, "（无开关，常开）")
    return rows


def build_preview(plugin: Any, persona: str, *, is_private: bool = False,
                  msg_mode: int = 1, nsfw: bool | None = None) -> dict:
    """装配预览。`nsfw` 缺省取全局 `allow_erotic_content`，与实际注入保持一致。"""
    doc = plugin.personas.resolve(persona) if persona else None
    if doc is None:
        docs = plugin.personas.all()
        doc = docs[0] if docs else None
    if doc is None:
        return {"error": "无可用人格"}
    if nsfw is None:
        nsfw = _flag(plugin, "allow_erotic_content", True)
    from .persona import authoring as A
    p = A.preview(doc, is_private=is_private, msg_mode=msg_mode, nsfw=bool(nsfw))
    p.pop("legacy_text", None)
    p["nsfw"] = bool(nsfw)
    return p


def build_doctor(plugin: Any) -> dict:
    from .persona import doctor as doctor_mod
    return doctor_mod.run().to_dict()


def reload_all(plugin: Any) -> dict:
    plugin.personas.reload()
    plugin.state.invalidate()
    return {"personas": len(plugin.personas)}


# ---------------------------------------------------------------- 注意力

def build_attention(plugin: Any) -> dict:
    """所有会话的概率拆解 —— 用来回答"为什么它这会儿不接话"。"""
    try:
        rows = plugin.attention.snapshot()
    except Exception as e:
        return {"error": repr(e), "rows": []}
    return {
        "rows": rows,
        "config": plugin.attention.stats(),
        "judge": plugin.judge.report(),
    }


def reset_attention(plugin: Any, umo: str) -> dict:
    plugin.attention.reset(umo)
    return {"reset": umo}


# ---------------------------------------------------------------- 记忆

def _mem_key(plugin: Any, umo: str) -> str:
    """会话 → 记忆组键（没归组时就是 umo 本身）。"""
    try:
        return plugin.groups.group_of(umo)
    except Exception:
        return umo


def build_memory(plugin: Any, umo: str = "") -> dict:
    key = _mem_key(plugin, umo) if umo else ""
    atoms = []
    pending = 0
    if key:
        try:
            for a in plugin.memory.store.load(key):
                atoms.append({
                    "id": a.id, "kind": a.kind, "ts": a.ts,
                    "hits": a.hits, "importance": a.importance,
                    "has_vec": bool(a.vec),
                    "text": a.text,
                    "topics": (a.metadata or {}).get("topics") or [],
                    "facts": (a.metadata or {}).get("key_facts") or [],
                    "participants": (a.metadata or {}).get("participants") or [],
                    "degraded": bool((a.metadata or {}).get("degraded")),
                })
        except Exception as e:
            return {"error": repr(e)}
        try:
            pending = plugin.memory.pending_size(key)
        except Exception:
            pending = 0
    atoms.sort(key=lambda x: -(x["ts"] or 0))
    st = {}
    try:
        st = plugin.memory.stats()
    except Exception:
        pass
    return {"umo": umo, "key": key, "atoms": atoms, "pending": pending,
            "stats": st}


async def recall_debug(plugin: Any, umo: str, query: str,
                       persona: str = "", top_k: int = 8,
                       policy: int = 0) -> dict:
    """跑一次真实召回，把**两路各自的命中与融合明细**都摊开。

    这是排障"为什么它没想起来"的关键：能看到文档路几分、图谱路几分、
    是不是被 min_score 挡掉了。

    `persona` + `policy`（0 关闭 / 1 本命优先降权 / 2 严格隔离）用于预览
    「记忆的人格策略」实际效果，语义与配置项 `memory_persona_policy` 一致。
    """
    if not query.strip():
        return {"error": "query 为空"}
    key = _mem_key(plugin, umo)
    svc = plugin.memory
    if not svc.available:
        return {"error": "记忆不可用（embedding 未就绪或已关闭）",
                "doc_hits": [], "graph_hits": [], "fused": []}

    from .memory.retrieval import RouteHit
    from .memory.service import (PERSONA_OTHER_FACTOR, PERSONA_POLICY_PREFER,
                                 PERSONA_POLICY_STRICT, SUPERSEDED_FACTOR)
    from .memory.retrieval import asks_about_past

    min_score = float(svc._cfg("memory_min_score", 0.35) or 0.35)
    half_life = float(svc._cfg("memory_decay_days", 30.0) or 30.0)
    # 打分的三个可选项必须和线上召回一致，否则这个调试页会"说谎"
    kind_decay = bool(svc._cfg("memory_kind_decay", True))
    sup_factor = (1.0 if asks_about_past(query) else SUPERSEDED_FACTOR)

    policy = int(policy or 0)
    strict = policy >= PERSONA_POLICY_STRICT
    other_factor = (PERSONA_OTHER_FACTOR
                    if policy == PERSONA_POLICY_PREFER else 1.0)
    prefer = persona if policy >= PERSONA_POLICY_PREFER else ""
    policy_labels = {0: "关闭（共享）", 1: "本命优先（降权）", 2: "严格隔离"}

    doc_hits_raw = []
    qv = await svc.embedder.embed(query)
    if qv:
        for atom, score in svc.store.recall(key, qv, top_k=max(top_k * 2, top_k),
                                            min_score=0.0,
                                            half_life_days=half_life,
                                            persona=persona or None, uid=None,
                                            prefer_persona=prefer,
                                            other_factor=other_factor,
                                            strict_persona=strict,
                                            kind_decay=kind_decay,
                                            superseded_factor=sup_factor):
            doc_hits_raw.append((atom, score))
    graph_hits_raw = []
    if bool(svc._cfg("enable_graph_retrieval", True)):
        try:
            graph_hits_raw = await svc.graph_retriever.search(
                query, max(top_k * 2, top_k), session_id=key or None,
                persona_id=persona or None, prefer_persona=prefer or None,
                other_factor=other_factor, strict_persona=strict)
        except Exception:
            graph_hits_raw = []

    doc_hits = [RouteHit(key=str((a.metadata or {}).get("source_memory_id") or a.id), score=s, content=a.text,
                         metadata=dict(a.metadata or {}))
                for a, s in doc_hits_raw]
    graph_hits = [RouteHit(key=str(r.doc_id or f"g:{r.entry_key}"), score=r.final_score,
                           content=r.content, source_memory_id=r.doc_id,
                           metadata=dict(r.metadata))
                  for r in graph_hits_raw]

    fused = svc.dual.fuse(query, doc_hits, graph_hits, k=top_k,
                          diversify=bool(svc._cfg("memory_mmr", True)))
    from .memory.retrieval import route_weights
    dw, gw, intent = route_weights(
        query, doc_w=float(svc._cfg("document_route_weight", 0.65) or 0.65),
        graph_w=float(svc._cfg("graph_route_weight", 0.35) or 0.35))

    return {
        "key": key, "query": query, "intent": intent,
        "weights": {"document": round(dw, 3), "graph": round(gw, 3)},
        "min_score": min_score,
        "policy": policy, "policy_label": policy_labels.get(policy, str(policy)),
        "doc_hits": [{"id": a.id, "score": round(s, 4),
                      "passed_min_score": s >= min_score,
                      "text": a.text[:400]} for a, s in doc_hits_raw[:top_k]],
        "graph_hits": [{"key": r.entry_key,
                        "score": round(r.final_score, 4),
                        "breakdown": r.score_breakdown,
                        "text": r.content[:400]} for r in graph_hits_raw[:top_k]],
        "fused": [{"key": h.key, "final": round(h.final_score, 4),
                   "doc": round(h.doc_score, 4), "graph": round(h.graph_score, 4),
                   "text": h.content[:400]} for h in fused],
    }


def forget_memory(plugin: Any, umo: str) -> dict:
    key = _mem_key(plugin, umo)
    r = plugin.memory.forget(key)
    return {"key": key, **r}


def forget_memory_atoms(plugin: Any, umo: str, atom_ids: list) -> dict:
    """按 id 删除某会话的若干条记忆（WebUI 勾选删除）。

    与 `forget_memory`（清空整个记忆键）不同：只删勾中的那些，
    并逐条连带清掉其派生的图谱条目。
    """
    key = _mem_key(plugin, umo)
    ids = [str(i).strip() for i in (atom_ids or []) if str(i or "").strip()]
    if not ids:
        return {"key": key, "deleted": 0, "graph_dropped": 0, "requested": 0}
    deleted = dropped = 0
    for aid in ids:
        try:
            r = plugin.memory.forget_atom(key, aid)
        except Exception:
            continue
        deleted += int(r.get("deleted") or 0)
        dropped += int(r.get("graph_dropped") or 0)
    return {"key": key, "deleted": deleted, "graph_dropped": dropped,
            "requested": len(ids)}


async def edit_memory_atom(plugin: Any, umo: str, atom_id: str,
                           text: str) -> dict:
    """改写单条记忆的正文（WebUI「编辑」）。

    向量与图谱条目由 `MemoryService.edit_atom` 一起重建 —— 这里只做参数校验。
    失败用 `{"error": ...}` 返回（不能占 `ok` 这个键：bridge 用 `ok` 判成败，
    放进去会被当成"接口出错"直接抛异常）。
    """
    key = _mem_key(plugin, umo)
    aid = str(atom_id or "").strip()
    if not aid:
        return {"key": key, "error": "missing_id"}
    got = await plugin.memory.edit_atom(key, aid, text)
    return {"key": key, **got}


# ---------------------------------------------------------------- 图谱

def build_graph(plugin: Any, limit: int = 160, persona: str = "",
                session: str = "") -> dict:
    """给可视化用的子图：按度数取前 N 个节点，只保留它们之间的边。

    `persona` / `session` 非空时只统计该人格 / 该会话贡献的条目
    （图谱条目带 `persona_id` 与 `session_id`，见 core/graph/model.py）。
    """
    g = plugin.memory.graph
    try:
        stats = g.stats()
    except Exception as e:
        return {"error": repr(e), "nodes": [], "edges": []}

    entries = list(g.entries.values())
    pcount: dict[str, int] = {}
    scount: dict[str, int] = {}
    for e in entries:
        pid = getattr(e, "persona_id", "") or ""
        sid = getattr(e, "session_id", "") or ""
        pcount[pid] = pcount.get(pid, 0) + 1
        scount[sid] = scount.get(sid, 0) + 1

    allowed_keys: set[str] | None = None
    if persona or session:
        allowed_keys = set()
        for e in entries:
            if persona and (getattr(e, "persona_id", "") or "") != persona:
                continue
            if session and (getattr(e, "session_id", "") or "") != session:
                continue
            allowed_keys.update(getattr(e, "node_keys", []) or [])
        if not allowed_keys:
            return {"nodes": [], "edges": [], "stats": stats,
                    "truncated": False, "total_nodes": 0,
                    "personas": _graph_personas(plugin, pcount),
                    "sessions": _graph_sessions(plugin, scount),
                    "filtered": True}

    deg: dict[str, int] = {}
    for e in g.edges.values():
        if e.status != "active":
            continue
        if allowed_keys is not None and (
                e.source_key not in allowed_keys
                or e.target_key not in allowed_keys):
            continue
        deg[e.source_key] = deg.get(e.source_key, 0) + 1
        deg[e.target_key] = deg.get(e.target_key, 0) + 1

    # 没有被边连上的节点（例如只有 fact，没抽到关系）也要能看到
    for k in g.nodes:
        if allowed_keys is not None and k not in allowed_keys:
            continue
        deg.setdefault(k, 0)

    top = sorted(deg.items(), key=lambda kv: -kv[1])[:max(1, limit)]
    keep = {k for k, _ in top}

    nodes = []
    for k in keep:
        n = g.nodes.get(k)
        if n is None:
            continue
        nodes.append({
            "id": k, "type": n.node_type, "value": n.value,
            "degree": deg.get(k, 0),
            "meta": n.metadata if isinstance(n.metadata, dict) else {},
        })

    edges = []
    for e in g.edges.values():
        if e.status != "active":
            continue
        if e.source_key in keep and e.target_key in keep:
            edges.append({"source": e.source_key, "target": e.target_key,
                          "relation": e.relation_type,
                          "confidence": e.confidence,
                          "weight": e.weight})
    truncated = len(deg) > len(keep)
    return {"nodes": nodes, "edges": edges, "stats": stats,
            "truncated": truncated, "total_nodes": len(deg),
            "personas": _graph_personas(plugin, pcount),
            "sessions": _graph_sessions(plugin, scount),
            "persona": persona, "session": session}


def _graph_personas(plugin: Any, counts: dict[str, int]) -> list[dict]:
    """图谱里出现的人格（带条目数）。空 persona_id 记为「未标注」。"""
    out = []
    for pid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if not pid:
            out.append({"id": "", "name": "（未标注）", "count": n})
            continue
        d = plugin.personas.get(pid)
        out.append({"id": pid,
                    "name": d.display_name if d else f"{pid}（已删除）",
                    "count": n})
    return out


def _graph_sessions(plugin: Any, counts: dict[str, int]) -> list[dict]:
    out = []
    for sid, n in sorted(counts.items(), key=lambda kv: -kv[1])[:200]:
        out.append({"umo": sid, "count": n})
    return out


def build_graph_node(plugin: Any, key: str) -> dict:
    g = plugin.memory.graph
    n = g.nodes.get(key)
    if n is None:
        return {"error": f"找不到节点 {key}"}
    nb = g.neighbors(key, limit=40)
    ents = g.entries_for_node(key, limit=20)
    hood = g.expand([key], hops=1, limit=40)
    return {
        "node": {"id": key, "type": n.node_type, "value": n.value,
                 "meta": n.metadata if isinstance(n.metadata, dict) else {}},
        "neighbors": [{"key": k, "relation": r, "confidence": c,
                       "value": (g.nodes.get(k).value if g.nodes.get(k) else k),
                       "type": (g.nodes.get(k).node_type
                                if g.nodes.get(k) else "")}
                      for k, r, c in nb],
        "entries": [{"type": e.entry_type, "relation": e.relation_type,
                     "content": e.content, "memory_id": e.source_memory_id,
                     "session": e.session_id} for e in ents],
        "neighborhood_size": len(hood),
    }


def graph_search(plugin: Any, q: str, k: int = 10, session: str = "") -> dict:
    g = plugin.memory.graph
    hits = g.bm25_search(q, k, session_id=session or None)
    return {"query": q, "hits": [
        {"key": e.entry_key, "score": round(s, 4), "type": e.entry_type,
         "content": e.content, "nodes": list(e.node_keys),
         "session": e.session_id} for e, s in hits]}


# ---------------------------------------------------------------- 交互/调度

def build_interactions(plugin: Any) -> dict:
    out = {}
    for name, obj, meth in (
        ("poke", "poke", "report"), ("merge", "merge", "report"),
        ("tts", "tts", "report"),
        ("segment", "segmenter", "report"),
        ("templates", "templates", "stats"),
    ):
        try:
            out[name] = getattr(getattr(plugin, obj), meth)()
        except Exception as e:
            out[name] = {"error": repr(e)}
    # 撤回取消：`RecallGuard` 那一整类早就删了（它没有任何生产调用点），
    # 现在的实现是 `_active_triggers`（umo → 本轮触发消息 id）+ 框架的
    # `active_event_registry`。旧代码还照着已删除的属性取值，
    # 于是交互页永远显示一行 `AttributeError: ... has no attribute 'recall_guard'`。
    try:
        out["recall"] = {
            "enabled": bool(plugin._cfg("enable_recall_cancel", True)),
            "active_triggers": len(getattr(plugin, "_active_triggers", {}) or {}),
            "cancelled": int(getattr(plugin, "_stats", {}).get(
                "recalls_cancelled", 0) or 0),
        }
    except Exception as e:
        out["recall"] = {"error": repr(e)}
    try:
        out["config"] = {
            "enable_markers": plugin._cfg("enable_markers", True),
            "enable_quote": plugin._cfg("enable_quote", True),
            "enable_segmentation": plugin._cfg("enable_segmentation", True),
            "enable_streaming": plugin._cfg("enable_streaming", False),
            "enable_recall_cancel": plugin._cfg("enable_recall_cancel", True),
            "merge_pending_messages": plugin._cfg("merge_pending_messages", True),
            "poke_enabled": plugin._cfg("poke_enabled", True),
            "enable_tts": plugin._cfg("enable_tts", False),
        }
    except Exception:
        out["config"] = {}
    return out


def build_proactive(plugin: Any) -> dict:
    s = plugin.scheduler
    out = {}
    # ProactiveScheduler 只有 report / candidates / should_emit，
    # **没有** activity() —— 旧写法让页面永远显示一个 AttributeError 对象。
    for n in ("report", "candidates"):
        try:
            out[n] = getattr(s, n)()
        except Exception as e:
            out[n] = {"error": repr(e)}
    try:
        st = getattr(s, "state", None)
        sess = dict(getattr(st, "sessions", {}) or {})
        # 值是 SessionActivity dataclass —— 必须摊平成 dict，
        # 否则 JSON 序列化层会炸。且只保留**真会话**的键：
        #   * 形如 `umo1` / `ready` 的假键（旧版测试写进来的）不是 UMO；
        #   * 测试脚本打进来的（`webchat:FriendMessage:webchat!astrbot!
        #     newapi-balance-e2e-*`）与被删掉的旧会话**语法上是合法 UMO**，
        #     但没有状态文件 —— 判定交给调度器的 `is_known`（与出话闸门
        #     同一判据，保证"页面上看得到"与"真的会开口"永远一致）。
        act = {}
        stale: list[str] = []
        for u, a in sess.items():
            rec = {
                "umo": getattr(a, "umo", u),
                "last_user_ts": getattr(a, "last_user_ts", 0.0),
                "last_proactive_ts": getattr(a, "last_proactive_ts", 0.0),
                "persona": getattr(a, "persona", ""),
                "failures": getattr(a, "failures", 0),
                # 自最近一条用户消息以来连发了几条主动消息。页面据此显示
                # 「静默连发 2/2」—— 群里死活没人说话时，这就是它闭嘴的原因。
                "silent_streak": getattr(a, "silent_streak", 0),
            }
            known = True
            try:
                known = bool(s.is_known(u))
            except Exception:
                known = True
            if is_spec_umo(u) and known:
                act[str(u)] = rec
            else:
                stale.append(str(u))
        out["activity"] = act
        if stale:
            out["stale_proactive"] = stale
    except Exception as e:
        out["activity"] = {"error": repr(e)}
    try:
        out["config"] = {
            "quiet": plugin._cfg("proactive_quiet_hours", ""),
            "min_interval": plugin._cfg("proactive_min_interval", 45),
            "idle_minutes": plugin._cfg("proactive_idle_minutes", 90),
            "silent_max": plugin._cfg("proactive_silent_max", 2),
            "enabled": plugin._cfg("enable_proactive", True),
        }
    except Exception:
        out["config"] = {}
    # 全局总闸的实际状态：`global` 是配置值，`running` 是循环真的在跑。
    # 页面上两个都要摆出来 —— 只报一个就会重演"配置说关、它却在发"。
    try:
        rep = out.get("report") if isinstance(out.get("report"), dict) else {}
        out["effective"] = {
            "global": bool(getattr(s, "enabled", False)),
            "running": bool(getattr(s, "running", False)),
            "sessions_on": int(rep.get("sessions_on", 0) or 0),
            "sessions_off": int(rep.get("sessions_off", 0) or 0),
        }
    except Exception as e:
        out["effective"] = {"error": repr(e)}
    try:
        out["overrides"] = s.overrides()
    except Exception as e:
        out["overrides"] = {"error": repr(e)}
    return out


def set_proactive_override(plugin: Any, umo: str, values: dict) -> dict:
    """设置某会话的主动消息覆盖（空参数则视为清除该会话覆盖）。

    会话级 `enabled` 才是"这个会话要不要主动开口"的实际开关，而且
    **全局不放行时它一律不作数**：`enable_proactive=false` 时，这里写
    `enabled: true` 只会被存下来、不会出话（见 `proactive.should_emit`）。
    所以返回值里带上 `effective`，页面据此提示用户。
    """
    ov = plugin.scheduler.set_override(umo, values or {})
    # 已放行但循环因故没起来（例如刚被放行）时补一次；没放行时
    # ensure_started() 会直接返回 False，不会越权启动。
    try:
        plugin.scheduler.ensure_started()
    except Exception:
        pass
    return {"overrides": ov, **build_proactive(plugin)}


def clear_proactive_override(plugin: Any, umo: str) -> dict:
    plugin.scheduler.clear_override(umo)
    return {"overrides": plugin.scheduler.overrides(),
            **build_proactive(plugin)}


# ---------------------------------------------------------------- 会话发言参数

def _session_params_store(plugin: Any):
    return getattr(plugin, "_session_params", None)


def _is_private_umo(umo: str) -> bool:
    """按 UMO 第二段判定会话类型（FriendMessage = 私聊）。"""
    try:
        return str(umo).split(":", 2)[1] == "FriendMessage"
    except Exception:
        return False


def build_session_params(plugin: Any) -> dict:
    """返回规则定义 + 每个规格化会话的覆盖与解析结果（驱动「会话设置」页签）。"""
    from .session_params import (MODE_LABELS, RULE_IDS, RULE_LABELS,
                                 describe)
    sp = _session_params_store(plugin)
    ov_all = {}
    try:
        if sp is not None:
            ov_all = dict(sp.as_dict())
    except Exception:
        ov_all = {}
    try:
        global_mode = int(plugin._cfg("private_msg_mode", 1) or 1)
    except Exception:
        global_mode = 1
    try:
        sessions = [s["umo"] for s in build_sessions(plugin)["sessions"]]
    except Exception:
        sessions = []
    rows = []
    for u in sessions:
        priv = _is_private_umo(u)
        try:
            r = describe(priv, ov_all.get(u), global_mode=global_mode)
        except Exception:
            r = {"mode": 1, "nsfw": True, "mode_label": "",
                 "inherited": True}
        rows.append({"umo": u, "is_private": priv,
                     "override": ov_all.get(u) or None, "resolve": r})
    return {
        "modes": [{"id": m, "label": lbl} for m, lbl in
                  sorted(MODE_LABELS.items())],
        "rules": [{"id": rid, "label": RULE_LABELS.get(rid, rid)}
                  for rid in RULE_IDS],
        "rows": rows,
        "overrides": ov_all,
    }


def set_session_params(plugin: Any, umo: str, values: dict) -> dict:
    """写入某会话的发言参数覆盖（合法键合并；空覆盖 = 清除该会话）。"""
    from .session_params import normalize_override
    sp = _session_params_store(plugin)
    if sp is None:
        raise RuntimeError("会话参数存储未初始化")
    umo = str(umo)
    v = normalize_override(values or {})
    if not v:
        # 空/全非法参数 = 清除该会话覆盖，恢复类型默认
        sp.pop(umo, persist=False)
        sp.save()
        return build_session_params(plugin)
    cur = dict(sp.get(umo) or {})
    cur.update(v)
    sp.set(umo, cur, persist=False)
    sp.save()
    return build_session_params(plugin)


def clear_session_params(plugin: Any, umo: str) -> dict:
    sp = _session_params_store(plugin)
    if sp is not None:
        try:
            sp.pop(str(umo), persist=False)
        except Exception:
            pass
        try:
            sp.save()
        except Exception:
            pass
    return build_session_params(plugin)


# ---------------------------------------------------------------- 分会话特权

def _session_privileges_store(plugin: Any):
    """`state/_session_privileges.json` 的 JsonMap；缺失时返回 None。"""
    return getattr(plugin, "_session_privileges", None)


def _priv_scope_source(pv: Any, scope: str) -> str | None:
    """scope 命中的覆盖键（精确 UMO > 平台:类型 > 平台）；未命中返回 None。"""
    parts = [p for p in str(scope or "").split(":") if p]
    for i in range(len(parts), 0, -1):
        key = ":".join(parts[:i])
        if pv.per_session.get(key):
            return key
    return None


def build_session_privileges(plugin: Any) -> dict:
    """特权用户：**每个实例一名**（实例 config 的 `privilege_uid`）。

    旧的「全局名单 + 分会话前缀覆盖」已随实例化下线：实例边界本身就是
    那个前缀。这里返回的 rows 以实例 id 当 umo 字段，前端复用同一张表。
    """
    rows = []
    insts: list[str] = []
    try:
        insts = [m["id"] for m in plugin._inst_store.discover()]
    except Exception:
        insts = []
    for i in insts:
        uid = str(plugin._cfg("privilege_uid", "", inst=i) or "").strip()
        rows.append({
            "umo": i,
            "effective": [uid] if uid else [],
            "source": i,
        })
    return {"global": [], "overrides": {}, "rows": rows, "platforms": insts}


def set_session_privileges(plugin: Any, scope: str, qids: Any) -> dict:
    """把某实例的特权用户设为**第一名**（只允许一名）。"""
    inst = str(scope or "").strip().strip(":")
    if not inst:
        raise RuntimeError("scope 不能为空（传实例 id，如 BotA）")
    if isinstance(qids, (list, tuple)):
        uid = next((str(q).strip() for q in qids if str(q).strip()), "")
    else:
        uid = str(qids or "").strip()
    cfg = plugin.cfg_of(inst)
    cfg["privilege_uid"] = uid
    try:
        cfg.save_config()
    except Exception as e:
        raise RuntimeError(f"保存失败：{e!r}")
    return build_session_privileges(plugin)


def clear_session_privileges(plugin: Any, scope: str) -> dict:
    inst = str(scope or "").strip()
    if inst:
        try:
            cfg = plugin.cfg_of(inst)
            cfg["privilege_uid"] = ""
            cfg.save_config()
        except Exception:
            pass
    return build_session_privileges(plugin)


# ---------------------------------------------------------------- 记忆组

def build_groups(plugin: Any) -> dict:
    """人格组：组定义（含人格/副人格）+ 未归组会话 + 可选人格。"""
    g = plugin.groups
    groups = g.all_groups()
    # ungrouped 必须拿**真实 umo** 去比：groups 里存的是真实 umo，
    # 而 state.sessions() 给的是 safe 目录名，直接相减会让所有会话都
    # 被列成"未归组"（实测 18 个全中，真实只有 11 个未归组）。
    s2r = _safe_to_real(plugin)
    real_all = [s2r.get(d, d) for d in plugin.state.sessions()]
    grouped = {u for m in groups.values() for u in (m.get("members") or [])}
    try:
        personas = [{"id": d.id, "name": d.display_name}
                    for d in plugin.personas.all()]
    except Exception:
        personas = []
    insts: list[dict] = []
    try:
        for m in plugin._inst_store.discover():
            iid = m["id"]
            insts.append({"id": iid,
                          "enabled": bool(m.get("enabled", True)),
                          "grants": plugin._persona_pool.grants(iid)})
    except Exception:
        pass
    return {"groups": groups, "stats": g.stats(),
            "ungrouped": [u for u in real_all if u not in grouped],
            "personas": personas, "instances": insts}


def assign_group(plugin: Any, umos: Any, group: str) -> dict:
    items = list(umos) if isinstance(umos, (list, tuple)) else [umos]
    added = plugin.groups.assign_many(items, group)
    for u in added:
        try:
            plugin.apply_group_to_session(u)
        except Exception:
            pass
    return {"added": added, **build_groups(plugin)}


def remove_group(plugin: Any, umos: Any) -> dict:
    items = list(umos) if isinstance(umos, (list, tuple)) else [umos]
    removed = plugin.groups.remove_many(items)
    return {"removed": removed, **build_groups(plugin)}


def create_group(plugin: Any, name: str, persona: str = "",
                 instance: str = "", costars: Any = None) -> dict:
    name = str(name or "").strip()
    if not name:
        raise ValueError("组名不能为空")
    pid = str(persona or "").strip()
    if pid and pid not in plugin.personas:
        raise ValueError(f"未知人格：{pid}")
    plugin.groups.create(name, persona=pid, costars=costars,
                         instance=str(instance or "").strip())
    return {"group": name, **build_groups(plugin)}


def delete_group(plugin: Any, name: str) -> dict:
    plugin.groups.delete(str(name or "").strip())
    return build_groups(plugin)


def set_group_persona(plugin: Any, group: str, pid: str) -> dict:
    """给组指定人格，并把组内成员立刻对齐。"""
    group = str(group or "").strip()
    pid = str(pid or "").strip()
    if pid:
        if pid not in plugin.personas:
            raise ValueError(f"未知人格：{pid}")
        meta = plugin.groups.meta(group)
        inst = str(meta.get("instance") or "")
        pool = getattr(plugin, "_persona_pool", None)
        if pool is not None and inst and not pool.is_available(inst, pid):
            raise ValueError(f"人格 {pid} 未授权给实例 {inst}，请先在「人格分配」里授权")
    plugin.groups.set_persona(group, pid)
    for u in plugin.groups.members(group):
        try:
            plugin.apply_group_to_session(u)
        except Exception:
            pass
    return {"group": group, **build_groups(plugin)}


def set_group_costars(plugin: Any, group: str, pids: Any) -> dict:
    group = str(group or "").strip()
    clean = [str(x).strip() for x in (pids or []) if str(x).strip()]
    for p in clean:
        if p not in plugin.personas:
            raise ValueError(f"未知人格：{p}")
    plugin.groups.set_costars(group, clean)
    for u in plugin.groups.members(group):
        try:
            plugin.apply_group_to_session(u)
        except Exception:
            pass
    return {"group": group, **build_groups(plugin)}


def rename_group(plugin: Any, old: str, new: str) -> dict:
    """重命名人格组（保留全部成员；目标组已存在时等价于并入）。"""
    old, new = str(old).strip(), str(new).strip()
    if not old or not new:
        raise ValueError("组名不能为空")
    if old != new:
        new = plugin.groups.rename(old, new)
    return {"group": new, **build_groups(plugin)}


# ---------------------------------------------------------------- 多人格同台

def build_costars(plugin: Any) -> dict:
    """启用多人格同台的会话 + 登场人格（主 + 副）+ 是否生效。

    「生效」= 该会话当前是剧情模式（mode 1）**且**有副人格 —— 与主流程
    `on_llm_request` 的注入闸门同源（见 core/costars.py 与 main._costar_block）。
    """
    from .session_params import MODE_LABELS

    personas = getattr(plugin, "personas", None)
    rows: list[dict] = []
    for umo, pids in plugin.costars.all().items():
        is_private = ("FriendMessage" in umo or "PrivateMessage" in umo)
        try:
            mode, _nsfw = plugin._resolve_session_params(umo, is_private)
        except Exception:
            mode = 1 if is_private else 2
        try:
            doc = plugin._persona_for(umo)
        except Exception:
            doc = None
        cast = []
        for pid in pids:
            d = personas.get(pid) if personas is not None else None
            cast.append({"id": pid, "name": d.display_name if d else pid})
        rows.append({
            "umo": umo,
            "is_private": is_private,
            "mode": int(mode),
            "mode_label": MODE_LABELS.get(int(mode), str(mode)),
            "main_id": doc.id if doc else "",
            "main_name": doc.display_name if doc else "",
            "costars": cast,
            "effective": int(mode) == 1 and bool(cast),
        })
    rows.sort(key=lambda r: r["umo"])
    try:
        cap = int(plugin._costar_max())
    except Exception:
        cap = 3
    try:
        enabled = bool(plugin._cfg("costar_enabled", True))
    except Exception:
        enabled = True
    return {
        "rows": rows,
        "count": len(rows),
        "max": cap,
        "enabled": enabled,
        "personas": ([{"id": d.id, "name": d.display_name}
                      for d in personas.all()] if personas is not None else []),
    }


def remove_costar(plugin: Any, umo: str, pid: str) -> dict:
    """把一个副人格移出某会话（WebUI 卡片里的「撤下」按钮）。"""
    plugin.costars.remove(str(umo), str(pid))
    return build_costars(plugin)


# ---------------------------------------------------------------- 配置

def _load_schema() -> dict:
    """读取插件配置 schema（`_conf_schema.json`），供前端渲染表单。"""
    from . import paths
    p = paths.PLUGIN_DIR / "_conf_schema.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


DEFAULT_TPL = "__default__"


def build_config(plugin: Any, config: Any = None, inst: str = "") -> dict:
    """当前插件配置全量：schema（表单渲染用）+ current（当前值）。

    `inst == "__default__"` 时编辑的是**框架面板那份配置**——它只当模板：
    新建实例时复制它，改它不会影响任何已存在实例的 config.json。
    """
    schema = _load_schema()
    cur = {}
    try:
        if config is not None:
            cfg = config
        elif inst == DEFAULT_TPL:
            cfg = plugin.config
        else:
            cfg = plugin.cfg_of(inst)
        cur = dict(cfg)             # AstrBotConfig 是 dict 子类，直接拷贝
    except Exception:
        cur = {}
    # 不存在的键补 schema 默认值，便于页面稳定渲染
    for k, spec in schema.items():
        if k not in cur:
            cur[k] = spec.get("default")
    out = {"schema": schema, "current": cur}
    try:
        out["inst"] = inst or getattr(plugin, "_default_inst", "")
        out["instances"] = [DEFAULT_TPL] + [
            m["id"] for m in plugin._inst_store.discover()]
    except Exception:
        pass
    return out


def _coerce_value(v: Any, spec: dict) -> Any:
    """按 schema 把前端提交的值转换/夹紧成合法类型。"""
    t = str(spec.get("type") or "")
    if v is None:
        return None
    try:
        if t == "bool":
            out = v is True or str(v).strip().lower() in ("1", "true", "yes", "on")
        elif t == "int":
            out = int(float(v))
        elif t == "float":
            out = float(v)
        elif t == "list":
            if isinstance(v, list):
                out = [str(x).strip() for x in v if str(x).strip()]
            else:
                out = [x.strip() for x in str(v).replace("，", ",").split(",")
                       if x.strip()]
        else:                       # string / text / 其它
            out = str(v)
    except (TypeError, ValueError):
        raise ValueError(f"配置项 {spec.get('description', '')} 类型不合法")
    # 数值项夹紧到 schema 的 min/max
    if t in ("int", "float"):
        lo, hi = spec.get("min"), spec.get("max")
        try:
            if lo is not None:
                out = max(lo, out)
            if hi is not None:
                out = min(hi, out)
        except TypeError:
            pass
    return out


def set_config(plugin: Any, values: dict, inst: str = "") -> dict:
    """批量写入插件配置 → 落盘 → 热同步到运行中的子对象。

    `inst` 非空时写该实例的 config.json；否则写默认实例。
    与 `/persona global` 命令走同一条路径（`cfg[k]=v` + `save_config()`）。
    """
    schema = _load_schema()
    if not isinstance(values, dict):
        values = {}
    cfg = plugin.config if inst == DEFAULT_TPL else plugin.cfg_of(inst)
    applied: list[str] = []
    for k, v in values.items():
        spec = schema.get(k)
        if spec is None:
            continue                # 不认识的键直接忽略
        try:
            cfg[k] = _coerce_value(v, spec)
        except ValueError as e:
            raise ValueError(f"{k}: {e}")
        applied.append(k)
    try:
        cfg.save_config()
    except Exception as e:
        raise ValueError(f"配置保存失败（写入磁盘）：{e!r}")
    synced: list[str] = []
    try:
        r = plugin.sync_config()
        if isinstance(r, dict):
            synced = r.get("synced") or []
    except Exception as e:
        synced = [f"sync 失败: {e!r}"]
    return {"applied": applied, "synced": synced,
            **build_config(plugin, cfg, inst)}


# ---------------------------------------------------------------- 残留清理

def prune_stale(plugin: Any, *, sessions: list[str] | None = None,
                users: list[str] | None = None,
                proactive: list[str] | None = None) -> dict:
    """删除残留数据（管理页「清理残留」按钮调用，破坏性、不可撤销）。

    三类：
      sessions  —— 删除该会话的**全部**状态：state 目录、会话人格/注入覆盖
                   记录、记忆（仅当未归组；归组的会连同整组记忆被共享，
                   必须整体判断，这里跳过并提示）、主动消息活跃项、事件模板、
                   记忆组归属。
      users     —— 删除印象文件与昵称记录。
      proactive —— 仅删除主动消息状态里的脏键（如测试残留的 umo1/ready）。
    """
    import shutil
    from .state.store import safe_segment

    st = plugin.state
    sched = getattr(plugin, "scheduler", None)
    mem = getattr(plugin, "memory", None)
    grp = getattr(plugin, "groups", None)
    un = getattr(plugin, "_user_names", None)
    gn = getattr(plugin, "_group_names", None)
    tpl = getattr(plugin, "templates", None)

    removed_s: list[str] = []
    removed_u: list[str] = []
    removed_p: list[str] = []
    skipped: list[str] = []
    for umo in sessions or []:
        umo = str(umo)
        key = _mem_key(plugin, umo) if mem else umo
        # 归组的会话也能清：只清"它自己"的状态，**不动组共享记忆**
        #（组里还有别的成员，shared 记忆删了会连累他们）
        grouped = grp is not None and key != umo
        # 状态目录
        d = st.root / safe_segment(umo)
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        if ":" in umo:
            st.session_persona.pop(umo)
            st.session_initialized.discard(umo)
        # 注入覆盖：键是 `safe_umo|persona|uid`
        sfx = safe_segment(umo) + "|"
        for k in list(st.inject_overrides):
            if k.startswith(sfx):
                st.inject_overrides.pop(k)
        # 记忆（归组的跳过：那是整组共享的）
        if mem is not None and not grouped:
            try:
                mem.forget(key)
            except Exception:
                pass
        # 主动消息活跃项
        if sched is not None:
            try:
                if umo in sched.state.sessions:
                    sched.state.sessions.pop(umo, None)
            except Exception:
                pass
        # 事件模板
        if tpl is not None:
            try:
                tpl.pop(umo)
            except Exception:
                pass
        # 记忆组归属
        if grp is not None:
            try:
                grp.remove(umo)
            except Exception:
                pass
        # 群名映射
        if gn is not None:
            try:
                gn.pop(umo)
            except Exception:
                pass
        removed_s.append(umo)

    imp_dir = st.root / "_impressions"
    for uid in users or []:
        f = imp_dir / f"{safe_segment(uid)}.json"
        if f.is_file():
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass
        if un is not None:
            try:
                un.pop(uid)
            except Exception:
                pass
        removed_u.append(uid)

    if sched is not None:
        for u in proactive or []:
            # 走 forget() 而不是裸 pop：当日配额计数也要一起清，
            # 否则同名会话以后回来时会被一条幽灵配额挡在 daily-quota。
            try:
                if sched.forget(u):
                    removed_p.append(str(u))
            except Exception:
                if str(u) in sched.state.sessions:
                    sched.state.sessions.pop(str(u), None)
                    removed_p.append(str(u))
        try:
            sched.save(force=True)
        except Exception:
            pass
    # 状态缓存失效，后续读取从磁盘重建
    try:
        st.invalidate()
    except Exception:
        pass
    return {"sessions": removed_s, "users": removed_u,
            "proactive": removed_p, "skipped_linked": skipped}


# ---------------------------------------------------------------- 分段测试

async def segment_test(plugin: Any, text: str, is_private: bool = True,
                       umo: str = "", msg_mode: int = 1) -> dict:
    """直接跑一次分段子代理，看它到底怎么切、有没有被内容校验拦下。

    `force=True`：流式传输开着时子代理是整体停用的（`enabled_for` 返回假），
    但这是用户**显式点的试跑**，必须真跑一次 —— 否则页面永远显示"没结果"，
    用户会以为提示词坏了。
    """
    seg = plugin.segmenter
    out = await seg.segment(text, is_private=bool(is_private), umo=umo or "",
                            msg_mode=int(msg_mode or 1), force=True)
    from .output import (clean_segments, normalize_speaker_segments,
                         split_narrative, split_raw)
    local = []
    if int(msg_mode or 1) == 1:
        local = split_narrative(text)
    else:
        local = split_raw(
            text, max_len=int(plugin._cfg("segment_max_len", 120) or 120),
            min_len=int(plugin._cfg("segment_min_len", 8) or 8))
    # 与输出层同序：先角色名前缀归位（多人格同台），再标点清理
    try:
        names = plugin._speaker_names(umo or "", bool(is_private))
        if names:
            local = normalize_speaker_segments(local, names)
    except Exception:
        pass
    local = clean_segments(local)
    return {"input": text, "agent": out, "local_fallback": local,
            "used_agent": bool(out), "will_send": out or local,
            "stats": seg.report()}


# ---------------------------------------------------------------- 分段提示词

def build_segment_prompts(plugin: Any) -> dict:
    """四份分段提示词（私聊/群聊 × 纯信息/剧情向）+ 是否被自定义。

    `active=False` 表示**当前不生效**：流式传输开着时子代理整体停用
    （见 `segment_agent.enabled_for`），页面据此挂一条提示 —— 不然用户改了
    提示词、发现没反应，只会以为插件坏了。
    """
    try:
        return {"prompts": plugin.segmenter.prompts(),
                "active": not bool(plugin._cfg("enable_streaming", False)),
                "streaming": bool(plugin._cfg("enable_streaming", False))}
    except Exception as e:
        return {"error": repr(e)}


def set_segment_prompt(plugin: Any, key: str, text: str) -> dict:
    """写入某份提示词的覆盖（空文本 = 恢复默认）。"""
    from .segment_agent import DEFAULT_PROMPTS
    key = str(key or "").strip()
    if key not in DEFAULT_PROMPTS:
        raise RuntimeError(f"未知提示词键: {key}（可选 {list(DEFAULT_PROMPTS)}）")
    plugin.segmenter.set_prompt(key, text)
    return {"ok": True, **build_segment_prompts(plugin)}


def reset_segment_prompts(plugin: Any, key: str = "") -> dict:
    """恢复默认：`key` 为空时全部重置。"""
    plugin.segmenter.reset_prompt(str(key or "").strip() or None)
    return {"ok": True, **build_segment_prompts(plugin)}


# ---------------------------------------------------------------- 角色库
#
# 与聊天命令 `/character` 共用 `plugin.characters` / `plugin.char_matcher`：
# 页面改了立即生效，不需要重启（matcher 每次编辑后 reload）。

def build_characters(plugin: Any, inst: str = "") -> dict:
    """全部角色：本名/英文名/外号/简介/是否对应人格。"""
    try:
        plugin.seed_characters()
    except Exception:
        pass
    out = []
    for h in plugin.char_matcher.entries():
        out.append({
            "name": h.name, "en": h.en,
            "aliases": list(h.aliases),
            "brief": h.brief, "world": h.world,
            "persona_id": h.persona_id,
            "is_persona": bool(h.persona_id),
        })
    return {
        "characters": out,
        "count": len(out),
        "path": str(getattr(plugin.characters, "path", "")),
        "enabled": bool(plugin._cfg("char_mention_enabled", True)),
        "inst": inst or getattr(plugin, "_default_inst", ""),
        "instances": [m["id"] for m in plugin._inst_store.discover()],
    }


def set_character(plugin: Any, name: str, aliases: Any = None) -> dict:
    """新增/更新一个角色。`aliases` 传字符串（逗号/顿号分隔）或数组。"""
    name = str(name or "").strip()
    if not name:
        return {"ok": False, "message": "角色名不能为空"}
    if name not in plugin.characters.load():
        plugin.characters.add_character(name)
    if aliases is not None:
        raw = ("、".join(str(a) for a in aliases)
               if isinstance(aliases, (list, tuple)) else str(aliases))
        plugin.characters.set_aliases(name, raw)
    plugin.reload_characters()
    return {"ok": True, "message": f"已保存「{name}」",
            "aliases": plugin.characters.load().get(name) or []}


def delete_character(plugin: Any, name: str) -> dict:
    name = str(name or "").strip()
    ok = plugin.characters.delete_character(name)
    plugin.reload_characters()
    return {"ok": bool(ok),
            "message": f"已删除「{name}」及其外号" if ok else f"角色库中没有「{name}」"}


def match_characters(plugin: Any, text: str, persona: str = "") -> dict:
    """试跑一次匹配（排障/预览用）：返回命中名单与将要注入的正文。"""
    text = str(text or "")
    doc = plugin.personas.resolve(persona) if str(persona or "").strip() else None
    min_len = int(plugin._num(plugin._cfg("char_mention_min_len"), 2) or 2)
    try:
        names = plugin.char_matcher.match(text, min_len=min_len)
    except Exception as e:
        return {"text": text, "matched": [], "block": "", "error": repr(e)}
    return {
        "text": text,
        "matched": names,
        "block": plugin._character_block(text, doc),
        "min_len": min_len,
        "enabled": bool(plugin._cfg("char_mention_enabled", True)),
    }


# ---------------------------------------------------------------- 注册

def register(plugin: Any) -> list[str]:
    """注册全部 WebAPI，返回注册成功的路径列表。"""
    ctx = plugin.context
    reg = ctx.register_web_api
    P = API_PREFIX
    done: list[str] = []

    async def _wrap(fn):
        try:
            r = fn()
            if hasattr(r, "__await__"):
                r = await r
            return _ok(r)
        except Exception as e:
            return _err(repr(e))

    def add(path: str, handler, methods, desc: str) -> None:
        try:
            reg(f"{P}{path}", handler, methods, desc)
            done.append(f"{P}{path}")
        except Exception:
            pass

    # ---- 只读：参数一律用 _q/_qi/_qb 从查询串读 ----

    async def personas():
        return await _wrap(lambda: build_personas(plugin))

    async def persona(pid: str = ""):
        return await _wrap(lambda: build_persona(plugin, pid or _q("pid")))

    async def characters(inst: str = ""):
        i = inst or _q("inst") or plugin._default_inst
        if i == DEFAULT_TPL:
            i = plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_characters(plugin, i))
        finally:
            plugin.end_inst(tok)

    async def sessions():
        # 会话页要先拉一次 AstrBot 侧存活集：刚被用户在 AstrBot 删掉的会话
        # 立刻从下拉消失、进「残留数据」（孤儿会话可一键清理）。
        alive = None
        ref = getattr(plugin, "_refresh_alive", None)
        if callable(ref):
            try:
                alive = await ref(force=True)
            except Exception:
                alive = None
        return await _wrap(lambda: build_sessions(plugin, alive=alive))

    async def sessions_refresh_names(payload: dict | None = None):
        """调平台 API 补群名与成员昵称（见 refresh_names 的文档）。"""
        d = await _payload(payload)
        return await _wrap(lambda: refresh_names(
            plugin,
            groups=bool(d.get("groups", True)),
            members=bool(d.get("members", True)),
            friends=bool(d.get("friends", True)),
            force=bool(d.get("force", False))))

    async def stats(inst: str = ""):
        return await _wrap(lambda: build_stats(plugin, inst or _q("inst")))

    async def doctor():
        return await _wrap(lambda: build_doctor(plugin))

    async def state_get(umo: str = "", persona: str = "", uid: str = ""):
        return await _wrap(lambda: build_state(
            plugin, umo or _q("umo"), persona or _q("persona"),
            uid or _q("uid")))

    async def impression_get(uid: str = ""):
        return await _wrap(lambda: build_impression(plugin, uid or _q("uid")))

    async def preview(persona: str = "", is_private: bool = False,
                      msg_mode: int = 1, nsfw: str = ""):
        p = persona or _q("persona")
        priv = is_private or _qb("is_private")
        mode = _qi("msg_mode", 1) if not msg_mode or msg_mode == 1 else msg_mode
        # 查询串没带 nsfw 时传 None → 由 build_preview 取全局 allow_erotic_content
        ns = None if str(nsfw or "").strip() == "" else _qb("nsfw", True)
        return await _wrap(lambda: build_preview(
            plugin, p, is_private=priv, msg_mode=mode, nsfw=ns))

    async def attention(inst: str = ""):
        i = inst or _q("inst") or plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_attention(plugin))
        finally:
            plugin.end_inst(tok)

    async def memory(umo: str = "", inst: str = ""):
        i = inst or _q("inst") or plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_memory(plugin, umo or _q("umo")))
        finally:
            plugin.end_inst(tok)

    async def graph(limit: int = 0, persona: str = "", session: str = "",
                    inst: str = ""):
        i = inst or _q("inst") or plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_graph(
                plugin, limit or _qi("limit", 160),
                persona if persona else _q("persona"),
                session if session else _q("session")))
        finally:
            plugin.end_inst(tok)

    async def graph_node(key: str = ""):
        return await _wrap(lambda: build_graph_node(plugin, key or _q("key")))

    async def graph_search_(q: str = "", k: int = 0, session: str = ""):
        return await _wrap(lambda: graph_search(
            plugin, q or _q("q"), k or _qi("k", 10), session or _q("session")))

    async def interactions(inst: str = ""):
        i = inst or _q("inst") or plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_interactions(plugin))
        finally:
            plugin.end_inst(tok)

    async def proactive(inst: str = ""):
        # 先强刷一次 AstrBot 侧存活集：孤儿会话从「活跃度」消失、进残留，
        # 与 `_session_known`（出话闸门）共用同一判据，页面看得见=真会开口。
        ref = getattr(plugin, "_refresh_alive", None)
        if callable(ref):
            try:
                await ref(force=True)
            except Exception:
                pass
        i = inst or _q("inst") or plugin._default_inst
        tok = plugin.use_inst(i)
        try:
            return await _wrap(lambda: build_proactive(plugin))
        finally:
            plugin.end_inst(tok)

    async def groups():
        return await _wrap(lambda: build_groups(plugin))

    # ---- 写入：参数一律读 JSON body ----

    async def _payload(payload: dict | None) -> dict:
        """POST body 不会进函数签名，必须主动读。"""
        if isinstance(payload, dict) and payload:
            return payload
        return await _body()

    async def reload_():
        return await _wrap(lambda: reload_all(plugin))

    async def state_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_state(
            plugin, str(d.get("umo", "")), str(d.get("persona", "")),
            str(d.get("uid", "")), d.get("values") or {}))

    async def log_clear(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: clear_log(
            plugin, str(d.get("umo", "")), str(d.get("persona", "")),
            str(d.get("uid", ""))))

    async def inject_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_inject(
            plugin, str(d.get("umo", "")), str(d.get("persona", "")),
            str(d.get("uid", "")), str(d.get("text", ""))))

    async def impression_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_impression(
            plugin, str(d.get("uid", "")),
            sfw=d.get("sfw"), nsfw=d.get("nsfw"),
            sfw_custom=d.get("sfw_custom"),
            nsfw_custom=d.get("nsfw_custom"),
            reset_count=bool(d.get("reset_count"))))

    async def attention_reset(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: reset_attention(
            plugin, str(d.get("umo", ""))))

    async def memory_recall(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: recall_debug(
            plugin, str(d.get("umo", "")), str(d.get("query", "")),
            persona=str(d.get("persona", "")),
            top_k=int(d.get("top_k", 8) or 8),
            policy=int(d.get("policy", 0) or 0)))

    async def memory_forget(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: forget_memory(
            plugin, str(d.get("umo", ""))))

    async def memory_delete(payload: dict | None = None):
        d = await _payload(payload)
        ids = d.get("ids")
        if isinstance(ids, str):
            ids = [i for i in ids.split(",") if i.strip()]
        return await _wrap(lambda: forget_memory_atoms(
            plugin, str(d.get("umo", "")), ids or []))

    async def memory_edit(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: edit_memory_atom(
            plugin, str(d.get("umo", "")), str(d.get("id", "")),
            str(d.get("text", ""))))

    async def groups_assign(payload: dict | None = None):
        d = await _payload(payload)
        umos = d.get("umos")
        if not isinstance(umos, (list, tuple)):
            umos = [d.get("umo", "")]
        return await _wrap(lambda: assign_group(
            plugin, list(umos), str(d.get("group", ""))))

    async def groups_remove(payload: dict | None = None):
        d = await _payload(payload)
        umos = d.get("umos")
        if not isinstance(umos, (list, tuple)):
            umos = [d.get("umo", "")]
        return await _wrap(lambda: remove_group(plugin, list(umos)))

    async def groups_create(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: create_group(
            plugin, str(d.get("name", "")), str(d.get("persona", "")),
            str(d.get("instance", "")), d.get("costars")))

    async def groups_delete(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: delete_group(plugin, str(d.get("name", ""))))

    async def groups_persona(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_group_persona(
            plugin, str(d.get("group", "")), str(d.get("persona", ""))))

    async def groups_costars(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_group_costars(
            plugin, str(d.get("group", "")), d.get("pids")))

    async def groups_sync_cards(payload: dict | None = None):
        d = await _payload(payload)
        g = str(d.get("group") or _q("group") or "")
        return await _wrap(lambda: plugin.sync_group_cards(group=g))

    async def groups_rename(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: rename_group(
            plugin, str(d.get("old", "")), str(d.get("new", ""))))

    async def costars():
        return await _wrap(lambda: build_costars(plugin))

    async def costars_remove(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: remove_costar(
            plugin, str(d.get("umo", "")), str(d.get("persona", ""))))

    async def config_get(inst: str = ""):
        return await _wrap(lambda: build_config(plugin, inst=inst or _q("inst")))

    async def config_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_config(
            plugin, (d.get("values") or {}) if isinstance(d, dict) else {},
            str(d.get("inst") or _q("inst") or "")))

    # ---- 实例 / 人格池（WebUI「人格分配」拖拽页）----
    async def instances():
        return await _wrap(lambda: build_instances(plugin))

    async def persona_pool():
        return await _wrap(lambda: persona_pool_matrix(plugin))

    async def persona_pool_grant_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: persona_pool_grant(
            plugin, str(d.get("inst", "")), str(d.get("pid", ""))))

    async def persona_pool_revoke_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: persona_pool_revoke(
            plugin, str(d.get("inst", "")), str(d.get("pid", ""))))

    async def persona_pool_bulk_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: persona_pool_bulk(
            plugin, str(d.get("inst", "")), d.get("pids")))

    async def persona_pool_default_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: persona_pool_default(
            plugin, str(d.get("inst", "")), str(d.get("pid", ""))))

    async def session_params():
        return await _wrap(lambda: build_session_params(plugin))

    async def session_privileges():
        return await _wrap(lambda: build_session_privileges(plugin))

    async def session_privileges_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_session_privileges(
            plugin, str(d.get("scope", "")), d.get("qids")))

    async def session_privileges_clear(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: clear_session_privileges(
            plugin, str(d.get("scope", ""))))

    async def proactive_override_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_proactive_override(
            plugin, str(d.get("umo", "")), d.get("values") or {}))

    async def session_params_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_session_params(
            plugin, str(d.get("umo", "")), d.get("values") or {}))

    async def session_params_clear(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: clear_session_params(
            plugin, str(d.get("umo", ""))))

    async def proactive_override_clear(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: clear_proactive_override(
            plugin, str(d.get("umo", ""))))

    async def prune_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: prune_stale(
            plugin,
            sessions=d.get("sessions") or None,
            users=d.get("users") or None,
            proactive=d.get("proactive") or None))

    async def segment_test_(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: segment_test(
            plugin, str(d.get("text", "")),
            is_private=bool(d.get("is_private", True)),
            umo=str(d.get("umo", "")),
            msg_mode=int(d.get("msg_mode", 1) or 1)))

    async def segment_prompts():
        return await _wrap(lambda: build_segment_prompts(plugin))

    async def segment_prompts_set(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: set_segment_prompt(
            plugin, str(d.get("key", "")), str(d.get("text", "") or "")))

    async def segment_prompts_reset(payload: dict | None = None):
        d = await _payload(payload)
        return await _wrap(lambda: reset_segment_prompts(
            plugin, str(d.get("key", "") or "")))

    async def characters_set(payload: dict | None = None):
        d = await _payload(payload)
        tok = plugin.use_inst(str(d.get("inst") or _q("inst") or ""))
        try:
            return await _wrap(lambda: set_character(
                plugin, str(d.get("name", "")), d.get("aliases")))
        finally:
            plugin.end_inst(tok)

    async def characters_delete(payload: dict | None = None):
        d = await _payload(payload)
        tok = plugin.use_inst(str(d.get("inst") or _q("inst") or ""))
        try:
            return await _wrap(lambda: delete_character(
                plugin, str(d.get("name", ""))))
        finally:
            plugin.end_inst(tok)

    async def characters_match(payload: dict | None = None):
        d = await _payload(payload)
        tok = plugin.use_inst(str(d.get("inst") or _q("inst") or ""))
        try:
            return await _wrap(lambda: match_characters(
                plugin, str(d.get("text", "")), str(d.get("persona", ""))))
        finally:
            plugin.end_inst(tok)

    add("/personas", personas, ["GET"], "人格列表与当前人格")
    add("/persona", persona, ["GET"], "单个人格详情（章节树/文件/注入策略）")
    add("/characters", characters, ["GET"], "角色名/外号库（含简介与是否对应人格）")
    add("/characters/set", characters_set, ["POST"], "新增/更新角色及其外号")
    add("/characters/delete", characters_delete, ["POST"], "删除角色及其外号")
    add("/characters/match", characters_match, ["POST"], "试跑角色名匹配（排障）")
    add("/sessions", sessions, ["GET"], "会话/成员索引与注入覆盖")
    add("/sessions/refresh-names", sessions_refresh_names, ["POST"],
        "调平台 API 补齐群名与成员昵称")
    add("/stats", stats, ["GET"], "运行统计（全子系统）")
    add("/doctor", doctor, ["GET"], "doctor 自检报告")
    add("/preview", preview, ["GET"], "人格装配预览与节省量")
    add("/reload", reload_, ["POST"], "重新加载人格数据")
    add("/state", state_get, ["GET"], "读取指定会话/成员状态")
    add("/state/set", state_set, ["POST"], "修改状态数值")
    add("/state/clear-log", log_clear, ["POST"], "清空状态日志与情绪")
    add("/inject/set", inject_set, ["POST"], "设置/清除注入覆盖")
    add("/impression", impression_get, ["GET"], "读取用户印象")
    add("/impression/set", impression_set, ["POST"], "编辑用户印象")
    add("/attention", attention, ["GET"], "注意力概率拆解与判定统计")
    add("/attention/reset", attention_reset, ["POST"], "重置某会话的注意力状态")
    add("/memory", memory, ["GET"], "长期记忆原子与待固化缓冲")
    add("/memory/recall", memory_recall, ["POST"], "跑一次双路召回并展开明细")
    add("/memory/forget", memory_forget, ["POST"], "清空某会话的记忆")
    add("/memory/delete", memory_delete, ["POST"],
        "按 id 删除某会话的若干条记忆（连带其图谱条目）")
    add("/memory/edit", memory_edit, ["POST"],
        "改写单条记忆的正文（重算向量并重建其图谱条目）")
    add("/graph", graph, ["GET"], "知识图谱子图（可视化用）")
    add("/graph/node", graph_node, ["GET"], "图谱节点详情（邻居/条目）")
    add("/graph/search", graph_search_, ["GET"], "图谱路关键词检索")
    add("/interactions", interactions, ["GET"], "戳一戳/连发/撤回/TTS/分段统计")
    add("/proactive", proactive, ["GET"], "主动消息调度状态与候选")
    add("/session-params", session_params, ["GET"], "会话发言参数（模式/规则/解析结果）")
    add("/session-params/set", session_params_set, ["POST"], "写入某会话的发言参数覆盖")
    add("/session-params/clear", session_params_clear, ["POST"], "清除某会话发言参数覆盖")
    add("/session-privileges", session_privileges, ["GET"],
        "分会话特权用户（全局+覆盖+每会话生效名单）")
    add("/session-privileges/set", session_privileges_set, ["POST"],
        "写入某 scope（平台/平台:类型/精确UMO）的特权覆盖")
    add("/session-privileges/clear", session_privileges_clear, ["POST"],
        "清除某 scope 的特权覆盖")
    add("/groups", groups, ["GET"], "人格组：组定义 / 成员 / 未归组会话")
    add("/groups/create", groups_create, ["POST"], "新建人格组")
    add("/groups/delete", groups_delete, ["POST"], "删除人格组（成员转为独立）")
    add("/groups/assign", groups_assign, ["POST"], "把一批会话加入人格组")
    add("/groups/remove", groups_remove, ["POST"], "把一批会话移出人格组")
    add("/groups/persona", groups_persona, ["POST"], "设置人格组的人格（组内同步）")
    add("/groups/costars", groups_costars, ["POST"], "设置人格组的副人格（组内同步）")
    add("/groups/sync-cards", groups_sync_cards, ["POST"], "把组内会话的群名片对齐到当前人格")
    add("/groups/rename", groups_rename, ["POST"], "重命名人格组（保留成员）")
    add("/costars", costars, ["GET"], "多人格同台：启用会话与登场人格")
    add("/costars/remove", costars_remove, ["POST"],
        "把一个副人格移出某会话的同台")
    add("/config", config_get, ["GET"], "读取插件配置（schema + 当前值）")
    add("/config/set", config_set, ["POST"], "批量修改配置并保存、热同步")
    add("/instances", instances, ["GET"], "实例清单（配置/授权/记忆库）")
    add("/persona-pool", persona_pool, ["GET"], "人格池 × 实例授权矩阵")
    add("/persona-pool/grant", persona_pool_grant_, ["POST"], "给实例授权一个可用人格")
    add("/persona-pool/revoke", persona_pool_revoke_, ["POST"], "撤销某实例的人格授权")
    add("/persona-pool/bulk", persona_pool_bulk_, ["POST"], "整行保存某实例的授权列表")
    add("/persona-pool/default", persona_pool_default_, ["POST"], "设置某实例的默认人格")
    add("/proactive/override", proactive_override_set, ["POST"],
        "设置某会话的主动消息参数覆盖")
    add("/proactive/override/clear", proactive_override_clear, ["POST"],
        "清除某会话的主动消息覆盖")
    add("/prune", prune_, ["POST"], "删除残留会话/用户/主动状态数据")
    add("/segment/test", segment_test_, ["POST"], "试跑分段子代理")
    add("/segment-prompts", segment_prompts, ["GET"],
        "分段提示词（私聊/群聊 × 纯信息/剧情向）当前值")
    add("/segment-prompts/set", segment_prompts_set, ["POST"],
        "写入某份分段提示词的覆盖（空文本=恢复默认）")
    add("/segment-prompts/reset", segment_prompts_reset, ["POST"],
        "恢复默认（key 留空=全部）")
    return done
