# config_manager/pet_brain.py
from pydantic import Field, model_validator
from typing import Dict, ClassVar, List, Literal
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


ReactionKey = Literal[
    "click_happy",
    "click_neutral",
    "spam",
    "double_click",
    "drag_playful",
    "drag_annoyed",
]

DEFAULT_REACTIONS: Dict[ReactionKey, str] = {
    "click_happy": "joy",
    "click_neutral": "surprise",
    "spam": "anger",
    "double_click": "surprise",
    "drag_playful": "smirk",
    "drag_annoyed": "anger",
}


class PetMovementConfig(I18nMixin):
    """Autonomous desktop movement (Phase 3A) settings."""

    enabled: bool = Field(True, alias="enabled")
    min_interval_min: float = Field(4.0, alias="min_interval_min", ge=0)
    max_per_hour: int = Field(8, alias="max_per_hour", ge=0)
    chance: float = Field(0.4, alias="chance", ge=0, le=1)
    wander_threshold: float = Field(0.35, alias="wander_threshold", ge=0, le=1)
    post_conversation_quiet_min: float = Field(
        1.0, alias="post_conversation_quiet_min", ge=0
    )
    command_timeout_s: float = Field(60.0, alias="command_timeout_s", ge=0)

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(en="Enable autonomous movement.", zh="启用自主移动。"),
        "min_interval_min": Description(
            en="Minimum minutes between movements.",
            zh="两次移动之间的最小间隔（分钟）。",
        ),
        "max_per_hour": Description(
            en="Maximum movements per rolling hour.", zh="每小时内移动的最大次数。"
        ),
        "chance": Description(
            en="Probability of moving once eligible.", zh="满足条件后实际移动的概率。"
        ),
        "wander_threshold": Description(
            en="Minimum wander score (boredom/curiosity/energy/sleepiness) needed to wander.",
            zh="闲逛所需的最小分数（无聊/好奇/精力/困倦）。",
        ),
        "post_conversation_quiet_min": Description(
            en="Minutes after a conversation ends before movement can start.",
            zh="对话结束后多少分钟内不移动。",
        ),
        "command_timeout_s": Description(
            en="Seconds after which an unanswered movement command is dropped.",
            zh="移动命令未收到结果多少秒后视为超时。",
        ),
    }


class PetContextualConfig(I18nMixin):
    """Contextual movement: approach the window the user just switched to."""

    enabled: bool = Field(True, alias="enabled")
    categories: List[Category] = Field(
        default_factory=lambda: ["coding", "unity"], alias="categories"
    )
    min_curiosity: float = Field(0.5, alias="min_curiosity", ge=0, le=1)
    cooldown_min: float = Field(30.0, alias="cooldown_min", ge=0)

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable contextual movement (uses the foreground window rectangle, never its title).",
            zh="启用情境移动（使用前台窗口位置，从不读取标题）。",
        ),
        "categories": Description(
            en="Process categories that trigger an approach when the user switches to them.",
            zh="切换到这些进程分类时触发靠近。",
        ),
        "min_curiosity": Description(
            en="Minimum curiosity needed to approach.", zh="靠近所需的最小好奇心。"
        ),
        "cooldown_min": Description(
            en="Minimum minutes between contextual approaches.",
            zh="两次情境靠近之间的最小间隔（分钟）。",
        ),
    }


