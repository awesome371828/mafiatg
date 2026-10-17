from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

APP_NAME = "Mafia Keep"
ADMIN_ID = 6652898792
TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
DATA_PATH = Path(os.getenv("DATA_PATH", "data.json"))
CONTAINER_SECONDS = max(60, int(os.getenv("CONTAINER_SECONDS", "18000")))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(APP_NAME)
router = Router()
dp = Dispatcher()
dp.include_router(router)
bot: Bot | None = None
bot_username = "MafiaKeepBot"
games: dict[int, dict[str, Any]] = {}
player_to_chat: dict[int, int] = {}
online_since = time.monotonic()
data_lock = asyncio.Lock()

ROLE_DESCRIPTIONS = {
    "Мирный": "Днём обсуждай подозреваемых и голосуй за исключение мафии.",
    "Мафия": "Ночью выбирай жертву вместе с мафией. Не выдай себя днём.",
    "Дон": "Глава мафии. Участвует в выборе жертвы и считается мафией при проверке.",
    "Комиссар": "Каждую ночь проверяй одного игрока и узнавай, относится ли он к мафии.",
    "Сержант": "Помощник комиссара. Если комиссар погиб, получает возможность проводить проверки.",
    "Доктор": "Ночью выбери игрока, которого хочешь спасти от убийства.",
    "Маньяк": "Ночью выбирай отдельную жертву. Побеждает, когда остаётся один.",
    "Любовница": "Ночью блокируй действие выбранного игрока.",
    "Адвокат": "Выбери подзащитного: проверка комиссара сочтёт его мирным.",
    "Самоубийца": "Твоя особая цель — быть изгнанным дневным голосованием.",
    "Бомж": "Ночью наблюдай за игроком и узнавай, кто к нему приходил.",
    "Счастливчик": "Один раз можешь пережить ночное убийство благодаря удаче.",
    "Камикадзе": "Если город изгонит тебя днём, случайный живой игрок погибнет вместе с тобой.",
    "Журналист": "Ночью проверь игрока и узнай его сторону, но не точную роль.",
    "Телохранитель": "Ночью прикрывай игрока и погибни вместо него при нападении.",
    "Мэр": "Твой голос на дневном голосовании считается за два.",
}
ROLE_KEYS = {
    "Мафия": "mafia", "Дон": "don", "Комиссар": "commissioner", "Сержант": "sergeant",
    "Доктор": "doctor", "Маньяк": "maniac", "Любовница": "mistress", "Адвокат": "lawyer",
    "Самоубийца": "suicide", "Бомж": "bum", "Счастливчик": "lucky",
    "Камикадзе": "kamikaze", "Журналист": "journalist", "Телохранитель": "bodyguard", "Мэр": "mayor",
}
DEFAULT_SETTINGS: dict[str, Any] = {
    "min_players": 2, "max_players": 20, "night_seconds": 25, "day_seconds": 30,
    "registration_seconds": 300, "show_roles": True, "test_mode": True,
    "roles_enabled": {key: True for key in ROLE_KEYS.values()},
}
DEFAULT_DATA: dict[str, Any] = {
    "users": {}, "bans": {}, "totals": {"games": 0, "wins": {}},
    "settings": DEFAULT_SETTINGS, "active_games": {},
}


def load_data() -> dict[str, Any]:
    if not DATA_PATH.exists():
        return json.loads(json.dumps(DEFAULT_DATA))
    raw = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("data.json must contain an object")
    for key, value in DEFAULT_DATA.items():
        raw.setdefault(key, json.loads(json.dumps(value)))
    raw["totals"].setdefault("games", 0)
    raw["totals"].setdefault("wins", {})
    for key, value in DEFAULT_SETTINGS.items():
        raw["settings"].setdefault(key, json.loads(json.dumps(value)))
    raw["settings"].setdefault("roles_enabled", dict(DEFAULT_SETTINGS["roles_enabled"]))
    for key, value in ROLE_KEYS.items():
        raw["settings"]["roles_enabled"].setdefault(value, True)
    return raw


data = load_data()

# Restore the persisted state of each active game before polling starts.
for _chat_text, _saved_game in data.get("active_games", {}).items():
    try:
        _chat_id = int(_chat_text)
        _saved_game["players"] = {int(uid): player for uid, player in _saved_game.get("players", {}).items()}
        _saved_game["votes"] = {int(uid): int(target) for uid, target in _saved_game.get("votes", {}).items()}
        _saved_game["night_actions"] = {int(uid): int(target) for uid, target in _saved_game.get("night_actions", {}).items()}
        games[_chat_id] = _saved_game
        for _uid, _player in _saved_game["players"].items():
            if _saved_game.get("phase") == "lobby" or _player.get("alive", False):
                player_to_chat[_uid] = _chat_id
    except (TypeError, ValueError, KeyError) as _exc:
        log.warning("Skipping invalid saved game %s: %s", _chat_text, _exc)


async def save_data() -> None:
    async with data_lock:
        data["active_games"] = {str(chat_id): game for chat_id, game in games.items()}
        DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DATA_PATH)


def settings() -> dict[str, Any]:
    return data["settings"]


def touch_user(user: User) -> dict[str, Any]:
    key = str(user.id)
    record = data["users"].setdefault(key, {
        "user_id": user.id, "username": user.username, "first_name": user.first_name,
        "games": 0, "wins": 0, "losses": 0, "banned": False, "last_seen": None,
    })
    record.update(username=user.username, first_name=user.first_name,
                  last_seen=datetime.now(timezone.utc).isoformat())
    return record


