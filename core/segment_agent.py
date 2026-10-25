# -*- coding: utf-8 -*-
"""分段子代理：让 LLM 按语义决定分段，**直接返回 JSON 数组**。

协议选择（为什么不是标记）
--------------------------
旧环境走的是「子代理插 `§n` 标记 → 再按标记切分」。这条路有两个问题：
  1. **标记会漏**：子代理忘了删、或主 agent 自己写了 `§n`，它就原样发给用户
  2. **换行与标记混淆**：真实换行和分段标记共用一个切分规则，语义分不开

改成结构化输出后，模型返回的就是**发送用的段列表本身**：

    ["第一段", "第二段", "第三段"]

不需要标记词汇，也不存在"标记漏进消息"这个失败模式。
（`code` 块、`{"segments": [...]}` 包装都做兼容解析。）

内容守恒校验
------------
分段子代理**只该切分、不该改写**。所以拿到数组后把元素拼回去，
与原文做归一化比对：

  * 完全一致 → 采用
  * 差异很小（相似度 ≥ `segment_verify_ratio`，默认 0.95）→ 采用，但计入 `lenient`
  * 否则 → **弃用**，回退本地规则（`core/output.py:split_segments`）

这一步是最后一道闸：宁可切得难看，也不能让角色说的话被改掉。

与"唯一生成入口"的关系
----------------------
它**不生成发给用户的内容**：输入是已生成好的回复文本，输出只是切分结果。
不碰主对话 `req`，用独立 provider、temperature=0。
"""
from __future__ import annotations

import asyncio
import difflib
import json
import re
from typing import Any

from .judge import resolve_subagent_provider
from .output import clean_segments
from .session_params import MODE_NARRATIVE, MODE_PLAIN

import logging

# 用 stdlib logger 而不是 `from astrbot.api import logger`：
# `core/*` 必须能**脱离框架**被 import（测试就是这么用的）。
# 名字取 "astrbot"，输出会并进框架自己的 logger，级别/格式一致。
logger = logging.getLogger("astrbot")
import time

# 兼容模型可能返回的几种包装
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _norm(s: str) -> str:
    """归一化：去掉所有空白，用于内容守恒比对。"""
    return "".join(ch for ch in (s or "") if not ch.isspace())


def parse_segments(raw: str) -> list[str] | None:
    """把模型输出解析成段列表；解析不出来返回 None。

    依次尝试：直接 JSON → 去掉 code fence 再 JSON → 从文本里抠出第一个数组。
    """
    txt = (raw or "").strip()
    if not txt:
        return None

    def _coerce(obj: Any) -> list[str] | None:
        if isinstance(obj, list):
            segs = [str(x) for x in obj if str(x).strip()]
            return segs or None
        if isinstance(obj, dict):
            for key in ("segments", "parts", "result", "data", "items"):
                v = obj.get(key)
                if isinstance(v, list):
                    segs = [str(x) for x in v if str(x).strip()]
                    return segs or None
        return None

    # 1) 直接解析
    try:
        got = _coerce(json.loads(txt))
        if got:
            return got
    except Exception:
        pass
    # 2) 去 code fence
    m = _FENCE_RE.search(txt)
    if m:
        try:
            got = _coerce(json.loads(m.group(1).strip()))
            if got:
                return got
        except Exception:
            pass
    # 3) 抠第一个 [...] 数组
    i, j = txt.find("["), txt.rfind("]")
    if 0 <= i < j:
        try:
            got = _coerce(json.loads(txt[i:j + 1]))
            if got:
                return got
        except Exception:
            pass
    return None


def verify_preserved(segments: list[str], original: str,
                     *, min_ratio: float = 0.95) -> tuple[bool, float, bool]:
    """校验分段结果是否忠实保留原文。

    返回 (是否可用, 相似度, 是否完全一致)。
    """
    joined = "".join(segments)
    a, b = _norm(joined), _norm(original)
    if not a or not b:
        return False, 0.0, False
    if a == b:
        return True, 1.0, True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio >= min_ratio, ratio, False


# ---------------------------------------------------------------- 提示词

_COMMON_RULES = (
    "**只切分，不改写**：不得增删、替换、翻译、润色任何一个字。\n"
    "**不要输出解释**：直接输出 JSON 数组，不要代码块、不要前后缀。\n"
    "**断点位置**：只在完整句结尾（。！？…~）或话题转换处断开；"
    "绝不在逗号、顿号、分号前断开。\n"
    "**每段以完整语义收尾**：不要出现以「，」开头的段落。\n"
)

