from __future__ import annotations

import asyncio
import calendar
import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
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
from .rating_site import RatingSiteClient, RatingSiteError
from .storage import BotStorage


ROLE_LABELS: dict[str, str] = {
    "player": "игрок",
    "captain": "капитан",
    "representative": "представитель",
}

MENU_LOGIN = "Авторизация"
MENU_ROLE = "Выбрать роль"
MENU_CHANGE_ROLE = "Изменить роль"
MENU_SYNC = "Показать синхроны"
MENU_CREATE_VENUE = "Создать площадку"
MENU_LOGOUT = "Выйти"

MENU_POLL_LEGACY = "создать опрос"
MENU_REQUEST_LEGACY = "подать заявку"

POLL_OPTION_ANY = "Буду играть любой"
POLL_OPTION_NONE = "Не буду играть ни один"

PENDING_LOGIN_EMAIL = "login_email"
PENDING_LOGIN_PASSWORD = "login_password"
PENDING_REQUEST_SITE_PASSWORD = "request_site_password"
PENDING_REQUEST_TOURNAMENT = "request_tournament"
PENDING_REQUEST_VENUE = "request_venue"
PENDING_REQUEST_REPRESENTATIVE = "request_representative"
PENDING_REQUEST_DATE = "request_date"
PENDING_REQUEST_TEAMS = "request_teams"
PENDING_REQUEST_NARRATOR = "request_narrator"
PENDING_REQUEST_COMMENT = "request_comment"
PENDING_REQUEST_PASSWORD = "request_password"

DATE_PICKER_DAYS = 60
DATE_CALLBACK_IGNORE = "date:ignore"
DATE_CALLBACK_PICK_PREFIX = "date:pick:"
REQUEST_CALLBACK_START_PREFIX = "request:start:"
REQUEST_CALLBACK_VENUE_DEFAULT = "request:venue:default"
REQUEST_CALLBACK_VENUE_CUSTOM = "request:venue:custom"
REQUEST_CALLBACK_REPRESENTATIVE_DEFAULT = "request:representative:default"
REQUEST_CALLBACK_REPRESENTATIVE_CUSTOM = "request:representative:custom"

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
MONTH_LABELS = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
SKIP_MARKERS = {"-", "нет", "пропустить"}


@dataclass(slots=True)
class TournamentRequestDraft:
    tournament_id: int | None = None
    venue_id: int | None = None
    representative_id: int | None = None
    date_start: str | None = None
    approximate_teams_count: int | None = None
    narrator_id: int | None = None
    comment: str | None = None
    default_venue_id: int | None = None
    default_venue_label: str | None = None
    default_representative_id: int | None = None
    default_representative_label: str | None = None


@dataclass(slots=True)
class RuntimeState:
    pending_action: str | None = None
    temp_email: str | None = None
    site_password: str | None = None
    selected_date: date | None = None
    tournaments: dict[int, TournamentSummary] = field(default_factory=dict)
    tournament_order: list[int] = field(default_factory=list)
    selected_tournament_ids: set[int] = field(default_factory=set)
    request_draft: TournamentRequestDraft | None = None


