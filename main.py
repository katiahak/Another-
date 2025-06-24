import os
import asyncio
import base58
import json
import time
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
    # Render passes the PORT environment variable; use it.
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
MAX_WALLETS_PER_USER = 50
POLL_INTERVAL        = float(os.getenv("POLL_INTERVAL", "30"))  # seconds
TOKEN_PROGRAM_ID     = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"

# --- Logging ---
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- State ---
user_wallets = {}   # user_id -> list of wallet addresses
wallet_state  = {}   # wallet_address -> dict with SOL balance and token balances
wallet_tasks  = {}   # wallet_address -> asyncio.Task

# --- Utilities ---
def is_valid_address(addr: str) -> bool:
    try:
        raw = base58.b58decode(addr.strip())
        return len(raw) == 32
    except Exception:
        return False

# RPC HTTP helpers using requests
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
    # For getTokenAccountsByOwner, we pass the address and the token program id as strings.
    params = [addr, {"programId": TOKEN_PROGRAM_ID}, {"encoding": "jsonParsed"}]
    res = rpc_request("getTokenAccountsByOwner", params)
    if res:
        return res.get("value", [])
    return []

# --- Monitoring per wallet ---
async def monitor_wallet(addr: str, bot, chat_id: int):
    # Initialize state – fetch initial SOL balance and SPL token accounts
    sol_balance = await fetch_balance(addr)
    token_accounts = await fetch_token_accounts(addr)
    token_balances = {}
    for item in token_accounts:
        info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
        mint = info.get("mint")
        amt = info.get("tokenAmount", {}).get("uiAmount") or 0
        if mint and amt > 0:
            token_balances[mint] = amt
    wallet_state[addr] = {"sol": sol_balance, "tokens": token_balances}
    logger.info(f"Initial state for {addr}: SOL={sol_balance}, tokens={list(token_balances.keys())}")

    # --- SOL Monitoring: Websocket subscription if available, otherwise polling
    async def sol_ws():
        nonlocal sol_balance
        while True:
            try:
                async with websockets.connect(RPC_WS_URL) as ws:
                    req = {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "accountSubscribe",
                        "params": [addr, {"encoding": "base64"}]
                    }
                    await ws.send(json.dumps(req))
                    await ws.recv()
                    async for message in ws:
                        msg = json.loads(message)
                        params = msg.get("params", {})
                        result = params.get("result", {})
                        value = result.get("value", {})
                        lamports = value.get("lamports")
                        if lamports is None:
                            continue
                        new_bal = lamports
                        diff = new_bal - sol_balance
                        if diff != 0:
                            sol = diff/1e9
                            verb = "Received" if sol > 0 else "Sent"
                            text = f"{verb} {abs(sol):.9f} SOL on {addr}"
                            await bot.send_message(chat_id=chat_id, text=text)
                            sol_balance = new_bal
            except Exception as e:
                logger.error(f"WS error for {addr}: {e}")
                await asyncio.sleep(5)

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

    # --- SPL Token Monitoring (Polling)
    async def spl_poll():
        nonlocal token_balances
        while True:
            accounts = await fetch_token_accounts(addr)
            curr = {}
            for item in accounts:
                info = item.get("account", {}).get("data", {}).get("parsed", {}).get("info", {})
                mint = info.get("mint")
                amt = info.get("tokenAmount", {}).get("uiAmount") or 0
                if mint:
                    curr[mint] = amt
            for mint, amt in curr.items():
                prev_amt = token_balances.get(mint, 0)
                if mint not in token_balances and amt > 0:
                    text = f"New token acquired on {addr}: {mint}, amount: {amt}"
                    await bot.send_message(chat_id=chat_id, text=text)
                elif amt > prev_amt:
                    diff = amt - prev_amt
                    text = f"Received {diff} of token {mint} on {addr}"
                    await bot.send_message(chat_id=chat_id, text=text)
                elif amt < prev_amt:
                    diff = prev_amt - amt
                    text = f"Sent {diff} of token {mint} on {addr}"
                    await bot.send_message(chat_id=chat_id, text=text)
            token_balances = {m: a for m, a in curr.items() if a > 0}
            await asyncio.sleep(POLL_INTERVAL)

    tasks = []
    if USE_WEBSOCKETS:
        tasks.append(asyncio.create_task(sol_ws()))
    else:
        tasks.append(asyncio.create_task(sol_poll()))
    tasks.append(asyncio.create_task(spl_poll()))
    await asyncio.gather(*tasks)

# --- Telegram Bot Handlers ---
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != AUTHORIZED_USER_ID:
        return
    await update.message.reply_text("Send a Solana address to track.")

async def handle_wallet(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != AUTHORIZED_USER_ID:
        return
    addr = (update.message.text or "").strip()
    if not is_valid_address(addr):
        await update.message.reply_text("Invalid address.")
        return
    wallets = user_wallets.setdefault(uid, [])
    if addr in wallets:
        await update.message.reply_text(f"Already tracking {addr}")
        return
    if len(wallets) >= MAX_WALLETS_PER_USER:
        await update.message.reply_text(f"Max {MAX_WALLETS_PER_USER} wallets reached")
        return
    wallets.append(addr)
    task = asyncio.create_task(monitor_wallet(addr, ctx.bot, uid))
    wallet_tasks[addr] = task
    await update.message.reply_text(f"Now tracking {addr}")

async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != AUTHORIZED_USER_ID:
        return
    for addr in user_wallets.get(uid, []):
        task = wallet_tasks.get(addr)
        if task:
            task.cancel()
    user_wallets[uid] = []
    await update.message.reply_text("Stopped all monitoring.")

# --- Main Entrypoint ---
def main():
    # Start the uptime web server thread
    threading.Thread(target=run_web_server, daemon=True).start()
    # Build and start the Telegram bot
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_wallet))
    logger.info("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