PRIVATE_SYSTEM = (
    "你是「私聊发言分段器」。把给定的角色回复切成若干条短消息。\n\n"
    "输出格式（严格遵守）：一个 JSON 字符串数组，例如\n"
    '["第一段内容", "第二段内容"]\n\n'
    "切分要求：\n"
    + _COMMON_RULES +
    "- 私聊可以切得细一些：一个语气/一个意思一条\n"
    "- （括号内的神态动作描写）与紧跟的台词放同一段\n"
    "{format_spec_block}"
)

GROUP_SYSTEM = (
    "你是「群聊发言分段器」。把给定的角色回复切成若干条短消息。\n\n"
    "输出格式（严格遵守）：一个 JSON 字符串数组，例如\n"
    '["第一段内容", "第二段内容"]\n\n'
    "切分要求：\n"
    + _COMMON_RULES +
    "- **群聊比私聊更克制**：切得更少、每条更短，避免刷屏\n"
    "- 拆成 2~4 段即可，不要过度拆分\n"
    "- 每段至少 {min_segment} 个字符，太碎的并进相邻段\n"
    "- 一口气说完的内容不要硬拆；只有一段时也返回单元素数组\n"
)

# 剧情向专用底线：只保留「守恒 + JSON 输出」。
# 刻意**不含** `_COMMON_RULES` 里的「只在完整句结尾断开」——剧情向的断点
# 是括号块本身，不是句末标点；留着那条会跟「括号单独成段」直接打架。
_COMMON_FIDELITY = (
    "**只切分，不改写**：不得增删、替换、翻译、润色任何一个字。\n"
    "**不要输出解释**：直接输出 JSON 数组，不要代码块、不要前后缀。\n"
)

# 剧情向（括号描写）专用：**机械规则**——每个括号一段、括号间说话一段，
# 说话无论多长都不再拆。信息模式（纯信息）沿用上面两份提示（"信息模式不变"）。
NARRATIVE_SPLIT_RULES = (
    "- 每个（括号内的神态动作描写）**单独成一段**\n"
    "- 括号之间的说话内容**单独成一段**\n"
    "- 说话内容无论多长都只作一段，绝不按句子/逗号/语气词再拆\n"
    "- 示例：原文"
    "「（香奈美拿起袋子）哇，引航者，这里面装着什么呀"
    "（香奈美捏了捏袋子）好软哦」应切成\n"
    '  ["（香奈美拿起袋子）", "哇，引航者，这里面装着什么呀",'
    ' "（香奈美捏了捏袋子）", "好软哦"]\n'
)

PRIVATE_NARRATIVE_SYSTEM = (
    "你是「私聊剧情向发言分段器」。把给定的角色回复切成若干条短消息。\n\n"
    "输出格式（严格遵守）：一个 JSON 字符串数组，例如\n"
    '["第一段内容", "第二段内容"]\n\n'
    "切分规则（机械执行，不需要考虑句子长短）：\n"
    + _COMMON_FIDELITY + NARRATIVE_SPLIT_RULES +
    "{format_spec_block}"
)

GROUP_NARRATIVE_SYSTEM = (
    "你是「群聊剧情向发言分段器」。把给定的角色回复切成若干条短消息。\n\n"
    "输出格式（严格遵守）：一个 JSON 字符串数组，例如\n"
    '["第一段内容", "第二段内容"]\n\n'
    "切分规则（机械执行，不需要考虑句子长短）：\n"
    + _COMMON_FIDELITY + NARRATIVE_SPLIT_RULES
)

# 四份提示词（可按 私聊/群聊 × 纯信息/剧情向 在 WebUI 编辑覆盖；
# 覆盖存在 `state/_segment_prompts.json`，恢复默认即删覆盖回落到本表）。
DEFAULT_PROMPTS: dict[str, str] = {
    "private_plain": PRIVATE_SYSTEM,
    "private_narrative": PRIVATE_NARRATIVE_SYSTEM,
    "group_plain": GROUP_SYSTEM,
    "group_narrative": GROUP_NARRATIVE_SYSTEM,
}


def prompt_key(is_private: bool, msg_mode: int) -> str:
    """(是否私聊, 发言模式) → 提示词键。"""
    mode = "narrative" if int(msg_mode or 0) == MODE_NARRATIVE else "plain"
    return ("private" if is_private else "group") + "_" + mode


