#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sqlite3, time, asyncio, logging, traceback
from datetime import datetime, timedelta

import pytz
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
)

# ========= Config por variables de entorno =========
BOT_TOKEN  = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()  # puede ser -100xxxxx o @canal
CHANNEL_ID = None
if CHANNEL_ID_RAW:
    try:
        # si viene numérico (ej: -100123...), úsalo como int; si no, quedará string (@canal)
        CHANNEL_ID = int(CHANNEL_ID_RAW)
    except ValueError:
        CHANNEL_ID = CHANNEL_ID_RAW

ADMIN_IDS  = {int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()}
LINK_VALID_HOURS = int(os.getenv("LINK_VALID_HOURS", "48"))
TZ = pytz.timezone(os.getenv("TZ", "America/Santiago"))
DB = os.getenv("DB_PATH", "/data/suscriptores.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)


# ========= Utilidades =========
def now_cl() -> datetime:
    return datetime.now(TZ)

def fmt_fecha(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

def db():
    conn = sqlite3.connect(DB)
    conn.execute("""CREATE TABLE IF NOT EXISTS subs(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        start_ts INTEGER,
        expire_ts INTEGER,
        last_warn TEXT DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS links(
        invite_link TEXT PRIMARY KEY,
        username TEXT,
        invite_expire_ts INTEGER,
        plan_days INTEGER
    )""")
    return conn

async def must_admin(update: Update) -> bool:
    """Permite comandos solo a ADMIN_IDS. Si ADMIN_IDS está vacío, permite a cualquiera."""
    if not ADMIN_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("No tienes permiso para este comando.")
        return False
    return True


# ========= Comandos =========
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "/ping\n"
        "/help\n"
        "/link @usuario DIAS  → genera link 1 uso (caduca en horas configuradas)\n"
        "/renew @usuario DIAS → suma DIAS al vencimiento del usuario\n"
        "/list                → lista suscriptores y vencimientos\n"
        "/check               → fuerza expulsión de vencidos ahora"
    )
    await update.message.reply_text(txt)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    if not CHANNEL_ID:
        return await update.message.reply_text("Falta configurar CHANNEL_ID.")
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /link @usuario DIAS")

    username = context.args[0].lstrip("@").lower()
    try:
        dias = int(context.args[1])
    except ValueError:
        return await update.message.reply_text("DIAS debe ser número. Ej: /link @usuario 30")

    expire_unix = int((now_cl() + timedelta(hours=LINK_VALID_HOURS)).timestamp())

    # crear link de invitación 1 uso con join request
    invite = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire_unix,
        member_limit=1,
        creates_join_request=True
    )

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO links(invite_link, username, invite_expire_ts, plan_days) VALUES(?,?,?,?)",
            (invite.invite_link, username, expire_unix, dias)
        )

    await update.message.reply_text(
        f"🔗 Link para @{username}: {invite.invite_link}\n"
        f"Válido {LINK_VALID_HOURS} horas, 1 uso. Plan: {dias} días."
    )

async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /renew @usuario DIAS")

    username = context.args[0].lstrip("@").lower()
    try:
        dias = int(context.args[1])
    except ValueError:
        return await update.message.reply_text("DIAS debe ser número. Ej: /renew @usuario 30")

    now_ts = int(now_cl().timestamp())
    add_secs = dias * 86400

    with db() as conn:
        row = conn.execute("SELECT user_id, expire_ts FROM subs WHERE lower(username)=?", (username,)).fetchone()
        if not row:
            return await update.message.reply_text(f"No encuentro a @{username} en la base.")
        uid, exp_ts = row
        base_ts = exp_ts if exp_ts and exp_ts > now_ts else now_ts
        new_exp = base_ts + add_secs
        conn.execute("UPDATE subs SET expire_ts=? WHERE user_id=?", (new_exp, uid))

    await update.message.reply_text(f"🔄 Renovado @{username} hasta {fmt_fecha(new_exp)} (~{dias}d añadidos).")

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    with db() as conn:
        rows = conn.execute("SELECT user_id, username, expire_ts FROM subs ORDER BY expire_ts").fetchall()
    if not rows:
        return await update.message.reply_text("Sin suscriptores.")

    now_ts = int(time.time())
    out = []
    for uid, uname, exp in rows:
        dias = int((exp - now_ts) / 86400)
        estado = "✅" if exp > now_ts else "⛔"
        out.append(f"{estado} @{(uname or '-') } (id:{uid}) vence {fmt_fecha(exp)} (≈{dias}d)")
    txt = "\n".join(out)
    await update.message.reply_text(txt[:4000])

async def run_checks_with_bot(bot):
    """Banea y desbanea a vencidos para sacarlos del canal."""
    now_dt = now_cl()
    with db() as conn:
        rows = conn.execute("SELECT user_id, username, expire_ts FROM subs").fetchall()
    for uid, uname, exp in rows:
        exp_dt = datetime.fromtimestamp(exp, TZ)
        if now_dt > exp_dt:
            try:
                if CHANNEL_ID:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=uid)
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=uid)
                    logging.info("Expulsado vencido id=%s @%s", uid, uname)
            except Exception as e:
                logging.warning("No pude expulsar id=%s @%s: %s", uid, uname, e)

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    await run_checks_with_bot(context.bot)
    await update.message.reply_text("Chequeo completo.")


# ========= Handler de solicitudes de unión =========
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    # verificar que sea el canal correcto (si pasaste @, también funciona)
    if CHANNEL_ID and req.chat.id != CHANNEL_ID:
        # si CHANNEL_ID es string @canal, Telegram reporta id numérico igual; en ese caso no filtres
        if isinstance(CHANNEL_ID, int):
            return

    link_url = req.invite_link.invite_link if req.invite_link else None
    plan_days = None
    username_asignado = None

    if link_url:
        with db() as conn:
            row = conn.execute(
                "SELECT username, plan_days FROM links WHERE invite_link=?",
                (link_url,)
            ).fetchone()
        if row:
            username_asignado, plan_days = row

    # si no hay registro del link, declina (evita colados)
    if not plan_days:
        try:
            await context.bot.decline_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
        except Exception:
            pass
        return

    # registrar suscripción y aprobar
    start_ts = int(now_cl().timestamp())
    expire_ts = int((now_cl() + timedelta(days=plan_days)).timestamp())

    with db() as conn:
        conn.execute("""
            INSERT INTO subs(user_id, username, start_ts, expire_ts, last_warn)
            VALUES(?,?,?,?, '')
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                start_ts=excluded.start_ts,
                expire_ts=excluded.expire_ts,
                last_warn=''
        """, (req.from_user.id, username_asignado or (req.from_user.username or "").lower(), start_ts, expire_ts))

    await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)

    # bienvenida por DM si es posible
    try:
        await context.bot.send_message(
            chat_id=req.from_user.id,
            text=f"✅ Acceso aprobado. Tu plan vence el {fmt_fecha(expire_ts)}."
        )
    except Forbidden:
        pass


# ========= Tarea diaria programada (9:00 America/Santiago) =========
def schedule_daily_checks(app):
    async def runner():
        while True:
            now = now_cl()
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await run_checks_with_bot(app.bot)
            except Exception as e:
                logging.warning("Fallo en chequeo diario: %s", e)
    app.create_task(runner())


# ========= Arranque =========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN no está definido")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # comandos
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("renew", cmd_renew))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))

    # join requests
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    # tarea diaria
    schedule_daily_checks(app)

    logging.info("Bot iniciando polling...")
    app.run_polling(allowed_updates=["message", "chat_join_request"])

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:", e)
        traceback.print_exc()
        input("Presiona ENTER para salir...")