def is_banned(uid: int) -> bool:
    return str(uid) in data["bans"] or bool(data["users"].get(str(uid), {}).get("banned"))


def display_name(uid: int) -> str:
    rec = data["users"].get(str(uid), {})
    username = rec.get("username")
    name = "@" + username if username else rec.get("first_name") or f"Игрок {uid}"
    return escape(str(name))


def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID


def kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="➕ Добавить в чат", url=f"https://t.me/{bot_username}?startgroup=true")]]
    if bot_username:
        rows.append([InlineKeyboardButton(text="🎭 Создать игру в группе", url=f"https://t.me/{bot_username}?startgroup=true")])
    if bot:
        # The owner-only button is built only for the owner in /start.
        pass
    return kb(rows)


def lobby_text(chat_id: int) -> str:
    game = games[chat_id]
    players = game["players"]
    roster = "\n".join(f"• {display_name(uid)}" for uid in players) or "Пока пусто"
    minimum = int(settings()["min_players"])
    test = " · тестовый режим" if settings()["test_mode"] and minimum == 2 else ""
    return (f"🎭 <b>MAFIA KEEP</b> · Регистрация открыта\n"
            f"👥 <b>{len(players)}/{int(settings()['max_players'])}</b> · старт от {minimum}{test}\n\n"
            f"{roster}\n\nЖми кнопку, чтобы войти.")


def lobby_markup(chat_id: int) -> InlineKeyboardMarkup:
    count = len(games[chat_id]["players"])
    return kb([
        [InlineKeyboardButton(text=f"🎭 Записаться · {count}", callback_data=f"join:{chat_id}")],
        [InlineKeyboardButton(text="✖ Выйти", callback_data=f"leave:{chat_id}"),
         InlineKeyboardButton(text="▶ Начать", callback_data=f"begin:{chat_id}")],
    ])


def player_keyboard(chat_id: int, action: str, candidates: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=name[:60], callback_data=f"{action}:{chat_id}:{uid}")]
            for uid, name in candidates]
    return kb(rows or [[InlineKeyboardButton(text="Нет доступных целей", callback_data="noop")]])


def alive_players(game: dict[str, Any]) -> list[int]:
    return [uid for uid, p in game["players"].items() if p.get("alive")]


def choose_mafia_target(votes: list[int]) -> int | None:
    """Return a target only when one candidate has the unique highest vote count."""
    counts: dict[int, int] = {}
    for target in votes:
        counts[target] = counts.get(target, 0) + 1
    if not counts:
        return None
    highest = max(counts.values())
    leaders = [target for target, count in counts.items() if count == highest]
    return leaders[0] if len(leaders) == 1 else None


def choose_day_victim(game: dict[str, Any]) -> int | None:
    """Resolve valid living votes; a tie or no votes means nobody is expelled."""
    alive = set(alive_players(game))
    counts: dict[int, int] = {}
    for voter, target in game.get("votes", {}).items():
        if voter in alive and target in alive:
            weight = 2 if game["players"][voter]["role"] == "Мэр" else 1
            counts[target] = counts.get(target, 0) + weight
    if not counts:
        return None
    highest = max(counts.values())
    leaders = [target for target, amount in counts.items() if amount == highest]
    return leaders[0] if len(leaders) == 1 else None


def side(role: str) -> str:
    if role in ("Мафия", "Дон"):
        return "mafia"
    if role == "Маньяк":
        return "maniac"
    return "town"


def outcome(game: dict[str, Any]) -> str | None:
    alive = alive_players(game)
    if not alive:
        return "town"
    mafia = sum(side(game["players"][uid]["role"]) == "mafia" for uid in alive)
    maniac = sum(side(game["players"][uid]["role"]) == "maniac" for uid in alive)
    town = len(alive) - mafia - maniac
    if game.get("test_mode_game") and len(game["players"]) == 2 and len(alive) == 2:
        return None
    if mafia == 0 and maniac == 0:
        return "town"
    if maniac and maniac >= mafia + town:
        return "maniac"
    if mafia and mafia >= town + maniac:
        return "mafia"
    return None


