# -*- coding: utf-8 -*-
"""历史卫生：剥离被取代的工具结果。

为什么必须靠 strip 而不是 flag
------------------------------
`internal.py:468` 的消息级 `_no_save` **只跳过 `assistant` / `user`**，
`role: "tool"` 不在其中 —— **工具结果一定落库**。
所以 `world_read` 读一次 7 KB 世界资料，就会永久留在历史里；
多读几次就沉积多版本旧设定，既膨胀上下文又与新注入冲突。

对策（方案 3.5.1-③）
--------------------
每轮把**上一轮之前**的目标工具结果替换为占位符：
  * 保留 `assistant`(tool_calls) ↔ `tool`(tool_call_id) 的配对，避免 API 校验失败
  * 只保留**最近一轮**的完整结果（模型刚读过的东西还在，不必立刻抹掉）
  * 更早的替换成一行占位符

移植自旧 `lili_state._strip_persona_rule_history`（main.py:1532-1561），
并推广为"任意工具名 + 可配置保留轮数"。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# 本插件需要清理的工具（读大段文本、结果会长期占据上下文）。
# 只列**真实注册**的工具。需要新增时，请与 main.py 的
# @filter.llm_tool(name=...) 保持一致：
#   * world_*：世界资料，读一次 7 KB，读完即弃；
#   * persona_list：人格长列表（拿 id 即弃）；
#   * persona_switch：结果含「已切换/本轮中止」说明，衔接完成后即弃
#     ——就算切换链路中止了本轮，这里也兜底，避免残留。
#   * memory_search / memory_note：检索结果每轮都会按新问题重新注入，
#     旧的那份是过期快照，留着只会占上下文、还可能误导。
DEFAULT_TOOL_NAMES = ("world_read", "world_search", "world_list",
                      "persona_list", "persona_switch",
                      "memory_search", "memory_note")

PLACEHOLDER = "[此前读取的规则/设定全文已剥离，以当前注入的内容为准]"

# ---------------------------------------------------------------- 历史 reminder
#
# 框架往每条用户消息上贴 `<system_reminder>`（User ID / Group name /
# Current datetime，另有内置插件 group_chat_context 的"上次回复后的群消息"
# 块），并**随消息一起落库**。其中只有两样东西有长期价值：
#   * **说话人身份**（User ID / Nickname）——群聊历史里每条 USER 消息是
#     谁发的，全靠它区分（插件自己不维护历史级说话人，群里最近发言只覆盖
#     30 分钟）。剥掉它 = 模型分不清历史里谁说了哪句。
#   * （无）——Group name 每条都一样且当轮 reminder 有；Current datetime
#     在历史里是**过期时间戳**，留着只会误导；群聊流水块已被后续消息取代。
#
# 对策（2026-10-01 定稿）：**改写而非删除**——identity reminder 替换成一行
# 轻量标签 `（说话人：昵称，uid 123）`，Group name / datetime / 群聊流水块
# 直接删除。当轮消息的 reminder 走 `extra_user_content_parts` 注入、不经
# 过 contexts，不受影响；群聊流水块由内置插件每轮重新注入最新版。
#
# 私聊例外（2026-10-01 晚追加）：私聊里身份标签是废话（只有两个人），
# 改为「丢身份、留时间」——reminder 压成 `[2026-10-01 周四 17:42]`。
# 群聊的时间由 chatcore_neo 的 `_live` 流水前缀兜底，私聊没有 `_live`，
# 所以这里必须把 datetime 留下。
#
# **content 有两种形态**（2026-10-01 实测存储）：纯字符串，或分块列表
# `[{"type":"text","text":"晚上好"},{"type":"text","text":"<system_reminder>…"}]`
# ——框架把 extra_user_content_parts 作为**独立 text 分块**落库。标签不含
# `<system_reminder>`，处理后的分块下一轮不会再被匹配（幂等）；清完整条
# 消息为空则还原原文，避免给 API 发空 user 内容。
_REMINDER_RE = re.compile(r"<system_reminder>.*?</system_reminder>", re.DOTALL)

# 框架 _append_system_reminders 的身份行格式：`User ID: {uid}, Nickname: {nick}`
# uid/nickname 都可能为空串（异常事件的 sender 字段缺失），分组用 `*` 容忍。
_IDENTITY_RE = re.compile(r"User ID:\s*([^\s,，]*)\s*[，,]\s*Nickname:\s*([^\n<]*)")


def _speaker_tag(reminder_block: str) -> str:
    """从 reminder 块里提取说话人身份，生成轻量标签；无身份行返回空串。"""
    m = _IDENTITY_RE.search(reminder_block)
    if not m:
        return ""
    uid = (m.group(1) or "").strip()
    nick = (m.group(2) or "").strip()
    if uid and nick:
        return f"（说话人：{nick}，uid {uid}）"
    if uid:
        return f"（说话人 uid {uid}）"
    if nick:
        return f"（说话人：{nick}）"
    return ""


# 私聊用的时间行（框架原文）：`Current datetime: 2026-10-01 17:42 (CST), Weekday: Thursday`
# 私聊历史**不留身份**（只有你们两个人，「说话人」是废话），但**留时间**：
# 群聊那边时间由 `_live` 流水前缀兜底，私聊没有 `_live`，只能靠这里。
_DATE_RE = re.compile(r"Current datetime:\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")
_WEEKDAY_RE = re.compile(r"Weekday:\s*([A-Za-z]+)")
_WEEKDAY_CN = {"Monday": "周一", "Tuesday": "周二", "Wednesday": "周三",
               "Thursday": "周四", "Friday": "周五", "Saturday": "周六",
               "Sunday": "周日"}


def _time_tag(reminder_block: str) -> str:
    """私聊模式：把 reminder 里的时间压成 `[2026-10-01 周四 17:42]`。

    找不到 datetime 行返回空串——调用方按"整块删除"处理（与旧行为一致）。
    """
    m = _DATE_RE.search(reminder_block)
    if not m:
        return ""
    w = _WEEKDAY_RE.search(reminder_block)
    week = f" {_WEEKDAY_CN.get(w.group(1), w.group(1))}" if w else ""
    return f"[{m.group(1)}{week} {m.group(2)}]"


def _transform_text(text: str, *, private: bool = False) -> str:
    """把文本里的 reminder 块替换为标签（无身份的块替换为空），再收拾空白。

    群聊（默认）：身份行 → `（说话人：昵称，uid xxx）`；
    私聊（`private=True`）：身份行丢弃、datetime → `[2026-10-01 周四 17:42]`。
    """
    if private:
        t = _REMINDER_RE.sub(lambda m: _time_tag(m.group(0)), text)
    else:
        t = _REMINDER_RE.sub(lambda m: _speaker_tag(m.group(0)), text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+\n", "\n", t)).strip()


def _clean_message_content(content, removed: list, *, private: bool = False):
    """处理单条消息的 content（str 或分块 list），返回清理后的新值。

    `private=True` 时按私聊规则改（丢身份、留时间）。
    `removed` 是 [字符数] 的单元素列表（便于在原地遍历里累加）。
    """
    if isinstance(content, str):
        if "<system_reminder>" not in content:
            return content
        cleaned = _transform_text(content, private=private)
        if not cleaned:
            return content          # 全文就是 reminder：还原，别发空消息
        removed[0] += len(content) - len(cleaned)
        return cleaned
    if isinstance(content, list):
        if not any("<system_reminder>" in str(_part_text(p) or "")
                   for p in content):
            return content
        new_parts = []
        part_removed = 0
        for p in content:
            t = _part_text(p)
            if t is None or "<system_reminder>" not in t:
                new_parts.append(p)
                continue
            cleaned = _transform_text(t, private=private)
            if not cleaned:             # 无标签可留（如群聊流水块）：整块删除
                part_removed += len(t)
                continue
            part_removed += len(t) - len(cleaned)
            if isinstance(p, dict):
                p = dict(p)
                p["text"] = cleaned
            new_parts.append(p)
        if not new_parts:
            return content          # 清完全空：还原（净减少计 0）
        removed[0] += part_removed
        return new_parts
    return content


def _part_text(p):
    """取消息分块里的纯文本；非文本分块返回 None。"""
    if isinstance(p, str):
        return p
    if isinstance(p, dict) and isinstance(p.get("text"), str):
        return p["text"]
    return None


def strip_system_reminders(contexts: list, *,
                           private: bool = False) -> tuple[list, int]:
    """原地改写历史消息里的 `<system_reminder>` 块。

    身份行（User ID / Nickname）压成一行说话人标签保留；Group name /
    datetime / 群聊流水块删除。`private=True`（私聊会话）时改为：
    身份行丢弃、datetime 压成 `[2026-10-01 周四 17:42]` 保留
    （群聊的时间由 `_live` 流水前缀兜底，私聊没有 `_live`）。
    content 为字符串或分块列表（多模态形态）都能处理；图片等非文本分块
    原样保留。返回 (同一个 list, 净减少的字符数)。幂等：标签不含 reminder
    标记，重复执行零改动。
    """
    if not contexts:
        return contexts, 0
    removed = [0]
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        content = ctx.get("content")
        if isinstance(content, str):
            if "<system_reminder>" not in content:
                continue
        elif isinstance(content, list):
            if not any("<system_reminder>" in str(_part_text(p) or "")
                       for p in content):
                continue
        else:
            continue
        ctx["content"] = _clean_message_content(content, removed,
                                                private=private)
    return contexts, removed[0]


def _iter_tool_calls(contexts: list) -> Iterable[tuple[int, str, str]]:
    """产出 (消息下标, tool_call_id, 工具名)。"""
    for i, ctx in enumerate(contexts):
        if not isinstance(ctx, dict) or ctx.get("role") != "assistant":
            continue
        for tc in ctx.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name") if isinstance(fn, dict) else None
            cid = tc.get("id")
            if name and cid:
                yield i, str(cid), str(name)


def strip_tool_results(contexts: list, *,
                       tool_names: Iterable[str] = DEFAULT_TOOL_NAMES,
                       keep_recent: int = 1,
                       placeholder: str = PLACEHOLDER) -> list:
    """原地把"旧"工具结果替换为占位符，返回同一个 list。

    `keep_recent` 表示保留最近 N "轮"（按 assistant 消息分组）的完整结果。
    """
    if not contexts:
        return contexts
    names = set(tool_names)

    # 按 assistant 消息下标分组：同一条 assistant 消息里的多个 tool_call 算一轮
    by_msg: dict[int, list[str]] = {}
    for idx, cid, name in _iter_tool_calls(contexts):
        if name in names:
            by_msg.setdefault(idx, []).append(cid)

    ordered_msgs = sorted(by_msg)
    keep_ids: set[str] = set()
    for idx in ordered_msgs[-keep_recent:] if keep_recent > 0 else []:
        keep_ids.update(by_msg[idx])

    strip_ids = {cid for idx in ordered_msgs for cid in by_msg[idx]} - keep_ids
    if not strip_ids:
        return contexts

    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        if ctx.get("role") == "tool" and str(ctx.get("tool_call_id")) in strip_ids:
            ctx["content"] = placeholder
    return contexts


def stripped_report(contexts: list, *,
                    tool_names: Iterable[str] = DEFAULT_TOOL_NAMES) -> dict:
    """统计当前历史里的工具结果状况（doctor / 调试用）。"""
    names = set(tool_names)
    calls = [(idx, cid, name) for idx, cid, name in _iter_tool_calls(contexts)
             if name in names]
    total_bytes = 0
    stripped = 0
    ids = {cid for _i, cid, _n in calls}
    for ctx in contexts:
        if isinstance(ctx, dict) and ctx.get("role") == "tool" \
                and str(ctx.get("tool_call_id")) in ids:
            content = ctx.get("content") or ""
            if not isinstance(content, str):
                content = str(content)
            total_bytes += len(content.encode("utf-8"))
            if content.strip() == PLACEHOLDER:
                stripped += 1
    return {"tool_results": len(ids), "bytes": total_bytes,
            "already_stripped": stripped}
