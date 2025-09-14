# bot_ux_plus.py
# Telegram bot: compliments & roasts with UX upgrades.
#
# Install:
#   pip install python-telegram-bot==21.4 httpx==0.27.2
#
# Run:
#   Edit BOT_TOKEN, then: python bot_ux_plus.py
#
# Notes:
# - Uses polling for simplicity (switch to webhooks easily).
# - Per-chat memory prevents repeats (recent window 50).

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from collections import deque, defaultdict
from time import monotonic
from typing import Optional, Callable, Awaitable

import httpx
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    CallbackQueryHandler,
)

BOT_TOKEN = "8311109816:AAGz-X1nSwC8YcT3XdwDZgnCK-S1EFdBrKU"  # ← replace this!

# ---------- Logging ----------
logging.basicConfig(
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("compliroast-bot")

# ---------- HTTP client (shared) ----------
HTTP_TIMEOUT = 8.0
_http_client: Optional[httpx.AsyncClient] = None

async def on_startup(app: Application) -> None:
    global _http_client
    _http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT)
    log.info("HTTP client ready.")

async def on_shutdown(app: Application) -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
    log.info("HTTP client closed.")

# ---------- Safety filter ----------
# Keep it clean even in "spicy" mode: we still block slurs.
BANNED_SUBSTRINGS = {
    "retard", "retarded",
    "faggot", "tranny",
    "kike", "spic", "wetback",
    "chink", "gook",
    "sandnigger", "nigger",
}

def is_clean(text: str) -> bool:
    t = text.lower()
    return not any(bad in t for bad in BANNED_SUBSTRINGS)

# ---------- Per-chat state ----------
RECENT_MAX = 50
DEFAULT_COOLDOWN = 2  # seconds

@dataclass
class ChatState:
    recent: deque[str] = field(default_factory=lambda: deque(maxlen=RECENT_MAX))
    mode: str = "pg"          # "pg" or "spicy" (spicy just loosens phrasing in fallbacks)
    cooldown: int = DEFAULT_COOLDOWN
    admin_lock: bool = True   # when True, only admins can change settings
    last_sent_ts: float = 0.0

state_by_chat: dict[int, ChatState] = defaultdict(ChatState)

def get_state(chat_id: int) -> ChatState:
    return state_by_chat[chat_id]

def normalize(text: str) -> str:
    return " ".join(text.lower().split())

def remember(chat_id: int, text: str) -> None:
    get_state(chat_id).recent.append(normalize(text))

def is_new_for_chat(chat_id: int, text: str) -> bool:
    return normalize(text) not in get_state(chat_id).recent

def cooldown_left(chat_id: int) -> float:
    s = get_state(chat_id)
    elapsed = monotonic() - s.last_sent_ts
    left = max(0.0, s.cooldown - elapsed)
    return left

def stamp_sent(chat_id: int) -> None:
    get_state(chat_id).last_sent_ts = monotonic()

# ---------- MarkdownV2 escaping ----------
_MD_V2_SPECIALS = r"_*[]()~`>#+-=|{}.!".replace(".", r"\.")  # we escape dot too

def escape_md(text: str) -> str:
    # Escape all specials for MarkdownV2
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", text)

# ---------- Fetchers ----------
async def fetch_compliment() -> Optional[str]:
    url = "https://complimentr.com/api"
    try:
        r = await _http_client.get(url)
        r.raise_for_status()
        data = r.json()
        text = (data.get("compliment") or "").strip()
        if text:
            text = text[0].upper() + text[1:]
            if text[-1] not in ".!?":
                text += "."
            if is_clean(text):
                return text
    except Exception as e:
        log.warning("Compliment fetch failed: %s", e)

    # Fallbacks
    fallbacks = [
        "You’re doing great!",
        "Your taste is impeccable.",
        "You make hard things look easy.",
        "Your curiosity is your superpower.",
        "Your energy is contagious.",
        "You have a gift for clarity.",
    ]
    return random.choice([t for t in fallbacks if is_clean(t)])

