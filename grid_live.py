import ccxt
import time
import os
import json
import urllib.request
import urllib.parse
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ================================================================
# SETTINGS
# ================================================================
SYMBOL         = 'BTC/USDC'
TIMEFRAME      = '1h'
ORDER_AMOUNT   = 10            # $10 per grid level
MAX_SPEND      = 400           # never spend more than $400
STOP_LOSS_PCT  = 0.12          # 12% drop from grid center = emergency exit
CHECK_INTERVAL = 300           # check every 5 minutes
RESTART_WAIT   = 600           # wait 10 min after stop loss before restarting

# Volatility grid settings (auto-adjusted based on ATR ratio)
GRID_LEVELS_CALM     = 16      # tight calm market
GRID_LEVELS_NORMAL   = 12      # normal market
GRID_LEVELS_VOLATILE = 8       # wide volatile market

GRID_SPREAD_CALM     = 0.00015 # 0.015%
GRID_SPREAD_NORMAL   = 0.00020 # 0.020%
GRID_SPREAD_VOLATILE = 0.00050 # 0.050%

ATR_CALM_THRESHOLD   = 0.7     # ATR ratio below this = calm
ATR_VOLATILE_THRESHOLD = 1.5   # ATR ratio above this = volatile

# Telegram
TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ================================================================
# CONNECT
# ================================================================
api_key    = os.getenv('API_KEY')
api_secret = os.getenv('API_SECRET')

print(f"DEBUG: API_KEY    = {'YES' if api_key else 'NO - EMPTY!'}")
print(f"DEBUG: API_SECRET = {'YES' if api_secret else 'NO - EMPTY!'}")
print(f"DEBUG: TELEGRAM   = {'YES' if TELEGRAM_TOKEN else 'NOT SET'}")

exchange = ccxt.binance({
    'apiKey' : api_key,
    'secret' : api_secret,
    'options': {'defaultType': 'spot'},
})
print("RUNNING LIVE - Real money active")

# ================================================================
# LOGGING
# ================================================================
def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line      = f"[{timestamp}] {msg}"
    print(line)
    with open('bot_log.txt', 'a', encoding='utf-8') as f:
        f.write(line + '\n')

# ================================================================
# TELEGRAM
# ================================================================
def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        text = urllib.parse.quote(f"BTC Bot\n{msg}")
        url  = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={text}"
        urllib.request.urlopen(url, timeout=5)
    except Exception as e:
        log(f"Telegram failed: {e}")

# ================================================================
# STATE & STATS
# ================================================================
def save_state(state):
    with open('bot_state.json', 'w') as f:
        json.dump(state, f, indent=2)

def load_state():
    try:
        with open('bot_state.json', 'r') as f:
            return json.load(f)
    except:
        return None

def clear_state():
    try:
        os.remove('bot_state.json')
    except:
        pass

def load_stats():
    try:
        with open('bot_stats.json', 'r') as f:
            return json.load(f)
    except:
        return {
            'start_balance' : None,
            'start_time'    : datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_profit'  : 0.0,
            'total_cycles'  : 0,
            'total_buys'    : 0,
            'total_sells'   : 0,
            'stop_losses'   : 0,
            'recenters'     : 0,
            'last_mode'     : None,
        }

def save_stats(stats):
    with open('bot_stats.json', 'w') as f:
        json.dump(stats, f, indent=2)

# ================================================================
# MARKET DATA & INDICATORS
# ================================================================
def get_candles(limit=60):
    ohlcv  = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]
    return closes, highs, lows

def calc_atr(closes, highs, lows, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i]  - closes[i-1])
        )
        trs.append(tr)
    if len(trs) < period:
        return None, None
    atr_current = sum(trs[-period:]) / period
    atr_avg     = sum(trs[-50:]) / min(50, len(trs))
    return atr_current, atr_current / atr_avg if atr_avg > 0 else 1.0

