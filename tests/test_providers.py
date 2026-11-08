# -*- coding: utf-8 -*-
"""provider 解析的回归测试。

背景（真实事故）
----------------
`core/memory/embedder.py` 与 `core/tts.py` 的 provider 解析三处全错：
  * `mgr.get_provider_by_id(...)` —— ProviderManager 上是 **async**，没 await 拿到协程
  * `mgr.get_all_providers()`      —— ProviderManager 上**不存在**（只有 `get_insts`）
  * `get_using_provider(provider_type="embedding"/"tts")` —— 该参数不存在
结果：**整个长期记忆层与 TTS 完全不可用**，而且异常被吞掉，用户只看到"功能没反应"。

正确的 API 在 `Context` 上（同步）：
  `get_all_embedding_providers()` / `get_all_tts_providers()` /
  `get_provider_by_id(pid)` / `get_using_tts_provider()`

这里用一个**照着 AstrBot 真实签名**造的假 Context 来验证解析。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.memory.embedder import Embedder, pick_provider  # noqa: E402
from core.tts import TTS  # noqa: E402

# 每个 Embedder 都必须显式给 db_path：默认值指向真实 plugin_data，
# 会读到线上 bot 写下的向量缓存库（几千条 4096 维向量）。
import tempfile as _tf  # noqa: E402
from pathlib import Path as _P  # noqa: E402


def _emb(ctx, *a, **kw):
    kw.setdefault("db_path", _P(_tf.mkdtemp()) / "memory.db")
    return Embedder(ctx, *a, **kw)


class _Prov:
    """最小 provider：只有 AstrBot 真实存在的方法。"""

    def __init__(self, pid: str, kind: str, model: str = ""):
        self.provider_config = {"id": pid, "type": kind,
                                "model_config": {"model": model}}

    async def get_embedding(self, text):
        return [0.1, 0.2, 0.3]

    async def get_audio(self, text):
        return "/tmp/x.wav"


class _Ctx:
    """照 AstrBot Context 的真实签名写（只有这些方法存在）。"""

    def __init__(self, emb=None, tts=None):
        self._emb = emb or []
        self._tts = tts or []
        self.calls: list[str] = []

    def get_all_embedding_providers(self):
        self.calls.append("get_all_embedding_providers")
        return list(self._emb)

    def get_all_tts_providers(self):
        self.calls.append("get_all_tts_providers")
        return list(self._tts)

    def get_all_providers(self):
        """只有 chat provider（真实 Context 就是这样）。"""
        return []

    def get_provider_by_id(self, pid):
        self.calls.append(f"get_provider_by_id({pid})")
        for p in self._emb + self._tts:
            if p.provider_config["id"] == pid:
                return p
        return None

    def get_using_provider(self, umo=None):
        """**不接受 provider_type**（真实签名）。"""
        return None

    def get_using_tts_provider(self, umo=None):
        self.calls.append("get_using_tts_provider")
        return self._tts[0] if self._tts else None


# ── 用户真实环境里的 provider id（取自 cmd_config.json）──
EMB_IDS = ["BAAI_embedding", "Qwen3-Embedding-8B"]
TTS_IDS = ["minimax_tts"]


def _emb_ctx():
    return _Ctx(emb=[_Prov(i, "openai_embedding") for i in EMB_IDS])


def _tts_ctx():
    return _Ctx(tts=[_Prov(i, "minimax_tts_api") for i in TTS_IDS])


def _code_only(path) -> str:
    """只保留真正的代码行，剥掉注释与字符串字面量。

    否则我在 docstring 里解释旧 bug 时会写到 `get_all_providers` /
    `provider_type=` 这些字面量，把"禁止再出现"的静态检查误触发。
    """
    import io
    import tokenize
    out = []
    with open(path, encoding="utf-8") as f:
        for tok in tokenize.generate_tokens(f.readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            if tok.type == tokenize.NAME or tok.string in "=().,":
                out.append(tok.string)
    return " ".join(out)


# ---------------------------------------------------------------- embedding

def test_embedder_resolves_first_embedding_provider():
    e = _emb(_emb_ctx())
    assert e.available is True
    assert e._resolve().provider_config["id"] == "BAAI_embedding"


def test_embedder_exact_id():
    e = _emb(_emb_ctx(), provider_id="Qwen3-Embedding-8B")
    assert e._resolve().provider_config["id"] == "Qwen3-Embedding-8B"


def test_embedder_tolerates_model_name_instead_of_id():
    """**用户真实配置就是这个值**：provider_embedding='Qwen/Qwen3-Embedding-8B'，
    而真实 id 是 'Qwen3-Embedding-8B'（没有 Qwen/ 前缀）。必须能兜上。"""
    e = _emb(_emb_ctx(), provider_id="Qwen/Qwen3-Embedding-8B")
    got = e._resolve()
    assert got is not None, "带前缀的模型名必须能匹配到 provider"
    assert got.provider_config["id"] == "Qwen3-Embedding-8B"


def test_embedder_unknown_id_falls_back_and_records_reason():
    """填了不存在的 id 时回退到第一个，并把原因留给用户看（而不是静默失败）。

    审计指出：这条用例的**名字**声称检查 reason，实际只断言了 not None，
    等于把静默回退洗白了 —— 现在真的断言原因。
    """
    e = _emb(_emb_ctx(), provider_id="完全不存在的模型")
    assert e._resolve() is not None
    assert e.available is True
    assert e._last_error, "回退必须留下原因，否则用户在管理页看不到配置被忽略"
    assert "完全不存在的模型" in e._last_error


def test_embedder_no_embedding_provider_is_unavailable():
    e = _emb(_Ctx(emb=[]))
    assert e.available is False


def test_embedder_ignores_non_embedding_provider():
    """provider_id 指向一个没有 get_embedding 的对象时不能选中它。"""

    class _Chat:
        provider_config = {"id": "some-chat", "type": "openai_chat_completion"}

        async def text_chat(self, **kw):
            return None

    ctx = _Ctx(emb=[_Prov(i, "openai_embedding") for i in EMB_IDS])
    ctx._emb.append(_Chat())
    e = _emb(ctx, provider_id="some-chat")
    got = e._resolve()
    assert got is not None and hasattr(got, "get_embedding")


def test_embedder_never_calls_missing_manager_methods():
    """回归：不许再出现 get_all_providers / async get_provider_by_id 这类调用。"""
    src = _code_only(Path(__file__).resolve().parent.parent
                     / "core" / "memory" / "embedder.py")
    assert "get_all_providers" not in src, \
        "ProviderManager 没有 get_all_providers，别再调"
    assert 'provider_type=' not in src, \
        "Context.get_using_provider 不接受 provider_type"
    assert "provider_manager" not in src, \
        "应该用 Context 的同步方法，不要下沉到 ProviderManager"


def test_embedder_embed_works_end_to_end():
    e = _emb(_emb_ctx())
    v = asyncio.run(e.embed("你好"))
    assert v == [0.1, 0.2, 0.3]


def test_embedder_failure_counter_resets_on_success():
    """连续失败计数必须在成功后复位，否则抖 5 次就永久关闭记忆。"""
    p = _Prov("x", "openai_embedding")
    calls = {"n": 0}

    async def flaky(text):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("网络抖动")
        return [1.0, 0.0]

    p.get_embedding = flaky
    ctx = _Ctx(emb=[p])
    e = _emb(ctx)
    assert asyncio.run(e.embed("a")) == []      # 失败
    assert asyncio.run(e.embed("b")) == []      # 失败
    assert e._failed == 2
    assert asyncio.run(e.embed("c")) == [1.0, 0.0]   # 成功
    assert e._failed == 0, "成功后必须复位连续失败计数"


# ---------------------------------------------------------------- TTS

def test_tts_resolves_tts_provider():
    t = TTS(_tts_ctx(), enabled=True)
    assert t.available is True, "TTS 必须能解析到 provider（旧实现恒为 False）"


def test_tts_exact_id():
    t = TTS(_tts_ctx(), enabled=True, provider_id="minimax_tts")
    assert t._resolve().provider_config["id"] == "minimax_tts"


def test_tts_provider_id_is_actually_used():
    """旧实现 `_resolve()` 从不读 provider_id —— 这就是幽灵配置。"""
    ctx = _Ctx(tts=[_Prov("tts_a", "minimax_tts_api"),
                    _Prov("tts_b", "minimax_tts_api")])
    t = TTS(ctx, enabled=True, provider_id="tts_b")
    got = t._resolve()
    assert got is not None
    assert got.provider_config["id"] == "tts_b", \
        "指定了 provider_id 就必须用它，不能永远取第一个"


def test_tts_disabled_still_unavailable():
    t = TTS(_tts_ctx(), enabled=False)
    assert t.available is False


def test_tts_no_provider_is_unavailable():
    t = TTS(_Ctx(tts=[]), enabled=True)
    assert t.available is False


def test_tts_never_calls_get_using_provider_with_provider_type():
    src = _code_only(Path(__file__).resolve().parent.parent
                     / "core" / "tts.py")
    assert "provider_type=" not in src, \
        "Context.get_using_provider 不接受 provider_type（旧 bug）"


# ---------------------------------------------------------------- 助手本身

def test_pick_provider_returns_none_when_nothing_usable():
    assert pick_provider(_Ctx(), "", kind="embedding") is None
    assert pick_provider(_Ctx(), "x", kind="tts") is None


def test_pick_provider_case_insensitive():
    ctx = _Ctx(emb=[_Prov("MiXeD_Case", "openai_embedding")])
    got = pick_provider(ctx, "mixed_case", kind="embedding")
    assert got is not None and got.provider_config["id"] == "MiXeD_Case"
