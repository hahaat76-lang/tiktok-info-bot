import os
import json
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone
from io import BytesIO

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)
from telegram.helpers import escape_markdown

from scraper import TikTokScraper

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")

# Conversation states
WAITING_USERNAME, WAITING_ID, WAITING_VIDEO_URL, WAITING_COMPARE = range(4)

# Rate limit storage: {user_id: {"count": int, "start_time": datetime}}
rate_limit = {}
RATE_LIMIT_COUNT = 20
RATE_LIMIT_WINDOW = 300  # 5 minutes
RATE_LIMIT_COOLDOWN = 600  # 10 minutes

# User language preferences: {user_id: "ar" or "en"}
user_langs = {}

# Language strings cache
lang_strings = {}

scraper = TikTokScraper()


def load_languages():
    """Load language files."""
    global lang_strings
    lang_dir = os.path.join(os.path.dirname(__file__), "lang")
    for lang_file in ["ar.json", "en.json"]:
        lang_code = lang_file.replace(".json", "")
        filepath = os.path.join(lang_dir, lang_file)
        with open(filepath, "r", encoding="utf-8") as f:
            lang_strings[lang_code] = json.load(f)


def t(user_id: int, key: str, **kwargs) -> str:
    """Get translated string for user."""
    lang = user_langs.get(user_id, "ar")
    text = lang_strings.get(lang, lang_strings["ar"]).get(key, key)
    if kwargs:
        text = text.format(**kwargs)
    return text