def get_volatility_mode(atr_ratio):
    if atr_ratio is None:
        return 'NORMAL', GRID_LEVELS_NORMAL, GRID_SPREAD_NORMAL
    if atr_ratio < ATR_CALM_THRESHOLD:
        return 'CALM', GRID_LEVELS_CALM, GRID_SPREAD_CALM
    elif atr_ratio > ATR_VOLATILE_THRESHOLD:
        return 'VOLATILE', GRID_LEVELS_VOLATILE, GRID_SPREAD_VOLATILE
    else:
        return 'NORMAL', GRID_LEVELS_NORMAL, GRID_SPREAD_NORMAL

def get_balance():
    balance = exchange.fetch_balance()
    return balance['USDC']['free'], balance['BTC']['free']

# ================================================================
# GRID HELPERS
# ================================================================
def build_grid(center, levels, spread):
    grid = []
    for i in range(-levels // 2, levels // 2 + 1):
        price = round(center * (1 + i * spread), 2)
        grid.append({'price': price, 'status': 'ready', 'buy_price': None})
    return sorted(grid, key=lambda x: x['price'])

def grid_out_of_range(price, grid):
    low  = grid[0]['price']
    high = grid[-1]['price']
    margin = (high - low) * 0.1
    return price < (low - margin) or price > (high + margin)

# ================================================================
# ORDERS
# ================================================================
def place_order(side, amount_usdc, price):
    try:
        btc_amount = round(amount_usdc / price, 5)
        if btc_amount < 0.00001:
            log(f"ORDER SKIPPED: too small")
            return None
        if side == 'buy':
            order = exchange.create_market_buy_order(SYMBOL, btc_amount)
        else:
            order = exchange.create_market_sell_order(SYMBOL, btc_amount)
        log(f"ORDER OK: {side.upper()} {btc_amount} BTC @ ~${price:,.0f}")
        return order
    except Exception as e:
        log(f"ORDER FAILED: {e}")
        return None

def sell_all_btc(price, reason=""):
    try:
        _, btc = get_balance()
        if btc > 0.00001:
            order = exchange.create_market_sell_order(SYMBOL, round(btc * 0.999, 5))
            log(f"SELL ALL: {round(btc*0.999,5)} BTC @ ~${price:,.0f} | {reason}")
            return True
        log("SELL ALL: Nothing to sell")
        return True
    except Exception as e:
        log(f"SELL ALL FAILED: {e}")
        return False

# ================================================================
# HOURLY SUMMARY
# ================================================================
def print_summary(stats, usdc, btc, price, vol_mode):
    total = usdc + btc * price
    pnl   = total - stats['start_balance'] if stats['start_balance'] else 0
    pct   = pnl / stats['start_balance'] * 100 if stats['start_balance'] else 0
    log("=" * 55)
    log("HOURLY SUMMARY")
    log(f"  Volatility mode : {vol_mode}")
    log(f"  BTC price       : ${price:,.2f}")
    log(f"  USDC balance    : ${usdc:,.2f}")
    log(f"  BTC held        : {btc:.6f} (~${btc*price:,.2f})")
    log(f"  Total value     : ${total:,.2f}")
    log(f"  PnL             : ${pnl:+.2f} ({pct:+.2f}%)")
    log(f"  Cycles done     : {stats['total_cycles']}")
    log(f"  Total profit    : ${stats['total_profit']:+.4f}")
    log(f"  Stop losses     : {stats['stop_losses']}")
    log(f"  Grid recenters  : {stats['recenters']}")
    log("=" * 55)
    telegram(
        f"Hourly Update\n"
        f"Mode: {vol_mode}\n"
        f"BTC: ${price:,.0f}\n"
        f"Balance: ${total:,.2f}\n"
        f"PnL: ${pnl:+.2f} ({pct:+.2f}%)\n"
        f"Cycles: {stats['total_cycles']} | Profit: ${stats['total_profit']:+.2f}"
    )

# ================================================================
# MAIN BOT LOOP
# ================================================================
def run_session(stats):
    log("-" * 55)
    log("STARTING NEW SESSION")

    # Get market data
    closes, highs, lows = get_candles(limit=60)
    current_price       = closes[-1]
    atr, atr_ratio      = calc_atr(closes, highs, lows)
    vol_mode, levels, spread = get_volatility_mode(atr_ratio)

    log(f"Current price : ${current_price:,.2f}")
    log(f"ATR ratio     : {atr_ratio:.2f}" if atr_ratio else "ATR: calculating...")
    log(f"Volatility    : {vol_mode} | Levels: {levels} | Spread: {spread*100:.3f}%")

    # Load or build grid state
    state = load_state()
    if state:
        grid         = state['grid']
        center_price = state['center_price']
        last_price   = state['last_price']
        total_spent  = state.get('total_spent', 0)
        log(f"Loaded grid centered at ${center_price:,.0f}")
    else:
        center_price = current_price
        grid         = build_grid(center_price, levels, spread)
        last_price   = current_price
        total_spent  = 0
        log(f"New grid built | Center: ${center_price:,.0f} | Mode: {vol_mode}")

    stop_loss_price = center_price * (1 - STOP_LOSS_PCT)
    log(f"Stop loss at  : ${stop_loss_price:,.0f}")

    # Record start balance once
    if stats['start_balance'] is None:
        usdc, btc = get_balance()
        stats['start_balance'] = usdc + btc * current_price
        save_stats(stats)
        log(f"Start balance : ${stats['start_balance']:,.2f}")
        telegram(f"Bot started!\nBalance: ${stats['start_balance']:,.2f}\nBTC: ${current_price:,.0f}\nMode: {vol_mode}")

    save_state({
        'center_price': center_price,
        'grid'        : grid,
        'last_price'  : last_price,
        'total_spent' : total_spent,
    })

    last_summary_hour = -1
    last_vol_mode     = vol_mode

    while True:
        try:
            closes, highs, lows  = get_candles(limit=60)
            current_price        = closes[-1]
            atr, atr_ratio       = calc_atr(closes, highs, lows)
            vol_mode, levels, spread = get_volatility_mode(atr_ratio)
            usdc, btc            = get_balance()

            log(f"${current_price:,.2f} | Mode: {vol_mode} | "
                f"USDC: ${usdc:,.2f} | BTC: {btc:.6f} | "
                f"Spent: ${total_spent:,.2f} | ATR ratio: {atr_ratio:.2f}" if atr_ratio
                else f"${current_price:,.2f} | USDC: ${usdc:,.2f}")

            # Hourly summary
            current_hour = datetime.now().hour
            if current_hour != last_summary_hour:
                print_summary(stats, usdc, btc, current_price, vol_mode)
                last_summary_hour = current_hour

            # Volatility mode change — rebuild grid
            if vol_mode != last_vol_mode:
                log(f"VOLATILITY SHIFT: {last_vol_mode} -> {vol_mode}")
                telegram(f"Volatility shift!\n{last_vol_mode} -> {vol_mode}\nBTC: ${current_price:,.0f}")
                sell_all_btc(current_price, "vol mode change")
                time.sleep(3)
                usdc, btc    = get_balance()
                center_price = current_price
                grid         = build_grid(center_price, levels, spread)
                last_price   = current_price
                total_spent  = 0
                stop_loss_price = center_price * (1 - STOP_LOSS_PCT)
                last_vol_mode = vol_mode
                log(f"New grid: {levels} levels @ {spread*100:.3f}% | Stop: ${stop_loss_price:,.0f}")

            # Stop loss check
            if current_price <= stop_loss_price:
                log("!" * 55)
                log(f"STOP LOSS! BTC ${current_price:,.0f} <= ${stop_loss_price:,.0f}")
                log("!" * 55)
                telegram(
                    f"STOP LOSS TRIGGERED\n"
                    f"BTC dropped to ${current_price:,.0f}\n"
                    f"Stop loss was ${stop_loss_price:,.0f}\n"
                    f"Selling all BTC and restarting in 10 min"
                )
                sell_all_btc(current_price, "stop loss")
                stats['stop_losses'] += 1
                save_stats(stats)
                clear_state()
                log(f"Waiting {RESTART_WAIT//60} min before restart...")
                time.sleep(RESTART_WAIT)
                return 'restart'

            # Auto recenter
            if grid_out_of_range(current_price, grid):
                log(f"RECENTER: ${current_price:,.0f} outside grid")
                telegram(f"Grid recentered\nNew center: ${current_price:,.0f}\nMode: {vol_mode}")
                sell_all_btc(current_price, "recenter")
                time.sleep(3)
                usdc, btc    = get_balance()
                center_price = current_price
                grid         = build_grid(center_price, levels, spread)
                last_price   = current_price
                total_spent  = 0
                stop_loss_price = center_price * (1 - STOP_LOSS_PCT)
                stats['recenters'] += 1
                save_stats(stats)
                log(f"New grid centered at ${center_price:,.0f}")

            # Max spend check
            if total_spent >= MAX_SPEND:
                log(f"Max spend ${MAX_SPEND} reached — buys paused")

            # Grid trading
            for level in grid:
                gp = level['price']

                # BUY
                if (current_price <= gp < last_price
                        and level['status'] == 'ready'
                        and usdc >= ORDER_AMOUNT
                        and total_spent < MAX_SPEND):
                    order = place_order('buy', ORDER_AMOUNT, current_price)
                    if order:
                        level['status']    = 'bought'
                        level['buy_price'] = current_price
                        total_spent       += ORDER_AMOUNT
                        stats['total_buys'] += 1
                        usdc -= ORDER_AMOUNT

                # SELL
                elif (current_price >= gp > last_price
                        and level['status'] == 'bought'):
                    bp          = level['buy_price']
                    btc_to_sell = ORDER_AMOUNT / bp
                    order       = place_order('sell', btc_to_sell * current_price, current_price)
                    if order:
                        profit = (current_price - bp) / bp * ORDER_AMOUNT
                        level['status']    = 'ready'
                        level['buy_price'] = None
                        total_spent        = max(0, total_spent - ORDER_AMOUNT)
                        stats['total_sells']  += 1
                        stats['total_cycles'] += 1
                        stats['total_profit'] += profit
                        save_stats(stats)
                        log(f"CYCLE DONE! Profit: ${profit:+.4f} | Total: ${stats['total_profit']:+.4f}")
                        telegram(
                            f"Cycle complete!\n"
                            f"Profit: ${profit:+.4f}\n"
                            f"Total profit: ${stats['total_profit']:+.2f}\n"
                            f"Cycles: {stats['total_cycles']}"
                        )

            last_price = current_price
            save_state({
                'center_price': center_price,
                'grid'        : grid,
                'last_price'  : last_price,
                'total_spent' : total_spent,
            })

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)

# ================================================================
# OUTER LOOP — restarts forever
# ================================================================
def run_bot():
    log("=" * 55)
    log("VOLATILITY ADAPTIVE GRID BOT")
    log(f"Symbol    : {SYMBOL}")
    log(f"Order     : ${ORDER_AMOUNT} per level")
    log(f"Max spend : ${MAX_SPEND}")
    log(f"Stop loss : {STOP_LOSS_PCT*100:.0f}% from center")
    log(f"Auto-recenter  : ON")
    log(f"Auto-restart   : ON")
    log(f"Telegram alerts: {'ON' if TELEGRAM_TOKEN else 'OFF'}")
    log("=" * 55)

    stats    = load_stats()
    restarts = 0

    while True:
        result = run_session(stats)
        if result == 'restart':
            restarts += 1
            log(f"Restarting (#{restarts})...")
            stats = load_stats()

if __name__ == '__main__':
    run_bot()
