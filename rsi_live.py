import ccxt
import pandas as pd
import numpy as np
import time
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ---- SETTINGS ----
SYMBOL        = 'BTC/USDT'
TIMEFRAME     = '4h'
TRADE_AMOUNT  = 100          # $ per trade
TESTNET       = True         # Set to False for real trading

RSI_PERIOD    = 14
RSI_BUY       = 32
RSI_SELL      = 68
STOP_LOSS     = 0.03
TAKE_PROFIT   = 0.08
COOLDOWN_HRS  = 72
EMA_FAST      = 9
EMA_SLOW      = 21

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
    with open('rsi_live_log.txt', 'a', encoding='utf-8') as f:
        f.write(line + '\n')

# ---- CALCULATE RSI ----
def calculate_rsi(closes, period=14):
    closes = pd.Series(closes)
    delta  = closes.diff()
    gain   = delta.where(delta > 0, 0).rolling(period).mean()
    loss   = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs     = gain / loss
    rsi    = 100 - (100 / (1 + rs))
    return rsi.iloc[-1]

# ---- GET MARKET DATA ----
def get_data():
    ohlcv         = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    df            = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    closes        = df['close'].values
    rsi           = calculate_rsi(closes, RSI_PERIOD)
    ema_fast      = pd.Series(closes).ewm(span=EMA_FAST, adjust=False).mean().iloc[-1]
    ema_slow      = pd.Series(closes).ewm(span=EMA_SLOW, adjust=False).mean().iloc[-1]
    current_price = closes[-1]
    return current_price, rsi, ema_fast, ema_slow

# ---- GET BALANCE ----
def get_balance():
    balance = exchange.fetch_balance()
    usdt    = balance['USDT']['free']
    btc     = balance['BTC']['free']
    return usdt, btc

# ---- PLACE ORDER ----
def place_order(side, amount_usdt, price):
    try:
        btc_amount = amount_usdt / price
        btc_amount = round(btc_amount, 5)
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
    log("RSI LIVE BOT STARTED")
    log(f"Mode: {'TESTNET' if TESTNET else 'LIVE'}")
    log(f"Symbol: {SYMBOL} | Timeframe: {TIMEFRAME}")
    log(f"RSI Buy: {RSI_BUY} | RSI Sell: {RSI_SELL}")
    log(f"Stop Loss: {STOP_LOSS*100}% | Take Profit: {TAKE_PROFIT*100}%")
    log("=" * 50)

    in_position    = False
    entry_price    = 0
    last_stop_time = None

    while True:
        try:
            current_price, rsi, ema_fast, ema_slow = get_data()
            usdt_balance, btc_balance = get_balance()

            log(f"Price: ${current_price:,.2f} | RSI: {rsi:.1f} | EMA Fast: ${ema_fast:,.0f} | EMA Slow: ${ema_slow:,.0f}")
            log(f"Balance: ${usdt_balance:,.2f} USDT | {btc_balance:.6f} BTC")

            in_position = btc_balance * current_price > 10

            if not in_position:
                if last_stop_time:
                    hours_since_stop = (datetime.now() - last_stop_time).seconds / 3600
                    if hours_since_stop < COOLDOWN_HRS:
                        log(f"Cooldown active: {COOLDOWN_HRS - hours_since_stop:.1f} hours remaining")
                        time.sleep(3600)
                        continue

                if (rsi < RSI_BUY and
                        ema_fast > ema_slow and
                        usdt_balance >= TRADE_AMOUNT):
                    log(f"BUY SIGNAL! RSI: {rsi:.1f} | EMA Fast > EMA Slow")
                    place_order('buy', TRADE_AMOUNT, current_price)
                    entry_price = current_price
                else:
                    log(f"No buy signal. RSI needs to be < {RSI_BUY} (currently {rsi:.1f})")

            else:
                stop = entry_price * (1 - STOP_LOSS) if entry_price > 0 else 0
                tp   = entry_price * (1 + TAKE_PROFIT) if entry_price > 0 else 0

                log(f"In position | Entry: ${entry_price:,.0f} | Stop: ${stop:,.0f} | TP: ${tp:,.0f}")

                sell_reason = None
                if entry_price > 0 and current_price <= stop:
                    sell_reason    = 'STOP LOSS'
                    last_stop_time = datetime.now()
                elif entry_price > 0 and current_price >= tp:
                    sell_reason = 'TAKE PROFIT'
                elif rsi > RSI_SELL:
                    sell_reason = 'RSI OVERBOUGHT'

                if sell_reason:
                    btc_to_sell = round(btc_balance * 0.99, 5)
                    log(f"SELL SIGNAL: {sell_reason}")
                    place_order('sell', btc_to_sell * current_price, current_price)
                    entry_price = 0
                else:
                    log(f"Holding position. RSI: {rsi:.1f}")

        except Exception as e:
            log(f"ERROR: {e}")

        log(f"Sleeping 1 hour until next check...")
        log("-" * 50)
        time.sleep(3600)

# ---- RUN ----
if __name__ == '__main__':
    run_bot()