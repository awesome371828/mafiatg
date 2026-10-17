from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import random
from urllib.parse import parse_qsl
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import aiosqlite
import uvicorn
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

DB_PATH = os.getenv("DB_PATH", "cryptoroyale.sqlite3")
BOT_TOKEN = os.getenv("TELEGRAM_BOT", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6652898792"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://awesome371828.github.io/cryptoroyale/")
PUBLIC_WS_URL = os.getenv("PUBLIC_WS_URL", "")
TICK_RATE = 30
TICK_SECONDS = 1 / TICK_RATE
MATCH_WAIT_SECONDS = 3
CARDS = {
    "knight": {"name": "Кибер-рыцарь", "cost": 3, "hp": 520, "damage": 58, "speed": 35, "range": 20, "cooldown": .85, "radius": 11},
    "archer": {"name": "Неон-лучник", "cost": 3, "hp": 270, "damage": 42, "speed": 31, "range": 105, "cooldown": 1.0, "radius": 9},
    "giant": {"name": "Крипто-голем", "cost": 5, "hp": 1250, "damage": 105, "speed": 19, "range": 24, "cooldown": 1.25, "radius": 15},
    "bomb": {"name": "Пиксель-бомба", "cost": 2, "hp": 160, "damage": 180, "speed": 43, "range": 30, "cooldown": .7, "radius": 10},
}

db: aiosqlite.Connection | None = None
bot: Bot | None = None
dp = Dispatcher()
api = FastAPI(title="Crypto Royale API", version="1.0.0")
api.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])


