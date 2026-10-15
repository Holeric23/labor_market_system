"""Durable delivery under the existing single-worker OS lock."""
import asyncio
from datetime import datetime, timedelta, timezone
from market_system.search.store import now


class TemporaryDeliveryError(Exception):
    def __init__(self, retry_after=30):
        self.retry_after=max(1,min(int(retry_after),86400))


class PermanentDeliveryError(Exception):
    pass


class TelegramSender:
    def __init__(self,settings):
        self.settings=settings
        from .security import install_redaction
        install_redaction(settings)

    def __call__(self,user,text):
        return asyncio.run(self.send(user,text))

    async def send(self,user,text):
        from aiogram import Bot
        from aiogram.client.session.aiohttp import AiohttpSession
        from aiogram.exceptions import TelegramRetryAfter, TelegramForbiddenError, TelegramBadRequest
        from aiogram.exceptions import TelegramAPIError
        async with Bot(self.settings.token,session=AiohttpSession(proxy=self.settings.telegram_proxy_url)) as bot:
            try:
                message=await bot.send_message(user,text,parse_mode=None)
                return message.message_id
            except TelegramRetryAfter as exc:
                raise TemporaryDeliveryError(exc.retry_after) from None
            except (TelegramForbiddenError,TelegramBadRequest):
                raise PermanentDeliveryError() from None
            except (TelegramAPIError,OSError,TimeoutError):
                raise TemporaryDeliveryError() from None


class DeliveryWorker:
    def __init__(self,store,owner,sender,after_send=None):
        self.store,self.owner,self.sender=store,owner,sender
        self.after_send=after_send

    def recover(self):
        with self.store.transaction() as con:
            con.execute("UPDATE telegram_attempts SET outcome='interrupted',finished_at=?,error='Worker stopped; delivery may have succeeded' WHERE finished_at IS NULL",(now(),))
            con.execute("UPDATE telegram_deliveries SET state='queued',owner=NULL WHERE state='running'")

    def run_one(self):
        with self.store.transaction() as con:
            row=con.execute("SELECT d.*,o.payload,o.content_id,o.result_set_id FROM telegram_deliveries d JOIN telegram_outbox o USING(event_id) WHERE d.state='queued' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at,d.event_id,d.user_id LIMIT 1",(now(),)).fetchone()
            if row is None:
                return False
            event,user=row['event_id'],row['user_id']
            allowed=con.execute('SELECT 1 FROM telegram_access WHERE user_id=? AND enabled=1',(user,)).fetchone()
            subscribed=con.execute("SELECT 1 FROM telegram_delivery_subscriptions ds JOIN telegram_subscriptions s ON s.id=ds.subscription_id WHERE ds.event_id=? AND ds.user_id=? AND s.status<>'deleted' LIMIT 1",(event,user)).fetchone()
            if not allowed or not subscribed:
                con.execute("UPDATE telegram_deliveries SET state='cancelled',error='Access or subscription withdrawn' WHERE event_id=? AND user_id=?",(event,user))
                return True
            attempt=row['attempts']+1
            con.execute("UPDATE telegram_deliveries SET state='running',owner=?,attempts=? WHERE event_id=? AND user_id=?",(self.owner,attempt,event,user))
            con.execute('INSERT INTO telegram_attempts(event_id,user_id,attempt,started_at) VALUES(?,?,?,?)',(event,user,attempt,now()))
        message_id=None
        try:
            from market_system.search.service import Catalog
            from .catalog import card_text
            # Render the immutable ready snapshot; source text remains stored once.
            card=Catalog(self.store.path).get(row['content_id'],row['result_set_id'])
            if card is None or row['payload']!='{"render_version":1}':
                raise PermanentDeliveryError()
            message_id=self.sender(user,card_text(card,event))
            if type(message_id) is not int or message_id<=0:
                raise TemporaryDeliveryError()
            if self.after_send:
                self.after_send()
            state,error,next_at='sent',None,now()
        except PermanentDeliveryError:
            state,error,next_at='failed','Telegram rejected delivery',now()
        except Exception as exc:
            # Never persist raw network/API exceptions: they may contain tokens.
            delay=exc.retry_after if isinstance(exc,TemporaryDeliveryError) else min(3600,30*2**min(attempt-1,7))
            state,error='queued','Temporary failure; delivery may have succeeded'
            next_at=(datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat()
        with self.store.transaction() as con:
            owned=con.execute("SELECT 1 FROM telegram_deliveries WHERE event_id=? AND user_id=? AND state='running' AND owner=?",(event,user,self.owner)).fetchone()
            if not owned:
                raise RuntimeError('Delivery ownership lost')
            con.execute('UPDATE telegram_deliveries SET state=?,next_attempt_at=?,owner=NULL,error=?,message_id=?,sent_at=? WHERE event_id=? AND user_id=?',
                        (state,next_at,error,message_id,now() if state=='sent' else None,event,user))
            con.execute('UPDATE telegram_attempts SET finished_at=?,outcome=?,error=?,message_id=? WHERE event_id=? AND user_id=? AND attempt=?',
                        (now(),state,error,message_id,event,user,attempt))
        return True
