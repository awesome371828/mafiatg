from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, User,
)

APP_NAME = "Mafia Keep"
ADMIN_ID = 6652898792
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DATA_PATH = Path(os.getenv("DATA_PATH", "data.json"))
CONTAINER_SECONDS = int(os.getenv("CONTAINER_SECONDS", "17400"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(APP_NAME)
router = Router()
dp = Dispatcher()
dp.include_router(router)
bot: Bot | None = None
shutdown_event = asyncio.Event()
games: dict[int, dict[str, Any]] = {}
player_to_chat: dict[int, int] = {}
online_since = time.monotonic()
data_lock = asyncio.Lock()

DEFAULT_DATA: dict[str, Any] = {"users": {}, "bans": {}, "totals": {"games": 0, "wins": {}}}


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return json.loads(json.dumps(DEFAULT_DATA))
    try:
        raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("data.json must contain a JSON object")
        raw.setdefault("users", {})
        raw.setdefault("bans", {})
        raw.setdefault("totals", {"games": 0, "wins": {}})
        raw["totals"].setdefault("games", 0)
        raw["totals"].setdefault("wins", {})
        return raw
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        log.exception("Could not read data.json: %s", exc)
        raise


data = load_data()


async def save_data() -> None:
    async with data_lock:
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        temp = DATA_PATH.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(DATA_PATH)


def touch_user(user: User) -> dict[str, Any]:
    key = str(user.id)
    record = data["users"].setdefault(key, {
        "user_id": user.id, "username": user.username,
        "first_name": user.first_name, "games": 0, "wins": 0,
        "losses": 0, "banned": False, "last_seen": None,
    })
    record["username"] = user.username
    record["first_name"] = user.first_name
    record["last_seen"] = datetime.now(timezone.utc).isoformat()
    return record


def is_banned(user_id: int) -> bool:
    return str(user_id) in data["bans"] or bool(data["users"].get(str(user_id), {}).get("banned"))


def display_name(user_id: int) -> str:
    record = data["users"].get(str(user_id), {})
    username = record.get("username")
    return f"@{username}" if username else record.get("first_name") or str(user_id)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def player_keyboard(chat_id: int, action: str, candidates: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name[:60], callback_data=f"{action}:{chat_id}:{uid}")]
            for uid, name in candidates]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def lobby_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🟢 Записаться", callback_data=f"join:{chat_id}")],
        [InlineKeyboardButton(text="🔴 Покинуть игру", callback_data=f"leave:{chat_id}")],
        [InlineKeyboardButton(text="🎭 Начать игру", callback_data=f"begin:{chat_id}")],
    ])


def alive_players(game: dict[str, Any]) -> list[int]:
    return [uid for uid in game["players"] if game["players"][uid]["alive"]]


