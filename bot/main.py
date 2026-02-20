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

from .rating_api import (
    RatingApiClient,
    RatingApiError,
    TournamentSummary,
    TownSummary,
    VenueTypeSummary,
)
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
MENU_CREATE_VENUE = "создать площадку"
MENU_LOGOUT = "выйти"

POLL_OPTION_ANY = "Буду играть любой"
POLL_OPTION_NONE = "Не буду играть ни один"

PENDING_LOGIN_EMAIL = "login_email"
PENDING_LOGIN_PASSWORD = "login_password"
PENDING_REPRESENTATIVE_DATE = "representative_date"
PENDING_VENUE_TOWN = "venue_town"
PENDING_VENUE_NAME = "venue_name"
PENDING_VENUE_TYPE = "venue_type"
PENDING_VENUE_ADDRESS = "venue_address"
PENDING_VENUE_URLS = "venue_urls"

DATE_PICKER_DAYS = 21
DATE_CALLBACK_IGNORE = "date:ignore"
DATE_CALLBACK_MANUAL = "date:manual"
DATE_CALLBACK_PICK_PREFIX = "date:pick:"

WEEKDAY_LABELS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")

VENUE_CALLBACK_START_PREFIX = "venue:start:"
VENUE_CALLBACK_TOWN_PREFIX = "venue:town:"
VENUE_CALLBACK_TOWN_RETRY = "venue:town:retry"
VENUE_CALLBACK_TYPE_PREFIX = "venue:type:"
VENUE_CALLBACK_SUBMIT = "venue:submit"
VENUE_CALLBACK_CANCEL = "venue:cancel"

SKIP_MARKERS = {"-", "нет", "пропустить"}


