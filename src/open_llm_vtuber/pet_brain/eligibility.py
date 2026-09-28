"""Client eligibility policy for the proactive behavior scheduler (spec §8.2).

`ClientEligibility` is a read-only view over the real server-side connection
bookkeeping (`client_connections`, `client_contexts`,
`current_conversation_tasks`, `chat_group_manager`). The BehaviorScheduler (a
later task) uses it to decide which connected clients it may act on. It never
mutates anything it is given.
"""

from typing import Any, Dict, Iterable, List, Mapping, Optional

from .pet_brain import PetBrain
from .presence import ClientPresence


class ClientEligibility:
    def __init__(
        self,
        client_connections: Dict[str, Any],
        client_contexts: Dict[str, Any],
        current_conversation_tasks: Dict[str, Optional[Any]],
        chat_group_manager: Any,
    ):
        self.client_connections = client_connections
        self.client_contexts = client_contexts
        self.current_conversation_tasks = current_conversation_tasks
        self.chat_group_manager = chat_group_manager

    def brain_for(self, uid: str) -> Optional[PetBrain]:
        context = self.client_contexts.get(uid)
        if context is None:
            return None
        brain = getattr(context, "pet_brain", None)
        if brain is None:
            return None
        if not brain.config.behavior.enabled:
            return None
        return brain

    def in_group(self, uid: str) -> bool:
        group = self.chat_group_manager.get_client_group(uid)
        if group is None:
            return False
        return len(group.members) > 1

    def handles(self, uid: str) -> bool:
        return (
            uid in self.client_connections
            and uid in self.client_contexts
            and self.brain_for(uid) is not None
            and not self.in_group(uid)
        )

    def check(self, uid: str, presence: Optional[ClientPresence]) -> Optional[str]:
        if uid not in self.client_connections:
            return "not_connected"
        if uid not in self.client_contexts or self.brain_for(uid) is None:
            return "not_handled"
        if self.in_group(uid):
            return "in_group"
        if presence is not None:
            if presence.reserved:
                return "reserved"
            if presence.user_turn_pending:
                return "user_turn_pending"
        task = self.current_conversation_tasks.get(uid)
        if task is not None and not task.done():
            return "conversation_running"
        return None

    def order(
        self, uids: Iterable[str], presences: Mapping[str, ClientPresence]
    ) -> List[str]:
        def sort_key(uid: str):
            presence = presences.get(uid)
            interaction = presence.last_user_interaction if presence else None
            has_interaction = interaction is not None
            return (
                not has_interaction,
                -interaction if has_interaction else 0.0,
                uid,
            )

        return sorted(uids, key=sort_key)
