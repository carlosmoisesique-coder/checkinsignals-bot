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

async def must_admin(update: Update) -> bool:
    if not ADMIN_IDS or 0 in ADMIN_IDS: return True
    uid = update.effective_user.id if update.effective_user else 0
    if uid not in ADMIN_IDS:
        if update.message:
            await update.message.reply_text("No tienes permiso para este comando.")
        return False
    return True

def fmt_fecha(ts: int): return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/link @usuario DIAS\n/renew @usuario DIAS\n/list\n/check\n/ping"
    )

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong")

async def cmd_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await must_admin(update): return
    if not CHANNEL_ID:
        return await update.message.reply_text("Falta CHANNEL_ID")
    if len(context.args) < 2:
        return await update.message.reply_text("Uso: /link @usuario DIAS")
    username = context.args[0].lstrip("@").lower()
    dias = int(context.args[1])
    expire_unix = int((now_cl() + timedelta(hours=LINK_VALID_HOURS)).timestamp())
    invite = await context.bot.create_chat_invite_link(
        chat_id=CHANNEL_ID,
        expire_date=expire_unix,
        member_limit=1,
        creates_join_request=True
    )
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO links(invite_link, username, invite_expire_ts, plan_days) VALUES(?,?,?,?)",
                     (invite.invite_link, username, expire_unix, dias))
    await update.message.reply_text(
        f"🔗 Link para @{username}: {invite.invite_link}\nVálido {LINK_VALID_HOURS}h"
    )

async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # tu lógica para aceptar/rechazar

