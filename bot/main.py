from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .rating_api import RatingApiClient, RatingApiError, TournamentSummary
from .storage import BotStorage


ROLE_LABELS: dict[str, str] = {
    "player": "игрок",
    "captain": "капитан",
    "representative": "представитель",
}

MENU_LOGIN = "авторизоваться"
MENU_ROLE = "выбрать роль"
MENU_SYNC = "показать синхроны"
MENU_POLL = "создать опрос"
MENU_LOGOUT = "выйти"

POLL_OPTION_ANY = "Буду играть любой"
POLL_OPTION_NONE = "Не буду играть ни один"

PENDING_LOGIN_EMAIL = "login_email"
PENDING_LOGIN_PASSWORD = "login_password"
PENDING_REPRESENTATIVE_DATE = "representative_date"

DATE_PICKER_DAYS = 21
DATE_CALLBACK_IGNORE = "date:ignore"
DATE_CALLBACK_MANUAL = "date:manual"
DATE_CALLBACK_PICK_PREFIX = "date:pick:"

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")


@dataclass(slots=True)
class RuntimeState:
    pending_action: str | None = None
    temp_email: str | None = None
    selected_date: date | None = None
    tournaments: dict[int, TournamentSummary] = field(default_factory=dict)
    tournament_order: list[int] = field(default_factory=list)
    selected_tournament_ids: set[int] = field(default_factory=set)


def get_runtime_state(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> RuntimeState:
    runtime: dict[int, RuntimeState] = context.application.bot_data.setdefault("runtime", {})
    if user_id not in runtime:
        runtime[user_id] = RuntimeState()
    return runtime[user_id]


def get_storage(context: ContextTypes.DEFAULT_TYPE) -> BotStorage:
    return context.application.bot_data["storage"]


def get_rating_api(context: ContextTypes.DEFAULT_TYPE) -> RatingApiClient:
    return context.application.bot_data["rating_api"]


def hide_main_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove(remove_keyboard=True)


def tournament_markup(tournament_id: int, selected: bool) -> InlineKeyboardMarkup:
    text = "✅ В голосовании" if selected else "➕ Добавить в голосование"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text=text, callback_data=f"vote:{tournament_id}")]]
    )


def build_role_keyboard() -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for code, label in ROLE_LABELS.items():
        row.append(InlineKeyboardButton(label.title(), callback_data=f"role:{code}"))
        if len(row) == 2:
            buttons.append(row)
            row = []

    if row:
        buttons.append(row)

    return InlineKeyboardMarkup(buttons)


def get_date_picker_end(window_days: int = DATE_PICKER_DAYS) -> date:
    today = date.today()
    end_day = today + timedelta(days=window_days - 1)
    # Fill the last row fully to Sunday so there are no trailing placeholder dots.
    return end_day + timedelta(days=(6 - end_day.weekday()))


def build_date_picker_markup(
    *,
    window_days: int = DATE_PICKER_DAYS,
) -> InlineKeyboardMarkup:
    today = date.today()
    calendar_end = get_date_picker_end(window_days)
    calendar_start = today - timedelta(days=today.weekday())

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(day, callback_data=DATE_CALLBACK_IGNORE) for day in WEEKDAY_LABELS]
    ]

    cursor = calendar_start
    while cursor <= calendar_end:
        week_row: list[InlineKeyboardButton] = []
        for _ in range(7):
            if cursor < today:
                week_row.append(InlineKeyboardButton("·", callback_data=DATE_CALLBACK_IGNORE))
            else:
                label = str(cursor.day)
                if cursor == today:
                    label = f"•{cursor.day}"
                week_row.append(
                    InlineKeyboardButton(
                        label,
                        callback_data=f"{DATE_CALLBACK_PICK_PREFIX}{cursor.isoformat()}",
                    )
                )
            cursor += timedelta(days=1)
        rows.append(week_row)

    rows.append(
        [InlineKeyboardButton("✍️ Ввести дату вручную", callback_data=DATE_CALLBACK_MANUAL)]
    )
    return InlineKeyboardMarkup(rows)


