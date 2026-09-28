# config_manager/pet_brain.py
from pydantic import Field, model_validator
from typing import Dict, ClassVar, Literal
from .i18n import I18nMixin, Description

Category = Literal["coding", "unity", "office", "browser", "media", "gaming", "unknown"]

DEFAULT_PROCESS_CATEGORIES: Dict[str, Category] = {
    "Code.exe": "coding",
    "devenv.exe": "coding",
    "pycharm64.exe": "coding",
    "idea64.exe": "coding",
    "rider64.exe": "coding",
    "WindowsTerminal.exe": "coding",
    "Unity.exe": "unity",
    "Unity Hub.exe": "unity",
    "WINWORD.EXE": "office",
    "EXCEL.EXE": "office",
    "POWERPNT.EXE": "office",
    "OUTLOOK.EXE": "office",
    "ONENOTE.EXE": "office",
    "chrome.exe": "browser",
    "msedge.exe": "browser",
    "firefox.exe": "browser",
    "brave.exe": "browser",
    "opera.exe": "browser",
    "vlc.exe": "media",
    "Spotify.exe": "media",
    "PotPlayerMini64.exe": "media",
    "mpc-hc64.exe": "media",
    "steam.exe": "gaming",
    "EpicGamesLauncher.exe": "gaming",
}


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


class BehaviorConfig(I18nMixin):
    """Top-level switch for the Phase 2 behavior scheduler."""

    enabled: bool = Field(True, alias="enabled")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable the behavior scheduler (proactive speech, idle expressions, context awareness). Only effective when pet_brain_config.enabled.",
            zh="启用行为调度器（主动搭话、空闲表情、上下文感知）。仅在 pet_brain_config.enabled 为 True 时生效。",
        ),
    }


class ProactiveConfig(I18nMixin):
    """Proactive speech (Mili speaks up on her own) settings."""

    enabled: bool = Field(True, alias="enabled")
    min_interval_min: float = Field(10.0, alias="min_interval_min", ge=0)
    post_conversation_quiet_min: float = Field(
        3.0, alias="post_conversation_quiet_min", ge=0
    )
    max_per_hour: int = Field(3, alias="max_per_hour", ge=0)
    threshold: float = Field(0.30, alias="threshold", ge=0, le=1)
    chance: float = Field(0.35, alias="chance", ge=0, le=1)
    ignored_after_min: float = Field(5.0, alias="ignored_after_min", ge=0)
    max_backoff_min: float = Field(60.0, alias="max_backoff_min", ge=0)

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(en="Enable proactive speech.", zh="启用主动搭话。"),
        "min_interval_min": Description(
            en="Minimum minutes between proactive turns.",
            zh="两次主动搭话之间的最小间隔（分钟）。",
        ),
        "post_conversation_quiet_min": Description(
            en="Minutes of quiet required after a conversation ends before proactive speech can trigger again.",
            zh="对话结束后，需要安静等待的分钟数才能再次触发主动搭话。",
        ),
        "max_per_hour": Description(
            en="Maximum proactive turns per rolling hour.",
            zh="每小时内主动搭话的最大次数。",
        ),
        "threshold": Description(
            en="Minimum eligibility score required before a proactive turn is even considered.",
            zh="触发主动搭话所需的最小资格分数。",
        ),
        "chance": Description(
            en="Probability of actually speaking once eligible.",
            zh="满足资格后实际开口的概率。",
        ),
        "ignored_after_min": Description(
            en="Minutes after which an ignored proactive turn is considered ignored (used by the backoff).",
            zh="主动搭话被忽略多少分钟后视为被无视（用于退避计算）。",
        ),
        "max_backoff_min": Description(
            en="Upper bound in minutes for the exponential backoff after being ignored. Must be >= min_interval_min.",
            zh="被无视后指数退避的最大分钟数上限。必须大于等于 min_interval_min。",
        ),
    }

    @model_validator(mode="after")
    def _check_backoff_bounds(self) -> "ProactiveConfig":
        if self.max_backoff_min < self.min_interval_min:
            raise ValueError(
                "max_backoff_min must be >= min_interval_min "
                f"(got max_backoff_min={self.max_backoff_min}, "
                f"min_interval_min={self.min_interval_min})"
            )
        return self


class IdleExpressionConfig(I18nMixin):
    """Idle expression (small unprompted Live2D expressions) settings."""

    enabled: bool = Field(True, alias="enabled")
    min_interval_min: float = Field(3.0, alias="min_interval_min", ge=0)
    max_per_hour: int = Field(10, alias="max_per_hour", ge=0)
    chance: float = Field(0.3, alias="chance", ge=0, le=1)

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(en="Enable idle expressions.", zh="启用空闲表情。"),
        "min_interval_min": Description(
            en="Minimum minutes between idle expressions.",
            zh="两次空闲表情之间的最小间隔（分钟）。",
        ),
        "max_per_hour": Description(
            en="Maximum idle expressions per rolling hour.",
            zh="每小时内空闲表情的最大次数。",
        ),
        "chance": Description(
            en="Probability of showing an idle expression once eligible.",
            zh="满足条件后实际显示空闲表情的概率。",
        ),
    }


class ContextConfig(I18nMixin):
    """Windows foreground-process context awareness settings."""

    enabled: bool = Field(True, alias="enabled")
    away_after_min: float = Field(10.0, alias="away_after_min", ge=0)
    process_categories: Dict[str, Category] = Field(
        default_factory=lambda: dict(DEFAULT_PROCESS_CATEGORIES),
        alias="process_categories",
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable context awareness. False falls back to NullContextSensor semantics (fail closed).",
            zh="启用上下文感知。False 时回退到 NullContextSensor 语义（失效时关闭）。",
        ),
        "away_after_min": Description(
            en="Minutes of no foreground-process change before the user is considered away.",
            zh="前台进程多少分钟无变化后视为用户离开。",
        ),
        "process_categories": Description(
            en="Map of process basename (case-insensitive) to category: coding, unity, office, browser, media, gaming, unknown.",
            zh="进程文件名（不区分大小写）到分类的映射：coding, unity, office, browser, media, gaming, unknown。",
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
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig, alias="behavior")
    proactive: ProactiveConfig = Field(
        default_factory=ProactiveConfig, alias="proactive"
    )
    idle_expression: IdleExpressionConfig = Field(
        default_factory=IdleExpressionConfig, alias="idle_expression"
    )
    context: ContextConfig = Field(default_factory=ContextConfig, alias="context")

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable PetBrain (mood, lifecycle, emotion gate, tool permission enforcement). False keeps the original behavior.",
            zh="启用 PetBrain（情绪、生命周期、表情闸门、工具权限）。False 保持原有行为。",
        ),
        "emotion": Description(en="Emotion gate settings", zh="表情闸门设置"),
        "permission": Description(en="Tool permission policy", zh="工具权限策略"),
        "behavior": Description(
            en="Behavior scheduler top-level switch", zh="行为调度器总开关"
        ),
        "proactive": Description(en="Proactive speech settings", zh="主动搭话设置"),
        "idle_expression": Description(
            en="Idle expression settings", zh="空闲表情设置"
        ),
        "context": Description(en="Context awareness settings", zh="上下文感知设置"),
    }