async def fetch_roast() -> Optional[str]:
    # API sometimes returns literal "You are a $n." (placeholder) — reject it.
    url = "https://insult.mattbas.org/api/insult.json?template=You%20are%20a%20$n."
    try:
        r = await _http_client.get(url)
        r.raise_for_status()
        data = r.json()
        text = (data.get("insult") or "").strip()
        if text and text.lower() != "you are a $n." and len(text) > 3 and is_clean(text):
            return text
    except Exception as e:
        log.warning("Roast fetch failed: %s", e)

    # Fallbacks — a touch “spicier” but still safe
    fallbacks = [
        "You’re like a cloud—when you disappear, it’s a beautiful day.",
        "I’d agree with you, but then we’d both be wrong.",
        "Somewhere out there is a tree working hard for your oxygen. You owe it an apology.",
        "You have the charisma of a dial tone.",
        "Your opinions would be better if they stayed buffering.",
        "You’re not the dumbest person alive, but you better hope they don’t die.",
    ]
    return random.choice([t for t in fallbacks if is_clean(t)])

async def get_unique_text(
    chat_id: int,
    fetcher: Callable[[], Awaitable[Optional[str]]],
    max_attempts: int = 6,
) -> str:
    candidate: Optional[str] = None
    for _ in range(max_attempts):
        candidate = await fetcher()
        if not candidate:
            continue
        if is_new_for_chat(chat_id, candidate):
            break
        candidate = None
    if not candidate:
        candidate = await fetcher() or "I’ve got nothing new… yet!"
    remember(chat_id, candidate)
    return candidate

# ---------- UI ----------
def main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✨ Another Compliment", callback_data="more_compliment"),
            InlineKeyboardButton("🔥 Another Roast", callback_data="more_roast"),
        ]
    ])

