#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sqlite3, time, asyncio
from datetime import datetime, timedelta
import pytz
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, ChatJoinRequestHandler
from telegram.error import Forbidden

BOT_TOKEN  = os.getenv("BOT_TOKEN", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
if CHANNEL_ID and CHANNEL_ID.lstrip("-").isdigit():
    CHANNEL_ID = int(CHANNEL_ID)
ADMIN_IDS  = {int(x) for x in (os.getenv("ADMIN_IDS") or "0").split(",") if x.strip().isdigit()}
LINK_VALID_HOURS = int(os.getenv("LINK_VALID_HOURS", "48"))
TZ = pytz.timezone(os.getenv("TZ", "America/Santiago"))
DB = os.getenv("DB_PATH", "/data/suscriptores.db")

def now_cl():
    return datetime.now(TZ)

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

# --- join request: aprobar automáticamente ---
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.chat_join_request:
        return
    # Solo el canal configurado
    if CHANNEL_ID and update.chat_join_request.chat.id != CHANNEL_ID:
        return

    # Aprueba la solicitud
    await update.chat_join_request.approve()

    # Intenta mandar DM de bienvenida (fallará si nunca habló con el bot)
    try:
        await context.bot.send_message(
            chat_id=update.chat_join_request.from_user.id,
            text="✅ Acceso aprobado, bienvenido al canal."
        )
    except Forbidden:
        # el usuario no inició chat con el bot; ignora
        pass


# --- arranque del bot: registra handlers y queda escuchando ---
def main():
    token = BOT_TOKEN or os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN no está definido")

