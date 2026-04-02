import ccxt
import time
import os
import json
import signal
import tempfile
import urllib.request
import urllib.parse
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

# ================================================================
# ALL-WEATHER BOT v7
# SIDEWAYS  = Bull Grid 12L @ 1.0%  (buy low sell high)
# UPTREND   = Dual Momentum EMA9/21 (ride the trend)
# DOWNTREND = Bear Grid 8L @ 0.75%  (sell high buy back lower)
# ADX hysteresis: enter>25, exit<15
# RSI filter: skip bull grid buys when RSI>58
# ================================================================
SYMBOL          = 'BTC/USDC'
TIMEFRAME       = '1h'
ORDER_AMOUNT    = 20
MAX_SPEND       = 450
STOP_LOSS_PCT   = 0.12
CHECK_INTERVAL  = 120
RESTART_WAIT    = 600
BULL_LEVELS     = 12
BULL_SPREAD     = 0.0100
BEAR_LEVELS     = 8
BEAR_SPREAD     = 0.0075
MAX_BTC_SELL    = 0.80
EMA_FAST        = 9
EMA_SLOW        = 21
TRAIL_ATR_MULT  = 2.0
TAKE_PROFIT     = 0.04
RSI_BUY_MAX     = 58
ADX_ENTER       = 25
ADX_EXIT        = 15
ADX_PERIOD      = 14

TELEGRAM_TOKEN   = os.getenv('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')
api_key          = os.getenv('API_KEY')
api_secret       = os.getenv('API_SECRET')

print(f"API_KEY    = {'YES' if api_key else 'MISSING'}")
print(f"API_SECRET = {'YES' if api_secret else 'MISSING'}")
print(f"TELEGRAM   = {'YES' if TELEGRAM_TOKEN else 'NOT SET'}")

exchange = ccxt.binance({
    'apiKey' : api_key,
    'secret' : api_secret,
    'options': {'defaultType': 'spot'},
    'enableRateLimit': True,
})

# ================================================================
# SHUTDOWN
# ================================================================
_shutdown = False

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal — finishing cycle…")

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ================================================================
# LOGGING
# ================================================================
_log_file = open('v7_log.txt', 'a', encoding='utf-8', buffering=1)

def log(msg):
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    _log_file.write(line + '\n')

# ================================================================
# TELEGRAM
# ================================================================
def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        data = urllib.parse.urlencode({
            'chat_id': TELEGRAM_CHAT_ID,
            'text'   : f"BTC v7\n{msg}",
        }).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data=data, timeout=5
        )
    except Exception as e:
        log(f"Telegram error: {e}")

# ================================================================
# STATE
# ================================================================
def _write(path, data):
    fd, tmp = tempfile.mkstemp(dir='.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, path)
    except:
        try: os.unlink(tmp)
        except: pass
        raise

def save_state(s): _write('v7_state.json', json.dumps(s, indent=2))
def load_state():
    try:
        with open('v7_state.json') as f: return json.load(f)
    except: return None

def clear_state():
    try: os.remove('v7_state.json')
    except: pass

def load_stats():
    try:
        with open('v7_stats.json') as f: return json.load(f)
    except:
        return {
            'start_balance': None,
            'start_time'   : datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'total_profit' : 0.0,
            'total_cycles' : 0,
            'bull_cycles'  : 0,
            'bear_cycles'  : 0,
            'mom_cycles'   : 0,
            'mode_switches': 0,
            'stop_losses'  : 0,
        }

def save_stats(s): _write('v7_stats.json', json.dumps(s, indent=2))

# ================================================================
# EXCHANGE
# ================================================================
def _retry(fn, retries=3):
    for i in range(retries):
        try: return fn()
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable) as e:
            if i == retries - 1: raise
            log(f"Retry {i+1}/{retries}: {e}")
            time.sleep(3 * (i + 1))

