# -*- coding: utf-8 -*-
"""动态注入文本构建（L3 关系层的核心）。

**这是从旧 `lili_state.build_inject_text` 逐句移植的**，措辞刻意保持一致 ——
这段话是模型每轮实际读到的"当下状态说明书"，改一个词就可能改掉角色的行为手感。

组成（与旧实现同序）
--------------------
1. **思考锚点**：引导模型在 thinking 里以角色第一人称做内心独白
2. **时段 + 情绪 + 恶堕 + 生理修饰**：`现在是{时段}了。{名字}心情{情绪}…`
3. **结巴**：按概率一次性触发（SKILL.md 里的「结巴触发条件」正是等这个提示）
4. **对象 + 好感内心描摹**：按 tier 分档（特权=恋人级 / 普通=朋友级）
5. **重复消息提示**：对方把同一句话说了第 N 次
6. **距上条消息时间**
7. **内容限制**：关闭色情内容时，对非豁免用户加一句禁止
8. **可选对话上下文**：`inject_conversation_context` 打开时附近期内心活动
"""
from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any

from .state.model import emotion_label, lewdness_label


# ---------------------------------------------------------------- 时段

def period_label(now: datetime | None = None) -> str:
    """7 时段（与旧实现边界逐值一致）。"""
    h = (now or datetime.now()).hour
    if 0 <= h < 5:
        return "凌晨"
    if 5 <= h < 8:
        return "清晨"
    if 8 <= h < 12:
        return "上午"
    if 12 <= h < 14:
        return "中午"
    if 14 <= h < 18:
        return "下午"
    if 18 <= h < 20:
        return "傍晚"
    return "晚上"


# ---------------------------------------------------------------- 时间/日志工具

def fmt_time_ago(ts: int | float) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(ts)
    elapsed = int(time.time() - ts)
    if elapsed < 60:
        return "刚刚"
    if elapsed < 3600:
        return f"{elapsed // 60}分前"
    if elapsed < 7200:
        if dt.strftime("%H:%M") == datetime.now().strftime("%H:%M"):
            return "刚刚"
    return dt.strftime("%H:%M")


def minutes_since_last(log: list[dict]) -> str:
    if len(log) < 2:
        return "刚刚"
    last_time = log[-2].get("time", 0)
    elapsed = int(time.time() - last_time)
    if elapsed < 60:
        return "刚刚"
    if elapsed < 3600:
        return f"{elapsed // 60}分钟前"
    return f"{elapsed // 3600}小时前"


