import ccxt
import pandas as pd
import numpy as np

# ================================================================
# SETTINGS
# ================================================================
SYMBOL           = 'BTC/USDC'
STARTING_BALANCE = 1000
STARTING_BTC     = 0.014
ORDER_AMOUNT     = 50
BINANCE_FEE      = 0.00075    # with BNB discount (0.075%)

# ================================================================
# DOWNLOAD — multiple timeframes
# ================================================================
def download_data(timeframe, candles):
    print(f"Downloading {candles} x {timeframe} candles...")
    exchange = ccxt.binance()
    ohlcv    = exchange.fetch_ohlcv(SYMBOL, timeframe, limit=candles)
    df       = pd.DataFrame(ohlcv, columns=['timestamp','open','high','low','close','volume'])
    df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
    print(f"  Period : {df['timestamp'].iloc[0].strftime('%Y-%m-%d')} to "
          f"{df['timestamp'].iloc[-1].strftime('%Y-%m-%d')}")
    print(f"  Range  : ${df['close'].min():,.0f} - ${df['close'].max():,.0f}")
    print(f"  Move   : {((df['close'].iloc[-1]/df['close'].iloc[0])-1)*100:+.1f}%")
    return df

# ================================================================
# INDICATORS
# ================================================================
def add_indicators(df):
    # ATR
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(abs(df['high'] - df['close'].shift()),
                   abs(df['low']  - df['close'].shift()))
    )
    df['atr'] = df['tr'].rolling(14).mean()

    # RSI
    delta     = df['close'].diff()
    gain      = delta.clip(lower=0).rolling(14).mean()
    loss      = (-delta.clip(upper=0)).rolling(14).mean()
    df['rsi'] = 100 - (100 / (1 + gain / loss))

    # EMAs
    df['ema9']  = df['close'].ewm(span=9,  adjust=False).mean()
    df['ema21'] = df['close'].ewm(span=21, adjust=False).mean()
    df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()

    # Proper Wilder ADX
    df['up_move']  = df['high'].diff()
    df['dn_move']  = -df['low'].diff()
    df['plus_dm']  = np.where((df['up_move'] > df['dn_move']) & (df['up_move'] > 0), df['up_move'], 0.0)
    df['minus_dm'] = np.where((df['dn_move'] > df['up_move']) & (df['dn_move'] > 0), df['dn_move'], 0.0)

    period = 14
    df['sm_tr']    = df['tr'].ewm(alpha=1/period,    adjust=False).mean() * period
    df['sm_plus']  = df['plus_dm'].ewm(alpha=1/period,  adjust=False).mean() * period
    df['sm_minus'] = df['minus_dm'].ewm(alpha=1/period, adjust=False).mean() * period

    df['plus_di']  = 100 * df['sm_plus']  / (df['sm_tr'] + 1e-10)
    df['minus_di'] = 100 * df['sm_minus'] / (df['sm_tr'] + 1e-10)
    df['dx']       = 100 * abs(df['plus_di'] - df['minus_di']) / \
                           (df['plus_di'] + df['minus_di'] + 1e-10)
    df['adx']      = df['dx'].ewm(alpha=1/period, adjust=False).mean()

    return df

# ================================================================
# MODE DETECTION (with ADX threshold parameter)
# ================================================================
def get_mode(row, adx_threshold=20):
    if pd.isna(row['adx']):
        return 'SIDEWAYS'
    bull = row['ema9'] > row['ema21'] and row['close'] > row['ema50']
    bear = row['ema9'] < row['ema21'] and row['close'] < row['ema50']
    if row['adx'] < adx_threshold:
        return 'SIDEWAYS'
    elif bull:
        return 'UPTREND'
    elif bear:
        return 'DOWNTREND'
    return 'SIDEWAYS'