class SegmentAgent:
    """按需调用分段子代理；不可用或校验失败时返回空列表，由调用方回退。"""

    def __init__(self, context: Any, config: Any,
                 prompts_store: Any | None = None) -> None:
        self.context = context
        self.config = config
        # 提示词覆盖存储（dict-like，通常就是 state/_segment_prompts.json 的
        # JsonMap）；缺省 {}= 全部走内置默认。
        # 注意不能用 `or {}`：空 JsonMap 的 __len__==0 会触发 falsy，
        # 被塌成普通 dict，之后 set_prompt 就永远"存储未初始化"了。
        self.prompts_store = {} if prompts_store is None else prompts_store
        self.stats = {"calls": 0, "used": 0, "lenient": 0, "rejected": 0,
                      "parse_failed": 0, "skipped_short": 0, "errors": 0,
                      "fallback": 0, "last_error": ""}

    # ------------------------------------------------------------ 提示词
    def _prompt(self, key: str) -> str:
        """取某键提示词：覆盖存储有值用覆盖，否则用内置默认。"""
        default = DEFAULT_PROMPTS.get(key, "")
        try:
            v = self.prompts_store.get(key)
        except Exception:
            v = None
        return str(v).strip() if str(v or "").strip() else default

    def prompts(self) -> dict:
        """四份提示词的当前值 + 是否被自定义（驱动 WebUI 编辑器）。"""
        out = {}
        for key, default in DEFAULT_PROMPTS.items():
            cur = self._prompt(key)
            out[key] = {"current": cur, "customized": cur != default}
        return out

    def set_prompt(self, key: str, text: str) -> bool:
        """写入某份提示词的覆盖；空文本 = 清除覆盖（回落到内置默认）。"""
        if key not in DEFAULT_PROMPTS:
            return False
        if not hasattr(self.prompts_store, "set"):
            raise RuntimeError("分段提示词存储未初始化")
        t = (text or "").strip()
        if t:
            self.prompts_store.set(key, t, persist=False)
        else:
            self.prompts_store.pop(key, persist=False)
        self.prompts_store.save()
        return True

    def reset_prompt(self, key: str | None = None) -> None:
        """恢复默认：`key=None` 时全部重置。"""
        if not hasattr(self.prompts_store, "pop"):
            return
        keys = [key] if key else list(DEFAULT_PROMPTS)
        for k in keys:
            if k in DEFAULT_PROMPTS:
                self.prompts_store.pop(k, persist=False)
        self.prompts_store.save()

    # ------------------------------------------------------------ 配置
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.config.get(key, default)
        except Exception:
            return default
        return default if v is None else v

    def enabled_for(self, *, is_private: bool) -> bool:
        """该场景是否启用分段子代理。

        **流式传输开启时整体停用**（`enable_streaming`）：文本已经在往外流，
        等一次 LLM 分段回来早就晚了，子代理只吃"完整回复"、流式轮次里根本
        没有完整回复。WebUI 侧同步把「私聊/群聊分段使用子代理」那组开关
        隐藏掉（见 `_conf_schema.json` 里这几项的 `condition`），这里再做
        一道硬闸 —— 免得以后长了新的调用点，绕开界面又把它叫起来。
        """
        if bool(self._cfg("enable_streaming", False)):
            return False
        if is_private:
            return bool(self._cfg("private_format_agent", True))
        return bool(self._cfg("group_format_agent", True))

    def threshold_for(self, *, is_private: bool) -> int:
        """低于阈值不调用（短回复硬切没意义，还白花一次调用）。"""
        key = ("private_format_threshold" if is_private
               else "group_format_threshold")
        default = 0 if is_private else 20
        try:
            return int(self._cfg(key, default) or 0)
        except (TypeError, ValueError):
            return default

    def _verify_ratio(self) -> float:
        try:
            return float(self._cfg("segment_verify_ratio", 0.95) or 0.95)
        except (TypeError, ValueError):
            return 0.95

    # ------------------------------------------------------------ 主接口
    async def segment(self, text: str, *, is_private: bool,
                      umo: str = "", msg_mode: int = MODE_PLAIN,
                      force: bool = False) -> list[str]:
        """返回分段列表；不可用时返回 []（调用方回退本地规则）。

        返回值已经过标点清理（`clean_segments`），可直接进消息链。
        `msg_mode` 决定用哪份提示词（纯信息/剧情向，见 prompt_key）。

        `force=True` 时**无视 `enabled_for` 闸门** —— 只给 WebUI 的
        「分段子代理试跑」用：流式开着时子代理整体停用，但用户仍可能想看看
        "它会怎么切"来调那四份提示词。
        """
        text = (text or "").strip()
        if not text:
            return []
        if not force and not self.enabled_for(is_private=is_private):
            return []
        thr = self.threshold_for(is_private=is_private)
        if thr and len(text) < thr:
            self.stats["skipped_short"] += 1
            return []

        self.stats["calls"] += 1
        _kind = "私聊" if is_private else "群聊"
        _t0 = time.time()
        try:
            raw = await self._ask(text, is_private=is_private, umo=umo,
                                  msg_mode=msg_mode)
        except asyncio.TimeoutError:
            self.stats["errors"] += 1
            self.stats["fallback"] += 1
            self.stats["last_error"] = "timeout"
            logger.info(
                f"[chatcore_neo] 分段代理·超时（回退本地规则）{_kind} "
                f"umo={umo!r} 原文 {len(text)} 字 "
                f"耗时 {(time.time()-_t0)*1000:.0f}ms")
            return []
        except Exception as e:
            self.stats["errors"] += 1
            self.stats["fallback"] += 1
            self.stats["last_error"] = repr(e)
            logger.info(
                f"[chatcore_neo] 分段代理·异常（回退本地规则）{_kind} "
                f"umo={umo!r} 原文 {len(text)} 字 "
                f"耗时 {(time.time()-_t0)*1000:.0f}ms 错误={e!r}")
            return []

        _ms = (time.time() - _t0) * 1000
        segments = parse_segments(raw)
        if not segments:
            self.stats["parse_failed"] += 1
            self.stats["fallback"] += 1
            logger.info(
                f"[chatcore_neo] 分段代理·解析失败（回退本地规则）{_kind} "
                f"umo={umo!r} 原文 {len(text)} 字 耗时 {_ms:.0f}ms "
                f"模型原始输出={raw[:80]!r}")
            return []

        ok, ratio, exact = verify_preserved(
            segments, text, min_ratio=self._verify_ratio())
        if not ok:
            self.stats["rejected"] += 1
            self.stats["fallback"] += 1
            self.stats["last_error"] = f"内容被改写 (相似度 {ratio:.3f})"
            logger.info(
                f"[chatcore_neo] 分段代理·内容被改写（回退本地规则）{_kind} "
                f"umo={umo!r} 相似度={ratio:.3f} 段数={len(segments)} "
                f"耗时 {_ms:.0f}ms")
            return []
        if not exact:
            self.stats["lenient"] += 1

        cleaned = clean_segments(segments)
        if not cleaned:
            self.stats["fallback"] += 1
            logger.info(
                f"[chatcore_neo] 分段代理·清理后为空（回退本地规则）{_kind} "
                f"umo={umo!r} 耗时 {_ms:.0f}ms")
            return []
        self.stats["used"] += 1
        logger.info(
            f"[chatcore_neo] 分段代理·返回 {_kind} umo={umo!r} "
            f"原文 {len(text)} 字 → {len(cleaned)} 段 "
            f"各段长度={[len(s) for s in cleaned]} "
            f"相似度={ratio:.3f} 严格守恒={exact} 耗时 {_ms:.0f}ms")
        return cleaned

    async def _ask(self, text: str, *, is_private: bool, umo: str = "",
                   msg_mode: int = MODE_PLAIN) -> str:
        model = str(self._cfg(
            "private_format_model" if is_private else "group_format_model",
            "") or "")
        timeout = float(self._cfg(
            "private_format_timeout" if is_private else "group_format_timeout",
            10) or 10)
        # umo 必须透传：模型留空时要靠它解析"会话主模型"，
        # 传空串会让留空=永远拿不到 provider，子代理静默失效
        prov = resolve_subagent_provider(self.context, model, umo)
        if prov is None or not hasattr(prov, "text_chat"):
            raise RuntimeError("无可用 provider")
        if is_private:
            from .paths import FRAMEWORK_DIR
            from .persona.outline import strip_frontmatter
            spec = ""
            p = FRAMEWORK_DIR / "format" / "narrative.md"
            if p.exists():
                try:
                    spec = strip_frontmatter(p.read_text(encoding="utf-8"))[:1500]
                except Exception:
                    spec = ""
            # 兜底：规范文件若又混进旧 `§n` 协议，对分段子代理无意义，剥掉避免误导
            # （提示词侧已不再教 `§n`，这里是防止回归；`n`/数字一起吃掉）
            spec = re.sub(r"§\s*(?:\d+|[nN])?", "", spec).strip()
            block = f"\n私聊格式规范参考（只看排版要求）：\n{spec}\n" if spec else ""
            system = self._prompt(prompt_key(is_private, msg_mode)).replace(
                "{format_spec_block}", block)
        else:
            try:
                min_seg = int(self._cfg("group_format_min_segment", 18) or 18)
            except (TypeError, ValueError):
                min_seg = 18
            system = self._prompt(prompt_key(is_private, msg_mode)).replace(
                "{min_segment}", str(min_seg))

        user = (f"【待分段的回复原文】（共 {len(text)} 字符）\n{text}\n\n"
                "请只输出 JSON 字符串数组：")
        resp = await asyncio.wait_for(
            prov.text_chat(system_prompt=system, prompt=user, temperature=0),
            timeout=timeout)
        return getattr(resp, "completion_text", "") or ""

    def report(self) -> dict:
        return dict(self.stats)
