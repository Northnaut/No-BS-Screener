import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"[CONFIG ERROR] Missing required environment variable: {name}. Check your .env file.", file=sys.stderr)
        sys.exit(1)
    return value


BOT_TOKEN: str = _require("BOT_TOKEN")
GEMINI_API_KEY: str = _require("GEMINI_API_KEY")
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
DB_PATH: str = os.getenv("DB_PATH", "bot.db")
POLL_INTERVAL_MINUTES: int = int(os.getenv("POLL_INTERVAL_MINUTES", "15"))
POSTS_PER_FETCH: int = int(os.getenv("POSTS_PER_FETCH", "25"))
