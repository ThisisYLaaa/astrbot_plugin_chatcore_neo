# -*- coding: utf-8 -*-
"""输出层：分段 / 标点清理 / 标记 / 撤回取消。

分段为什么不用标记协议
----------------------
旧实现在文本里插 `§n` 标记，再由 `split_segments` 按 `§n|\\n+` 切开。
这条路有两个实际问题：
  1. **标记会漏进消息**：一旦子代理忘了删、或主 agent 自己写了 `§n`，
     它就原样发给用户。旧环境为此专门写了 `_normalize()` 去剥它。
  2. **换行与标记语义混淆**：真实换行和分段标记用同一个切分规则，
     没法区分"这里就是一段"和"这里只是换行"。

新实现改成**结构化输出**：分段子代理直接返回 JSON 数组
`["句子1", "句子2"]`（见 `core/segment_agent.py`），
数组本身就是发送用的段列表 —— 不需要标记，也不可能漏字符。

> **重建说明（2026-09-12）**：本文件曾被一次错误的批量编辑截断
> （只保留了 `_MARK_RE` 那行之前的内容，`parse_markers` / `strip_markers` /
> `clean_segments` / `split_raw` / `split_segments` / `RecallGuard` 全丢）。
> 下面这些函数是按 `tests/test_w3_w4.py` 的**行为契约** + 旧插件
> `astrbot_plugin_chatcore_lite/formatter.py` 重建的。
> 能精确重建是因为测试把每条规则都钉死了（`test_clean_*` / `test_split_*`）——
> 这次是"测试即规格"把我救回来的。

分工
----
* `split_raw()`      —— 纯切分，**内容守恒**（拼回去 == 原文），不碰标点
* `clean_segments()` —— 只做标点清理与段首延续标点合并
* `split_segments()` —— 上面两步的组合（默认清理）
* `parse_markers()`  —— 从模型输出里抽 `[[at:QQ]]` / `[[reply:消息id]]`
* `strip_markers()`  —— 把标记从正文里拿掉
* `fix_literal_mentions()` —— 模型手写 `[@昵称]` 的兜底转换
"""
from __future__ import annotations

import re
import time

# 模型输出的 At / 引用标记。
#
# **必须容错**：模型经常写坏 —— 少一个右括号（实测 `[[reply:10006]`）、
# 用全角括号/冒号、只写一层方括号。严格只认 `[[at:x]]` 的后果是：
# 既没转成真组件，也没被剥掉，**原始标记文本直接发给用户**。
# 所以这里接受 1~2 个开括号、1~2 个闭括号、半角或全角冒号。
# `at` 后面必须是 QQ 号（纯数字）；`reply` 后面是消息 id，
# **不同平台不一定是数字**（本项目的合成调试事件就是 `dbg-...`），
# 所以只排除空白和括号，不限制字符集。
#
# **量词必须是贪婪的 `{1,64}`，不能写 `{1,64}?`**：值字符类本身已经排除了
# 括号，贪婪匹配会自然停在第一个括号处；写成懒惰则只吃 1 个字符就收工，
# 于是 `[[reply:dbg-1789188327022]]` 会被截成 `Reply(id="d")`，
# 剩下的 `bg-1789188327022]` 漏进正文 —— 比严格匹配还糟。
_MARK_RE = re.compile(
    r"[\[【]{1,2}\s*(at|reply)\s*[:：]\s*([^\]】\s]{1,64})\s*[\]】]{0,2}",
    re.IGNORECASE)

# 兜底清扫：任何"看起来像标记"的残渣都不能漏出去
# （正文里正常不会出现 `at:` / `reply:` 紧跟一串 id 的形态）
_MARK_RESIDUE = re.compile(
    r"[\[【]{1,2}\s*(?:at|reply)\s*[:：]\s*[^\]】\s]{0,64}\s*[\]】]{0,2}",
    re.IGNORECASE)