def check_rate_limit(user_id: int) -> tuple[bool, int, int]:
    """Check if user is rate limited. Returns (is_limited, minutes, seconds)."""
    current_time = datetime.now(timezone.utc)

    if user_id not in rate_limit:
        rate_limit[user_id] = {"count": 0, "start_time": current_time}

    user_data = rate_limit[user_id]
    elapsed = (current_time - user_data["start_time"]).total_seconds()

    if elapsed > RATE_LIMIT_WINDOW:
        user_data["count"] = 1
        user_data["start_time"] = current_time
        return False, 0, 0

    if user_data["count"] >= RATE_LIMIT_COUNT:
        remaining = RATE_LIMIT_COOLDOWN - elapsed
        if remaining <= 0:
            user_data["count"] = 1
            user_data["start_time"] = current_time
            return False, 0, 0
        minutes = int(remaining // 60)
        seconds = int(remaining % 60)
        return True, minutes, seconds

    user_data["count"] += 1
    return False, 0, 0


# Search history: {user_id: [{"username": ..., "time": ...}, ...]}
search_history = {}

# Favorites: {user_id: ["username1", "username2", ...]}
favorites = {}


def save_to_history(user_id: int, username: str):
    """Save a search to user's history."""
    if user_id not in search_history:
        search_history[user_id] = []
    search_history[user_id].insert(0, {
        "username": username,
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    })
    if len(search_history[user_id]) > 20:
        search_history[user_id] = search_history[user_id][:20]


def build_user_response(data: dict, user_id: int) -> str:
    """Build formatted user info response."""
    lang = user_langs.get(user_id, "ar")
    response = t(user_id, "account_details")

    fields = [
        ("username", data["username"]),
        ("nickname", data["nickname"]),
        ("user_id_field", data.get("user_id", "N/A")),
        ("bio", data["bio"]),
        ("followers", data["followers"]),
        ("following", data["following"]),
        ("likes", data["likes"]),
        ("videos", data["videos"]),
        ("friends", data.get("friends", "0")),
        ("digg", data.get("digg", "0")),
        ("verified", t(user_id, "yes") if data["verified"] else t(user_id, "no")),
        ("private", t(user_id, "yes") if data["private"] else t(user_id, "no")),
        ("created", data["created"]),
        ("region", data["region"]),
        ("language_field", data.get("language", "N/A")),
        ("bio_link_field", data.get("bio_link", "N/A")),
        ("profile_link", data["profile_link"]),
    ]

    for key, value in fields:
        label = t(user_id, key)
        safe_label = escape_markdown(label, version=2)
        safe_value = escape_markdown(str(value), version=2)
        response += f"*{safe_label}:* {safe_value}\n"

    return response


# ─── Command Handlers ───────────────────────────────────────────────


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user_id = update.effective_user.id
    if user_id not in user_langs:
        user_langs[user_id] = "ar"

    keyboard = [
        [
            InlineKeyboardButton("🔍 " + ("بحث" if user_langs.get(user_id) == "ar" else "Search"), callback_data="action:search"),
            InlineKeyboardButton("🔢 ID", callback_data="action:id"),
        ],
        [
            InlineKeyboardButton("🎥 " + ("فيديو" if user_langs.get(user_id) == "ar" else "Video"), callback_data="action:video"),
            InlineKeyboardButton("⚖️ " + ("مقارنة" if user_langs.get(user_id) == "ar" else "Compare"), callback_data="action:compare"),
        ],
        [
            InlineKeyboardButton("⭐ " + ("المفضلة" if user_langs.get(user_id) == "ar" else "Favorites"), callback_data="action:fav"),
            InlineKeyboardButton("📜 " + ("السجل" if user_langs.get(user_id) == "ar" else "History"), callback_data="action:history"),
        ],
        [
            InlineKeyboardButton("🌐 " + ("اللغة" if user_langs.get(user_id) == "ar" else "Language"), callback_data="action:lang"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        t(user_id, "welcome"),
        reply_markup=reply_markup,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help command."""
    user_id = update.effective_user.id
    await update.message.reply_text(
        t(user_id, "help"),
        parse_mode=ParseMode.MARKDOWN,
    )


async def lang_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /lang command."""
    user_id = update.effective_user.id
    keyboard = [
        [
            InlineKeyboardButton("🇸🇦 العربية", callback_data="lang:ar"),
            InlineKeyboardButton("🇺🇸 English", callback_data="lang:en"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        t(user_id, "choose_lang"),
        reply_markup=reply_markup,
    )


# ─── Search by Username ─────────────────────────────────────────────


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /search command - ask for username."""
    user_id = update.effective_user.id

    is_limited, mins, secs = check_rate_limit(user_id)
    if is_limited:
        await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
        return ConversationHandler.END

    await update.message.reply_text(t(user_id, "ask_username"))
    return WAITING_USERNAME


async def handle_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process username input and fetch data."""
    user_id = update.effective_user.id
    username = update.message.text.strip().lstrip("@")

    msg = await update.message.reply_text(t(user_id, "searching"))

    result = await scraper.get_user_by_username(username)

    if result.get("error"):
        await msg.edit_text(t(user_id, "error_not_found"))
        return ConversationHandler.END

    save_to_history(user_id, result["username"])

    # Send profile picture
    if result.get("profile_pic"):
        try:
            await update.message.reply_photo(photo=result["profile_pic"])
        except Exception:
            pass

    response = build_user_response(result, user_id)

    keyboard = [
        [
            InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
            InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
        ],
        [
            InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.edit_text(
        response,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    return ConversationHandler.END


# ─── Search by ID ────────────────────────────────────────────────────


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /id command - ask for user ID."""
    user_id = update.effective_user.id

    is_limited, mins, secs = check_rate_limit(user_id)
    if is_limited:
        await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
        return ConversationHandler.END

    await update.message.reply_text(t(user_id, "ask_id"))
    return WAITING_ID


async def handle_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process user ID input and fetch data."""
    user_id = update.effective_user.id
    tiktok_id = update.message.text.strip()

    msg = await update.message.reply_text(t(user_id, "searching"))

    result = await scraper.get_user_by_id(tiktok_id)

    if result.get("error"):
        await msg.edit_text(t(user_id, "error_not_found"))
        return ConversationHandler.END

    save_to_history(user_id, result["username"])

    if result.get("profile_pic"):
        try:
            await update.message.reply_photo(photo=result["profile_pic"])
        except Exception:
            pass

    response = build_user_response(result, user_id)

    keyboard = [
        [
            InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
            InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
        ],
        [
            InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.edit_text(
        response,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
        reply_markup=reply_markup,
    )
    return ConversationHandler.END


# ─── Video Download ──────────────────────────────────────────────────


async def video_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /video command - ask for video URL."""
    user_id = update.effective_user.id

    is_limited, mins, secs = check_rate_limit(user_id)
    if is_limited:
        await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
        return ConversationHandler.END

    await update.message.reply_text(t(user_id, "ask_video_url"))
    return WAITING_VIDEO_URL


async def handle_video_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process video URL and send video without watermark."""
    user_id = update.effective_user.id
    video_url = update.message.text.strip()

    msg = await update.message.reply_text(t(user_id, "downloading_video"))

    result = await scraper.get_video_no_watermark(video_url)

    if result.get("error"):
        await msg.edit_text(t(user_id, "error_video"))
        return ConversationHandler.END

    try:
        caption = ""
        if result.get("title"):
            caption = f"📝 {result['title']}\n"
        if result.get("author"):
            caption += f"👤 @{result['author']}"

        await update.message.reply_video(
            video=result["video_url"],
            caption=caption if caption else None,
            supports_streaming=True,
        )
        await msg.edit_text(t(user_id, "video_sent"))
    except Exception:
        await msg.edit_text(t(user_id, "error_video"))

    return ConversationHandler.END


# ─── Callback Handlers ──────────────────────────────────────────────


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all inline button callbacks."""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    # Language selection
    if data.startswith("lang:"):
        lang_code = data.split(":")[1]
        user_langs[user_id] = lang_code
        await query.message.edit_text(t(user_id, "lang_changed"))
        return

    # Action buttons from /start
    if data.startswith("action:"):
        action = data.split(":")[1]
        if action == "search":
            await query.message.reply_text(t(user_id, "ask_username"))
            context.user_data["pending_action"] = "search"
        elif action == "id":
            await query.message.reply_text(t(user_id, "ask_id"))
            context.user_data["pending_action"] = "id"
        elif action == "video":
            await query.message.reply_text(t(user_id, "ask_video_url"))
            context.user_data["pending_action"] = "video"
        elif action == "compare":
            await query.message.reply_text(t(user_id, "ask_compare"))
            context.user_data["pending_action"] = "compare"
        elif action == "fav":
            if user_id not in favorites or not favorites[user_id]:
                await query.message.reply_text(t(user_id, "fav_empty"))
            else:
                fav_list = favorites[user_id]
                response = t(user_id, "fav_title", count=len(fav_list))
                kb = []
                for i, uname in enumerate(fav_list, 1):
                    response += f"{i}. @{uname}\n"
                    kb.append([InlineKeyboardButton(f"🔍 @{uname}", callback_data=f"favsearch:{uname}")])
                await query.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "history":
            if user_id not in search_history or not search_history[user_id]:
                await query.message.reply_text(t(user_id, "history_empty"))
            else:
                history = search_history[user_id][:10]
                response = t(user_id, "history_title")
                kb = []
                for i, entry in enumerate(history, 1):
                    response += f"{i}. @{entry['username']} - {entry['time']}\n"
                    kb.append([InlineKeyboardButton(f"🔍 @{entry['username']}", callback_data=f"favsearch:{entry['username']}")])
                await query.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(kb))
        elif action == "lang":
            keyboard = [
                [
                    InlineKeyboardButton("🇸🇦 العربية", callback_data="lang:ar"),
                    InlineKeyboardButton("🇺🇸 English", callback_data="lang:en"),
                ]
            ]
            await query.message.reply_text(
                t(user_id, "choose_lang"),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    # Refresh user info
    if data.startswith("refresh:"):
        parts = data.split(":")
        search_type = parts[1]
        identifier = parts[2]

        is_limited, mins, secs = check_rate_limit(user_id)
        if is_limited:
            await query.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
            return

        if search_type == "username":
            result = await scraper.get_user_by_username(identifier)
        else:
            result = await scraper.get_user_by_id(identifier)

        if result.get("error"):
            await query.message.reply_text(t(user_id, "error_not_found"))
            return

        response = build_user_response(result, user_id)
        keyboard = [
            [
                InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
                InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
            ]
        ]
        await query.message.edit_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Add to favorites
    if data.startswith("addfav:"):
        username = data.split(":", 1)[1]
        if user_id not in favorites:
            favorites[user_id] = []
        if username.lower() in [f.lower() for f in favorites[user_id]]:
            await query.message.reply_text(t(user_id, "fav_exists", username=username))
        else:
            favorites[user_id].append(username)
            await query.message.reply_text(t(user_id, "fav_added", username=username))
        return

    # Search from favorites/history
    if data.startswith("favsearch:"):
        username = data.split(":", 1)[1]
        result = await scraper.get_user_by_username(username)
        if result.get("error"):
            await query.message.reply_text(t(user_id, "error_not_found"))
            return
        save_to_history(user_id, result["username"])
        if result.get("profile_pic"):
            try:
                await query.message.reply_photo(photo=result["profile_pic"])
            except Exception:
                pass
        response = build_user_response(result, user_id)
        keyboard = [
            [
                InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
                InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
            ],
            [
                InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
            ]
        ]
        await query.message.reply_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Raw data
    if data.startswith("raw:"):
        parts = data.split(":")
        search_type = parts[1]
        identifier = parts[2]

        result = await scraper.get_user_by_username(identifier)

        if result.get("error"):
            await query.message.reply_text(t(user_id, "error_not_found"))
            return

        raw_data = {
            "user": result.get("raw_user", {}),
            "stats": result.get("raw_stats", {}),
        }

        date_str = datetime.now().strftime("%Y-%m-%d")
        filename = f"{date_str}_{identifier}_raw.json"
        json_data = json.dumps(raw_data, indent=2, ensure_ascii=False)
        bio = BytesIO(json_data.encode("utf-8"))
        bio.name = filename
        bio.seek(0)

        await query.message.reply_document(document=bio, filename=filename)
        return


# ─── Fallback message handler for action buttons ────────────────────


async def handle_pending_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text messages when a pending action is set from inline buttons."""
    user_id = update.effective_user.id
    pending = context.user_data.get("pending_action")

    if not pending:
        # If user just sends a username directly, treat as search
        username = update.message.text.strip()
        if username and not username.startswith("/"):
            is_limited, mins, secs = check_rate_limit(user_id)
            if is_limited:
                await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
                return

            msg = await update.message.reply_text(t(user_id, "searching"))
            result = await scraper.get_user_by_username(username)

            if result.get("error"):
                await msg.edit_text(t(user_id, "error_not_found"))
                return

            save_to_history(user_id, result["username"])

            if result.get("profile_pic"):
                try:
                    await update.message.reply_photo(photo=result["profile_pic"])
                except Exception:
                    pass

            response = build_user_response(result, user_id)
            keyboard = [
                [
                    InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
                    InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
                ],
                [
                    InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
                ]
            ]
            await msg.edit_text(
                response,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        return

    context.user_data.pop("pending_action", None)
    text = update.message.text.strip()

    if pending == "search":
        is_limited, mins, secs = check_rate_limit(user_id)
        if is_limited:
            await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
            return

        msg = await update.message.reply_text(t(user_id, "searching"))
        result = await scraper.get_user_by_username(text)

        if result.get("error"):
            await msg.edit_text(t(user_id, "error_not_found"))
            return

        save_to_history(user_id, result["username"])

        if result.get("profile_pic"):
            try:
                await update.message.reply_photo(photo=result["profile_pic"])
            except Exception:
                pass

        response = build_user_response(result, user_id)
        keyboard = [
            [
                InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
                InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
            ],
            [
                InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
            ]
        ]
        await msg.edit_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif pending == "id":
        is_limited, mins, secs = check_rate_limit(user_id)
        if is_limited:
            await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
            return

        msg = await update.message.reply_text(t(user_id, "searching"))
        result = await scraper.get_user_by_id(text)

        if result.get("error"):
            await msg.edit_text(t(user_id, "error_not_found"))
            return

        save_to_history(user_id, result["username"])

        if result.get("profile_pic"):
            try:
                await update.message.reply_photo(photo=result["profile_pic"])
            except Exception:
                pass

        response = build_user_response(result, user_id)
        keyboard = [
            [
                InlineKeyboardButton(t(user_id, "refresh"), callback_data=f"refresh:username:{result['username']}"),
                InlineKeyboardButton(t(user_id, "raw_data"), callback_data=f"raw:username:{result['username']}"),
            ],
            [
                InlineKeyboardButton(t(user_id, "add_fav"), callback_data=f"addfav:{result['username']}"),
            ]
        ]
        await msg.edit_text(
            response,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif pending == "compare":
        parts = text.split()
        if len(parts) < 2:
            await update.message.reply_text(t(user_id, "ask_compare"))
            return
        user1_name = parts[0].lstrip("@")
        user2_name = parts[1].lstrip("@")
        msg = await update.message.reply_text(t(user_id, "comparing"))
        result1 = await scraper.get_user_by_username(user1_name)
        result2 = await scraper.get_user_by_username(user2_name)
        if result1.get("error") or result2.get("error"):
            await msg.edit_text(t(user_id, "error_compare"))
            return
        response = t(user_id, "compare_title")
        compare_fields = [
            ("username", "username"), ("nickname", "nickname"),
            ("followers", "followers"), ("following", "following"),
            ("likes", "likes"), ("videos", "videos"),
            ("verified", "verified"), ("created", "created"),
            ("region", "region"), ("language_field", "language"),
        ]
        vs = t(user_id, "vs")
        for label_key, data_key in compare_fields:
            label = t(user_id, label_key)
            v1 = result1.get(data_key, "N/A")
            v2 = result2.get(data_key, "N/A")
            if data_key == "verified":
                v1 = t(user_id, "yes") if v1 else t(user_id, "no")
                v2 = t(user_id, "yes") if v2 else t(user_id, "no")
            safe_label = escape_markdown(str(label), version=2)
            safe_v1 = escape_markdown(str(v1), version=2)
            safe_v2 = escape_markdown(str(v2), version=2)
            safe_vs = escape_markdown(vs, version=2)
            response += f"*{safe_label}:*\n{safe_v1} {safe_vs} {safe_v2}\n\n"
        await msg.edit_text(response, parse_mode=ParseMode.MARKDOWN_V2, disable_web_page_preview=True)

    elif pending == "video":
        msg = await update.message.reply_text(t(user_id, "downloading_video"))
        result = await scraper.get_video_no_watermark(text)

        if result.get("error"):
            await msg.edit_text(t(user_id, "error_video"))
            return

        try:
            caption = ""
            if result.get("title"):
                caption = f"📝 {result['title']}\n"
            if result.get("author"):
                caption += f"👤 @{result['author']}"

            await update.message.reply_video(
                video=result["video_url"],
                caption=caption if caption else None,
                supports_streaming=True,
            )
            await msg.edit_text(t(user_id, "video_sent"))
        except Exception:
            await msg.edit_text(t(user_id, "error_video"))


# ─── Compare Two Accounts ────────────────────────────────────────────


async def compare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /compare command."""
    user_id = update.effective_user.id
    is_limited, mins, secs = check_rate_limit(user_id)
    if is_limited:
        await update.message.reply_text(t(user_id, "rate_limited", minutes=mins, seconds=secs))
        return ConversationHandler.END
    await update.message.reply_text(t(user_id, "ask_compare"))
    return WAITING_COMPARE


async def handle_compare_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Process compare input."""
    user_id = update.effective_user.id
    text = update.message.text.strip()
    parts = text.split()
    if len(parts) < 2:
        await update.message.reply_text(t(user_id, "ask_compare"))
        return WAITING_COMPARE

    user1_name = parts[0].lstrip("@")
    user2_name = parts[1].lstrip("@")

    msg = await update.message.reply_text(t(user_id, "comparing"))

    result1 = await scraper.get_user_by_username(user1_name)
    result2 = await scraper.get_user_by_username(user2_name)

    if result1.get("error") or result2.get("error"):
        await msg.edit_text(t(user_id, "error_compare"))
        return ConversationHandler.END

    response = t(user_id, "compare_title")

    compare_fields = [
        ("username", "username", "username"),
        ("nickname", "nickname", "nickname"),
        ("followers", "followers", "followers"),
        ("following", "following", "following"),
        ("likes", "likes", "likes"),
        ("videos", "videos", "videos"),
        ("verified", "verified", "verified"),
        ("created", "created", "created"),
        ("region", "region", "region"),
        ("language_field", "language", "language"),
    ]

    vs = t(user_id, "vs")
    for label_key, data_key, _ in compare_fields:
        label = t(user_id, label_key)
        v1 = result1.get(data_key, "N/A")
        v2 = result2.get(data_key, "N/A")
        if data_key == "verified":
            v1 = t(user_id, "yes") if v1 else t(user_id, "no")
            v2 = t(user_id, "yes") if v2 else t(user_id, "no")
        safe_label = escape_markdown(str(label), version=2)
        safe_v1 = escape_markdown(str(v1), version=2)
        safe_v2 = escape_markdown(str(v2), version=2)
        safe_vs = escape_markdown(vs, version=2)
        response += f"*{safe_label}:*\n{safe_v1} {safe_vs} {safe_v2}\n\n"

    await msg.edit_text(
        response,
        parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True,
    )
    return ConversationHandler.END


# ─── Favorites ───────────────────────────────────────────────────────


async def fav_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /fav command."""
    user_id = update.effective_user.id
    args = update.message.text.strip().split(maxsplit=2)

    # /fav add username
    if len(args) >= 3 and args[1].lower() == "add":
        username = args[2].lstrip("@")
        if user_id not in favorites:
            favorites[user_id] = []
        if username.lower() in [f.lower() for f in favorites[user_id]]:
            await update.message.reply_text(t(user_id, "fav_exists", username=username))
        else:
            favorites[user_id].append(username)
            await update.message.reply_text(t(user_id, "fav_added", username=username))
        return

    # /fav remove username
    if len(args) >= 3 and args[1].lower() in ("remove", "del", "rm"):
        username = args[2].lstrip("@")
        if user_id in favorites:
            favorites[user_id] = [f for f in favorites[user_id] if f.lower() != username.lower()]
        await update.message.reply_text(t(user_id, "fav_removed", username=username))
        return

    # /fav - show favorites
    if user_id not in favorites or not favorites[user_id]:
        await update.message.reply_text(t(user_id, "fav_empty"))
        return

    fav_list = favorites[user_id]
    response = t(user_id, "fav_title", count=len(fav_list))
    keyboard = []
    for i, username in enumerate(fav_list, 1):
        response += f"{i}. @{username}\n"
        keyboard.append([InlineKeyboardButton(f"🔍 @{username}", callback_data=f"favsearch:{username}")])

    await update.message.reply_text(
        response,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


# ─── Search History ──────────────────────────────────────────────────


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command."""
    user_id = update.effective_user.id

    if user_id not in search_history or not search_history[user_id]:
        await update.message.reply_text(t(user_id, "history_empty"))
        return

    history = search_history[user_id][:10]
    response = t(user_id, "history_title")
    keyboard = []
    for i, entry in enumerate(history, 1):
        response += f"{i}. @{entry['username']} - {entry['time']}\n"
        keyboard.append([InlineKeyboardButton(f"🔍 @{entry['username']}", callback_data=f"favsearch:{entry['username']}")])

    await update.message.reply_text(
        response,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
    )


# ─── Error Handler ───────────────────────────────────────────────────


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors."""
    logger.error(f"Error: {context.error}")
    try:
        if update and update.effective_user:
            user_id = update.effective_user.id
            error_msg = t(user_id, "error_general")
        else:
            error_msg = "⚠️ An error occurred."

        if update.message:
            await update.message.reply_text(error_msg)
        elif update.callback_query:
            await update.callback_query.message.reply_text(error_msg)
    except Exception as e:
        logger.error(f"Error handler failed: {e}")


# ─── Main ────────────────────────────────────────────────────────────


def main():
    """Start the bot."""
    load_languages()

    if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("=" * 50)
        print("❌ ERROR: Bot token not set!")
        print("Please set your bot token in the .env file:")
        print("BOT_TOKEN=your_token_here")
        print("=" * 50)
        return

    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handlers
    search_conv = ConversationHandler(
        entry_points=[CommandHandler("search", search_command)],
        states={WAITING_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_username_input)]},
        fallbacks=[CommandHandler("start", start_command)],
    )

    id_conv = ConversationHandler(
        entry_points=[CommandHandler("id", id_command)],
        states={WAITING_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_id_input)]},
        fallbacks=[CommandHandler("start", start_command)],
    )

    video_conv = ConversationHandler(
        entry_points=[CommandHandler("video", video_command)],
        states={WAITING_VIDEO_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_video_input)]},
        fallbacks=[CommandHandler("start", start_command)],
    )

    compare_conv = ConversationHandler(
        entry_points=[CommandHandler("compare", compare_command)],
        states={WAITING_COMPARE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_compare_input)]},
        fallbacks=[CommandHandler("start", start_command)],
    )

    # Register handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("lang", lang_command))
    app.add_handler(CommandHandler("fav", fav_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(search_conv)
    app.add_handler(id_conv)
    app.add_handler(video_conv)
    app.add_handler(compare_conv)
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_pending_action))
    app.add_error_handler(error_handler)

    # Health check server for Render
    PORT = int(os.environ.get("PORT", 10000))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass

    def run_health_server():
        server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
        server.serve_forever()

    threading.Thread(target=run_health_server, daemon=True).start()

    print(f"🤖 Bot is running... (health check on port {PORT})")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
