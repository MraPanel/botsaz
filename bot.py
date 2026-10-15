"""
ربات مادر (ربات‌ساز میرزا)
- تست ۲۴ ساعته و خرید/تمدید ۳۰ روزه
- ساخت ربات مشتری روی هاست خودت (کپی از قالب میرزا + نوشتن کانفیگ + ست‌کردن وبهوک)
- پنل مالک/ادمین: مدیریت همه ربات‌ها، ویرایش متن‌ها و دکمه‌ها، قیمت و کارت، مدیریت ادمین‌ها
"""
import asyncio
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("factory")

# ───────────────────────── تنظیمات (از env) ─────────────────────────
MAIN_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
BASE_URL = os.environ["BASE_URL"].rstrip("/")            # مثلا https://example.com
TEMPLATE_DIR = Path(os.environ["TEMPLATE_DIR"])          # سورس میرزا (قالب)
BOTS_DIR = Path(os.environ["BOTS_DIR"])                  # جایی که ربات‌های مشتری ساخته می‌شود
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "bots/bot_{id}/index.php")
CONFIG_TPL = Path(os.environ.get("CONFIG_TPL", "config.php.tpl"))
CONFIG_FILENAME = os.environ.get("CONFIG_FILENAME", "config.php")
MYSQL_ROOT_PASS = os.environ.get("MYSQL_ROOT_PASS", "")  # خالی = ساخت دیتابیس انجام نمی‌شود
DB_PATH = os.environ.get("DB_PATH", "factory.db")

TRIAL_SECONDS = 24 * 3600
MONTH_SECONDS = 30 * 24 * 3600

# ───────────────────────── متن‌ها و دکمه‌های قابل ویرایش ─────────────────────────
DEFAULTS = {
    # متن‌ها
    "welcome": "سلام 👋\nاینجا می‌توانی ربات فروش کانفیگ میرزای خودت را بسازی.",
    "help": "۱) در @BotFather یک ربات بساز و توکن را بگیر.\n"
            "۲) آیدی عددی خودت را آماده کن (مثلا از @userinfobot).\n"
            "۳) تست ۲۴ ساعته را بزن یا اشتراک یک‌ماهه بخر.",
    "ask_token": "توکن رباتت را بفرست (از @BotFather):",
    "ask_admin": "آیدی عددی ادمین ربات را بفرست:",
    "bad_token": "❌ توکن معتبر نیست یا قبلا ثبت شده. دوباره بفرست:",
    "bad_admin": "❌ آیدی عددی معتبر نیست. فقط عدد بفرست:",
    "creating": "⏳ در حال ساخت ربات...",
    "trial_done": "✅ ربات تستی ساخته شد و ۲۴ ساعت فعال است.\n@{username}",
    "trial_used": "شما قبلا از تست رایگان استفاده کرده‌اید.",
    "pay_info": "💳 مبلغ: {price} تومان\nشماره کارت: {card}\n\n"
                "بعد از پرداخت، عکس رسید را همین‌جا بفرست.",
    "receipt_sent": "✅ رسید ارسال شد. بعد از تایید مدیر، ربات فعال می‌شود.",
    "paid_done": "✅ پرداخت تایید شد. ربات @{username} تا {expires} فعال است.",
    "paid_rejected": "❌ پرداخت شما تایید نشد. با پشتیبانی تماس بگیرید.",
    "expired_note": "⛔️ اشتراک ربات @{username} تمام شد. برای فعال‌سازی دوباره تمدید کن.",
    "no_bots": "هنوز رباتی نداری.",
    "create_failed": "❌ ساخت ربات ناموفق بود. بعدا دوباره تلاش کن یا به پشتیبانی بگو.",
    # دکمه‌ها
    "btn_trial": "🧪 تست ۲۴ ساعته",
    "btn_buy": "🛒 خرید یک‌ماهه",
    "btn_mybots": "🤖 ربات‌های من",
    "btn_help": "ℹ️ راهنما",
    "btn_renew": "🔄 تمدید یک‌ماهه",
    "btn_back": "🔙 برگشت",
    # تنظیمات عددی
    "price": "100000",
    "card": "0000-0000-0000-0000",
}
TEXT_KEYS = [k for k in DEFAULTS if not k.startswith("btn_") and k not in ("price", "card")]
BTN_KEYS = [k for k in DEFAULTS if k.startswith("btn_")]

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, trial_used INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS bots(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, token TEXT UNIQUE,
  username TEXT, admin_id INTEGER, kind TEXT, status TEXT,
  created_at INTEGER, expires_at INTEGER);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, bot_id INTEGER,
  status TEXT, file_id TEXT, created_at INTEGER);
