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
from urllib.parse import parse_qsl

from aiogram import Bot, Dispatcher
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo
import aiohttp
from aiohttp import web

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
                await bot.send_message(r, "👥 По твоей ссылке пришёл %s! +20 💎 ждут тебя в игре." % name, reply_markup=play_kb())
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
    return web.json_response({"me": {"rating": rating, "rank": await rank_rating(row), "left": max(0, ARENA_DAILY - used), "w": row.get("wins", 0), "l": row.get("losses", 0)},
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
    if used >= ARENA_DAILY:
        return web.json_response({"error": "limit"})
    if target < 0:
        opp = make_bot(row["squad"], -target - 1, row.get("rating", 1000))
    else:
        opp = await db_get(target)
        if not opp or not opp.get("squad") or target == uid:
            return web.json_response({"error": "gone"})
    await db_put(uid, day=day_key(), used=used + 1)
    pending[uid] = {"opp": opp["uid"], "name": opp["name"], "orating": opp.get("rating", 1000), "t": time.time()}
    return web.json_response({"opp": {"uid": opp["uid"], "name": opp["name"], "rating": opp.get("rating", 1000), "squad": opp["squad"]}, "left": ARENA_DAILY - used - 1})


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
            try:
                nm = (u.get("first_name") or "Игрок")[:20]
                txt = ("🛡️ %s атаковал твой отряд на арене и проиграл. Рейтинг: %+d" % (nm, od)) if not win else ("⚔️ %s атаковал твой отряд на арене и победил. Рейтинг: %+d" % (nm, od))
                await bot.send_message(p["opp"], txt, reply_markup=play_kb())
            except Exception:
                pass
    return web.json_response({"ok": True, "delta": d, "rating": new, "reward": 3 if win else 0})


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
    p["seen"] = time.time()
    save()
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
        return {"g": None, "list": [{"id": k, "n": v["name"], "c": len(v["m"]), "p": v.get("pts", 0)} for k, v in top]}
    rows = {int(r["uid"]): r for r in await players_rows(g["m"])}
    mem = []
    for m in g["m"]:
        r = rows.get(m, {})
        mem.append({"n": r.get("name", "Герой"), "s": r.get("best", 0), "o": m == g["o"], "me": m == uid})
    mem.sort(key=lambda x: -x["s"])
    g["pts"] = sum(x["s"] for x in mem)
    await kv_put("guilds", gs)
    return {"g": {"id": gid, "n": g["name"], "own": g["o"] == uid, "m": mem, "p": g["pts"], "perk": g_perk(len(g["m"])), "max": GUILD_MAX}}


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
    app.router.add_post("/arena/state", arena_state)
    app.router.add_post("/arena/fight", arena_fight)
    app.router.add_post("/arena/result", arena_result)
    for p, h in (("/guild/me", guild_me), ("/guild/create", guild_create), ("/guild/join", guild_join), ("/guild/leave", guild_leave),
                 ("/wb/state", wb_state_api), ("/wb/start", wb_start), ("/wb/hit", wb_hit), ("/wb/claim", wb_claim)):
        app.router.add_post(p, h)
        app.router.add_route("OPTIONS", p, health)
    for p in ("/notify", "/score", "/top", "/me", "/arena/state", "/arena/fight", "/arena/result"):
        app.router.add_route("OPTIONS", p, health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(reminders())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