def get_runtime_state(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> RuntimeState:
    runtime: dict[int, RuntimeState] = context.application.bot_data.setdefault("runtime", {})
    if user_id not in runtime:
        runtime[user_id] = RuntimeState()
    return runtime[user_id]


def get_storage(context: ContextTypes.DEFAULT_TYPE) -> BotStorage:
    return context.application.bot_data["storage"]


def get_rating_api(context: ContextTypes.DEFAULT_TYPE) -> RatingApiClient:
    return context.application.bot_data["rating_api"]


def get_rating_site(context: ContextTypes.DEFAULT_TYPE) -> RatingSiteClient:
    return context.application.bot_data["rating_site"]


def hide_main_keyboard() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def build_version_label() -> str:
    commit = (os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or "").strip()
    if not commit:
        return "local"
    return commit[:7]


def build_main_menu_markup(*, is_authorized: bool, role: str | None) -> ReplyKeyboardMarkup:
    if not is_authorized:
        keyboard = [[MENU_LOGIN]]
    elif role == "representative":
        keyboard = [
            [MENU_SYNC, MENU_CREATE_VENUE],
            [MENU_CHANGE_ROLE, MENU_LOGOUT],
        ]
    elif role in ROLE_LABELS:
        keyboard = [[MENU_CHANGE_ROLE, MENU_LOGOUT]]
    else:
        keyboard = [[MENU_ROLE, MENU_LOGOUT]]

    return ReplyKeyboardMarkup(
        keyboard=keyboard,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def main_menu_markup(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> ReplyKeyboardMarkup:
    storage = get_storage(context)
    persisted = storage.get_user_state(user_id)
    return build_main_menu_markup(
        is_authorized=bool(persisted.rating_token),
        role=persisted.role,
    )


def tournament_markup(tournament_id: int, selected: bool) -> InlineKeyboardMarkup:
    text = "✅ В голосовании" if selected else "➕ Добавить в голосование"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(text=text, callback_data=f"vote:{tournament_id}"),
                InlineKeyboardButton(
                    text="📝 Подать заявку",
                    callback_data=f"{REQUEST_CALLBACK_START_PREFIX}{tournament_id}",
                ),
            ]
        ]
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
    max_selectable = today + timedelta(days=window_days - 1)

    rows: list[list[InlineKeyboardButton]] = []

    month_cursor = date(today.year, today.month, 1)
    month_starts: list[date] = []
    while month_cursor <= max_selectable:
        month_starts.append(month_cursor)
        if month_cursor.month == 12:
            month_cursor = date(month_cursor.year + 1, 1, 1)
        else:
            month_cursor = date(month_cursor.year, month_cursor.month + 1, 1)

    for month_index, month_start in enumerate(month_starts):
        month_days = calendar.monthrange(month_start.year, month_start.month)[1]
        month_end = date(month_start.year, month_start.month, month_days)
        visible_start = max(today, month_start)
        visible_end = min(max_selectable, month_end)
        if visible_start > visible_end:
            continue

        rows.append(
            [
                InlineKeyboardButton(
                    f"{MONTH_LABELS[month_start.month - 1]} {month_start.year}",
                    callback_data=DATE_CALLBACK_IGNORE,
                )
            ]
        )
        rows.append([InlineKeyboardButton(day, callback_data=DATE_CALLBACK_IGNORE) for day in WEEKDAY_LABELS])

        grid_start = month_start - timedelta(days=month_start.weekday())
        grid_end = month_end + timedelta(days=(6 - month_end.weekday()))
        cursor = grid_start
        while cursor <= grid_end:
            week_row: list[InlineKeyboardButton] = []
            for _ in range(7):
                if visible_start <= cursor <= visible_end:
                    label = str(cursor.day)
                    if cursor == today:
                        label = f"•{cursor.day}"
                    week_row.append(
                        InlineKeyboardButton(
                            label,
                            callback_data=f"{DATE_CALLBACK_PICK_PREFIX}{cursor.isoformat()}",
                        )
                    )
                else:
                    week_row.append(InlineKeyboardButton("·", callback_data=DATE_CALLBACK_IGNORE))
                cursor += timedelta(days=1)
            rows.append(week_row)

        if month_index < len(month_starts) - 1:
            rows.append([InlineKeyboardButton("·", callback_data=DATE_CALLBACK_IGNORE)])

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


def reset_request_draft(state: RuntimeState) -> None:
    state.request_draft = None
    if state.pending_action in {
        PENDING_REQUEST_SITE_PASSWORD,
        PENDING_REQUEST_TOURNAMENT,
        PENDING_REQUEST_VENUE,
        PENDING_REQUEST_REPRESENTATIVE,
        PENDING_REQUEST_DATE,
        PENDING_REQUEST_TEAMS,
        PENDING_REQUEST_NARRATOR,
        PENDING_REQUEST_COMMENT,
        PENDING_REQUEST_PASSWORD,
    }:
        state.pending_action = None


def parse_request_date_start(raw: str) -> str:
    parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    return parsed.strftime("%Y-%m-%d %H:%M")


def is_expired_jwt_error(exc: RatingApiError) -> bool:
    message = str(exc).casefold()
    return (
        "expired jwt token" in message
        or ("jwt" in message and "expired" in message)
        or ("token" in message and "expired" in message)
    )


async def notify_tournaments_error(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    exc: RatingApiError,
) -> None:
    if is_expired_jwt_error(exc):
        storage = get_storage(context)
        storage.clear_rating_token(user_id)
        state = get_runtime_state(context, user_id)
        state.pending_action = None
        await context.bot.send_message(
            chat_id=chat_id,
            text="Сессия истекла. Нажмите «Авторизация» и войдите снова.",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Не удалось получить турниры: {exc}",
        reply_markup=main_menu_markup(context, user_id),
    )


def get_callback_chat_id(update: Update) -> int | None:
    query = update.callback_query
    if query is not None and query.message is not None and query.message.chat is not None:
        return query.message.chat.id
    if update.effective_chat is not None:
        return update.effective_chat.id
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    runtime_state = get_runtime_state(context, update.effective_user.id)
    runtime_state.pending_action = None
    runtime_state.request_draft = None

    storage = get_storage(context)
    persisted = storage.get_user_state(update.effective_user.id)

    if not persisted.rating_token:
        text = "Бот готов. Нажмите «Авторизация» в меню."
    elif persisted.role == "representative":
        text = "Вы авторизованы как представитель. Выберите действие в меню."
    elif persisted.role in ROLE_LABELS:
        text = f"Вы авторизованы. Текущая роль: {ROLE_LABELS[persisted.role]}."
    else:
        text = "Вы авторизованы. Теперь выберите роль."

    await update.message.reply_text(
        text,
        reply_markup=main_menu_markup(context, update.effective_user.id),
    )


async def version_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text(f"Версия: {build_version_label()}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None
    state.temp_email = None
    state.request_draft = None

    await update.message.reply_text(
        "Текущее действие отменено.",
        reply_markup=main_menu_markup(context, update.effective_user.id),
    )


async def login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = PENDING_LOGIN_EMAIL
    state.temp_email = None

    await update.message.reply_text(
        "Введите email от rating.chgk.info.",
        reply_markup=main_menu_markup(context, update.effective_user.id),
    )


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    storage = get_storage(context)
    storage.clear_rating_token(update.effective_user.id)

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None
    state.temp_email = None
    state.site_password = None
    state.request_draft = None

    await update.message.reply_text(
        "Сессия rating очищена.",
        reply_markup=main_menu_markup(context, update.effective_user.id),
    )


async def choose_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    await update.message.reply_text(
        "Выберите роль:",
        reply_markup=build_role_keyboard(),
    )


async def create_venue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    storage = get_storage(context)
    persisted = storage.get_user_state(update.effective_user.id)

    if not persisted.rating_token:
        text = "Сначала авторизуйтесь через меню."
    elif persisted.role != "representative":
        text = "Функция доступна только для роли «представитель»."
    else:
        text = (
            "Создание площадки в боте пока отключено.\n"
            "Используйте форму на rating.chgk.info, затем вернитесь в бот."
        )

    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=text,
        reply_markup=main_menu_markup(context, update.effective_user.id),
    )


async def request_representative_date(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    storage = get_storage(context)
    persisted = storage.get_user_state(update.effective_user.id)

    if persisted.role != "representative":
        await update.message.reply_text(
            "Эта функция доступна для роли «представитель». Сначала выберите роль.",
            reply_markup=main_menu_markup(context, update.effective_user.id),
        )
        return
    if not persisted.rating_token:
        await update.message.reply_text(
            "Сессия неактивна. Нажмите «Авторизация» и войдите снова.",
            reply_markup=main_menu_markup(context, update.effective_user.id),
        )
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None

    today = date.today()
    end_day = today + timedelta(days=DATE_PICKER_DAYS - 1)
    await update.message.reply_text(
        (
            "Выберите дату проведения синхрона.\n"
            f"Доступный диапазон: {today.isoformat()} — {end_day.isoformat()}."
        ),
        reply_markup=build_date_picker_markup(),
    )


async def load_request_prefill(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    tournament_id: int,
) -> None:
    storage = get_storage(context)
    persisted = storage.get_user_state(user_id)
    state = get_runtime_state(context, user_id)
    draft = state.request_draft

    if draft is None:
        return

    draft.default_venue_id = persisted.default_venue_id
    draft.default_venue_label = persisted.default_venue_label
    draft.default_representative_id = persisted.player_id
    draft.default_representative_label = persisted.player_name

    if not persisted.rating_email or not state.site_password:
        return

    rating_site = get_rating_site(context)
    try:
        prefill = await rating_site.get_request_prefill(
            email=persisted.rating_email,
            password=state.site_password,
            tournament_id=tournament_id,
        )
    except RatingSiteError as exc:
        logging.info(
            "Request prefill unavailable for user_id=%s tournament_id=%s: %s",
            user_id,
            tournament_id,
            exc,
        )
        return

    if prefill.venue_id is not None:
        draft.default_venue_id = prefill.venue_id
        draft.default_venue_label = prefill.venue_label or f"ID {prefill.venue_id}"

    if prefill.representative_id is not None:
        draft.default_representative_id = prefill.representative_id
        draft.default_representative_label = (
            prefill.representative_label or f"ID {prefill.representative_id}"
        )

    if prefill.approximate_teams_count is not None:
        draft.approximate_teams_count = prefill.approximate_teams_count


async def prompt_request_venue_step(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> None:
    state = get_runtime_state(context, user_id)
    draft = state.request_draft
    state.pending_action = PENDING_REQUEST_VENUE

    if draft is None:
        await context.bot.send_message(chat_id=chat_id, text="Черновик заявки потерян. Начните снова: /request")
        return

    if draft.default_venue_id is not None:
        venue_label = draft.default_venue_label or f"ID {draft.default_venue_id}"
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Шаг 1/7: площадка.\n"
                f"По умолчанию: {venue_label}\n"
                "Выберите действие:"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Использовать площадку по умолчанию", callback_data=REQUEST_CALLBACK_VENUE_DEFAULT)],
                    [InlineKeyboardButton("✏️ Выбрать другую площадку", callback_data=REQUEST_CALLBACK_VENUE_CUSTOM)],
                ]
            ),
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="Шаг 1/7: введите ID площадки (venue ID).",
        reply_markup=main_menu_markup(context, user_id),
    )


