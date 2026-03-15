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
ORDER_AMOUNT   = 10
MAX_SPEND      = 400
STOP_LOSS_PCT  = 0.12
CHECK_INTERVAL = 300
RESTART_WAIT   = 600

# Bull Grid (SIDEWAYS)
BULL_LEVELS    = 12
BULL_SPREAD    = 0.0100        # 1.0% — backtest winner

# Bear Grid (DOWNTREND)
BEAR_LEVELS    = 8
BEAR_SPREAD    = 0.0050        # 0.5% — backtest winner
MAX_BTC_SELL   = 0.80          # never sell more than 80% of BTC holdings

# Momentum (UPTREND)
EMA_FAST       = 9
EMA_SLOW       = 21
TRAIL_ATR_MULT = 2.0
TAKE_PROFIT    = 0.04

# Mode detection
ADX_TREND_MIN  = 20

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
            'mode_switches' : 0,
            'last_mode'     : None,
            'bear_cycles'   : 0,
            'bull_cycles'   : 0,
            'momentum_cycles': 0,
        }

def save_stats(stats):
    with open('bot_stats.json', 'w') as f:
        json.dump(stats, f, indent=2)

# ================================================================
# MARKET DATA & INDICATORS
# ================================================================
def get_candles(limit=80):
    ohlcv  = exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=limit)
    closes = [c[4] for c in ohlcv]
    highs  = [c[2] for c in ohlcv]
    lows   = [c[3] for c in ohlcv]
    return closes, highs, lows

def calc_indicators(closes, highs, lows):
    n = len(closes)

    # ATR
    trs = [max(highs[i]-lows[i],
               abs(highs[i]-closes[i-1]),
               abs(lows[i]-closes[i-1]))
           for i in range(1, n)]
    atr = sum(trs[-14:]) / 14 if len(trs) >= 14 else None

    # EMA
    def ema(vals, span):
        k = 2 / (span + 1)
        e = vals[0]
        for v in vals[1:]:
            e = v * k + e * (1 - k)
        return e

    ema_fast      = ema(closes[-EMA_FAST*3:], EMA_FAST)
    ema_slow      = ema(closes[-EMA_SLOW*3:], EMA_SLOW)
    ema_fast_prev = ema(closes[-EMA_FAST*3-1:-1], EMA_FAST)
    ema_slow_prev = ema(closes[-EMA_SLOW*3-1:-1], EMA_SLOW)
    ema50         = ema(closes[-150:], 50)

    # RSI
    gains  = [max(closes[-i] - closes[-i-1], 0) for i in range(1, 15)]
    losses = [max(closes[-i-1] - closes[-i], 0) for i in range(1, 15)]
    avg_g  = sum(gains)  / 14 or 0.0001
    avg_l  = sum(losses) / 14 or 0.0001
    rsi    = 100 - (100 / (1 + avg_g / avg_l))

    # ADX
    up_moves  = [highs[i] - highs[i-1] for i in range(1, n)]
    dn_moves  = [lows[i-1] - lows[i]   for i in range(1, n)]
    plus_dm   = [u if u > d and u > 0 else 0 for u, d in zip(up_moves, dn_moves)]
    minus_dm  = [d if d > u and d > 0 else 0 for u, d in zip(up_moves, dn_moves)]
    atr14     = sum(trs[-14:]) / 14 if len(trs) >= 14 else 1
    plus_di   = 100 * sum(plus_dm[-14:])  / 14 / atr14
    minus_di  = 100 * sum(minus_dm[-14:]) / 14 / atr14
    dx        = 100 * abs(plus_di - minus_di) / (plus_di + minus_di + 0.0001)

    return {
        'atr'          : atr or 100,
        'ema_fast'     : ema_fast,
        'ema_slow'     : ema_slow,
        'ema_fast_prev': ema_fast_prev,
        'ema_slow_prev': ema_slow_prev,
        'ema50'        : ema50,
        'rsi'          : rsi,
        'adx'          : dx,
        'plus_di'      : plus_di,
        'minus_di'     : minus_di,
    }