def get_candles():
    ohlcv = _retry(lambda: exchange.fetch_ohlcv(SYMBOL, TIMEFRAME, limit=80))
    return ([c[4] for c in ohlcv],
            [c[2] for c in ohlcv],
            [c[3] for c in ohlcv])

def get_balance():
    b = _retry(lambda: exchange.fetch_balance())
    return b['USDC']['free'], b['BTC']['free']

# ================================================================
# INDICATORS
# ================================================================
def _ema(vals, span):
    k = 2.0 / (span + 1); e = vals[0]
    for v in vals[1:]: e = v*k + e*(1-k)
    return e

def _wilder(vals, p):
    s = sum(vals[:p]); r = [s]
    for v in vals[p:]: s = s - s/p + v; r.append(s)
    return r

def indicators(closes, highs, lows):
    n = len(closes)
    trs = [highs[0]-lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i]-lows[i],
                       abs(highs[i]-closes[i-1]),
                       abs(lows[i]-closes[i-1])))
    atr = _wilder(trs, ADX_PERIOD)[-1] / ADX_PERIOD

    pdm, mdm = [], []
    for i in range(1, n):
        u = highs[i]-highs[i-1]; d = lows[i-1]-lows[i]
        pdm.append(u if u > d and u > 0 else 0)
        mdm.append(d if d > u and d > 0 else 0)

    adx = 0
    if len(pdm) >= ADX_PERIOD:
        sp = _wilder(pdm, ADX_PERIOD)
        sm = _wilder(mdm, ADX_PERIOD)
        st = _wilder(trs[1:], ADX_PERIOD)
        pdi = 100 * sp[-1] / (st[-1] + 1e-10)
        mdi = 100 * sm[-1] / (st[-1] + 1e-10)
        dxs = []
        for a, b, c in zip(sp, sm, st):
            p = 100*a/(c+1e-10); m = 100*b/(c+1e-10)
            dxs.append(100*abs(p-m)/(p+m+1e-10))
        if len(dxs) >= ADX_PERIOD:
            adx = _wilder(dxs, ADX_PERIOD)[-1] / ADX_PERIOD

    ef  = _ema(closes[-EMA_FAST*3:],      EMA_FAST)
    es  = _ema(closes[-EMA_SLOW*3:],      EMA_SLOW)
    efp = _ema(closes[-EMA_FAST*3-1:-1],  EMA_FAST)
    esp = _ema(closes[-EMA_SLOW*3-1:-1],  EMA_SLOW)
    e50 = _ema(closes[-150:], 50)

    gs, ls = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gs.append(max(d,0)); ls.append(max(-d,0))
    ag = sum(gs[-14:])/14 or 1e-10
    al = sum(ls[-14:])/14 or 1e-10
    rsi = 100 - 100/(1 + ag/al)

    return {'atr': max(atr,1), 'ef': ef, 'es': es, 'efp': efp,
            'esp': esp, 'e50': e50, 'rsi': rsi, 'adx': adx}

def mode(ind, closes, last=None):
    adx  = ind['adx']
    bull = ind['ef'] > ind['es'] and closes[-1] > ind['e50']
    bear = ind['ef'] < ind['es'] and closes[-1] < ind['e50']
    if last is None or last == 'SIDEWAYS':
        if adx >= ADX_ENTER:
            if bull: return 'UPTREND'
            if bear: return 'DOWNTREND'
        return 'SIDEWAYS'
    else:
        if adx < ADX_EXIT:  return 'SIDEWAYS'
        if bull:             return 'UPTREND'
        if bear:             return 'DOWNTREND'
        return last