async def prompt_request_representative_step(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> None:
    state = get_runtime_state(context, user_id)
    draft = state.request_draft
    state.pending_action = PENDING_REQUEST_REPRESENTATIVE

    if draft is None:
        await context.bot.send_message(chat_id=chat_id, text="Черновик заявки потерян. Начните снова: /request")
        return

    if draft.default_representative_id is not None:
        representative_label = (
            draft.default_representative_label or f"ID {draft.default_representative_id}"
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Шаг 2/7: представитель.\n"
                f"По умолчанию: {representative_label}\n"
                "Выберите действие:"
            ),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✅ Использовать представителя по умолчанию", callback_data=REQUEST_CALLBACK_REPRESENTATIVE_DEFAULT)],
                    [InlineKeyboardButton("✏️ Выбрать другого представителя", callback_data=REQUEST_CALLBACK_REPRESENTATIVE_CUSTOM)],
                ]
            ),
        )
        return

    await context.bot.send_message(
        chat_id=chat_id,
        text="Шаг 2/7: введите ID представителя.",
        reply_markup=main_menu_markup(context, user_id),
    )


async def continue_request_flow(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
) -> None:
    state = get_runtime_state(context, user_id)
    draft = state.request_draft
    if draft is None:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Черновик заявки потерян. Начните снова: /request",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    tournament_id = draft.tournament_id
    if tournament_id is None:
        state.pending_action = PENDING_REQUEST_TOURNAMENT
        if state.tournament_order:
            sample = ", ".join(str(item) for item in state.tournament_order[:10])
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Введите ID турнира для подачи заявки.\n"
                    f"Недавно загруженные ID: {sample}"
                ),
                reply_markup=main_menu_markup(context, user_id),
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Введите ID турнира (число).",
                reply_markup=main_menu_markup(context, user_id),
            )
        return

    await load_request_prefill(
        context=context,
        user_id=user_id,
        tournament_id=tournament_id,
    )
    await prompt_request_venue_step(
        context=context,
        chat_id=chat_id,
        user_id=user_id,
    )


