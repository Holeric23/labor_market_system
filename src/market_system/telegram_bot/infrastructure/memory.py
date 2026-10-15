from uuid import UUID
from datetime import datetime, timezone

from market_system.telegram_bot.domain.matching import matches
from market_system.telegram_bot.domain.models import Post, Role, SearchFilter, Subscription, User, utcnow


class MemoryUserRepository:
    def __init__(self, admins=()):
        self.admins = set(admins)
        self._users: dict[int, User] = {}

    async def is_admin(self, user_id):
        return user_id in self.admins

    async def get_user(self, user_id):
        value = self._users.get(user_id)
        return value.model_copy(deep=True) if value else None

    async def update(self, user):
        user = user.model_copy(deep=True)
        user.role = Role.ADMIN if user.user_id in self.admins else Role.USER
        self._users[user.user_id] = user


class MemoryPostRepository:
    def __init__(self, posts=()):
        self._posts = {post.id: post.model_copy(deep=True) for post in posts}

    def add(self, post: Post):
        self._posts[post.id] = post.model_copy(deep=True)

    def _matching(self, filter):
        now = utcnow()
        return sorted((p for p in self._posts.values() if matches(p, filter, now)),
                      key=lambda p: (p.parsed_date or datetime.min.replace(tzinfo=timezone.utc), str(p.id)), reverse=True)

    async def search_posts(self, filter, limit, offset):
        return [p.model_copy(deep=True) for p in self._matching(filter)[offset:offset + limit]]

    async def count_posts(self, filter):
        return len(self._matching(filter))


class MemorySubscriptionRepository:
    def __init__(self):
        self._subscriptions: dict[UUID, Subscription] = {}

    async def create_subscription(self, user_id, filter):
        payload = filter.subscription_payload()
        for sub in self._subscriptions.values():
            if sub.user_id == user_id and sub.is_active and sub.filter == payload:
                return sub.model_copy(deep=True)
        sub = Subscription(user_id=user_id, filter=payload)
        self._subscriptions[sub.id] = sub
        return sub.model_copy(deep=True)

    async def get_user_subscriptions(self, user_id):
        return [s.model_copy(deep=True) for s in self._subscriptions.values()
                if s.user_id == user_id and s.is_active]

    async def delete_subscription(self, sub_id, user_id):
        sub = self._subscriptions.get(sub_id)
        if sub is None or sub.user_id != user_id:
            return False
        del self._subscriptions[sub_id]
        return True

    async def find_matching_subscriptions(self, new_post):
        now = utcnow()
        return sorted({s.user_id for s in self._subscriptions.values()
                       if s.is_active and matches(new_post, SearchFilter.model_validate(s.filter), now)})
