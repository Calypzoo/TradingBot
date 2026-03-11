import ccxt
import pandas as pd
import numpy as np
import time
import os
import json
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ---- SETTINGS ----
SYMBOL        = 'BTC/USDT'
TESTNET       = True         # Set to False for real trading

GRID_LEVELS   = 28
GRID_SPREAD   = 0.00030      # 0.030%
ORDER_AMOUNT  = 50           # $ per grid level

# ---- CONNECT TO BINANCE ----
if TESTNET:
    exchange = ccxt.binance({
        'apiKey' : os.getenv('TESTNET_API_KEY'),
        'secret' : os.getenv('TESTNET_API_SECRET'),
        'options': {'defaultType': 'spot'},
    })
    exchange.set_sandbox_mode(True)
    print("RUNNING ON TESTNET - No real money at risk")
else:
    exchange = ccxt.binance({
        'apiKey' : os.getenv('API_KEY'),
        'secret' : os.getenv('API_SECRET'),
        'options': {'defaultType': 'spot'},
    })
    print("RUNNING LIVE - Real money at risk!")

# ---- LOGGING ----
def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line      = f"[{timestamp}] {msg}"
    print(line)
    with open('grid_live_log.txt', 'a', encoding='utf-8') as f:
        f.write(line + '\n')

# ---- SAVE/LOAD GRID STATE ----
def save_state(state):
    with open('grid_state.json', 'w') as f:
        json.dump(state, f, indent=2)

def load_state():
    try:
        with open('grid_state.json', 'r') as f:
            return json.load(f)
    except:
        return None

# ---- BUILD GRID ----
def build_grid(center_price):
    grid = []
    for i in range(-GRID_LEVELS // 2, GRID_LEVELS // 2 + 1):
        price = round(center_price * (1 + i * GRID_SPREAD), 2)
        grid.append({'price': price, 'status': 'ready'})
    return sorted(grid, key=lambda x: x['price'])

# ---- GET BALANCE ----
def get_balance():
    balance = exchange.fetch_balance()
    usdt    = balance['USDT']['free']
    btc     = balance['BTC']['free']
    return usdt, btc

# ---- PLACE ORDER ----
def place_order(side, amount_usdt, price):
    try:
        btc_amount = round(amount_usdt / price, 5)
        if side == 'buy':
            order = exchange.create_market_buy_order(SYMBOL, btc_amount)
        else:
            order = exchange.create_market_sell_order(SYMBOL, btc_amount)
        log(f"ORDER PLACED: {side.upper()} {btc_amount} BTC at ~${price:,.0f}")
        return order
    except Exception as e:
        log(f"ORDER FAILED: {e}")
        return None

# ---- MAIN BOT LOOP ----
def run_bot():
    log("=" * 50)
    log("GRID LIVE BOT STARTED")
    log(f"Mode: {'TESTNET' if TESTNET else 'LIVE'}")
    log(f"Symbol: {SYMBOL} | Levels: {GRID_LEVELS} | Spread: {GRID_SPREAD*100:.3f}%")
    log(f"Order Amount: ${ORDER_AMOUNT} per level")
    log("=" * 50)

    # Load or build grid state
    state = load_state()
    if state:
        log(f"Loaded existing grid centered at ${state['center_price']:,.0f}")
        grid         = state['grid']
        center_price = state['center_price']
        last_price   = state['last_price']
    else:
        ticker       = exchange.fetch_ticker(SYMBOL)
        center_price = ticker['last']
        grid         = build_grid(center_price)
        last_price   = center_price
        log(f"New grid built around ${center_price:,.0f}")
        save_state({'center_price': center_price, 'grid': grid, 'last_price': last_price})

    # Print grid levels
    log("Grid levels:")
    for level in grid:
        log(f"  ${level['price']:,.2f} --- {level['status']}")

    while True:
        try:
            ticker        = exchange.fetch_ticker(SYMBOL)
            current_price = ticker['last']
            usdt_balance, btc_balance = get_balance()

            log(f"Price: ${current_price:,.2f} | Balance: ${usdt_balance:,.2f} USDT | {btc_balance:.6f} BTC")

            for level in grid:
                grid_price = level['price']

                # BUY when price drops to grid level
                if (current_price <= grid_price < last_price
                        and level['status'] == 'ready'
                        and usdt_balance >= ORDER_AMOUNT):
                    log(f"BUY at grid level ${grid_price:,.2f}")
                    order = place_order('buy', ORDER_AMOUNT, current_price)
                    if order:
                        level['status']    = 'bought'
                        level['buy_price'] = current_price

                # SELL when price rises back above grid level
                elif (current_price >= grid_price > last_price
                        and level['status'] == 'bought'):
                    btc_to_sell = ORDER_AMOUNT / level.get('buy_price', grid_price)
                    log(f"SELL at grid level ${grid_price:,.2f}")
                    order = place_order('sell', btc_to_sell * current_price, current_price)
                    if order:
                        level['status'] = 'ready'
                        pnl = (current_price - level.get('buy_price', grid_price)) / level.get('buy_price', grid_price) * 100
                        log(f"Grid cycle complete! PnL: {pnl:.2f}%")

            last_price = current_price

            # Save state after every check
            save_state({'center_price': center_price, 'grid': grid, 'last_price': last_price})

        except Exception as e:
            log(f"ERROR: {e}")

        # Check every 5 minutes
        log(f"Sleeping 5 minutes...")
        log("-" * 50)
        time.sleep(300)

# ---- RUN ----
if __name__ == '__main__':
    run_bot()