async def start_request_flow(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    tournament_id: int | None = None,
) -> None:
    storage = get_storage(context)
    persisted = storage.get_user_state(user_id)
    state = get_runtime_state(context, user_id)

    if persisted.role != "representative":
        await context.bot.send_message(
            chat_id=chat_id,
            text="Подача заявки доступна только для роли «представитель».",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if not persisted.rating_email:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Нужно выполнить /login, чтобы сохранить email для входа на сайт.",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if tournament_id is not None and tournament_id <= 0:
        tournament_id = None

    state.request_draft = TournamentRequestDraft(tournament_id=tournament_id)
    if not state.site_password:
        state.pending_action = PENDING_REQUEST_SITE_PASSWORD
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Введите пароль от rating.chgk.info для автоподстановки данных заявки.\n"
                "Сообщение с паролем будет удалено."
            ),
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    await continue_request_flow(
        context=context,
        chat_id=chat_id,
        user_id=user_id,
    )


async def request_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    await start_request_flow(
        context=context,
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id,
        tournament_id=None,
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
            reply_markup=main_menu_markup(context, user_id),
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
            "Готово. Выберите турниры кнопками «Добавить в голосование».\n"
            "Для подачи заявки используйте кнопку «Подать заявку» или /request.\n"
            "Для опроса нажмите «Создать опрос»."
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
    callback_chat_id = get_callback_chat_id(update)

    if data.startswith("role:"):
        role = data.split(":", 1)[1]
        if role not in ROLE_LABELS:
            await query.answer()
            return

        storage = get_storage(context)
        storage.upsert_role(user_id, role)

        await query.answer()
        await query.edit_message_text(f"Роль установлена: {ROLE_LABELS[role]}.")
        if callback_chat_id is not None:
            await context.bot.send_message(
                chat_id=callback_chat_id,
                text="Роль обновлена. Выберите действие в меню.",
                reply_markup=main_menu_markup(context, user_id),
            )
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

    if data.startswith(REQUEST_CALLBACK_START_PREFIX):
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return

        raw_id = data.removeprefix(REQUEST_CALLBACK_START_PREFIX)
        tournament_id = int(raw_id) if raw_id.isdigit() else None
        await query.answer()
        await start_request_flow(
            context=context,
            chat_id=callback_chat_id,
            user_id=user_id,
            tournament_id=tournament_id,
        )
        return

    if data == REQUEST_CALLBACK_VENUE_DEFAULT:
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return
        state = get_runtime_state(context, user_id)
        draft = state.request_draft
        if draft is None or draft.default_venue_id is None:
            await query.answer("Площадка по умолчанию не найдена.", show_alert=True)
            return

        draft.venue_id = draft.default_venue_id
        await query.answer("Площадка выбрана.")
        await prompt_request_representative_step(
            context=context,
            chat_id=callback_chat_id,
            user_id=user_id,
        )
        return

    if data == REQUEST_CALLBACK_VENUE_CUSTOM:
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return
        state = get_runtime_state(context, user_id)
        state.pending_action = PENDING_REQUEST_VENUE
        await query.answer()
        await context.bot.send_message(
            chat_id=callback_chat_id,
            text="Шаг 1/7: введите ID площадки (venue ID).",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if data == REQUEST_CALLBACK_REPRESENTATIVE_DEFAULT:
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return
        state = get_runtime_state(context, user_id)
        draft = state.request_draft
        if draft is None or draft.default_representative_id is None:
            await query.answer("Представитель по умолчанию не найден.", show_alert=True)
            return

        draft.representative_id = draft.default_representative_id
        state.pending_action = PENDING_REQUEST_DATE
        await query.answer("Представитель выбран.")
        await context.bot.send_message(
            chat_id=callback_chat_id,
            text="Шаг 3/7: введите дату и время проведения в формате YYYY-MM-DD HH:MM (UTC+7).",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if data == REQUEST_CALLBACK_REPRESENTATIVE_CUSTOM:
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return
        state = get_runtime_state(context, user_id)
        state.pending_action = PENDING_REQUEST_REPRESENTATIVE
        await query.answer()
        await context.bot.send_message(
            chat_id=callback_chat_id,
            text="Шаг 2/7: введите ID представителя.",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if data == "poll:create":
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
            return
        await query.answer()
        await create_poll(context=context, chat_id=callback_chat_id, user_id=user_id)
        return

    if data == DATE_CALLBACK_IGNORE:
        await query.answer()
        return

    if data.startswith(DATE_CALLBACK_PICK_PREFIX):
        if callback_chat_id is None:
            await query.answer("Не удалось определить чат.", show_alert=True)
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

        today = date.today()
        max_day = today + timedelta(days=DATE_PICKER_DAYS - 1)
        if target_date < today or target_date > max_day:
            await query.answer("Можно выбрать только дату из ближайших 60 дней.", show_alert=True)
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
                chat_id=callback_chat_id,
                user_id=user_id,
                target_date=target_date,
            )
        except RatingApiError as exc:
            await notify_tournaments_error(
                context=context,
                chat_id=callback_chat_id,
                user_id=user_id,
                exc=exc,
            )
        return

    await query.answer()


async def submit_request_draft(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    password: str,
) -> None:
    state = get_runtime_state(context, user_id)
    draft = state.request_draft
    if draft is None:
        state.pending_action = None
        await context.bot.send_message(
            chat_id=chat_id,
            text="Черновик заявки потерян. Начните снова: /request",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if (
        draft.tournament_id is None
        or draft.venue_id is None
        or draft.representative_id is None
        or draft.date_start is None
        or draft.narrator_id is None
    ):
        state.pending_action = None
        await context.bot.send_message(
            chat_id=chat_id,
            text="Черновик заявки неполный. Начните снова: /request",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    storage = get_storage(context)
    persisted = storage.get_user_state(user_id)
    if not persisted.rating_email:
        state.pending_action = None
        await context.bot.send_message(
            chat_id=chat_id,
            text="Не найден email. Выполните /login и повторите.",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    rating_site = get_rating_site(context)
    state.site_password = password
    await context.bot.send_message(chat_id=chat_id, text="Отправляю заявку на сайт...")

    try:
        result = await rating_site.submit_tournament_request(
            email=persisted.rating_email,
            password=password,
            tournament_id=draft.tournament_id,
            venue_id=draft.venue_id,
            representative_id=draft.representative_id,
            date_start=draft.date_start,
            approximate_teams_count=draft.approximate_teams_count,
            narrator_id=draft.narrator_id,
            comment=draft.comment,
        )
    except RatingSiteError as exc:
        reset_request_draft(state)
        logging.warning(
            "Tournament request submit failed for user_id=%s email=%s tournament_id=%s: %s",
            user_id,
            persisted.rating_email,
            draft.tournament_id,
            exc,
        )
        await context.bot.send_message(chat_id=chat_id, text=f"Не удалось отправить заявку: {exc}")
        return

    storage.upsert_default_venue(
        user_id,
        venue_id=draft.venue_id,
        venue_label=draft.default_venue_label or f"ID {draft.venue_id}",
    )
    reset_request_draft(state)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"{result.message}\nURL: {result.final_url}",
        reply_markup=main_menu_markup(context, user_id),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    text = (update.message.text or "").strip()
    text_normalized = text.casefold()
    user_id = update.effective_user.id
    state = get_runtime_state(context, user_id)

    if text_normalized == MENU_LOGIN.casefold():
        await login(update, context)
        return
    if text_normalized in {MENU_ROLE.casefold(), MENU_CHANGE_ROLE.casefold()}:
        await choose_role(update, context)
        return
    if text_normalized == MENU_SYNC.casefold():
        await request_representative_date(update, context)
        return
    if text_normalized == MENU_CREATE_VENUE.casefold():
        await create_venue(update, context)
        return
    if text_normalized == MENU_POLL_LEGACY.casefold():
        if update.effective_chat is None:
            return
        await create_poll(context=context, chat_id=update.effective_chat.id, user_id=user_id)
        return
    if text_normalized == MENU_REQUEST_LEGACY.casefold():
        if update.effective_chat is None:
            return
        await start_request_flow(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
            tournament_id=None,
        )
        return
    if text_normalized == MENU_LOGOUT.casefold():
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

        storage.upsert_rating_email(user_id, state.temp_email)
        storage.upsert_rating_token(user_id, token)
        player = user_data.get("player") if isinstance(user_data, dict) else None
        player_id: int | None = None
        player_name: str | None = None
        if isinstance(player, dict):
            raw_id = player.get("id")
            if isinstance(raw_id, int):
                player_id = raw_id
            elif isinstance(raw_id, str) and raw_id.isdigit():
                player_id = int(raw_id)

            formatted_name = format_player_name(player)
            if formatted_name and formatted_name != "Неизвестный редактор":
                player_name = formatted_name
        storage.upsert_player_profile(
            user_id,
            player_id=player_id,
            player_name=player_name,
        )

        state.pending_action = None
        state.temp_email = None
        state.site_password = password

        try:
            await update.message.delete()
        except Exception:
            pass

        player_label = ""
        if isinstance(player, dict):
            player_label = format_player_name(player)

        suffix = f"\nПрофиль: {player_label}" if player_label else ""
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Авторизация успешна.{suffix}",
            reply_markup=main_menu_markup(context, user_id),
        )
        return

    if state.pending_action == PENDING_REQUEST_SITE_PASSWORD:
        if state.request_draft is None:
            state.pending_action = None
            await update.message.reply_text("Черновик заявки потерян. Начните снова: /request")
            return

        password = text.strip()
        if not password:
            await update.message.reply_text("Пароль не может быть пустым.")
            return

        state.site_password = password
        try:
            await update.message.delete()
        except Exception:
            pass

        await continue_request_flow(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
        return

    if state.pending_action == PENDING_REQUEST_TOURNAMENT:
        if state.request_draft is None:
            state.request_draft = TournamentRequestDraft()

        if not text.isdigit():
            await update.message.reply_text("ID турнира должен быть числом.")
            return

        tournament_id = int(text)
        if tournament_id <= 0:
            await update.message.reply_text("ID турнира должен быть положительным числом.")
            return

        state.request_draft.tournament_id = tournament_id
        await continue_request_flow(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
        return

    if state.pending_action == PENDING_REQUEST_VENUE:
        if state.request_draft is None or state.request_draft.tournament_id is None:
            state.pending_action = PENDING_REQUEST_TOURNAMENT
            await update.message.reply_text("Сначала укажите ID турнира.")
            return

        if not text.isdigit():
            await update.message.reply_text("ID площадки должен быть числом.")
            return

        venue_id = int(text)
        if venue_id <= 0:
            await update.message.reply_text("ID площадки должен быть положительным числом.")
            return

        state.request_draft.venue_id = venue_id
        state.request_draft.default_venue_id = venue_id
        state.request_draft.default_venue_label = f"ID {venue_id}"
        await prompt_request_representative_step(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
        )
        return

    if state.pending_action == PENDING_REQUEST_REPRESENTATIVE:
        if state.request_draft is None or state.request_draft.venue_id is None:
            state.pending_action = PENDING_REQUEST_VENUE
            await update.message.reply_text("Сначала укажите площадку.")
            return

        if not text.isdigit():
            await update.message.reply_text("ID представителя должен быть числом.")
            return

        representative_id = int(text)
        if representative_id <= 0:
            await update.message.reply_text("ID представителя должен быть положительным числом.")
            return

        state.request_draft.representative_id = representative_id
        state.pending_action = PENDING_REQUEST_DATE
        await update.message.reply_text(
            "Шаг 3/7: введите дату и время проведения в формате YYYY-MM-DD HH:MM (UTC+7)."
        )
        return

    if state.pending_action == PENDING_REQUEST_DATE:
        if (
            state.request_draft is None
            or state.request_draft.venue_id is None
            or state.request_draft.representative_id is None
        ):
            state.pending_action = PENDING_REQUEST_REPRESENTATIVE
            await update.message.reply_text("Сначала укажите представителя.")
            return

        try:
            date_start = parse_request_date_start(text)
        except ValueError:
            await update.message.reply_text("Нужен формат YYYY-MM-DD HH:MM, например 2026-03-14 19:00.")
            return

        state.request_draft.date_start = date_start
        state.pending_action = PENDING_REQUEST_TEAMS
        teams_default = ""
        if state.request_draft.approximate_teams_count is not None:
            teams_default = f" (по умолчанию: {state.request_draft.approximate_teams_count})"
        await update.message.reply_text(
            f"Шаг 4/7: примерное количество команд (число) или «-», чтобы пропустить{teams_default}."
        )
        return

    if state.pending_action == PENDING_REQUEST_TEAMS:
        if state.request_draft is None or state.request_draft.date_start is None:
            state.pending_action = PENDING_REQUEST_DATE
            await update.message.reply_text("Сначала укажите дату проведения.")
            return

        if text.casefold() in SKIP_MARKERS:
            state.request_draft.approximate_teams_count = None
        else:
            if not text.isdigit():
                await update.message.reply_text("Нужно число команд или «-».")
                return
            teams_count = int(text)
            if teams_count <= 0:
                await update.message.reply_text("Количество команд должно быть больше нуля.")
                return
            state.request_draft.approximate_teams_count = teams_count

        state.pending_action = PENDING_REQUEST_NARRATOR
        await update.message.reply_text("Шаг 5/7: ID ведущего (чтеца).")
        return

    if state.pending_action == PENDING_REQUEST_NARRATOR:
        if state.request_draft is None:
            state.pending_action = None
            await update.message.reply_text("Черновик заявки потерян. Начните снова: /request")
            return

        if not text.isdigit():
            await update.message.reply_text("Нужно число (ID ведущего).")
            return
        narrator_id = int(text)
        if narrator_id <= 0:
            await update.message.reply_text("ID ведущего должен быть положительным.")
            return
        state.request_draft.narrator_id = narrator_id

        state.pending_action = PENDING_REQUEST_COMMENT
        await update.message.reply_text("Шаг 6/7: комментарий или «-», чтобы пропустить.")
        return

    if state.pending_action == PENDING_REQUEST_COMMENT:
        if state.request_draft is None:
            state.pending_action = None
            await update.message.reply_text("Черновик заявки потерян. Начните снова: /request")
            return

        state.request_draft.comment = None if text.casefold() in SKIP_MARKERS else text
        if state.site_password:
            state.pending_action = PENDING_REQUEST_PASSWORD
            await update.message.reply_text("Шаг 7/7: использую ранее введённый пароль.")
            await submit_request_draft(
                context=context,
                chat_id=update.effective_chat.id,
                user_id=user_id,
                password=state.site_password,
            )
        else:
            state.pending_action = PENDING_REQUEST_PASSWORD
            storage = get_storage(context)
            persisted = storage.get_user_state(user_id)
            email_label = persisted.rating_email or "(не указан)"
            await update.message.reply_text(
                (
                    "Шаг 7/7: введите пароль от rating.chgk.info для отправки заявки.\n"
                    f"Используется email: {email_label}\n"
                    "Если нужен другой аккаунт, сначала выполните «Авторизация»."
                )
            )
        return

    if state.pending_action == PENDING_REQUEST_PASSWORD:
        password = text.strip()
        if not password:
            await update.message.reply_text("Пароль не может быть пустым.")
            return
        state.site_password = password
        try:
            await update.message.delete()
        except Exception:
            pass
        await submit_request_draft(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
            password=password,
        )
        return

    await update.message.reply_text(
        "Не понял сообщение. Используйте кнопки меню или /start.",
        reply_markup=main_menu_markup(context, user_id),
    )


async def handle_unmatched_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    command_token = (update.message.text or "").strip().split(maxsplit=1)[0]
    command_name = command_token[1:].split("@", 1)[0].casefold() if command_token.startswith("/") else ""

    if command_name == "start":
        await start(update, context)
        return
    if command_name == "login":
        await login(update, context)
        return
    if command_name == "logout":
        await logout(update, context)
        return
    if command_name == "role":
        await choose_role(update, context)
        return
    if command_name == "date":
        await request_representative_date(update, context)
        return
    if command_name == "request":
        await request_command(update, context)
        return
    if command_name == "poll":
        await poll_command(update, context)
        return
    if command_name == "version":
        await version_command(update, context)
        return
    if command_name in {"create_venue", "venue"}:
        await create_venue(update, context)
        return
    if command_name == "cancel":
        await cancel(update, context)
        return

    await update.message.reply_text(
        (
            "Команда не распознана. Доступно: /start, /login, /role, /date, "
            "/poll, /request, /create_venue, /cancel, /logout, /version."
        ),
        reply_markup=main_menu_markup(context, update.effective_user.id)
        if update.effective_user is not None
        else hide_main_keyboard(),
    )


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.exception("Unhandled exception while processing update", exc_info=context.error)

    chat_id: int | None = None
    user_id: int | None = None
    if isinstance(update, Update):
        if update.effective_user is not None:
            user_id = update.effective_user.id
        if update.effective_chat is not None:
            chat_id = update.effective_chat.id
        elif (
            update.callback_query is not None
            and update.callback_query.message is not None
            and update.callback_query.message.chat is not None
        ):
            chat_id = update.callback_query.message.chat.id

    if chat_id is not None:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Произошла внутренняя ошибка. "
                    "Попробуйте повторить команду через несколько секунд."
                ),
                reply_markup=main_menu_markup(context, user_id)
                if user_id is not None
                else hide_main_keyboard(),
            )
        except Exception:
            logging.exception("Failed to notify user about internal error")


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
    rating_site_base = os.getenv("RATING_SITE_BASE", "https://rating.chgk.info")
    rating_site_timeout = float(os.getenv("RATING_SITE_TIMEOUT", "20"))
    db_path = ensure_db_path(Path(os.getenv("BOT_DB_PATH", "bot.db")))

    application = Application.builder().token(telegram_token).build()

    application.bot_data["storage"] = BotStorage(db_path)
    application.bot_data["rating_api"] = RatingApiClient(
        rating_api_base,
        timeout_seconds=rating_api_timeout,
    )
    application.bot_data["rating_site"] = RatingSiteClient(
        rating_site_base,
        timeout_seconds=rating_site_timeout,
    )
    application.bot_data["runtime"] = {}

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("role", choose_role))
    application.add_handler(CommandHandler("date", request_representative_date))
    application.add_handler(CommandHandler("request", request_command))
    application.add_handler(CommandHandler("poll", poll_command))
    application.add_handler(CommandHandler("version", version_command))
    application.add_handler(CommandHandler("create_venue", create_venue))
    application.add_handler(CommandHandler("venue", create_venue))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(MessageHandler(filters.COMMAND, handle_unmatched_command))
    application.add_error_handler(handle_error)

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