def build_roles(n: int) -> list[str]:
    enabled = settings()["roles_enabled"]
    if n == 2 and settings()["test_mode"]:
        return ["Мафия", "Комиссар"]
    roles: list[str] = ["Мафия"] * max(1, n // 4)
    candidates = [
        (8, "Дон"), (5, "Комиссар"), (9, "Сержант"), (5, "Доктор"),
        (9, "Маньяк"), (8, "Любовница"), (10, "Адвокат"), (9, "Самоубийца"),
        (8, "Бомж"), (6, "Счастливчик"), (10, "Камикадзе"), (8, "Журналист"),
        (7, "Телохранитель"), (7, "Мэр"),
    ]
    for min_n, role in candidates:
        if n >= min_n and enabled.get(ROLE_KEYS.get(role, ""), True) and len(roles) < n:
            roles.append(role)
    roles.extend(["Мирный"] * max(0, n - len(roles)))
    return roles[:n]


async def safe_send(chat_id: int, text: str, **kwargs: Any) -> None:
    try:
        await bot.send_message(chat_id, text, **kwargs)  # type: ignore[union-attr]
    except (TelegramForbiddenError, TelegramBadRequest) as exc:
        log.info("Cannot send message to %s: %s", chat_id, exc)


async def refresh_lobby(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game or game["phase"] != "lobby" or not game.get("lobby_message_id"):
        return
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=game["lobby_message_id"],
                                    text=lobby_text(chat_id), reply_markup=lobby_markup(chat_id))  # type: ignore[union-attr]
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.info("Lobby edit failed: %s", exc)


async def finish_game(chat_id: int, winner: str, reason: str = "") -> None:
    game = games.get(chat_id)
    if not game:
        return
    names = {"town": "🏆 Победил город", "mafia": "🩸 Победила мафия", "maniac": "🔪 Победил маньяк"}
    roles = "\n".join(f"• {display_name(uid)} — {escape(p['role'])}{'' if p.get('alive') else ' · выбыл'}"
                       for uid, p in game["players"].items())
    await safe_send(chat_id, f"{names.get(winner, 'Игра завершена')}!\n\n<b>Роли:</b>\n{roles}" +
                    (f"\n\n{escape(reason)}" if reason else "") +
                    "\n\nНовая партия: /game")
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


def role_action(role: str, game: dict[str, Any]) -> str | None:
    actions = {
        "Мафия": "kill", "Дон": "kill", "Комиссар": "check", "Доктор": "heal",
        "Маньяк": "maniac", "Любовница": "visit", "Адвокат": "lawyer",
        "Бомж": "observe", "Журналист": "journalist", "Телохранитель": "guard",
    }
    if role == "Сержант":
        if not any(p["alive"] and p["role"] == "Комиссар" for p in game["players"].values()):
            return "check"
    return actions.get(role)


async def start_night(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    game["phase"] = "night"
    game["night_actions"] = {}
    game["votes"] = {}
    game["round"] += 1
    game["phase_deadline"] = time.time() + int(settings()["night_seconds"])
    await save_data()
    await safe_send(chat_id, f"🌙 <b>Ночь {game['round']}</b> · город засыпает.")
    alive = alive_players(game)
    targets = [(uid, display_name(uid)) for uid in alive]
    for uid in alive:
        role = game["players"][uid]["role"]
        action = role_action(role, game)
        if not action:
            continue
        choices = [(tid, name) for tid, name in targets if tid != uid]
        if action == "kill":
            choices = [(tid, name) for tid, name in choices if side(game["players"][tid]["role"]) != "mafia"]
        try:
            await bot.send_message(uid, f"🌙 Ночь {game['round']} · <b>{escape(role)}</b>\n{escape(ROLE_DESCRIPTIONS.get(role, 'Выберите действие.'))}",
                                   reply_markup=player_keyboard(chat_id, action, choices))  # type: ignore[union-attr]
        except (TelegramForbiddenError, TelegramBadRequest):
            await safe_send(chat_id, f"⚠️ {display_name(uid)}: открой личку с ботом и нажми /start.")
    await asyncio.sleep(max(1, float(game.get("phase_deadline") or time.time()) - time.time()))
    if chat_id in games and games[chat_id].get("phase") == "night":
        await resolve_night(chat_id)


async def resolve_night(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    actions = game.get("night_actions", {})
    players = game["players"]
    blocked = {target for actor, target in actions.items()
               if players.get(actor, {}).get("alive") and players[actor]["role"] == "Любовница"}
    kills = [target for actor, target in actions.items()
             if players.get(actor, {}).get("alive") and actor not in blocked
             and players[actor]["role"] in ("Мафия", "Дон")]
    maniac_kills = [target for actor, target in actions.items()
                    if players.get(actor, {}).get("alive") and actor not in blocked
                    and players[actor]["role"] == "Маньяк"]
    heals = {target for actor, target in actions.items()
             if players.get(actor, {}).get("alive") and players[actor]["role"] == "Доктор" and actor not in blocked}
    guards = {target: actor for actor, target in actions.items()
              if players.get(actor, {}).get("alive") and players[actor]["role"] == "Телохранитель" and actor not in blocked}
    lawyer_targets = {target for actor, target in actions.items()
                      if players.get(actor, {}).get("alive") and actor not in blocked
                      and players[actor]["role"] == "Адвокат"}
    targets_to_kill: list[int] = []
    mafia_target = choose_mafia_target(kills)
    if mafia_target is not None:
        targets_to_kill.append(mafia_target)
    targets_to_kill.extend(maniac_kills[:1])
    died: set[int] = set()
    for target in targets_to_kill:
        if target in heals:
            continue
        if target in guards:
            guard_id = guards[target]
            if players[guard_id]["alive"]:
                died.add(guard_id)
            continue
        if players.get(target, {}).get("alive"):
            if players[target]["role"] == "Счастливчик" and not players[target].get("lucky_used") and random.random() < 0.5:
                players[target]["lucky_used"] = True
                continue
            died.add(target)
    for actor, target in actions.items():
        if not players.get(actor, {}).get("alive") or actor in blocked or target not in players:
            continue
        role = players[actor]["role"]
        if role in ("Комиссар", "Сержант") and role_action(role, game) == "check":
            masked = target in lawyer_targets
            is_mafia = side(players[target]["role"]) == "mafia" and not masked
            await safe_send(actor, f"🕵️ Проверка: {display_name(target)} — {'мафия' if is_mafia else 'не мафия'}.")
        elif role == "Журналист":
            await safe_send(actor, f"📰 Проверка: {display_name(target)} — сторона «{ {'mafia':'мафия','town':'город','maniac':'одиночка'}[side(players[target]['role'])] }».")
        elif role == "Бомж":
            visitors = [display_name(a) for a, t in actions.items() if t == target and a != actor]
            await safe_send(actor, f"👣 К игроку {display_name(target)} приходили: " +
                            (", ".join(visitors) if visitors else "никто не приходил") + ".")
    for uid in died:
        if uid in players:
            players[uid]["alive"] = False
            player_to_chat.pop(uid, None)
    if died:
        lines = [f"☠️ Ночью выбыл: {display_name(uid)}" +
                 (f" · {escape(players[uid]['role'])}" if settings()["show_roles"] else "") for uid in died]
        await safe_send(chat_id, "\n".join(lines))
    else:
        await safe_send(chat_id, "🌅 Все пережили ночь.")
    result = outcome(game)
    if result:
        await finish_game(chat_id, result)
    else:
        await start_day(chat_id)


async def start_day(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    game["phase"] = "day"
    game["votes"] = {}
    game["phase_deadline"] = time.time() + int(settings()["day_seconds"])
    candidates = [(uid, display_name(uid)) for uid in alive_players(game)]
    await save_data()
    await safe_send(chat_id, f"☀️ <b>День {game['round']}</b> · обсудите подозрения и голосуйте.",
                    reply_markup=player_keyboard(chat_id, "vote", candidates))
    await asyncio.sleep(max(1, float(game.get("phase_deadline") or time.time()) - time.time()))
    if chat_id in games and games[chat_id].get("phase") == "day":
        await resolve_day(chat_id)


async def resolve_day(chat_id: int) -> None:
    game = games.get(chat_id)
    if not game:
        return
    victim = choose_day_victim(game)
    if victim is None:
        await safe_send(chat_id, "⚖️ Ничья или нет голосов. Никто не изгнан.")
    else:
        game["players"][victim]["alive"] = False
        player_to_chat.pop(victim, None)
        role = game["players"][victim]["role"]
        await safe_send(chat_id, f"⚖️ Город изгнал {display_name(victim)}" +
                        (f" · {escape(role)}" if settings()["show_roles"] else "") + ".")
        if role == "Самоубийца":
            await finish_game(chat_id, "town", "Самоубийца добился своей цели: его изгнали днём.")
            return
        if role == "Камикадзе":
            targets = [uid for uid in alive_players(game) if uid != victim]
            if targets:
                target = random.choice(targets)
                game["players"][target]["alive"] = False
                player_to_chat.pop(target, None)
                await safe_send(chat_id, f"💥 Последний ход камикадзе: вместе с ним погиб {display_name(target)}.")
    result = outcome(game)
    if result:
        await finish_game(chat_id, result)
    else:
        await start_night(chat_id)


async def begin_game(chat_id: int, starter_id: int) -> str:
    game = games.get(chat_id)
    if not game:
        return "Сначала создай лобби: /game"
    if game["phase"] != "lobby":
        return "Партия уже идёт."
    if len(game["players"]) < int(settings()["min_players"]):
        return f"Нужно минимум {settings()['min_players']} игрока."
    if starter_id not in game["players"] and not is_admin(starter_id) and starter_id != game["host"]:
        return "Начать может участник лобби или создатель."
    # Preflight every DM before assigning any roles. Avoid half-started games.
    failed: list[int] = []
    for uid in game["players"]:
        try:
            await bot.send_message(uid, "🎭 Mafia Keep: партия начинается. Сейчас придёт ваша роль.")  # type: ignore[union-attr]
        except (TelegramForbiddenError, TelegramBadRequest):
            failed.append(uid)
    if failed:
        names = ", ".join(display_name(uid) for uid in failed)
        return f"Не могу написать: {names}. Пусть каждый нажмёт /start в личке, затем повторите запуск."
    ids = list(game["players"])
    roles = build_roles(len(ids))
    random.shuffle(roles)
    game["test_mode_game"] = bool(settings()["test_mode"] and len(ids) == 2)
    for uid, role in zip(ids, roles):
        game["players"][uid].update(role=role, alive=True, lucky_used=False)
        try:
            await bot.send_message(uid, f"🎭 <b>Твоя роль: {escape(role)}</b>\n\n{escape(ROLE_DESCRIPTIONS.get(role, 'Найди свою цель.'))}")  # type: ignore[union-attr]
        except (TelegramForbiddenError, TelegramBadRequest):
            return f"Не удалось отправить роль игроку {display_name(uid)}. Проверьте личные сообщения."
    game["phase"] = "starting"
    await safe_send(chat_id, "🎭 Роли розданы. Город засыпает…")
    asyncio.create_task(start_night(chat_id))
    return "Партия началась."


def owner_keyboard() -> InlineKeyboardMarkup:
    return kb([
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton(text="🎮 Игры", callback_data="adm:games")],
        [InlineKeyboardButton(text="⚙️ Настройки игры", callback_data="adm:settings"),
         InlineKeyboardButton(text="🎭 Роли", callback_data="adm:roles")],
        [InlineKeyboardButton(text="🧹 Сбросить все игры", callback_data="adm:reset")],
        [InlineKeyboardButton(text="💾 Сохранить данные", callback_data="adm:save")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="adm:broadcast")],
        [InlineKeyboardButton(text="🚫 Управление банами", callback_data="adm:bans")],
    ])


def settings_keyboard() -> InlineKeyboardMarkup:
    s = settings()
    return kb([
        [InlineKeyboardButton(text=f"Мин. игроков: {s['min_players']}", callback_data="noop"),
         InlineKeyboardButton(text="−", callback_data="cfg:min:-1"), InlineKeyboardButton(text="+", callback_data="cfg:min:1")],
        [InlineKeyboardButton(text=f"Макс. игроков: {s['max_players']}", callback_data="noop"),
         InlineKeyboardButton(text="−", callback_data="cfg:max:-1"), InlineKeyboardButton(text="+", callback_data="cfg:max:1")],
        [InlineKeyboardButton(text=f"Ночь: {s['night_seconds']}с", callback_data="noop"),
         InlineKeyboardButton(text="−", callback_data="cfg:night:-5"), InlineKeyboardButton(text="+", callback_data="cfg:night:5")],
        [InlineKeyboardButton(text=f"День: {s['day_seconds']}с", callback_data="noop"),
         InlineKeyboardButton(text="−", callback_data="cfg:day:-5"), InlineKeyboardButton(text="+", callback_data="cfg:day:5")],
        [InlineKeyboardButton(text=f"Регистрация: {s['registration_seconds']}с", callback_data="noop"),
         InlineKeyboardButton(text="−", callback_data="cfg:reg:-30"), InlineKeyboardButton(text="+", callback_data="cfg:reg:30")],
        [InlineKeyboardButton(text=f"Тест на 2 игрока: {'ВКЛ' if s['test_mode'] else 'ВЫКЛ'}", callback_data="cfg:toggle:test_mode")],
        [InlineKeyboardButton(text=f"Показывать роль после смерти: {'ДА' if s['show_roles'] else 'НЕТ'}", callback_data="cfg:toggle:show_roles")],
        [InlineKeyboardButton(text="⬅️ В панель", callback_data="adm:home")],
    ])


def roles_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for role, key in ROLE_KEYS.items():
        rows.append([InlineKeyboardButton(text=f"{'🟢' if settings()['roles_enabled'].get(key, True) else '⚫'} {role}",
                                          callback_data=f"cfg:role:{key}")])
    rows.append([InlineKeyboardButton(text="⬅️ В панель", callback_data="adm:home")])
    return kb(rows)


@router.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return
    touch_user(message.from_user)
    await save_data()
    if is_banned(message.from_user.id):
        await message.answer("⛔ Доступ закрыт.")
        return
    global bot_username
    bot_username = (await bot.get_me()).username or bot_username  # type: ignore[union-attr]
    rows = [[InlineKeyboardButton(text="➕ Добавить в чат", url=f"https://t.me/{bot_username}?startgroup=true")]]
    if is_admin(message.from_user.id):
        rows.append([InlineKeyboardButton(text="🛡 Панель владельца", callback_data="adm:home")])
    await message.answer("🎭 <b>Mafia Keep</b>\nСоздай игру в группе командой /game.", reply_markup=kb(rows))


@router.message(Command("game", "new_game", "mafia"))
async def new_game(message: Message) -> None:
    if not message.from_user or message.chat.type == ChatType.PRIVATE:
        await message.answer("Запусти /game в группе.")
        return
    if is_banned(message.from_user.id):
        return
    if message.chat.id in games:
        await message.answer("Игра уже создана в этом чате.")
        return
    touch_user(message.from_user)
    games[message.chat.id] = {
        "phase": "lobby", "host": message.from_user.id, "players": {}, "round": 0,
        "votes": {}, "night_actions": {}, "lobby_message_id": None,
        "registration_deadline": time.time() + int(settings()["registration_seconds"]),
        "phase_deadline": None,
    }
    sent = await message.answer(lobby_text(message.chat.id), reply_markup=lobby_markup(message.chat.id))
    games[message.chat.id]["lobby_message_id"] = sent.message_id
    await save_data()
    # Registration closes automatically. Host can start earlier once the minimum is reached.
    asyncio.create_task(registration_timeout(message.chat.id, sent.message_id))


async def registration_timeout(chat_id: int, message_id: int) -> None:
    initial = games.get(chat_id)
    deadline = float(initial.get("registration_deadline") or time.time()) if initial else time.time()
    await asyncio.sleep(max(1, deadline - time.time()))
    game = games.get(chat_id)
    if game and game["phase"] == "lobby" and game.get("lobby_message_id") == message_id:
        await safe_send(chat_id, "⌛ Регистрация закрыта. Для новой партии напишите /game.")
        for uid in game["players"]:
            player_to_chat.pop(uid, None)
        games.pop(chat_id, None)


@router.message(Command("start_game"))
async def start_game_command(message: Message) -> None:
    if not message.from_user:
        return
    await message.answer(await begin_game(message.chat.id, message.from_user.id))


@router.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("join:"))
async def join_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    uid = callback.from_user.id
    touch_user(callback.from_user)
    if is_banned(uid):
        await callback.answer("Доступ закрыт.", show_alert=True)
        return
    chat_id = int(callback.data.split(":")[1])
    game = games.get(chat_id)
    if not game or game["phase"] != "lobby":
        await callback.answer("Регистрация закрыта.", show_alert=True)
        return
    if uid in game["players"]:
        await callback.answer("Ты уже в игре.")
        return
    if uid in player_to_chat:
        await callback.answer("Ты уже записан в другую игру.", show_alert=True)
        return
    if len(game["players"]) >= int(settings()["max_players"]):
        await callback.answer("Мест больше нет.", show_alert=True)
        return
    game["players"][uid] = {"role": "Не назначена", "alive": True}
    player_to_chat[uid] = chat_id
    await save_data()
    await callback.answer("Ты в игре!")
    await refresh_lobby(chat_id)


@router.callback_query(F.data.startswith("leave:"))
async def leave_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    chat_id = int(callback.data.split(":")[1])
    game = games.get(chat_id)
    uid = callback.from_user.id
    if game and game["phase"] == "lobby" and uid in game["players"]:
        game["players"].pop(uid, None)
        player_to_chat.pop(uid, None)
        await save_data()
        await callback.answer("Ты вышел.")
        await refresh_lobby(chat_id)
    else:
        await callback.answer("Сейчас выйти нельзя.", show_alert=True)


@router.callback_query(F.data.startswith("begin:"))
async def begin_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    await callback.answer(await begin_game(int(callback.data.split(":")[1]), callback.from_user.id), show_alert=True)


@router.callback_query(F.data.startswith(("vote:", "kill:", "check:", "heal:", "maniac:", "visit:", "lawyer:", "observe:", "journalist:", "guard:")))
async def action_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.data:
        return
    action, chat_text, target_text = callback.data.split(":")
    chat_id, target, uid = int(chat_text), int(target_text), callback.from_user.id
    game = games.get(chat_id)
    if not game or uid not in game["players"] or not game["players"][uid].get("alive"):
        await callback.answer("Ты не участвуешь в этой игре.", show_alert=True)
        return
    if target not in game["players"] or not game["players"][target].get("alive") or target == uid:
        await callback.answer("Цель недоступна.", show_alert=True)
        return
    player = game["players"][uid]
    if action == "kill" and side(game["players"][target]["role"]) == "mafia":
        await callback.answer("Мафия не может выбрать союзника.", show_alert=True)
        return
    if action == "vote":
        if game["phase"] != "day":
            await callback.answer("Голосовать можно днём.", show_alert=True)
            return
        player["vote_target"] = target
        game["votes"][uid] = target
        await save_data()
        await callback.answer(f"Голос принят: {display_name(target)}")
        return
    if game["phase"] != "night":
        await callback.answer("Это действие доступно только ночью.", show_alert=True)
        return
    expected = role_action(player["role"], game)
    if action != expected:
        await callback.answer("Твоя роль не может выполнить это действие.", show_alert=True)
        return
    game["night_actions"][uid] = target
    await save_data()
    await callback.answer("Принято.")


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    if not message.from_user:
        return
    rec = touch_user(message.from_user)
    await save_data()
    await message.answer(f"🎭 <b>{escape(message.from_user.full_name)}</b>\n"
                         f"Партий: {rec.get('games', 0)} · Побед: {rec.get('wins', 0)} · Поражений: {rec.get('losses', 0)}")


@router.message(Command("leaderboard"))
async def leaderboard_command(message: Message) -> None:
    ordered = sorted(data["users"].values(), key=lambda u: (int(u.get("wins", 0)), int(u.get("games", 0))), reverse=True)[:10]
    lines = ["🏆 <b>Топ победителей</b>"]
    lines.extend(f"{i}. {display_name(int(u.get('user_id', 0)))} — {int(u.get('wins', 0))}" for i, u in enumerate(ordered, 1))
    await message.answer("\n".join(lines))


@router.message(Command("admin"))
async def admin_command(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("⛔ Нет доступа.")
        return
    await message.answer("🛡 <b>Mafia Keep · владелец</b>", reply_markup=owner_keyboard())


@router.callback_query(F.data.startswith("adm:"))
async def admin_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id):
        await callback.answer("Только владелец.", show_alert=True)
        return
    if not callback.data:
        return
    section = callback.data.split(":", 1)[1]
    await callback.answer()
    if not callback.message:
        return
    if section == "home":
        await callback.message.edit_text("🛡 <b>Mafia Keep · владелец</b>", reply_markup=owner_keyboard())
    elif section == "stats":
        await callback.message.edit_text(
            f"📊 <b>Статистика</b>\nПользователей: {len(data['users'])}\nАктивных игр: {len(games)}\n"
            f"Банов: {len(data['bans'])}\nЗавершено партий: {data['totals']['games']}\n"
            f"Аптайм процесса: {int(time.monotonic() - online_since)} сек.",
            reply_markup=kb([[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]]))
    elif section == "games":
        body = "\n".join(f"• <code>{cid}</code> · {g['phase']} · {len(g['players'])} игроков"
                          for cid, g in games.items()) or "Активных игр нет."
        await callback.message.edit_text("🎮 <b>Игры</b>\n" + body,
                                         reply_markup=kb([[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]]))
    elif section == "settings":
        await callback.message.edit_text("⚙️ <b>Настройки игры</b>", reply_markup=settings_keyboard())
    elif section == "roles":
        await callback.message.edit_text("🎭 <b>Роли · нажми, чтобы включить или выключить</b>",
                                         reply_markup=roles_keyboard())
    elif section == "reset":
        await callback.message.edit_text(
            "⚠️ <b>Сбросить все активные игры?</b> Статистика игроков сохранится.",
            reply_markup=kb([
                [InlineKeyboardButton(text="🧹 Да, сбросить", callback_data="adm:reset_confirm")],
                [InlineKeyboardButton(text="Отмена", callback_data="adm:home")],
            ]))
    elif section == "reset_confirm":
        for game in games.values():
            for uid in game["players"]:
                player_to_chat.pop(uid, None)
        games.clear()
        await save_data()
        await callback.message.edit_text("🧹 Активные игры сброшены. Статистика сохранена.", reply_markup=owner_keyboard())
    elif section == "save":
        await save_data()
        await callback.message.edit_text("💾 <b>Данные сохранены.</b> Активные игры, настройки и статистика записаны.",
                                         reply_markup=owner_keyboard())
    elif section == "broadcast":
        await callback.message.edit_text("📣 Отправь команду /broadcast текст объявления.",
                                         reply_markup=kb([[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]]))
    elif section == "bans":
        body = "\n".join(f"• <code>{uid}</code> — {escape(str(info.get('reason', 'без причины')))}"
                          for uid, info in data["bans"].items()) or "Блокировок нет."
        await callback.message.edit_text("🚫 <b>Блокировки</b>\n" + body +
                                         "\n\nКоманды: /ban_player ID причина · /unban_player ID",
                                         reply_markup=kb([[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm:home")]]))


@router.callback_query(F.data.startswith("cfg:"))
async def config_callback(callback: CallbackQuery) -> None:
    if not callback.from_user or not is_admin(callback.from_user.id) or not callback.data:
        await callback.answer("Только владелец.", show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) == 3 and parts[1] == "toggle":
        key = parts[2]
        settings()[key] = not bool(settings()[key])
    elif len(parts) == 3 and parts[1] == "role":
        key = parts[2]
        settings()["roles_enabled"][key] = not settings()["roles_enabled"].get(key, True)
    elif len(parts) == 3:
        key, delta = parts[1], int(parts[2])
        if key == "min":
            settings()["min_players"] = max(2, min(int(settings()["max_players"]), int(settings()["min_players"]) + delta))
        elif key == "max":
            settings()["max_players"] = max(int(settings()["min_players"]), min(30, int(settings()["max_players"]) + delta))
        elif key == "night":
            settings()["night_seconds"] = max(10, min(120, int(settings()["night_seconds"]) + delta))
        elif key == "day":
            settings()["day_seconds"] = max(10, min(180, int(settings()["day_seconds"]) + delta))
        elif key == "reg":
            settings()["registration_seconds"] = max(60, min(1800, int(settings()["registration_seconds"]) + delta))
    await save_data()
    await callback.answer("Настройка сохранена.")
    if callback.message:
        if len(parts) >= 2 and parts[1] == "role":
            await callback.message.edit_reply_markup(reply_markup=roles_keyboard())
        else:
            await callback.message.edit_reply_markup(reply_markup=settings_keyboard())


@router.message(Command("set_min_players"))
async def set_min_players(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /set_min_players 2")
        return
    value = int(parts[1])
    if not 2 <= value <= int(settings()["max_players"]):
        await message.answer("Минимум: 2; значение не должно превышать максимум.")
        return
    settings()["min_players"] = value
    await save_data()
    await message.answer(f"Минимум игроков: {value}.")


@router.message(Command("ban_player"))
async def ban_player(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Формат: /ban_player ID причина")
        return
    uid = int(parts[1])
    if uid == ADMIN_ID:
        await message.answer("Владельца нельзя заблокировать.")
        return
    reason = parts[2] if len(parts) > 2 else "нарушение правил"
    data["bans"][str(uid)] = {"reason": reason, "by": ADMIN_ID, "at": datetime.now(timezone.utc).isoformat()}
    if str(uid) in data["users"]:
        data["users"][str(uid)]["banned"] = True
    for cid, game in list(games.items()):
        if uid in game["players"]:
            game["players"].pop(uid, None)
            player_to_chat.pop(uid, None)
            await refresh_lobby(cid)
    await save_data()
    await message.answer(f"⛔ Игрок {uid} заблокирован.")


@router.message(Command("unban_player"))
async def unban_player(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Формат: /unban_player ID")
        return
    uid = int(parts[1])
    data["bans"].pop(str(uid), None)
    if str(uid) in data["users"]:
        data["users"][str(uid)]["banned"] = False
    await save_data()
    await message.answer(f"✅ Блокировка {uid} снята.")


@router.message(Command("stat"))
async def stat_command(message: Message) -> None:
    if message.from_user and is_admin(message.from_user.id):
        await message.answer(f"Пользователей: {len(data['users'])}\nИгры: {len(games)}\nБаны: {len(data['bans'])}\nЗавершено: {data['totals']['games']}")


@router.message(Command("game_status"))
async def game_status(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    if message.chat.type == ChatType.PRIVATE:
        body = "\n".join(f"{cid}: {g['phase']} · {len(g['players'])} игроков" for cid, g in games.items()) or "Активных игр нет."
        await message.answer(body)
        return
    game = games.pop(message.chat.id, None)
    if game:
        for uid in game["players"]:
            player_to_chat.pop(uid, None)
        await message.answer("Игра в этом чате сброшена.")
    else:
        await message.answer("Игры нет.")


@router.message(Command("cancel_game"))
async def cancel_game(message: Message) -> None:
    if not message.from_user or message.chat.type == ChatType.PRIVATE:
        await message.answer("Команда работает в группе.")
        return
    game = games.get(message.chat.id)
    if not game:
        await message.answer("Игры нет.")
        return
    if message.from_user.id not in (game.get("host"), ADMIN_ID):
        await message.answer("Отменить может создатель или владелец.")
        return
    for uid in game["players"]:
        player_to_chat.pop(uid, None)
    games.pop(message.chat.id, None)
    await message.answer("Партия отменена.")


@router.message(Command("broadcast"))
async def broadcast_command(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        return
    body = (message.text or "").partition(" ")[2].strip()
    if not body:
        await message.answer("Формат: /broadcast текст")
        return
    sent = failed = 0
    await message.answer("Рассылка запущена.")
    for uid in list(data["users"]):
        try:
            await bot.send_message(int(uid), f"📣 <b>Mafia Keep</b>\n\n{escape(body)}")  # type: ignore[union-attr]
            sent += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            failed += 1
        await asyncio.sleep(0.05)
    await message.answer(f"Готово. Доставлено: {sent}; ошибок: {failed}.")


@router.message(F.text)
async def text_shortcuts(message: Message) -> None:
    if not message.from_user or not message.text or message.text.startswith("/"):
        return
    phrase = " ".join(message.text.lower().replace("ё", "е").strip().split())
    normalized = re.sub(r"[!.,?]+$", "", phrase)
    triggers = {
        "старт мафия", "старт мафии", "старт мафию", "старт мафиии",
        "начать мафию", "начать мафия", "начни мафию", "запустить мафию",
        "запусти мафию", "регистрация на мафию", "регистрация мафии",
        "создать мафию", "создать игру мафия", "мафия старт", "го мафия",
        "го в мафию", "игра мафия", "начинаем мафию", "начинаем мафия",
        "открыть регистрацию мафия", "открыть регистрацию на мафию",
        "мафия", "играем в мафию", "погнали мафия", "го в мафку",
    }
    mafia_intent = bool(re.search(
        r"\b(старт|начать|начни|запустить|запусти|создать|открыть|начинаем|погнали|го)\b.*\b(мафи\w*|мафк\w*)\b",
        normalized,
    ) or re.search(r"\b(мафи\w*|мафк\w*)\b.*\b(старт|начать|запустить|го)\b", normalized))
    if message.chat.type != ChatType.PRIVATE and (normalized in triggers or mafia_intent):
        await new_game(message)


@router.message()
async def remember_user(message: Message) -> None:
    if not message.from_user:
        return
    touch_user(message.from_user)
    await save_data()


async def resume_saved_games() -> None:
    """Restore the phase timers for persisted games after a runner restart."""
    now = time.time()
    for chat_id, game in list(games.items()):
        phase = game.get("phase")
        if phase == "lobby":
            message_id = game.get("lobby_message_id")
            if message_id:
                asyncio.create_task(registration_timeout(chat_id, int(message_id)))
        elif phase == "starting":
            asyncio.create_task(start_night(chat_id))
        elif phase in ("night", "day"):
            deadline = float(game.get("phase_deadline") or now)
            asyncio.create_task(resume_phase_timer(chat_id, phase, max(1, deadline - now)))


async def resume_phase_timer(chat_id: int, phase: str, delay: float) -> None:
    # Telegram messages survive a runner restart; re-send missing night prompts so
    # players are never locked out if the process stopped between phase save and DM.
    game = games.get(chat_id)
    if not game or game.get("phase") != phase:
        return
    if phase == "night":
        for uid in alive_players(game):
            if uid in game.get("night_actions", {}):
                continue
            role = game["players"][uid]["role"]
            action = role_action(role, game)
            if not action:
                continue
            choices = [(target, display_name(target)) for target in alive_players(game) if target != uid]
            if action == "kill":
                choices = [(target, name) for target, name in choices
                           if side(game["players"][target]["role"]) != "mafia"]
            try:
                await bot.send_message(
                    uid,
                    f"🌙 Ночь {game['round']} продолжается · <b>{escape(role)}</b>\n"
                    f"{escape(ROLE_DESCRIPTIONS.get(role, 'Выберите действие.'))}",
                    reply_markup=player_keyboard(chat_id, action, choices),
                )  # type: ignore[union-attr]
            except (TelegramForbiddenError, TelegramBadRequest):
                log.info("Could not restore night prompt for player %s", uid)
    await asyncio.sleep(max(1, delay))
    game = games.get(chat_id)
    if not game or game.get("phase") != phase:
        return
    if phase == "night":
        await resolve_night(chat_id)
    elif phase == "day":
        await resolve_day(chat_id)


async def timed_shutdown() -> None:
    await asyncio.sleep(CONTAINER_SECONDS)
    log.warning("Container runtime limit reached; stopping gracefully.")
    try:
        await bot.send_message(ADMIN_ID, "♻️ Mafia Keep сохраняет данные перед перезапуском.")  # type: ignore[union-attr]
    except Exception:
        log.exception("Owner notification failed")
    await save_data()
    await dp.stop_polling()


async def run() -> None:
    global bot, bot_username
    if not TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is missing. Add it in repository Settings > Secrets and variables > Actions.")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    shutdown_task: asyncio.Task | None = None
    try:
        me = await bot.get_me()
        bot_username = me.username or bot_username
        log.info("%s started as @%s; owner=%s; restored_games=%s", APP_NAME, me.username, ADMIN_ID, len(games))
        await resume_saved_games()
        shutdown_task = asyncio.create_task(timed_shutdown())
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), close_bot_session=False)
    except Exception:
        log.exception("Bot failed during startup or polling. Check TELEGRAM_TOKEN and Telegram API connectivity.")
        raise
    finally:
        if shutdown_task:
            shutdown_task.cancel()
        await save_data()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Stopped")
