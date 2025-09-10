#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sqlite3, time, logging, traceback
from datetime import datetime, timedelta, time as dtime

import pytz
from telegram import Update
from telegram.error import Forbidden
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
)

# ========= Config =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "").strip()  # -100xxxxx o @canal
CHANNEL_ID = None
if CHANNEL_ID_RAW:
    try:
        CHANNEL_ID = int(CHANNEL_ID_RAW)
    except ValueError:
        CHANNEL_ID = CHANNEL_ID_RAW

ADMIN_IDS = {int(x) for x in (os.getenv("ADMIN_IDS") or "").split(",") if x.strip().isdigit()}
LINK_VALID_HOURS = int(os.getenv("LINK_VALID_HOURS", "48"))
TZ = pytz.timezone(os.getenv("TZ", "America/Santiago"))
DB = os.getenv("DB_PATH", "suscriptores.db")  # local, junto al código

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

# ========= Utils & DB =========
def now_cl() -> datetime:
    return datetime.now(TZ)

def fmt_fecha(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

def db():
    db_path = os.path.abspath(DB)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS subs(
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        start_ts INTEGER,
        expire_ts INTEGER,
        last_warn TEXT DEFAULT ''
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS links(
        invite_link TEXT PRIMARY KEY,
        plan_days INTEGER,
        invite_expire_ts INTEGER
    )""")
    return conn

async def must_admin(update: Update) -> bool:
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
    await update.message.reply_text(
        "/ping\n"
        "/help\n"
        "/link DIAS       → genera link (1 uso, se aprueba solo)\n"
        "/renew @usuario DIAS → renueva a un usuario ya existente\n"
        "/list            → lista suscriptores y vencimientos\n"
        "/check           → fuerza expulsión de vencidos ahora\n"
        "/linkraw         → link de prueba sin DB (diagnóstico)\n"
        "/checkperms      → muestra permisos del bot en el canal"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Crea link de solicitud (join request). SOLO recibe DIAS."""
    if not await must_admin(update):
        return
    if not CHANNEL_ID:
        return await update.message.reply_text("Falta configurar CHANNEL_ID.")
    if len(context.args) < 1:
        return await update.message.reply_text("Uso: /link DIAS   (ej: /link 30)")

    try:
        dias = int(context.args[0])
        if dias <= 0:
            raise ValueError
    except ValueError:
        return await update.message.reply_text("DIAS debe ser número positivo. Ej: /link 30")

    expire_unix = int((now_cl() + timedelta(hours=LINK_VALID_HOURS)).timestamp())

    # Importante: para join request NO usar member_limit con PTB v20
    invite = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire_unix,
        creates_join_request=True
    )

    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO links(invite_link, plan_days, invite_expire_ts) VALUES(?,?,?)",
            (invite.invite_link, dias, expire_unix)
        )

    await update.message.reply_text(
        f"🔗 Link: {invite.invite_link}\n"
        f"Válido {LINK_VALID_HOURS} horas. 1 uso (se revoca al aprobar). Plan: {dias} días."
    )

