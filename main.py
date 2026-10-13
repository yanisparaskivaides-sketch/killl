"""Compatibility entry point; the bot's implementation lives in bot.main."""

import asyncio

from bot.main import main

if __name__ == "__main__":
    asyncio.run(main())
