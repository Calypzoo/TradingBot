"""
backtest_grid_v77.py  (06.11.2026)

Backtest der ECHTEN v7.7.0-Grid-Logik (Kauf Level i -> Verkauf Level i+1,
Lots ueberleben Mode-Switch/Recenter, DOWNTREND/UPTREND = halten) mit Sweep
ueber die vier Hebel, die den Live-Befund 17.10.-03.11. erklaeren:

  depth    : max. gleichzeitig offene Bull-Lots (1 = heutiges Verhalten de facto)
  spread   : BULL_SPREAD 0.75 / 1.0 / 1.5 %
  ema_gate : REGIME_FILTER (kein Kauf unter EMA20)  an/aus
  dt_buy   : Grid-Kaeufe auch im DOWNTREND erlaubt   an/aus

Datenquelle: Binance via ccxt, BTC/USDC.
  - 1h-Kerzen -> Indikatoren (ADX/EMA/RSI) NUR auf geschlossenen Kerzen,
    80-Bar-Fenster wie live (fetch_ohlcv limit=80)
  - 5m-Kerzen -> Preis-Ticks (naehert den 120-s-CHECK_INTERVAL an)
Kerzen werden in ./bt_cache/ gecacht, zweiter Lauf braucht kein Netz.

Aufruf (Privat-Rechner, Notepad -> cmd):
  pip install ccxt pandas
  python backtest_grid_v77.py                 # Apr-Nov 2026, voller Sweep
  python backtest_grid_v77.py --since 2026-07-01
  python backtest_grid_v77.py --quick         # nur 8 Kombinationen
  python backtest_grid_v77.py --synthetic     # Selbsttest ohne Netz

Kapital wie live: 890 USDC + Kern 0.002 BTC (Kern wird nie verkauft und
NICHT in den Bot-Ertrag gerechnet - er ist reine BTC-Exposure).
Fees 0.075 % je Seite (BNB-Rabatt).
"""
import argparse, os, sys, time, math, itertools
from datetime import datetime, timezone

try:
    import pandas as pd
except ImportError:
    print("pip install pandas"); sys.exit(1)

# ----------------------------------------------------------------
# Live-Konstanten v7.7.0 (bot_v7.py)
# ----------------------------------------------------------------
ORDER_AMOUNT    = 70
MAX_SPEND       = 900
STOP_LOSS_PCT   = 0.12
BULL_LEVELS     = 12
RSI_BUY_MAX     = 58
ADX_ENTER       = 25
ADX_EXIT        = 15
EMA_FAST        = 9
EMA_SLOW        = 21
ADX_PERIOD      = 14
REGIME_SPAN     = 20
MIN_PROFIT_PCT  = 0.0020
MIN_TICKET_USD  = 25.0
WHIPSAW_MAX     = 6
BTC_CORE_MIN    = 0.002
START_USDC      = 890.0
FEE             = 0.00075
CANDLE_WINDOW   = 80          # fetch_ohlcv(limit=80) -> 79 geschlossene Kerzen
RESTART_WAIT_S  = 600

# ----------------------------------------------------------------
# Indikatoren - 1:1 aus bot_v7.py
# ----------------------------------------------------------------
def _ema(vals, span):
    k = 2.0 / (span + 1); e = vals[0]
    for v in vals[1:]: e = v*k + e*(1-k)
    return e

def _wilder(vals, p):
    s = sum(vals[:p]); r = [s]
    for v in vals[p:]: s = s - s/p + v; r.append(s)
    return r