# ================================================================
# GRIDS
# ================================================================
def bull_grid(center):
    g = []
    for i in range(-BULL_LEVELS//2, BULL_LEVELS//2+1):
        g.append({'p': round(center*(1+i*BULL_SPREAD),2),
                  'st': 'ready', 'bp': None})
    return sorted(g, key=lambda x: x['p'])

def bear_grid(center):
    g = []
    for i in range(-BEAR_LEVELS//2, BEAR_LEVELS//2+1):
        g.append({'p': round(center*(1+i*BEAR_SPREAD),2),
                  'st': 'ready', 'sp': None})
    return sorted(g, key=lambda x: x['p'])

def out_of_range(price, grid):
    lo = grid[0]['p']; hi = grid[-1]['p']
    m  = (hi-lo)*0.10
    return price < lo-m or price > hi+m

# ================================================================
# ORDERS
# ================================================================
def buy_usdc(amount_usdc, price):
    try:
        qty = round(amount_usdc/price, 5)
        if qty < 0.00001: return None
        o = _retry(lambda: exchange.create_market_buy_order(SYMBOL, qty))
        log(f"BUY  {qty} BTC @ ~${price:,.0f}")
        return o
    except Exception as e:
        log(f"BUY FAILED: {e}"); return None

def sell_btc(qty, price):
    try:
        qty = round(qty, 5)
        if qty < 0.00001: return None
        o = _retry(lambda: exchange.create_market_sell_order(SYMBOL, qty))
        log(f"SELL {qty} BTC @ ~${price:,.0f}")
        return o
    except Exception as e:
        log(f"SELL FAILED: {e}"); return None

def sell_all(price, reason):
    _, btc = get_balance()
    qty = round(btc*0.999, 5)
    if qty > 0.00001:
        _retry(lambda: exchange.create_market_sell_order(SYMBOL, qty))
        log(f"SELL ALL {qty} BTC @ ~${price:,.0f} | {reason}")

# ================================================================
# SUMMARY
# ================================================================
def summary(stats, usdc, btc, price, m):
    total = usdc + btc*price
    pnl   = total - stats['start_balance'] if stats['start_balance'] else 0
    pct   = pnl/stats['start_balance']*100 if stats['start_balance'] else 0
    log("=" * 52)
    log(f"  Mode     : {m}")
    log(f"  BTC      : ${price:,.2f}")
    log(f"  USDC     : ${usdc:,.2f}")
    log(f"  BTC held : {btc:.6f} (~${btc*price:,.2f})")
    log(f"  Total    : ${total:,.2f}")
    log(f"  PnL      : ${pnl:+.2f} ({pct:+.2f}%)")
    log(f"  Profit   : ${stats['total_profit']:+.4f} | Cycles: {stats['total_cycles']}")
    log(f"  Bull/Bear/Mom: {stats['bull_cycles']}/{stats['bear_cycles']}/{stats['mom_cycles']}")
    log("=" * 52)
    telegram(
        f"Mode: {m} | BTC: ${price:,.0f}\n"
        f"Balance: ${total:,.2f} | PnL: ${pnl:+.2f} ({pct:+.2f}%)\n"
        f"Profit: ${stats['total_profit']:+.2f} | "
        f"B/Br/M: {stats['bull_cycles']}/{stats['bear_cycles']}/{stats['mom_cycles']}"
    )

# ================================================================
# MAIN
# ================================================================
def run_session(stats):
    global _shutdown
    log("-" * 52)
    log("NEW SESSION")

    closes, highs, lows = get_candles()
    price = closes[-1]
    ind   = indicators(closes, highs, lows)
    m     = mode(ind, closes)

    log(f"${price:,.2f} | {m} | ADX:{ind['adx']:.1f} | RSI:{ind['rsi']:.1f}")

    state = load_state() or {}

    if not stats['start_balance']:
        usdc, btc = get_balance()
        stats['start_balance'] = usdc + btc*price
        save_stats(stats)
        log(f"Start balance: ${stats['start_balance']:,.2f}")
        telegram(
            f"ALL-WEATHER BOT v7 STARTED\n"
            f"Balance: ${stats['start_balance']:,.2f}\n"
            f"BTC: ${price:,.0f} | Mode: {m}\n"
            f"Order: ${ORDER_AMOUNT} | Check: {CHECK_INTERVAL}s\n"
            f"Bull: 1% | Bear: 0.75% | RSI<{RSI_BUY_MAX}"
        )

    last_hour  = -1
    last_mode  = state.get('mode', None)
    bgrid      = state.get('bgrid', [])
    bcenter    = state.get('bcenter', price)
    blast      = state.get('blast', price)
    bspent     = state.get('bspent', 0)
    ngrid      = state.get('ngrid', [])
    ncenter    = state.get('ncenter', price)
    nlast      = state.get('nlast', price)
    nsold      = state.get('nsold', 0)
    mom_on     = state.get('mom_on', False)
    mom_bp     = state.get('mom_bp', None)
    mom_ts     = state.get('mom_ts', None)
    dirty      = False

    while not _shutdown:
        try:
            closes, highs, lows = get_candles()
            price = closes[-1]
            ind   = indicators(closes, highs, lows)
            m     = mode(ind, closes, last_mode)
            usdc, btc = get_balance()
            atr   = ind['atr']
            rsi   = ind['rsi']

            log(f"${price:,.2f} | {m} | ADX:{ind['adx']:.1f} | "
                f"RSI:{rsi:.1f} | USDC:${usdc:,.2f} | BTC:{btc:.6f}")

            dirty = False

            # Hourly summary
            h = datetime.now().hour
            if h != last_hour:
                summary(stats, usdc, btc, price, m)
                last_hour = h

            # Mode switch
            if m != last_mode and last_mode is not None:
                log(f"MODE: {last_mode} → {m}")
                telegram(f"Mode: {last_mode} → {m}\nBTC: ${price:,.0f}")

                if last_mode == 'SIDEWAYS' and btc > 0.00001:
                    sell_all(price, "leaving SIDEWAYS")
                    time.sleep(3); usdc, btc = get_balance()

                if last_mode == 'UPTREND' and mom_on and btc > 0.00001:
                    sell_all(price, "leaving UPTREND")
                    time.sleep(3); usdc, btc = get_balance()
                    mom_on = False; mom_bp = None; mom_ts = None

                if last_mode == 'DOWNTREND' and nsold > 0.00001:
                    cost = nsold * price
                    if usdc >= cost * 1.001:
                        if sell_btc(-nsold, price) is not None:
                            o = _retry(lambda: exchange.create_market_buy_order(
                                SYMBOL, round(nsold, 5)))
                            log(f"BEAR BUYBACK: {nsold:.5f} BTC")
                            telegram(f"Bear buyback {nsold:.5f} BTC @ ${price:,.0f}")
                            nsold = 0; time.sleep(3); usdc, btc = get_balance()
                    else:
                        log(f"WARNING: Can't afford buyback ${cost:,.0f}")

                bgrid = []; bspent = 0
                ngrid = []; nsold  = 0
                mom_on = False; mom_bp = None; mom_ts = None

                if m == 'SIDEWAYS':
                    bgrid = bull_grid(price); bcenter = price
                    blast = price; bspent = 0
                elif m == 'DOWNTREND':
                    ngrid = bear_grid(price); ncenter = price
                    nlast = price; nsold = 0
                elif m == 'UPTREND':
                    pass

                stats['mode_switches'] += 1
                save_stats(stats); dirty = True

            last_mode = m

            # ---- SIDEWAYS: Bull Grid + RSI filter ----
            if m == 'SIDEWAYS':
                if not bgrid:
                    bgrid = bull_grid(price); bcenter = price
                    blast = price; bspent = 0
                    log(f"Bull grid @ ${bcenter:,.0f} | {BULL_LEVELS}L | {BULL_SPREAD*100:.1f}%")
                    dirty = True

                # Stop loss
                if price <= bcenter * (1 - STOP_LOSS_PCT):
                    log(f"STOP LOSS @ ${price:,.0f}")
                    telegram(f"STOP LOSS ${price:,.0f}\nRestarting in 10min")
                    sell_all(price, "stop loss")
                    stats['stop_losses'] += 1; save_stats(stats)
                    clear_state(); time.sleep(RESTART_WAIT)
                    return 'restart'

                # Recenter
                if out_of_range(price, bgrid):
                    log(f"BULL RECENTER @ ${price:,.0f}")
                    sell_all(price, "recenter"); time.sleep(3)
                    usdc, btc = get_balance()
                    bgrid = bull_grid(price); bcenter = price
                    blast = price; bspent = 0; dirty = True
                    telegram(f"Bull recentered ${price:,.0f}")

                for lv in bgrid:
                    gp = lv['p']
                    # BUY — with RSI filter
                    if (price <= gp < blast
                            and lv['st'] == 'ready'
                            and usdc >= ORDER_AMOUNT
                            and bspent < MAX_SPEND
                            and rsi < RSI_BUY_MAX):
                        if buy_usdc(ORDER_AMOUNT, price):
                            lv['st'] = 'bought'; lv['bp'] = price
                            bspent += ORDER_AMOUNT; usdc -= ORDER_AMOUNT
                            dirty = True
                    # SELL
                    elif (price >= gp > blast
                            and lv['st'] == 'bought'
                            and lv['bp']):
                        bp  = lv['bp']
                        qty = ORDER_AMOUNT / bp
                        if sell_btc(qty, price):
                            profit = (price - bp) * qty
                            lv['st'] = 'ready'; lv['bp'] = None
                            bspent = max(0, bspent - ORDER_AMOUNT)
                            stats['total_profit'] += profit
                            stats['total_cycles'] += 1
                            stats['bull_cycles']  += 1
                            save_stats(stats); dirty = True
                            log(f"BULL CYCLE ${profit:+.4f} | Total ${stats['total_profit']:+.4f}")
                            telegram(f"Bull cycle ${profit:+.4f}\nTotal ${stats['total_profit']:+.2f}")
                blast = price

            # ---- DOWNTREND: Bear Grid ----
            elif m == 'DOWNTREND':
                if not ngrid:
                    ngrid = bear_grid(price); ncenter = price
                    nlast = price; nsold = 0
                    log(f"Bear grid @ ${ncenter:,.0f} | {BEAR_LEVELS}L | {BEAR_SPREAD*100:.2f}%")
                    dirty = True

                if out_of_range(price, ngrid):
                    log(f"BEAR RECENTER @ ${price:,.0f}")
                    ngrid = bear_grid(price); ncenter = price
                    nlast = price; nsold = 0; dirty = True
                    telegram(f"Bear recentered ${price:,.0f}")

                bpl = (btc * MAX_BTC_SELL) / BEAR_LEVELS if btc > 0.00001 else 0

                for lv in ngrid:
                    gp = lv['p']
                    # SELL BTC on bounce up
                    if (price >= gp > nlast
                            and lv['st'] == 'ready'
                            and btc >= bpl
                            and bpl > 0.00001):
                        if sell_btc(bpl, price):
                            lv['st'] = 'sold'; lv['sp'] = price
                            nsold += bpl; dirty = True
                            log(f"BEAR SELL {bpl:.5f} BTC @ ${price:,.0f}")
                    # BUY BACK cheaper
                    elif (price <= gp < nlast
                            and lv['st'] == 'sold'
                            and lv['sp']):
                        sp   = lv['sp']
                        cost = bpl * price
                        if usdc >= cost*1.001 and price < sp:
                            if buy_usdc(cost, price):
                                profit = (sp - price) * bpl
                                lv['st'] = 'ready'; lv['sp'] = None
                                nsold = max(0, nsold - bpl)
                                stats['total_profit'] += profit
                                stats['total_cycles'] += 1
                                stats['bear_cycles']  += 1
                                save_stats(stats); dirty = True
                                log(f"BEAR CYCLE ${profit:+.4f} | Total ${stats['total_profit']:+.4f}")
                                telegram(f"Bear cycle ${profit:+.4f}\nTotal ${stats['total_profit']:+.2f}")
                nlast = price

            # ---- UPTREND: Momentum ----
            elif m == 'UPTREND':
                cup = ind['efp'] <= ind['esp'] and ind['ef'] > ind['es']
                cdn = ind['efp'] >= ind['esp'] and ind['ef'] < ind['es']

                if mom_on and mom_bp:
                    new_ts = price - atr*TRAIL_ATR_MULT
                    if mom_ts is None or new_ts > mom_ts: mom_ts = new_ts
                    gain     = (price - mom_bp) / mom_bp
                    stop_hit = mom_ts and price <= mom_ts
                    tp_hit   = gain >= TAKE_PROFIT

                    if stop_hit or tp_hit or cdn:
                        reason = "TP" if tp_hit else ("trail" if stop_hit else "EMA cross")
                        qty = min(ORDER_AMOUNT/mom_bp, btc*0.999)
                        if sell_btc(qty, price):
                            profit = (price - mom_bp) * qty
                            stats['total_profit'] += profit
                            stats['total_cycles'] += 1
                            stats['mom_cycles']   += 1
                            save_stats(stats); dirty = True
                            log(f"MOM SELL ({reason}) {gain*100:.2f}% ${profit:+.4f}")
                            telegram(f"Momentum sell ({reason})\n{gain*100:.2f}% ${profit:+.4f}")
                            mom_on = False; mom_bp = None; mom_ts = None

                if cup and not mom_on and usdc >= ORDER_AMOUNT:
                    if buy_usdc(ORDER_AMOUNT, price):
                        mom_on = True; mom_bp = price
                        mom_ts = price - atr*TRAIL_ATR_MULT
                        dirty  = True
                        log(f"MOM BUY @ ${price:,.0f} trail ${mom_ts:,.0f}")
                        telegram(f"Momentum buy ${price:,.0f}\nTrail ${mom_ts:,.0f}")

            if dirty:
                save_state({
                    'mode': m,
                    'bgrid': bgrid, 'bcenter': bcenter,
                    'blast': blast, 'bspent': bspent,
                    'ngrid': ngrid, 'ncenter': ncenter,
                    'nlast': nlast, 'nsold': nsold,
                    'mom_on': mom_on, 'mom_bp': mom_bp, 'mom_ts': mom_ts,
                })

        except Exception as e:
            log(f"ERROR: {e}")

        time.sleep(CHECK_INTERVAL)

    log("Shutdown — saving state")
    save_state({
        'mode': m,
        'bgrid': bgrid, 'bcenter': bcenter,
        'blast': blast, 'bspent': bspent,
        'ngrid': ngrid, 'ncenter': ncenter,
        'nlast': nlast, 'nsold': nsold,
        'mom_on': mom_on, 'mom_bp': mom_bp, 'mom_ts': mom_ts,
    })
    return 'shutdown'

# ================================================================
# ENTRY POINT
# ================================================================
def main():
    log("=" * 52)
    log("ALL-WEATHER BOT v7")
    log(f"Symbol  : {SYMBOL}")
    log(f"Order   : ${ORDER_AMOUNT} | Max: ${MAX_SPEND}")
    log(f"Check   : {CHECK_INTERVAL}s")
    log(f"Bull    : {BULL_LEVELS}L @ {BULL_SPREAD*100:.1f}% | RSI<{RSI_BUY_MAX}")
    log(f"Bear    : {BEAR_LEVELS}L @ {BEAR_SPREAD*100:.2f}%")
    log(f"Mom     : EMA{EMA_FAST}/{EMA_SLOW} trail {TRAIL_ATR_MULT}x ATR")
    log(f"ADX     : enter>{ADX_ENTER} exit<{ADX_EXIT}")
    log(f"Telegram: {'ON' if TELEGRAM_TOKEN else 'OFF'}")
    log("=" * 52)
    telegram("ALL-WEATHER BOT v7 ONLINE")

    stats    = load_stats()
    restarts = 0
    while not _shutdown:
        result = run_session(stats)
        if result == 'shutdown': break
        restarts += 1
        log(f"Restart #{restarts}")
        stats = load_stats()
    _log_file.close()

if __name__ == '__main__':
    main()
