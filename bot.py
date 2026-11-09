"""Бот для игры «Пепел Эльдары»: принимает события из игры и пишет игроку в чат.

Запуск:  BOT_TOKEN=123:ABC  python bot.py
Переменные:
  BOT_TOKEN  токен от @BotFather (обязательно)
  GAME_URL   ссылка на игру (https://mpodzolckov-arch.github.io/Game-/)
  PORT       порт сервера (хостинг обычно задаёт сам)
"""
import asyncio
import hashlib
import hmac
import json
import os
import random
import time
import urllib.parse
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, BotCommand, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, MenuButtonWebApp, Message, PreCheckoutQuery, WebAppInfo
import aiohttp
from aiohttp import web, WSMsgType

TOKEN = os.environ["BOT_TOKEN"]
GAME_URL = os.environ.get("GAME_URL", "https://mpodzolckov-arch.github.io/Game-/")
PORT = int(os.environ.get("PORT", "8080"))
FILE = "players.json"
SCORES = "scores.json"
REMIND_AFTER = 20 * 3600  # напомнить о награде через 20 часов после последней
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_KEY", "")
CLASSES = {"warrior", "mage", "archer", "assassin", "medic", "wmage", "fmage", "witch", "fairy", "swordsman", "lightning", "angel", "samurai", "druid", "pirate", "necro", "hunter", "bard"}
ARENA_DAILY = 5
ARENA_REWARD_FIGHTS = 10  # бои с наградой в день (сами бои без ограничений)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
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


def user_info(init_data: str):
    """Проверяет подпись Telegram, возвращает данные игрока (dict) или None."""
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
    if hmac.compare_digest(good, got) and "user" in data:
        return json.loads(data["user"])
    return None


def user_id(init_data: str):
    u = user_info(init_data)
    return u["id"] if u else None



session = None
local_db: dict = {}  # запасной вариант без базы (пропадает при перезапуске)


def day_key():
    return time.strftime("%Y-%m-%d", time.gmtime())


def load_local():
    global local_db
    try:
        with open("local_db.json", encoding="utf-8") as f:
            local_db = json.load(f)
    except Exception:
        local_db = {}


def save_local():
    try:
        with open("local_db.json", "w", encoding="utf-8") as f:
            json.dump(local_db, f, ensure_ascii=False)
    except Exception:
        pass


async def sb(method, path, params=None, body=None, prefer=None, count=False):
    headers = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
    if prefer:
        headers["Prefer"] = prefer
    if count:
        headers["Prefer"] = "count=exact"
        headers["Range"] = "0-0"
    async with session.request(method, SB_URL + "/rest/v1/" + path, params=params, json=body, headers=headers) as r:
        if r.status >= 300:
            raise RuntimeError("supabase %s %s" % (r.status, await r.text()))
        if count:
            return int(r.headers.get("Content-Range", "0/0").split("/")[-1] or 0)
        txt = await r.text()
        return json.loads(txt) if txt else None


async def db_get(uid):
    uid = int(uid)
    if SB_URL:
        rows = await sb("GET", "players", {"uid": "eq.%d" % uid, "select": "*"})
        return rows[0] if rows else None
    return local_db.get(str(uid))


async def db_put(uid, **fields):
    uid = int(uid)
    if SB_URL:
        row = await db_get(uid)
        if row is None:
            row = {"uid": uid}
        row.update(fields)
        await sb("POST", "players", {"on_conflict": "uid"}, [row], prefer="resolution=merge-duplicates,return=minimal")
        return
    row = local_db.setdefault(str(uid), {"uid": uid, "name": "Герой", "best": 0, "best_t": 0, "rating": 1000,
                                         "wins": 0, "losses": 0, "squad": None, "day": "", "used": 0})
    row.update(fields)
    save_local()


def sort_best(rows):
    return sorted(rows, key=lambda r: (-r.get("best", 0), r.get("best_t", 0)))


async def top_best(limit=20):
    if SB_URL:
        return await sb("GET", "players", {"select": "uid,name,best", "best": "gt.0", "order": "best.desc,best_t.asc", "limit": str(limit)})
    return sort_best([r for r in local_db.values() if r.get("best", 0) > 0])[:limit]


def safe(default):
    def deco(fn):
        async def w(*a, **k):
            try:
                return await fn(*a, **k)
            except Exception as e:
                print('db error', fn.__name__, e)
                return default
        return w
    return deco


@safe(None)
async def rank_best(row):
    if not row or row.get("best", 0) <= 0:
        return None
    if SB_URL:
        n = await sb("GET", "players", {"select": "uid", "or": "(best.gt.%d,and(best.eq.%d,best_t.lt.%s))" % (row["best"], row["best"], row.get("best_t", 0))}, count=True)
        return n + 1
    return sum(1 for r in sort_best(list(local_db.values())) if (-r.get("best", 0), r.get("best_t", 0)) < (-row["best"], row.get("best_t", 0))) + 1


@safe(0)
async def total_best():
    if SB_URL:
        return await sb("GET", "players", {"select": "uid", "best": "gt.0"}, count=True)
    return sum(1 for r in local_db.values() if r.get("best", 0) > 0)


async def top_rating(limit=10):
    if SB_URL:
        return await sb("GET", "players", {"select": "uid,name,rating,wins,losses", "squad": "not.is.null", "order": "rating.desc", "limit": str(limit)})
    rows = [r for r in local_db.values() if r.get("squad")]
    return sorted(rows, key=lambda r: -r.get("rating", 1000))[:limit]


@safe(None)
async def rank_rating(row):
    if SB_URL:
        return await sb("GET", "players", {"select": "uid", "squad": "not.is.null", "rating": "gt.%d" % row["rating"]}, count=True) + 1
    return sum(1 for r in local_db.values() if r.get("squad") and r.get("rating", 1000) > row["rating"]) + 1


async def near(uid, rating, spread):
    lo, hi = rating - spread, rating + spread
    if SB_URL:
        return await sb("GET", "players", {"select": "uid,name,rating,wins,losses,squad", "squad": "not.is.null", "uid": "neq.%d" % int(uid),
                                           "and": "(rating.gte.%d,rating.lte.%d)" % (lo, hi), "limit": "40"})
    return [r for r in local_db.values() if r.get("squad") and r["uid"] != int(uid) and lo <= r.get("rating", 1000) <= hi]


