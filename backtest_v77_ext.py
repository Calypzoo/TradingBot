"""
backtest_v77_ext.py  (07.11.2026)

Erweiterung zu backtest_grid_v77.py (muss im selben Ordner liegen, Kerzen-
Cache wird mitbenutzt). Zwei Fragen:

TEIL A - Stop-Fix vs. DOWNTREND-Kauf
  stopfix : bcenter zieht beim Recenter auch in Hold-Modi mit (UPTREND /
            DOWNTREND), damit der Stop-Loss nicht auf einem veralteten
            bcenter feuert (Juni-Verlust). Ohne stopfix = Live-Verhalten.
  dt_buy  : Grid-Kaeufe auch im DOWNTREND (EMA20- und RSI-Gate bleiben).
  Sweep ueber d2/d4 x 0.75/1.0 % x stopfix x dt_buy, EMA-Gate immer an.

TEIL B - Momentum im UPTREND (bot_v7.py-Logik, MOMENTUM_ENABLED war seit
  v7.6.11 aus) auf der besten Grid-Konfiguration aus Teil A:
  ticket  : 70 / 150 / 300 $
  entry   : 'cross' = EMA-9/21-Kreuzung nach oben waehrend UPTREND (live)
            'enter' = immer long, solange UPTREND und keine Position (neu)
  trail   : ATR-Multiplikator 2.0 / 3.0
  tp      : Take-Profit 4 % / 8 %
  rsi70   : Entry nur bei RSI < 70 (offener Task 2) an/aus
  Exit wie live: Trail-Stop (ratchet), TP, EMA-Kreuzung nach unten -
  nur solange UPTREND; bei Modewechsel wird die Position gehalten und
  im naechsten UPTREND weiter verwaltet. Cooldown 4 h nach Exit.

Aufruf:
  python backtest_v77_ext.py --since 2025-11-01
  python backtest_v77_ext.py --since 2026-04-01
  python backtest_v77_ext.py --since 2025-11-01 --grid 4,0.0075,1   # Teil B auf
                                   fester Grid-Konfig: depth,spread,dt_buy
"""
import argparse, itertools, time, sys, os
from datetime import datetime, timezone
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest_grid_v77 as G

TRAIL_ATR_MULT = 2.0
TAKE_PROFIT    = 0.04
MOM_COOLDOWN_H = 4