def format_player_name(player: dict[str, Any]) -> str:
    if not isinstance(player, dict):
        return "Неизвестный редактор"

    parts = []
    for key in ("surname", "name", "patronymic"):
        value = player.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())

    if parts:
        return " ".join(parts)

    player_id = player.get("id")
    if player_id is None:
        return "Неизвестный редактор"

    return f"ID {player_id}"


def format_tournament_message(tournament: TournamentSummary) -> str:
    editors = ", ".join(format_player_name(item) for item in tournament.editors)
    if not editors:
        editors = "не указаны"

    difficulty_forecast = "не указана"
    if isinstance(tournament.difficulty_forecast, (int, float)):
        difficulty_forecast = f"{float(tournament.difficulty_forecast):.2f}"

    return (
        f"<b>{tournament.name}</b>\n"
        f"Редакторы: {editors}\n"
        f"Заявленная сложность: {difficulty_forecast}"
    )


def truncate_option(text: str, max_len: int = 100) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1].rstrip() + "…"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    runtime_state = get_runtime_state(context, update.effective_user.id)
    runtime_state.pending_action = None

    await update.message.reply_text(
        "Бот для rating.chgk.info готов.\n"
        "1) Введите /login\n"
        "2) Введите /role\n"
        "3) Для роли «представитель» нажмите /date\n"
        "4) После выбора турниров нажмите /poll",
        reply_markup=hide_main_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None
    state.temp_email = None

    await update.message.reply_text("Текущее действие отменено.", reply_markup=hide_main_keyboard())


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = PENDING_LOGIN_EMAIL
    state.temp_email = None

    await update.message.reply_text(
        "Введите email от rating.chgk.info.",
        reply_markup=hide_main_keyboard(),
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    storage = get_storage(context)
    storage.clear_rating_token(update.effective_user.id)

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None
    state.temp_email = None

    await update.message.reply_text("Сессия rating очищена.", reply_markup=hide_main_keyboard())


async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Выберите роль:",
        reply_markup=build_role_keyboard(),
    )


async def request_representative_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    storage = get_storage(context)
    persisted = storage.get_user_state(update.effective_user.id)

    if persisted.role != "representative":
        await update.message.reply_text(
            "Эта функция доступна для роли «представитель». Сначала выберите роль.",
            reply_markup=hide_main_keyboard(),
        )
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None

    today = date.today()
    end_day = get_date_picker_end(DATE_PICKER_DAYS)
    await update.message.reply_text(
        (
            "Выберите дату проведения синхрона.\n"
            f"Ближайший диапазон: {today.isoformat()} — {end_day.isoformat()}."
        ),
        reply_markup=build_date_picker_markup(),
    )


async def send_tournaments_for_date(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    target_date: date,
) -> None:
    storage = get_storage(context)
    api = get_rating_api(context)
    persisted = storage.get_user_state(user_id)
    runtime_state = get_runtime_state(context, user_id)

    await context.bot.send_message(chat_id=chat_id, text="Ищу синхроны на выбранную дату...")

    tournaments = await api.get_sync_tournaments_by_date(
        target_date,
        bearer_token=persisted.rating_token,
    )

    runtime_state.pending_action = None
    runtime_state.selected_date = target_date
    runtime_state.tournaments = {item.id: item for item in tournaments}
    runtime_state.tournament_order = [item.id for item in tournaments]
    runtime_state.selected_tournament_ids.clear()

    if not tournaments:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Синхроны на эту дату не найдены.",
            reply_markup=hide_main_keyboard(),
        )
        return

    for tournament in tournaments:
        await context.bot.send_message(
            chat_id=chat_id,
            text=format_tournament_message(tournament),
            parse_mode="HTML",
            reply_markup=tournament_markup(tournament.id, selected=False),
        )

    await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "Готово. Выберите турниры кнопками «Добавить в голосование», "
            "затем нажмите «Создать опрос»."
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("🗳 Создать опрос", callback_data="poll:create")]]
        ),
    )


