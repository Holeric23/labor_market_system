import hmac
import json

from aiohttp import web
from pydantic import ValidationError

from market_system.telegram_bot.domain.models import PostCreated, PublicationCreated


def create_event_app(worker, secret: str, backend: str = "memory", post_loader=None) -> web.Application:
    """ML calls this endpoint AFTER committing a new post to its own database.

    It accepts a complete domain Post, so this layer never reads the database.
    Stable event_id/post.id are required when the producer retries a 503 response.
    """
    if len(secret) < 32:
        raise ValueError("EVENT_SECRET must contain at least 32 characters")
    app = web.Application(client_max_size=512 * 1024)

    async def health(request):
        return web.json_response({"status": "ok", "storage": backend})

    def authorize(request):
        supplied = request.headers.get("Authorization", "").encode("utf-8")
        expected = ("Bearer " + secret).encode("utf-8")
        if not hmac.compare_digest(supplied, expected):
            raise web.HTTPUnauthorized()

    async def deliver(event_id, post):
        try:
            success = await worker.on_new_post_created(event_id, post)
        except ValueError:
            return web.json_response({"error": "event_id conflicts with an earlier payload"}, status=409)
        return web.json_response({"status": "delivered" if success else "retry"}, status=200 if success else 503)

    async def created(request):
        authorize(request)
        try:
            event = PostCreated.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "Invalid PostCreated payload"}, status=400)
        return await deliver(event.event_id, event.post)

    async def publication_created(request):
        authorize(request)
        try:
            event = PublicationCreated.model_validate(await request.json())
        except (ValidationError, json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "Invalid PublicationCreated payload"}, status=400)
        post = await post_loader(event.publication_id)
        if post is None:
            return web.json_response({"error": "Publication is not available in the selected model/profile run"}, status=404)
        return await deliver(event.event_id, post)

    app.router.add_get("/health", health)
    app.router.add_post("/events/posts", created)
    if post_loader is not None:
        app.router.add_post("/events/publications", publication_created)
    return app