# ================================================================
# HELPERS
# ================================================================
def build_bull_grid(center, levels, spread):
    g = []
    for i in range(-levels//2, levels//2+1):
        p = round(center * (1 + i * spread), 2)
        g.append({'price': p, 'status': 'ready', 'buy_price': None})
    return sorted(g, key=lambda x: x['price'])

def build_bear_grid(center, levels, spread):
    g = []
    for i in range(-levels//2, levels//2+1):
        p = round(center * (1 + i * spread), 2)
        g.append({'price': p, 'status': 'ready', 'sell_price': None})
    return sorted(g, key=lambda x: x['price'])

def out_of_range(price, grid):
    lo = grid[0]['price']; hi = grid[-1]['price']
    m  = (hi - lo) * 0.10
    return price < lo - m or price > hi + m

def make_result(name, usdc, btc, df, buys, sells, fees, start_total):
    final = usdc + btc * df['close'].iloc[-1]
    ret   = (final - start_total) / start_total * 100
    return {
        'name'  : name,
        'return': round(ret, 2),
        'buys'  : buys,
        'sells' : sells,
        'profit': round(final - start_total, 2),
        'fees'  : round(fees, 2),
        'final' : round(final, 2),
    }

# ================================================================
# STRATEGY: Current v5 bot (baseline)
# ================================================================
def strat_v5_baseline(df, adx_thresh=20, bull_spread=0.010,
                      bear_spread=0.005, bull_levels=12, bear_levels=8):
    usdc          = STARTING_BALANCE
    btc           = STARTING_BTC
    start_total   = usdc + btc * df['close'].iloc[0]
    buys = sells  = 0
    fees          = 0.0
    last_price    = df['close'].iloc[0]
    bull_grid = bear_grid = []
    bull_spent = bear_sold = 0.0
    last_mode  = None
    btc_per_lvl = 0.0

    mom_pos = False; mom_bp = None; mom_ts = None

    for i in range(2, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i-1]
        price = row['close']
        mode  = get_mode(row, adx_thresh)

        # Mode switch
        if mode != last_mode and last_mode is not None:
            if last_mode in ('SIDEWAYS', 'UPTREND') and btc > 0.00001:
                f = btc * price * BINANCE_FEE
                usdc += btc * price - f; fees += f
                trading_btc = min(btc, bull_spent/price) if bull_spent > 0 else 0
                btc = max(0, btc - trading_btc)
            if last_mode == 'UPTREND' and mom_pos:
                if btc > 0.00001:
                    f = btc * price * BINANCE_FEE
                    usdc += btc * price - f; fees += f; btc = 0.0
                mom_pos = False; mom_bp = None; mom_ts = None
            bull_grid = []; bear_grid = []
            bull_spent = bear_sold = 0.0
        last_mode = mode

        atr = row['atr'] if not pd.isna(row['atr']) else 100

        # SIDEWAYS — Bull Grid
        if mode == 'SIDEWAYS':
            if not bull_grid:
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0
            if out_of_range(price, bull_grid):
                if btc > 0.00001:
                    f = btc * price * BINANCE_FEE
                    usdc += btc * price - f; fees += f; btc = 0.0
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0; continue
            for lv in bull_grid:
                gp = lv['price']
                if (price <= gp < last_price and lv['status'] == 'ready'
                        and usdc >= ORDER_AMOUNT
                        and bull_spent < STARTING_BALANCE * 0.8):
                    f = ORDER_AMOUNT * BINANCE_FEE
                    btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                    bull_spent += ORDER_AMOUNT; fees += f
                    lv['status'] = 'bought'; lv['buy_price'] = price; buys += 1
                elif (price >= gp > last_price and lv['status'] == 'bought'):
                    bp = lv['buy_price']
                    bts = (ORDER_AMOUNT-ORDER_AMOUNT*BINANCE_FEE)/bp
                    gross = bts*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= bts
                    bull_spent = max(0, bull_spent-ORDER_AMOUNT); fees += f
                    lv['status'] = 'ready'; lv['buy_price'] = None; sells += 1
            last_price = price

        # DOWNTREND — Bear Grid
        elif mode == 'DOWNTREND':
            if not bear_grid:
                bear_grid  = build_bear_grid(price, bear_levels, bear_spread)
                last_price = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
            if out_of_range(price, bear_grid):
                bear_grid  = build_bear_grid(price, bear_levels, bear_spread)
                last_price = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
                continue
            for lv in bear_grid:
                gp = lv['price']
                if (price >= gp > last_price and lv['status'] == 'ready'
                        and btc >= btc_per_lvl and btc_per_lvl > 0.00001):
                    gross = btc_per_lvl*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= btc_per_lvl
                    bear_sold += btc_per_lvl; fees += f
                    lv['status'] = 'sold'; lv['sell_price'] = price; sells += 1
                elif (price <= gp < last_price and lv['status'] == 'sold'
                        and lv['sell_price']):
                    sp = lv['sell_price']
                    cost = btc_per_lvl*price; f = cost*BINANCE_FEE
                    if usdc >= cost+f and price < sp:
                        usdc -= cost+f; btc += btc_per_lvl
                        bear_sold = max(0, bear_sold-btc_per_lvl); fees += f
                        lv['status'] = 'ready'; lv['sell_price'] = None; buys += 1
            last_price = price

        # UPTREND — Momentum
        elif mode == 'UPTREND':
            cross_up   = prev['ema9'] <= prev['ema21'] and row['ema9'] > row['ema21']
            cross_down = prev['ema9'] >= prev['ema21'] and row['ema9'] < row['ema21']
            if mom_pos and mom_bp:
                new_ts = price - atr * 2.0
                if mom_ts is None or new_ts > mom_ts: mom_ts = new_ts
                gain = (price - mom_bp) / mom_bp
                if price <= mom_ts or gain >= 0.04 or cross_down:
                    pos_btc = min(ORDER_AMOUNT/mom_bp, btc*0.999)
                    f = pos_btc*price*BINANCE_FEE
                    usdc += pos_btc*price-f; btc -= pos_btc; fees += f
                    mom_pos = False; mom_bp = None; mom_ts = None; sells += 1
            if cross_up and not mom_pos and usdc >= ORDER_AMOUNT:
                f = ORDER_AMOUNT*BINANCE_FEE
                btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                fees += f; mom_bp = price; mom_ts = price-atr*2.0
                mom_pos = True; buys += 1

    return make_result('v5 Baseline', usdc, btc, df, buys, sells, fees, start_total)

# ================================================================
# STRATEGY: Lever 3 — RSI entry filter on bull grid
# Only buy when RSI < rsi_buy_max (avoids buying local highs)
# ================================================================
def strat_rsi_filtered(df, rsi_buy_max=58, adx_thresh=20,
                        bull_spread=0.010, bear_spread=0.005,
                        bull_levels=12, bear_levels=8):
    usdc          = STARTING_BALANCE
    btc           = STARTING_BTC
    start_total   = usdc + btc * df['close'].iloc[0]
    buys = sells  = 0
    fees          = 0.0
    last_price    = df['close'].iloc[0]
    bull_grid = bear_grid = []
    bull_spent = bear_sold = 0.0
    last_mode  = None
    btc_per_lvl = 0.0
    mom_pos = False; mom_bp = None; mom_ts = None

    for i in range(2, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i-1]
        price = row['close']
        mode  = get_mode(row, adx_thresh)
        rsi   = row['rsi'] if not pd.isna(row['rsi']) else 50

        if mode != last_mode and last_mode is not None:
            if last_mode in ('SIDEWAYS', 'UPTREND') and btc > 0.00001:
                f = btc * price * BINANCE_FEE
                usdc += btc * price - f; fees += f; btc = 0.0
            if last_mode == 'UPTREND' and mom_pos:
                mom_pos = False; mom_bp = None; mom_ts = None
            bull_grid = []; bear_grid = []
            bull_spent = bear_sold = 0.0
        last_mode = mode

        atr = row['atr'] if not pd.isna(row['atr']) else 100

        if mode == 'SIDEWAYS':
            if not bull_grid:
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0
            if out_of_range(price, bull_grid):
                if btc > 0.00001:
                    f = btc * price * BINANCE_FEE
                    usdc += btc * price - f; fees += f; btc = 0.0
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0; continue
            for lv in bull_grid:
                gp = lv['price']
                # RSI FILTER — only buy when RSI is not overbought
                rsi_ok = rsi < rsi_buy_max
                if (price <= gp < last_price and lv['status'] == 'ready'
                        and usdc >= ORDER_AMOUNT
                        and bull_spent < STARTING_BALANCE * 0.8
                        and rsi_ok):
                    f = ORDER_AMOUNT * BINANCE_FEE
                    btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                    bull_spent += ORDER_AMOUNT; fees += f
                    lv['status'] = 'bought'; lv['buy_price'] = price; buys += 1
                elif (price >= gp > last_price and lv['status'] == 'bought'):
                    bp = lv['buy_price']
                    bts = (ORDER_AMOUNT-ORDER_AMOUNT*BINANCE_FEE)/bp
                    gross = bts*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= bts
                    bull_spent = max(0, bull_spent-ORDER_AMOUNT); fees += f
                    lv['status'] = 'ready'; lv['buy_price'] = None; sells += 1
            last_price = price

        elif mode == 'DOWNTREND':
            if not bear_grid:
                bear_grid   = build_bear_grid(price, bear_levels, bear_spread)
                last_price  = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
            if out_of_range(price, bear_grid):
                bear_grid   = build_bear_grid(price, bear_levels, bear_spread)
                last_price  = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
                continue
            for lv in bear_grid:
                gp = lv['price']
                if (price >= gp > last_price and lv['status'] == 'ready'
                        and btc >= btc_per_lvl and btc_per_lvl > 0.00001):
                    gross = btc_per_lvl*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= btc_per_lvl
                    bear_sold += btc_per_lvl; fees += f
                    lv['status'] = 'sold'; lv['sell_price'] = price; sells += 1
                elif (price <= gp < last_price and lv['status'] == 'sold'
                        and lv['sell_price']):
                    sp = lv['sell_price']
                    cost = btc_per_lvl*price; f = cost*BINANCE_FEE
                    if usdc >= cost+f and price < sp:
                        usdc -= cost+f; btc += btc_per_lvl
                        bear_sold = max(0, bear_sold-btc_per_lvl); fees += f
                        lv['status'] = 'ready'; lv['sell_price'] = None; buys += 1
            last_price = price

        elif mode == 'UPTREND':
            cross_up   = prev['ema9'] <= prev['ema21'] and row['ema9'] > row['ema21']
            cross_down = prev['ema9'] >= prev['ema21'] and row['ema9'] < row['ema21']
            if mom_pos and mom_bp:
                new_ts = price - atr * 2.0
                if mom_ts is None or new_ts > mom_ts: mom_ts = new_ts
                gain = (price - mom_bp) / mom_bp
                if price <= mom_ts or gain >= 0.04 or cross_down:
                    pos_btc = min(ORDER_AMOUNT/mom_bp, btc*0.999)
                    f = pos_btc*price*BINANCE_FEE
                    usdc += pos_btc*price-f; btc -= pos_btc; fees += f
                    mom_pos = False; mom_bp = None; mom_ts = None; sells += 1
            if cross_up and not mom_pos and usdc >= ORDER_AMOUNT:
                f = ORDER_AMOUNT*BINANCE_FEE
                btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                fees += f; mom_bp = price; mom_ts = price-atr*2.0
                mom_pos = True; buys += 1

    return make_result(f'RSI Filter <{rsi_buy_max}', usdc, btc, df,
                       buys, sells, fees, start_total)

# ================================================================
# STRATEGY: Lever 4 — Timeframe split
# Grid uses short TF data, momentum uses long TF data
# Simulated by: grid on 1h, momentum triggered only on 4h signal
# We approximate by requiring EMA cross on BOTH timeframes
# ================================================================
def strat_timeframe_split(df_short, df_long, bull_spread=0.010,
                           bear_spread=0.005, bull_levels=12,
                           bear_levels=8, adx_thresh=20):
    """
    df_short = 1h candles for grid trading
    df_long  = 4h candles for momentum signal
    We align them by timestamp.
    """
    # Build 4h momentum signals
    df_long = add_indicators(df_long.copy())
    mom_signals = set()
    for i in range(2, len(df_long)):
        row  = df_long.iloc[i]
        prev = df_long.iloc[i-1]
        if prev['ema9'] <= prev['ema21'] and row['ema9'] > row['ema21']:
            mom_signals.add(row['timestamp'])

    usdc          = STARTING_BALANCE
    btc           = STARTING_BTC
    start_total   = usdc + btc * df_short['close'].iloc[0]
    buys = sells  = 0
    fees          = 0.0
    last_price    = df_short['close'].iloc[0]
    bull_grid = bear_grid = []
    bull_spent = bear_sold = 0.0
    last_mode  = None
    btc_per_lvl = 0.0
    mom_pos = False; mom_bp = None; mom_ts = None

    # Find nearest 4h signal for each 1h candle
    long_timestamps = sorted(df_long['timestamp'].tolist())

    def has_recent_4h_cross(ts, lookback_hours=8):
        cutoff = ts - pd.Timedelta(hours=lookback_hours)
        return any(cutoff <= s <= ts for s in mom_signals)

    for i in range(2, len(df_short)):
        row   = df_short.iloc[i]
        prev  = df_short.iloc[i-1]
        price = row['close']
        mode  = get_mode(row, adx_thresh)
        rsi   = row['rsi'] if not pd.isna(row['rsi']) else 50
        atr   = row['atr'] if not pd.isna(row['atr']) else 100

        if mode != last_mode and last_mode is not None:
            if last_mode in ('SIDEWAYS', 'UPTREND') and btc > 0.00001:
                f = btc * price * BINANCE_FEE
                usdc += btc * price - f; fees += f; btc = 0.0
            if last_mode == 'UPTREND' and mom_pos:
                mom_pos = False; mom_bp = None; mom_ts = None
            bull_grid = []; bear_grid = []
            bull_spent = bear_sold = 0.0
        last_mode = mode

        if mode == 'SIDEWAYS':
            if not bull_grid:
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0
            if out_of_range(price, bull_grid):
                if btc > 0.00001:
                    f = btc * price * BINANCE_FEE
                    usdc += btc * price - f; fees += f; btc = 0.0
                bull_grid  = build_bull_grid(price, bull_levels, bull_spread)
                last_price = price; bull_spent = 0.0; continue
            for lv in bull_grid:
                gp = lv['price']
                if (price <= gp < last_price and lv['status'] == 'ready'
                        and usdc >= ORDER_AMOUNT
                        and bull_spent < STARTING_BALANCE * 0.8
                        and rsi < 58):
                    f = ORDER_AMOUNT * BINANCE_FEE
                    btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                    bull_spent += ORDER_AMOUNT; fees += f
                    lv['status'] = 'bought'; lv['buy_price'] = price; buys += 1
                elif (price >= gp > last_price and lv['status'] == 'bought'):
                    bp = lv['buy_price']
                    bts = (ORDER_AMOUNT-ORDER_AMOUNT*BINANCE_FEE)/bp
                    gross = bts*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= bts
                    bull_spent = max(0, bull_spent-ORDER_AMOUNT); fees += f
                    lv['status'] = 'ready'; lv['buy_price'] = None; sells += 1
            last_price = price

        elif mode == 'DOWNTREND':
            if not bear_grid:
                bear_grid   = build_bear_grid(price, bear_levels, bear_spread)
                last_price  = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
            if out_of_range(price, bear_grid):
                bear_grid   = build_bear_grid(price, bear_levels, bear_spread)
                last_price  = price; bear_sold = 0.0
                btc_per_lvl = (btc * 0.8) / bear_levels if btc > 0.00001 else 0
                continue
            for lv in bear_grid:
                gp = lv['price']
                if (price >= gp > last_price and lv['status'] == 'ready'
                        and btc >= btc_per_lvl and btc_per_lvl > 0.00001):
                    gross = btc_per_lvl*price; f = gross*BINANCE_FEE
                    usdc += gross-f; btc -= btc_per_lvl
                    bear_sold += btc_per_lvl; fees += f
                    lv['status'] = 'sold'; lv['sell_price'] = price; sells += 1
                elif (price <= gp < last_price and lv['status'] == 'sold'
                        and lv['sell_price']):
                    sp = lv['sell_price']
                    cost = btc_per_lvl*price; f = cost*BINANCE_FEE
                    if usdc >= cost+f and price < sp:
                        usdc -= cost+f; btc += btc_per_lvl
                        bear_sold = max(0, bear_sold-btc_per_lvl); fees += f
                        lv['status'] = 'ready'; lv['sell_price'] = None; buys += 1
            last_price = price

        elif mode == 'UPTREND':
            # Only enter momentum on 4h signal confirmation
            has_signal = has_recent_4h_cross(row['timestamp'])
            cross_down = prev['ema9'] >= prev['ema21'] and row['ema9'] < row['ema21']
            if mom_pos and mom_bp:
                new_ts = price - atr * 2.0
                if mom_ts is None or new_ts > mom_ts: mom_ts = new_ts
                gain = (price - mom_bp) / mom_bp
                if price <= mom_ts or gain >= 0.04 or cross_down:
                    pos_btc = min(ORDER_AMOUNT/mom_bp, btc*0.999)
                    f = pos_btc*price*BINANCE_FEE
                    usdc += pos_btc*price-f; btc -= pos_btc; fees += f
                    mom_pos = False; mom_bp = None; mom_ts = None; sells += 1
            if has_signal and not mom_pos and usdc >= ORDER_AMOUNT:
                f = ORDER_AMOUNT*BINANCE_FEE
                btc += (ORDER_AMOUNT-f)/price; usdc -= ORDER_AMOUNT
                fees += f; mom_bp = price; mom_ts = price-atr*2.0
                mom_pos = True; buys += 1

    return make_result('TF Split (1h grid/4h mom)', usdc, btc, df_short,
                       buys, sells, fees, start_total)

# ================================================================
# BUY & HOLD
# ================================================================
def strat_buy_hold(df):
    start = df['close'].iloc[0]; end = df['close'].iloc[-1]
    total_start = STARTING_BALANCE + STARTING_BTC * start
    total_end   = STARTING_BALANCE * (end/start) + STARTING_BTC * end
    ret = (total_end - total_start) / total_start * 100
    return {'name': 'Buy & Hold', 'return': round(ret,2), 'buys': 1,
            'sells': 0, 'profit': round(total_end-total_start,2),
            'fees': 1.0, 'final': round(total_end,2)}

# ================================================================
# RUN ALL
# ================================================================
def run_all():
    print("\n" + "="*65)
    print("LEVER 2: LONG HISTORY TEST (4h candles = ~1 year)")
    print("="*65)
    df_4h = download_data('4h', 2000)
    df_4h = add_indicators(df_4h)

    print("\n" + "="*65)
    print("LEVER 2+3: SHORT HISTORY TEST (1h candles = 41 days)")
    print("="*65)
    df_1h = download_data('1h', 1000)
    df_1h = add_indicators(df_1h)

    # Also get 4h data aligned with 1h period for timeframe split test
    df_4h_short = download_data('4h', 250)
    df_4h_short = add_indicators(df_4h_short)

    print("\nRunning strategies on LONG history (4h, ~1 year)...")
    long_results = [
        strat_v5_baseline(df_4h, adx_thresh=20),
        strat_rsi_filtered(df_4h, rsi_buy_max=55, adx_thresh=20),
        strat_rsi_filtered(df_4h, rsi_buy_max=58, adx_thresh=20),
        strat_rsi_filtered(df_4h, rsi_buy_max=62, adx_thresh=20),
        strat_rsi_filtered(df_4h, rsi_buy_max=58, adx_thresh=25),
        strat_buy_hold(df_4h),
    ]
    long_results[-2]['name'] = 'RSI<58 + ADX>25'
    long_results.sort(key=lambda x: x['return'], reverse=True)

    print(f"\n{'='*65}")
    print(f"LONG HISTORY RESULTS (~1 year, 4h candles)")
    print(f"{'='*65}")
    print(f"{'Strategy':<28} {'Return':>8} {'Buys':>5} {'Sells':>5} "
          f"{'Profit':>9} {'Fees':>8}")
    print(f"{'-'*65}")
    for r in long_results:
        print(f"{r['name']:<28} {r['return']:>7.2f}% {r['buys']:>5} "
              f"{r['sells']:>5}   ${r['profit']:>7.2f} ${r['fees']:>6.2f}")

    print("\nRunning strategies on SHORT history (1h, 41 days)...")
    short_results = [
        strat_v5_baseline(df_1h, adx_thresh=20),
        strat_rsi_filtered(df_1h, rsi_buy_max=55, adx_thresh=20),
        strat_rsi_filtered(df_1h, rsi_buy_max=58, adx_thresh=20),
        strat_rsi_filtered(df_1h, rsi_buy_max=62, adx_thresh=20),
        strat_timeframe_split(df_1h, df_4h_short),
        strat_buy_hold(df_1h),
    ]
    short_results[-2]['name'] = 'TF Split 1h/4h'
    short_results.sort(key=lambda x: x['return'], reverse=True)

    print(f"\n{'='*65}")
    print(f"SHORT HISTORY RESULTS (41 days, 1h candles)")
    print(f"{'='*65}")
    print(f"{'Strategy':<28} {'Return':>8} {'Buys':>5} {'Sells':>5} "
          f"{'Profit':>9} {'Fees':>8}")
    print(f"{'-'*65}")
    for r in short_results:
        print(f"{r['name']:<28} {r['return']:>7.2f}% {r['buys']:>5} "
              f"{r['sells']:>5}   ${r['profit']:>7.2f} ${r['fees']:>6.2f}")

    # Final verdict
    best_long  = long_results[0]
    best_short = short_results[0]
    bnh_long   = next(r for r in long_results  if 'Hold' in r['name'])
    bnh_short  = next(r for r in short_results if 'Hold' in r['name'])

    print(f"\n{'='*65}")
    print(f"FINAL VERDICT")
    print(f"{'='*65}")
    print(f"Long history winner : {best_long['name']} "
          f"({best_long['return']:+.2f}% vs B&H {bnh_long['return']:+.2f}%)")
    print(f"Short history winner: {best_short['name']} "
          f"({best_short['return']:+.2f}% vs B&H {bnh_short['return']:+.2f}%)")
    print(f"\nIf same strategy wins both = strong signal to deploy")
    print(f"If different = market regime dependent, use long history winner")
    print(f"{'='*65}")
    print(f"\nNOTE: All results use 0.075% fee (BNB discount)")
    print(f"      Current bot uses 0.1% — enable BNB to match these results!")

if __name__ == '__main__':
    run_all()
