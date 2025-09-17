import asyncio
import argparse
from app.db.base import session_scope
from app.db.models import ProactiveOutbox, ChatState
from app.utils.time import utcnow

"""Force enqueue a proactive message for a chat.

Usage:
  python -m scripts.force_proactive --chat 7067878259 --intent proactive_generic --text "Привет! Это тест"

Optionally set --send-now to bypass waiting for userbot worker (will set sent_at immediately, NOT recommended for usual flow).
"""


def build_parser():
    p = argparse.ArgumentParser(description="Force proactive enqueue")
    p.add_argument("--chat", type=int, required=True, help="Chat ID")
    p.add_argument("--intent", type=str, default="proactive_generic", help="Intent name")
    p.add_argument("--text", type=str, required=True, help="Text to send")
    p.add_argument("--send-now", action="store_true", help="Mark as sent immediately (debug)")
    return p


async def main():
    args = build_parser().parse_args()
    async with session_scope() as session:
        st = await session.get(ChatState, args.chat)
        if st is None:
            raise SystemExit(f"ChatState for chat {args.chat} not found. Send at least one message first.")
        if not st.proactive_via_userbot:
            st.proactive_via_userbot = True  # ensure routing
        po = ProactiveOutbox(chat_id=args.chat, intent=args.intent, text=args.text, meta_json={})
        if args.send_now:
            po.sent_at = utcnow()
        session.add(po)
    print(f"Enqueued proactive: chat={args.chat} intent={args.intent} send_now={args.send_now}")


if __name__ == "__main__":
    asyncio.run(main())
