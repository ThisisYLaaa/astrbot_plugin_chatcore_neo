# -*- coding: utf-8 -*-
"""流式输出层：边收边切，实时发出。

与非流式的分工
--------------
* **非流式**：整段回复到手 → `core/output.py` 的正则 / 分段子代理 →
  由一个 `on_decorating_result` 钩子整批交回框架。
* **流式**：文本是一点点长出来的，必须**边收边切边发**。切出来的每一段
  仍然走同一套 `core/output.py` 正则（`§n`、`[[at:]]`、句尾标点清理），
  所以两种模式发出去的消息形态是一致的 —— 差别只在"什么时候发"。

本模块只做**切分**（纯文本进出，不 import 框架），发送在 `main.py` 的
`_stream_deliver()` 里完成。这样切分规则可以脱离 AstrBot 单测。

为什么要"安全前缀"（`safe_prefix_len`）
--------------------------------------
流式增量会把任何东西从中间劈开：`[[at:123` / `（香奈美撑着`。
在这些位置切开的话，标记会被截成两半（半截发出去、半截留在下一段），
剧情向的括号块也会被劈碎。`safe_prefix_len()` 把"最后一个还没闭合的
开括号之后"的部分留到下一轮，等它长完整再切。

括号不闭合怎么办
----------------
模型写坏是常态（实测出现过没闭合的 `[@江一楠 这条呀…`）。
所以留手有两个上限，超了就判定为坏文本、不再卡住发送：
方括号按标记长度上限（`_MARK_MAX`，与 `_MARK_RE` 的 `{1,64}` 对齐），
其余按 `hold_limit`（默认 `max(200, max_len*4)`）。

剧情向（`MODE_NARRATIVE`）
--------------------------
切点与 `core/output.py:split_narrative()` 同一套机械规则：括号描写单独
成段、括号之间的说话单独成段、说话不细拆。唯一的差别是**说话过长**
（超过 `max_len`）时在句末标点处断开 —— 流式不能无限攒着不说话，
否则"实时"就没意义了；这条只影响超长说话，普通长度仍是一整段。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .output import LEADING_CONTINUATION_PUNCT, legacy_marks_to_newlines
from .output import speaker_header_re
from .session_params import MODE_NARRATIVE

import re

# 句末标点（与 `output._SENT_END` 一致）：换行也算句末
SENT_END = "。！？!?…~～\n"

# 可能"还在成形中"的括号对（`[[at:]]` 标记与剧情向括号描写都用得上）
_PAIRS = {"[": "]", "【": "】", "（": "）", "(": ")"}
_CLOSE = {v: k for k, v in _PAIRS.items()}
_MARK_OPEN = "[【"
_PAREN_OPEN = "（("

# 方括号标记最长 64 字符（与 `output._MARK_RE` 的 `{1,64}` 对齐）。
# 超过这个长度仍未闭合，就当模型写了个孤零零的 `[`，不再卡住发送。
_MARK_MAX = 64

# `§n` 这种旧分段标记**可能被增量劈开**（`§` 与 `n` 分两次到）。
# 若立刻把孤零零的 `§` 换成换行，后到的 `n` 就变成一个多出来的字母
# （实测过：`§n` → `\n` + `n`）。所以结尾处"还没写完的标记"先扣在
# `carry` 里等下一个增量。
_CARRY_RE = re.compile(r"§\s*\d*$")


# ---------------------------------------------------------------- 安全前缀

def _match_close(text: str, i: int) -> "int | None":
    """返回与 `text[i]` 配对的闭括号**之后**的下标；找不到返回 None。

    只配对同一种括号（`（` 配 `）`、`(` 配 `)`），中英文混写时按字面处理。
    """
    opener = text[i]
    closer = _PAIRS.get(opener)
    if closer is None:
        return None
    depth = 0
    for k in range(i, len(text)):
        ch = text[k]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return k + 1
    return None


def safe_prefix_len(text: str, *, hold_limit: int = 0) -> int:
    """返回"可以安全切分"的长度：尾部还没长完的部分留到下一轮。

    * 未闭合的 `[` / `【`：可能是还没写完的 `[[at:QQ]]` 标记 → 留着；
      超过 `_MARK_MAX` 个字符仍未闭合则判定为坏文本（不卡住发送）。
    * 未闭合的 `（` / `(`：剧情向的括号块还没结束 → 留着；
      `hold_limit > 0` 时超过该长度也判定为坏文本（模型漏了闭括号）。
    """
    stack: list = []
    for i, ch in enumerate(text):
        if ch in _PAIRS:
            stack.append((ch, i))
        elif ch in _CLOSE:
            if stack and stack[-1][0] == _CLOSE[ch]:
                stack.pop()
    if not stack:
        return len(text)
    hold = len(text)
    for opener, idx in stack:
        left = len(text) - idx
        if opener in _MARK_OPEN:
            if left > _MARK_MAX:
                continue
        elif hold_limit > 0 and left > hold_limit:
            continue
        hold = min(hold, idx)
    return min(hold, len(text))


def _sent_ends(text: str, start: int) -> list:
    """所有"完整句"的切点（句末标点之后）。

    `start` 就是"碎片合并下限"：早于它的句末标点不切，留给后面的内容
    一起发（否则会出现「嗯。」这种一两个字一条的碎嘴）。
    """
    out: list = []
    i = max(1, int(start or 1))
    while i < len(text):
        if text[i - 1] in SENT_END:
            out.append(i)
        i += 1
    return out


def next_block(text: str, start: int = 0) -> "tuple[int, int] | None":
    """找 `start` 之后第一个**已闭合**的括号块：返回 (开括号下标, 闭括号后一位)。

    没有闭合的括号（模型写坏）直接跳过 —— 它当普通文字处理，不能因为一个
    漏掉的 `）` 把后面所有内容都卡住。
    """
    i = max(0, int(start or 0))
    while True:
        j = -1
        for k in range(i, len(text)):
            if text[k] in _PAREN_OPEN:
                j = k
                break
        if j < 0:
            return None
        end = _match_close(text, j)
        if end is not None:
            return j, end
        i = j + 1


# ---------------------------------------------------------------- 切段器

@dataclass
class StreamSplitter:
    """流式切段器：喂增量文本，吐出"现在就能发"的段。

    * `enabled=False`（= 关掉消息分段）：什么都不切，全部攒到 `flush()`，
      由调用方一次发完（流式仍然照跑，只是不分条）。
    * `names`：多人格同台的显示名集合。**名牌（`【角色名】`）本身就是一个
      切点** —— 它跟着自己那段内容走，所以既不会挂在上一条的末尾
      （实测坏形态：`…说得那么响 【知更鸟·晴歌】`），也不会自成一个空段
      （见 `_cut` / `_bare_header`，与 `output.normalize_speaker_segments`
      同一套口径）。
    """

    max_len: int = 120
    min_len: int = 8
    mode: int = 1
    names: tuple = ()
    enabled: bool = True
    buf: str = ""
    carry: str = ""
    stats: dict = field(default_factory=lambda: {"feeds": 0, "cuts": 0,
                                                 "chars": 0, "held": 0})

    # ------------------------------------------------------------ 对外接口
    def feed(self, text: str) -> list:
        """喂一段增量，返回此刻已经成形的段（0 个或多个）。"""
        t = text or ""
        if not t:
            return []
        self.stats["feeds"] += 1
        self.stats["chars"] += len(t)
        t = self.carry + t
        self.carry = ""
        m = _CARRY_RE.search(t)
        if m:
            # 结尾是个"还没写完的 `§n`"：扣着，等下一个增量
            self.carry = t[m.start():]
            t = t[:m.start()]
        self.buf += legacy_marks_to_newlines(t)
        return self.drain()

    def drain(self) -> list:
        """只吐已经成形的段，不强行收尾。"""
        if not self.enabled:
            return []
        out: list = []
        while True:
            seg = self._take()
            if not seg:
                break
            out.append(seg)
            self.stats["cuts"] += 1
        return out

    def flush(self) -> list:
        """收尾：把剩下的全部吐出（不再等切点）。

        收尾若只剩名牌（模型漏写了内容 / 流刚好断在名字后面），**保留**这条
        —— 绝不吞字（与 `output.normalize_speaker_segments` 同口径）。
        """
        if self.carry:
            # 流结束时还扣着的半个 `§n`：就地转成边界，绝不漏给用户
            self.buf += legacy_marks_to_newlines(self.carry)
            self.carry = ""
        if not self.enabled:
            rest = self.buf.strip()
            self.buf = ""
            return [rest] if rest else []
        out = self.drain()
        rest = self.buf.strip()
        self.buf = ""
        if rest:
            out.append(rest)
            self.stats["cuts"] += 1
        return out

    def leftover(self) -> str:
        """还攒在缓冲区里的原文（调试/日志用）。"""
        return self.buf + self.carry

    # ------------------------------------------------------------ 内部
    def _hold_limit(self) -> int:
        """括号一直不闭合时最多卡多久（字符）。"""
        return max(200, int(self.max_len or 0) * 4)

    def _take(self) -> str:
        """从缓冲区取一段可发的内容；取不到返回空串。"""
        while True:
            if not self.buf:
                return ""
            safe = self.buf[:safe_prefix_len(self.buf,
                                             hold_limit=self._hold_limit())]
            if not safe:
                self.stats["held"] += 1
                return ""
            cut = self._cut(safe)
            if cut <= 0:
                return ""
            seg, self.buf = self.buf[:cut], self.buf[cut:]
            if seg.strip():
                return seg
            # 切出来的只有空白（例如段首的换行）：丢掉继续切，不能卡住
            self.stats["blank"] = self.stats.get("blank", 0) + 1

    def _cut(self, safe: str) -> int:
        """第一个**合法**的切点；0 = 此刻还不该切。

        合法性只有一条：切出来的段**不能"只有名牌"**。名牌（`【角色名】`）
        必须跟着它后面那段内容走 —— 否则用户会收到一条只写着名字的消息，
        而那句台词被甩到了后面。实测过的坏形态：
            `尴尬的…说得那么响 【知更鸟·晴歌】` / `（她把谱本抱得紧了些…）`
        正确形态：名牌贴在自己那段内容的开头。
        """
        for idx in self._positions(safe):
            if self._bare_header(self.buf[:idx]):
                continue
            return idx
        return 0

    def _positions(self, safe: str) -> list:
        """所有候选切点（升序、去重）。

        候选来源：
          * **名牌出现的位置**：名牌自己起一段（贴住后面那句台词）；
          * 剧情向：括号块前（说话自成一段）/ 括号块后（描写自成一段）；
          * 纯信息：句末标点；
          * 剧情向的说话过长（> `max_len`）时也让步到句末标点；
          * 实在没有边界且已超长：硬切。
        """
        pos: set = set()
        rx = speaker_header_re(self.names)
        if rx is not None:
            for m in rx.finditer(safe):
                if m.start() > 0:
                    pos.add(m.start())
        over = len(safe) > int(self.max_len or 0)
        if self.mode == MODE_NARRATIVE:
            # 括号块：**块前与块后都收进来** —— 块前那个若只切出名牌
            # （`【香奈美】（动作）`），`_cut` 会退到"块后"，名牌就跟内容走了。
            nxt = 0
            while True:
                blk = next_block(safe, nxt)
                if blk is None:
                    break
                start, end = blk
                if start > 0:
                    pos.add(start)
                pos.add(end)
                nxt = end
            if over:
                pos.update(_sent_ends(safe, self.min_len))
        else:
            pos.update(_sent_ends(safe, self.min_len))
        if over:
            pos.add(self._hard_cut(safe))
        return sorted(p for p in pos if 0 < p <= len(safe))

    def _bare_header(self, text: str) -> bool:
        """这段是不是"只有名牌"（去掉名牌就什么都不剩）。"""
        rx = speaker_header_re(self.names)
        if rx is None:
            return False
        body = (text or "").strip()
        if not body:
            return False
        return rx.sub("", body).strip() == ""

    def _hard_cut(self, safe: str) -> int:
        """超长硬切：把紧跟的延续性标点（，、；）留在上一段。

        否则下一段会以「，」开头 —— `clean_segments` 会把它 lstrip 掉，
        那半句的语气就丢了。
        """
        cut = int(self.max_len or 0)
        while cut < len(safe) and safe[cut] in LEADING_CONTINUATION_PUNCT:
            cut += 1
        return cut


def split_stream_text(text: str, *, max_len: int = 120, min_len: int = 8,
                      mode: int = 1, names=(), enabled: bool = True) -> list:
    """一次性把整段文本按流式规则切开（给测试/试跑用，等价于
    `feed(text)` + `flush()`）。"""
    sp = StreamSplitter(max_len=max_len, min_len=min_len, mode=mode,
                        names=tuple(names or ()), enabled=enabled)
    return sp.feed(text) + sp.flush()