async def init_db() -> None:
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','banned')),
            is_vip INTEGER NOT NULL DEFAULT 0 CHECK(is_vip IN (0,1)),
            gold INTEGER NOT NULL DEFAULT 500,
            trophies INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
        )
    """)
    await db.commit()


async def ensure_user(user_id: int, username: str | None = None) -> dict[str, Any]:
    assert db is not None
    await db.execute(
        "INSERT INTO users(user_id, username) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET username=COALESCE(excluded.username, users.username), updated_at=strftime('%s','now')",
        (user_id, username),
    )
    await db.commit()
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row)


def verify_telegram_init_data(init_data: str) -> dict[str, Any] | None:
    """Validate Telegram WebApp initData using the official HMAC-SHA-256 scheme."""
    if not BOT_TOKEN or not init_data or len(init_data) > 16384:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash or "auth_date" not in pairs or "user" not in pairs:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated_hash, received_hash):
            return None
        auth_date = int(pairs["auth_date"])
        now = int(time.time())
        if auth_date > now + 60 or now - auth_date > 86400:
            return None
        user = json.loads(pairs["user"])
        if not isinstance(user, dict) or not isinstance(user.get("id"), int):
            return None
        return user
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


async def get_user(user_id: int) -> dict[str, Any] | None:
    assert db is not None
    cur = await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


class StatusMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id != ADMIN_ID:
            record = await ensure_user(user.id, user.username)
            if record["status"] == "banned":
                if isinstance(event, Message):
                    return None
        return await handler(event, data)


dp.message.middleware(StatusMiddleware())


def admin_only(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id == ADMIN_ID)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    if not message.from_user:
        return
    user = await ensure_user(message.from_user.id, message.from_user.username)
    if user["status"] == "banned":
        await message.answer("⛔ Вы заблокированы администратором Crypto Royale.")
        return
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Открыть Crypto Royale", web_app=WebAppInfo(url=WEBAPP_URL))
    ]])
    await message.answer(
        "⚡ <b>CRYPTO ROYALE</b>\n\n"
        "Арена ждёт командира. Собирай отряд, копи эликсир, прокачивай профиль "
        "и поднимайся по трофейной лестнице!\n\n"
        "🛡 Три башни • ⚡ Эликсир • 🤖 Быстрый матч с ИИ",
        reply_markup=keyboard,
        parse_mode="HTML",
    )


@dp.message(Command("ban"))
async def ban_user(message: Message) -> None:
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban USER_ID")
        return
    user_id = int(parts[1])
    await ensure_user(user_id)
    assert db is not None
    await db.execute("UPDATE users SET status='banned' WHERE user_id=?", (user_id,))
    await db.commit()
    await disconnect_user(user_id)
    await message.answer(f"⛔ Игрок {user_id} заблокирован в Crypto Royale.")
    if bot:
        with suppress(Exception):
            await bot.send_message(user_id, "⛔ Вы заблокированы администратором Crypto Royale.")


@dp.message(Command("unban"))
async def unban_user(message: Message) -> None:
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban USER_ID")
        return
    user_id = int(parts[1])
    await ensure_user(user_id)
    assert db is not None
    await db.execute("UPDATE users SET status='active' WHERE user_id=?", (user_id,))
    await db.commit()
    await message.answer(f"✅ Блокировка игрока {user_id} снята.")
    if bot:
        with suppress(Exception):
            await bot.send_message(user_id, "✅ Доступ к Crypto Royale восстановлен.")


@dp.message(Command("give_vip"))
async def give_vip(message: Message) -> None:
    if not admin_only(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /give_vip USER_ID")
        return
    user_id = int(parts[1])
    await ensure_user(user_id)
    assert db is not None
    await db.execute("UPDATE users SET is_vip=1 WHERE user_id=?", (user_id,))
    await db.commit()
    await message.answer(f"💎 VIP включён для игрока {user_id}. В бою эликсир восстанавливается на 50% быстрее.")


@dp.message(Command("stat"))
async def stats(message: Message) -> None:
    if not admin_only(message):
        return
    assert db is not None
    cur = await db.execute("SELECT COUNT(*) AS total, SUM(status='banned') AS banned FROM users")
    row = await cur.fetchone()
    await message.answer(f"📊 Игроков в базе: {row['total'] or 0}\n⛔ Заблокировано: {row['banned'] or 0}\n🟢 Онлайн-соединений: {len(manager.user_sockets)}")


@dataclass
class Unit:
    id: str
    owner: int
    card: str
    x: float
    y: float
    hp: float
    max_hp: float
    cooldown: float = 0.0


@dataclass
class Tower:
    id: str
    owner: int
    kind: str
    x: float
    y: float
    hp: float
    max_hp: float
    cooldown: float = 0.0


@dataclass
class Player:
    user_id: int
    username: str | None
    vip: bool
    ws: WebSocket | None = None
    elixir: float = 5.0
    gold: int = 0
    trophies: int = 0


@dataclass
class Room:
    id: str
    players: dict[int, Player]
    towers: list[Tower]
    units: list[Unit] = field(default_factory=list)
    started: bool = False
    finished: bool = False
    winner: int | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_broadcast: float = 0.0
    ai_id: int = -random.randint(1000000, 9000000)
    ai_timer: float = 1.2
    task: asyncio.Task | None = None


class MatchManager:
    def __init__(self):
        self.waiting: Player | None = None
        self.rooms: dict[str, Room] = {}
        self.user_rooms: dict[int, str] = {}
        self.user_sockets: dict[int, WebSocket] = {}
        self.lock = asyncio.Lock()

    def make_room(self, p1: Player, p2: Player) -> Room:
        room_id = f"cr-{p1.user_id}-{p2.user_id}-{int(time.time()*1000)}"
        towers = []
        for owner, side in ((p2.user_id, "enemy"), (p1.user_id, "player")):
            y = 95 if owner == p2.user_id else 505
            hp = 1700
            towers.extend([
                Tower(f"{owner}-left", owner, "guard", 110, y, hp, hp),
                Tower(f"{owner}-king", owner, "king", 200, y + (25 if y < 200 else -25), 2600, 2600),
                Tower(f"{owner}-right", owner, "guard", 290, y, hp, hp),
            ])
        room = Room(room_id, {p1.user_id: p1, p2.user_id: p2}, towers)
        self.rooms[room_id] = room
        self.user_rooms[p1.user_id] = room_id
        self.user_rooms[p2.user_id] = room_id
        room.started = True
        room.task = asyncio.create_task(self.run_room(room))
        return room

    async def send(self, player: Player, payload: dict[str, Any]) -> None:
        if player.ws:
            try:
                await player.ws.send_text(json.dumps(payload, separators=(",", ":")))
            except Exception:
                player.ws = None

    async def send_state(self, room: Room) -> None:
        state = {
            "type": "state", "roomId": room.id, "finished": room.finished,
            "winner": room.winner,
            "players": [{"id": p.user_id, "username": p.username, "vip": p.vip, "elixir": round(p.elixir, 2)} for p in room.players.values()],
            "towers": [vars(t) for t in room.towers],
            "units": [vars(u) for u in room.units],
            "cards": CARDS,
            "serverTime": time.time(),
        }
        await asyncio.gather(*(self.send(p, state) for p in room.players.values()))

    async def join(self, player: Player) -> Room | None:
        async with self.lock:
            if player.user_id in self.user_rooms:
                room = self.rooms.get(self.user_rooms[player.user_id])
                if room and not room.finished:
                    room.players[player.user_id].ws = player.ws
                    room.players[player.user_id].username = player.username
                    await self.send_state(room)
                    return room
            if self.waiting and self.waiting.user_id != player.user_id:
                opponent = self.waiting
                self.waiting = None
                return self.make_room(opponent, player)
            self.waiting = player
        await self.send(player, {"type": "queue", "message": "Ищем соперника…"})
        await asyncio.sleep(MATCH_WAIT_SECONDS)
        async with self.lock:
            if self.waiting and self.waiting.user_id == player.user_id:
                self.waiting = None
                ai = Player(self._ai_user_id(player.user_id), "Crypto AI", False)
                room = self.make_room(player, ai)
                await self.send(player, {"type": "match", "mode": "ai", "roomId": room.id, "message": "Соперник не найден — подключён Crypto AI."})
                return room
            return self.rooms.get(self.user_rooms.get(player.user_id, ""))

    @staticmethod
    def _ai_user_id(user_id: int) -> int:
        return -abs(user_id) - 10000000

    async def run_room(self, room: Room) -> None:
        last = time.monotonic()
        while not room.finished:
            now = time.monotonic()
            dt = min(now - last, .1)
            last = now
            self.simulate(room, dt)
            if now - room.last_broadcast >= TICK_SECONDS:
                room.last_broadcast = now
                await self.send_state(room)
            await asyncio.sleep(TICK_SECONDS)
        await self.send_state(room)
        await self.finish_room(room)

    def simulate(self, room: Room, dt: float) -> None:
        if room.finished:
            return
        for p in room.players.values():
            p.elixir = min(10, p.elixir + dt * (.42 if p.vip else .28))
        if any(p.user_id < 0 for p in room.players.values()):
            room.ai_timer -= dt
            if room.ai_timer <= 0:
                ai = next(p for p in room.players.values() if p.user_id < 0)
                playable = [key for key, card in CARDS.items() if ai.elixir >= card["cost"]]
                if playable:
                    card = random.choice(playable)
                    self.spawn(room, ai, card, random.uniform(100, 300), random.uniform(260, 330))
                room.ai_timer = random.uniform(.9, 2.4)
        for unit in list(room.units):
            if unit.hp <= 0:
                room.units.remove(unit)
                continue
            enemies = [t for t in room.towers if t.owner != unit.owner and t.hp > 0]
            if not enemies:
                continue
            target = min(enemies, key=lambda t: abs(t.x - unit.x) + abs(t.y - unit.y))
            card = CARDS[unit.card]
            distance = math.hypot(target.x - unit.x, target.y - unit.y)
            unit.cooldown -= dt
            if distance <= card["range"] + 18:
                if unit.cooldown <= 0:
                    target.hp = max(0, target.hp - card["damage"])
                    unit.cooldown = card["cooldown"]
            else:
                step = card["speed"] * dt
                dx, dy = target.x - unit.x, target.y - unit.y
                length = max(1, math.hypot(dx, dy))
                unit.x += dx / length * step
                unit.y += dy / length * step
        for tower in room.towers:
            if tower.hp <= 0:
                continue
            tower.cooldown -= dt
            if tower.cooldown > 0:
                continue
            targets = [u for u in room.units if u.owner != tower.owner and math.hypot(u.x - tower.x, u.y - tower.y) < 125]
            if targets:
                target = min(targets, key=lambda u: math.hypot(u.x - tower.x, u.y - tower.y))
                target.hp -= 65 if tower.kind == "king" else 45
                tower.cooldown = .75
        for owner in list(room.players):
            enemy_towers = [t for t in room.towers if t.owner != owner]
            if enemy_towers and all(t.hp <= 0 for t in enemy_towers):
                room.finished, room.winner = True, owner
                break
        kings = [t for t in room.towers if t.kind == "king"]
        if any(t.hp <= 0 for t in kings):
            dead = next(t for t in kings if t.hp <= 0)
            room.finished = True
            room.winner = next((p for p in room.players if p != dead.owner), None)

    def spawn(self, room: Room, player: Player, card_key: str, x: float, y: float) -> bool:
        card = CARDS.get(card_key)
        if not card or player.elixir < card["cost"] or room.finished:
            return False
        # Clamp a player's units to their own side; AI uses the opposite side.
        y = max(250, min(440, y)) if player.user_id >= 0 else max(160, min(350, y))
        player.elixir -= card["cost"]
        room.units.append(Unit(f"u-{random.randrange(10**9)}", player.user_id, card_key, max(25, min(375, x)), y, card["hp"], card["hp"]))
        return True

    async def action(self, user_id: int, payload: dict[str, Any]) -> None:
        room_id = self.user_rooms.get(user_id)
        room = self.rooms.get(room_id or "")
        if not room or room.finished:
            return
        player = room.players.get(user_id)
        if not player:
            return
        if payload.get("action") == "play":
            card = str(payload.get("card", ""))
            try:
                x, y = float(payload.get("x", 200)), float(payload.get("y", 400))
            except (TypeError, ValueError):
                return
            success = self.spawn(room, player, card, x, y)
            await self.send(player, {"type": "action", "ok": success, "message": "Отряд призван." if success else "Недостаточно эликсира или неизвестная карта."})

    async def leave(self, user_id: int, ws: WebSocket | None = None) -> None:
        if ws and self.user_sockets.get(user_id) is not ws:
            return
        self.user_sockets.pop(user_id, None)
        room_id = self.user_rooms.get(user_id)
        room = self.rooms.get(room_id or "")
        if room and user_id in room.players:
            room.players[user_id].ws = None
            if room.finished:
                self.user_rooms.pop(user_id, None)
        async with self.lock:
            if self.waiting and self.waiting.user_id == user_id:
                self.waiting = None

    async def finish_room(self, room: Room) -> None:
        for user_id, player in list(room.players.items()):
            if user_id > 0:
                assert db is not None
                won = user_id == room.winner
                await db.execute(
                    "UPDATE users SET gold=gold+?, trophies=MAX(0,trophies+?), wins=wins+?, losses=losses+?, level=MIN(50, 1 + CAST(MAX(0,trophies+?) / 100 AS INTEGER)), updated_at=strftime('%s','now') WHERE user_id=?",
                    (120 if won else 45, 28 if won else -16, int(won), int(not won), 28 if won else -16, user_id),
                )
                await db.commit()
                await self.send(player, {"type": "result", "result": "victory" if won else "defeat", "gold": 120 if won else 45, "trophyDelta": 28 if won else -16})
                self.user_rooms.pop(user_id, None)
        self.rooms.pop(room.id, None)

    async def disconnect_user(self, user_id: int) -> None:
        ws = self.user_sockets.get(user_id)
        if ws:
            with suppress(Exception):
                await ws.send_text(json.dumps({"type": "banned", "message": "Вы забанены админом Crypto Royale!"}))
                await ws.close(code=4003, reason="banned")
        await self.leave(user_id, ws)


manager = MatchManager()
disconnect_user = manager.disconnect_user


@api.on_event("startup")
async def startup() -> None:
    await init_db()
    if BOT_TOKEN:
        global bot
        bot = Bot(BOT_TOKEN)
        asyncio.create_task(dp.start_polling(bot))


@api.on_event("shutdown")
async def shutdown() -> None:
    if bot:
        with suppress(Exception):
            await dp.stop_polling()
            await bot.session.close()
    if db:
        await db.close()
    for room in list(manager.rooms.values()):
        if room.task:
            room.task.cancel()


@api.get("/")
async def root():
    return {"name": "Crypto Royale", "status": "online", "tick_rate": TICK_RATE, "websocket": "/ws"}


@api.get("/health")
async def health():
    return {"ok": True, "database": db is not None, "rooms": len(manager.rooms), "online": len(manager.user_sockets)}


@api.get("/api/profile/{user_id}")
async def profile(user_id: int, x_telegram_init_data: str = Header(default="", alias="X-Telegram-Init-Data")):
    telegram_user = verify_telegram_init_data(x_telegram_init_data)
    if not telegram_user or telegram_user["id"] != user_id:
        raise HTTPException(status_code=401, detail="Valid Telegram WebApp initData required")
    user = await get_user(user_id)
    if not user:
        return {"ok": False, "error": "user_not_registered"}
    if user["status"] == "banned":
        return {"ok": False, "banned": True, "message": "Вы забанены админом Crypto Royale!"}
    return {"ok": True, "profile": user}


@api.get("/api/leaderboard")
async def leaderboard():
    assert db is not None
    cur = await db.execute("SELECT user_id, username, trophies, level, wins FROM users WHERE status='active' ORDER BY trophies DESC, wins DESC LIMIT 50")
    return {"players": [dict(r) for r in await cur.fetchall()]}


@api.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    user_id: int | None = None
    try:
        raw = await asyncio.wait_for(ws.receive_text(), timeout=15)
        hello = json.loads(raw)
        if hello.get("type") != "auth":
            await ws.send_json({"type": "error", "message": "Сначала отправьте auth."})
            await ws.close(code=4400)
            return
        telegram_user = verify_telegram_init_data(str(hello.get("initData") or ""))
        if not telegram_user:
            await ws.send_json({"type": "error", "message": "Не удалось подтвердить Telegram WebApp. Откройте игру из Telegram и обновите её."})
            await ws.close(code=4401, reason="invalid initData")
            return
        user_id = int(telegram_user["id"])
        if hello.get("userId") is not None and str(hello.get("userId")) != str(user_id):
            await ws.send_json({"type": "error", "message": "Telegram ID не совпадает с проверенными данными."})
            await ws.close(code=4401, reason="user mismatch")
            return
        if user_id <= 0:
            await ws.send_json({"type": "error", "message": "Некорректный user ID."})
            await ws.close(code=4400)
            return
        verified_username = telegram_user.get("username") or " ".join(filter(None, [telegram_user.get("first_name"), telegram_user.get("last_name")])) or None
        user = await ensure_user(user_id, str(verified_username or "")[:64] or None)
        if user["status"] == "banned":
            await ws.send_json({"type": "banned", "message": "Вы забанены админом Crypto Royale!"})
            await ws.close(code=4003, reason="banned")
            return
        manager.user_sockets[user_id] = ws
        player = Player(user_id, user.get("username"), bool(user["is_vip"]), ws, gold=user["gold"], trophies=user["trophies"])
        room = await manager.join(player)
        if room:
            await manager.send(player, {"type": "match", "mode": "ai" if any(p.user_id < 0 for p in room.players.values()) else "pvp", "roomId": room.id, "message": "Бой начался!"})
        while True:
            payload = json.loads(await ws.receive_text())
            if payload.get("type") == "play":
                await manager.action(user_id, payload)
            elif payload.get("type") == "ping":
                await ws.send_json({"type": "pong", "time": time.time()})
    except WebSocketDisconnect:
        pass
    except asyncio.TimeoutError:
        with suppress(Exception):
            await ws.close(code=4408, reason="auth timeout")
    except (json.JSONDecodeError, ValueError):
        with suppress(Exception):
            await ws.send_json({"type": "error", "message": "Некорректное сообщение."})
    finally:
        if user_id is not None:
            await manager.leave(user_id, ws)


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("WARNING: TELEGRAM_BOT is unset; the WebSocket/API server will start, but Telegram bot polling is disabled.")
    uvicorn.run(api, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8000")))