""")
db.commit()


def T(key: str) -> str:
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else DEFAULTS[key]


def set_setting(key: str, value: str):
    db.execute("INSERT INTO settings(key,value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def is_owner(uid: int) -> bool:
    return uid == OWNER_ID


def is_admin(uid: int) -> bool:
    return is_owner(uid) or db.execute(
        "SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None


def fmt_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


# ───────────────────────── ارتباط با تلگرام و هاست ─────────────────────────
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")


async def tg_call(token: str, method: str, **params):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://api.telegram.org/bot{token}/{method}", json=params) as r:
            return await r.json()


def webhook_url(bot_id: int) -> str:
    return f"{BASE_URL}/{WEBHOOK_PATH.format(id=bot_id)}"


def _provision_files(bot_id: int, token: str, username: str, admin_id: int):
    target = BOTS_DIR / f"bot_{bot_id}"
    if not target.exists():
        shutil.copytree(TEMPLATE_DIR, target)
    db_name = f"mirza_{bot_id}"
    db_user = f"mirza_{bot_id}"
    db_pass = secrets.token_hex(12)
    if MYSQL_ROOT_PASS:
        sql = (f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4; "
               f"CREATE USER IF NOT EXISTS '{db_user}'@'localhost' IDENTIFIED BY '{db_pass}'; "
               f"ALTER USER '{db_user}'@'localhost' IDENTIFIED BY '{db_pass}'; "
               f"GRANT ALL ON `{db_name}`.* TO '{db_user}'@'localhost'; FLUSH PRIVILEGES;")
        subprocess.run(["mysql", "-uroot", f"-p{MYSQL_ROOT_PASS}", "-e", sql],
                       check=True, capture_output=True)
    cfg = CONFIG_TPL.read_text(encoding="utf-8")
    for k, v in {"TOKEN": token, "ADMIN_ID": admin_id, "BOT_USERNAME": username,
                 "DOMAIN": BASE_URL, "DB_NAME": db_name, "DB_USER": db_user,
                 "DB_PASS": db_pass, "BOT_ID": bot_id}.items():
        cfg = cfg.replace("{" + k + "}", str(v))
    (target / CONFIG_FILENAME).write_text(cfg, encoding="utf-8")


async def provision(bot_id: int, token: str, username: str, admin_id: int) -> bool:
    try:
        await asyncio.to_thread(_provision_files, bot_id, token, username, admin_id)
        res = await tg_call(token, "setWebhook", url=webhook_url(bot_id))
        if not res.get("ok"):
            log.error("setWebhook failed: %s", res)
            return False
        return True
    except Exception:
        log.exception("provision failed")
        return False


async def enable_bot(row) -> bool:
    res = await tg_call(row["token"], "setWebhook", url=webhook_url(row["id"]))
    return bool(res.get("ok"))


async def disable_bot(row):
    await tg_call(row["token"], "deleteWebhook")


async def destroy_bot(row):
    await disable_bot(row)
    shutil.rmtree(BOTS_DIR / f"bot_{row['id']}", ignore_errors=True)
    db.execute("DELETE FROM bots WHERE id=?", (row["id"],))
    db.commit()


# ───────────────────────── کیبوردها ─────────────────────────
def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_menu(uid: int):
    rows = [[btn(T("btn_trial"), "trial")], [btn(T("btn_buy"), "buy")],
            [btn(T("btn_mybots"), "my")], [btn(T("btn_help"), "help")]]
    if is_admin(uid):
        rows.append([btn("⚙️ پنل مدیریت", "panel")])
    return kb(rows)


def back_btn(data="home"):
    return [btn(T("btn_back"), data)]


# ───────────────────────── State ها ─────────────────────────
class Create(StatesGroup):
    token = State()
    admin = State()


class Receipt(StatesGroup):
    wait = State()


class Edit(StatesGroup):
    value = State()


class AddAdmin(StatesGroup):
    uid = State()


r = Router()


# ───────────────────────── کاربر ─────────────────────────
@r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (m.from_user.id,))
    db.commit()
    await m.answer(T("welcome"), reply_markup=main_menu(m.from_user.id))


@r.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text(T("welcome"), reply_markup=main_menu(c.from_user.id))


@r.callback_query(F.data == "help")
async def help_(c: CallbackQuery):
    await c.message.edit_text(T("help"), reply_markup=kb([back_btn()]))


@r.callback_query(F.data.in_({"trial", "buy"}))
async def begin_create(c: CallbackQuery, state: FSMContext):
    if c.data == "trial":
        row = db.execute("SELECT trial_used FROM users WHERE user_id=?", (c.from_user.id,)).fetchone()
        if (row and row["trial_used"]) and not is_admin(c.from_user.id):
            return await c.answer(T("trial_used"), show_alert=True)
    await state.set_state(Create.token)
    await state.update_data(kind=c.data)
    await c.message.edit_text(T("ask_token"), reply_markup=kb([back_btn()]))


@r.message(Create.token)
async def got_token(m: Message, state: FSMContext):
    token = (m.text or "").strip()
    if not TOKEN_RE.match(token) or db.execute(
            "SELECT 1 FROM bots WHERE token=?", (token,)).fetchone():
        return await m.answer(T("bad_token"))
    info = await tg_call(token, "getMe")
    if not info.get("ok"):
        return await m.answer(T("bad_token"))
    await state.update_data(token=token, username=info["result"]["username"])
    await state.set_state(Create.admin)
    await m.answer(T("ask_admin"))


@r.message(Create.admin)
async def got_admin(m: Message, state: FSMContext):
    txt = (m.text or "").strip()
    if not txt.isdigit():
        return await m.answer(T("bad_admin"))
    d = await state.get_data()
    admin_id = int(txt)
    now = int(time.time())
    kind = d["kind"]
    cur = db.execute(
        "INSERT INTO bots(user_id,token,username,admin_id,kind,status,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (m.from_user.id, d["token"], d["username"], admin_id, kind,
         "active" if kind == "trial" else "pending",
         now, now + TRIAL_SECONDS if kind == "trial" else 0))
    db.commit()
    bot_id = cur.lastrowid

    if kind == "trial":
        await m.answer(T("creating"))
        ok = await provision(bot_id, d["token"], d["username"], admin_id)
        if not ok:
            db.execute("DELETE FROM bots WHERE id=?", (bot_id,))
            db.commit()
            await state.clear()
            return await m.answer(T("create_failed"), reply_markup=main_menu(m.from_user.id))
        db.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (m.from_user.id,))
        db.commit()
        await state.clear()
        return await m.answer(T("trial_done").format(username=d["username"]),
                              reply_markup=main_menu(m.from_user.id))

    await ask_receipt(m, state, bot_id)


async def ask_receipt(target: Message, state: FSMContext, bot_id: int):
    await state.set_state(Receipt.wait)
    await state.update_data(bot_id=bot_id)
    await target.answer(T("pay_info").format(price=T("price"), card=T("card")),
                        reply_markup=kb([back_btn()]))


@r.callback_query(F.data.startswith("renew:"))
async def renew(c: CallbackQuery, state: FSMContext):
    bot_id = int(c.data.split(":")[1])
    row = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?", (bot_id, c.from_user.id)).fetchone()
    if not row:
        return await c.answer("یافت نشد", show_alert=True)
    await ask_receipt(c.message, state, bot_id)
    await c.answer()


@r.message(Receipt.wait)
async def got_receipt(m: Message, state: FSMContext, bot: Bot):
    if not m.photo and not m.document:
        return await m.answer("لطفا عکس رسید را بفرست.")
    d = await state.get_data()
    file_id = m.photo[-1].file_id if m.photo else m.document.file_id
    cur = db.execute("INSERT INTO payments(user_id,bot_id,status,file_id,created_at) VALUES(?,?,?,?,?)",
                     (m.from_user.id, d["bot_id"], "waiting", file_id, int(time.time())))
    db.commit()
    pid = cur.lastrowid
    brow = db.execute("SELECT username FROM bots WHERE id=?", (d["bot_id"],)).fetchone()
    caption = (f"💰 رسید جدید #{pid}\nکاربر: {m.from_user.id} (@{m.from_user.username})\n"
               f"ربات: @{brow['username']}")
    markup = kb([[btn("✅ تایید", f"pay_ok:{pid}"), btn("❌ رد", f"pay_no:{pid}")]])
    targets = {OWNER_ID} | {x["user_id"] for x in db.execute("SELECT user_id FROM admins")}
    for t in targets:
        try:
            if m.photo:
                await bot.send_photo(t, file_id, caption=caption, reply_markup=markup)
            else:
                await bot.send_document(t, file_id, caption=caption, reply_markup=markup)
        except Exception:
            log.warning("cannot notify %s", t)
    await state.clear()
    await m.answer(T("receipt_sent"), reply_markup=main_menu(m.from_user.id))


@r.callback_query(F.data == "my")
async def my_bots(c: CallbackQuery):
    rows = db.execute("SELECT * FROM bots WHERE user_id=? AND status!='pending'", (c.from_user.id,)).fetchall()
    if not rows:
        return await c.message.edit_text(T("no_bots"), reply_markup=kb([back_btn()]))
    kbrows = [[btn(f"@{x['username']} — {x['status']}", f"mybot:{x['id']}")] for x in rows]
    kbrows.append(back_btn())
    await c.message.edit_text("ربات‌های تو:", reply_markup=kb(kbrows))


@r.callback_query(F.data.startswith("mybot:"))
async def my_bot(c: CallbackQuery):
    row = db.execute("SELECT * FROM bots WHERE id=? AND user_id=?",
                     (int(c.data.split(":")[1]), c.from_user.id)).fetchone()
    if not row:
        return await c.answer("یافت نشد", show_alert=True)
    txt = (f"🤖 @{row['username']}\nوضعیت: {row['status']}\nنوع: {row['kind']}\n"
           f"انقضا: {fmt_time(row['expires_at'])}")
    await c.message.edit_text(txt, reply_markup=kb([[btn(T("btn_renew"), f"renew:{row['id']}")], back_btn("my")]))


# ───────────────────────── تایید / رد پرداخت ─────────────────────────
@r.callback_query(F.data.startswith(("pay_ok:", "pay_no:")))
async def pay_decide(c: CallbackQuery, bot: Bot):
    if not is_admin(c.from_user.id):
        return await c.answer("دسترسی نداری", show_alert=True)
    action, pid = c.data.split(":")
    pay = db.execute("SELECT * FROM payments WHERE id=?", (int(pid),)).fetchone()
    if not pay or pay["status"] != "waiting":
        return await c.answer("قبلا بررسی شده", show_alert=True)
    brow = db.execute("SELECT * FROM bots WHERE id=?", (pay["bot_id"],)).fetchone()
    if not brow:
        return await c.answer("ربات حذف شده", show_alert=True)

    if action == "pay_no":
        db.execute("UPDATE payments SET status='rejected' WHERE id=?", (pay["id"],))
        if brow["status"] == "pending":
            db.execute("DELETE FROM bots WHERE id=?", (brow["id"],))
        db.commit()
        await bot.send_message(pay["user_id"], T("paid_rejected"))
        return await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n❌ رد شد")

    now = int(time.time())
    if brow["status"] == "pending":
        ok = await provision(brow["id"], brow["token"], brow["username"], brow["admin_id"])
        if not ok:
            return await c.answer("ساخت ربات ناموفق بود، لاگ را ببین", show_alert=True)
        new_exp = now + MONTH_SECONDS
    else:
        new_exp = max(now, brow["expires_at"]) + MONTH_SECONDS
        if brow["status"] in ("expired", "disabled"):
            if not await enable_bot(brow):
                return await c.answer("فعال‌سازی وبهوک ناموفق بود", show_alert=True)
    db.execute("UPDATE bots SET status='active', kind='paid', expires_at=? WHERE id=?", (new_exp, brow["id"]))
    db.execute("UPDATE payments SET status='approved' WHERE id=?", (pay["id"],))
    db.commit()
    await bot.send_message(pay["user_id"], T("paid_done").format(
        username=brow["username"], expires=fmt_time(new_exp)))
    await c.message.edit_caption(caption=(c.message.caption or "") + "\n\n✅ تایید شد")


# ───────────────────────── پنل مدیریت ─────────────────────────
def panel_menu(uid: int):
    rows = [[btn("🤖 همه ربات‌ها", "abots")],
            [btn("📝 ویرایش متن‌ها", "edit:text"), btn("🔘 ویرایش دکمه‌ها", "edit:btn")],
            [btn("💰 قیمت و کارت", "edit:cfg")]]
    if is_owner(uid):
        rows.append([btn("👥 مدیریت ادمین‌ها", "admins")])
    rows.append(back_btn())
    return kb(rows)


@r.message(Command("panel"))
async def panel_cmd(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("⚙️ پنل مدیریت", reply_markup=panel_menu(m.from_user.id))


@r.callback_query(F.data == "panel")
async def panel(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return await c.answer("دسترسی نداری", show_alert=True)
    await state.clear()
    await c.message.edit_text("⚙️ پنل مدیریت", reply_markup=panel_menu(c.from_user.id))


@r.callback_query(F.data == "abots")
async def abots(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    rows = db.execute("SELECT * FROM bots ORDER BY id DESC LIMIT 40").fetchall()
    kbrows = [[btn(f"#{x['id']} @{x['username']} — {x['status']}", f"abot:{x['id']}")] for x in rows]
    kbrows.append(back_btn("panel"))
    await c.message.edit_text(f"کل ربات‌ها: {len(rows)}", reply_markup=kb(kbrows))


@r.callback_query(F.data.startswith("abot:"))
async def abot(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    row = db.execute("SELECT * FROM bots WHERE id=?", (int(c.data.split(":")[1]),)).fetchone()
    if not row:
        return await c.answer("یافت نشد", show_alert=True)
    txt = (f"#{row['id']} @{row['username']}\nمالک: {row['user_id']}\nادمین ربات: {row['admin_id']}\n"
           f"وضعیت: {row['status']} ({row['kind']})\nانقضا: {fmt_time(row['expires_at'])}")
    i = row["id"]
    await c.message.edit_text(txt, reply_markup=kb([
        [btn("➕ ۳۰ روز", f"aact:add:{i}"), btn("⏯ فعال/غیرفعال", f"aact:tog:{i}")],
        [btn("🗑 حذف", f"aact:del:{i}")], back_btn("abots")]))


@r.callback_query(F.data.startswith("aact:"))
async def aact(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    _, act, i = c.data.split(":")
    row = db.execute("SELECT * FROM bots WHERE id=?", (int(i),)).fetchone()
    if not row:
        return await c.answer("یافت نشد", show_alert=True)
    now = int(time.time())
    if act == "add":
        exp = max(now, row["expires_at"]) + MONTH_SECONDS
        if row["status"] != "active":
            if not await enable_bot(row):
                return await c.answer("وبهوک ست نشد", show_alert=True)
        db.execute("UPDATE bots SET status='active', expires_at=? WHERE id=?", (exp, row["id"]))
        db.commit()
        await c.answer("۳۰ روز اضافه شد")
    elif act == "tog":
        if row["status"] == "active":
            await disable_bot(row)
            db.execute("UPDATE bots SET status='disabled' WHERE id=?", (row["id"],))
        else:
            if not await enable_bot(row):
                return await c.answer("وبهوک ست نشد", show_alert=True)
            db.execute("UPDATE bots SET status='active' WHERE id=?", (row["id"],))
        db.commit()
        await c.answer("انجام شد")
    elif act == "del":
        await destroy_bot(row)
        await c.answer("حذف شد")
        return await abots(c)
    c.data = f"abot:{i}"
    await abot(c)


# ---- ویرایش متن/دکمه/تنظیمات ----
@r.callback_query(F.data.startswith("edit:"))
async def edit_list(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return
    group = c.data.split(":")[1]
    keys = {"text": TEXT_KEYS, "btn": BTN_KEYS, "cfg": ["price", "card"]}[group]
    rows = [[btn(k, f"ek:{k}")] for k in keys]
    rows.append(back_btn("panel"))
    await c.message.edit_text("کدام را ویرایش کنم؟", reply_markup=kb(rows))


@r.callback_query(F.data.startswith("ek:"))
async def edit_key(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        return
    key = c.data.split(":")[1]
    await state.set_state(Edit.value)
    await state.update_data(key=key)
    await c.message.edit_text(f"مقدار فعلی «{key}»:\n\n{T(key)}\n\nمقدار جدید را بفرست:",
                              reply_markup=kb([back_btn("panel")]))


@r.message(Edit.value)
async def edit_value(m: Message, state: FSMContext):
    if not is_admin(m.from_user.id):
        return
    d = await state.get_data()
    set_setting(d["key"], m.text or "")
    await state.clear()
    await m.answer("✅ ذخیره شد.", reply_markup=panel_menu(m.from_user.id))


# ---- مدیریت ادمین‌ها (فقط مالک) ----
@r.callback_query(F.data == "admins")
async def admins(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        return await c.answer("فقط مالک", show_alert=True)
    rows = [[btn(f"🗑 {x['user_id']}", f"deladm:{x['user_id']}")]
            for x in db.execute("SELECT user_id FROM admins")]
    rows.append([btn("➕ افزودن ادمین", "addadm")])
    rows.append(back_btn("panel"))
    await c.message.edit_text("ادمین‌ها:", reply_markup=kb(rows))


@r.callback_query(F.data == "addadm")
async def add_adm(c: CallbackQuery, state: FSMContext):
    if not is_owner(c.from_user.id):
        return
    await state.set_state(AddAdmin.uid)
    await c.message.edit_text("آیدی عددی ادمین جدید را بفرست:")


@r.message(AddAdmin.uid)
async def add_adm_uid(m: Message, state: FSMContext):
    if not is_owner(m.from_user.id):
        return
    if not (m.text or "").strip().isdigit():
        return await m.answer("فقط عدد بفرست.")
    db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES(?)", (int(m.text.strip()),))
    db.commit()
    await state.clear()
    await m.answer("✅ اضافه شد.", reply_markup=panel_menu(m.from_user.id))


@r.callback_query(F.data.startswith("deladm:"))
async def del_adm(c: CallbackQuery):
    if not is_owner(c.from_user.id):
        return
    db.execute("DELETE FROM admins WHERE user_id=?", (int(c.data.split(":")[1]),))
    db.commit()
    await admins(c)


# ───────────────────────── کنترل انقضا ─────────────────────────
async def expiry_loop(bot: Bot):
    while True:
        try:
            now = int(time.time())
            rows = db.execute("SELECT * FROM bots WHERE status='active' AND expires_at<?", (now,)).fetchall()
            for row in rows:
                await disable_bot(row)
                db.execute("UPDATE bots SET status='expired' WHERE id=?", (row["id"],))
                db.commit()
                try:
                    await bot.send_message(row["user_id"], T("expired_note").format(username=row["username"]),
                                           reply_markup=kb([[btn(T("btn_renew"), f"renew:{row['id']}")]]))
                except Exception:
                    pass
        except Exception:
            log.exception("expiry loop error")
        await asyncio.sleep(300)


async def main():
    BOTS_DIR.mkdir(parents=True, exist_ok=True)
    bot = Bot(MAIN_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(r)
    asyncio.create_task(expiry_loop(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
