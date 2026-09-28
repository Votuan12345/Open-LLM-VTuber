from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from loguru import logger

from ..config_manager.pet_brain import PermissionConfig


class PermissionLevel(str, Enum):
    READ = "read"
    INTERACT = "interact"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ToolRequest:
    tool_name: str
    server_name: Optional[str] = None
    arguments: Any = None
    source: str = "llm"


@dataclass(frozen=True)
class PermissionDecision:
    allowed: bool
    level: PermissionLevel
    reason: str


class ConfirmationProvider(ABC):
    """Asks the human to approve a destructive action. Must return True only on explicit approval."""

    @abstractmethod
    async def confirm(self, request: ToolRequest, level: PermissionLevel) -> bool:
        pass


class DenyAllConfirmation(ConfirmationProvider):
    """Used until a real confirmation channel (UI) exists."""

    async def confirm(self, request: ToolRequest, level: PermissionLevel) -> bool:
        return False


class PermissionGuard:
    """Enforced at the execution layer; nothing the LLM says can change a decision.

    - READ / INTERACT: auto-allowed only when the tool is explicitly listed in config.
    - Anything unlisted is DESTRUCTIVE and needs confirmation (fail-closed).
    """

    def __init__(
        self,
        config: PermissionConfig,
        confirmation: Optional[ConfirmationProvider] = None,
    ):
        self._levels = {
            name: PermissionLevel(level) for name, level in config.tool_levels.items()
        }
        self._confirmation = confirmation or DenyAllConfirmation()

    def classify(self, request: ToolRequest) -> PermissionLevel:
        return self._levels.get(request.tool_name, PermissionLevel.DESTRUCTIVE)

    async def check(self, request: ToolRequest) -> PermissionDecision:
        level = self.classify(request)
        logger.info(
            f"[ToolRequest] tool={request.tool_name} server={request.server_name} "
            f"source={request.source} level={level.value}"
        )

        if level in (PermissionLevel.READ, PermissionLevel.INTERACT):
            decision = PermissionDecision(True, level, "Whitelisted")
        else:
            try:
                confirmed = await self._confirmation.confirm(request, level)
            except Exception as e:
                logger.error(f"[Permission] Confirmation provider failed: {e}")
                confirmed = False
            decision = PermissionDecision(
                confirmed,
                level,
                "Confirmed by user" if confirmed else "Confirmation required",
            )

        if decision.allowed:
            logger.info(f"[Permission] ALLOWED {request.tool_name} ({decision.reason})")
        else:
            logger.warning(
                f"[Permission] DENIED {request.tool_name} [Reason] {decision.reason}"
            )
        return decision