async def send_md_reply(update: Update, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    chat = update.effective_chat
    await update.effective_chat.send_action(ChatAction.TYPING)
    await asyncio.sleep(0.2)  # tiny pause for nicer UX
    safe = escape_md(text)
    await update.effective_message.reply_text(
        safe,
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=reply_markup,
        disable_web_page_preview=True,
    )

# ---------- Permissions ----------
async def is_chat_admin(update: Update, user_id: int) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    try:
        member = await update.effective_chat.get_member(user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False

def settings_locked(chat_id: int) -> bool:
    return get_state(chat_id).admin_lock

# ---------- Handlers: basics ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    s = get_state(update.effective_chat.id)
    msg = (
        "Hey! I flip a coin on /random and send a compliment or a roast.\n"
        "Buttons below for quick picks.\n\n"
        "*Commands*\n"
        "• /force_compliment — always compliment\n"
        "• /force_roast — always roast\n"
        "• /mode pg|spicy — set vibe (admin-locked)\n"
        "• /cooldown <secs> — rate limit (admin-locked)\n"
        "• /admin_lock on|off — restrict settings to admins (default on)\n"
        "• /about — what is this\n"
        "• /source — links & credits\n\n"
        f"*Current*: mode={s.mode}, cooldown={s.cooldown}s, admin_lock={'on' if s.admin_lock else 'off'}"
    )
    await send_md_reply(update, msg, main_keyboard())

async def random_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    left = cooldown_left(chat_id)
    if left > 0:
        await send_md_reply(update, f"Please wait {left:.1f}s before the next one.")
        return
    stamp_sent(chat_id)
    if random.random() < 0.5:
        text = await get_unique_text(chat_id, fetch_compliment)
        await send_md_reply(update, f"✨ Compliment: {text}", main_keyboard())
    else:
        text = await get_unique_text(chat_id, fetch_roast)
        await send_md_reply(update, f"🔥 Roast: {text}", main_keyboard())

async def force_compliment_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    left = cooldown_left(chat_id)
    if left > 0:
        await send_md_reply(update, f"Please wait {left:.1f}s before the next one.")
        return
    stamp_sent(chat_id)
    text = await get_unique_text(chat_id, fetch_compliment)
    await send_md_reply(update, f"✨ Compliment: {text}", main_keyboard())

async def force_roast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    left = cooldown_left(chat_id)
    if left > 0:
        await send_md_reply(update, f"Please wait {left:.1f}s before the next one.")
        return
    stamp_sent(chat_id)
    text = await get_unique_text(chat_id, fetch_roast)
    await send_md_reply(update, f"🔥 Roast: {text}", main_keyboard())

# ---------- Handlers: callbacks (inline buttons) ----------
async def more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    left = cooldown_left(chat_id)
    if left > 0:
        await query.message.reply_text(f"Please wait {left:.1f}s.")
        return
    stamp_sent(chat_id)

    if query.data == "more_compliment":
        text = await get_unique_text(chat_id, fetch_compliment)
        await query.message.reply_text(
            escape_md(f"✨ Compliment: {text}"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )
    elif query.data == "more_roast":
        text = await get_unique_text(chat_id, fetch_roast)
        await query.message.reply_text(
            escape_md(f"🔥 Roast: {text}"),
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=main_keyboard(),
        )

# ---------- Handlers: settings & info ----------
async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if settings_locked(chat_id) and not await is_chat_admin(update, update.effective_user.id):
        await send_md_reply(update, "Only admins can change the mode right now \\(admin\\_lock is on\\).")
        return
    if not context.args or context.args[0].lower() not in {"pg", "spicy"}:
        await send_md_reply(update, "Usage: /mode pg|spicy")
        return
    get_state(chat_id).mode = context.args[0].lower()
    await send_md_reply(update, f"Mode set to *{get_state(chat_id).mode}*.")

async def cooldown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if settings_locked(chat_id) and not await is_chat_admin(update, update.effective_user.id):
        await send_md_reply(update, "Only admins can change cooldown \\(admin\\_lock is on\\).")
        return
    if not context.args:
        await send_md_reply(update, f"Current cooldown: *{get_state(chat_id).cooldown}s*")
        return
    try:
        secs = int(context.args[0])
        secs = max(0, min(3600, secs))
    except ValueError:
        await send_md_reply(update, "Usage: /cooldown <seconds>")
        return
    get_state(chat_id).cooldown = secs
    await send_md_reply(update, f"Cooldown set to *{secs}s*.")

async def admin_lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    # Changing admin lock itself should require admin.
    if not await is_chat_admin(update, update.effective_user.id):
        await send_md_reply(update, "Only admins can change admin\\_lock.")
        return
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        await send_md_reply(update, "Usage: /admin_lock on|off")
        return
    val = context.args[0].lower() == "on"
    get_state(chat_id).admin_lock = val
    await send_md_reply(update, f"admin\\_lock is now *{'on' if val else 'off'}*.")

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "I fetch compliments and roasts from public APIs and try not to repeat myself.\n"
        "Inline buttons make it quick; admins can tune mode/cooldown."
    )
    await send_md_reply(update, msg)

async def source_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "*APIs & libs*\n"
        "• Compliments: complimentr\\.com/api\n"
        "• Roasts: insult\\.mattbas\\.org/api\n"
        "• Library: python\\-telegram\\-bot 21\\.x, httpx"
    )
    await send_md_reply(update, msg)

# ---------- App bootstrap ----------
def main() -> None:
    if not BOT_TOKEN or "PASTE_YOUR_BOT_TOKEN_HERE" in BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN with your real token from @BotFather.")

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("random", random_cmd))
    app.add_handler(CommandHandler("force_compliment", force_compliment_cmd))
    app.add_handler(CommandHandler("force_roast", force_roast_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("cooldown", cooldown_cmd))
    app.add_handler(CommandHandler("admin_lock", admin_lock_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("source", source_cmd))

    # Inline buttons
    app.add_handler(CallbackQueryHandler(more_callback, pattern="^(more_compliment|more_roast)$"))

    log.info("Bot running. Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
