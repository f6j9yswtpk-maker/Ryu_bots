import os
from loguru import logger


def _telegram_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None, None
    try:
        import telegram  # type: ignore
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not chat_id:
            return None, None
        return telegram.Bot(token=token), chat_id
    except ImportError:
        return None, None


def notify(message: str) -> None:
    """Send a Telegram alert. Silently skips if not configured."""
    bot, chat_id = _telegram_bot()
    if bot is None:
        return

    import asyncio
    import threading

    def _send() -> None:
        try:
            asyncio.run(bot.send_message(chat_id=chat_id, text=message))
        except Exception as exc:
            logger.warning(f"Telegram notification failed: {exc}")

    # Run in a daemon thread so it gets its own fresh event loop.
    # This avoids "asyncio.run() cannot be called from a running event loop"
    # when notify() is called from inside uvicorn's loop (web server thread).
    threading.Thread(target=_send, daemon=True).start()