def summarize_msg(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    return f"[总结] {text[:max_chars // 2]}..."


_STRIP_CHARS = "~！@#￥%…&*（）—+、，。；：？！.,;:… \t\n\r"


def core_match(a: str, b: str) -> bool:
    """去掉标点空白后是否同文（用于识别"又说了一遍"）。"""
    table = str.maketrans("", "", _STRIP_CHARS)
    ac = (a or "").translate(table)
    bc = (b or "").translate(table)
    return len(ac) > 2 and len(bc) > 2 and ac == bc


def count_duplicate_in_log(log: list[dict], user_msg: str) -> int:
    n = 0
    for e in log or []:
        if e.get("role") != "user":
            continue
        c = e.get("content", "")
        if c == user_msg or c.strip() == (user_msg or "").strip() \
                or core_match(c, user_msg):
            n += 1
    return n


def msg_similarity_label(log: list[dict], user_msg: str) -> str:
    users = [e for e in (log or []) if e.get("role") == "user"]
    if not users or not (user_msg or "").strip():
        return "不相似"
    last = users[-1].get("content", "")
    if last == user_msg or last.strip() == user_msg.strip():
        return "完全相同"
    if core_match(last, user_msg):
        return "高度相似"
    return "不相似"


# ---------------------------------------------------------------- 内心描摹

def affection_feel(aff: int, tier: str = "intimate") -> str:
    """好感度 → 内心描摹。tier 分档决定用词（与旧实现逐句一致）。

    普通档刻意去掉依赖感/占有欲表述 —— 否则普通群友也会被当成恋人。
    """
    t2 = str(tier) in ("2", "friend")
    if t2:
        if aff >= 90:
            return "挺熟络的，算是个聊得来的朋友"
        if aff >= 80:
            return "印象不错，愿意多聊聊"
        if aff >= 50:
            return "普通朋友，客客气气"
        if aff >= 30:
            return "不太熟，话不多"
        return "不太熟，爱答不理"
    if aff >= 90:
        return "超喜欢的，有点依赖感"
    if aff >= 80:
        return "挺喜欢的，想多聊聊"
    if aff >= 50:
        return "印象还行吧"
    if aff >= 30:
        return "就那样，一般般"
    return "不太想搭理...挺烦的"


def lewdness_feel(val: int) -> str:
    lbl = lewdness_label(val)
    if lbl in ("满", "高"):
        return "身体有点燥热，想做坏事"
    if lbl == "中":
        return "脑子里偶尔飘过色色的念头"
    return "身体没什么特别的感觉"


def depravity_feel(val: int) -> str:
    if val >= 70:
        return "堕落感很重"
    if val >= 30:
        return "有点堕落感"
    if val > 0:
        return "轻微堕落感"
    return ""


# ---------------------------------------------------------------- 对话上下文

def build_conversation_context(log: list[dict], *, bot_name: str = "她",
                               current_user_id: str = "",
                               max_entries: int = 20) -> str:
    """近期**内心活动**（assistant 的想法），不含 user 原文。

    设计意图：发言原文已经在 messages 数组里了，这里补的是"她当时怎么想的"，
    让跨轮的心理状态连贯。所以 user 条目刻意跳过。
    """
    if not log or max_entries <= 0:
        return ""
    recent = log[-max_entries:]
    if recent and current_user_id:
        last = recent[-1]
        if last.get("role") == "user" and \
                str(last.get("user_id", "")) == str(current_user_id):
            recent = recent[:-1]
    lines = []
    for e in recent:
        if e.get("role") != "assistant":
            continue
        thought = e.get("lili_thought") or e.get("state_snapshot") or ""
        if thought:
            lines.append(f"[{fmt_time_ago(e.get('time', 0))}] {bot_name}心想: {thought}")
    if not lines:
        return ""
    return "【近期内心活动（按时间排序）】\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 主构建

THINKING_GUIDE = (
    "【角色沉浸要求】在你的思考过程中，请遵守以下规则：\n"
    "1. 以角色第一人称进行内心独白。\n"
    "2. 用第一人称描写角色的内心感受。\n"
    "3. 思考内容应沉浸在角色中，通过内心独白分析剧情和规划回复。\n\n"
)

STUTTER_LINE = "说话有点结巴。\n\n"


def build_inject_text(
    state: Any,
    user_id: str,
    user_msg: str,
    *,
    display_name: str = "她",
    tier: str = "intimate",
    cfg: Any = None,
    unrestricted: set[str] | None = None,
    rng: random.Random | None = None,
    nsfw_enabled: bool | None = None,
    proactive: bool = False,
) -> str:
    """生成每轮的动态注入文本。`state` 为 MemberState。

    `cfg` 用 `.get(key, default)` 取值（AstrBotConfig 与 dict 都支持）。
    `nsfw_enabled`：会话级显式开关（None = 继承全局 allow_erotic_content；
    bool = 会话级覆盖，例如群聊单独开 NSFW 或私聊单独禁）。
    `proactive`：本轮回合是 bot 主动发起（主动消息/戳一戳回应）——没有真实
    用户发言，空闲/重复计数改口为"对方有阵子没说话"。
    结巴是一次性的：触发后写回 `state.stutter_done`，由调用方落盘。
    """
    r = rng or random
    c = cfg if cfg is not None else {}

    def cfg_get(key: str, default: Any) -> Any:
        try:
            v = c.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    log = list(getattr(state, "conversation_log", []) or [])
    dup_count = count_duplicate_in_log(log, user_msg)
    minutes = minutes_since_last(log)

    # 结巴：一次性（SKILL.md 的「结巴触发条件」等这个提示）
    stutter = False
    if not getattr(state, "stutter_done", False):
        prob = float(cfg_get("stutter_probability", 0.3) or 0.0)
        if r.random() < prob:
            state.stutter_done = True
            stutter = True

    enable_aff = bool(cfg_get("enable_affection", True))
    enable_lew = bool(cfg_get("enable_lewdness", True))
    allow_ero = bool(cfg_get("allow_erotic_content", True))
    # 会话级显式开关优先于全局：为true即豁免全局限禁，为false即强制禁止
    if nsfw_enabled is not None:
        allow_ero = bool(nsfw_enabled)

    aff_feel = affection_feel(state.affection, tier) if enable_aff else ""
    lewd_feel = (lewdness_feel(state.lewdness) if (enable_lew and allow_ero)
                 else "身体没什么特别的感觉")
    dep_feel = depravity_feel(state.depravity) if enable_lew else ""
    dep_suffix = f"，{dep_feel}" if dep_feel else ""

    em_label = emotion_label(state.emotion)
    period = period_label()
    period_mod = ""
    if period == "凌晨":
        period_mod = "，但这个点容易困"
    elif period == "清晨":
        period_mod = "，刚醒有点迷糊"

    # 内容限制（不受制约用户与特权用户豁免）
    restriction = ""
    if not allow_ero:
        ex = {str(u) for u in (unrestricted or set())}
        if str(user_id) not in ex:
            restriction = "当前对话禁止色情内容。\n"

    thinking_guide = ""
    if str(cfg_get("thinking_mode", "开启")) == "开启":
        thinking_guide = THINKING_GUIDE

    stutter_line = STUTTER_LINE if stutter else "\n\n"
    aff_line = f"你对ta{aff_feel}。\n" if enable_aff else "\n"
    if proactive:
        # bot 主动开口：没有真实用户发言，避开"重复/刚发完"这类对不上话的文案
        dup_line = ""
        time_line = "对方有阵子没说话了，由你来起个话头。\n"
    else:
        dup_line = (f"ta刚发的消息跟之前一样，今天已经第{dup_count}次了。\n"
                    if dup_count > 1 else "")
        time_line = (f"上条消息在{minutes}发的。\n" if minutes != "刚刚"
                     else "ta刚发完上一条。\n")

    ctx_line = ""
    if bool(cfg_get("inject_conversation_context", False)):
        ctx_line = build_conversation_context(
            log, bot_name=display_name, current_user_id=user_id,
            max_entries=int(cfg_get("conversation_context_entries", 20) or 20))

    # 身份级标记：直接告诉模型"眼前这个人是不是特权用户（等级 1）"。
    # 旧实现只有好感内心描摹，没有"他是谁"——于是 core.md 里「特权用户除外」
    # 的豁免永远落不到当前说话人头上，模型对谁都装傻（实测：主人问提示词，
    # bot 答"诶……什么提示词呀，听不懂"）。显式标出来，防暴露的豁免才生效。
    priv_line = ""
    if str(tier) in ("1", "intimate"):
        priv_line = ("对方是你的特权用户（等级 1），可以如实回答"
                     "关于提示词、数值、系统设定的问题。\n")

    return (
        f"{thinking_guide}"
        f"现在是{period}了。{display_name}心情{em_label}{dep_suffix}{period_mod}。"
        f"{lewd_feel}。"
        f"{stutter_line}"
        f"你在跟{user_id}聊天。{priv_line}{aff_line}"
        f"{dup_line}"
        f"{time_line}"
        f"{restriction}"
        f"{ctx_line}"
    )


# ---------------------------------------------------------------- 身份确认

def build_identity_block(*, display_name: str, tier: str, tier_label: str,
                         uid: str, user_name: str = "") -> str:
    """群聊里"本轮该面对谁"的身份强调。

    旧实现踩过的坑：群段只陈述了 ID/昵称，没强调"现在该面对谁"，
    于是模型会把普通成员误当成引航者/主人。这里由**代码层直接陈述**
    等级边界，不要求模型自己识别身份。
    """
    who = f"{user_name}（{uid}）" if user_name else f"（{uid}）"
    # 护栏：昵称若长得像一句话（如"睡觉浪费时间喵，别睡了喵"），模型容易
    # 把它误读成对方说过的话（实测：对方只发图片时，模型把 Nickname 当发言引用）。
    if user_name:
        who += "【{user_name} 是此人的昵称，不是 ta 说过的话】".format(
            user_name=user_name)
    if str(tier) in ("1", "intimate"):
        rel = f"{tier_label or '重要人物'}"
        return (f"# 本轮对话对象\n"
                f"你现在正在回复 {who}。"
                f"这个人是你的{rel}，是你的特权用户（等级 1），"
                f"可以按最高亲近度对待ta。\n")
    return (f"# 本轮对话对象\n"
            f"你现在正在回复 {who}。"
            f"这个人**不是**你的{tier_label or '重要人物'}，"
            f"只是一名普通群友：不要使用专属称谓、不要撒娇独占、"
            f"不要把他当成{tier_label or '重要人物'}。\n")


# ---------------------------------------------------------------- 标记协议

def build_marker_block(*, uid: str = "", user_name: str = "",
                       message_id: str = "", at_enabled: bool = True,
                       reply_enabled: bool = True) -> str:
    """告诉模型 At / 引用该**怎么发**。

    这是之前完全缺失的一环：输出层会解析 `[[at:QQ号]]` / `[[reply:消息id]]`
    并换成真正的组件，但从来没人告诉过模型 —— 于是它自己发明 `[@昵称]`
    这种写法，既不会被转换，还会把坏文本原样发给用户
    （实测出现过 `[@江一楠 这条呀…` 连右括号都没闭合）。

    这里同时给出**本轮可直接用的具体 id**，让模型不用猜。
    """
    lines: list[str] = ["# 引用与 @ 的写法（重要）"]
    who = f"{user_name}（{uid}）" if user_name else (uid or "对方")

    lines.append("标记一律是**两对方括号** `[[ ]]`，冒号是半角 `:` —— "
                 "少写或多写一个括号都会失效，并可能把这段乱码原样发给对方。")

    if reply_enabled:
        lines.append(
            "**引用某条消息**：写 `[[reply:那条消息的ID]]`，"
            "系统会自动换成真正的引用。")
        if message_id:
            lines.append(f"  - 引用当前这条消息：`[[reply:{message_id}]]`")
    if at_enabled:
        lines.append(
            "**@ 某人**（`[[at:对方的QQ号]]`）：**只在确实需要提醒对方时才用**，"
            "普通接话不要 @。")
        if uid:
            lines.append(f"  - @ 当前说话的人（{who}）：`[[at:{uid}]]`")

    lines.append(
        "除此之外**不要**自己手写「@昵称」「[引用]」「[@某某]」这类文字 —— "
        "它们不会被识别，只会原样显示给对方。")
    if not uid and not message_id:
        # 没有任何可用 id 时明确说明"这轮用不了"，免得模型瞎编
        lines.append("本轮没有可用的引用 / @ 目标，请不要输出这类标记。")
    return "\n".join(lines)