def detect_mode(ind, closes):
    adx     = ind['adx']
    bullish = ind['ema_fast'] > ind['ema_slow'] and closes[-1] > ind['ema50']
    bearish = ind['ema_fast'] < ind['ema_slow'] and closes[-1] < ind['ema50']
    if adx < ADX_TREND_MIN:
        return 'SIDEWAYS'
    elif bullish:
        return 'UPTREND'
    elif bearish:
        return 'DOWNTREND'
    return 'SIDEWAYS'

def get_balance():
    balance = exchange.fetch_balance()
    return balance['USDC']['free'], balance['BTC']['free']

# ================================================================
# GRID BUILDERS
# ================================================================
def build_bull_grid(center):
    grid = []
    for i in range(-BULL_LEVELS // 2, BULL_LEVELS // 2 + 1):
        price = round(center * (1 + i * BULL_SPREAD), 2)
        grid.append({'price': price, 'status': 'ready', 'buy_price': None})
    return sorted(grid, key=lambda x: x['price'])

def build_bear_grid(center):
    grid = []
    for i in range(-BEAR_LEVELS // 2, BEAR_LEVELS // 2 + 1):
        price = round(center * (1 + i * BEAR_SPREAD), 2)
        grid.append({'price': price, 'status': 'ready', 'sell_price': None})
    return sorted(grid, key=lambda x: x['price'])

def grid_out_of_range(price, grid):
    low    = grid[0]['price']
    high   = grid[-1]['price']
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

def place_btc_order(side, btc_amount, price):
    try:
        btc_amount = round(btc_amount, 5)
        if btc_amount < 0.00001:
            log(f"ORDER SKIPPED: too small")
            return None
        if side == 'sell':
            order = exchange.create_market_sell_order(SYMBOL, btc_amount)
        else:
            order = exchange.create_market_buy_order(SYMBOL, btc_amount)
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
def print_summary(stats, usdc, btc, price, mode):
    total = usdc + btc * price
    pnl   = total - stats['start_balance'] if stats['start_balance'] else 0
    pct   = pnl / stats['start_balance'] * 100 if stats['start_balance'] else 0
    log("=" * 55)
    log("HOURLY SUMMARY")
    log(f"  Mode            : {mode}")
    log(f"  BTC price       : ${price:,.2f}")
    log(f"  USDC            : ${usdc:,.2f}")
    log(f"  BTC held        : {btc:.6f} (~${btc*price:,.2f})")
    log(f"  Total value     : ${total:,.2f}")
    log(f"  PnL             : ${pnl:+.2f} ({pct:+.2f}%)")
    log(f"  Bull cycles     : {stats['bull_cycles']}")
    log(f"  Bear cycles     : {stats['bear_cycles']}")
    log(f"  Momentum cycles : {stats['momentum_cycles']}")
    log(f"  Total profit    : ${stats['total_profit']:+.4f}")
    log(f"  Mode switches   : {stats['mode_switches']}")
    log("=" * 55)
    telegram(
        f"Hourly Update\n"
        f"Mode: {mode}\n"
        f"BTC: ${price:,.0f}\n"
        f"Balance: ${total:,.2f}\n"
        f"PnL: ${pnl:+.2f} ({pct:+.2f}%)\n"
        f"Bull: {stats['bull_cycles']} | Bear: {stats['bear_cycles']} | Mom: {stats['momentum_cycles']}"
    )

# ================================================================
# MAIN SESSION
# ================================================================
def run_session(stats):
    log("-" * 55)
    log("STARTING NEW SESSION")

    closes, highs, lows = get_candles(limit=80)
    current_price       = closes[-1]
    ind                 = calc_indicators(closes, highs, lows)
    mode                = detect_mode(ind, closes)

    log(f"Price: ${current_price:,.2f} | Mode: {mode} | "
        f"ADX: {ind['adx']:.1f} | RSI: {ind['rsi']:.1f}")

    state = load_state() or {}

    if stats['start_balance'] is None:
        usdc, btc = get_balance()
        stats['start_balance'] = usdc + btc * current_price
        save_stats(stats)
        log(f"Start balance: ${stats['start_balance']:,.2f}")
        telegram(
            f"All-Weather Bot started!\n"
            f"Balance: ${stats['start_balance']:,.2f}\n"
            f"BTC: ${current_price:,.0f}\n"
            f"Mode: {mode}\n"
            f"SIDEWAYS=BullGrid | UPTREND=Momentum | DOWNTREND=BearGrid"
        )

    last_summary_hour = -1
    last_mode         = state.get('mode', None)

    # Bull grid state
    bull_grid   = state.get('bull_grid', [])
    bull_center = state.get('bull_center', current_price)
    bull_last   = state.get('bull_last', current_price)
    bull_spent  = state.get('bull_spent', 0)

    # Bear grid state
    bear_grid   = state.get('bear_grid', [])
    bear_center = state.get('bear_center', current_price)
    bear_last   = state.get('bear_last', current_price)
    bear_sold   = state.get('bear_sold', 0)

    # Momentum state
    mom_position  = state.get('mom_position', False)
    mom_buy_price = state.get('mom_buy_price', None)
    mom_trail     = state.get('mom_trail', None)

    while True:
        try:
            closes, highs, lows = get_candles(limit=80)
            current_price       = closes[-1]
            ind                 = calc_indicators(closes, highs, lows)
            mode                = detect_mode(ind, closes)
            usdc, btc           = get_balance()
            atr                 = ind['atr']

            log(f"${current_price:,.2f} | {mode} | ADX:{ind['adx']:.1f} | "
                f"RSI:{ind['rsi']:.1f} | USDC:${usdc:,.2f} | BTC:{btc:.6f}")

            # Hourly summary
            current_hour = datetime.now().hour
            if current_hour != last_summary_hour:
                print_summary(stats, usdc, btc, current_price, mode)
                last_summary_hour = current_hour

            # Mode switch
            if mode != last_mode and last_mode is not None:
                log(f"MODE SWITCH: {last_mode} -> {mode}")
                telegram(f"Mode switched!\n{last_mode} -> {mode}\nBTC: ${current_price:,.0f}")

                # Switching OUT of sideways — close bull grid positions
                if last_mode == 'SIDEWAYS' and btc > 0.00001:
                    # Only sell trading BTC, not bear grid BTC
                    trading_btc = min(btc, bull_spent / current_price)
                    if trading_btc > 0.00001:
                        sell_all_btc(current_price, "mode switch from sideways")
                        time.sleep(3)
                        usdc, btc = get_balance()

                # Switching OUT of uptrend — close momentum position
                if last_mode == 'UPTREND' and mom_position and btc > 0.00001:
                    sell_all_btc(current_price, "mode switch from uptrend")
                    time.sleep(3)
                    usdc, btc     = get_balance()
                    mom_position  = False
                    mom_buy_price = None
                    mom_trail     = None

                # Reset relevant grid
                if mode == 'SIDEWAYS':
                    bull_grid   = build_bull_grid(current_price)
                    bull_center = current_price
                    bull_last   = current_price
                    bull_spent  = 0
                elif mode == 'DOWNTREND':
                    bear_grid   = build_bear_grid(current_price)
                    bear_center = current_price
                    bear_last   = current_price
                    bear_sold   = 0
                elif mode == 'UPTREND':
                    mom_position  = False
                    mom_buy_price = None
                    mom_trail     = None

                stats['mode_switches'] += 1
                save_stats(stats)

            last_mode = mode

            # ============================================================
            # MODE: SIDEWAYS — Bull Grid (buy low sell high)
            # ============================================================
            if mode == 'SIDEWAYS':
                if not bull_grid:
                    bull_grid   = build_bull_grid(current_price)
                    bull_center = current_price
                    bull_last   = current_price
                    log(f"Bull grid built @ ${bull_center:,.0f} | {BULL_LEVELS}L | {BULL_SPREAD*100:.1f}%")

                # Stop loss
                stop = bull_center * (1 - STOP_LOSS_PCT)
                if current_price <= stop:
                    log(f"STOP LOSS: ${current_price:,.0f} <= ${stop:,.0f}")
                    telegram(f"STOP LOSS!\nBTC: ${current_price:,.0f}\nRestarting in 10 min")
                    sell_all_btc(current_price, "stop loss")
                    stats['stop_losses'] += 1
                    save_stats(stats)
                    clear_state()
                    time.sleep(RESTART_WAIT)
                    return 'restart'

                # Recenter
                if grid_out_of_range(current_price, bull_grid):
                    log(f"BULL RECENTER: ${current_price:,.0f}")
                    sell_all_btc(current_price, "bull recenter")
                    time.sleep(3)
                    usdc, btc   = get_balance()
                    bull_grid   = build_bull_grid(current_price)
                    bull_center = current_price
                    bull_last   = current_price
                    bull_spent  = 0
                    telegram(f"Bull grid recentered\n${current_price:,.0f}")

                for lv in bull_grid:
                    gp = lv['price']
                    if (current_price <= gp < bull_last
                            and lv['status'] == 'ready'
                            and usdc >= ORDER_AMOUNT
                            and bull_spent < MAX_SPEND):
                        order = place_order('buy', ORDER_AMOUNT, current_price)
                        if order:
                            lv['status']    = 'bought'
                            lv['buy_price'] = current_price
                            bull_spent     += ORDER_AMOUNT
                            stats['total_buys'] += 1
                            usdc -= ORDER_AMOUNT

                    elif (current_price >= gp > bull_last
                            and lv['status'] == 'bought'):
                        bp          = lv['buy_price']
                        btc_to_sell = ORDER_AMOUNT / bp
                        order       = place_order('sell', btc_to_sell * current_price, current_price)
                        if order:
                            profit = (current_price - bp) / bp * ORDER_AMOUNT
                            lv['status']    = 'ready'
                            lv['buy_price'] = None
                            bull_spent      = max(0, bull_spent - ORDER_AMOUNT)
                            stats['total_sells']  += 1
                            stats['total_cycles'] += 1
                            stats['bull_cycles']  += 1
                            stats['total_profit'] += profit
                            save_stats(stats)
                            log(f"BULL CYCLE: ${profit:+.4f} | Total: ${stats['total_profit']:+.4f}")
                            telegram(f"Bull grid cycle!\nProfit: ${profit:+.4f}\nTotal: ${stats['total_profit']:+.2f}")

                bull_last = current_price

            # ============================================================
            # MODE: DOWNTREND — Bear Grid (sell high buy back lower)
            # ============================================================
            elif mode == 'DOWNTREND':
                if not bear_grid:
                    bear_grid   = build_bear_grid(current_price)
                    bear_center = current_price
                    bear_last   = current_price
                    log(f"Bear grid built @ ${bear_center:,.0f} | {BEAR_LEVELS}L | {BEAR_SPREAD*100:.1f}%")

                # Recenter
                if grid_out_of_range(current_price, bear_grid):
                    log(f"BEAR RECENTER: ${current_price:,.0f}")
                    bear_grid   = build_bear_grid(current_price)
                    bear_center = current_price
                    bear_last   = current_price
                    bear_sold   = 0
                    telegram(f"Bear grid recentered\n${current_price:,.0f}")

                btc_per_level = (btc * MAX_BTC_SELL) / BEAR_LEVELS if btc > 0.00001 else 0

                for lv in bear_grid:
                    gp = lv['price']

                    # SELL BTC on price bounce up
                    if (current_price >= gp > bear_last
                            and lv['status'] == 'ready'
                            and btc >= btc_per_level
                            and btc_per_level > 0.00001):
                        order = place_btc_order('sell', btc_per_level, current_price)
                        if order:
                            lv['status']     = 'sold'
                            lv['sell_price'] = current_price
                            bear_sold       += btc_per_level
                            stats['total_sells'] += 1
                            log(f"BEAR SELL: {btc_per_level:.5f} BTC @ ${current_price:,.0f}")

                    # BUY BACK cheaper on price drop
                    elif (current_price <= gp < bear_last
                            and lv['status'] == 'sold'):
                        sp        = lv['sell_price']
                        cost      = btc_per_level * current_price
                        if usdc >= cost * 1.001 and sp and current_price < sp:
                            order = place_btc_order('buy', btc_per_level, current_price)
                            if order:
                                profit = (sp - current_price) * btc_per_level
                                lv['status']     = 'ready'
                                lv['sell_price'] = None
                                bear_sold        = max(0, bear_sold - btc_per_level)
                                stats['total_buys']   += 1
                                stats['total_cycles'] += 1
                                stats['bear_cycles']  += 1
                                stats['total_profit'] += profit
                                save_stats(stats)
                                log(f"BEAR CYCLE: ${profit:+.4f} | Total: ${stats['total_profit']:+.4f}")
                                telegram(f"Bear grid cycle!\nProfit: ${profit:+.4f}\nTotal: ${stats['total_profit']:+.2f}")

                bear_last = current_price

            # ============================================================
            # MODE: UPTREND — Dual Momentum with trailing stop
            # ============================================================
            elif mode == 'UPTREND':
                cross_up   = (ind['ema_fast_prev'] <= ind['ema_slow_prev'] and
                              ind['ema_fast']      >  ind['ema_slow'])
                cross_down = (ind['ema_fast_prev'] >= ind['ema_slow_prev'] and
                              ind['ema_fast']      <  ind['ema_slow'])

                # Manage open position
                if mom_position and mom_buy_price:
                    new_stop = current_price - atr * TRAIL_ATR_MULT
                    if mom_trail is None or new_stop > mom_trail:
                        mom_trail = new_stop

                    gain     = (current_price - mom_buy_price) / mom_buy_price
                    stop_hit = mom_trail and current_price <= mom_trail
                    tp_hit   = gain >= TAKE_PROFIT

                    if stop_hit or tp_hit or cross_down:
                        reason = "take profit" if tp_hit else ("trail stop" if stop_hit else "EMA cross")
                        order  = place_order('sell', btc * current_price * 0.999, current_price)
                        if order:
                            profit = gain * ORDER_AMOUNT
                            stats['total_sells']      += 1
                            stats['total_cycles']     += 1
                            stats['momentum_cycles']  += 1
                            stats['total_profit']     += profit
                            save_stats(stats)
                            log(f"MOMENTUM SELL ({reason}): {gain*100:.2f}% | ${profit:+.4f}")
                            telegram(f"Momentum sell ({reason})\nGain: {gain*100:.2f}%\nProfit: ${profit:+.4f}")
                            mom_position  = False
                            mom_buy_price = None
                            mom_trail     = None

                # Enter on EMA cross up
                if cross_up and not mom_position and usdc >= ORDER_AMOUNT:
                    order = place_order('buy', ORDER_AMOUNT, current_price)
                    if order:
                        mom_position  = True
                        mom_buy_price = current_price
                        mom_trail     = current_price - atr * TRAIL_ATR_MULT
                        stats['total_buys'] += 1
                        log(f"MOMENTUM BUY @ ${current_price:,.0f} | Trail: ${mom_trail:,.0f}")
                        telegram(f"Momentum buy!\nBTC: ${current_price:,.0f}\nTrail: ${mom_trail:,.0f}")

            # Save full state
            save_state({
                'mode'         : mode,
                'bull_grid'    : bull_grid,
                'bull_center'  : bull_center,
                'bull_last'    : bull_last,
                'bull_spent'   : bull_spent,
                'bear_grid'    : bear_grid,
                'bear_center'  : bear_center,
                'bear_last'    : bear_last,
                'bear_sold'    : bear_sold,
                'mom_position' : mom_position,
                'mom_buy_price': mom_buy_price,
                'mom_trail'    : mom_trail,
            })

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)

# ================================================================
# OUTER LOOP
# ================================================================
def run_bot():
    log("=" * 55)
    log("ALL-WEATHER BOT v4")
    log(f"Symbol     : {SYMBOL}")
    log(f"Order      : ${ORDER_AMOUNT} | Max spend: ${MAX_SPEND}")
    log(f"SIDEWAYS   : Bull Grid {BULL_LEVELS}L @ {BULL_SPREAD*100:.1f}%")
    log(f"UPTREND    : Dual Momentum EMA {EMA_FAST}/{EMA_SLOW}")
    log(f"DOWNTREND  : Bear Grid {BEAR_LEVELS}L @ {BEAR_SPREAD*100:.1f}%")
    log(f"Telegram   : {'ON' if TELEGRAM_TOKEN else 'OFF'}")
    log("=" * 55)
    telegram("All-Weather Bot v4 started!\nSIDEWAYS=BullGrid | UPTREND=Momentum | DOWNTREND=BearGrid")

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
