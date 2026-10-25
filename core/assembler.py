# -*- coding: utf-8 -*-
"""PromptAssembler —— 全插件**唯一**允许修改 `req` 的地方（方案 3.5）。

八分区契约
----------
| # | 分区 | 去向 | 稳定性 |
|---|---|---|---|
| 1 | 人格规则 | `req.system_prompt` 尾部 | 人格不变则字节稳定 |
| 1b | 说话风格（L2） | 同上 | 同上 |
| 1c | 关系行为（按档位） | 同上 | 同上 |
| 1d | 角色背景（lore.md 原文） | 同上 | 同上 |
| 2 | 世界观提示（一行） | 同上 | 同上 |
| 3 | 发言格式规范 | 同上 | 同上 |
| — | —— 以上构成可缓存前缀，**不得含时间戳** —— | | |
| 3.6 | 多人格同台（副人格规则） | `extra_user_content_parts` + `mark_as_temp()` | 开启同台才出现 |
| 4 | 数值状态（含思考锚点/结巴/时段） | `extra_user_content_parts` + `mark_as_temp()` | 每轮变 |
| 5 | 用户印象 | 同上 | 每 N 轮变 |
| 6 | 记忆召回块 | 同上 | 每轮变 |
| 7 | 引用回溯 / 连发合并 / 本轮对象 | 同上 | 每轮变 |
| 7b | 本轮提到的角色资料 | 同上 | 命中才出现 |
| 8 | 对话历史上下文 / 系统通知 | 同上 | 每轮变 |

两条硬性纪律
------------
* **动态内容一律不写 `req.contexts`** —— 会落库并累积（旧实现的"每条 user
  消息都嵌着旧 conversation_log"就是这么来的，见 lili_state/main.py:1906-1912）。
* **幂等**：每轮先按标记剔除自己上一轮的注入，再重新装配，
  保证同一分区在一轮请求里恰好出现一次。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .inject import build_identity_block, build_inject_text
from .persona import render as R
from .persona.model import PersonaDoc
from .state.model import MemberState

MARK_BEGIN = "<!-- chatcore_neo:stable:begin -->"
MARK_END = "<!-- chatcore_neo:stable:end -->"

SEP = "\n\n---\n\n"

H_PERSONA = "# 人格规则（{persona}）"
H_WORLD = "# 世界观资料"

# 动态段标题
H_STATE = "# 当前状态"
H_IMPRESSION = "# 你对这个用户的印象"
H_MEMORY = "# 相关长期记忆"
H_QUOTE = "# 被引用的消息"
H_MERGE = "# 用户连发消息"
H_LIVE = "# 群里最近发言（带说话人）"
H_CHARACTERS = "# 消息里提到的角色（资料参考）"
H_COSTARS = "# 多人格同台"
H_NO_TEXT = "# 本轮无文字发言"
H_HISTORY = "# 近期上下文"
H_NOTICE = "# 系统通知"


@dataclass
class AssemblyInput:
    doc: PersonaDoc
    state: MemberState | None = None
    tier: str = "friend"
    is_private: bool = False
    msg_mode: int = 1
    uid: str = ""
    user_name: str = ""
    # @ / 引用协议说明（由 main.py 用本轮真实 QQ / 消息 id 生成）
    marker_block: str = ""
    user_msg: str = ""
    impression: str = ""
    memories: list[str] = field(default_factory=list)
    history: str = ""
    notices: list[str] = field(default_factory=list)
    quote_block: str = ""
    merge_notice: str = ""
    include_dialog: bool = False
    tier_label: str = ""
    cfg: Any = None
    unrestricted: set[str] = field(default_factory=set)
    identity_confirm: bool = True
    # 会话级 NSFW 开关（None = 继承全局 allow_erotic_content）
    nsfw_enabled: bool | None = None
    # 本轮回合是否为合成事件（主动消息/戳一戳回应）：状态文案需切换口径
    proactive: bool = False
    # 群聊近期发言流水（带说话人），用于纠正常见的"群聊认错人" ——
    # 框架 contexts 里的 user 消息没有说话人标识，模型分不清谁说的
    live_speakers: str = ""
    # 本轮消息里提到的角色（纯逻辑命中，见 core/characters.py）。
    # 走动态段 = extra_user_content_parts + mark_as_temp()，不落库。
    characters: str = ""
    # 多人格同台（剧情模式下同时扮演多个角色，见 core/costars.py）。
    # 同样走动态段 temp：不落库、不动 system_prompt 前缀。
    costars: str = ""
    # 本轮用户没有文字发言（纯图片/表情/@）：必须显式告诉模型，
    # 否则它会把"昵称/周边文本"脑补成用户说过的话
    empty_user_text: bool = False


@dataclass
class AssemblyResult:
    system_suffix: str = ""
    dynamic_text: str = ""
    parts: dict[str, int] = field(default_factory=dict)

    @property
    def stable_bytes(self) -> int:
        return len(self.system_suffix.encode("utf-8"))

    @property
    def dynamic_bytes(self) -> int:
        return len(self.dynamic_text.encode("utf-8"))


class PromptAssembler:
    """无状态装配器（每次调用传入输入，不持有会话状态）。"""

    def __init__(self, *, framework: bool = True) -> None:
        self.framework = framework

    # ------------------------------------------------------------ 稳定段
    def build_stable(self, inp: AssemblyInput) -> str:
        doc = inp.doc
        parts: list[str] = []

        # 1 人格规则（框架 slice 与人格正文各自带标题，不再套外壳）
        l1 = R.build_l1(doc, include_framework=self.framework,
                        is_private=inp.is_private, msg_mode=inp.msg_mode)
        if inp.include_dialog:
            dialog = doc.read_content("dialog")
            if dialog:
                l1 = l1 + f"{SEP}# 角色台词片段（dialog）\n\n{dialog}"
        if l1:
            parts.append(l1)

        # 1b 这个角色的说话习惯（L2 摘要）
        l2 = R.build_l2(doc)
        if l2:
            parts.append(l2)

        # 1c 与本轮对象的关系：tier 文件自带「与特权用户的关系与行为（等级 1）」
        #    标题，不再套一层「与当前用户的关系与行为（引航者）」的壳
        tier_body = R.load_tier_body(doc, inp.tier)
        if tier_body:
            parts.append(tier_body)

        # 1d NSFW 规则（会话级开启时才注入）
        #    **旧实现这里漏了**：nsfw 规则只在 legacy_shape（装配预览/迁移校验）
        #    里被读取，在线请求路径一次都没注入过。于是"WebUI 里开了 NSFW、
        #    模型照旧拒绝"——`nsfw_enabled` 只解除了禁止语（不再注入"禁止色情
        #    内容"），却没有任何正向许可与写法规范。实测：开启后提示词里
        #    「露骨 / ♡ / 非常主动」命中全为 False。
        if inp.nsfw_enabled:
            nsfw = R.load_nsfw_body(inp.is_private, inp.msg_mode)
            if nsfw:
                parts.append(nsfw)

        # 1e 角色背景（L4）：默认注入 lore.md 原文。原文里的「关键人物/关系」
        # 表、剧情时间线、游戏内设定是角色认知剧情关联人物的唯一来源，
        # 换成抽取摘要会让它认不出被截断的关系（详见 registry 的 lore_full）。
        lore = R.resolve_lore_section(doc)
        if lore:
            parts.append(lore)

        # 2 世界观提示（一行，仅当存在资料时）
        if R.build_world_hint(doc):
            try:
                from .world import WORLD_HINT_LINE, has_any_world_data
                if has_any_world_data():
                    parts.append(WORLD_HINT_LINE)
            except Exception:
                pass

        # 3 发言格式规范：narrative 已并入装配表（load_framework 按发言模式
        # 注入 format/narrative.md），此处不再单独追加，避免重复注入。
        return SEP.join(p for p in parts if p and p.strip())

    # ------------------------------------------------------------ 动态段
    def build_dynamic(self, inp: AssemblyInput) -> tuple[str, dict[str, int]]:
        """返回 (动态文本, 各分区字节数)。全部走 `extra_user_content_parts`。"""
        blocks: list[str] = []
        sizes: dict[str, int] = {}

        def add(title: str, body: str, key: str) -> None:
            body = (body or "").strip()
            if not body:
                return
            block = f"{title}\n{body}" if title else body
            blocks.append(block)
            sizes[key] = len(block.encode("utf-8"))

        # 3.6 多人格同台（剧情模式下同时扮演多个角色）。放最前，让角色框架
        #     先于数值状态出现 —— 同台时"我是谁"比"我心情如何"更该被先读到。
        add(H_COSTARS, inp.costars, "costars")

        # 4 数值状态（含思考锚点 / 结巴 / 时段修饰）
        if inp.state is not None:
            add(H_STATE, build_inject_text(
                inp.state, inp.uid or "对方", inp.user_msg or "",
                display_name=inp.doc.display_name,
                tier=inp.tier, cfg=inp.cfg,
                unrestricted=inp.unrestricted,
                nsfw_enabled=inp.nsfw_enabled,
                proactive=inp.proactive), "state")

        # 5 用户印象
        add(H_IMPRESSION, inp.impression, "impression")

        # 6 记忆召回
        if inp.memories:
            add(H_MEMORY,
                "（以下为长期记忆召回，仅作背景参考，无需刻意提及，"
                "更不要生硬复述；自然契合当前话题时融入即可，"
                "无关或生硬时忽略）\n"
                + "\n".join(f"- {m}" for m in inp.memories),
                "memory")

        # 7 引用回溯 / 连发合并 / 本轮对象
        add(H_QUOTE, inp.quote_block, "quote")
        add(H_MERGE, inp.merge_notice, "merge")
        if inp.identity_confirm and not inp.is_private and inp.uid:
            add("", build_identity_block(
                display_name=inp.doc.display_name, tier=inp.tier,
                tier_label=inp.tier_label, uid=inp.uid,
                user_name=inp.user_name), "identity")
        # @ / 引用的协议说明（缺了它模型就会自己发明 `[@昵称]`）
        if getattr(inp, "marker_block", ""):
            add("", inp.marker_block, "markers")

        # 7b 本轮消息里提到的角色：纯逻辑查表（core/characters.py），
        # 命中了才出现。给的是"这个名是谁、和当前人格什么关系"，
        # 免得角色扮演里因为不认识剧情人物而答"查无此人"。
        add(H_CHARACTERS, inp.characters, "characters")

        # 8 群聊近期发言流水（带说话人）：区分"这句话是谁说的"，
        # 否则模型会把群里其他人的话当成当前用户说的（认错人）。
        if getattr(inp, "live_speakers", ""):
            add(H_LIVE, inp.live_speakers, "live")

        # 8b 空文本护栏：对方本轮只发了图片/表情/@，没有文字发言 ——
        # 明确说出来，防止模型把昵称/周边文本脑补成 ta 说过的话（实测翻车点）。
        if getattr(inp, "empty_user_text", False):
            add(H_NO_TEXT,
                "对方本轮**没有文字发言**（可能是发图/表情/群名片/仅 @）。"
                "不要引用、转述或猜测 ta 说了什么；昵称不是 ta 说过的话。"
                "就图片/表情/现状自然地回应即可。", "notext")

        # 9 对话历史上下文 / 系统通知
        add(H_HISTORY, inp.history, "history")
        if inp.notices:
            add(H_NOTICE, "\n".join(f"- {n}" for n in inp.notices), "notice")

        return "\n\n".join(blocks), sizes

    # ------------------------------------------------------------ 组装
    def build(self, inp: AssemblyInput) -> AssemblyResult:
        stable = self.build_stable(inp)
        dynamic, sizes = self.build_dynamic(inp)
        suffix = f"\n\n{MARK_BEGIN}\n{stable}\n{MARK_END}" if stable else ""
        if stable:
            sizes["stable"] = len(stable.encode("utf-8"))
        return AssemblyResult(system_suffix=suffix, dynamic_text=dynamic,
                              parts=sizes)

    # ------------------------------------------------------------ 幂等
    @staticmethod
    def strip_previous(system_prompt: str) -> str:
        """剔除自己上一轮注入的整块（含标记），保证一段只出现一次。"""
        out = system_prompt or ""
        while True:
            i = out.find(MARK_BEGIN)
            if i < 0:
                break
            j = out.find(MARK_END, i)
            if j < 0:
                out = out[:i]
                break
            out = out[:i] + out[j + len(MARK_END):]
        return out.rstrip()