def indicators_ext(closes, highs, lows):
    ind = G.indicators(closes, highs, lows)
    n = len(closes)
    trs = [highs[0]-lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
    ind['atr'] = max(G._wilder(trs, G.ADX_PERIOD)[-1] / G.ADX_PERIOD, 1)
    ind['efp'] = G._ema(closes[-G.EMA_FAST*3-1:-1], G.EMA_FAST)
    ind['esp'] = G._ema(closes[-G.EMA_SLOW*3-1:-1], G.EMA_SLOW)
    return ind

def precompute_hourly(df1):
    c = df1.c.tolist(); h = df1.h.tolist(); l = df1.l.tolist(); ts = df1.ts.tolist()
    out = {}
    for i in range(G.CANDLE_WINDOW, len(c)):
        w = slice(i-G.CANDLE_WINDOW+1, i)
        out[ts[i]] = indicators_ext(c[w], h[w], l[w])
    return out

def simulate(ticks, hourly, spread, depth, dt_buy, stopfix,
             mom_ticket=0, mom_entry='cross', trail=TRAIL_ATR_MULT, tp=TAKE_PROFIT, rsi70=False):
    ORDER = G.ORDER_AMOUNT; FEE = G.FEE
    usdc = G.START_USDC; btc = G.BTC_CORE_MIN
    bgrid = None; bcenter = None; blast = None; bspent = 0
    last_mode = None; cur_hour = None; ind = None; m = 'SIDEWAYS'
    recent_buys = []; restart_until = 0
    cycles = 0; realized = 0.0; fees = 0.0; stops = 0
    mom_on = False; mom_bp = None; mom_ts = None; mom_qty = None; mom_cd_until = 0
    mom_real = 0.0; mom_trades = 0; mom_wins = 0; mom_exits = {'TP': 0, 'trail': 0, 'cross': 0}
    eq_peak = -1e9; max_dd = 0.0; max_open = 0; n_ticks = 0
    monthly = {}

    def tradeable(b): return max(0.0, b - G.BTC_CORE_MIN)

    for ts, price in ticks:
        hour = ts - ts % 3600_000
        if hour not in hourly: continue
        n_ticks += 1
        if hour != cur_hour:
            ind = hourly[hour]; cur_hour = hour
            m = G.mode(ind, last_mode)
            if last_mode == 'UPTREND' and m != 'UPTREND' and mom_on:
                mom_cd_until = ts + MOM_COOLDOWN_H*3600_000
            last_mode = m
        rsi = ind['rsi']; atr = ind['atr']
        mon = datetime.fromtimestamp(ts/1000, tz=timezone.utc).strftime('%Y-%m')
        monthly.setdefault(mon, 0.0)

        # ---- Bull-Lots an Ziel verkaufen (jeder Modus) ----
        if bgrid:
            for lv in bgrid:
                if lv['st'] != 'bought': continue
                tpv = lv['tp'] or G.bull_target(lv, spread)
                if price < tpv or price < lv['bp']*(1+G.MIN_PROFIT_PCT): continue
                qty = min(lv['q'], round(tradeable(btc)*0.999, 5))
                if qty < 0.00001: lv.update(st='ready', bp=None, q=None, tp=None); continue
                f = qty*price*FEE
                usdc += qty*price - f; btc -= qty; fees += f
                pnl = (price - lv['bp'])*qty - f - lv['bp']*qty*FEE
                realized += pnl; monthly[mon] += pnl; cycles += 1
                bspent = max(0, bspent - ORDER)
                lv.update(st='ready', bp=None, q=None, tp=None)

        grid_active = (m == 'SIDEWAYS') or (m == 'DOWNTREND' and dt_buy)

        if ts < restart_until:
            pass
        elif grid_active:
            if not bgrid:
                bgrid = G.bull_grid(price, spread); bcenter = price; blast = price; bspent = 0
            if m == 'SIDEWAYS' and price <= bcenter*(1-G.STOP_LOSS_PCT):
                q = round(tradeable(btc)*0.999, 5)
                if q >= 0.00001:
                    f = q*price*FEE; usdc += q*price - f; fees += f
                    cost = sum(lv['bp']*lv['q'] for lv in bgrid if lv['st'] == 'bought')
                    pnl = q*price - f - cost; realized += pnl; monthly[mon] += pnl
                    btc -= q
                stops += 1; bgrid = None; restart_until = ts + G.RESTART_WAIT_S*1000
                mom_on = False   # sell_all nimmt auch eine Momentum-Position mit
                continue
            if G.out_of_range(price, bgrid):
                bgrid = G.rebuild_bull_grid(price, bgrid, spread); bcenter = price; blast = price
                bspent = sum(ORDER for lv in bgrid if lv['st'] == 'bought')
            open_lots = sum(1 for lv in bgrid if lv['st'] == 'bought')
            for lv in bgrid:
                gp = lv['p']
                if (price <= gp < blast and lv['st'] == 'ready'
                        and usdc >= ORDER and bspent < G.MAX_SPEND
                        and open_lots < depth and rsi < G.RSI_BUY_MAX
                        and price >= ind['ereg']):
                    recent_buys = [t for t in recent_buys if ts - t <= 3600_000]
                    if len(recent_buys) >= G.WHIPSAW_MAX: continue
                    recent_buys.append(ts)
                    qty = round(ORDER/price, 5); f = ORDER*FEE
                    usdc -= ORDER; btc += qty; fees += f
                    lv.update(st='bought', bp=price, q=qty); lv['tp'] = G.bull_target(lv, spread)
                    bspent += ORDER; open_lots += 1
            blast = price
        else:
            # Hold-Modus. stopfix: Grid-Zentrum folgt dem Preis, damit der
            # Stop-Loss beim Rueckweg nach SIDEWAYS nicht auf altem bcenter feuert.
            if stopfix and bgrid and G.out_of_range(price, bgrid):
                bgrid = G.rebuild_bull_grid(price, bgrid, spread); bcenter = price; blast = price
                bspent = sum(ORDER for lv in bgrid if lv['st'] == 'bought')

        # ---- Momentum (nur UPTREND) ----
        if m == 'UPTREND' and mom_ticket > 0:
            cup = ind['efp'] <= ind['esp'] and ind['ef'] > ind['es']
            cdn = ind['efp'] >= ind['esp'] and ind['ef'] < ind['es']
            if mom_on and mom_bp:
                new_ts = price - atr*trail
                if mom_ts is None or new_ts > mom_ts: mom_ts = new_ts
                gain = (price - mom_bp)/mom_bp
                stop_hit = price <= mom_ts; tp_hit = gain >= tp
                if stop_hit or tp_hit or cdn:
                    reason = 'TP' if tp_hit else ('trail' if stop_hit else 'cross')
                    qty = min(mom_qty, btc*0.999)
                    f = qty*price*FEE
                    usdc += qty*price - f; btc -= qty; fees += f
                    pnl = (price-mom_bp)*qty - f - mom_bp*qty*FEE
                    mom_real += pnl; monthly[mon] += pnl; mom_trades += 1
                    if pnl > 0: mom_wins += 1
                    mom_exits[reason] += 1
                    mom_on = False; mom_bp = None; mom_ts = None; mom_qty = None
                    mom_cd_until = ts + MOM_COOLDOWN_H*3600_000
            trigger = cup if mom_entry == 'cross' else True   # 'enter' = immer long im UPTREND
            if (trigger and not mom_on and usdc >= mom_ticket and ts >= mom_cd_until
                    and price >= ind['ereg'] and (not rsi70 or rsi < 70)):
                recent_buys = [t for t in recent_buys if ts - t <= 3600_000]
                if len(recent_buys) < G.WHIPSAW_MAX:
                    recent_buys.append(ts)
                    qty = round(mom_ticket/price, 5); f = mom_ticket*FEE
                    usdc -= mom_ticket; btc += qty; fees += f
                    mom_on = True; mom_bp = price; mom_qty = qty; mom_ts = price - atr*trail

        open_lots = sum(1 for lv in (bgrid or []) if lv['st'] == 'bought')
        max_open = max(max_open, open_lots)
        eq = usdc + tradeable(btc)*price
        eq_peak = max(eq_peak, eq); max_dd = max(max_dd, eq_peak - eq)

    end_price = ticks[-1][1]
    unreal = sum((end_price - lv['bp'])*lv['q'] for lv in (bgrid or []) if lv['st'] == 'bought')
    mom_unreal = (end_price - mom_bp)*mom_qty if mom_on else 0.0
    return dict(cycles=cycles, grid_real=round(realized, 2), grid_unreal=round(unreal, 2),
                mom_trades=mom_trades, mom_wins=mom_wins, mom_real=round(mom_real, 2),
                mom_unreal=round(mom_unreal, 2), mom_exits=mom_exits,
                total=round(realized+unreal+mom_real+mom_unreal, 2),
                fees=round(fees, 2), stops=stops, max_dd=round(max_dd, 2), max_open=max_open,
                monthly=monthly)

# ----------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2025-11-01')
    ap.add_argument('--symbol', default='BTC/USDC')
    ap.add_argument('--grid', default=None, help='depth,spread,dt_buy fuer Teil B, z.B. 4,0.0075,1')
    a = ap.parse_args()

    since = int(datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()*1000)
    until = int(time.time()*1000)
    df1 = G.fetch(a.symbol, '1h', since - G.CANDLE_WINDOW*3600_000*2, until, 'bt_cache')
    df5 = G.fetch(a.symbol, '5m', since, until, 'bt_cache'); df5 = df5[df5.ts >= since]
    hourly = precompute_hourly(df1)
    ticks = [t for t in zip(df5.ts.tolist(), df5.c.tolist()) if (t[0]-t[0] % 3600_000) in hourly]
    p0, p1 = ticks[0][1], ticks[-1][1]
    days = (ticks[-1][0]-ticks[0][0])/86400_000
    print(f"\nZeitraum {datetime.fromtimestamp(ticks[0][0]/1000, tz=timezone.utc):%d.%m.%Y} - "
          f"{datetime.fromtimestamp(ticks[-1][0]/1000, tz=timezone.utc):%d.%m.%Y} ({days:.0f} Tage) | "
          f"{a.symbol} {p0:,.0f} -> {p1:,.0f} ({(p1/p0-1)*100:+.1f}%) | B&H 890 $ = {890*(p1/p0-1):+.2f} $")
    months = sorted({datetime.fromtimestamp(t[0]/1000, tz=timezone.utc).strftime('%Y-%m') for t in ticks})

    def mrow(lab, r):
        return f"{lab:<30}" + ''.join(f"{r['monthly'].get(mo, 0):>8.1f}" for mo in months)

    # ---------------- TEIL A ----------------
    print("\n=== TEIL A: Stop-Fix vs. DOWNTREND-Kauf (EMA-Gate an, kein Momentum) ===")
    hdr = f"{'depth':>5} {'spread':>6} {'stopfix':>7} {'dtbuy':>5} | {'cycles':>6} {'real$':>7} {'unreal$':>7} {'total$':>7} {'maxDD$':>7} {'maxOpen':>7} {'stops':>5}"
    print(hdr); print('-'*len(hdr))
    rowsA = []
    for depth, spread, sf, dtb in itertools.product([2, 4], [0.0075, 0.01], [False, True], [False, True]):
        r = simulate(ticks, hourly, spread, depth, dtb, sf)
        r.update(depth=depth, spread=spread, stopfix=sf, dt_buy=dtb); rowsA.append(r)
        print(f"{depth:>5} {spread*100:>5.2f}% {'on' if sf else 'off':>7} {'on' if dtb else 'off':>5} | "
              f"{r['cycles']:>6} {r['grid_real']:>7.2f} {r['grid_unreal']:>7.2f} {r['total']:>7.2f} "
              f"{r['max_dd']:>7.2f} {r['max_open']:>7} {r['stops']:>5}")
    print("\nMonatlich ($):")
    print(f"{'config':<30}" + ''.join(f"{mo[2:]:>8}" for mo in months))
    for r in rowsA:
        if r['depth'] == 4:
            print(mrow(f"d4 {r['spread']*100:.2f}% sf{'+' if r['stopfix'] else '-'} dt{'+' if r['dt_buy'] else '-'}", r))

    # Grid-Konfig fuer Teil B
    if a.grid:
        d, s, dtb = a.grid.split(','); best = dict(depth=int(d), spread=float(s), dt_buy=bool(int(dtb)), stopfix=True)
    else:
        best = max(rowsA, key=lambda r: r['total'])
    print(f"\nTeil B auf: depth {best['depth']}, spread {best['spread']*100:.2f}%, "
          f"stopfix {'on' if best['stopfix'] else 'off'}, dt_buy {'on' if best['dt_buy'] else 'off'}")

    # ---------------- TEIL B ----------------
    print("\n=== TEIL B: Momentum im UPTREND on top ===")
    hdr = f"{'ticket':>6} {'entry':>5} {'trail':>5} {'tp':>4} {'rsi70':>5} | {'momTr':>5} {'wins':>4} {'TP/tr/x':>8} {'mom$':>8} {'momUnr$':>7} {'grid$':>7} {'total$':>8} {'maxDD$':>7}"
    print(hdr); print('-'*len(hdr))
    base = simulate(ticks, hourly, best['spread'], best['depth'], best['dt_buy'], best['stopfix'])
    print(f"{'off':>6} {'-':>5} {'-':>5} {'-':>4} {'-':>5} | {0:>5} {0:>4} {'-':>8} {0:>8.2f} {0:>7.2f} "
          f"{base['grid_real']+base['grid_unreal']:>7.2f} {base['total']:>8.2f} {base['max_dd']:>7.2f}")
    rowsB = []
    for ticket, entry, trail, tp, r70 in itertools.product([70, 150, 300], ['cross', 'enter'], [2.0, 3.0], [0.04, 0.08], [False, True]):
        r = simulate(ticks, hourly, best['spread'], best['depth'], best['dt_buy'], best['stopfix'],
                     ticket, entry, trail, tp, r70)
        r.update(ticket=ticket, entry=entry, trail=trail, tp=tp, rsi70=r70); rowsB.append(r)
        ex = r['mom_exits']
        print(f"{ticket:>6} {entry:>5} {trail:>5.1f} {tp*100:>3.0f}% {'on' if r70 else 'off':>5} | "
              f"{r['mom_trades']:>5} {r['mom_wins']:>4} {ex['TP']:>2}/{ex['trail']:>2}/{ex['cross']:>2} "
              f"{r['mom_real']:>8.2f} {r['mom_unreal']:>7.2f} {r['grid_real']+r['grid_unreal']:>7.2f} "
              f"{r['total']:>8.2f} {r['max_dd']:>7.2f}")
    top = sorted(rowsB, key=lambda r: r['total'], reverse=True)[:3]
    print("\nMonatlich ($) - Basis vs. Top 3 Momentum:")
    print(f"{'config':<30}" + ''.join(f"{mo[2:]:>8}" for mo in months))
    print(mrow("grid only", base))
    for r in top:
        print(mrow(f"m{r['ticket']} {r['entry']} t{r['trail']:.0f} tp{r['tp']*100:.0f} r{'+' if r['rsi70'] else '-'}", r))

    pd.DataFrame([{k: v for k, v in r.items() if k not in ('monthly', 'mom_exits')} for r in rowsA]).to_csv('backtest_ext_A.csv', index=False)
    pd.DataFrame([{k: v for k, v in r.items() if k not in ('monthly', 'mom_exits')} for r in rowsB]).to_csv('backtest_ext_B.csv', index=False)
    print("\nErgebnisse: backtest_ext_A.csv, backtest_ext_B.csv")

if __name__ == '__main__':
    main()