def clean_squad(raw):
    """Проверяет присланный отряд: только известные классы и разумные числа."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4:
        return None
    out = []
    try:
        for h in raw:
            c = h.get("c")
            if c not in CLASSES:
                return None
            clamp = lambda v, a, b: max(a, min(b, float(v)))
            out.append({"c": c, "l": int(clamp(h["l"], 1, 100)), "hp": int(clamp(h["hp"], 1, 300000)), "a": int(clamp(h["a"], 1, 60000)),
                        "d": int(clamp(h["d"], 0, 6000)), "cd": int(clamp(h.get("cd", 4), 2, 8)), "sm": round(clamp(h.get("sm", 1), 1, 3), 2),
                        "hs": int(clamp(h.get("hs", -1), -1, 4)), "ws": int(clamp(h.get("ws", -1), -1, 4)), "n": str(h.get("n", ""))[:14]})
    except Exception:
        return None
    return out


def power(squad):
    return int(sum(h["hp"] / 10 + h["a"] * 3 + h["d"] * 2 for h in squad))


BOT_NAMES = ["Страж арены", "Тёмный гладиатор", "Призрачный рыцарь", "Кровавый чемпион", "Тень колизея"]


def make_bot(squad, k, rating):
    f = (0.8, 1.0, 1.2)[k % 3] * random.uniform(0.92, 1.08)
    out = []
    for h in squad:
        c = random.choice(sorted(CLASSES))
        out.append({"c": c, "l": max(1, int(h["l"] * f)), "hp": max(10, int(h["hp"] * f)), "a": max(1, int(h["a"] * f)), "d": int(h["d"] * f),
                    "cd": 4, "sm": 1, "hs": -1, "ws": -1, "n": ""})
    return {"uid": -(k + 1), "name": BOT_NAMES[random.randrange(len(BOT_NAMES))], "rating": max(0, rating + (k - 1) * 40), "squad": out, "bot": True}


def elo(rm, ro, win):
    e = 1 / (1 + 10 ** ((ro - rm) / 400))
    d = round(24 * ((1 if win else 0) - e))
    return max(1, d) if win else min(-1, d)


pending: dict = {}


async def touch(uid, name=None):
    """Отмечает активность игрока. Возвращает строку до обновления (None, если игрок новый)."""
    row = await db_get(uid)
    f = {"seen": time.time()}
    if row is None or not row.get("first"):
        f["first"] = time.time()
    if name:
        f["name"] = name
    await db_put(uid, **f)
    return row


async def count_since(col, since):
    if SB_URL:
        return await sb("GET", "players", {"select": "uid", col: "gte.%s" % since}, count=True)
    return sum(1 for r in local_db.values() if r.get(col, 0) >= since and r.get(col, 0) > 0)


async def me(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
        ref = str(body.get("ref", ""))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    uid = int(u["id"])
    name = (u.get("first_name") or u.get("username") or "Герой")[:20]
    before = await touch(uid, name)
    is_new = before is None or not before.get("first")
    out = {"welcome": 0, "claim": 0}
    try:
        r = int(ref.replace("ref_", "")) if ref.startswith("ref_") else 0
    except ValueError:
        r = 0
    row = await db_get(uid)
    if r and r != uid and is_new and not row.get("welcomed"):
        referrer = await db_get(r)
        if referrer:
            await db_put(uid, ref_by=r, welcomed=True)
            await db_put(r, invites=referrer.get("invites", 0) + 1)
            out["welcome"] = 10
            try:
                await bot.send_message(r, "👥 По твоей ссылке пришёл %s! +20 ♦️ ждут тебя в игре." % name, reply_markup=play_kb())
            except Exception:
                pass
    row = await db_get(uid)
    pend = row.get("invites", 0) - row.get("claimed", 0)
    if pend > 0:
        await db_put(uid, claimed=row.get("invites", 0))
        out["claim"] = pend * 20
    out["invites"] = row.get("invites", 0)
    return web.json_response(out)


async def stats(request: web.Request):
    if not ADMIN_KEY or request.query.get("key") != ADMIN_KEY:
        raise web.HTTPForbidden()
    now = time.time()
    return web.json_response({"всего_игроков": await count_since("first", 1), "новых_за_24ч": await count_since("first", now - 86400),
                              "активных_за_24ч": await count_since("seen", now - 86400), "активных_за_7д": await count_since("seen", now - 7 * 86400)})


# ---------- арена: сезоны, реванш ----------
SEASON_TOP = 30


def week_key():
    return int((time.time() - 4 * 86400) // 604800)  # неделя начинается в понедельник (UTC)


def season_reward(rank):
    return 300 if rank == 1 else 200 if rank == 2 else 150 if rank == 3 else 80 if rank <= 10 else 30 if rank <= SEASON_TOP else 0


async def season_roll():
    """В начале новой недели фиксирует таблицу лидеров прошлой недели (награды можно забрать один раз)."""
    w = week_key()
    async with kv_lock:
        ss = await kv_get("arena:season")
        if not isinstance(ss, dict) or "w" not in ss:
            ss = {"w": w, "prev": None}
            await kv_put("arena:season", ss)
        elif ss["w"] != w:
            top = await top_rating(SEASON_TOP)
            ss = {"w": w, "prev": {"w": ss["w"], "r": [[t["uid"], t["name"], t.get("rating", 1000)] for t in top], "cl": {}}}
            await kv_put("arena:season", ss)
    return ss


def season_info(ss, uid):
    out = {"left": int(604800 - (time.time() - 4 * 86400) % 604800)}
    pv = ss.get("prev")
    if pv:
        rank = next((i + 1 for i, r in enumerate(pv["r"]) if r[0] == uid), 0)
        out.update({"rank": rank, "rew": season_reward(rank) if rank else 0, "cl": str(uid) in pv["cl"]})
    return out


async def revenge_add(victim, attacker, name):
    """Запоминает, кто победил игрока на арене (для реванша)."""
    async with kv_lock:
        lst = await kv_get("av:%d" % victim)
        lst = [x for x in (lst if isinstance(lst, list) else []) if x.get("u") != attacker]
        lst.insert(0, {"u": attacker, "n": name, "t": int(time.time())})
        await kv_put("av:%d" % victim, lst[:5])


async def revenge_done(uid, target):
    async with kv_lock:
        lst = await kv_get("av:%d" % uid)
        if isinstance(lst, list) and any(x.get("u") == target for x in lst):
            await kv_put("av:%d" % uid, [x for x in lst if x.get("u") != target])


async def revenge_list(uid):
    async with kv_lock:
        lst = await kv_get("av:%d" % uid)
    out = []
    for x in (lst if isinstance(lst, list) else []):
        o = await db_get(x["u"])
        if o and o.get("squad"):
            out.append({"uid": o["uid"], "name": o.get("name") or x.get("n", "Игрок"), "rating": o.get("rating", 1000), "pw": power(o["squad"]),
                        "w": o.get("wins", 0), "l": o.get("losses", 0)})
    return out


async def arena_state(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    uid = int(u["id"])
    squad = clean_squad(body.get("squad"))
    name = (u.get("first_name") or u.get("username") or "Герой")[:20]
    row = await db_get(uid) or {}
    await touch(uid, name)
    fields = {"updated": time.time()}
    if squad:
        fields["squad"] = squad
    await db_put(uid, **fields)
    row = await db_get(uid)
    rating = row.get("rating", 1000)
    used = row.get("used", 0) if row.get("day") == day_key() else 0
    opps = []
    for spread in (150, 300, 600, 5000):
        opps = await near(uid, rating, spread)
        if len(opps) >= 5:
            break
    random.shuffle(opps)
    opps = opps[:5]
    mysq = row.get("squad") or squad or []
    items = [{"uid": o["uid"], "name": o["name"], "rating": o.get("rating", 1000), "pw": power(o["squad"]), "w": o.get("wins", 0), "l": o.get("losses", 0)} for o in opps]
    k = 0
    while len(items) < 3 and mysq:
        b = make_bot(mysq, k, rating)
        items.append({"uid": b["uid"], "name": b["name"], "rating": b["rating"], "pw": power(b["squad"]), "w": 0, "l": 0})
        k += 1
    tops = await top_rating(10)
    try:
        ss = season_info(await season_roll(), uid)
        rev = await revenge_list(uid)
    except Exception as e:
        print("arena extra", e)
        ss, rev = {}, []
    return web.json_response({"ss": ss, "rev": rev, "me": {"rating": rating, "rank": await rank_rating(row), "left": 999, "rl": max(0, ARENA_REWARD_FIGHTS - used), "w": row.get("wins", 0), "l": row.get("losses", 0)},
                              "opps": items, "top": [{"n": t["name"], "r": t.get("rating", 1000), "me": t["uid"] == uid} for t in tops], "db": bool(SB_URL)})


async def arena_fight(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
        target = int(body.get("opp"))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    uid = int(u["id"])
    row = await db_get(uid)
    if not row or not row.get("squad"):
        raise web.HTTPBadRequest()
    used = row.get("used", 0) if row.get("day") == day_key() else 0
    if target < 0:
        opp = make_bot(row["squad"], -target - 1, row.get("rating", 1000))
    else:
        opp = await db_get(target)
        if not opp or not opp.get("squad") or target == uid:
            return web.json_response({"error": "gone"})
    await db_put(uid, day=day_key(), used=used + 1)
    if target > 0:
        try:
            await revenge_done(uid, target)
        except Exception as e:
            print("revenge_done", e)
    pending[uid] = {"opp": opp["uid"], "name": opp["name"], "orating": opp.get("rating", 1000), "t": time.time()}
    return web.json_response({"opp": {"uid": opp["uid"], "name": opp["name"], "rating": opp.get("rating", 1000), "squad": opp["squad"]}, "left": 999})


async def arena_result(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
        win = bool(body.get("win"))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    uid = int(u["id"])
    p = pending.pop(uid, None)
    if not p or time.time() - p["t"] > 900:
        return web.json_response({"error": "nofight"})
    row = await db_get(uid)
    rm = row.get("rating", 1000)
    d = elo(rm, p["orating"], win)
    new = max(0, rm + d)
    await db_put(uid, rating=new, wins=row.get("wins", 0) + (1 if win else 0), losses=row.get("losses", 0) + (0 if win else 1))
    if p["opp"] > 0:
        o = await db_get(p["opp"])
        if o:
            od = -round(d * 0.5) if win else round(-d * 0.5)
            await db_put(p["opp"], rating=max(0, o.get("rating", 1000) + od))
            if win:
                try:
                    await revenge_add(p["opp"], uid, (u.get("first_name") or "Игрок")[:20])
                except Exception as e:
                    print("revenge_add", e)
            try:
                nm = (u.get("first_name") or "Игрок")[:20]
                txt = ("🛡️ %s атаковал твой отряд на арене и проиграл. Рейтинг: %+d" % (nm, od)) if not win else ("⚔️ %s атаковал твой отряд на арене и победил. Рейтинг: %+d" % (nm, od))
                await bot.send_message(p["opp"], txt, reply_markup=play_kb())
            except Exception:
                pass
    used_today = row.get("used", 0) if row.get("day") == day_key() else 0
    rew = 3 if (win and used_today <= ARENA_REWARD_FIGHTS) else 0
    return web.json_response({"ok": True, "delta": d, "rating": new, "reward": rew, "rl": max(0, ARENA_REWARD_FIGHTS - used_today)})


# ===== Битва башен: живой бой 1 на 1 (подбор + ретрансляция) =====
tb_queue = []


def tb_clean(raw):
    out = []
    for k in (raw or [])[:8]:
        k = str(k)[:16]
        if k.isalnum() and k not in out:
            out.append(k)
    return out


async def tb_send(p, obj):
    try:
        if not p["ws"].closed:
            await p["ws"].send_json(obj)
    except Exception:
        pass


async def tb_apply(win_p, lose_p):
    """Рейтинг и счётчики после живого боя (по рейтингу на момент подбора)."""
    try:
        d = elo(win_p["rating"], lose_p["rating"], True)
        rw = await db_get(win_p["uid"]) or {}
        rl = await db_get(lose_p["uid"]) or {}
        nw = max(0, rw.get("rating", 1000) + d)
        nl = max(0, rl.get("rating", 1000) - max(1, d // 2 + 1))
        await db_put(win_p["uid"], rating=nw, wins=rw.get("wins", 0) + 1)
        await db_put(lose_p["uid"], rating=nl, losses=rl.get("losses", 0) + 1)
        await tb_send(win_p, {"t": "res", "delta": nw - rw.get("rating", 1000), "rating": nw})
        await tb_send(lose_p, {"t": "res", "delta": nl - rl.get("rating", 1000), "rating": nl})
    except Exception as e:
        print("tb_apply", e)


async def tb_ws(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=15, max_msg_size=200000)
    await ws.prepare(request)
    me_p = None
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                d = json.loads(msg.data)
                t = d.get("t")
            except Exception:
                continue
            if t == "join" and me_p is None:
                u = user_info(d.get("initData", ""))
                if u is None:
                    await ws.send_json({"t": "err"})
                    break
                uid = int(u["id"])
                row = await db_get(uid) or {}
                if not row.get("squad"):
                    try:
                        await db_put(uid, name=(u.get("first_name") or "Игрок")[:20], squad=[{"c": "warrior", "l": 1, "hp": 100, "a": 10, "d": 0, "cd": 4, "sm": 1, "hs": -1, "ws": -1, "n": ""}])
                        row = await db_get(uid) or {}
                    except Exception as e:
                        print("tb squad", e)
                me_p = {"ws": ws, "uid": uid, "name": (u.get("first_name") or u.get("username") or "Игрок")[:20],
                        "deck": tb_clean(d.get("deck")), "lv": d.get("lv") if isinstance(d.get("lv"), dict) else {},
                        "tl": max(1, min(20, int(d.get("tl", 1) or 1))), "rating": row.get("rating", 1000), "room": None}
                tb_queue[:] = [q for q in tb_queue if q["uid"] != uid and not q["ws"].closed]
                if tb_queue:
                    best = min(tb_queue, key=lambda q: abs(q["rating"] - me_p["rating"]))
                    tb_queue.remove(best)
                    room = {"host": best, "guest": me_p, "done": False}
                    best["room"] = me_p["room"] = room
                    for p, role, o in ((best, "host", me_p), (me_p, "guest", best)):
                        p["role"] = role
                        await tb_send(p, {"t": "start", "role": role, "opp": {"name": o["name"], "deck": o["deck"], "lv": o["lv"], "tl": o["tl"], "rating": o["rating"]}})
                else:
                    tb_queue.append(me_p)
                    await ws.send_json({"t": "wait"})
            elif t == "leave":
                break
            elif me_p and me_p.get("room"):
                room = me_p["room"]
                other = room["guest"] if me_p is room["host"] else room["host"]
                if t == "snap" and me_p is room["host"]:
                    await other["ws"].send_str(msg.data)
                elif t == "cmd" and me_p is room["guest"]:
                    await other["ws"].send_str(msg.data)
                elif t == "end" and me_p is room["host"] and not room["done"]:
                    room["done"] = True
                    w = int(d.get("w", 0) or 0)
                    await tb_send(other, {"t": "end", "w": w})
                    if w > 0:
                        await tb_apply(room["host"], room["guest"])
                    elif w < 0:
                        await tb_apply(room["guest"], room["host"])
                    else:
                        await tb_send(room["host"], {"t": "res", "delta": 0, "rating": room["host"]["rating"]})
                        await tb_send(room["guest"], {"t": "res", "delta": 0, "rating": room["guest"]["rating"]})
    except Exception as e:
        print("tb_ws", e)
    finally:
        if me_p:
            tb_queue[:] = [q for q in tb_queue if q is not me_p]
            room = me_p.get("room")
            if room and not room["done"]:
                room["done"] = True
                other = room["guest"] if me_p is room["host"] else room["host"]
                await tb_send(other, {"t": "left"})
                await tb_apply(other, me_p)
        if not ws.closed:
            await ws.close()
    return ws


async def tb_state(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    uid = int(u["id"])
    row = await db_get(uid) or {}
    tops = await top_rating(10)
    return web.json_response({"rating": row.get("rating", 1000), "rank": await rank_rating(row) if row else 0, "w": row.get("wins", 0), "l": row.get("losses", 0),
                              "online": len(tb_queue), "top": [{"n": t["name"], "r": t.get("rating", 1000), "me": t["uid"] == uid} for t in tops]})


async def score(request: web.Request):
    try:
        body = await request.json()
        u = user_info(body.get("initData", ""))
        stage = int(body.get("stage", 0))
    except Exception:
        raise web.HTTPBadRequest()
    if u is None:
        raise web.HTTPForbidden()
    stage = max(0, min(stage, 10000))
    uid = int(u["id"])
    name = (u.get("first_name") or u.get("username") or "Герой")[:20]
    row = await touch(uid, name)
    if row is None or stage > row.get("best", 0):
        await db_put(uid, best=stage, best_t=time.time())
    return web.json_response({"ok": True})


async def top(request: web.Request):
    try:
        body = await request.json()
        uid = user_id(body.get("initData", ""))
    except Exception:
        raise web.HTTPBadRequest()
    if uid is None:
        raise web.HTTPForbidden()
    rk = await top_best(20)
    row = await db_get(uid)
    me = None
    if row and row.get("best", 0) > 0:
        me = {"rank": await rank_best(row), "s": row["best"], "n": row.get("name", "")}
    return web.json_response({"top": [{"n": r["name"], "s": r["best"], "me": r["uid"] == uid} for r in rk], "me": me, "total": await total_best()})


def play_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️ Играть", web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text="📖 Как играть", callback_data="help"),
         InlineKeyboardButton(text="🏆 Рейтинг", web_app=WebAppInfo(url=GAME_URL))],
    ])


WELCOME = (
    "🔥 <b>ПЕПЕЛ ЭЛЬДАРЫ</b> 🔥\n"
    "<i>Земли пали во тьму. Лишь герои способны вернуть свет.</i>\n\n"
    "{hi}, у тебя есть шанс стать легендой:\n\n"
    "⚔️ Собери отряд героев и разгроми чудовищ\n"
    "🐉 Победи мирового босса вместе с гильдией\n"
    "🐾 Вырасти питомцев, одень их в броню\n"
    "🏰 Сразись в «Битве башен» и на арене\n"
    "🗼 Покори Башню испытаний\n\n"
    "🎁 <b>Награда дня уже ждёт тебя!</b>"
)
HELP = (
    "📖 <b>Как играть</b>\n\n"
    "• Бои идут сами — жми ⚔️ для атаки и ✨ для умения\n"
    "• Из врагов падают предметы и золото — улучшай героев\n"
    "• 🐾 Питомцы: яйца в магазине, развитие и экспедиции\n"
    "• 🗼 Башня, ⚒ Кузница и 🏰 Гильдия — кнопки справа на сцене\n"
    "• Заходи каждый день: награда за серию дней растёт\n\n"
    "Я пришлю напоминание, когда тебя ждёт награда."
)
WELCOME_IMG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "welcome.jpg")
_welcome_fid = None
_bot_username = None


async def invite_link(uid):
    global _bot_username
    if not _bot_username:
        try:
            _bot_username = (await bot.get_me()).username
        except Exception:
            _bot_username = "my_game_agentbot"
    return "https://t.me/%s?startapp=ref_%s" % (_bot_username, uid)


async def menu_kb(uid):
    link = await invite_link(uid)
    share = "https://t.me/share/url?url=%s&text=%s" % (
        urllib.parse.quote(link, safe=""),
        urllib.parse.quote("🔥 Заходи в «Пепел Эльдары» — собери героев и победи дракона! Получишь бонус за вход ♦️", safe=""))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚔️  ИГРАТЬ  ⚔️", web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text="🎁 Награда дня", web_app=WebAppInfo(url=GAME_URL)),
         InlineKeyboardButton(text="🏰 Гильдия", web_app=WebAppInfo(url=GAME_URL))],
        [InlineKeyboardButton(text="🏆 Рейтинг", web_app=WebAppInfo(url=GAME_URL)),
         InlineKeyboardButton(text="📖 Как играть", callback_data="help")],
        [InlineKeyboardButton(text="👥 Позвать друга  +20 ♦️", url=share)],
    ])


@dp.message(CommandStart())
async def start(m: Message):
    global _welcome_fid
    try:
        await touch(m.from_user.id, (m.from_user.first_name or "Герой")[:20])
    except Exception:
        pass
    players.setdefault(str(m.from_user.id), {"last": time.time(), "reminded": 0})
    save()
    name = (m.from_user.first_name or "Герой")[:20].replace("<", "").replace(">", "").replace("&", "")
    text = WELCOME.format(hi="<b>%s</b>" % name)
    kb = await menu_kb(m.from_user.id)
    try:
        await bot.send_chat_action(m.chat.id, "upload_photo")
        if _welcome_fid:
            await m.answer_photo(_welcome_fid, caption=text, parse_mode="HTML", reply_markup=kb)
            return
        if os.path.exists(WELCOME_IMG):
            sent = await m.answer_photo(FSInputFile(WELCOME_IMG), caption=text, parse_mode="HTML", reply_markup=kb)
            if sent.photo:
                _welcome_fid = sent.photo[-1].file_id
            return
    except Exception as e:
        print("welcome photo:", e)
    await m.answer(text, parse_mode="HTML", reply_markup=kb)


@dp.message(Command("play"))
async def play_cmd(m: Message):
    await m.answer("⚔️ Герои ждут! Жми кнопку ниже:", reply_markup=play_kb())


@dp.message(Command("invite"))
async def invite_cmd(m: Message):
    link = await invite_link(m.from_user.id)
    await m.answer("👥 <b>Позови друга</b> — за каждого, кто зайдёт по твоей ссылке, получишь <b>+20 ♦️</b>, а друг — стартовый бонус.\n\n"
                   "Твоя ссылка:\n<code>%s</code>" % link, parse_mode="HTML", reply_markup=await menu_kb(m.from_user.id))


@dp.message(Command("help"))
async def help_cmd(m: Message):
    await m.answer(HELP, parse_mode="HTML", reply_markup=play_kb())


@dp.callback_query(F.data == "help")
async def help_cb(c: CallbackQuery):
    await c.answer()
    await c.message.answer(HELP, parse_mode="HTML", reply_markup=play_kb())


async def setup_profile():
    """Оформление бота: команды, описание, «о боте» (то же, что делается в BotFather)."""
    try:
        await bot.set_my_commands([BotCommand(command="start", description="🔥 Главное меню"),
                                   BotCommand(command="play", description="⚔️ Играть"),
                                   BotCommand(command="invite", description="👥 Позвать друга"),
                                   BotCommand(command="help", description="📖 Как играть")])
        await bot.set_my_description("🔥 ПЕПЕЛ ЭЛЬДАРЫ — тёмное фэнтези RPG.\n\n⚔️ Собери отряд героев\n🐉 Убей мирового босса с гильдией\n🐾 Выращивай питомцев\n🏰 Битвы башен и арена\n\nЖми «Старт» — и в бой!")
        await bot.set_my_short_description("🔥 Пепел Эльдары — тёмное фэнтези RPG")
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="⚔️ Играть", web_app=WebAppInfo(url=GAME_URL)))
    except Exception as e:
        print("setup_profile:", e)


async def notify(request: web.Request):
    try:
        body = await request.json()
        uid = user_id(body.get("initData", ""))
        kind = body.get("type")
        text = str(body.get("text", ""))[:300]
        ch = body.get("ch")
        ch = int(ch) if ch is not None and 0 <= int(ch) <= 6 else None
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
    p["seen"] = time.time()
    save()
    sent = False
    if ch is not None:
        # уведомление о сундуке — картинкой сундука своей категории
        base = os.path.dirname(os.path.abspath(__file__))
        path = next((p for p in (os.path.join(base, "ch%d.png" % ch), os.path.join(base, "chests", "ch%d.png" % ch)) if os.path.exists(p)), None)
        if path:
            try:
                await bot.send_photo(uid, FSInputFile(path), caption=text, reply_markup=play_kb())
                sent = True
            except Exception:
                pass
    if not sent:
        try:
            await bot.send_message(uid, text, reply_markup=play_kb())
        except Exception:
            pass  # игрок не нажимал /start у бота — писать ему пока нельзя
    return web.json_response({"ok": True})


# ---------- гильдии и мировой босс (данные лежат в таблице kv) ----------
KV_FILE = "kv.json"
kv_local: dict = {}
kv_lock = asyncio.Lock()
GUILD_MAX = 30
WB_MAX = 1_000_000
WB_TRIES = 3


def load_kv():
    global kv_local
    try:
        with open(KV_FILE, encoding="utf-8") as f:
            kv_local = json.load(f)
    except Exception:
        kv_local = {}


async def kv_get(k):
    if SB_URL:
        rows = await sb("GET", "kv", {"k": "eq." + k, "select": "v"})
        return rows[0]["v"] if rows else None
    return kv_local.get(k)


async def kv_put(k, v):
    if SB_URL:
        await sb("POST", "kv", {"on_conflict": "k"}, [{"k": k, "v": v}], prefer="resolution=merge-duplicates,return=minimal")
        return
    kv_local[k] = v
    try:
        with open(KV_FILE, "w", encoding="utf-8") as f:
            json.dump(kv_local, f, ensure_ascii=False)
    except Exception:
        pass


def api(fn):
    """Проверяет подпись Telegram и ловит ошибки базы (игра продолжает работать без этих функций)."""
    async def w(request):
        try:
            body = await request.json()
            u = user_info(body.get("initData", ""))
        except Exception:
            raise web.HTTPBadRequest()
        if u is None:
            raise web.HTTPForbidden()
        uid = int(u["id"])
        name = (u.get("first_name") or u.get("username") or "Герой")[:20]
        try:
            async with kv_lock:
                return web.json_response(await fn(uid, name, body))
        except (web.HTTPBadRequest, web.HTTPForbidden):
            raise
        except Exception as e:
            print("api error", fn.__name__, e)
            return web.json_response({"off": 1})
    return w


# ---------- магазин за Telegram Stars (XTR) ----------
# id: (цена в ⭐, название, описание)
SHOP = {
    "cr1": (25, "♦️ 50 кристаллов", "Небольшой запас кристаллов"),
    "cr2": (100, "♦️ 230 кристаллов", "Выгодный набор кристаллов"),
    "cr3": (400, "♦️ 1000 кристаллов", "Большой запас кристаллов"),
    "egg": (40, "🥚 3 яйца питомцев", "Три яйца для питомцев"),
    "pass": (150, "🎫 Пропуск героя, 30 дней", "+10% золота и 25 ♦️ каждый день в течение 30 дней"),
}
PASS_DAYS = 30
PASS_DAILY = 25


def shop_day():
    return time.strftime("%Y-%m-%d", time.gmtime())


async def shop_load(uid):
    d = await kv_get("shop:%d" % uid)
    d = d if isinstance(d, dict) else {}
    d.setdefault("pend", [])
    d.setdefault("seen", [])
    d.setdefault("pass", 0)
    d.setdefault("dc", "")
    return d


async def shop_credit(uid, item, charge_id, stars):
    """Записывает оплаченную покупку (один раз на платёж). True — записано."""
    if item not in SHOP or SHOP[item][0] != stars:
        return False
    async with kv_lock:
        d = await shop_load(uid)
        if charge_id in d["seen"]:
            return False
        d["seen"] = (d["seen"] + [charge_id])[-300:]
        if item == "pass":
            d["pass"] = max(d["pass"], time.time()) + PASS_DAYS * 86400
        else:
            d["pend"].append(item)
        await kv_put("shop:%d" % uid, d)
    return True


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        uid, item, _ = q.invoice_payload.split(":", 2)
        ok = int(uid) == q.from_user.id and item in SHOP and SHOP[item][0] == q.total_amount and q.currency == "XTR"
    except Exception:
        ok = False
    await q.answer(ok=ok, error_message=None if ok else "Покупка недоступна, попробуй ещё раз.")


@dp.message(F.successful_payment)
async def paid(m: Message):
    sp = m.successful_payment
    try:
        uid, item, _ = sp.invoice_payload.split(":", 2)
        if int(uid) != m.from_user.id:
            return
        ok = await shop_credit(int(uid), item, sp.telegram_payment_charge_id, sp.total_amount)
    except Exception as e:
        print("paid error", e)
        return
    if ok:
        await m.answer("✅ Спасибо за покупку: %s!\nОткрой игру — награда уже ждёт тебя." % SHOP[item][1], reply_markup=play_kb())


@api
async def shop_invoice(uid, name, body):
    item = str(body.get("item", ""))
    if item not in SHOP:
        raise web.HTTPBadRequest()
    price, title, desc = SHOP[item]
    url = await bot.create_invoice_link(title=title, description=desc, payload="%d:%s:%d" % (uid, item, int(time.time())),
                                        provider_token="", currency="XTR", prices=[LabeledPrice(label=title, amount=price)])
    return {"url": url}


def shop_out(d):
    act = d["pass"] > time.time()
    return {"pend": d["pend"], "pass": int(d["pass"]) if act else 0, "can": bool(act and d["dc"] != shop_day())}


@api
async def shop_state(uid, name, body):
    return shop_out(await shop_load(uid))


@api
async def shop_claim(uid, name, body):
    d = await shop_load(uid)
    items, d["pend"] = d["pend"], []
    daily = 0
    if body.get("daily") and d["pass"] > time.time() and d["dc"] != shop_day():
        d["dc"] = shop_day()
        daily = PASS_DAILY
    if items or daily:
        await kv_put("shop:%d" % uid, d)
    out = shop_out(d)
    out.update({"items": items, "daily": daily})
    return out


@api
async def arena_claim(uid, name, body):
    ss = await kv_get("arena:season")
    pv = ss.get("prev") if isinstance(ss, dict) else None
    if not pv:
        return {"ok": 0}
    rank = next((i + 1 for i, r in enumerate(pv["r"]) if r[0] == uid), 0)
    if not rank or str(uid) in pv["cl"]:
        return {"ok": 0}
    pv["cl"][str(uid)] = 1
    await kv_put("arena:season", ss)
    return {"ok": 1, "rank": rank, "cr": season_reward(rank)}


def g_perk(n):
    return 15 if n >= 10 else 10 if n >= 6 else 5 if n >= 3 else 0


async def guilds_load():
    gs = await kv_get("guilds")
    return gs if isinstance(gs, dict) and "g" in gs else {"n": 0, "g": {}}


def guild_find(gs, uid):
    for gid, g in gs["g"].items():
        if uid in g["m"]:
            return gid, g
    return None, None


async def players_rows(uids):
    if not uids:
        return []
    if SB_URL:
        return await sb("GET", "players", {"select": "uid,name,best", "uid": "in.(%s)" % ",".join(str(int(x)) for x in uids)})
    return [local_db[str(u)] for u in uids if str(u) in local_db]


async def guild_state(gs, uid):
    gid, g = guild_find(gs, uid)
    if g is None:
        top = sorted(gs["g"].items(), key=lambda kv: -kv[1].get("pts", 0))[:15]
        return {"g": None, "list": [{"id": k, "n": v["name"], "c": len(v["m"]), "p": v.get("pts", 0), "bk": v.get("bk", 0)} for k, v in top]}
    rows = {int(r["uid"]): r for r in await players_rows(g["m"])}
    mem = []
    for m in g["m"]:
        r = rows.get(m, {})
        mem.append({"n": r.get("name", "Герой"), "s": r.get("best", 0), "o": m == g["o"], "me": m == uid})
    mem.sort(key=lambda x: -x["s"])
    g["pts"] = sum(x["s"] for x in mem) + int(g.get("don", 0))
    await kv_put("guilds", gs)
    top = sorted(gs["g"].items(), key=lambda kv: (-kv[1].get("bk", 0), -kv[1].get("pts", 0)))[:10]
    lst = [{"id": k, "n": v["name"], "c": len(v["m"]), "p": v.get("pts", 0), "bk": v.get("bk", 0)} for k, v in top]
    return {"list": lst, "g": {"id": gid, "n": g["name"], "own": g["o"] == uid, "m": mem, "p": g["pts"], "bk": g.get("bk", 0), "perk": g_perk(len(g["m"])), "max": GUILD_MAX}}


@api
async def guild_me(uid, name, body):
    return await guild_state(await guilds_load(), uid)


@api
async def guild_create(uid, name, body):
    import re
    gs = await guilds_load()
    if guild_find(gs, uid)[1] is not None:
        return {"error": "inguild"}
    gname = re.sub(r"\s+", " ", str(body.get("name", "")).strip())
    if not re.fullmatch(r"[\w \-А-Яа-яЁё]{3,16}", gname):
        return {"error": "badname"}
    if any(g["name"].lower() == gname.lower() for g in gs["g"].values()):
        return {"error": "taken"}
    gs["n"] += 1
    gs["g"]["g%d" % gs["n"]] = {"name": gname, "o": uid, "m": [uid], "pts": 0}
    await kv_put("guilds", gs)
    out = await guild_state(gs, uid)
    out["ok"] = 1
    return out


@api
async def guild_join(uid, name, body):
    gs = await guilds_load()
    if guild_find(gs, uid)[1] is not None:
        return {"error": "inguild"}
    g = gs["g"].get(str(body.get("gid", "")))
    if g is None:
        return {"error": "nogd"}
    if len(g["m"]) >= GUILD_MAX:
        return {"error": "full"}
    g["m"].append(uid)
    await kv_put("guilds", gs)
    out = await guild_state(gs, uid)
    out["ok"] = 1
    return out


@api
async def guild_leave(uid, name, body):
    gs = await guilds_load()
    gid, g = guild_find(gs, uid)
    if g is None:
        return {"error": "noguild"}
    g["m"].remove(uid)
    if not g["m"]:
        del gs["g"][gid]
    elif g["o"] == uid:
        g["o"] = g["m"][0]
    await kv_put("guilds", gs)
    return await guild_state(gs, uid)


# ---------- чат, склад и задания гильдии ----------
QREW = {"ko": (10, 0), "bk": (15, 1), "dn": (10, 0)}  # награда каждому: кристаллы, золотой сундук


def gq_goal(g):
    n = max(3, len(g["m"]))
    return {"ko": 400 * n, "bk": 3 * n, "dn": 20000 * n}


def gq_get(g):
    wk = gb_week()
    q = g.get("q")
    if not isinstance(q, dict) or q.get("wk") != wk:
        q = {"wk": wk, "ko": 0, "bk": 0, "dn": 0, "cl": {}}
        g["q"] = q
    return q


def gq_out(g, uid):
    q = gq_get(g)
    return {"q": {"ko": q["ko"], "bk": q["bk"], "dn": q["dn"], "goal": gq_goal(g),
                  "cl": {k: 1 for k in QREW if str(uid) in q["cl"].get(k, [])}, "rew": {k: list(v) for k, v in QREW.items()}}}


async def g_mine(uid):
    gs = await guilds_load()
    gid, g = guild_find(gs, uid)
    return gs, gid, g


@api
async def guild_chat(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    return {"chat": g.get("chat", [])[-30:], "me": uid}


@api
async def guild_say(uid, name, body):
    import re
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    t = re.sub(r"\s+", " ", str(body.get("text", "")).strip())[:120]
    if not t:
        return {"error": "empty"}
    ch = g.setdefault("chat", [])
    now = int(time.time())
    if ch and ch[-1].get("u") == uid and now - ch[-1].get("ts", 0) < 2:
        return {"error": "fast"}
    ch.append({"u": uid, "n": name, "t": t, "ts": now})
    del ch[:-60]
    await kv_put("guilds", gs)
    return {"ok": 1, "chat": ch[-30:], "me": uid}


@api
async def guild_bank(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    dn = g.get("dn", {})
    rows = {int(r["uid"]): r for r in await players_rows([int(u) for u in dn])} if dn else {}
    top = sorted(dn.items(), key=lambda kv: -kv[1])[:5]
    return {"don": int(g.get("don", 0)), "my": int(dn.get(str(uid), 0)),
            "top": [{"n": rows.get(int(u), {}).get("name", "Герой"), "d": int(v)} for u, v in top]}


@api
async def guild_donate(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    try:
        gold = max(0, min(10_000_000, int(body.get("gold", 0))))
        cr = max(0, min(5000, int(body.get("cr", 0))))
    except (TypeError, ValueError):
        return {"error": "bad"}
    pts = gold // 1000 + cr * 10
    if pts < 1:
        return {"error": "small"}
    g["don"] = int(g.get("don", 0)) + pts
    dn = g.setdefault("dn", {})
    dn[str(uid)] = int(dn.get(str(uid), 0)) + pts
    gq_get(g)["dn"] += gold + cr * 1000
    await kv_put("guilds", gs)
    return {"ok": 1, "pts": pts, "don": g["don"], "my": dn[str(uid)]}


@api
async def guild_q(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    out = gq_out(g, uid)
    await kv_put("guilds", gs)
    return out


@api
async def guild_qrep(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    try:
        ko = max(0, min(2000, int(body.get("ko", 0))))
        bk = max(0, min(50, int(body.get("bk", 0))))
    except (TypeError, ValueError):
        return {"error": "bad"}
    q = gq_get(g)
    q["ko"] += ko
    q["bk"] += bk
    await kv_put("guilds", gs)
    return gq_out(g, uid)


@api
async def guild_qclaim(uid, name, body):
    gs, gid, g = await g_mine(uid)
    if g is None:
        return {"error": "noguild"}
    k = str(body.get("k", ""))
    if k not in QREW:
        return {"error": "bad"}
    q = gq_get(g)
    if q[k] < gq_goal(g)[k]:
        return {"error": "notyet"}
    done = q["cl"].setdefault(k, [])
    if str(uid) in done:
        return {"error": "claimed"}
    done.append(str(uid))
    await kv_put("guilds", gs)
    cr, cg = QREW[k]
    out = gq_out(g, uid)
    out.update({"ok": 1, "cr": cr, "cg": cg})
    return out



def wb_zi(day):
    return int(time.mktime(time.strptime(day, "%Y-%m-%d")) // 86400) % 18


async def wb_doc(day):
    d = await kv_get("wb:" + day)
    if not isinstance(d, dict):
        d = {"hp": WB_MAX, "max": WB_MAX, "p": {}}
    return d


def wb_rank(d, uid):
    items = sorted(d["p"].items(), key=lambda kv: -kv[1].get("d", 0))
    for i, (k, v) in enumerate(items):
        if k == str(uid):
            return i + 1
    return 0


async def wb_state(uid):
    day = day_key()
    d = await wb_doc(day)
    yd = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    y = await kv_get("wb:" + yd)
    me_ = d["p"].get(str(uid), {})
    top = sorted(d["p"].values(), key=lambda v: -v.get("d", 0))[:10]
    out = {"day": day, "zi": wb_zi(day), "hp": d["hp"], "max": d["max"], "att": WB_TRIES - me_.get("a", 0), "my": me_.get("d", 0),
           "top": [{"n": v.get("n", "Герой"), "d": v.get("d", 0)} for v in top], "rank": wb_rank(d, uid), "y": None}
    if isinstance(y, dict) and str(uid) in y["p"] and y["p"][str(uid)].get("d", 0) > 0:
        yp = y["p"][str(uid)]
        out["y"] = {"zi": wb_zi(yd), "d": yp["d"], "killed": y["hp"] <= 0, "rank": wb_rank(y, uid), "cl": bool(yp.get("cl"))}
    return out


@api
async def wb_state_api(uid, name, body):
    return await wb_state(uid)


@api
async def wb_start(uid, name, body):
    day = day_key()
    d = await wb_doc(day)
    if d["hp"] <= 0:
        return {"ok": 0, "dead": 1}
    p = d["p"].setdefault(str(uid), {"n": name, "d": 0, "a": 0, "pend": 0})
    p["n"] = name
    if p.get("a", 0) >= WB_TRIES:
        return {"ok": 0, "limit": 1}
    p["a"] = p.get("a", 0) + 1
    p["pend"] = 1
    await kv_put("wb:" + day, d)
    return {"ok": 1, "hp": d["hp"], "max": d["max"], "zi": wb_zi(day)}


@api
async def wb_hit(uid, name, body):
    day = day_key()
    d = await wb_doc(day)
    p = d["p"].get(str(uid))
    if not p or not p.get("pend"):
        return {"ok": 0}
    row = await db_get(uid)
    best = max(1, (row or {}).get("best", 1))
    cap = 600 * best + 30000
    try:
        dmg = int(body.get("dmg", 0))
    except (TypeError, ValueError):
        dmg = 0
    dmg = max(0, min(dmg, cap, d["hp"]))
    p["pend"] = 0
    p["d"] = p.get("d", 0) + dmg
    d["hp"] = max(0, d["hp"] - dmg)
    await kv_put("wb:" + day, d)
    out = await wb_state(uid)
    out["ok"] = 1
    out["dealt"] = dmg
    return out


@api
async def wb_claim(uid, name, body):
    yd = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    y = await kv_get("wb:" + yd)
    if not isinstance(y, dict):
        return {"ok": 0}
    p = y["p"].get(str(uid))
    if not p or p.get("d", 0) <= 0 or p.get("cl"):
        return {"ok": 0}
    p["cl"] = 1
    await kv_put("wb:" + yd, y)
    return {"ok": 1, "d": p["d"], "killed": y["hp"] <= 0, "rank": wb_rank(y, uid)}


# ---------- гильдейский босс ----------
GB_TRIES = 2


def gb_week(off=0):
    return int((time.time() - 4 * 86400) // 604800) + off


def gb_zi(gid, wk):
    try:
        return (wk * 5 + int(str(gid).lstrip("g"))) % 12
    except ValueError:
        return wk % 12


async def gb_doc(gid, g, wk):
    d = await kv_get("gb:%s:%d" % (gid, wk))
    if not isinstance(d, dict):
        mx = 150000 + 100000 * len(g["m"])
        d = {"hp": mx, "max": mx, "p": {}}
    return d


def gb_rew(dmg):
    return min(200, 40 + int(dmg) // 2000)


async def gb_state(uid, gs):
    gid, g = guild_find(gs, uid)
    if g is None:
        return {"g": 0}
    wk = gb_week()
    d = await gb_doc(gid, g, wk)
    me_ = d["p"].get(str(uid), {})
    used = me_.get("a", 0) if me_.get("ad") == day_key() else 0
    top = sorted(d["p"].values(), key=lambda v: -v.get("d", 0))[:10]
    out = {"g": 1, "name": g["name"], "zi": gb_zi(gid, wk), "hp": d["hp"], "max": d["max"], "att": GB_TRIES - used,
           "my": me_.get("d", 0), "top": [{"n": v.get("n", "Герой"), "d": v.get("d", 0)} for v in top], "bk": g.get("bk", 0),
           "left": int(604800 - (time.time() - 4 * 86400) % 604800), "y": None, "cl": bool(me_.get("cl"))}
    y = await gb_doc(gid, g, wk - 1)
    yp = y["p"].get(str(uid))
    if yp and yp.get("d", 0) > 0 and not yp.get("cl") and y["hp"] <= 0:
        out["y"] = {"d": yp["d"], "cr": gb_rew(yp["d"])}
    if out["hp"] <= 0 and me_.get("d", 0) > 0 and not me_.get("cl"):
        out["y"] = {"d": me_["d"], "cr": gb_rew(me_["d"])}
    return out


@api
async def gb_state_api(uid, name, body):
    return await gb_state(uid, await guilds_load())


@api
async def gb_start(uid, name, body):
    gs = await guilds_load()
    gid, g = guild_find(gs, uid)
    if g is None:
        return {"ok": 0, "noguild": 1}
    wk = gb_week()
    d = await gb_doc(gid, g, wk)
    if d["hp"] <= 0:
        return {"ok": 0, "dead": 1}
    p = d["p"].setdefault(str(uid), {"n": name, "d": 0, "a": 0})
    p["n"] = name
    if p.get("ad") != day_key():
        p["ad"] = day_key()
        p["a"] = 0
    if p["a"] >= GB_TRIES:
        return {"ok": 0, "limit": 1}
    p["a"] += 1
    p["pend"] = 1
    await kv_put("gb:%s:%d" % (gid, wk), d)
    return {"ok": 1, "hp": d["hp"], "max": d["max"], "zi": gb_zi(gid, wk)}


@api
async def gb_hit(uid, name, body):
    gs = await guilds_load()
    gid, g = guild_find(gs, uid)
    if g is None:
        return {"ok": 0}
    wk = gb_week()
    d = await gb_doc(gid, g, wk)
    p = d["p"].get(str(uid))
    if not p or not p.get("pend"):
        return {"ok": 0}
    row = await db_get(uid)
    best = max(1, (row or {}).get("best", 1))
    cap = 600 * best + 30000
    try:
        dmg = int(body.get("dmg", 0))
    except (TypeError, ValueError):
        dmg = 0
    dmg = max(0, min(dmg, cap, d["hp"]))
    p["pend"] = 0
    p["d"] = p.get("d", 0) + dmg
    was = d["hp"]
    d["hp"] = max(0, d["hp"] - dmg)
    await kv_put("gb:%s:%d" % (gid, wk), d)
    if was > 0 and d["hp"] <= 0:
        g["bk"] = g.get("bk", 0) + 1
        await kv_put("guilds", gs)
    out = await gb_state(uid, gs)
    out["ok"] = 1
    out["dealt"] = dmg
    return out


@api
async def gb_claim(uid, name, body):
    gs = await guilds_load()
    gid, g = guild_find(gs, uid)
    if g is None:
        return {"ok": 0}
    for off in (0, -1):
        wk = gb_week(off)
        d = await gb_doc(gid, g, wk)
        p = d["p"].get(str(uid))
        if d["hp"] <= 0 and p and p.get("d", 0) > 0 and not p.get("cl"):
            p["cl"] = 1
            await kv_put("gb:%s:%d" % (gid, wk), d)
            return {"ok": 1, "d": p["d"], "cr": gb_rew(p["d"]), "cg": 1}
    return {"ok": 0}


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
    """Раз в 10 минут напоминает о ежедневной награде (не чаще раза в сутки)."""
    while True:
        await asyncio.sleep(600)
        now = time.time()
        for uid, p in list(players.items()):
            if now - p.get("last", now) > REMIND_AFTER and now - p.get("reminded", 0) > 86400:
                p["reminded"] = now
                save()
                try:
                    await bot.send_message(int(uid), "🎁 Ежедневная награда ждёт тебя в «Пепле Эльдары»! "
                                           "Не прерывай серию дней.", reply_markup=play_kb())
                except Exception:
                    pass


async def main():
    global session
    load()
    load_local()
    load_kv()
    session = aiohttp.ClientSession()
    app = web.Application(middlewares=[cors])
    app.router.add_post("/notify", notify)
    app.router.add_post("/score", score)
    app.router.add_post("/top", top)
    app.router.add_post("/me", me)
    app.router.add_get("/stats", stats)
    app.router.add_get("/tb/ws", tb_ws)
    app.router.add_post("/tb/state", tb_state)
    app.router.add_post("/arena/state", arena_state)
    app.router.add_post("/arena/fight", arena_fight)
    app.router.add_post("/arena/result", arena_result)
    for p, h in (("/guild/me", guild_me), ("/guild/create", guild_create), ("/guild/join", guild_join), ("/guild/leave", guild_leave),
                 ("/guild/chat", guild_chat), ("/guild/say", guild_say), ("/guild/bank", guild_bank), ("/guild/donate", guild_donate),
                 ("/guild/q", guild_q), ("/guild/qrep", guild_qrep), ("/guild/qclaim", guild_qclaim),
                 ("/arena/claim", arena_claim), ("/shop/invoice", shop_invoice), ("/shop/state", shop_state), ("/shop/claim", shop_claim),
                 ("/gb/state", gb_state_api), ("/gb/start", gb_start), ("/gb/hit", gb_hit), ("/gb/claim", gb_claim),
                 ("/wb/state", wb_state_api), ("/wb/start", wb_start), ("/wb/hit", wb_hit), ("/wb/claim", wb_claim)):
        app.router.add_post(p, h)
        app.router.add_route("OPTIONS", p, health)
    for p in ("/notify", "/score", "/top", "/me", "/tb/state", "/arena/state", "/arena/fight", "/arena/result"):
        app.router.add_route("OPTIONS", p, health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(reminders())
    await setup_profile()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
