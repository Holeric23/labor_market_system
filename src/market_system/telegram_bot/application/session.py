from dataclasses import dataclass, field

from market_system.telegram_bot.domain.models import PostType, SearchFilter, State


@dataclass
class Session:
    state: State = State.IDLE
    filter: SearchFilter = field(default_factory=lambda: SearchFilter(post_type=PostType.VACANCY))
    editing: str | None = None


class SessionStore:
    """Session filters intentionally live only in RAM."""

    def __init__(self):
        self._sessions: dict[int, Session] = {}

    def get(self, user_id: int) -> Session:
        return self._sessions.setdefault(user_id, Session())

    def contains(self, user_id: int) -> bool:
        return user_id in self._sessions