class PetInteractionConfig(I18nMixin):
    """Reactions to click / double-click / drag on the pet."""

    enabled: bool = Field(True, alias="enabled")
    reaction_cooldown_s: float = Field(3.0, alias="reaction_cooldown_s", ge=0)
    spam_clicks: int = Field(5, alias="spam_clicks", ge=2)
    spam_window_s: float = Field(10.0, alias="spam_window_s", ge=0)
    drag_pause_min: float = Field(5.0, alias="drag_pause_min", ge=0)
    attention_pause_min: float = Field(3.0, alias="attention_pause_min", ge=0)
    reactions: Dict[ReactionKey, str] = Field(
        default_factory=lambda: dict(DEFAULT_REACTIONS), alias="reactions"
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(en="Enable interaction reactions.", zh="启用互动反应。"),
        "reaction_cooldown_s": Description(
            en="Seconds between two reaction expressions.",
            zh="两次反应表情之间的秒数。",
        ),
        "spam_clicks": Description(
            en="Clicks within spam_window_s that count as click spam.",
            zh="在 spam_window_s 内达到该点击次数视为连点。",
        ),
        "spam_window_s": Description(
            en="Window in seconds for click spam detection.",
            zh="连点检测的时间窗口（秒）。",
        ),
        "drag_pause_min": Description(
            en="Minutes autonomous movement pauses after the user drags the pet.",
            zh="用户拖动后暂停自主移动的分钟数。",
        ),
        "attention_pause_min": Description(
            en="Minutes autonomous movement pauses after a double-click.",
            zh="双击后暂停自主移动的分钟数。",
        ),
        "reactions": Description(
            en="Reaction key to emotionMap key: click_happy, click_neutral, spam, double_click, drag_playful, drag_annoyed.",
            zh="反应键到 emotionMap 键的映射：click_happy, click_neutral, spam, double_click, drag_playful, drag_annoyed。",
        ),
    }

    @model_validator(mode="after")
    def _fill_missing_reactions(self) -> "PetInteractionConfig":
        self.reactions = {**DEFAULT_REACTIONS, **self.reactions}
        return self


class PetIdleMotionConfig(I18nMixin):
    """Idle motions (yawn / stretch / look_around) from the model's motionMap."""

    enabled: bool = Field(True, alias="enabled")
    min_interval_min: float = Field(5.0, alias="min_interval_min", ge=0)
    max_per_hour: int = Field(6, alias="max_per_hour", ge=0)
    chance: float = Field(0.3, alias="chance", ge=0, le=1)

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(en="Enable idle motions.", zh="启用空闲动作。"),
        "min_interval_min": Description(
            en="Minimum minutes between idle motions.",
            zh="两次空闲动作之间的最小间隔（分钟）。",
        ),
        "max_per_hour": Description(
            en="Maximum idle motions per rolling hour.",
            zh="每小时内空闲动作的最大次数。",
        ),
        "chance": Description(
            en="Probability of playing an idle motion once eligible.",
            zh="满足条件后实际播放空闲动作的概率。",
        ),
    }


class DesktopPetConfig(I18nMixin):
    """Phase 3A desktop pet: movement, contextual movement, interactions, idle motions."""

    enabled: bool = Field(True, alias="enabled")
    movement: PetMovementConfig = Field(
        default_factory=PetMovementConfig, alias="movement"
    )
    contextual: PetContextualConfig = Field(
        default_factory=PetContextualConfig, alias="contextual"
    )
    interaction: PetInteractionConfig = Field(
        default_factory=PetInteractionConfig, alias="interaction"
    )
    idle_motion: PetIdleMotionConfig = Field(
        default_factory=PetIdleMotionConfig, alias="idle_motion"
    )

    DESCRIPTIONS: ClassVar[Dict[str, Description]] = {
        "enabled": Description(
            en="Enable the desktop pet lane. Only effective when pet_brain_config.enabled and behavior.enabled.",
            zh="启用桌宠行为。仅在 pet_brain_config.enabled 和 behavior.enabled 为 True 时生效。",
        ),
        "movement": Description(en="Autonomous movement settings", zh="自主移动设置"),
        "contextual": Description(en="Contextual movement settings", zh="情境移动设置"),
        "interaction": Description(
            en="Interaction reaction settings", zh="互动反应设置"
        ),
        "idle_motion": Description(en="Idle motion settings", zh="空闲动作设置"),
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
    desktop_pet: DesktopPetConfig = Field(
        default_factory=DesktopPetConfig, alias="desktop_pet"
    )

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
        "desktop_pet": Description(en="Desktop pet settings", zh="桌宠设置"),
    }
