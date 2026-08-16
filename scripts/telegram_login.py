"""
One-time interactive login for the Telegram userbot.

Run this yourself, from the project root, in your own terminal:

    python scripts/telegram_login.py

This does NOT run as part of the bot. It connects to Telegram as a real user
account (not a bot token) using Telethon, and Telegram will send a login
code to that account's Telegram app / SMS. You enter that code here.

Do not run this through an automated tool/agent — the login code is sent
directly to your device and nowhere else, so only you can complete it.

At the end it prints a session string. Copy it into your .env file as
TG_SESSION_STRING (along with TG_API_ID and TG_API_HASH). Keep it secret:
anyone with this string has full access to the Telegram account, no
password needed.
"""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv()


async def main() -> None:
    api_id = os.getenv("TG_API_ID") or input("API ID (from https://my.telegram.org/apps): ").strip()
    api_hash = os.getenv("TG_API_HASH") or input("API Hash (from https://my.telegram.org/apps): ").strip()

    client = TelegramClient(StringSession(), int(api_id), api_hash)

    print("\nConnecting... Telegram will ask for the phone number of the account "
          "you want to use as the userbot, then a login code sent to it.\n")

    await client.start()

    me = await client.get_me()
    session_string = client.session.save()

    print(f"\nLogged in as: {me.first_name} (@{me.username or 'no username'})")
    print("\nAdd these lines to your .env file:\n")
    print(f"TG_API_ID={api_id}")
    print(f"TG_API_HASH={api_hash}")
    print(f"TG_SESSION_STRING={session_string}")
    print("\nKeep TG_SESSION_STRING secret and out of git.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
