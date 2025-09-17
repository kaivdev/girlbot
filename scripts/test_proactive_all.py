import asyncio
import argparse
from typing import List

from app.db.base import session_scope
from app.db.models import ChatState, AssistantMessage, ProactiveOutbox
from app.bot.schemas.n8n_io import ChatInfo, Context, N8nRequest
from app.bot.services.n8n_client import call_n8n
from app.bot.services.history import fetch_recent_history
from app.config.settings import get_settings
from app.utils.time import utcnow

# Optional direct send via bot
try:
    from aiogram import Bot  # type: ignore
except Exception:  # pragma: no cover
    Bot = None  # type: ignore

INTENTS_ALL = [
    "proactive_morning",
    "proactive_evening",
    "proactive_reengage",
    "proactive_generic",
]


def build_parser():
    p = argparse.ArgumentParser(
        description="Generate (and optionally send) proactive messages for given chat. Bypasses schedule windows."
    )
    p.add_argument("--chat", type=int, required=True, help="Chat ID")
    p.add_argument(
        "--intents",
        type=str,
        default="all",
        help="Comma-separated list of intents (proactive_morning,proactive_evening,proactive_reengage,proactive_generic) or 'all'",
    )
    p.add_argument("--direct", action="store_true", help="Send immediately via bot token instead of enqueuing/outbox")
    p.add_argument("--force-userbot", action="store_true", help="Force proactive_via_userbot=True")
    p.add_argument("--dry-run", action="store_true", help="Only print generated texts, do not persist")
    p.add_argument("--history", type=int, default=10, help="Number of history pairs for non-trim intents")
    return p


async def generate_one(session, chat_state: ChatState, intent: str, settings, use_history_limit: int):
    persona = chat_state.persona_key or "nika"
    # mimic trimming behaviour: morning/evening/reengage -> trimmed; generic -> keep history
    trim = intent in {"proactive_morning", "proactive_evening", "proactive_reengage"}
    if trim:
        history = []
    else:
        history = await fetch_recent_history(session, chat_state.chat_id, limit_pairs=use_history_limit, persona=persona)
    ctx = Context(
        history=history,
        last_user_msg_at=chat_state.last_user_msg_at,
        last_assistant_at=chat_state.last_assistant_at,
    )
    req = N8nRequest(
        intent=intent,
        chat=ChatInfo(chat_id=chat_state.chat_id, user_id=None, persona=persona, memory_rev=chat_state.memory_rev),
        context=ctx,
    )
    resp = await call_n8n(req)
    return resp.reply, resp.meta.model_dump()


async def main():
    args = build_parser().parse_args()
    intents: List[str]
    if args.intents == "all":
        intents = INTENTS_ALL
    else:
        intents = [x.strip() for x in args.intents.split(",") if x.strip()]
    settings = get_settings()

    bot = None
    if args.direct:
        if Bot is None:
            raise SystemExit("aiogram not installed / Bot import failed")
        if not settings.telegram_bot_token:
            raise SystemExit("TELEGRAM_BOT_TOKEN missing for --direct mode")
        bot = Bot(token=settings.telegram_bot_token)

    async with session_scope() as session:
        state = await session.get(ChatState, args.chat)
        if state is None:
            raise SystemExit("ChatState not found. Send at least one message to the bot/userbot first.")
        if args.force_userbot:
            state.proactive_via_userbot = True
        results = []
        for intent in intents:
            try:
                text, meta = await generate_one(session, state, intent, settings, args.history)
            except Exception as e:  # n8n failure
                results.append((intent, f"ERROR: {e}", False))
                continue
            meta_full = {"intent": intent, **meta}
            if args.dry_run:
                results.append((intent, text, True))
                continue
            if bot and not state.proactive_via_userbot:
                # immediate send via Bot
                try:
                    await bot.send_message(state.chat_id, text)
                    session.add(AssistantMessage(chat_id=state.chat_id, text=text, meta_json=meta_full))
                    state.last_assistant_at = utcnow()
                    # update intent timestamps similar to scheduler
                    if intent == "proactive_morning":
                        state.last_morning_sent_at = utcnow()
                    elif intent == "proactive_evening":
                        state.last_goodnight_sent_at = utcnow()
                    elif intent == "proactive_reengage":
                        state.last_reengage_sent_at = utcnow()
                    results.append((intent, text, True))
                except Exception as e:
                    results.append((intent, f"SEND ERROR: {e}", False))
            else:
                # enqueue for userbot outbox
                session.add(
                    ProactiveOutbox(chat_id=state.chat_id, intent=intent, text=text, meta_json=meta_full)
                )
                # same timestamp updates
                if intent == "proactive_morning":
                    state.last_morning_sent_at = utcnow()
                elif intent == "proactive_evening":
                    state.last_goodnight_sent_at = utcnow()
                elif intent == "proactive_reengage":
                    state.last_reengage_sent_at = utcnow()
                results.append((intent, text, True))

    # Print summary
    print("=== Proactive Test Summary ===")
    for intent, text, ok in results:
        status = "OK" if ok else "FAIL"
        print(f"[{status}] {intent}: {text[:120].replace('\n',' ')}")

    if bot:
        await bot.session.close()  # type: ignore


if __name__ == "__main__":
    asyncio.run(main())