async def cmd_linkraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Link sin DB para probar permisos/canal."""
    if not await must_admin(update):
        return
    if not CHANNEL_ID:
        return await update.message.reply_text("Falta CHANNEL_ID.")
    expire_unix = int((now_cl() + timedelta(hours=LINK_VALID_HOURS)).timestamp())
    invite = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire_unix,
        creates_join_request=True
    )
    await update.message.reply_text(f"🔗 {invite.invite_link}")

async def cmd_renew(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /renew @usuario DIAS")

    username = context.args[0].lstrip("@").lower()
    try:
        dias = int(context.args[1])
        if dias <= 0:
            raise ValueError
    except ValueError:
        return await update.message.reply_text("DIAS debe ser número positivo. Ej: /renew @usuario 30")

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

    await update.message.reply_text(f"🔄 Renovado @{username} hasta {fmt_fecha(new_exp)} (+{dias}d).")

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
    await update.message.reply_text("\n".join(out)[:4000])

async def cmd_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    await run_checks_with_bot(context.bot)
    await update.message.reply_text("Chequeo completo.")

async def cmd_checkperms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update):
        return
    try:
        me = await context.bot.get_me()
        cm = await context.bot.get_chat_member(CHANNEL_ID, me.id)
        can_invite = getattr(cm, "can_invite_users", None)
        can_manage_chat = getattr(cm, "can_manage_chat", None)
        await update.message.reply_text(
            f"status: {cm.status}\n"
            f"can_invite_users: {can_invite}\n"
            f"can_manage_chat: {can_manage_chat}\n"
        )
    except Exception as e:
        await update.message.reply_text(f"❌ No pude leer permisos: {type(e).__name__}: {e}")

# ========= Lógica de vencidos =========
async def run_checks_with_bot(bot):
    now_dt = now_cl()
    with db() as conn:
        rows = conn.execute("SELECT user_id, username, expire_ts FROM subs").fetchall()
    for uid, uname, exp in rows:
        if now_dt > datetime.fromtimestamp(exp, TZ):
            try:
                if CHANNEL_ID:
                    await bot.ban_chat_member(chat_id=CHANNEL_ID, user_id=uid)
                    await bot.unban_chat_member(chat_id=CHANNEL_ID, user_id=uid)
                    logging.info("Expulsado vencido id=%s @%s", uid, uname)
            except Exception as e:
                logging.warning("No pude expulsar id=%s @%s: %s", uid, uname, e)

# ========= Join Request =========
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    if CHANNEL_ID and isinstance(CHANNEL_ID, int) and req.chat.id != CHANNEL_ID:
        return

    link_url = req.invite_link.invite_link if req.invite_link else None
    plan_days = None

    if link_url:
        with db() as conn:
            row = conn.execute(
                "SELECT plan_days FROM links WHERE invite_link=?",
                (link_url,)
            ).fetchone()
        if row:
            plan_days = int(row[0])

    if not plan_days:
        # link no registrado: rechazamos para evitar colados
        try:
            await context.bot.decline_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
        except Exception:
            pass
        return

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
        """, (req.from_user.id, (req.from_user.username or "").lower(), start_ts, expire_ts))

    # aprobar y revocar link (lo deja de 1 uso)
    await context.bot.approve_chat_join_request(chat_id=req.chat.id, user_id=req.from_user.id)
    try:
        if link_url:
            await context.bot.revoke_chat_invite_link(chat_id=req.chat.id, invite_link=link_url)
            with db() as conn:
                conn.execute("DELETE FROM links WHERE invite_link=?", (link_url,))
    except Exception as e:
        logging.warning("No pude revocar link %s: %s", link_url, e)

    try:
        await context.bot.send_message(
            chat_id=req.from_user.id,
            text=f"✅ Acceso aprobado. Tu plan vence el {fmt_fecha(expire_ts)}."
        )
    except Forbidden:
        pass

# ========= Job diario =========
async def daily_check(context: ContextTypes.DEFAULT_TYPE):
    try:
        await run_checks_with_bot(context.bot)
    except Exception as e:
        logging.warning("Fallo en chequeo diario: %s", e)

# ========= Keep-Alive (Replit) =========
def maybe_keep_alive():
    """Levanta el webserver de keep-alive si existe (Replit)."""
    try:
        from keep_alive import keep_alive
        keep_alive()
        logging.info("keep_alive activo en :8080")
    except Exception as e:
        logging.info("keep_alive no disponible (%s). Continuando sin él.", e)

# ========= Arranque =========
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN no está definido")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("link", cmd_link))
    app.add_handler(CommandHandler("renew", cmd_renew))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("linkraw", cmd_linkraw))
    app.add_handler(CommandHandler("checkperms", cmd_checkperms))

    app.add_handler(ChatJoinRequestHandler(on_join_request))

    try:
        app.job_queue.run_daily(
            daily_check,
            time=dtime(hour=9, minute=0, tzinfo=TZ),
            name="daily_check"
        )
    except Exception as e:
        logging.warning("JobQueue no disponible: %s", e)

    # muy IMPORTANTE para UptimeRobot/Replit:
    maybe_keep_alive()

    logging.info("Bot iniciando polling...")
    app.run_polling(allowed_updates=["message", "chat_join_request"])

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("FATAL:", e)
        traceback.print_exc()
        input("Presiona ENTER para salir...")