def indicators(closes, highs, lows):
    """closes/highs/lows = geschlossene Kerzen (laufende bereits entfernt)."""
    n = len(closes)
    trs = [highs[0]-lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    pdm, mdm = [], []
    for i in range(1, n):
        u = highs[i]-highs[i-1]; d = lows[i-1]-lows[i]
        pdm.append(u if u > d and u > 0 else 0)
        mdm.append(d if d > u and d > 0 else 0)
    adx = 0
    if len(pdm) >= ADX_PERIOD:
        sp = _wilder(pdm, ADX_PERIOD); sm = _wilder(mdm, ADX_PERIOD); st = _wilder(trs[1:], ADX_PERIOD)
        dxs = []
        for a, b, c in zip(sp, sm, st):
            p = 100*a/(c+1e-10); m = 100*b/(c+1e-10)
            dxs.append(100*abs(p-m)/(p+m+1e-10))
        if len(dxs) >= ADX_PERIOD:
            adx = _wilder(dxs, ADX_PERIOD)[-1] / ADX_PERIOD
    ef   = _ema(closes[-EMA_FAST*3:], EMA_FAST)
    es   = _ema(closes[-EMA_SLOW*3:], EMA_SLOW)
    ereg = _ema(closes[-REGIME_SPAN*3:], REGIME_SPAN)
    gs, ls = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]; gs.append(max(d, 0)); ls.append(max(-d, 0))
    ag = sum(gs[-14:])/14 or 1e-10; al = sum(ls[-14:])/14 or 1e-10
    rsi = 100 - 100/(1 + ag/al)
    return {'ef': ef, 'es': es, 'ereg': ereg, 'rsi': rsi, 'adx': adx, 'close': closes[-1]}

def mode(ind, last=None):
    adx = ind['adx']; c = ind['close']
    bull = ind['ef'] > ind['es'] and c > ind['ereg']
    bear = ind['ef'] < ind['es'] and c < ind['ereg']
    if last is None or last == 'SIDEWAYS':
        if adx >= ADX_ENTER:
            if bull: return 'UPTREND'
            if bear: return 'DOWNTREND'
        return 'SIDEWAYS'
    if adx < ADX_EXIT: return 'SIDEWAYS'
    if last == 'UPTREND':
        if bull: return 'UPTREND'
        if bear: return 'DOWNTREND' if adx >= ADX_ENTER else 'SIDEWAYS'
        return 'UPTREND'
    if last == 'DOWNTREND':
        if bear: return 'DOWNTREND'
        if bull: return 'UPTREND' if adx >= ADX_ENTER else 'SIDEWAYS'
        return 'DOWNTREND'
    return last