@dataclass(slots=True)
class VenueDraft:
    tournament_id: int | None = None
    town_id: int | None = None
    town_name: str | None = None
    venue_name: str | None = None
    venue_type_id: int | None = None
    venue_type_name: str | None = None
    address: str | None = None
    urls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeState:
    pending_action: str | None = None
    temp_email: str | None = None
    selected_date: date | None = None
    tournaments: dict[int, TournamentSummary] = field(default_factory=dict)
    tournament_order: list[int] = field(default_factory=list)
    selected_tournament_ids: set[int] = field(default_factory=set)
    venue_draft: VenueDraft | None = None
    town_candidates: dict[int, TownSummary] = field(default_factory=dict)
    venue_type_candidates: dict[int, VenueTypeSummary] = field(default_factory=dict)


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
        [
            [
                InlineKeyboardButton(text=text, callback_data=f"vote:{tournament_id}"),
                InlineKeyboardButton(
                    text="🏟 Создать площадку",
                    callback_data=f"{VENUE_CALLBACK_START_PREFIX}{tournament_id}",
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


def reset_venue_draft(state: RuntimeState) -> None:
    state.pending_action = None
    state.venue_draft = None
    state.town_candidates.clear()
    state.venue_type_candidates.clear()


def format_town_label(town: TownSummary) -> str:
    location_parts = [item for item in (town.region_name, town.country_name) if item]
    if not location_parts:
        return town.name
    location = ", ".join(location_parts)
    return f"{town.name} ({location})"


def build_town_markup(candidates: list[TownSummary]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for town in candidates:
        rows.append(
            [
                InlineKeyboardButton(
                    text=truncate_option(format_town_label(town), 60),
                    callback_data=f"{VENUE_CALLBACK_TOWN_PREFIX}{town.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("🔁 Другой поиск", callback_data=VENUE_CALLBACK_TOWN_RETRY)])
    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data=VENUE_CALLBACK_CANCEL)])
    return InlineKeyboardMarkup(rows)


def build_venue_types_markup(venue_types: list[VenueTypeSummary]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for venue_type in venue_types:
        row.append(
            InlineKeyboardButton(
                text=truncate_option(venue_type.name, 28),
                callback_data=f"{VENUE_CALLBACK_TYPE_PREFIX}{venue_type.id}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton("✖️ Отмена", callback_data=VENUE_CALLBACK_CANCEL)])
    return InlineKeyboardMarkup(rows)


def build_venue_submit_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Создать площадку", callback_data=VENUE_CALLBACK_SUBMIT),
                InlineKeyboardButton("✖️ Отмена", callback_data=VENUE_CALLBACK_CANCEL),
            ]
        ]
    )


def format_venue_draft_summary(draft: VenueDraft) -> str:
    address = draft.address or "не указан"
    urls = ", ".join(draft.urls) if draft.urls else "не указаны"
    town = draft.town_name or "не выбран"
    venue_type = draft.venue_type_name or "не выбран"
    venue_name = draft.venue_name or "не указано"

    return (
        "Проверьте данные площадки:\n"
        f"Город: {town}\n"
        f"Название: {venue_name}\n"
        f"Тип: {venue_type}\n"
        f"Адрес: {address}\n"
        f"Ссылки: {urls}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    runtime_state = get_runtime_state(context, update.effective_user.id)
    runtime_state.pending_action = None
    runtime_state.venue_draft = None
    runtime_state.town_candidates.clear()
    runtime_state.venue_type_candidates.clear()

    await update.message.reply_text(
        "Бот для rating.chgk.info готов.\n"
        "1) Введите /login\n"
        "2) Введите /role\n"
        "3) Для роли «представитель» нажмите /date\n"
        "4) Для создания площадки нажмите /venue\n"
        "5) После выбора турниров нажмите /poll",
        reply_markup=hide_main_keyboard(),
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return

    state = get_runtime_state(context, update.effective_user.id)
    state.pending_action = None
    state.temp_email = None
    state.venue_draft = None
    state.town_candidates.clear()
    state.venue_type_candidates.clear()

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
    state.venue_draft = None
    state.town_candidates.clear()
    state.venue_type_candidates.clear()

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


async def start_create_venue_flow(
    *,
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    tournament_id: int | None = None,
) -> None:
    try:
        storage = get_storage(context)
        persisted = storage.get_user_state(user_id)

        if persisted.role != "representative":
            await context.bot.send_message(
                chat_id=chat_id,
                text="Создание площадки доступно для роли «представитель». Сначала выберите роль.",
                reply_markup=hide_main_keyboard(),
            )
            return

        if not persisted.rating_token:
            await context.bot.send_message(
                chat_id=chat_id,
                text="Сначала авторизуйтесь через /login, чтобы создавать площадки.",
                reply_markup=hide_main_keyboard(),
            )
            return

        state = get_runtime_state(context, user_id)
        state.pending_action = PENDING_VENUE_TOWN
        state.venue_draft = VenueDraft(tournament_id=tournament_id)
        state.town_candidates.clear()
        state.venue_type_candidates.clear()

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "Создание площадки.\n"
                "Шаг 1/5: введите город (например, Москва)."
            ),
            reply_markup=hide_main_keyboard(),
        )
    except Exception as exc:
        logging.exception("Failed to start venue flow for user=%s", user_id)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Не удалось запустить создание площадки: {exc}",
            reply_markup=hide_main_keyboard(),
        )


async def create_venue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_chat is None or update.effective_user is None:
        return

    await start_create_venue_flow(
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
            "Готово. Выберите турниры кнопками «Добавить в голосование».\n"
            "Для площадки используйте «Создать площадку» на карточке турнира.\n"
            "Когда будете готовы, нажмите «Создать опрос»."
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
    state = get_runtime_state(context, user_id)

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
        tournament = state.tournaments.get(tournament_id)

        if tournament is None:
            await query.answer("Сначала загрузите список турниров по дате.", show_alert=True)
            return

        if tournament_id in state.selected_tournament_ids:
            state.selected_tournament_ids.remove(tournament_id)
            selected_now = False
        else:
            state.selected_tournament_ids.add(tournament_id)
            selected_now = True

        await query.edit_message_reply_markup(
            reply_markup=tournament_markup(tournament_id, selected_now)
        )

        await query.answer(
            f"Выбрано турниров: {len(state.selected_tournament_ids)}",
            show_alert=False,
        )
        return

    if data.startswith(VENUE_CALLBACK_START_PREFIX):
        if update.effective_chat is None:
            await query.answer()
            return

        raw_id = data.removeprefix(VENUE_CALLBACK_START_PREFIX)
        tournament_id = int(raw_id) if raw_id.isdigit() else None
        await query.answer()
        await start_create_venue_flow(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
            tournament_id=tournament_id,
        )
        return

    if data == VENUE_CALLBACK_TOWN_RETRY:
        state.pending_action = PENDING_VENUE_TOWN
        state.town_candidates.clear()
        await query.answer()
        await query.edit_message_text("Введите город заново (например, Москва).")
        return

    if data.startswith(VENUE_CALLBACK_TOWN_PREFIX):
        if update.effective_chat is None:
            await query.answer()
            return

        raw_id = data.removeprefix(VENUE_CALLBACK_TOWN_PREFIX)
        if not raw_id.isdigit():
            await query.answer("Некорректный город.", show_alert=True)
            return

        if state.pending_action != PENDING_VENUE_TOWN or state.venue_draft is None:
            await query.answer("Сначала начните создание площадки.", show_alert=True)
            return

        town_id = int(raw_id)
        town = state.town_candidates.get(town_id)
        if town is None:
            await query.answer("Город не найден в текущем списке.", show_alert=True)
            return

        state.venue_draft.town_id = town.id
        state.venue_draft.town_name = town.name
        state.pending_action = PENDING_VENUE_NAME
        await query.answer(f"Город: {town.name}")
        await query.edit_message_text(f"Город выбран: {format_town_label(town)}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Шаг 2/5: введите название площадки.",
            reply_markup=hide_main_keyboard(),
        )
        return

    if data.startswith(VENUE_CALLBACK_TYPE_PREFIX):
        if update.effective_chat is None:
            await query.answer()
            return

        raw_id = data.removeprefix(VENUE_CALLBACK_TYPE_PREFIX)
        if not raw_id.isdigit():
            await query.answer("Некорректный тип площадки.", show_alert=True)
            return

        if state.pending_action != PENDING_VENUE_TYPE or state.venue_draft is None:
            await query.answer("Сначала введите название площадки.", show_alert=True)
            return

        venue_type_id = int(raw_id)
        venue_type = state.venue_type_candidates.get(venue_type_id)
        if venue_type is None:
            await query.answer("Тип не найден в текущем списке.", show_alert=True)
            return

        state.venue_draft.venue_type_id = venue_type.id
        state.venue_draft.venue_type_name = venue_type.name
        state.pending_action = PENDING_VENUE_ADDRESS
        await query.answer(f"Тип: {venue_type.name}")
        await query.edit_message_text(f"Тип площадки: {venue_type.name}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="Шаг 4/5: введите адрес площадки или «-», чтобы пропустить.",
            reply_markup=hide_main_keyboard(),
        )
        return

    if data == VENUE_CALLBACK_CANCEL:
        reset_venue_draft(state)
        await query.answer("Отменено")
        await query.edit_message_text("Создание площадки отменено.")
        return

    if data == VENUE_CALLBACK_SUBMIT:
        if update.effective_chat is None:
            await query.answer()
            return

        draft = state.venue_draft
        if (
            draft is None
            or draft.town_id is None
            or draft.town_name is None
            or not draft.venue_name
            or draft.venue_type_id is None
            or draft.venue_type_name is None
        ):
            await query.answer("Данные площадки не заполнены.", show_alert=True)
            return

        storage = get_storage(context)
        persisted = storage.get_user_state(user_id)
        if not persisted.rating_token:
            await query.answer("Сначала авторизуйтесь через /login.", show_alert=True)
            return

        api = get_rating_api(context)
        await query.answer()
        try:
            created = await api.create_venue(
                name=draft.venue_name,
                town_id=draft.town_id,
                town_name=draft.town_name,
                venue_type_id=draft.venue_type_id,
                venue_type_name=draft.venue_type_name,
                address=draft.address,
                urls=draft.urls,
                bearer_token=persisted.rating_token,
            )
        except RatingApiError as exc:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"Не удалось создать площадку: {exc}",
                reply_markup=hide_main_keyboard(),
            )
            return

        venue_id = created.get("id") if isinstance(created, dict) else None
        venue_name = created.get("name") if isinstance(created, dict) else None
        venue_name_label = venue_name if isinstance(venue_name, str) and venue_name else draft.venue_name

        reset_venue_draft(state)

        try:
            await query.edit_message_text("Площадка успешно создана в rating API.")
        except Exception:
            pass

        suffix = f" (ID {venue_id})" if isinstance(venue_id, int) else ""
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"Создана площадка: {venue_name_label}{suffix}",
            reply_markup=hide_main_keyboard(),
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
    if text_normalized == MENU_CREATE_VENUE:
        if update.effective_chat is None:
            return
        await start_create_venue_flow(
            context=context,
            chat_id=update.effective_chat.id,
            user_id=user_id,
            tournament_id=None,
        )
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

    if state.pending_action == PENDING_VENUE_TOWN:
        if len(text) < 2:
            await update.message.reply_text("Введите хотя бы 2 символа названия города.")
            return

        api = get_rating_api(context)
        try:
            towns = await api.search_towns(text, page_size=10)
        except RatingApiError as exc:
            await update.message.reply_text(f"Не удалось найти города: {exc}")
            return

        if not towns:
            await update.message.reply_text(
                "Города не найдены. Уточните запрос и отправьте снова."
            )
            return

        state.town_candidates = {town.id: town for town in towns}
        await update.message.reply_text(
            "Шаг 1/5: выберите город из списка:",
            reply_markup=build_town_markup(towns),
        )
        return

    if state.pending_action == PENDING_VENUE_NAME:
        if state.venue_draft is None or state.venue_draft.town_id is None:
            await update.message.reply_text("Сначала выберите город.")
            state.pending_action = PENDING_VENUE_TOWN
            return

        venue_name = text.strip()
        if len(venue_name) < 3:
            await update.message.reply_text("Название площадки должно быть не короче 3 символов.")
            return

        state.venue_draft.venue_name = venue_name

        api = get_rating_api(context)
        try:
            venue_types = await api.get_venue_types()
        except RatingApiError as exc:
            await update.message.reply_text(f"Не удалось получить типы площадок: {exc}")
            return

        state.pending_action = PENDING_VENUE_TYPE
        state.venue_type_candidates = {item.id: item for item in venue_types}
        await update.message.reply_text(
            "Шаг 3/5: выберите тип площадки:",
            reply_markup=build_venue_types_markup(venue_types),
        )
        return

    if state.pending_action == PENDING_VENUE_ADDRESS:
        if state.venue_draft is None:
            await update.message.reply_text("Сначала начните создание площадки заново через /venue.")
            return

        normalized = text.casefold()
        state.venue_draft.address = None if normalized in SKIP_MARKERS else text
        state.pending_action = PENDING_VENUE_URLS
        await update.message.reply_text(
            "Шаг 5/5: введите ссылки площадки через запятую или «-», чтобы пропустить."
        )
        return

    if state.pending_action == PENDING_VENUE_URLS:
        if state.venue_draft is None:
            await update.message.reply_text("Сначала начните создание площадки заново через /venue.")
            return

        normalized = text.casefold()
        if normalized in SKIP_MARKERS:
            urls: list[str] = []
        else:
            chunks = [item.strip() for item in text.replace("\n", ",").split(",")]
            urls = [item for item in chunks if item]
            invalid = [url for url in urls if not (url.startswith("http://") or url.startswith("https://"))]
            if invalid:
                await update.message.reply_text(
                    "Ссылки должны начинаться с http:// или https://. "
                    "Исправьте и отправьте снова, либо введите «-»."
                )
                return

        state.venue_draft.urls = urls
        state.pending_action = None

        await update.message.reply_text(
            format_venue_draft_summary(state.venue_draft),
            reply_markup=build_venue_submit_markup(),
        )
        return

    await update.message.reply_text(
        "Не понял команду. Используйте /start или команды /login, /role, /date, /venue, /poll.",
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
    application.add_handler(CommandHandler("venue", create_venue_command))
    application.add_handler(CommandHandler("create_venue", create_venue_command))
    application.add_handler(CommandHandler("poll", poll_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