async def create_poll(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> None:
    runtime_state = get_runtime_state(context, user_id)

    if not runtime_state.selected_tournament_ids:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Сначала выберите хотя бы 1 турнир для голосования.",
        )
        return

    ordered_selected_ids = [
        tournament_id
        for tournament_id in runtime_state.tournament_order
        if tournament_id in runtime_state.selected_tournament_ids
    ]

    if len(ordered_selected_ids) < 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Для опроса нужен хотя бы 1 выбранный турнир.",
        )
        return

    # Telegram supports max 10 options. 2 are reserved for fixed answers.
    if len(ordered_selected_ids) > 8:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Telegram поддерживает максимум 10 вариантов. "
                "Уберите лишние турниры (можно выбрать не более 8)."
            ),
        )
        return

    options: list[str] = []
    seen: set[str] = set()

    for tournament_id in ordered_selected_ids:
        tournament = runtime_state.tournaments.get(tournament_id)
        if tournament is None:
            continue

        option = truncate_option(tournament.name, 95)
        if option in seen:
            option = truncate_option(f"{option} #{tournament_id}")

        seen.add(option)
        options.append(option)

    if len(options) < 1:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не удалось собрать варианты для опроса. Попробуйте выбрать заново.",
        )
        return

    options.append(POLL_OPTION_ANY)
    options.append(POLL_OPTION_NONE)

    poll_date = runtime_state.selected_date.isoformat() if runtime_state.selected_date else "дата"
    question = f"За какие турниры голосуем? ({poll_date})"

    await context.bot.send_poll(
        chat_id=chat_id,
        question=question,
        options=options,
        is_anonymous=False,
        allows_multiple_answers=True,
    )


