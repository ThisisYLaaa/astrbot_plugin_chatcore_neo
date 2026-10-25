# -*- coding: utf-8 -*-
"""TTS：把回复文本转成语音。

与"唯一生成入口"的关系
----------------------
TTS 不是语言生成，是**语音合成**，且不碰主对话的 `req`。
它读的是已经生成好的回复文本，产出的是音频文件。

失败一律静默降级：合成不出来就只发文字，绝不影响正常回复。
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any


class TTS:
    def __init__(self, context: Any, *, provider_id: str = "",
                 enabled: bool = False, only_private: bool = True,
                 max_chars: int = 200) -> None:
        self.context = context
        self.provider_id = (provider_id or "").strip()
        self.enabled = enabled
        self.only_private = only_private
        self.max_chars = max(1, int(max_chars))
        self._provider: Any = None
        self.stats = {"calls": 0, "ok": 0, "failed": 0, "skipped": 0,
                      "last_error": ""}

    # ------------------------------------------------------------ provider
    def _resolve(self) -> Any:
        """解析 TTS provider。

        旧实现调 `context.get_using_provider(provider_type="tts")`
        —— Context 的该方法**不接受 provider_type**，TypeError 被吞成
        `_provider=None`，于是 TTS 整体不可用；而且 `provider_id` 从来没被用过。
        """
        if self._provider is not None:
            return self._provider
        if self.context is None:
            return None
        from .memory.embedder import pick_provider
        try:
            pick_provider.last_error = ""
            self._provider = pick_provider(
                self.context, self.provider_id, kind="tts")
            reason = getattr(pick_provider, "last_error", "")
            if reason:
                self.stats["last_error"] = reason
        except Exception as e:
            self.stats["last_error"] = repr(e)
            self._provider = None
        return self._provider

    @property
    def available(self) -> bool:
        return self.enabled and self._resolve() is not None

    # ------------------------------------------------------------ 合成
    def should_synthesize(self, text: str, *, is_private: bool) -> bool:
        if not self.enabled:
            return False
        if self.only_private and not is_private:
            return False
        t = (text or "").strip()
        if not t or len(t) > self.max_chars:
            self.stats["skipped"] += 1
            return False
        return True

    async def synthesize(self, text: str) -> Any | None:
        """返回一个可直接塞进消息链的 Record 组件；失败返回 None。"""
        prov = self._resolve()
        if prov is None or not hasattr(prov, "get_audio"):
            self.stats["failed"] += 1
            self.stats["last_error"] = "无可用 TTS provider"
            return None
        self.stats["calls"] += 1
        try:
            timeout = 30.0
            r = prov.get_audio(text)
            if asyncio.iscoroutine(r):
                r = await asyncio.wait_for(r, timeout=timeout)
            if not r:
                self.stats["failed"] += 1
                self.stats["last_error"] = "provider 返回空"
                return None
            comp = self._to_record(str(r))
            if comp is None:
                self.stats["failed"] += 1
                self.stats["last_error"] = f"无法构造 Record: {str(r)[:80]}"
                return None
            self.stats["ok"] += 1
            return comp
        except Exception as e:
            self.stats["failed"] += 1
            self.stats["last_error"] = repr(e)
            return None

    @staticmethod
    def _to_record(value: str) -> Any | None:
        """把 provider 返回的路径/URL 转成 Record 组件。

        不同版本的 TTS provider 可能返回文件路径或 URL，两种都试。
        """
        try:
            from astrbot.api.message_components import Record
        except Exception:
            return None
        v = value.strip()
        try:
            if v.startswith(("http://", "https://")):
                return Record.fromURL(v)
            if Path(v).exists():
                return Record.fromFileSystem(v)
        except Exception:
            pass
        try:
            return Record(file=v)
        except Exception:
            return None

    def report(self) -> dict:
        return {**self.stats, "enabled": self.enabled,
                "only_private": self.only_private,
                "provider": self.provider_id or "(auto)",
                "available": self.available}
