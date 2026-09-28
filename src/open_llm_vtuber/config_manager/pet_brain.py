# config_manager/pet_brain.py
from pydantic import Field
from typing import Dict, ClassVar, Literal
from .i18n import I18nMixin, Description


class EmotionGateConfig(I18nMixin):
    """Settings for the EmotionManager gate in front of Live2D expressions."""

    fallback_emotion: str = Field("neutral", alias="fallback_emotion")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "fallback_emotion": Description(
            en="emotionMap key used when every requested expression is invalid",
            zh="当请求的表情全部无效时使用的 emotionMap 键",
        ),
    }


class PermissionConfig(I18nMixin):
    """Tool permission policy. Tools not listed are always treated as destructive."""

    tool_levels: Dict[str, Literal["read", "interact", "destructive"]] = Field(
        default_factory=dict, alias="tool_levels"
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "tool_levels": Description(
            en="Map of tool name to permission level (read / interact / destructive). Unlisted tools are destructive and need confirmation.",
            zh="工具名到权限级别的映射（read / interact / destructive）。未列出的工具视为 destructive，需要确认。",
        ),
    }


class PetBrainConfig(I18nMixin):
    """Configuration for the PetBrain companion core."""

    enabled: bool = Field(False, alias="enabled")
    emotion: EmotionGateConfig = Field(
        default_factory=EmotionGateConfig, alias="emotion"
    )
    permission: PermissionConfig = Field(
        default_factory=PermissionConfig, alias="permission"
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable PetBrain (mood, lifecycle, emotion gate, tool permission enforcement). False keeps the original behavior.",
            zh="启用 PetBrain（情绪、生命周期、表情闸门、工具权限）。False 保持原有行为。",
        ),
        "emotion": Description(en="Emotion gate settings", zh="表情闸门设置"),
        "permission": Description(en="Tool permission policy", zh="工具权限策略"),
    }
