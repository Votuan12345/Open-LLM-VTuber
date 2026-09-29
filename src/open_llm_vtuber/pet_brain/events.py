from enum import Enum


class BrainEvent(str, Enum):
    """Events fed into PetBrain by the rest of the system."""

    USER_INPUT = "user_input"
    PROACTIVE_TRIGGER = "proactive_trigger"
    RESPONSE_START = "response_start"
    RESPONSE_END = "response_end"
    INTERRUPTED = "interrupted"
    ERROR = "error"
    USER_RETURNED = "user_returned"
    PROACTIVE_SPOKEN = "proactive_spoken"
    PROACTIVE_IGNORED = "proactive_ignored"
    PET_CLICKED = "pet_clicked"
    PET_SPAMMED = "pet_spammed"
    PET_POKED = "pet_poked"
    PET_DRAGGED = "pet_dragged"
