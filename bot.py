import asyncio
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
from aiohttp import web

TOKEN = os.environ["BOT_TOKEN"]
GAME_URL = os.environ.get("GAME_URL", "https://mpodzolckov-arch.github.io/Game-/")
PORT = int(os.environ.get("PORT", "8080"))
FILE = "players.json"
REMIND_AFTER = 20 * 3600
ALLOWED = {"boss", "loot", "win", "hero", "daily"}

bot = Bot(TOKEN)
dp = Dispatcher()
players: dict = {}


def load():
    global players
    try:
        with open(FILE, encoding="utf-8") as f:
            players = json.load(f)
    except Exception:
        players = {}


def save():
    with open(FILE, "w", encoding="utf-8") as f:
        json.dump(players, f)


def user_id(init_data: str):
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(good, got) and "user" in data:
        return json.loads(data["user"])["id"]
    return None


def play_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⚔️ Играть", web_app=WebAppInfo(url=GAME_URL))
    ]])


@dp.message(CommandStart())
async def start(m: Message):
    players.setdefault(str(m.from_user.id), {"last": time.time(), "reminded": 0})
    save()
    await m.answer("Добро пожаловать в Тёмные земли! Здесь я буду присылать твои достижения "
                   "и напоминать о ежедневной награде.", reply_markup=play_kb())


async def notify(request: web.Request):
    try:
        body = await request.json()
        uid = user_id(body.get("initData", ""))
        kind = body.get("type")
        text = str(body.get("text", ""))[:300]
    except Exception:
        raise web.HTTPBadRequest()
    if uid is None:
        raise web.HTTPForbidden()
    if kind not in ALLOWED:
        raise web.HTTPBadRequest()
    p = players.setdefault(str(uid), {"last": time.time(), "reminded": 0})
    if kind == "daily":
        p["last"] = time.time()
        p["reminded"] = 0
    save()
    try:
        await bot.send_message(uid, text, reply_markup=play_kb())
    except Exception:
        pass
    return web.json_response({"ok": True})


@web.middleware
async def cors(request, handler):
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        resp = await handler(request)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return resp


async def health(request):
    return web.Response(text="ok")


async def reminders():
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for uid, p in list(players.items()):
            if now - p.get("last", now) > REMIND_AFTER and now - p.get("reminded", 0) > 86400:
                p["reminded"] = now
                save()
                try:
                    await bot.send_message(int(uid), "🎁 Ежедневная награда ждёт тебя в Тёмных землях! "
                                           "Не прерывай серию дней.", reply_markup=play_kb())
                except Exception:
                    pass


async def main():
    load()
    app = web.Application(middlewares=[cors])
    app.router.add_post("/notify", notify)
    app.router.add_route("OPTIONS", "/notify", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(reminders())
    await dp.start_polling(bot)


if name == "main":
    asyncio.run(main())