# 模型手写的「@昵称」（`[@张三]` / `【@张三】`）。协议是 `[[at:QQ号]]`，
# 但模型经常自己发明这种写法 —— 输出层要兜住，否则坏文本会原样发出去
# （实测出现过没闭合的 `[@江一楠 这条呀…`）。
_LITERAL_AT = re.compile(r"[\[【]\s*@\s*([^\]】\s][^\]】]{0,31}?)\s*[\]】]")

# 旧环境的**分段标记**协议（`§n` / `§1` / `§ 2`）。
#
# 新环境不用标记：分段子代理直接返回 JSON 段数组（见 segment_agent.py），
# 所以提示词里已经不再教模型写它。但**旧对话历史 / 模型惯性**仍可能让它冒出来，
# 而链路上没有任何环节会删 —— 结果就是 `§n` 原样出现在用户收到的消息里
# （2026-09-19 实测日志）。这里做最后一道闸。
#
# 处理方式是**当作分段边界**（换行）而不是直接删掉：旧语义里它就是"这里断开"，
# 直接删会把两句话粘成一句（「好累啊不过还挺开心的」）。转成换行后，
# 本地切分规则（`split_raw` 认 `\n` 为句末）与分段子代理都能正确断开。
#
# 标记本体是 `§n`（n 是字母，不是占位符），另有 `§1` / `§ 2` 这类变体；
# 正则必须把后面的 `n`/数字一起吃掉，否则会残留一个孤零零的 `n`
# （实测 `§n` 清完变成 `\nn`）。
_LEGACY_SEG_MARK = re.compile(r"§\s*(?:\d+|[nN])?")

# 句末标点（判定"一个完整句"结束）。换行也算。
_SENT_END = "。！？!?…~～\n"

DEFAULT_MAX_LEN = 120
DEFAULT_MIN_LEN = 8

# 段首为延续性标点时，说明断点切在了从句中间，需合并回上一段，
# 避免出现以逗号开头的坏消息（例如 "快睡吧§n，明天还有演出…"）
LEADING_CONTINUATION_PUNCT = ("，", "、", "；", ",")


# ---------------------------------------------------------------- 标记

def parse_markers(text: str) -> dict:
    """抽出标记，返回 {"at": [...], "reply": [...]}（去重，保持出现顺序）。

    容错：`[[at:1]]` / `[[at:1]` / `[at:1]` / `【at:1】` 都认。
    值必须是纯数字（QQ / 消息 id），否则丢弃 —— 免得把
    `[[at:某某]]` 这种瞎写的当成 id 塞进组件。
    """
    out: dict = {"at": [], "reply": []}
    for m in _MARK_RE.finditer(text or ""):
        kind = m.group(1).lower()
        val = (m.group(2) or "").strip()
        val = val.strip().strip("]】")
        if not val:
            continue
        # `at` 必须是 QQ 号（纯数字），否则会生成坏组件；
        # `reply` 接受任意无空白/无括号的 id。
        if kind == "at" and not val.isdigit():
            continue
        if val not in out[kind]:
            out[kind].append(val)
    return out