def role_counts(n: int) -> list[str]:
    mafia = max(1, n // 4)
    roles = ["Мафия"] * mafia
    if n >= 7:
        roles.append("Дон")
    if n >= 5:
        roles.append("Шериф")
    if n >= 6:
        roles.append("Доктор")
    if n >= 8:
        roles.append("Путана")
    roles.extend(["Мирный"] * (n - len(roles)))
    return roles[:n]


def side(role: str) -> str:
    return "mafia" if role in ("Мафия", "Дон") else "town"


def outcome(game: dict[str, Any]) -> str | None:
    alive = alive_players(game)
    mafia = sum(side(game["players"][uid]["role"]) == "mafia" for uid in alive)
    town = len(alive) - mafia
    if mafia == 0:
        return "town"
    if mafia >= town:
        return "mafia"
    return None


async def safe_send(chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)  # type: ignore[union-attr]
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("Could not send message to %s: %s", chat_id, exc)


async def finish_game(chat_id: int, winner: str) -> None:
    game = games.get(chat_id)
    if not game:
        return
    label = "🏆 Победили мирные жители!" if winner == "town" else "🩸 Победила мафия!"
    await safe_send(chat_id, f"{label}\nИгра завершена. Создайте новое лобби командой /new_game.")
    for uid, player in game["players"].items():
        rec = data["users"].setdefault(str(uid), {"user_id": uid, "games": 0, "wins": 0, "losses": 0})
        rec["games"] = int(rec.get("games", 0)) + 1
        won = side(player["role"]) == winner
        rec["wins" if won else "losses"] = int(rec.get("wins" if won else "losses", 0)) + 1
        player_to_chat.pop(uid, None)
    data["totals"]["games"] = int(data["totals"].get("games", 0)) + 1
    data["totals"]["wins"][winner] = int(data["totals"]["wins"].get(winner, 0)) + 1
    games.pop(chat_id, None)
    await save_data()


async def announce_state(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    lines = [f"🎭 <b>{APP_NAME} — игроки</b>"]
    for uid, p in game["players"].items():
        status = "💚" if p["alive"] else "☠️"
        lines.append(f"{status} {display_name(uid)}")
    await safe_send(chat_id, "\n".join(lines))


async def start_night(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    game["phase"] = "night"
    game["night_actions"] = {}
    game["votes"] = {}
    game["round"] += 1
    await safe_send(chat_id, f"🌙 <b>Ночь {game['round']}</b> наступила. Роли, способные действовать, выберите цель в личке с ботом.")
    alive = alive_players(game)
    targets = [(uid, display_name(uid)) for uid in alive]
    for uid in alive:
        role = game["players"][uid]["role"]
        action = {"Мафия": "kill", "Дон": "kill", "Шериф": "check",
                  "Доктор": "heal", "Путана": "visit"}.get(role)
        if action:
            try:
                await bot.send_message(uid, f"🌙 Ночь {game['round']}. Ваша роль: <b>{role}</b>. Выберите цель:",
                                       reply_markup=player_keyboard(chat_id, action, [(tid, name) for tid, name in targets if tid != uid]))  # type: ignore[union-attr]
            except (TelegramForbiddenError, TelegramBadRequest):
                await safe_send(chat_id, f"⚠️ {display_name(uid)} не открыл личку с ботом. Ночные действия недоступны, пока игрок не нажмёт /start в личном чате.")
    await asyncio.sleep(25)
    if chat_id not in games:
        return
    await resolve_night(chat_id)


async def resolve_night(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    actions = game.get("night_actions", {})
    kills = [target for actor, target in actions.items()
             if game["players"].get(actor, {}).get("alive")
             and game["players"][actor]["role"] in ("Мафия", "Дон")]
    heals = [target for actor, target in actions.items()
             if game["players"].get(actor, {}).get("alive")
             and game["players"][actor]["role"] == "Доктор"]
    visits = [target for actor, target in actions.items()
              if game["players"].get(actor, {}).get("alive")
              and game["players"][actor]["role"] == "Путана"]
    killed: int | None = None
    if kills:
        killed = max(set(kills), key=kills.count)
        if killed in heals or killed in visits:
            killed = None
    checks = [(actor, target) for actor, target in actions.items()
              if game["players"].get(actor, {}).get("role") == "Шериф"]
    for actor, target in checks:
        await safe_send(actor, f"🕵️ Проверка: {display_name(target)} — {'мафия' if side(game['players'][target]['role']) == 'mafia' else 'не мафия'}.")
    if killed is not None and game["players"][killed]["alive"]:
        game["players"][killed]["alive"] = False
        player_to_chat.pop(killed, None)
        await safe_send(chat_id, f"☠️ Ночью погиб {display_name(killed)}. Его роль: <b>{game['players'][killed]['role']}</b>.")
    else:
        await safe_send(chat_id, "🌅 Все пережили эту ночь.")
    result = outcome(game)
    if result:
        await finish_game(chat_id, result)
        return
    await start_day(chat_id)


async def start_day(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    game["phase"] = "day"
    game["votes"] = {}
    candidates = [(uid, display_name(uid)) for uid in alive_players(game)]
    await safe_send(chat_id, "☀️ <b>День!</b> Обсудите подозрения, затем проголосуйте. Голосование длится 30 секунд.",
                    reply_markup=player_keyboard(chat_id, "vote", candidates))
    await asyncio.sleep(30)
    if chat_id not in games:
        return
    await resolve_day(chat_id)


async def resolve_day(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    votes = game.get("votes", {})
    counts: dict[int, int] = {}
    for voter, target in votes.items():
        if voter in alive_players(game) and target in alive_players(game):
            counts[target] = counts.get(target, 0) + 1
    if counts:
        max_votes = max(counts.values())
        winners = [uid for uid, count in counts.items() if count == max_votes]
        if len(winners) == 1:
            victim = winners[0]
            game["players"][victim]["alive"] = False
            player_to_chat.pop(victim, None)
            await safe_send(chat_id, f"⚖️ Город изгнал {display_name(victim)}. Его роль: <b>{game['players'][victim]['role']}</b>.")
        else:
            await safe_send(chat_id, "⚖️ Ничья в голосовании. Никто не изгнан.")
    else:
        await safe_send(chat_id, "⚖️ Голосов не было. Никто не изгнан.")
    result = outcome(game)
    if result:
        await finish_game(chat_id, result)
    else:
        await start_night(chat_id)


async def begin_game(chat_id: int, starter_id: int) -> str:
    game = games.get(chat_id)
    if not game:
        return "Лобби не найдено. Создайте его командой /new_game."
    if game["phase"] != "lobby":
        return "Игра уже началась."
    if len(game["players"]) < 4:
        return "Для старта нужно минимум 4 игрока."
    if starter_id not in game["players"] and starter_id != ADMIN_ID:
        return "Начать игру может только участник лобби."
    ids = list(game["players"])
    roles = role_counts(len(ids))
    random.shuffle(roles)
    for uid, role in zip(ids, roles):
        game["players"][uid]["role"] = role
        game["players"][uid]["alive"] = True
        try:
            await bot.send_message(uid, f"🎭 Ваша роль в <b>{APP_NAME}</b>: <b>{role}</b>. " +
                                   ("Играйте за мафию и скрывайте свою команду." if side(role) == "mafia" else "Найдите мафию и выживите."))  # type: ignore[union-attr]
        except (TelegramForbiddenError, TelegramBadRequest):
            return f"Не могу написать игроку {display_name(uid)} в личку. Пусть он откроет бота и нажмёт /start, затем повторите /start_game."
    game["phase"] = "starting"
    await safe_send(chat_id, "🎬 Роли распределены! Начинаем с ночи.")
    asyncio.create_task(start_night(chat_id))
    return "Игра запущена."


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return
    touch_user(message.from_user)
    await save_data()
    if is_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы в Mafia Keep.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в чат", url=f"https://t.me/{(await bot.get_me()).username}?startgroup=true")],  # type: ignore[union-attr]
        [InlineKeyboardButton(text="📜 Правила", callback_data="rules")],
    ])
    await message.answer(
        "🕵️ <b>Добро пожаловать в Mafia Keep!</b>\n\n"
        "Организуйте игру в групповом чате, собирайте игроков, получайте роли и выясняйте, кто скрывается за маской мафии.\n\n"
        "1. Добавьте бота в группу.\n2. В группе отправьте /new_game.\n3. Участники нажимают «Записаться».\n4. От 4 игроков используйте /start_game.\n\n"
        "Важно: каждый участник должен открыть бота в личке и нажать /start, чтобы получать роль и ночные действия.",
        reply_markup=kb,
    )


@router.callback_query(F.data == "rules")
async def rules_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer("📜 <b>Правила Mafia Keep</b>\nМафия побеждает, когда мафии не меньше мирных. Мирные побеждают, когда вся мафия выбывает. Не раскрывайте свою роль без необходимости. Ночные действия доступны в личке.")


@router.message(Command("new_game"))
async def new_game(message: Message) -> None:
    if not message.from_user or message.chat.type == "private":
        await message.answer("Создать лобби можно в групповом чате.")
        return
    if is_banned(message.from_user.id):
        return
    if message.chat.id in games:
        await message.answer("В этом чате уже есть игра. Используйте /game_status или завершите текущую игру.")
        return
    touch_user(message.from_user)
    games[message.chat.id] = {"phase": "lobby", "host": message.from_user.id,
                              "players": {}, "round": 0, "votes": {}, "night_actions": {}}
    await message.answer("🎭 <b>Mafia Keep — новое лобби</b>\nНажмите кнопку ниже, чтобы войти. Для старта нужно минимум 4 игрока.",
                         reply_markup=lobby_keyboard(message.chat.id))


@router.message(Command("start_game"))
async def start_game_command(message: Message) -> None:
    if not message.from_user:
        return
    result = await begin_game(message.chat.id, message.from_user.id)
    await message.answer(result)


@router.callback_query(F.data.startswith("join:"))
async def join_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data or not callback.message:
        return
    touch_user(callback.from_user)
    if is_banned(callback.from_user.id):
        await callback.answer("Вы заблокированы.", show_alert=True)
        return
    chat_id = int(callback.data.split(":")[1])
    game = games.get(chat_id)
    if not game or game["phase"] != "lobby":
        await callback.answer("Регистрация уже закрыта.", show_alert=True)
        return
    if callback.from_user.id in game["players"]:
        await callback.answer("Вы уже в лобби.")
        return
    if callback.from_user.id in player_to_chat:
        await callback.answer("Вы уже участвуете в другой игре.", show_alert=True)
        return
    game["players"][callback.from_user.id] = {"role": "Не назначена", "alive": True}
    player_to_chat[callback.from_user.id] = chat_id
    await save_data()
    await callback.answer("Вы записаны!")
    await announce_state(chat_id)


@router.callback_query(F.data.startswith("leave:"))
async def leave_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    chat_id = int(callback.data.split(":")[1])
    game = games.get(chat_id)
    if game and game["phase"] == "lobby" and callback.from_user.id in game["players"]:
        game["players"].pop(callback.from_user.id, None)
        player_to_chat.pop(callback.from_user.id, None)
        await callback.answer("Вы вышли из лобби.")
        await announce_state(chat_id)
    else:
        await callback.answer("Выйти сейчас нельзя.", show_alert=True)


@router.callback_query(F.data.startswith("begin:"))
async def begin_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    chat_id = int(callback.data.split(":")[1])
    await callback.answer(await begin_game(chat_id, callback.from_user.id), show_alert=True)


@router.callback_query(F.data.startswith(("vote:", "kill:", "check:", "heal:", "visit:")))
async def action_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    action, chat_id_text, target_text = callback.data.split(":")
    chat_id, target = int(chat_id_text), int(target_text)
    game = games.get(chat_id)
    uid = callback.from_user.id
    if not game or uid not in game["players"] or not game["players"][uid]["alive"]:
        await callback.answer("Вы не участвуете в активной игре.", show_alert=True)
        return
    if target not in game["players"] or not game["players"][target]["alive"] or target == uid:
        await callback.answer("Эта цель недоступна.", show_alert=True)
        return
    if action == "vote":
        if game["phase"] != "day":
            await callback.answer("Голосовать можно только днём.", show_alert=True)
            return
        game["votes"][uid] = target
        await callback.answer(f"Ваш голос: {display_name(target)}")
    else:
        if game["phase"] != "night":
            await callback.answer("Ночные действия доступны только ночью.", show_alert=True)
            return
        allowed = {"kill": ("Мафия", "Дон"), "check": ("Шериф",),
                   "heal": ("Доктор",), "visit": ("Путана",)}
        if game["players"][uid]["role"] not in allowed[action]:
            await callback.answer("Ваша роль не может выполнять это действие.", show_alert=True)
            return
        game["night_actions"][uid] = target
        await callback.answer("Действие принято.")


@router.message(Command("ban_player"))
async def ban_player(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban_player USER_ID [причина]")
        return
    uid = int(parts[1])
    reason = parts[2] if len(parts) > 2 else "нарушение правил"
    data["bans"][str(uid)] = {"reason": reason, "by": message.from_user.id,
                              "at": datetime.now(timezone.utc).isoformat()}
    if str(uid) in data["users"]:
        data["users"][str(uid)]["banned"] = True
    for chat_id, game in list(games.items()):
        if uid in game["players"]:
            game["players"].pop(uid, None)
            player_to_chat.pop(uid, None)
            await safe_send(chat_id, f"⛔ Игрок {display_name(uid)} заблокирован администратором.")
    await save_data()
    await message.answer(f"⛔ Игрок {uid} заблокирован. Причина: {reason}")


@router.message(Command("unban_player"))
async def unban_player(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban_player USER_ID")
        return
    uid = int(parts[1])
    data["bans"].pop(str(uid), None)
    if str(uid) in data["users"]:
        data["users"][str(uid)]["banned"] = False
    await save_data()
    await message.answer(f"✅ Блокировка игрока {uid} снята.")


@router.message(Command("game_status"))
async def game_status(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type == "private":
        if len(games) == 0:
            await message.answer("Активных игр нет.")
        else:
            await message.answer("Активные игры:\n" + "\n".join(
                f"Чат {cid}: фаза={g['phase']}, игроков={len(g['players'])}"
                for cid, g in games.items()))
        return
    game = games.pop(message.chat.id, None)
    if game:
        for uid in game["players"]:
            player_to_chat.pop(uid, None)
        await message.answer("♻️ Игра в этом чате сброшена.")
    else:
        await message.answer("В этом чате нет активной игры.")


@router.message(Command("stat"))
async def stats(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    users = len(data["users"])
    banned = len(data["bans"])
    await message.answer(f"📊 <b>{APP_NAME}</b>\nИгроков в базе: {users}\nАктивных игр: {len(games)}\nБлокировок: {banned}\nВсего завершённых игр: {data['totals'].get('games', 0)}\nВремя процесса: {int(time.monotonic() - online_since)} сек.")


@router.message(Command("profile"))
async def profile(message: Message) -> None:
    if not message.from_user:
        return
    record = touch_user(message.from_user)
    await save_data()
    if is_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы в Mafia Keep.")
        return
    await message.answer(
        f"👤 <b>Профиль {message.from_user.full_name}</b>\n"
        f"🎮 Игр: {record.get('games', 0)}\n🏆 Побед: {record.get('wins', 0)}\n"
        f"💔 Поражений: {record.get('losses', 0)}\n"
        f"📈 Винрейт: {round(record.get('wins', 0) * 100 / max(1, record.get('games', 0)))}%"
    )


@router.message()
async def remember_user(message: Message) -> None:
    if not message.from_user:
        return
    touch_user(message.from_user)
    await save_data()
    if is_banned(message.from_user.id):
        return


async def timed_shutdown() -> None:
    await asyncio.sleep(max(60, CONTAINER_SECONDS))
    log.warning("Container time limit reached; gracefully stopping bot.")
    try:
        await bot.send_message(ADMIN_ID, "♻️ Mafia Keep: контейнер обновляется. Статистика и баны будут сохранены.")  # type: ignore[union-attr]
    except Exception:
        log.exception("Could not notify admin about shutdown")
    await save_data()
    shutdown_event.set()
    await dp.stop_polling()


async def run() -> None:
    global bot
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing. Add it in repository Settings > Secrets and variables > Actions.")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    me = await bot.get_me()
    log.info("%s starting as @%s; owner=%s", APP_NAME, me.username, ADMIN_ID)
    shutdown_task = asyncio.create_task(timed_shutdown(), name="container-shutdown-timer")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    finally:
        shutdown_task.cancel()
        await save_data()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped by keyboard interrupt")
