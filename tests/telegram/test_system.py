import asyncio
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User as TelegramUser
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from market_system.telegram_bot.application.services import AuthService, DeliveryService, NotifierWorker, text_chunks
from market_system.telegram_bot.bootstrap import build_application
from market_system.telegram_bot.domain.matching import matches
from market_system.telegram_bot.domain.models import Post, PostType, SearchFilter, State, User, utcnow
from market_system.telegram_bot.domain.ports import ForwardUnavailable, TelegramError
from market_system.telegram_bot.infrastructure.memory import MemoryPostRepository, MemorySubscriptionRepository, MemoryUserRepository
from market_system.telegram_bot.infrastructure.telegram import AiogramTelegramClient
from market_system.telegram_bot.presentation.events import create_event_app
from market_system.telegram_bot.presentation.router import AuthMiddleware, build_router


def post(**changes):
    values = dict(post_type="VACANCY", tg_channel_id=-1001234567890, tg_message_id=123,
                  tg_post_link="https://t.me/example/123", raw_text="Текст <b>без HTML</b>",
                  parsed_date=utcnow(), specialty="Python-разработчик", salary_min=100000,
                  salary_max=150000, salary_currency="RUB", work_format="REMOTE",
                  skills=["Python", "SQL"], is_relocation_possible=True)
    values.update(changes)
    return Post(**values)


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.forwards = []
        self.deleted = []
        self.fail_forward = False
        self.fail_send = False
        self.next_id = 100

    async def send_message(self, chat_id, text, keyboard=None, reply_to=None):
        if self.fail_send:
            raise TelegramError("test")
        self.next_id += 1
        self.messages.append(dict(id=self.next_id, chat_id=chat_id, text=text, keyboard=keyboard, reply_to=reply_to))
        return self.next_id

    async def forward_message(self, chat_id, from_chat_id, message_id):
        if self.fail_forward:
            raise ForwardUnavailable("test")
        self.next_id += 1
        self.forwards.append(dict(id=self.next_id, chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id))
        return self.next_id

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


class DomainTests(unittest.TestCase):
    def test_all_filters_and_casefold(self):
        filter = SearchFilter(post_type="VACANCY", specialty="РАЗРАБОТЧИК", salary_min=100000,
                              work_format="REMOTE", relocation=True, skills=[" Python ", "sql"], published_within_days=3)
        self.assertTrue(matches(post(), filter))
        for field, value in [("post_type", "RESUME"), ("salary_min", 200000), ("relocation", False),
                             ("specialty", "Дизайнер"), ("skills", ["Java"]), ("work_format", "OFFICE")]:
            data = filter.model_dump()
            data[field] = value
            self.assertFalse(matches(post(), SearchFilter(**data)), field)

    def test_unknown_values_and_date_boundaries(self):
        self.assertFalse(matches(post(salary_min=None), SearchFilter(post_type="VACANCY", salary_min=0)))
        self.assertFalse(matches(post(is_relocation_possible=None), SearchFilter(post_type="VACANCY", relocation=False)))
        now = utcnow()
        filter = SearchFilter(post_type="VACANCY", published_within_days=3)
        self.assertTrue(matches(post(parsed_date=now - timedelta(days=3)), filter, now))
        self.assertFalse(matches(post(parsed_date=now - timedelta(days=3, seconds=1)), filter, now))
        self.assertFalse(matches(post(parsed_date=now + timedelta(seconds=1)), filter, now))

    def test_validation(self):
        for change in [dict(tg_channel_id=123), dict(salary_min=200000),
                       dict(tg_post_link="javascript:alert(1)"), dict(parsed_date="2026-10-01T00:00:00")]:
            with self.assertRaises(ValidationError):
                post(**change)
        for changes in [dict(salary_min=-1), dict(published_within_days=4), dict(salary_min=True)]:
            with self.assertRaises(ValidationError):
                SearchFilter(post_type="VACANCY", **changes)

    def test_subscription_payload_has_no_pagination(self):
        data = SearchFilter(post_type="RESUME", offset=9, limit=6).subscription_payload()
        self.assertNotIn("offset", data)
        self.assertNotIn("limit", data)

    def test_utf16_chunks(self):
        text = "😀" * 5000 + "текст"
        chunks = list(text_chunks(text))
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(c.encode("utf-16-le")) // 2 <= 3900 for c in chunks))


class ApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.telegram = FakeTelegram()
        self.users = MemoryUserRepository([1, 2])
        self.posts = MemoryPostRepository([post(tg_message_id=i) for i in range(1, 8)])
        self.subscriptions = MemorySubscriptionRepository()
        self.app = build_application(self.users, self.posts, self.subscriptions, self.telegram)
        self.controller = self.app.controller

    async def test_auth_closed_and_roles(self):
        self.assertFalse(await self.app.auth.check_access(3))
        self.assertTrue(await self.app.auth.check_access(1))
        await self.controller.home(1)
        self.assertEqual((await self.users.get_user(1)).role, "ADMIN")

    async def test_filter_edit_reset_and_user_isolation(self):
        await self.controller.callback(1, "type:RESUME")
        await self.controller.callback(1, "edit:salary_min")
        await self.controller.text(1, "100 000")
        self.assertEqual(self.controller.sessions.get(1).filter.salary_min, 100000)
        self.assertIsNone(self.controller.sessions.get(2).filter.salary_min)
        await self.controller.callback(1, "edit:skills")
        await self.controller.text(1, "Python, SQL, python")
        self.assertEqual(self.controller.sessions.get(1).filter.skills, ["python", "sql"])
        await self.controller.callback(1, "reset")
        self.assertEqual(self.controller.sessions.get(1).filter.post_type, PostType.RESUME)
        self.assertEqual(self.controller.sessions.get(1).state, State.SEARCH_SETUP)
        self.assertEqual(self.controller.sessions.get(1).filter.skills, [])

    async def test_bad_input_preserves_filter_and_can_cancel(self):
        await self.controller.callback(1, "edit:salary_min")
        await self.controller.text(1, "-20")
        self.assertIsNone(self.controller.sessions.get(1).filter.salary_min)
        self.assertEqual(self.controller.sessions.get(1).editing, "salary_min")
        await self.controller.home(1)
        self.assertIsNone(self.controller.sessions.get(1).editing)

    async def test_choices_and_clear(self):
        for field, value, expected in [("work_format", "REMOTE", "REMOTE"), ("relocation", "no", False),
                                       ("published_within_days", "7", 7)]:
            await self.controller.callback(1, "edit:" + field)
            await self.controller.callback(1, f"set:{field}:{value}")
            self.assertEqual(getattr(self.controller.sessions.get(1).filter, field), expected)
        await self.controller.callback(1, "edit:specialty")
        await self.controller.text(1, "Python")
        await self.controller.callback(1, "edit:specialty")
        await self.controller.callback(1, "clear:specialty")
        self.assertIsNone(self.controller.sessions.get(1).filter.specialty)

    async def test_pages_keep_posts_and_move_menu_below(self):
        await self.controller.home(1)
        await self.controller.show_results(1, restart=True)
        first_menu = self.telegram.messages[-1]["id"]
        self.assertEqual(len(self.telegram.forwards), 3)
        self.assertEqual(self.controller.sessions.get(1).filter.offset, 3)
        await self.controller.callback(1, "more")
        await self.controller.callback(1, "more")
        self.assertEqual(len(self.telegram.forwards), 7)
        self.assertEqual(len({p["message_id"] for p in self.telegram.forwards}), 7)
        self.assertEqual(self.controller.sessions.get(1).filter.offset, 7)
        self.assertIn(first_menu, self.telegram.deleted)
        delivered_ids = {p["id"] for p in self.telegram.forwards}
        delivered_ids |= {m["id"] for m in self.telegram.messages if m["reply_to"] is not None}
        self.assertFalse(delivered_ids.intersection(self.telegram.deleted))
        self.assertGreater(self.telegram.messages[-1]["id"], max(delivered_ids))
        self.assertNotIn("more", str(self.telegram.messages[-1]["keyboard"]))

    async def test_empty_search_allows_subscription(self):
        await self.controller.callback(1, "type:RESUME")
        await self.controller.show_results(1, restart=True)
        self.assertIn("Ничего не найдено", self.telegram.messages[-1]["text"])
        await self.controller.callback(1, "subscribe")
        self.assertEqual(len(await self.subscriptions.get_user_subscriptions(1)), 1)

    async def test_subscriptions_deduplicate_and_check_ownership(self):
        filter = SearchFilter(post_type="VACANCY", offset=3)
        sub = await self.subscriptions.create_subscription(1, filter)
        filter.offset = 6
        duplicate = await self.subscriptions.create_subscription(1, filter)
        self.assertEqual(sub.id, duplicate.id)
        self.assertFalse(await self.subscriptions.delete_subscription(sub.id, 2))
        self.assertTrue(await self.subscriptions.delete_subscription(sub.id, 1))

    async def test_subscription_list_paginates(self):
        for i in range(7):
            await self.subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY", specialty=str(i)))
        await self.controller.show_subscriptions(1)
        self.assertIn("subscriptions:1", str(self.telegram.messages[-1]["keyboard"]))
        await self.controller.callback(1, "subscriptions:1")
        self.assertIn("6.", self.telegram.messages[-1]["text"])

    async def test_forward_summary_replies_to_original(self):
        await DeliveryService(self.telegram).send_post_to_user(1, post())
        self.assertEqual(self.telegram.messages[0]["reply_to"], self.telegram.forwards[0]["id"])
        self.assertIn("Смотреть оригинал", str(self.telegram.messages[0]["keyboard"]))

    async def test_fallback_preserves_long_original_and_plain_text(self):
        self.telegram.fail_forward = True
        original = "😀 <b>original</b> " * 600
        await DeliveryService(self.telegram).send_post_to_user(1, post(raw_text=original))
        self.assertTrue("".join(m["text"] for m in self.telegram.messages).endswith(original))
        self.assertTrue(all(len(m["text"].encode("utf-16-le")) // 2 <= 3900 for m in self.telegram.messages))
        self.assertIsNotNone(self.telegram.messages[-1]["keyboard"])

    async def test_missing_channel_uses_fallback(self):
        await DeliveryService(self.telegram).send_post_to_user(1, post(tg_channel_id=None))
        self.assertFalse(self.telegram.forwards)
        self.assertIn("ОРИГИНАЛ", self.telegram.messages[-1]["text"])

    async def test_summary_failure_does_not_trigger_fallback(self):
        self.telegram.fail_send = True
        with self.assertRaises(TelegramError):
            await DeliveryService(self.telegram).send_post_to_user(1, post())
        self.assertEqual(len(self.telegram.forwards), 1)

    async def test_failed_menu_send_keeps_old_menu(self):
        await self.controller.home(1)
        menu_id = (await self.users.get_user(1)).last_menu_message_id
        self.telegram.fail_send = True
        with self.assertRaises(TelegramError):
            await self.controller.home(1)
        self.assertEqual((await self.users.get_user(1)).last_menu_message_id, menu_id)
        self.assertNotIn(menu_id, self.telegram.deleted)

    async def test_notifications_deduplicate_overlapping_subscriptions_and_retries(self):
        await self.subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY"))
        await self.subscriptions.create_subscription(1, SearchFilter(post_type="VACANCY", skills=["python"]))
        await self.subscriptions.create_subscription(3, SearchFilter(post_type="VACANCY"))
        post_value, event_id = post(), uuid4()
        results = await asyncio.gather(*(self.app.notifier.on_new_post_created(event_id, post_value) for _ in range(3)))
        self.assertEqual(results, [True] * 3)
        self.assertEqual(len(self.telegram.forwards), 1)
        await self.app.notifier.on_new_post_created(uuid4(), post_value)
        self.assertEqual(len(self.telegram.forwards), 1)
        with self.assertRaises(ValueError):
            await self.app.notifier.on_new_post_created(event_id, post())

    async def test_failed_notification_does_not_block_other_users(self):
        for uid in (1, 2):
            await self.subscriptions.create_subscription(uid, SearchFilter(post_type="VACANCY"))
        delivery = AsyncMock()
        delivery.send_post_to_user.side_effect = [TelegramError("failed"), None, None]
        worker = NotifierWorker(self.subscriptions, delivery, self.app.auth)
        post_value, event_id = post(), uuid4()
        self.assertFalse(await worker.on_new_post_created(event_id, post_value))
        self.assertTrue(await worker.on_new_post_created(event_id, post_value))
        self.assertEqual([c.args[0] for c in delivery.send_post_to_user.call_args_list], [1, 2, 1])


class TelegramAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_after_on_send_forward_and_delete(self):
        method = SendMessage(chat_id=1, text="test")
        for operation in ("send", "forward", "delete"):
            bot, sleep = AsyncMock(), AsyncMock()
            result = type("Result", (), {"message_id": 99})()
            target = {"send": bot.send_message, "forward": bot.forward_message, "delete": bot.delete_message}[operation]
            target.side_effect = [TelegramRetryAfter(method=method, message="Flood", retry_after=2), result]
            client = AiogramTelegramClient(bot, sleep=sleep)
            if operation == "send":
                await client.send_message(1, "test")
            elif operation == "forward":
                await client.forward_message(1, -100123, 1)
            else:
                await client.delete_message(1, 1)
            sleep.assert_awaited_once_with(2.1)
            self.assertEqual(target.await_count, 2)

    async def test_exhausted_retry_and_network_error_propagate(self):
        method = SendMessage(chat_id=1, text="test")
        for error in (TelegramRetryAfter(method=method, message="Flood", retry_after=1),
                      TelegramNetworkError(method=method, message="timeout")):
            bot = AsyncMock()
            bot.forward_message.side_effect = error
            with self.assertRaises(TelegramError) as result:
                await AiogramTelegramClient(bot, max_retries=0).forward_message(1, -100123, 1)
            self.assertNotIsInstance(result.exception, ForwardUnavailable)

    async def test_bad_request_allows_fallback(self):
        bot = AsyncMock()
        bot.forward_message.side_effect = TelegramBadRequest(method=SendMessage(chat_id=1, text="test"), message="protected")
        with self.assertRaises(ForwardUnavailable):
            await AiogramTelegramClient(bot).forward_message(1, -100123, 1)


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.worker = AsyncMock()
        self.worker.on_new_post_created.return_value = True
        self.secret = "a" * 32
        self.client = TestClient(TestServer(create_event_app(self.worker, self.secret)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_auth_validation_success_failure_and_conflict(self):
        payload = {"event_id": str(uuid4()), "post": post().model_dump(mode="json")}
        response = await self.client.post("/events/posts", json=payload)
        self.assertEqual(response.status, 401)
        self.worker.on_new_post_created.assert_not_awaited()
        headers = {"Authorization": "Bearer " + self.secret}
        response = await self.client.post("/events/posts", json={"oops": 1}, headers=headers)
        self.assertEqual(response.status, 400)
        response = await self.client.post("/events/posts", json=payload, headers=headers)
        self.assertEqual(response.status, 200)
        self.worker.on_new_post_created.return_value = False
        response = await self.client.post("/events/posts", json=payload, headers=headers)
        self.assertEqual(response.status, 503)
        self.worker.on_new_post_created.side_effect = ValueError("conflict")
        response = await self.client.post("/events/posts", json=payload, headers=headers)
        self.assertEqual(response.status, 409)

    async def test_publication_reference_requires_auth_and_resolves_post(self):
        loader = AsyncMock(return_value=post())
        async with TestClient(TestServer(create_event_app(self.worker, self.secret, "sqlite", loader))) as client:
            payload = {"event_id": str(uuid4()), "publication_id": 123}
            self.assertEqual((await client.post("/events/publications", json=payload)).status, 401)
            loader.assert_not_awaited()
            headers = {"Authorization": "Bearer " + self.secret}
            self.assertEqual((await client.post("/events/publications", json=payload, headers=headers)).status, 200)
            loader.assert_awaited_once_with(123)
            self.worker.on_new_post_created.assert_awaited_once()
            loader.return_value = None
            self.assertEqual((await client.post("/events/publications", json=payload, headers=headers)).status, 404)
            payload["publication_id"] = -1
            self.assertEqual((await client.post("/events/publications", json=payload, headers=headers)).status, 400)
            response = await client.get("/health")
            self.assertEqual((await response.json())["storage"], "sqlite")


class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_user_access_and_service_update(self):
        middleware = AuthMiddleware(AuthService(MemoryUserRepository([1])))
        handler = AsyncMock()
        user = TelegramUser(id=1, is_bot=False, first_name="Test")
        message = Message(message_id=1, date=utcnow(), chat=Chat(id=1, type="private"), from_user=user, text="/start")
        await middleware(handler, Update(update_id=1, message=message), {"event_from_user": user})
        handler.assert_awaited_once()
        await middleware(handler, Update(update_id=2), {})
        group = message.model_copy(update={"chat": Chat(id=-1001, type="supergroup")})
        await middleware(handler, Update(update_id=3, message=group), {"event_from_user": user})
        self.assertEqual(handler.await_count, 1)

    async def test_router_registration(self):
        app = build_application(MemoryUserRepository(), MemoryPostRepository(), MemorySubscriptionRepository(), FakeTelegram())
        router = build_router(app.controller)
        self.assertEqual(len(router.callback_query.handlers), 1)
        self.assertGreaterEqual(len(router.message.handlers), 4)


class DispatcherIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Exercise real aiogram routing and outer middleware with an offline Bot session."""

    async def asyncSetUp(self):
        self.telegram = FakeTelegram()
        self.app = build_application(MemoryUserRepository([1]), MemoryPostRepository([post() for _ in range(7)]),
                                     MemorySubscriptionRepository(), self.telegram)
        self.bot = Bot(token="123456:OFFLINE_TEST_TOKEN")
        self.bot.session.make_request = AsyncMock(return_value=True)
        self.dp = Dispatcher()
        self.dp.update.outer_middleware(AuthMiddleware(self.app.auth))
        self.dp.include_router(build_router(self.app.controller))
        self.user = TelegramUser(id=1, is_bot=False, first_name="Test")

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.bot.session.close()

    def message(self, text, message_id=1, user_id=1):
        return Message(message_id=message_id, date=utcnow(), chat=Chat(id=user_id, type="private"),
                       from_user=self.user.model_copy(update={"id": user_id}), text=text)

    async def click(self, data, message_id=None):
        if message_id is None:
            message_id = (await self.app.controller.users.get_user(1)).last_menu_message_id
        return await self.dp.feed_update(self.bot, Update(update_id=2, callback_query=CallbackQuery(
            id="query", from_user=self.user, chat_instance="chat", data=data,
            message=self.message("menu", message_id),
        )))

    async def test_start_edit_search_stale_click_and_subscribe(self):
        await self.dp.feed_update(self.bot, Update(update_id=1, message=self.message("/start")))
        self.assertIn("Поиск вакансий", self.telegram.messages[-1]["text"])
        await self.click("type:VACANCY")
        await self.click("edit:skills")
        await self.dp.feed_update(self.bot, Update(update_id=3, message=self.message("Python, SQL", 2)))
        self.assertEqual(self.app.controller.sessions.get(1).filter.skills, ["python", "sql"])
        old_menu = self.telegram.messages[-1]["id"]
        await self.click("search")
        self.assertEqual(len(self.telegram.forwards), 3)
        await self.click("search", old_menu)
        self.assertEqual(len(self.telegram.forwards), 3)
        await self.click("subscribe")
        self.assertEqual(len(await self.app.controller.subscriptions.list(1)), 1)

    async def test_simultaneous_more_click_is_handled_once(self):
        await self.dp.feed_update(self.bot, Update(update_id=1, message=self.message("/start")))
        await self.click("search")
        menu = self.telegram.messages[-1]["id"]
        await asyncio.gather(self.click("more", menu), self.click("more", menu))
        self.assertEqual(len(self.telegram.forwards), 6)

    async def test_persisted_menu_after_restart_cannot_subscribe_default_filter(self):
        await self.app.controller.users.update(User(user_id=1, last_menu_message_id=123))
        await self.click("subscribe", 123)
        self.assertEqual(await self.app.controller.subscriptions.list(1), [])
        self.assertEqual(self.telegram.messages, [])

    async def test_unauthorized_update_never_reaches_controller(self):
        await self.dp.feed_update(self.bot, Update(update_id=1, message=self.message("/start", user_id=999)))
        self.assertEqual(self.telegram.messages, [])
        self.bot.session.make_request.assert_awaited_once()
        self.assertIn("Доступ ограничен", self.bot.session.make_request.call_args.args[1].text)


if __name__ == "__main__":
    unittest.main()
