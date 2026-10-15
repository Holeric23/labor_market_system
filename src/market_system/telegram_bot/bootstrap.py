from dataclasses import dataclass

from market_system.telegram_bot.application.services import (
    AuthService, DeliveryService, NotifierWorker, SearchService, SubscriptionService, UIMessageManager,
)
from market_system.telegram_bot.application.session import SessionStore
from market_system.telegram_bot.domain.ports import IPostRepository, ISubscriptionRepository, IUserRepository, TelegramClient, INotificationReceipts
from market_system.telegram_bot.presentation.controller import BotController


@dataclass
class Application:
    controller: BotController
    notifier: NotifierWorker
    auth: AuthService


def build_application(users: IUserRepository, posts: IPostRepository,
                      subscriptions: ISubscriptionRepository, telegram: TelegramClient,
                      receipts: INotificationReceipts | None = None) -> Application:
    auth = AuthService(users)
    delivery = DeliveryService(telegram)
    controller = BotController(users, SessionStore(), UIMessageManager(telegram, users),
                               SearchService(posts), SubscriptionService(subscriptions), delivery)
    return Application(controller, NotifierWorker(subscriptions, delivery, auth, receipts), auth)