def strip_markers(text: str) -> str:
    """把标记从正文里拿掉，并收拾留下的多余空白。

    **两道**：先按容错正则去掉正规/半坏的标记，再用 `_MARK_RESIDUE`
    扫一遍残渣。只做一道的话，模型写歪一点就会把 `[[reply:123]`
    这种原文直接发给用户（实测发生过）。
    """
    cleaned = _MARK_RE.sub("", text or "")
    cleaned = _MARK_RESIDUE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"[ \t]+([，。！？；、,.!?;])", r"\1", cleaned)
    # 模型多写一个括号时，剥完会剩个孤立的 `]` / `】`
    cleaned = re.sub(r"^[\]】\[]{1,3}\s*", "", cleaned)
    cleaned = re.sub(r"\s*[\[【]{1,3}$", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned.strip()


def legacy_marks_to_newlines(text: str) -> str:
    """把 `§n` 就地换成换行（= 分段边界），**不动其他任何字符**。

    与 `strip_legacy_segment_marks()` 的区别：这个不做 strip / 折叠空白，
    因而可以安全地用在**流式增量**上（增量开头结尾的空格可能属于
    上一句/下一句，strip 掉就把内容改了）。
    """
    s = text or ""
    if "§" not in s:
        return s
    return _LEGACY_SEG_MARK.sub("\n", s)


def strip_legacy_segment_marks(text: str) -> str:
    """把旧 `§n` 分段标记转成换行（= 分段边界），绝不让它流到用户。

    只做"标记 → 边界"的转换，不碰任何正常文字；没有 `§` 时零开销。
    """
    s = text or ""
    if "§" not in s:
        return s
    s = legacy_marks_to_newlines(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n[ \t]+", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def fix_literal_mentions(text: str, *, uid: str = "",
                         user_name: str = "") -> tuple:
    """把模型手写的 `[@昵称]` 规范成标记；返回 (新文本, 需要 @ 的 QQ 列表)。

    * 昵称能对上本轮说话人 → 转成 `[[at:uid]]`（交给正常流程变真组件）
    * 对不上 → 去掉括号，只留纯文本（不当 @ 用，也不留坏括号）
    * 没闭合的 `[@xxx` → 去掉 `[@`
    """
    out = text or ""
    qqs: list = []
    name = (user_name or "").strip()

    def _sub(m: "re.Match") -> str:
        got = (m.group(1) or "").strip()
        if uid and name and got == name:
            qqs.append(uid)
            return f"[[at:{uid}]]"
        if uid and got == uid:
            qqs.append(uid)
            return f"[[at:{uid}]]"
        return got            # 不认识的人：退化成纯文本，不留坏括号

    out = _LITERAL_AT.sub(_sub, out)
    # 没闭合的：只删掉 "[@" / "【@" 这两个字符
    out = re.sub(r"[\[【]\s*@", "", out)
    return out, qqs


# ---------------------------------------------------------------- 切分

def _sentences(text: str) -> list:
    """按句末标点切成"句子"（标点跟在句尾）。换行也算句末。"""
    out: list = []
    buf = ""
    for ch in text or "":
        buf += ch
        if ch in _SENT_END:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def split_raw(text: str, *, max_len: int = DEFAULT_MAX_LEN,
              min_len: int = DEFAULT_MIN_LEN) -> list:
    """纯切分：每段 <= max_len，**内容守恒**（拼回去等于原文）。

    * 优先在句末标点处断
    * 单句就超长 → 硬切
    * 过短的段（< min_len）尽量并回上一段
    * 空文本返回 `[]`
    """
    if not text or not text.strip():
        return []
    max_len = max(1, int(max_len or DEFAULT_MAX_LEN))
    min_len = max(0, int(min_len or 0))

    out: list = []
    buf = ""
    for sent in _sentences(text):
        if not sent:
            continue
        if len(sent) > max_len:
            # 单句超长：先把攒着的吐出来，再硬切这一句
            if buf:
                out.append(buf)
                buf = ""
            while len(sent) > max_len:
                out.append(sent[:max_len])
                sent = sent[max_len:]
            buf = sent
            continue
        if buf and len(buf) + len(sent) > max_len:
            out.append(buf)
            buf = ""
        buf += sent
    if buf:
        out.append(buf)

    # 过短的段并回上一段（不超 max_len 才并）
    if min_len > 0:
        merged: list = []
        for s in out:
            if merged and len(s) < min_len and \
                    len(merged[-1]) + len(s) <= max_len:
                merged[-1] += s
            else:
                merged.append(s)
        out = merged
    return out


def clean_segments(segs: list) -> list:
    """标点清理（对应 `rules_base.md` 的句尾要求）。

    * 句尾不加句号：去掉**单个** `。`/`，`/`,`
    * `。。。` / `，，` 这类连续标点是情绪，保留
    * 段首延续性标点（`，、；,`）说明断点切在从句中间 → 并回上一段
    * 首段若以延续性标点开头 → 直接剥掉
    * 空段丢弃
    """
    result: list = []
    for raw in (segs or []):
        s = (raw or "").strip()
        if not s:
            continue
        # 上一段若被清成空串则丢弃，避免把延续标点并到空串上
        while result and not result[-1]:
            result.pop()
        if result and s[:1] in LEADING_CONTINUATION_PUNCT:
            s = result.pop() + s          # 标点自然落在上一段末尾
        else:
            s = s.lstrip("".join(LEADING_CONTINUATION_PUNCT))
        if not s:
            continue
        if re.search(r"[。，,]{2,}$", s):
            result.append(s)              # 。。。/，，保留
        else:
            result.append(re.sub(r"[。，,]$", "", s))
    return result


def split_segments(text: str, *, max_len: int = DEFAULT_MAX_LEN,
                   min_len: int = DEFAULT_MIN_LEN,
                   clean: bool = True) -> list:
    """切分 + （默认）标点清理。`clean=False` 得到保留原标点的原始切分。"""
    raw = split_raw(text, max_len=max_len, min_len=min_len)
    return clean_segments(raw) if clean else raw


# 剧情向（括号描写）切分：**机械规则**，与分段子代理的剧情向提示词同一套。
# 每个括号块一段、括号间说话一段、说话无论多长都不再拆。
_NARR_PAIR_RE = re.compile(r"[（(][^（）()]*[)）]")


def split_narrative(text: str) -> list:
    """剧情向切分：括号描写 / 括号间说话 各自成段（说话不细拆）。

    内容守恒：把段落拼回去等于原文（仅去空白差异）。无括号时整段返回。
    """
    t = (text or "").strip()
    if not t:
        return []
    out: list = []
    pos = 0
    for m in _NARR_PAIR_RE.finditer(t):
        if m.start() > pos:
            seg = t[pos:m.start()].strip()
            if seg:
                out.append(seg)
        out.append(m.group(0).strip())
        pos = m.end()
    tail = t[pos:].strip()
    if tail:
        out.append(tail)
    return out


# ---------------------------------------------------------------- 角色名前缀归位
#
# 多人格同台时，模型被要求「每条消息都以【角色名】开头」标明说话人
# （见 `core/costars.py:build_block`）。但分段规则只认括号与句末标点 ——
# 括号之间的说话会被当成一整段，于是**下一个角色的名牌就挂在该段末尾**：
# 香奈美说完台词后紧跟的 `【艾卡】` 被切进香奈美那一段，用户看到的是
# 香奈美在说「【艾卡】」。下面这组函数把名牌归位：名牌必须贴在**它自己
# 那段内容的开头**，既不能留在上一段末尾，也不能自成一个空段。
#
# **只在已知角色名集合里匹配**（同台的 [主角 + 副人格] 显示名），不做
# 「看到【】就当名牌」的宽松匹配 —— 正文里 `【公告】` / `【情绪】` 这类
# 方括号到处都是，宽松匹配会把正常文本切碎。


def speaker_header_re(names) -> "re.Pattern | None":
    """角色名前缀正则（`【香奈美】` / `[香奈美]`，允许名字两侧空白）。

    `names` 为空（或全是空白）→ 返回 `None`（= 不做任何处理）。
    """
    cand = [str(n) for n in (names or []) if str(n or "").strip()]
    if not cand:
        return None
    # 长名字优先：「艾」不会抢在「艾卡」前面把名字吃短
    alt = "|".join(re.escape(n) for n in sorted(cand, key=len, reverse=True))
    return re.compile(r"[【\[]\s*(?:" + alt + r")\s*[】\]]")


def normalize_speaker_segments(segs: list, names) -> list:
    """把角色名前缀归位到它所属段落的开头（多人格同台用）。

    * 段中间 / 段尾出现的名牌 → 从那里断开，名牌跟着**后面**的内容走
    * 只含名牌的段 → 挂起，并进下一段（名牌成为下一段的前缀）
    * 收尾只剩名牌（模型漏写内容）→ 保留，绝不吞字
    * 名字集合为空 / 正文里没有名牌 → **原样返回**（零影响）

    内容守恒：拼回去等于原文（仅去空白差异，不增删一个字）。
    """
    rx = speaker_header_re(names)
    src = list(segs or [])
    if rx is None:
        return src

    # ① 按名牌把每段拆成 (是否名牌, 文本) 序列
    pieces: list = []
    for raw in src:
        s = str(raw or "")
        pos = 0
        for m in rx.finditer(s):
            head = s[pos:m.start()].strip()
            if head:
                pieces.append((False, head))
            pieces.append((True, m.group(0).strip()))
            pos = m.end()
        tail = s[pos:].strip()
        if tail:
            pieces.append((False, tail))

    # ② 名牌挂起，贴在下一段内容的前面
    out: list = []
    pending = ""
    for is_hdr, txt in pieces:
        if is_hdr:
            pending += txt
            continue
        out.append((pending + txt).strip())
        pending = ""
    if pending:
        out.append(pending)
    return [s for s in out if s]


# ---------------------------------------------------------------- 撤回守卫

class RecallGuard:
    """记录"刚发出、还可能被撤回"的消息，供撤回取消用。

    `ttl <= 0` 表示**永不过期**。

    ⚠️ 当前**没有生产调用点**：撤回取消实际由 `main.py` 的
    `_active_triggers` + `active_event_registry.request_agent_stop_all`
    实现（按触发消息 id 精确匹配，不需要时间窗）。
    原来的 `recall_cancel_window` 配置因为只喂给这个从不被调用的结构，
    已被判定为幽灵配置并删除。
    保留此类是为了不破坏既有测试；要彻底清理请连
    `tests/test_w3_w4.py` 里的 3 个用例一起删。
    """

    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = float(ttl)
        self._pending: dict = {}      # umo -> {msg_id: 登记时间}
        self.stats = {"marked": 0, "cancelled": 0, "expired": 0}

    def _expired(self, ts: float, now: float) -> bool:
        return self.ttl > 0 and (now - ts) > self.ttl

    def mark_pending(self, umo: str, msg_id: str) -> None:
        if not umo or not msg_id:
            return
        self._pending.setdefault(str(umo), {})[str(msg_id)] = time.time()
        self.stats["marked"] += 1

    def is_pending(self, umo: str, msg_id: str) -> bool:
        """是否还处于"可撤回"窗口内。

        **只读**：过期项不在这里删（那是 `sweep()` 的职责）。
        旧重建版在这里顺手 pop，导致 `sweep()` 永远返回 0，
        `test_recall_guard_ttl_expires` 直接抓到了。
        """
        d = self._pending.get(str(umo)) or {}
        ts = d.get(str(msg_id))
        if ts is None:
            return False
        return not self._expired(ts, time.time())

    def cancel(self, umo: str, msg_id: str) -> bool:
        """撤回取消：命中返回 True，重复调用返回 False（no-op）。"""
        d = self._pending.get(str(umo)) or {}
        if str(msg_id) not in d:
            return False
        d.pop(str(msg_id), None)
        self.stats["cancelled"] += 1
        return True

    def settle(self, umo: str, msg_id: str) -> None:
        """消息确认发出（不再可能撤回）→ 撤销登记。"""
        d = self._pending.get(str(umo)) or {}
        d.pop(str(msg_id), None)

    def sweep(self) -> int:
        """清掉过期项，返回清掉的条数。"""
        now = time.time()
        n = 0
        for umo in list(self._pending):
            d = self._pending[umo]
            for mid in list(d):
                if self._expired(d[mid], now):
                    d.pop(mid, None)
                    n += 1
            if not d:
                self._pending.pop(umo, None)
        self.stats["expired"] += n
        return n

    def stats_dict(self) -> dict:
        return {**self.stats,
                "pending": sum(len(v) for v in self._pending.values()),
                "ttl": self.ttl}