# ----------------------------------------------------------------
# Grid - 1:1 aus bot_v7.py (spread als Parameter)
# ----------------------------------------------------------------
def bull_grid(center, spread):
    g = [{'p': round(center*(1+i*spread), 2), 'st': 'ready', 'bp': None, 'q': None, 'tp': None}
         for i in range(-BULL_LEVELS//2, BULL_LEVELS//2+1)]
    return sorted(g, key=lambda x: x['p'])

def bull_target(lv, spread):
    base = max(lv['p'], lv.get('bp') or 0)
    return round(base * (1 + spread), 2)

def rebuild_bull_grid(center, old, spread):
    held = [lv for lv in (old or []) if lv.get('st') == 'bought']
    g = bull_grid(center, spread); held_p = {lv['p'] for lv in held}
    return sorted([lv for lv in g if lv['p'] not in held_p] + held, key=lambda x: x['p'])

def out_of_range(price, grid):
    lo = grid[0]['p']; hi = grid[-1]['p']; m = (hi-lo)*0.10
    return price < lo-m or price > hi+m

# ----------------------------------------------------------------
# Daten
# ----------------------------------------------------------------
def fetch(symbol, tf, since_ms, until_ms, cache_dir):
    os.makedirs(cache_dir, exist_ok=True)
    fn = os.path.join(cache_dir, f"{symbol.replace('/','')}_{tf}_{since_ms}.csv")
    if os.path.exists(fn):
        df = pd.read_csv(fn)
        if df.ts.max() >= until_ms - 3600_000*2:
            return df
    import ccxt
    ex = ccxt.binance({'enableRateLimit': True, 'options': {'fetchCurrencies': False}})
    rows = []; cur = since_ms; step = 1000
    print(f"  fetch {symbol} {tf} ...", end='', flush=True)
    while cur < until_ms:
        batch = ex.fetch_ohlcv(symbol, tf, since=cur, limit=step)
        if not batch: break
        rows += batch; cur = batch[-1][0] + 1
        print('.', end='', flush=True)
        if len(batch) < step: break
    print()
    df = pd.DataFrame(rows, columns=['ts', 'o', 'h', 'l', 'c', 'v']).drop_duplicates('ts')
    df = df[df.ts < until_ms]
    df.to_csv(fn, index=False)
    return df

def synthetic():
    import random
    random.seed(7)
    n = 24*120; p = 80000.0; rows1h = []
    t0 = int(datetime(2026, 7, 1, tzinfo=timezone.utc).timestamp()*1000)
    for i in range(n):
        drift = 0.0008*math.sin(i/300) ; o = p
        p *= 1 + drift + random.gauss(0, 0.006)
        h = max(o, p)*(1+abs(random.gauss(0, 0.002))); l = min(o, p)*(1-abs(random.gauss(0, 0.002)))
        rows1h.append([t0+i*3600_000, o, h, l, p, 1])
    df1 = pd.DataFrame(rows1h, columns=['ts', 'o', 'h', 'l', 'c', 'v'])
    rows5 = []
    for r in rows1h:
        for j in range(12):
            f = j/11; px = r[1] + (r[4]-r[1])*f + random.gauss(0, 0.001)*r[1]
            rows5.append([r[0]+j*300_000, px, px, px, px, 1])
    return df1, pd.DataFrame(rows5, columns=['ts', 'o', 'h', 'l', 'c', 'v'])

# ----------------------------------------------------------------
# Simulation
# ----------------------------------------------------------------
def precompute_hourly(df1):
    """Fuer jede 1h-Kerze: Indikatoren auf den 79 vorherigen GESCHLOSSENEN
    Kerzen (= das, was der Bot waehrend dieser Stunde sieht)."""
    c = df1.c.tolist(); h = df1.h.tolist(); l = df1.l.tolist(); ts = df1.ts.tolist()
    out = {}
    for i in range(CANDLE_WINDOW, len(c)):
        w = slice(i-CANDLE_WINDOW+1, i)          # 79 geschlossene vor Kerze i
        out[ts[i]] = indicators(c[w], h[w], l[w])
    return out

def simulate(ticks, hourly, spread, depth, ema_gate, dt_buy, rsi_gate=True):
    usdc = START_USDC; btc = BTC_CORE_MIN
    bgrid = None; bcenter = None; blast = None; bspent = 0
    last_mode = None; cur_hour = None; ind = None; m = 'SIDEWAYS'
    recent_buys = []; restart_until = 0
    cycles = 0; realized = 0.0; fees = 0.0; buys = 0; stops = 0
    eq_peak = -1e9; max_dd = 0.0; max_open = 0
    active_ticks = 0; n_ticks = 0
    monthly = {}
    mode_ticks = {'SIDEWAYS': 0, 'UPTREND': 0, 'DOWNTREND': 0}

    def tradeable(b): return max(0.0, b - BTC_CORE_MIN)

    for ts, price in ticks:
        hour = ts - ts % 3600_000
        if hour not in hourly: continue
        n_ticks += 1
        if hour != cur_hour:
            ind = hourly[hour]; cur_hour = hour
            m = mode(ind, last_mode)
            if m != last_mode and last_mode is not None:
                pass        # NON_LIQUIDATING: nichts verkaufen, Grid bleibt
            last_mode = m
        mode_ticks[m] += 1
        rsi = ind['rsi']
        mon = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m')
        monthly.setdefault(mon, 0.0)

        # ---- _bull_sell_pass: in JEDEM Modus ----
        if bgrid:
            for lv in bgrid:
                if lv['st'] != 'bought': continue
                tp = lv['tp'] or bull_target(lv, spread)
                if price < tp or price < lv['bp']*(1+MIN_PROFIT_PCT): continue
                qty = min(lv['q'], round(tradeable(btc)*0.999, 5))
                if qty < 0.00001: lv.update(st='ready', bp=None, q=None, tp=None); continue
                proceeds = qty*price; f = proceeds*FEE
                usdc += proceeds - f; btc -= qty; fees += f
                pnl = (price - lv['bp'])*qty - f - lv['bp']*qty*FEE
                realized += pnl; monthly[mon] += pnl; cycles += 1
                bspent = max(0, bspent - ORDER_AMOUNT)
                lv.update(st='ready', bp=None, q=None, tp=None)

        if ts < restart_until:
            pass
        elif m == 'SIDEWAYS' or (m == 'DOWNTREND' and dt_buy):
            if not bgrid:
                bgrid = bull_grid(price, spread); bcenter = price; blast = price; bspent = 0
            # Stop loss (nur im SIDEWAYS wie live)
            if m == 'SIDEWAYS' and price <= bcenter*(1-STOP_LOSS_PCT):
                q = round(tradeable(btc)*0.999, 5)
                if q >= 0.00001:
                    f = q*price*FEE; usdc += q*price - f; fees += f
                    cost = sum(lv['bp']*lv['q'] for lv in bgrid if lv['st'] == 'bought')
                    pnl = q*price - f - cost; realized += pnl; monthly[mon] += pnl
                    btc -= q
                stops += 1; bgrid = None; restart_until = ts + RESTART_WAIT_S*1000
                continue
            if out_of_range(price, bgrid):
                bgrid = rebuild_bull_grid(price, bgrid, spread); bcenter = price; blast = price
                bspent = sum(ORDER_AMOUNT for lv in bgrid if lv['st'] == 'bought')
            open_lots = sum(1 for lv in bgrid if lv['st'] == 'bought')
            for lv in bgrid:
                gp = lv['p']
                if (price <= gp < blast and lv['st'] == 'ready'
                        and usdc >= ORDER_AMOUNT and bspent < MAX_SPEND
                        and open_lots < depth
                        and (not rsi_gate or rsi < RSI_BUY_MAX)
                        and (not ema_gate or price >= ind['ereg'])):
                    # whipsaw
                    recent_buys = [t for t in recent_buys if ts - t <= 3600_000]
                    if len(recent_buys) >= WHIPSAW_MAX: continue
                    recent_buys.append(ts)
                    qty = round(ORDER_AMOUNT/price, 5); f = ORDER_AMOUNT*FEE
                    usdc -= ORDER_AMOUNT; btc += qty; fees += f; buys += 1
                    lv.update(st='bought', bp=price, q=qty); lv['tp'] = bull_target(lv, spread)
                    bspent += ORDER_AMOUNT; open_lots += 1
            blast = price
        # UPTREND / DOWNTREND ohne dt_buy: halten

        open_lots = sum(1 for lv in (bgrid or []) if lv['st'] == 'bought')
        max_open = max(max_open, open_lots)
        if open_lots: active_ticks += 1
        # Equity OHNE Kern: USDC + Lots zu Marktwert
        eq = usdc + tradeable(btc)*price
        eq_peak = max(eq_peak, eq); max_dd = max(max_dd, eq_peak - eq)

    end_price = ticks[-1][1]
    unreal = sum((end_price - lv['bp'])*lv['q'] for lv in (bgrid or []) if lv['st'] == 'bought')
    open_lots = sum(1 for lv in (bgrid or []) if lv['st'] == 'bought')
    return dict(cycles=cycles, buys=buys, realized=round(realized, 2), unreal=round(unreal, 2),
                total=round(realized+unreal, 2), fees=round(fees, 2), stops=stops,
                max_dd=round(max_dd, 2), max_open=max_open, open_end=open_lots,
                per_cycle=round(realized/cycles, 3) if cycles else 0,
                active_pct=round(100*active_ticks/max(n_ticks, 1), 1),
                side_pct=round(100*mode_ticks['SIDEWAYS']/max(n_ticks, 1)),
                monthly=monthly)

# ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-04-01')
    ap.add_argument('--symbol', default='BTC/USDC')
    ap.add_argument('--quick', action='store_true')
    ap.add_argument('--synthetic', action='store_true')
    ap.add_argument('--no-rsi', action='store_true', help='zusaetzlich RSI-Gate an/aus sweepen')
    a = ap.parse_args()

    if a.synthetic:
        df1, df5 = synthetic(); print("SYNTHETIC self-test (Zufallskurs, Zahlen ohne Bedeutung)")
    else:
        since = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()*1000)
        since_1h = since - CANDLE_WINDOW*3600_000*2
        until = int(time.time()*1000)
        df1 = fetch(a.symbol, '1h', since_1h, until, 'bt_cache')
        df5 = fetch(a.symbol, '5m', since, until, 'bt_cache')
        df5 = df5[df5.ts >= since]
    hourly = precompute_hourly(df1)
    ticks = list(zip(df5.ts.tolist(), df5.c.tolist()))
    ticks = [t for t in ticks if (t[0] - t[0] % 3600_000) in hourly]
    p0, p1 = ticks[0][1], ticks[-1][1]
    days = (ticks[-1][0]-ticks[0][0])/86400_000
    print(f"\nZeitraum {datetime.fromtimestamp(ticks[0][0]/1000, tz=timezone.utc):%d.%m.%Y} - "
          f"{datetime.fromtimestamp(ticks[-1][0]/1000, tz=timezone.utc):%d.%m.%Y} ({days:.0f} Tage), "
          f"{len(ticks)} Ticks | BTC {p0:,.0f} -> {p1:,.0f} ({(p1/p0-1)*100:+.1f}%)")
    print(f"Referenz: Kern 0.002 BTC = {0.002*(p1-p0):+.2f} $ | B&H 890 $ = {890*(p1/p0-1):+.2f} $\n")

    if a.quick:
        combos = [(1, 0.0075, True, False), (4, 0.0075, True, False), (4, 0.0075, False, False),
                  (4, 0.01, False, False), (4, 0.01, False, True), (6, 0.01, False, True),
                  (12, 0.01, False, False), (6, 0.015, False, True)]
    else:
        combos = list(itertools.product([1, 2, 4, 6, 12], [0.0075, 0.01, 0.015], [True, False], [False, True]))
    rsi_opts = [True, False] if a.no_rsi else [True]

    rows = []
    hdr = f"{'depth':>5} {'spread':>6} {'ema':>4} {'dtbuy':>5} {'rsi':>4} | {'cycles':>6} {'buys':>5} {'real$':>8} {'unreal$':>8} {'total$':>8} {'$/cyc':>6} {'fees$':>6} {'maxDD$':>7} {'maxOpen':>7} {'open@end':>8} {'active%':>7} {'side%':>5} {'stops':>5}"
    print(hdr); print('-'*len(hdr))
    t0 = time.time()
    for (depth, spread, ema, dtb), rg in itertools.product(combos, rsi_opts):
        r = simulate(ticks, hourly, spread, depth, ema, dtb, rg)
        r.update(depth=depth, spread=spread, ema=ema, dt_buy=dtb, rsi=rg); rows.append(r)
        print(f"{depth:>5} {spread*100:>5.2f}% {'on' if ema else 'off':>4} {'on' if dtb else 'off':>5} {'on' if rg else 'off':>4} | "
              f"{r['cycles']:>6} {r['buys']:>5} {r['realized']:>8.2f} {r['unreal']:>8.2f} {r['total']:>8.2f} "
              f"{r['per_cycle']:>6.3f} {r['fees']:>6.2f} {r['max_dd']:>7.2f} {r['max_open']:>7} {r['open_end']:>8} "
              f"{r['active_pct']:>6.1f}% {r['side_pct']:>4}% {r['stops']:>5}")
    print(f"\n{len(rows)} Laeufe in {time.time()-t0:.0f}s")

    out = pd.DataFrame([{k: v for k, v in r.items() if k != 'monthly'} for r in rows])
    out.to_csv('backtest_grid_v77_results.csv', index=False)
    best = out.sort_values('total', ascending=False).head(5)
    print("\nTop 5 nach total$ (realisiert + offene Lots zu Marktwert):")
    print(best[['depth', 'spread', 'ema', 'dt_buy', 'rsi', 'cycles', 'realized', 'unreal', 'total', 'max_dd', 'active_pct']].to_string(index=False))
    live = out[(out.depth == 1) & (out.spread == 0.0075) & out.ema & ~out.dt_buy & out.rsi]
    print("\nLive-Konfiguration (v7.7.0 de facto: depth 1, 0.75%, EMA-Gate an, kein DT-Kauf):")
    print(live[['cycles', 'realized', 'unreal', 'total', 'max_dd', 'active_pct']].to_string(index=False))

    # Monatsrobustheit fuer Top 3 + live
    print("\nMonatlich realisiert ($) - Top 3 vs. live:")
    sel = list(best.index[:3]) + list(live.index)
    months = sorted({mo for r in rows for mo in r['monthly']})
    print(f"{'config':<28}" + ''.join(f"{mo:>9}" for mo in months))
    for i in sel:
        r = rows[i]
        lab = f"d{r['depth']} {r['spread']*100:.2f}% ema{'+' if r['ema'] else '-'} dt{'+' if r['dt_buy'] else '-'}"
        print(f"{lab:<28}" + ''.join(f"{r['monthly'].get(mo, 0):>9.2f}" for mo in months))
    print("\nErgebnisse: backtest_grid_v77_results.csv")
    print("Hinweis: total$ ist Bot-Ertrag OHNE Kern. maxDD$ = groesster Rueckgang von USDC+Lots.")

if __name__ == '__main__':
    main()