async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    await create_poll(
        context=context,
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or update.effective_user is None:
        return

    data = query.data or ""
    user_id = update.effective_user.id

    if data.startswith("role:"):
        role = data.split(":", 1)[1]
        if role not in ROLE_LABELS:
            await query.answer()
            return

        storage = get_storage(context)
        storage.upsert_role(user_id, role)

        await query.answer()
        await query.edit_message_text(f"Роль установлена: {ROLE_LABELS[role]}.")
        return

    if data.startswith("vote:"):
        raw_id = data.split(":", 1)[1]
        if not raw_id.isdigit():
            return

        tournament_id = int(raw_id)
        runtime_state = get_runtime_state(context, user_id)
        tournament = runtime_state.tournaments.get(tournament_id)

        if tournament is None:
            await query.answer("Сначала загрузите список турниров по дате.", show_alert=True)
            return

        if tournament_id in runtime_state.selected_tournament_ids:
            runtime_state.selected_tournament_ids.remove(tournament_id)
            selected_now = False
        else:
            runtime_state.selected_tournament_ids.add(tournament_id)
            selected_now = True

        await query.edit_message_reply_markup(
            reply_markup=tournament_markup(tournament_id, selected_now)
        )

        await query.answer(
            f"Выбрано турниров: {len(runtime_state.selected_tournament_ids)}",
            show_alert=False,
        )
        return

    if data == "poll:create":
        if update.effective_chat is None:
            await query.answer()
            return
        await query.answer()
        await create_poll(context=context, chat_id=update.effective_chat.id, user_id=user_id)
        return

    if data == DATE_CALLBACK_IGNORE:
        await query.answer()
        return

    if data == DATE_CALLBACK_MANUAL:
        state = get_runtime_state(context, user_id)
        state.pending_action = PENDING_REPRESENTATIVE_DATE
        await query.answer()
        await query.edit_message_text(
            "Введите дату в формате YYYY-MM-DD (например, 2026-03-10)."
        )
        return

    if data.startswith(DATE_CALLBACK_PICK_PREFIX):
        if update.effective_chat is None:
            await query.answer()
            return

        storage = get_storage(context)
        persisted = storage.get_user_state(user_id)
        if persisted.role != "representative":
            await query.answer("Сначала выберите роль «представитель».", show_alert=True)
            return

        raw_date = data.removeprefix(DATE_CALLBACK_PICK_PREFIX)
        try:
            target_date = date.fromisoformat(raw_date)
        except ValueError:
            await query.answer("Некорректная дата.", show_alert=True)
            return

        await query.answer(f"Выбрана дата: {target_date.isoformat()}")
        try:
            await query.edit_message_text(f"Дата выбрана: {target_date.isoformat()}")
        except Exception:
            pass

        state = get_runtime_state(context, user_id)
        state.pending_action = None

        try:
            await send_tournaments_for_date(
                context=context,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                target_date=target_date,
            )
        except RatingApiError as exc:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Не удалось получить турниры: {exc}",
                reply_markup=hide_main_keyboard(),
            )
        return

    await query.answer()


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    text = (update.message.text or "").strip()
    text_normalized = text.casefold()
    user_id = update.effective_user.id
    state = get_runtime_state(context, user_id)

    if text_normalized == MENU_LOGIN:
        await login(update, context)
        return
    if text_normalized == MENU_ROLE:
        await choose_role(update, context)
        return
    if text_normalized == MENU_SYNC:
        await request_representative_date(update, context)
        return
    if text_normalized == MENU_POLL:
        if update.effective_chat is None:
            return
        await create_poll(context=context, chat_id=update.effective_chat.id, user_id=user_id)
        return
    if text_normalized == MENU_LOGOUT:
        await logout(update, context)
        return

    if state.pending_action == PENDING_LOGIN_EMAIL:
        state.temp_email = text
        state.pending_action = PENDING_LOGIN_PASSWORD
        await update.message.reply_text("Теперь введите пароль от rating.chgk.info.")
        return

    if state.pending_action == PENDING_LOGIN_PASSWORD:
        if not state.temp_email:
            state.pending_action = PENDING_LOGIN_EMAIL
            await update.message.reply_text("Не найден email. Введите email заново.")
            return

        password = text
        api = get_rating_api(context)
        storage = get_storage(context)

        try:
            token = await api.login(state.temp_email, password)
            user_data = await api.get_current_user(token)
        except RatingApiError as exc:
            await update.message.reply_text(f"Авторизация не удалась: {exc}")
            return

        storage.upsert_rating_token(user_id, token)

        state.pending_action = None
        state.temp_email = None

        try:
            await update.message.delete()
        except Exception:
            pass

        player = user_data.get("player") if isinstance(user_data, dict) else None
        player_label = ""
        if isinstance(player, dict):
            player_label = format_player_name(player)

        suffix = f"\nПрофиль: {player_label}" if player_label else ""
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Авторизация успешна.{suffix}",
            reply_markup=hide_main_keyboard(),
        )
        return

    if state.pending_action == PENDING_REPRESENTATIVE_DATE:
        try:
            target_date = date.fromisoformat(text)
        except ValueError:
            await update.message.reply_text("Некорректная дата. Нужен формат YYYY-MM-DD.")
            return

        try:
            await send_tournaments_for_date(
                context=context,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                target_date=target_date,
            )
        except RatingApiError as exc:
            await update.message.reply_text(f"Не удалось получить турниры: {exc}")
        return

    await update.message.reply_text(
        "Не понял команду. Используйте /start или команды /login, /role, /date, /poll.",
        reply_markup=hide_main_keyboard(),
    )


def ensure_db_path(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_db_path = Path("/opt/render/project/src/bot.db")

    # Preserve previously saved role/token on first run after moving DB to persistent disk.
    if db_path != legacy_db_path and not db_path.exists() and legacy_db_path.exists():
        try:
            shutil.copy2(legacy_db_path, db_path)
        except OSError:
            pass

    return db_path


def main() -> None:
    asyncio.set_event_loop(asyncio.new_event_loop())

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not telegram_token:
        raise RuntimeError("Переменная TELEGRAM_BOT_TOKEN не задана")

    rating_api_base = os.getenv("RATING_API_BASE", "https://api.rating.chgk.info")
    rating_api_timeout = float(os.getenv("RATING_API_TIMEOUT", "15"))
    db_path = ensure_db_path(Path(os.getenv("BOT_DB_PATH", "bot.db")))

    application = Application.builder().token(telegram_token).build()

    application.bot_data["storage"] = BotStorage(db_path)
    application.bot_data["rating_api"] = RatingApiClient(
        rating_api_base,
        timeout_seconds=rating_api_timeout,
    )
    application.bot_data["runtime"] = {}

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("role", choose_role))
    application.add_handler(CommandHandler("date", request_representative_date))
    application.add_handler(CommandHandler("poll", poll_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
