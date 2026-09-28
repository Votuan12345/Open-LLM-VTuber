from enum import Enum


class BrainEvent(str, Enum):
    """Events fed into PetBrain by the rest of the system."""

    USER_INPUT = "user_input"
    PROACTIVE_TRIGGER = "proactive_trigger"
    RESPONSE_START = "response_start"
    RESPONSE_END = "response_end"
    INTERRUPTED = "interrupted"
    ERROR = "error"
