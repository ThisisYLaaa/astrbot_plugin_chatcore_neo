# -*- coding: utf-8 -*-
"""状态层：数值模型 / 持久化 / 特权分级。"""

from .model import (EXPLICIT_KW, NEG_KW, POS_KW, MemberState,  # noqa: F401
                    affection_desc, build_thought, emotion_label,
                    lewdness_label)
from .store import (TIER_FRIEND, TIER_INTIMATE, Privilege, StateStore,  # noqa: F401
                    safe_segment)

__all__ = [
    "MemberState", "emotion_label", "lewdness_label", "affection_desc",
    "build_thought", "POS_KW", "NEG_KW", "EXPLICIT_KW",
    "StateStore", "Privilege", "safe_segment", "TIER_INTIMATE", "TIER_FRIEND",
]
