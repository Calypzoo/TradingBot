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
GRID_LEVELS   = 16
GRID_SPREAD   = 0.00010      # 0.010%
ORDER_AMOUNT  = 10           # $10 per grid level

# ---- SAFETY SETTINGS ----
STOP_LOSS_PCT = 0.15         # Stop everything if BTC drops 15% from start
MAX_SPEND     = 400          # Never spend more than $400 total

# ---- CONNECT TO BINANCE ----
api_key    = os.getenv('API_KEY')
api_secret = os.getenv('API_SECRET')

print(f"DEBUG: API_KEY loaded = {'YES' if api_key else 'NO - EMPTY!'}")
print(f"DEBUG: API_SECRET loaded = {'YES' if api_secret else 'NO - EMPTY!'}")

exchange = ccxt.binance({
    'apiKey' : api_key,
    'secret' : api_secret,
    'options': {'defaultType': 'spot'},
})
print("RUNNING LIVE - Real money active")

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

# ---- SELL ALL BTC (Emergency exit) ----
def emergency_sell_all(current_price):
    try:
        usdt_balance, btc_balance = get_balance()
        if btc_balance > 0.0001:
            btc_to_sell = round(btc_balance * 0.999, 5)
            order = exchange.create_market_sell_order(SYMBOL, btc_to_sell)
            log(f"EMERGENCY SELL: Sold {btc_to_sell} BTC at ~${current_price:,.0f}")
            return True
    except Exception as e:
        log(f"EMERGENCY SELL FAILED: {e}")
    return False

# ---- MAIN BOT LOOP ----
def run_bot():
    log("=" * 50)
    log("GRID LIVE BOT STARTED")
    log(f"Symbol: {SYMBOL} | Levels: {GRID_LEVELS} | Spread: {GRID_SPREAD*100:.3f}%")
    log(f"Order Amount: ${ORDER_AMOUNT} per level")
    log(f"Stop Loss: {STOP_LOSS_PCT*100:.0f}% drop from start price")
    log(f"Max Spend: ${MAX_SPEND}")
    log("=" * 50)

    # Load or build grid state
    state = load_state()
    if state:
        log(f"Loaded existing grid centered at ${state['center_price']:,.0f}")
        grid         = state['grid']
        center_price = state['center_price']
        last_price   = state['last_price']
        total_spent  = state.get('total_spent', 0)
    else:
        ticker       = exchange.fetch_ticker(SYMBOL)
        center_price = ticker['last']
        grid         = build_grid(center_price)
        last_price   = center_price
        total_spent  = 0
        log(f"New grid built around ${center_price:,.0f}")
        save_state({
            'center_price': center_price,
            'grid'        : grid,
            'last_price'  : last_price,
            'total_spent' : total_spent
        })

    # Calculate stop loss price
    stop_loss_price = center_price * (1 - STOP_LOSS_PCT)
    log(f"Stop loss price set at ${stop_loss_price:,.0f} (15% below ${center_price:,.0f})")

    # Print grid levels
    log("Grid levels:")
    for level in grid:
        log(f"  ${level['price']:,.2f} --- {level['status']}")

    while True:
        try:
            ticker        = exchange.fetch_ticker(SYMBOL)
            current_price = ticker['last']
            usdt_balance, btc_balance = get_balance()

            log(f"Price: ${current_price:,.2f} | Balance: ${usdt_balance:,.2f} USDT | {btc_balance:.6f} BTC | Spent: ${total_spent:,.2f}")

            # ---- STOP LOSS CHECK ----
            if current_price <= stop_loss_price:
                log("!" * 50)
                log(f"STOP LOSS TRIGGERED!")
                log(f"BTC dropped to ${current_price:,.0f} which is below stop loss of ${stop_loss_price:,.0f}")
                log(f"Selling all BTC and pausing bot...")
                log("!" * 50)
                emergency_sell_all(current_price)
                log("Bot paused. Restart manually when market recovers.")
                log("Check your Binance account and reassess before restarting.")
                break

            # ---- MAX SPEND CHECK ----
            if total_spent >= MAX_SPEND:
                log(f"Max spend limit of ${MAX_SPEND} reached. Not placing new buys.")

            for level in grid:
                grid_price = level['price']

                # BUY when price drops to grid level
                if (current_price <= grid_price < last_price
                        and level['status'] == 'ready'
                        and usdt_balance >= ORDER_AMOUNT
                        and total_spent < MAX_SPEND):
                    log(f"BUY at grid level ${grid_price:,.2f}")
                    order = place_order('buy', ORDER_AMOUNT, current_price)
                    if order:
                        level['status']    = 'bought'
                        level['buy_price'] = current_price
                        total_spent       += ORDER_AMOUNT

                # SELL when price rises back above grid level
                elif (current_price >= grid_price > last_price
                        and level['status'] == 'bought'):
                    btc_to_sell = ORDER_AMOUNT / level.get('buy_price', grid_price)
                    log(f"SELL at grid level ${grid_price:,.2f}")
                    order = place_order('sell', btc_to_sell * current_price, current_price)
                    if order:
                        level['status'] = 'ready'
                        total_spent     = max(0, total_spent - ORDER_AMOUNT)
                        pnl = (current_price - level.get('buy_price', grid_price)) / level.get('buy_price', grid_price) * 100
                        log(f"Grid cycle complete! PnL: {pnl:.2f}%")

            last_price = current_price

            # Save state after every check
            save_state({
                'center_price': center_price,
                'grid'        : grid,
                'last_price'  : last_price,
                'total_spent' : total_spent
            })

        except Exception as e:
            log(f"ERROR: {e}")

        # Check every 5 minutes
        log(f"Sleeping 5 minutes...")
        log("-" * 50)
        time.sleep(300)

# ---- RUN ----
if __name__ == '__main__':
    run_bot()
