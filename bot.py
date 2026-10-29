"""
🎲 Рандомайзер — Telegram-бот
Розыгрыши (капча-пазл, номера, рулетка, спонсоры) · Посты · Автоответчик (Telegram Business)
aiogram 3.x · SQLite
"""
import asyncio
import csv
import io
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape as esc

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile, BusinessConnection, CallbackQuery, ChatAdministratorRights,
    InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    KeyboardButtonRequestChat, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("randomizer")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DB_PATH = os.getenv("DB_PATH", "randomizer.db")
TZ_HOURS = int(os.getenv("TZ_OFFSET", "3"))
TZ = timezone(timedelta(hours=TZ_HOURS))
TZL = f"UTC{TZ_HOURS:+d}"

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(
    parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
router = Router()
BOT_USERNAME = ""
BOT_ID = 0
db: aiosqlite.Connection

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS giveaways(
  id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER, chat_id INTEGER, chat_title TEXT,
  chat_link TEXT, title TEXT, descr TEXT, media_type TEXT, media_id TEXT, winners INTEGER,
  prizes TEXT, end_type TEXT, end_at INTEGER, end_subs INTEGER, sponsors TEXT,
  captcha INTEGER, numbered INTEGER, roulette INTEGER, status TEXT, post_msg_id INTEGER,
  created INTEGER);
CREATE TABLE IF NOT EXISTS participants(
  gid INTEGER, user_id INTEGER, name TEXT, username TEXT, num INTEGER, joined INTEGER,
  PRIMARY KEY(gid, user_id));
CREATE TABLE IF NOT EXISTS biz(id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER);
CREATE TABLE IF NOT EXISTS ar(
  user_id INTEGER PRIMARY KEY, enabled INTEGER, text TEXT, delay INTEGER, cool INTEGER, read INTEGER);
CREATE TABLE IF NOT EXISTS ar_log(owner INTEGER, chat_id INTEGER, ts INTEGER, PRIMARY KEY(owner, chat_id));
"""


async def init_db():
    global db
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.executescript(SCHEMA)
    await db.commit()


async def fetchone(sql, a=()):
    async with db.execute(sql, a) as c:
        return await c.fetchone()


async def fetchall(sql, a=()):
    async with db.execute(sql, a) as c:
        return await c.fetchall()


async def run(sql, a=()):
    c = await db.execute(sql, a)
    await db.commit()
    return c


def norm(r):
    g = dict(r)
    g["sponsors"] = json.loads(g["sponsors"] or "[]")
    return g


async def load_g(gid):
    r = await fetchone("SELECT * FROM giveaways WHERE id=?", (gid,))
    return norm(r) if r else None


# ───────────────────────── ДИЗАЙН / UI ─────────────────────────
LINE = "━━━━━━━━━━━━━━"
WELCOME = (
    "<b>🎲 Рандомайзер</b>\n"
    "<i>Честные розыгрыши · красивые посты · умный автоответчик</i>\n"
    f"{LINE}\n"
    "🎁 Розыгрыши с капчей, номерами и рулеткой\n"
    "📣 Посты с фото/GIF и кнопками\n"
    "🤖 Автоответчик в личных сообщениях\n"
    f"{LINE}\n"
    "Выберите действие 👇"
)


def ikb(*rows):
    out = []
    for row in rows:
        out.append([
            InlineKeyboardButton(text=t, url=c) if c.startswith("http")
            else InlineKeyboardButton(text=t, callback_data=c)
            for t, c in row
        ])
    return InlineKeyboardMarkup(inline_keyboard=out)


MENU = ikb(
    [("🎁 Создать розыгрыш", "g:new")],
    [("📣 Создать пост", "p:new"), ("🤖 Автоответчик", "ar:open")],
    [("📋 Мои розыгрыши", "g:list")],
)
CANCEL = ikb([("✖️ Отмена", "menu")])


def head(title, step=None, total=8):
    s = f"<b>{title}</b>\n"
    if step:
        s += f"<code>{'▰' * step}{'▱' * (total - step)}  {step}/{total}</code>\n"
    return s + LINE + "\n"


def fmt(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%d.%m.%Y %H:%M")


async def render(ev, state: FSMContext, text, kb=None):
    """Одна «живая» панель: редактируем одно сообщение, чтобы чат не засорялся."""
    if isinstance(ev, CallbackQuery):
        chat_id, mid = ev.message.chat.id, ev.message.message_id
        await state.update_data(panel=mid)
    else:
        chat_id = ev.chat.id
        mid = (await state.get_data()).get("panel")
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=mid, reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    m = await bot.send_message(chat_id, text, reply_markup=kb)
    await state.update_data(panel=m.message_id)


async def out(ev, text, kb=None):
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.edit_text(text, reply_markup=kb)
            return
        except TelegramBadRequest:
            pass
        await ev.message.answer(text, reply_markup=kb)
    else:
        await ev.answer(text, reply_markup=kb)


async def kill(m: Message):
    try:
        await m.delete()
    except Exception:
        pass


async def gd(state):
    return (await state.get_data()).get("d", {})


async def gset(state, **kw):
    d = await gd(state)
    d.update(kw)
    await state.update_data(d=d)
    return d


async def drop_kb(chat_id, state):
    k = (await state.get_data()).get("kbm")
    if k:
        try:
            await bot.delete_message(chat_id, k)
        except Exception:
            pass
        try:
            x = await bot.send_message(chat_id, "…", reply_markup=ReplyKeyboardRemove())
            await x.delete()
        except Exception:
            pass
        await state.update_data(kbm=None)


async def clear_pv(chat_id, state):
    for mid in (await state.get_data()).get("pv", []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass
    await state.update_data(pv=[])


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class G(StatesGroup):
    chat = State(); title = State(); media = State(); winners = State()
    prizes = State(); end_time = State(); end_subs = State(); sponsors = State()


class P(StatesGroup):
    chat = State(); media = State(); text = State(); buttons = State()


class A(StatesGroup):
    text = State(); delay = State(); cool = State()


# ───────────────────────── УТИЛИТЫ ─────────────────────────
async def is_member(chat, uid, default=False):
    try:
        m = await bot.get_chat_member(chat, uid)
    except Exception:
        return default
    return m.status in ("member", "administrator", "creator") or (
        m.status == "restricted" and getattr(m, "is_member", False))


async def send_any(chat_id, d, text, kb=None):
    """Отправка поста с фото/GIF/видео или без. Возвращает список сообщений (последнее — с кнопками)."""
    mt, fid = d.get("media_type"), d.get("media_id")
    if not mt:
        return [await bot.send_message(chat_id, text, reply_markup=kb)]
    send = {"photo": bot.send_photo, "animation": bot.send_animation, "video": bot.send_video}[mt]
    if len(text) <= 1024:
        return [await send(chat_id, fid, caption=text, reply_markup=kb)]
    first = await send(chat_id, fid)
    return [first, await bot.send_message(chat_id, text, reply_markup=kb)]


def post_text(g):
    t = f"🎁 <b>{esc(g['title'])}</b>\n"
    if g.get("descr"):
        t += f"\n{esc(g['descr'])}\n"
    t += f"\n{LINE}\n🏆 <b>Призы · победителей: {g['winners']}</b>\n{esc(g['prizes'])}\n{LINE}\n"
    if g["end_type"] == "time":
        t += f"⏰ Итоги: <b>{fmt(g['end_at'])}</b> ({TZL})\n"
    else:
        t += f"👥 Итоги при <b>{g['end_subs']}</b> подписчиках\n"
    if g["sponsors"]:
        t += "📌 Спонсоры: " + ", ".join("@" + s["u"] for s in g["sponsors"]) + "\n"
    tags = []
    if g["captcha"]:
        tags.append("🛡 капча")
    if g["numbered"]:
        tags.append("🔢 номера")
    if g["roulette"]:
        tags.append("🎰 рулетка")
    if tags:
        t += " · ".join(tags) + "\n"
    return t + "\n👇 Нажмите «Участвовать»"


def join_kb(g, n, gid=None):
    gid = gid or g.get("id")
    url = f"https://t.me/{BOT_USERNAME}?start=g{gid}" if gid else f"https://t.me/{BOT_USERNAME}"
    rows = [[(f"🎁 Участвовать · {n}", url)]]
    for s in g["sponsors"]:
        rows.append([(f"📌 {s['title'][:30]}", f"https://t.me/{s['u']}")])
    return ikb(*rows)


# ───────────────────────── МЕНЮ ─────────────────────────
@router.message(F.text == "✖️ Отмена")
async def msg_cancel(m: Message, state: FSMContext):
    await drop_kb(m.chat.id, state)
    await state.clear()
    await kill(m)
    await m.answer(WELCOME, reply_markup=MENU)


@router.message(CommandStart(deep_link=True, magic=F.args.regexp(r"^g\d+$")))
async def start_join(m: Message, command: CommandObject):
    await join_flow(m, int(command.args[1:]))


@router.message(CommandStart())
@router.message(Command("menu"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(WELCOME, reply_markup=MENU)


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await drop_kb(cb.message.chat.id, state)
    await clear_pv(cb.message.chat.id, state)
    await state.clear()
    await out(cb, WELCOME, MENU)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer("🏁 Розыгрыш завершён", show_alert=True)


# ───────────────────────── ВЫБОР ЧАТА (общий) ─────────────────────────
def _rights(post, inv):
    return ChatAdministratorRights(
        is_anonymous=False, can_manage_chat=True, can_delete_messages=False,
        can_manage_video_chats=False, can_restrict_members=False, can_promote_members=False,
        can_change_info=False, can_invite_users=inv,
        can_post_messages=True if post else None, can_edit_messages=True if post else None)


def pick_kb():
    ch = KeyboardButtonRequestChat(
        request_id=1, chat_is_channel=True, bot_is_member=True,
        user_administrator_rights=_rights(True, False), bot_administrator_rights=_rights(True, True))
    gr = KeyboardButtonRequestChat(
        request_id=2, chat_is_channel=False, bot_is_member=True,
        user_administrator_rights=_rights(False, False), bot_administrator_rights=_rights(False, True))
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📢 Выбрать канал", request_chat=ch),
                   KeyboardButton(text="👥 Выбрать группу", request_chat=gr)],
                  [KeyboardButton(text="✖️ Отмена")]],
        resize_keyboard=True, one_time_keyboard=True)


async def ask_chat(cb: CallbackQuery, state: FSMContext, kind):
    await state.clear()
    await state.update_data(kind=kind, d={})
    await state.set_state(G.chat if kind == "g" else P.chat)
    ac = f"https://t.me/{BOT_USERNAME}?startchannel=true&admin=post_messages+edit_messages+invite_users"
    ag = f"https://t.me/{BOT_USERNAME}?startgroup=true&admin=delete_messages+invite_users"
    title = "🎁 Новый розыгрыш" if kind == "g" else "📣 Новый пост"
    await render(cb, state,
                 head(title + " · Куда публикуем?", 1 if kind == "g" else None) +
                 "1️⃣ Добавьте бота в канал или группу\n"
                 "2️⃣ Права выдам я сам — вам останется подтвердить\n"
                 "3️⃣ Выберите чат кнопкой внизу 👇",
                 ikb([("➕ В канал", ac), ("➕ В группу", ag)], [("✖️ Отмена", "menu")]))
    k = await cb.message.answer("👇 Выберите чат", reply_markup=pick_kb())
    await state.update_data(kbm=k.message_id)


@router.message(F.chat_shared, StateFilter(G.chat, P.chat))
async def chat_shared(m: Message, state: FSMContext):
    cs = m.chat_shared
    try:
        chat = await bot.get_chat(cs.chat_id)
        me = await bot.get_chat_member(cs.chat_id, BOT_ID)
    except Exception:
        await m.answer("⚠️ Не вижу этот чат. Добавьте бота и выберите снова.")
        return
    if me.status != "administrator" or (chat.type == "channel" and not getattr(me, "can_post_messages", False)):
        await m.answer("⚠️ Бот должен быть администратором с правом публикации. Выберите чат ещё раз — права подтянутся.")
        return
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = await bot.export_chat_invite_link(cs.chat_id)
        except Exception:
            link = None
    await gset(state, chat_id=cs.chat_id, chat_title=chat.title or "Чат", chat_link=link,
               chat_user=chat.username)
    await drop_kb(m.chat.id, state)
    await kill(m)
    kind = (await state.get_data())["kind"]
    if kind == "g":
        await step_title(m, state)
    else:
        await step_pmedia(m, state)


# ───────────────────────── РОЗЫГРЫШ: МАСТЕР ─────────────────────────
@router.callback_query(F.data == "g:new")
async def g_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await ask_chat(cb, state, "g")


async def step_title(ev, state):
    await state.set_state(G.title)
    await render(ev, state, head("✍️ Название и описание", 2) +
                 "Отправьте <b>одним сообщением</b>:\n"
                 "• <b>1-я строка</b> — название\n• <b>дальше</b> — описание (по желанию)\n\n"
                 "<i>Пример:</i>\n<code>Розыгрыш iPhone 17\nПодпишись и выиграй!</code>", CANCEL)


@router.message(G.title, F.text)
async def g_title(m: Message, state: FSMContext):
    await kill(m)
    parts = m.text.strip().split("\n", 1)
    await gset(state, title=parts[0][:100], descr=(parts[1].strip() if len(parts) > 1 else "")[:500])
    await step_media(m, state)


async def step_media(ev, state):
    await state.set_state(G.media)
    await render(ev, state, head("🖼 Фото или GIF", 3) + "Отправьте картинку или GIF для поста.",
                 ikb([("⏭ Без медиа", "g:skipmedia")], [("✖️ Отмена", "menu")]))


@router.message(G.media, F.photo | F.animation | F.video)
async def g_media(m: Message, state: FSMContext):
    if m.animation:
        mt, fid = "animation", m.animation.file_id
    elif m.photo:
        mt, fid = "photo", m.photo[-1].file_id
    else:
        mt, fid = "video", m.video.file_id
    await kill(m)
    await gset(state, media_type=mt, media_id=fid)
    await step_winners(m, state)


@router.callback_query(F.data == "g:skipmedia")
async def g_skipmedia(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await step_winners(cb, state)


async def step_winners(ev, state):
    await state.set_state(G.winners)
    await render(ev, state, head("🏆 Сколько победителей?", 4) + "Выберите или отправьте число (1–100).",
                 ikb([(str(n), f"g:w:{n}") for n in (1, 2, 3, 5, 10)], [("✖️ Отмена", "menu")]))


@router.callback_query(F.data.startswith("g:w:"))
async def g_w_cb(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await gset(state, winners=int(cb.data[4:]))
    await step_prizes(cb, state)


@router.message(G.winners, F.text)
async def g_w_msg(m: Message, state: FSMContext):
    await kill(m)
    if not m.text.strip().isdigit() or not 1 <= int(m.text) <= 100:
        return await render(m, state, head("🏆 Сколько победителей?", 4) + "⚠️ Нужно число от 1 до 100.", CANCEL)
    await gset(state, winners=int(m.text))
    await step_prizes(m, state)


async def step_prizes(ev, state, warn=""):
    await state.set_state(G.prizes)
    await render(ev, state, head("🎁 Призы", 5) + warn +
                 "Напишите список призов, до <b>500 символов</b>:\n\n"
                 "<code>1 iPhone 17\n2. iPad\n3. Стиральная машина</code>", CANCEL)


@router.message(G.prizes, F.text)
async def g_prizes(m: Message, state: FSMContext):
    await kill(m)
    t = m.text.strip()
    if len(t) > 500:
        return await step_prizes(m, state, f"⚠️ Слишком длинно: {len(t)}/500\n\n")
    await gset(state, prizes=t)
    await step_end(m, state)


async def step_end(ev, state):
    await state.set_state(None)
    await render(ev, state, head("⏰ Когда подводим итоги?", 6) + "Выберите условие завершения:",
                 ikb([("📅 По дате и времени", "g:e:time")],
                     [("👥 По числу подписчиков", "g:e:subs")], [("✖️ Отмена", "menu")]))


@router.callback_query(F.data == "g:e:time")
async def g_e_time(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(G.end_time)
    await render(cb, state, head("📅 Дата завершения", 6) +
                 f"Отправьте в формате <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> ({TZL})\n"
                 f"Например: <code>{fmt(int(time.time()) + 86400 * 3)}</code>\n\nили выберите быстро 👇",
                 ikb([("1 час", "g:q:3600"), ("24 часа", "g:q:86400")],
                     [("3 дня", "g:q:259200"), ("7 дней", "g:q:604800")], [("✖️ Отмена", "menu")]))


@router.callback_query(F.data.startswith("g:q:"))
async def g_quick(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await gset(state, end_type="time", end_at=int(time.time()) + int(cb.data[4:]), end_subs=0)
    await step_sponsors(cb, state)


@router.message(G.end_time, F.text)
async def g_end_time(m: Message, state: FSMContext):
    await kill(m)
    try:
        ts = int(datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=TZ).timestamp())
        assert ts > time.time() + 60
    except Exception:
        return await render(m, state, head("📅 Дата завершения", 6) +
                            f"⚠️ Не понял дату или она в прошлом.\nФормат: <code>ДД.ММ.ГГГГ ЧЧ:ММ</code> ({TZL})", CANCEL)
    await gset(state, end_type="time", end_at=ts, end_subs=0)
    await step_sponsors(m, state)


@router.callback_query(F.data == "g:e:subs")
async def g_e_subs(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await gd(state)
    try:
        cur = await bot.get_chat_member_count(d["chat_id"])
    except Exception:
        cur = 0
    await gset(state, cur_subs=cur)
    await state.set_state(G.end_subs)
    await render(cb, state, head("👥 Цель по подписчикам", 6) +
                 f"Сейчас в чате: <b>{cur}</b>\nОтправьте число, при достижении которого подведём итоги.", CANCEL)


@router.message(G.end_subs, F.text)
async def g_end_subs(m: Message, state: FSMContext):
    await kill(m)
    d = await gd(state)
    if not m.text.strip().isdigit() or int(m.text) <= d.get("cur_subs", 0):
        return await render(m, state, head("👥 Цель по подписчикам", 6) +
                            f"⚠️ Число должно быть больше текущего ({d.get('cur_subs', 0)}).", CANCEL)
    await gset(state, end_type="subs", end_subs=int(m.text), end_at=0)
    await step_sponsors(m, state)


async def step_sponsors(ev, state, note=""):
    await state.set_state(G.sponsors)
    d = await gd(state)
    sp = d.get("sponsors", [])
    cur = "\n".join(f"• @{s['u']}" for s in sp) or "<i>пока никого</i>"
    await render(ev, state, head("📌 Спонсоры", 7) + note +
                 "Отправьте @username каналов-спонсоров (до 5). Бот должен быть <b>админом</b> в каждом — "
                 "иначе нельзя проверить подписку.\n\n"
                 f"<b>Добавлены:</b>\n{cur}",
                 ikb([("➡️ Далее", "g:sp:done"), ("🧹 Очистить", "g:sp:clear")], [("✖️ Отмена", "menu")]))


@router.message(G.sponsors, F.text)
async def g_sponsors(m: Message, state: FSMContext):
    await kill(m)
    d = await gd(state)
    sp = d.get("sponsors", [])
    notes = []
    for u in re.findall(r"(?:@|t\.me/)([A-Za-z0-9_]{4,})", m.text):
        if len(sp) >= 5 or any(s["u"].lower() == u.lower() for s in sp):
            continue
        try:
            chat = await bot.get_chat("@" + u)
            me = await bot.get_chat_member(chat.id, BOT_ID)
            if me.status != "administrator":
                notes.append(f"⚠️ @{u}: бот не админ")
                continue
            sp.append({"id": chat.id, "title": chat.title or u, "u": chat.username or u})
        except Exception:
            notes.append(f"⚠️ @{u}: не найден")
    await gset(state, sponsors=sp)
    await step_sponsors(m, state, ("\n".join(notes) + "\n\n") if notes else "")


@router.callback_query(F.data == "g:sp:clear")
async def g_sp_clear(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await gset(state, sponsors=[])
    await step_sponsors(cb, state)


@router.callback_query(F.data == "g:sp:done")
async def g_sp_done(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await gd(state)
    d.setdefault("sponsors", [])
    d.setdefault("captcha", 1); d.setdefault("numbered", 0); d.setdefault("roulette", 0)
    await state.update_data(d=d)
    await step_settings(cb, state)


async def step_settings(ev, state):
    await state.set_state(None)
    d = await gd(state)
    on = lambda k: "✅ вкл" if d.get(k) else "▫️ выкл"
    await render(ev, state, head("⚙️ Финальные настройки", 8) +
                 "🛡 <b>Капча</b> — участник собирает пазл\n"
                 "🔢 <b>Номера</b> — каждому #1, #2… В итогах только номера\n"
                 "🎰 <b>Рулетка</b> — анимация при выборе победителей",
                 ikb([(f"🛡 Капча-пазл: {on('captcha')}", "g:s:captcha")],
                     [(f"🔢 Номера: {on('numbered')}", "g:s:numbered")],
                     [(f"🎰 Рулетка: {on('roulette')}", "g:s:roulette")],
                     [("👁 Предпросмотр", "g:preview")], [("✖️ Отмена", "menu")]))


@router.callback_query(F.data.startswith("g:s:"))
async def g_toggle(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    k = cb.data[4:]
    d = await gd(state)
    await gset(state, **{k: 0 if d.get(k) else 1})
    await step_settings(cb, state)


@router.callback_query(F.data == "g:preview")
async def g_preview(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    d = await gd(state)
    chat_id = cb.message.chat.id
    await clear_pv(chat_id, state)
    try:
        await cb.message.delete()
    except Exception:
        pass
    msgs = await send_any(chat_id, d, post_text(d), join_kb(d, 0))
    ctl = await bot.send_message(
        chat_id, f"<b>👁 Так выглядит пост</b>\nПубликуем в «{esc(d['chat_title'])}»?",
        reply_markup=ikb([("🚀 Опубликовать", "g:pub")], [("⬅️ Настройки", "g:back")]))
    await state.update_data(pv=[x.message_id for x in msgs], panel=ctl.message_id)


@router.callback_query(F.data == "g:back")
async def g_back(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await clear_pv(cb.message.chat.id, state)
    await step_settings(cb, state)


@router.callback_query(F.data == "g:pub")
async def g_pub(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Публикую…")
    d = await gd(state)
    chat_id = cb.message.chat.id
    cur = await run(
        "INSERT INTO giveaways(owner,chat_id,chat_title,chat_link,title,descr,media_type,media_id,winners,"
        "prizes,end_type,end_at,end_subs,sponsors,captcha,numbered,roulette,status,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
        (cb.from_user.id, d["chat_id"], d["chat_title"], d.get("chat_link"), d["title"], d.get("descr", ""),
         d.get("media_type"), d.get("media_id"), d["winners"], d["prizes"], d["end_type"], d["end_at"],
         d["end_subs"], json.dumps(d["sponsors"]), d["captcha"], d["numbered"], d["roulette"], int(time.time())))
    gid = cur.lastrowid
    try:
        msgs = await send_any(d["chat_id"], d, post_text(d), join_kb(d, 0, gid))
    except Exception as e:
        await run("DELETE FROM giveaways WHERE id=?", (gid,))
        return await bot.send_message(chat_id, f"❌ Не получилось опубликовать: <code>{esc(str(e))}</code>")
    await run("UPDATE giveaways SET post_msg_id=? WHERE id=?", (msgs[-1].message_id, gid))
    await clear_pv(chat_id, state)
    link = f"\n🔗 https://t.me/{d['chat_user']}/{msgs[-1].message_id}" if d.get("chat_user") else ""
    await render(cb, state, f"<b>✅ Розыгрыш #{gid} опубликован!</b>\n{LINE}\n"
                 f"Итоги бот опубликует сам, а победителей я пришлю вам в личку.{link}", MENU)
    await state.clear()


# ───────────────────────── УЧАСТИЕ ─────────────────────────
CAPT = {}
EM = {0: "⬜", 1: "1️⃣", 2: "2️⃣", 3: "3️⃣", 4: "4️⃣", 5: "5️⃣", 6: "6️⃣", 7: "7️⃣", 8: "8️⃣"}
SOLVED = [1, 2, 3, 4, 5, 6, 7, 8, 0]


def new_board():
    while True:
        b, prev = SOLVED[:], None
        for _ in range(random.randint(12, 18)):
            z = b.index(0)
            r, c = divmod(z, 3)
            opts = [(r + dr) * 3 + c + dc for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))
                    if 0 <= r + dr < 3 and 0 <= c + dc < 3]
            opts = [o for o in opts if o != prev] or opts
            n = random.choice(opts)
            b[z], b[n] = b[n], b[z]
            prev = z
        if b != SOLVED:
            return b


def capt_kb(gid, b):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=EM[b[r * 3 + c]], callback_data=f"cp:{gid}:{r * 3 + c}") for c in range(3)]
        for r in range(3)])


CAPT_TEXT = ("<b>🧩 Капча · соберите пазл</b>\n" + LINE +
             "\nРасставьте цифры по порядку <b>1 → 8</b>, пустая клетка — в конце.\n"
             "Нажимайте на плитку рядом с пустой клеткой 👇")


async def join_flow(ev, gid):
    u = ev.from_user
    g = await load_g(gid)
    if not g or g["status"] != "active":
        return await out(ev, "😔 Розыгрыш не найден или уже завершён.")
    row = await fetchone("SELECT num FROM participants WHERE gid=? AND user_id=?", (gid, u.id))
    if row:
        n = f"\n🔢 Ваш номер: <b>#{row['num']}</b>" if g["numbered"] else ""
        return await out(ev, f"✅ <b>Вы уже участвуете!</b>{n}\n\nЖдите итоги в «{esc(g['chat_title'])}» 🍀")
    chats = [{"id": g["chat_id"], "title": g["chat_title"], "link": g["chat_link"]}] + [
        {"id": s["id"], "title": s["title"], "link": f"https://t.me/{s['u']}"} for s in g["sponsors"]]
    missing = [c for c in chats if not await is_member(c["id"], u.id)]
    if missing:
        rows = [[(f"📢 {c['title'][:32]}", c["link"])] for c in missing if c["link"]]
        rows.append([("✅ Я подписался — проверить", f"j:{gid}")])
        return await out(ev, f"<b>🎁 {esc(g['title'])}</b>\n{LINE}\nЧтобы участвовать, подпишитесь:",
                         ikb(*rows))
    if g["captcha"]:
        CAPT[(u.id, gid)] = new_board()
        return await out(ev, CAPT_TEXT, capt_kb(gid, CAPT[(u.id, gid)]))
    await register(ev, g)


@router.callback_query(F.data.startswith("j:"))
async def j_check(cb: CallbackQuery):
    await cb.answer()
    await join_flow(cb, int(cb.data[2:]))


@router.callback_query(F.data.startswith("cp:"))
async def captcha_tap(cb: CallbackQuery):
    _, gid, idx = cb.data.split(":")
    gid, idx = int(gid), int(idx)
    key = (cb.from_user.id, gid)
    b = CAPT.get(key)
    if not b:
        await cb.answer()
        return await join_flow(cb, gid)
    z = b.index(0)
    if abs(z // 3 - idx // 3) + abs(z % 3 - idx % 3) != 1:
        return await cb.answer("Нажмите на плитку рядом с пустой клеткой")
    b[z], b[idx] = b[idx], b[z]
    if b == SOLVED:
        CAPT.pop(key, None)
        await cb.answer("🎉 Пазл собран!")
        g = await load_g(gid)
        if not g or g["status"] != "active":
            return await out(cb, "😔 Розыгрыш уже завершён.")
        return await register(cb, g)
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=capt_kb(gid, b))
    except TelegramBadRequest:
        pass


async def register(ev, g):
    u = ev.from_user
    await run(
        "INSERT OR IGNORE INTO participants(gid,user_id,name,username,num,joined) VALUES(?,?,?,?,"
        "(SELECT COALESCE(MAX(num),0)+1 FROM participants WHERE gid=?),?)",
        (g["id"], u.id, u.full_name[:64], u.username, g["id"], int(time.time())))
    row = await fetchone("SELECT num FROM participants WHERE gid=? AND user_id=?", (g["id"], u.id))
    n = f"\n🔢 Ваш номер: <b>#{row['num']}</b>" if g["numbered"] else ""
    await out(ev, f"<b>✅ Вы в игре!</b>\n{LINE}\n🎁 {esc(g['title'])}{n}\n\nИтоги — в «{esc(g['chat_title'])}». Удачи 🍀")


# ───────────────────────── ЗАВЕРШЕНИЕ ─────────────────────────
def label(p, g):
    if g["numbered"]:
        return f"<b>#{p['num']}</b>"
    return esc(p["name"]) + (f" (@{p['username']})" if p["username"] else "")


MEDALS = ["🥇", "🥈", "🥉"]


async def csv_to(owner, gid):
    rows = await fetchall("SELECT num,user_id,name,username FROM participants WHERE gid=? ORDER BY num", (gid,))
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["number", "user_id", "name", "username"])
    for r in rows:
        w.writerow([r["num"], r["user_id"], r["name"], r["username"] or ""])
    await bot.send_document(owner, BufferedInputFile(s.getvalue().encode("utf-8-sig"), f"giveaway_{gid}.csv"),
                            caption=f"📄 Участники розыгрыша #{gid}")


async def still_ok(g, uid):
    for cid in [g["chat_id"]] + [s["id"] for s in g["sponsors"]]:
        if not await is_member(cid, uid, default=True):
            return False
    return True


async def finish(gid):
    c = await run("UPDATE giveaways SET status='finished' WHERE id=? AND status='active'", (gid,))
    if c.rowcount == 0:
        return
    g = await load_g(gid)
    pool = [dict(p) for p in await fetchall("SELECT * FROM participants WHERE gid=?", (gid,))]
    random.SystemRandom().shuffle(pool)
    winners = []
    for p in pool:
        if len(winners) >= g["winners"]:
            break
        if await still_ok(g, p["user_id"]):
            winners.append(p)

    if g["roulette"] and pool:
        try:
            rm = await bot.send_message(g["chat_id"], "🎰 <b>Запускаем рулетку…</b>")
            for _ in range(5):
                await asyncio.sleep(1.4)
                try:
                    await rm.edit_text(f"🎰 <b>Рулетка крутится…</b>\n\n▶️ {label(random.choice(pool), g)}")
                except Exception:
                    pass
            await asyncio.sleep(1.4)
            await rm.delete()
        except Exception:
            pass

    prizes = [re.sub(r"^\s*\d+\s*[.)\-:]?\s*", "", l).strip() for l in g["prizes"].splitlines() if l.strip()]
    t = (f"🏁 <b>Итоги розыгрыша</b>\n🎁 <b>{esc(g['title'])}</b>\n{LINE}\n"
         f"👥 Участников: <b>{len(pool)}</b>\n\n")
    if winners:
        t += "🏆 <b>Победители</b>\n"
        mapped = len(prizes) == g["winners"]
        for i, w in enumerate(winners):
            pz = f" — {esc(prizes[i])}" if mapped and i < len(prizes) else ""
            t += f"{MEDALS[i] if i < 3 else '▫️'} {label(w, g)}{pz}\n"
        if not mapped:
            t += f"\n🎁 <b>Призы:</b>\n{esc(g['prizes'])}\n"
        t += "\n📩 Администратор свяжется с победителями."
    else:
        t += "😔 Подходящих участников не нашлось."
    try:
        await bot.send_message(g["chat_id"], t, reply_to_message_id=g["post_msg_id"],
                               allow_sending_without_reply=True)
    except Exception:
        log.exception("result post failed")
    try:
        await bot.edit_message_reply_markup(chat_id=g["chat_id"], message_id=g["post_msg_id"],
                                            reply_markup=ikb([("🏁 Розыгрыш завершён", "noop")]))
    except Exception:
        pass

    # админу — контакты победителей
    a = f"🏁 <b>Розыгрыш «{esc(g['title'])}» завершён</b>\n{LINE}\n"
    if winners:
        a += "Напишите каждому победителю:\n\n"
        for i, w in enumerate(winners, 1):
            pz = f"\n    🎁 {esc(prizes[i - 1])}" if len(prizes) == g["winners"] else ""
            a += (f"{i}. <a href=\"tg://user?id={w['user_id']}\">{esc(w['name'])}</a>"
                  f"{' @' + w['username'] if w['username'] else ''} · #{w['num']}{pz}\n")
    else:
        a += "Победителей нет."
    try:
        await bot.send_message(g["owner"], a)
        await csv_to(g["owner"], gid)
    except Exception:
        pass


LASTCNT = {}


async def scheduler():
    while True:
        try:
            for r in await fetchall("SELECT * FROM giveaways WHERE status='active'"):
                g = norm(r)
                if g["end_type"] == "time" and time.time() >= g["end_at"]:
                    asyncio.create_task(finish(g["id"]))
                    continue
                if g["end_type"] == "subs":
                    try:
                        n = await bot.get_chat_member_count(g["chat_id"])
                    except Exception:
                        n = 0
                    if n >= g["end_subs"]:
                        asyncio.create_task(finish(g["id"]))
                        continue
                cnt = (await fetchone("SELECT COUNT(*) c FROM participants WHERE gid=?", (g["id"],)))["c"]
                if LASTCNT.get(g["id"], 0) != cnt and g["post_msg_id"]:
                    LASTCNT[g["id"]] = cnt
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=g["chat_id"], message_id=g["post_msg_id"], reply_markup=join_kb(g, cnt))
                    except Exception:
                        pass
        except Exception:
            log.exception("scheduler")
        await asyncio.sleep(20)


# ───────────────────────── МОИ РОЗЫГРЫШИ ─────────────────────────
@router.callback_query(F.data == "g:list")
async def g_list(cb: CallbackQuery):
    await cb.answer()
    rows = await fetchall("SELECT id,title,status FROM giveaways WHERE owner=? ORDER BY id DESC LIMIT 10",
                          (cb.from_user.id,))
    if not rows:
        return await out(cb, f"<b>📋 Мои розыгрыши</b>\n{LINE}\nПока пусто. Создайте первый 🎁", MENU)
    kb = [[(f"{'🟢' if r['status'] == 'active' else '🏁'} #{r['id']} · {r['title'][:24]}", f"g:v:{r['id']}")]
          for r in rows] + [[("⬅️ Меню", "menu")]]
    await out(cb, f"<b>📋 Мои розыгрыши</b>\n{LINE}\nПоследние 10:", ikb(*kb))


async def own(cb, gid):
    g = await load_g(gid)
    if not g or g["owner"] != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return None
    return g


@router.callback_query(F.data.startswith("g:v:"))
async def g_view(cb: CallbackQuery):
    await cb.answer()
    g = await own(cb, int(cb.data[4:]))
    if not g:
        return
    cnt = (await fetchone("SELECT COUNT(*) c FROM participants WHERE gid=?", (g["id"],)))["c"]
    end = f"⏰ {fmt(g['end_at'])}" if g["end_type"] == "time" else f"👥 до {g['end_subs']} подписчиков"
    rows = []
    if g["status"] == "active":
        rows.append([("🏁 Завершить сейчас", f"g:f:{g['id']}")])
    rows += [[("📄 Участники (CSV)", f"g:c:{g['id']}")], [("⬅️ Назад", "g:list")]]
    await out(cb, f"<b>🎁 {esc(g['title'])}</b>\n{LINE}\n"
                  f"Статус: {'🟢 идёт' if g['status'] == 'active' else '🏁 завершён'}\n"
                  f"Чат: {esc(g['chat_title'])}\nУчастников: <b>{cnt}</b>\nПобедителей: {g['winners']}\n{end}",
              ikb(*rows))


@router.callback_query(F.data.startswith("g:f:"))
async def g_force(cb: CallbackQuery):
    g = await own(cb, int(cb.data[4:]))
    if g:
        await cb.answer("Завершаю…")
        asyncio.create_task(finish(g["id"]))


@router.callback_query(F.data.startswith("g:c:"))
async def g_csv(cb: CallbackQuery):
    g = await own(cb, int(cb.data[4:]))
    if g:
        await cb.answer()
        await csv_to(cb.from_user.id, g["id"])


# ───────────────────────── ПОСТЫ ─────────────────────────
@router.callback_query(F.data == "p:new")
async def p_new(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await ask_chat(cb, state, "p")


async def step_pmedia(ev, state):
    await state.set_state(P.media)
    await render(ev, state, head("🖼 Фото или GIF") + "Отправьте картинку или GIF для поста.",
                 ikb([("⏭ Без медиа", "p:skip")], [("✖️ Отмена", "menu")]))


@router.message(P.media, F.photo | F.animation | F.video)
async def p_media(m: Message, state: FSMContext):
    if m.animation:
        mt, fid = "animation", m.animation.file_id
    elif m.photo:
        mt, fid = "photo", m.photo[-1].file_id
    else:
        mt, fid = "video", m.video.file_id
    await kill(m)
    await gset(state, media_type=mt, media_id=fid)
    await step_ptext(m, state)


@router.callback_query(F.data == "p:skip")
async def p_skip(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await step_ptext(cb, state)


async def step_ptext(ev, state, warn=""):
    await state.set_state(P.text)
    d = await gd(state)
    lim = 1024 if d.get("media_type") else 3800
    await render(ev, state, head("✍️ Название и описание") + warn +
                 f"Отправьте текст поста (до {lim} симв.). Жирный, курсив и ссылки сохранятся.", CANCEL)


@router.message(P.text, F.text)
async def p_text(m: Message, state: FSMContext):
    await kill(m)
    d = await gd(state)
    lim = 1024 if d.get("media_type") else 3800
    if len(m.text) > lim:
        return await step_ptext(m, state, f"⚠️ Слишком длинно: {len(m.text)}/{lim}\n\n")
    await gset(state, text=m.html_text)
    await step_pbtn(m, state)


async def step_pbtn(ev, state, warn=""):
    await state.set_state(P.buttons)
    await render(ev, state, head("📌 Подписка / Спонсор") + warn +
                 "Добавьте кнопки — <b>по одной в строке</b>:\n"
                 "<code>Наш канал - @channel\nСпонсор - https://t.me/sponsor</code>",
                 ikb([("⏭ Без кнопок", "p:nobtn")], [("✖️ Отмена", "menu")]))


def parse_btns(text):
    res = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = re.split(r"\s+[-—–]\s+", line.strip(), maxsplit=1)
        if len(parts) != 2:
            return None
        name, link = parts[0].strip(), parts[1].strip()
        if link.startswith("@"):
            link = "https://t.me/" + link[1:]
        elif link.startswith("t.me/"):
            link = "https://" + link
        if not link.startswith("http"):
            return None
        res.append((name[:40], link))
    return res[:8]


@router.message(P.buttons, F.text)
async def p_btn(m: Message, state: FSMContext):
    await kill(m)
    b = parse_btns(m.text)
    if not b:
        return await step_pbtn(m, state, "⚠️ Формат: <code>Название - ссылка</code>\n\n")
    await gset(state, btns=b)
    await p_preview(m, state)


@router.callback_query(F.data == "p:nobtn")
async def p_nobtn(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await gset(state, btns=[])
    await p_preview(cb, state)


def pkb(d):
    return ikb(*[[(t, u)] for t, u in d.get("btns", [])]) if d.get("btns") else None


async def p_preview(ev, state):
    d = await gd(state)
    chat_id = ev.chat.id if isinstance(ev, Message) else ev.message.chat.id
    await clear_pv(chat_id, state)
    if isinstance(ev, CallbackQuery):
        try:
            await ev.message.delete()
        except Exception:
            pass
    else:
        mid = (await state.get_data()).get("panel")
        if mid:
            try:
                await bot.delete_message(chat_id, mid)
            except Exception:
                pass
    msgs = await send_any(chat_id, d, d["text"], pkb(d))
    ctl = await bot.send_message(chat_id, f"<b>👁 Предпросмотр</b>\nПубликуем в «{esc(d['chat_title'])}»?",
                                 reply_markup=ikb([("🚀 Опубликовать", "p:pub")], [("✖️ Отмена", "menu")]))
    await state.update_data(pv=[x.message_id for x in msgs], panel=ctl.message_id)
    await state.set_state(None)


@router.callback_query(F.data == "p:pub")
async def p_pub(cb: CallbackQuery, state: FSMContext):
    await cb.answer("Публикую…")
    d = await gd(state)
    try:
        await send_any(d["chat_id"], d, d["text"], pkb(d))
    except Exception as e:
        return await cb.message.answer(f"❌ Ошибка: <code>{esc(str(e))}</code>")
    await clear_pv(cb.message.chat.id, state)
    await render(cb, state, f"<b>✅ Пост опубликован</b>\n{LINE}\nЧат: {esc(d['chat_title'])}", MENU)
    await state.clear()


# ───────────────────────── АВТООТВЕТЧИК ─────────────────────────
DEF_AR = "Привет! Сейчас я занят — напишу, когда освобожусь 🙌"


async def ar_get(uid):
    r = await fetchone("SELECT * FROM ar WHERE user_id=?", (uid,))
    if not r:
        await run("INSERT INTO ar(user_id,enabled,text,delay,cool,read) VALUES(?,0,?,3,300,1)", (uid, DEF_AR))
        r = await fetchone("SELECT * FROM ar WHERE user_id=?", (uid,))
    return dict(r)


async def ar_panel(ev, state, warn=""):
    uid = ev.from_user.id
    s = await ar_get(uid)
    con = await fetchone("SELECT 1 FROM biz WHERE user_id=? AND enabled=1", (uid,))
    conn = "✅ подключён" if con else "⚠️ не подключён"
    guide = "" if con else (
        "\n<b>Как подключить</b> (нужен Telegram Premium):\n"
        f"Настройки → <i>Telegram для бизнеса</i> → <i>Чат-боты</i> → введите <code>@{BOT_USERNAME}</code>\n")
    await render(ev, state, head("🤖 Автоответчик") + warn +
                 f"Статус: {'🟢 включён' if s['enabled'] else '🔴 выключен'}\n"
                 f"Бизнес-подключение: {conn}\n\n"
                 f"💬 <i>{esc(s['text'][:200])}</i>\n\n"
                 f"⏱ Ответ через: <b>{s['delay']} с</b>  (0–1000)\n"
                 f"🔁 Не чаще раза в: <b>{s['cool']} с</b> на чат  (0 — всегда)\n"
                 f"👁 Читать сообщения: <b>{'да' if s['read'] else 'нет'}</b>\n{guide}",
                 ikb([("🔴 Выключить" if s["enabled"] else "🟢 Включить", "ar:t:enabled")],
                     [("✏️ Текст ответа", "ar:e:text")],
                     [("⏱ Задержка", "ar:e:delay"), ("🔁 Пауза", "ar:e:cool")],
                     [(f"👁 Читать: {'вкл' if s['read'] else 'выкл'}", "ar:t:read")],
                     [("⬅️ Меню", "menu")]))


@router.callback_query(F.data == "ar:open")
async def ar_open(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await state.set_state(None)
    await ar_panel(cb, state)


@router.callback_query(F.data.startswith("ar:t:"))
async def ar_toggle(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    k = cb.data[5:]
    s = await ar_get(cb.from_user.id)
    await run(f"UPDATE ar SET {k}=? WHERE user_id=?", (0 if s[k] else 1, cb.from_user.id))
    await ar_panel(cb, state)


@router.callback_query(F.data.startswith("ar:e:"))
async def ar_edit(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    k = cb.data[5:]
    prompts = {
        "text": ("✏️ Текст ответа", "Отправьте текст, который бот будет отправлять от вашего имени.", A.text),
        "delay": ("⏱ Задержка", "Через сколько секунд отвечать?\nОтправьте число <b>0–1000</b>.", A.delay),
        "cool": ("🔁 Пауза", "Как часто отвечать одному и тому же чату?\nОтправьте число секунд <b>0–1000</b> "
                             "(0 — отвечать на каждое сообщение).", A.cool)}
    t, body, st = prompts[k]
    await state.set_state(st)
    await render(cb, state, head(t) + body, ikb([("⬅️ Назад", "ar:open")]))


@router.message(A.text, F.text)
async def ar_text(m: Message, state: FSMContext):
    await kill(m)
    await run("UPDATE ar SET text=? WHERE user_id=?", (m.text[:1000], m.from_user.id))
    await state.set_state(None)
    await ar_panel(m, state)


@router.message(A.delay, F.text)
@router.message(A.cool, F.text)
async def ar_num(m: Message, state: FSMContext):
    await kill(m)
    col = "delay" if await state.get_state() == A.delay.state else "cool"
    if not m.text.strip().isdigit() or not 0 <= int(m.text) <= 1000:
        return await ar_panel(m, state, "⚠️ Нужно число от 0 до 1000\n\n")
    await run(f"UPDATE ar SET {col}=? WHERE user_id=?", (int(m.text), m.from_user.id))
    await state.set_state(None)
    await ar_panel(m, state)


@router.business_connection()
async def on_bc(bc: BusinessConnection):
    en = 1 if getattr(bc, "is_enabled", True) else 0
    await run("INSERT OR REPLACE INTO biz VALUES(?,?,?)", (bc.id, bc.user.id, en))
    try:
        await bot.send_message(
            bc.user.id,
            "✅ <b>Автоответчик подключён!</b>\nНастройте текст и время в разделе «🤖 Автоответчик».",
            reply_markup=ikb([("🤖 Настроить", "ar:open")]) if en else None)
    except Exception:
        pass


async def ar_send(bc_id, chat_id, text, delay):
    await asyncio.sleep(delay)
    try:
        await bot.send_message(chat_id, text, business_connection_id=bc_id)
    except Exception:
        log.exception("autoreply failed")


@router.business_message()
async def on_bm(m: Message):
    if not m.business_connection_id or not m.from_user or m.from_user.is_bot:
        return
    row = await fetchone("SELECT user_id FROM biz WHERE id=? AND enabled=1", (m.business_connection_id,))
    if not row or m.from_user.id == row["user_id"]:
        return
    owner = row["user_id"]
    s = await ar_get(owner)
    if not s["enabled"] or not s["text"]:
        return
    if s["read"]:
        try:
            await bot.read_business_message(business_connection_id=m.business_connection_id,
                                            chat_id=m.chat.id, message_id=m.message_id)
        except Exception:
            pass
    now = int(time.time())
    last = await fetchone("SELECT ts FROM ar_log WHERE owner=? AND chat_id=?", (owner, m.chat.id))
    if s["cool"] > 0 and last and now - last["ts"] < s["cool"]:
        return
    await run("INSERT OR REPLACE INTO ar_log VALUES(?,?,?)", (owner, m.chat.id, now))
    asyncio.create_task(ar_send(m.business_connection_id, m.chat.id, s["text"], s["delay"]))


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global BOT_USERNAME, BOT_ID
    await init_db()
    me = await bot.get_me()
    BOT_USERNAME, BOT_ID = me.username, me.id
    log.info("Запущен @%s", BOT_USERNAME)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    asyncio.create_task(scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
