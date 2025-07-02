import os
import asyncio
import base58
import json
import threading
import logging
from datetime import datetime
import requests

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from fastapi import FastAPI
import uvicorn

# --- FASTAPI for uptime ---
fast_app = FastAPI()

@fast_app.get("/")
async def root():
    return {"status": "OK"}

def run_web_server():
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(fast_app, host="0.0.0.0", port=port)

# --- Configuration ---
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "7743232071:AAFeRJbJGbbyYrcg3v2BKsuVVRgj17Eat-c")
AUTHORIZED_USER_ID   = int(os.getenv("AUTHORIZED_USER_ID", "7683338204"))
RPC_HTTP_URL         = os.getenv("RPC_HTTP_URL", "https://api.mainnet-beta.solana.com")
RPC_WS_URL           = os.getenv("RPC_WS_URL", "wss://api.mainnet-beta.solana.com")
USE_WEBSOCKETS       = False
try:
    import websockets
    USE_WEBSOCKETS = True
except ImportError:
    USE_WEBSOCKETS = False
MAX_WALLETS_PER_USER = 100
POLL_INTERVAL        = float(os.environ.get("POLL_INTERVAL", "30"))  # seconds
TOKEN_PROGRAM_ID     = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
DATA_FILE            = "wallets.json"

# --- Logging ---
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- State ---
user_wallets = {}   # user_id -> list of wallet addresses
wallet_state  = {}   # wallet_address -> dict with SOL balance and token balances
wallet_tasks  = {}   # wallet_address -> asyncio.Task

# --- Persistence ---
def load_data():
    global user_wallets
    try:
        with open(DATA_FILE, "r") as f:
            user_wallets = json.load(f)
        logger.info("Loaded wallet data for users: %s", list(user_wallets.keys()))
    except FileNotFoundError:
        user_wallets = {}

def save_data():
    with open(DATA_FILE, "w") as f:
        json.dump(user_wallets, f)
    logger.info("Saved wallet data.")

# --- Utilities ---
def is_valid_address(addr: str) -> bool:
    try:
        raw = base58.b58decode(addr.strip())
        return len(raw) == 32
    except Exception:
        return False

def rpc_request(method: str, params: list):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = requests.post(RPC_HTTP_URL, json=payload, timeout=10)
        return r.json().get("result")
    except Exception as e:
        logger.error(f"RPC request error {method}: {e}")
        return None

async def fetch_balance(addr: str) -> int:
    res = rpc_request("getBalance", [addr])
    if res:
        return res.get("value", 0)
    return 0

async def fetch_token_accounts(addr: str):
    params = [addr, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}]
    res = rpc_request("getTokenAccountsByOwner", params)
    if res:
        return res.get("value", [])
    return []

# --- Monitoring per wallet ---
async def monitor_wallet(addr: str, bot, chat_id: int):
    sol_balance = await fetch_balance(addr)
    token_accounts = await fetch_token_accounts(addr)
    token_balances = {item["account"]["data"]["parsed"]["info"]["mint"]:
                      item["account"]["data"]["parsed"]["info"]["tokenAmount"]["uiAmount"]
                      for item in token_accounts if item.get("account")}
    wallet_state[addr] = {"sol": sol_balance, "tokens": token_balances}
    logger.info(f"Initial state for {addr}: SOL={sol_balance}, tokens={list(token_balances.keys())}")

    async def sol_poll():
        nonlocal sol_balance
        while True:
            new_bal = await fetch_balance(addr)
            diff = new_bal - sol_balance
            if diff != 0:
                sol = diff/1e9
                verb = "Received" if sol > 0 else "Sent"
                text = f"{verb} {abs(sol):.9f} SOL on {addr}"
                await bot.send_message(chat_id=chat_id, text=text)
                sol_balance = new_bal
            await asyncio.sleep(POLL_INTERVAL)

    async def spl_poll():
        nonlocal token_balances
        MIN_AMOUNT = 5000
        while True:
            accounts = await fetch_token_accounts(addr)
            curr = {}
            for item in accounts:
                info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                mint = info.get("mint")
                amt = info.get("tokenAmount", {}).get("uiAmount") or 0
                curr[mint] = amt

            for mint, amt in curr.items():
                prev_amt = token_balances.get(mint, 0)
                if mint not in token_balances and amt > MIN_AMOUNT:
                    await bot.send_message(chat_id=chat_id, text=f"New token acquired on {addr}: {mint}, amount: {amt}")
                elif amt > prev_amt and (amt - prev_amt) > MIN_AMOUNT:
                    diff = amt - prev_amt
                    await bot.send_message(chat_id=chat_id, text=f"Received {diff} of token {mint} on {addr}")
                elif amt < prev_amt and (prev_amt - amt) > MIN_AMOUNT:
                    diff = prev_amt - amt
                    await bot.send_message(chat_id=chat_id, text=f"Sent {diff} of token {mint} on {addr}")

            token_balances = {m: a for m, a in curr.items() if a > 0}
            await asyncio.sleep(POLL_INTERVAL)

    tasks = [asyncio.create_task(sol_poll()), asyncio.create_task(spl_poll())]
    wallet_tasks[addr] = tasks
    await asyncio.gather(*tasks)

# --- Supervisor to auto-restart monitors ---
async def ensure_monitored(addr: str, bot, chat_id: int):
    while True:
        try:
            await monitor_wallet(addr, bot, chat_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("monitor_wallet for %s crashed: %s. Restarting in 5s", addr, e)
            await asyncio.sleep(5)

# --- Telegram Bot Handlers ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text("Send a Solana address to track.")

async def handle_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != AUTHORIZED_USER_ID:
        return
    addr = update.message.text.strip()
    if not is_valid_address(addr):
        await update.message.reply_text("Invalid address.")
        return
    wallets = user_wallets.setdefault(str(uid), [])
    if addr in wallets:
        await update.message.reply_text(f"Already tracking {addr}")
        return
    if len(wallets) >= MAX_WALLETS_PER_USER:
        await update.message.reply_text(f"Max {MAX_WALLETS_PER_USER} wallets reached")
        return
    wallets.append(addr)
    save_data()
    task = asyncio.create_task(ensure_monitored(addr, ctx.bot, uid))
    wallet_tasks[addr] = task
    await update.message.reply_text(f"Now tracking {addr}")

async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != AUTHORIZED_USER_ID:
        return
    for addr in user_wallets.get(str(uid), []):
        task = wallet_tasks.get(addr)
        if task:
            task.cancel()
    user_wallets[str(uid)] = []
    save_data()
    await update.message.reply_text("Stopped all monitoring.")

# --- Main Entrypoint ---
def main():
    load_data()
    threading.Thread(target=run_web_server, daemon=True).start()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet))
    for uid, wallets in user_wallets.items():
        for addr in wallets:
            task = asyncio.create_task(ensure_monitored(addr, app.bot, int(uid)))
            wallet_tasks[addr] = task
    logger.info("Bot running with persisted wallets: %s", user_wallets)
    app.run_polling()

if __name__ == "__main__":
    main()
    
