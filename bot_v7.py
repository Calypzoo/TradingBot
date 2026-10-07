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
# ALL-WEATHER BOT v7.6
# SIDEWAYS  = Bull Grid 12L @ 0.75% (buy low sell high)  [v7.6.11]
# UPTREND   = hold (Momentum-Entries seit v7.6.11 aus)
# DOWNTREND = Bear Grid 4L @ 0.75%  (sell high buy back lower)  [v7.6.7]
# ADX hysteresis: enter>25, exit<15
# RSI filter: skip bull grid buys when RSI>58
# v7.6: non-liquidating mode switch, e50 regime gate, min-ticket,
#       min-profit, whipsaw cooldown, bear-buyback bugfix
# v7.6.5: trade watchdog (stale-trade alarm + last-trade age in summary)
# v7.6.6: watchdog threshold 48h -> 72h (kalibriert an Livedaten 07-08/2026)
# v7.6.7: BEAR_LEVELS 8 -> 4 (Bear Grid war dauerhaft unter MIN_TICKET_USD)
# v7.6.8: Regime-Gate EMA50 -> EMA20 (Sweep auf echten 1h-Daten, 6 Mon.),
#         Momentum-Ticket getrennt (MOM_ORDER_AMOUNT), Grid-Ticket 40 -> 70,
#         Positionsmenge wird pro Level/Momentum gespeichert (kein Over-Sell)
# v7.6.9: Momentum-Whipsaw-Fix: (a) Indikatoren nur auf GESCHLOSSENEN Kerzen
#         (kein Intra-Stunden-Repaint), (b) Trend->Gegentrend nur bei ADX>=25,
#         sonst SIDEWAYS, (c) Momentum-Re-Entry-Cooldown nach Exit/Abbruch
# v7.6.10: BTC_CORE_MIN - Kernbestand, den der Bot nie verkauft (Bear Grid,
#          Stop-Loss, sell_all). Vom Bot selbst eroeffnete Positionen (Bull-
#          Level, Momentum) bleiben voll schliessbar.
# v7.6.11: Momentum-Entries AUS (Sweep 04-10/2026: Momentum verliert in 24/24
#          Paarungen, -10.74 $ Bot-Ertrag), BULL_SPREAD 1.0% -> 0.75% (16/16
#          besser, +0.87 $). Exit-Pfad fuer evtl. offene Momentum-Position
#          bleibt aktiv. Kern unveraendert 0.002 (Risikoentscheidung des Users).
# ================================================================
SYMBOL          = 'BTC/USDC'
TIMEFRAME       = '1h'
ORDER_AMOUNT    = 70    # v7.6.8: 40 -> 70. Grid-Ticket (Bull Buy/Sell). Fee-Quote
                        # unveraendert (prozentual), aber mehr Kapital pro Zyklus.
                        # Worst case Bull Grid: 6 untere Levels * 70 = $420.
MOMENTUM_ENABLED = False # v7.6.11: Momentum-Entries aus. Lever-Sweep 04-10/2026
                        # (48 Laeufe, geschlossene Kerzen, v7.6.9-Fixes aktiv):
                        # Momentum AN verliert in 24 von 24 Paarungen gegen AUS,
                        # Ø -1.2 pp Return, -10.74 $ Bot-Ertrag, Trefferquote
                        # 110/464. Live 18.09.-07.10.: 0 von 7. Der Exit-Pfad
                        # (Trail/TP/Cross) bleibt aktiv, damit eine evtl. noch
                        # offene Position sauber geschlossen wird. UPTREND ohne
                        # Momentum = halten (Inventar + Kern), kein Grid.
MOM_ORDER_AMOUNT = 150  # Ticket fuer Momentum-Entries, nur wenn MOMENTUM_ENABLED.
                        # Bisher lief Momentum mit ORDER_AMOUNT=40: der +18.8%-Trade
                        # vom 22.08. brachte $7.52. Mit 150 waeren es $28.
                        # Risiko: Trail-Stop 2xATR = typ. 2-4% von 150 = $3-6 pro Stop.
MAX_SPEND       = 900
STOP_LOSS_PCT   = 0.12
CHECK_INTERVAL  = 120
RESTART_WAIT    = 600
BULL_LEVELS     = 12
BULL_SPREAD     = 0.0075 # v7.6.11: 1.0% -> 0.75%. Sweep: 0.75% schlaegt 1.0% in
                        # 16/16 Paarungen (+0.87 $ Bot-Ertrag/6 Mon.), 0.5% faellt
                        # wegen Fees wieder ab.
BEAR_LEVELS     = 4     # v7.6.7: 8 -> 4. Order pro Level = btc*MAX_BTC_SELL/LEVELS.
                        # Bei 0.00239 BTC (~$153): 8 Levels = $15.31, 5 = $24.50,
                        # 4 = $30.63. Nur 4 liegt ueber MIN_TICKET_USD ($25), alles
                        # darueber laesst das Bear Grid dauerhaft pausieren.
                        # ACHTUNG: faellt der BTC-Gegenwert unter ~$125, blockiert
                        # auch 4. Der Watchdog (v7.6.5) meldet das dann.
                        # Optimale Level-Zahl: siehe backtest_regime.py Sweep.
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
 
# ================================================================
# v7.6 PATCHES
#   Backtest-validiert (5m, Jan-Mai 2026): Improved schlaegt Baseline 5/5
#   Monate, v.a. durch (a) keine Mode-Switch-Mass-Liquidation und (b) keinen
#   Kauf ins fallende Messer. Alle Gates sind ZUSATZ-Bedingungen: sie koennen
#   nur Kaeufe VERHINDERN, nie einen Trade erzwingen.
#   HINWEIS Regime-Span: Backtest lief auf 5m (EMA150 = 12.5h). Auf 1h wurde
#   zunaechst e50 als Naeherung genutzt (50h, ~4x traeger). v7.6.8: Sweep auf
#   echten 1h-Daten (Mar-Sep 2026, 30 Kombinationen) -> EMA20 schlaegt EMA50
#   in jeder Bear-Level-Variante (Ø +2.11% vs +1.39%). 20h liegt zudem am
#   naechsten an den validierten 12.5h. Kuerzere Spannen (<20) noch ungetestet.
# ================================================================
BOT_VERSION         = 'v7.6.11'
NON_LIQUIDATING     = True      # Mode-Switch & Recenter liquidieren NICHT mehr
MIN_TICKET_USD      = 25.0      # keine Entry-Orders < diesem Wert (Anti-Fragmentierung)
MIN_PROFIT_PCT      = 0.0020    # Bull-Sell nur wenn >= 0.20% ueber Einstand (> Fee-Huerde ~0.15%)
REGIME_FILTER       = True      # kein Bull-Grid-Kauf wenn Preis < Regime-EMA (Falling-Knife-Schutz)
REGIME_SPAN         = 20        # v7.6.8: EMA-Spanne des Regime-Gates auf 1h (vorher 50).
                                # Braucht REGIME_SPAN*3 Kerzen; fetch_ohlcv limit=80
                                # reicht bis Spanne 26. Fuer laengere Spannen limit erhoehen!
WHIPSAW_MAX_TRADES  = 6         # max. Entry-Buys pro rollender Stunde, dann Cooldown

# ================================================================
# v7.6.9 MOMENTUM-WHIPSAW-FIX
#   Befund 18.09.-07.10.: 7 Momentum-Entries a $150, 0 Gewinner. Am 06.10.
#   drei Entries in 3h (00:03, 02:12, 02:58), dazwischen Bear-Grid-Abverkauf.
#   Ursachen: (a) Signale liefen auf der LAUFENDEN 1h-Kerze -> EMA9/21-Cross
#   konnte mehrfach pro Stunde feuern (Repaint). (b) UPTREND<->DOWNTREND
#   wechselte ohne ADX-Hysterese (ADX_ENTER galt nur aus SIDEWAYS).
#   (c) Kein Cooldown: nach Mode-Abbruch sofort neuer Entry moeglich.
# ================================================================
CLOSED_CANDLES_ONLY = True      # Indikatoren ohne die laufende Kerze berechnen
MOM_COOLDOWN_H      = 4         # Stunden Sperre fuer Momentum-Entry nach Exit/Abbruch

# ================================================================
# v7.6.10 BTC-KERNBESTAND
#   Befund 18.07.-07.10.: Bot +37 USDC, Buy & Hold ~+331. Grund: der Bot
#   verkauft in jeder Rally sein BTC (Bear Grid, Stop-Loss) und steht dann
#   mit USDC daneben. BTC_CORE_MIN ist ein Boden in BTC, unter den KEIN
#   Verkaufspfad geht:
#     - Bear Grid dimensioniert seine Levels nur aus (btc - core)
#     - Stop-Loss / sell_all verkaufen nur (btc - core)
#     - Bull-Level und Momentum verkaufen weiterhin GENAU die Menge, die
#       sie selbst gekauft haben (lv['q'], mom_qty) - der Bot kann seine
#       eigenen Positionen immer schliessen.
#   Konsequenzen, bewusst:
#     - Der Kern faellt in einem Crash mit. Der 12%-Stop schuetzt ihn NICHT.
#     - Weniger handelbares BTC -> kleinere Bear-Orders; faellt
#       (btc-core)*0.8/BEAR_LEVELS*Preis unter MIN_TICKET_USD, pausiert
#       das Bear Grid (wird geloggt).
#   Wert in BTC, nicht USD: ein USD-Kern wuerde bei steigendem Kurs BTC
#   abverkaufen - das Gegenteil von Halten.
#   Startwert 0.002 = Bestand laut Export 07.10. (0.00204 BTC). Hoeher
#   setzen heisst: BTC kaufen UND diesen Wert anpassen.
# ================================================================
BTC_CORE_MIN        = 0.002     # BTC, die der Bot nie verkauft (0 = aus)
 
# ================================================================
# v7.6.5 WATCHDOG
#   Hintergrund: Im April lief der Bot 24 Tage ohne einen einzigen Trade,
#   ohne dass es auffiel. Der Watchdog macht Trade-Stille sichtbar:
#   Telegram-Alarm, sobald STALE_TRADE_HOURS ohne ausgefuehrte Order
#   vergangen sind (max. 1 Erinnerung pro STALE_REMIND_HOURS). Zusaetzlich
#   zeigt die stuendliche Summary jetzt das Alter des letzten Trades.
#   Reines Monitoring - kann keinen Trade ausloesen oder verhindern.
# ================================================================
STALE_TRADE_HOURS   = 72        # Alarm, wenn so lange keine Order ausgefuehrt wurde
                                # v7.6.6: 48h -> 72h. Livedaten 18.07.-19.08.: 7 Luecken
                                # >48h im Normalbetrieb, nur 1 >72h (max 4 Tage).
STALE_REMIND_HOURS  = 24        # Alarm-Erinnerung hoechstens einmal pro X Stunden
 
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
    # v7.6.1: fetchCurrencies=False -> load_markets ruft NICHT den
    # geo-gesperrten SAPI-Endpoint capital/config/getall auf (451 in DE/EU).
    # Bot braucht keine Currency-Metadaten; Kurse/Balance/Orders laufen ueber api/v3.
    'options': {'defaultType': 'spot', 'fetchCurrencies': False},
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
    # v7.6.9: Binance liefert die laufende Kerze als letztes Element.
    # Fuer stabile Signale (kein Repaint) wird sie hier abgeschnitten.
    # Der LIVE-Preis fuer Grid/Trail bleibt davon unberuehrt (closes[-1]
    # am Aufrufer). ind['close'] = letzter GESCHLOSSENER Close.
    if CLOSED_CANDLES_ONLY and len(closes) > 1:
        closes, highs, lows = closes[:-1], highs[:-1], lows[:-1]
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
    ereg = _ema(closes[-REGIME_SPAN*3:], REGIME_SPAN)  # v7.6.8: war e50 fix
 
    gs, ls = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        gs.append(max(d,0)); ls.append(max(-d,0))
    ag = sum(gs[-14:])/14 or 1e-10
    al = sum(ls[-14:])/14 or 1e-10
    rsi = 100 - 100/(1 + ag/al)
 
    return {'atr': max(atr,1), 'ef': ef, 'es': es, 'efp': efp,
            'esp': esp, 'ereg': ereg, 'rsi': rsi, 'adx': adx,
            'close': closes[-1]}  # v7.6.9: letzter geschlossener Close
 
def mode(ind, closes, last=None):
    adx  = ind['adx']
    # v7.6.9: Regime-Vergleich auf geschlossenem Close (ind['close']), nicht
    # auf dem Live-Tick -> kein Flattern um die EMA20 innerhalb der Stunde.
    c    = ind.get('close', closes[-1])
    bull = ind['ef'] > ind['es'] and c > ind['ereg']
    bear = ind['ef'] < ind['es'] and c < ind['ereg']
    if last is None or last == 'SIDEWAYS':
        if adx >= ADX_ENTER:
            if bull: return 'UPTREND'
            if bear: return 'DOWNTREND'
        return 'SIDEWAYS'
    if adx < ADX_EXIT:
        return 'SIDEWAYS'
    # v7.6.9: Direkter Wechsel Trend -> Gegentrend nur mit ADX >= ADX_ENTER.
    # Vorher reichte ADX >= ADX_EXIT (15): UPTREND<->DOWNTREND flatterte bei
    # schwachem Trend frei hin und her. Schwache Umkehr -> SIDEWAYS (dort
    # greift dann wieder die normale 25er-Eintrittsschwelle).
    if last == 'UPTREND':
        if bull: return 'UPTREND'
        if bear: return 'DOWNTREND' if adx >= ADX_ENTER else 'SIDEWAYS'
        return 'UPTREND'
    if last == 'DOWNTREND':
        if bear: return 'DOWNTREND'
        if bull: return 'UPTREND' if adx >= ADX_ENTER else 'SIDEWAYS'
        return 'DOWNTREND'
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
def buy_usdc(amount_usdc, price, is_entry=True):
    try:
        if is_entry and amount_usdc < MIN_TICKET_USD:
            log(f"SKIP BUY < min ticket (${amount_usdc:.2f} < ${MIN_TICKET_USD:.0f})")
            return None
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
 
def tradeable_btc(btc):
    """v7.6.10: BTC, ueber die der Bot verfuegen darf (Bestand minus Kern)."""
    return max(0.0, btc - BTC_CORE_MIN)

def sell_all(price, reason):
    _, btc = get_balance()
    qty = round(tradeable_btc(btc)*0.999, 5)   # v7.6.10: Kern bleibt
    if qty > 0.00001:
        _retry(lambda: exchange.create_market_sell_order(SYMBOL, qty))
        log(f"SELL ALL {qty} BTC @ ~${price:,.0f} | {reason}"
            + (f" | core kept {BTC_CORE_MIN:.5f}" if BTC_CORE_MIN > 0 else ""))
    elif BTC_CORE_MIN > 0:
        log(f"SELL ALL skipped ({reason}): balance {btc:.5f} <= core {BTC_CORE_MIN:.5f}")
 
# ================================================================
# SUMMARY
# ================================================================
def summary(stats, usdc, btc, price, m, last_trade_ts=None):
    total = usdc + btc*price
    pnl   = total - stats['start_balance'] if stats['start_balance'] else 0
    pct   = pnl/stats['start_balance']*100 if stats['start_balance'] else 0
    # v7.6.5: Alter des letzten Trades (None = noch keiner bekannt)
    lt = (f"{(time.time()-last_trade_ts)/3600:.1f}h ago"
          if last_trade_ts else "none yet")
    log("=" * 52)
    log(f"  Mode     : {m}")
    log(f"  BTC      : ${price:,.2f}")
    log(f"  USDC     : ${usdc:,.2f}")
    log(f"  BTC held : {btc:.6f} (~${btc*price:,.2f})")
    log(f"  Total    : ${total:,.2f}")
    log(f"  PnL      : ${pnl:+.2f} ({pct:+.2f}%)")
    log(f"  Profit   : ${stats['total_profit']:+.4f} | Cycles: {stats['total_cycles']}")
    log(f"  Bull/Bear/Mom: {stats['bull_cycles']}/{stats['bear_cycles']}/{stats['mom_cycles']}")
    log(f"  LastTrade: {lt}")
    log("=" * 52)
    telegram(
        f"Mode: {m} | BTC: ${price:,.0f}\n"
        f"Balance: ${total:,.2f} | PnL: ${pnl:+.2f} ({pct:+.2f}%)\n"
        f"Profit: ${stats['total_profit']:+.2f} | "
        f"B/Br/M: {stats['bull_cycles']}/{stats['bear_cycles']}/{stats['mom_cycles']}\n"
        f"Last trade: {lt}"
        + (f"\nBTC {btc:.5f} | core {BTC_CORE_MIN:.5f} | tradeable {tradeable_btc(btc):.5f}"
           if BTC_CORE_MIN > 0 else "")
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
            f"Grid: ${ORDER_AMOUNT} | Mom: {('$'+str(MOM_ORDER_AMOUNT)) if MOMENTUM_ENABLED else 'off'} | Check: {CHECK_INTERVAL}s\n"
            f"Bull: {BULL_SPREAD*100:.2f}% | Bear: {BEAR_SPREAD*100:.2f}% | RSI<{RSI_BUY_MAX}"
        )

    # v7.6.10: Kernbestand pruefen - bei jedem Session-Start, nicht nur beim
    # allerersten. Liegt der Bestand unter dem Kern, kann der Bot nichts aus
    # dem Altbestand verkaufen (Bear Grid, Stop-Loss inaktiv) - das soll
    # sichtbar sein, nicht still passieren.
    if BTC_CORE_MIN > 0:
        _, _btc0 = get_balance()
        _trd0 = tradeable_btc(_btc0)
        log(f"BTC core: balance {_btc0:.5f} | core {BTC_CORE_MIN:.5f} | tradeable {_trd0:.5f}")
        if _btc0 < BTC_CORE_MIN:
            log(f"WARNING: balance below BTC_CORE_MIN - bear grid & stop-loss inactive "
                f"until balance > core")
            telegram(f"WARNING: BTC {_btc0:.5f} < core {BTC_CORE_MIN:.5f}\n"
                     f"Bear grid & stop-loss inactive until balance > core")
 
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
    mom_qty    = state.get('mom_qty', None)  # v7.6.8: None bei Alt-State
    mom_cd_until = state.get('mom_cd_until', 0.0)  # v7.6.9: Re-Entry-Sperre (epoch)
    mom_ts     = state.get('mom_ts', None)
    dirty      = False
    bear_paused = False  # v7.6.4: einmaliges Logging der Bear-Grid-Pause
 
    # v7.6.5 Watchdog: letzter ausgefuehrter Trade (epoch) + Alarm-Drossel.
    # last_trade_ts wird im State persistiert; vor dem ersten Trade dient der
    # Session-Start als Referenz (Restart verschiebt den Alarm schlimmstenfalls
    # um eine Session, loest aber keinen Fehlalarm aus).
    session_start    = time.time()
    last_trade_ts    = state.get('last_trade_ts')
    last_stale_alert = 0.0
 
    # v7.6 Whipsaw-Cooldown: max WHIPSAW_MAX_TRADES Entry-Buys pro rollender Stunde
    recent_buys = []
    def _whipsaw_ok():
        now = time.time()
        while recent_buys and now - recent_buys[0] > 3600:
            recent_buys.pop(0)
        if len(recent_buys) >= WHIPSAW_MAX_TRADES:
            return False
        recent_buys.append(now)
        return True
 
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
                summary(stats, usdc, btc, price, m, last_trade_ts)
                last_hour = h
 
            # v7.6.5 Watchdog: Alarm bei zu langer Trade-Stille
            _wd_ref = last_trade_ts or session_start
            stale_h = (time.time() - _wd_ref) / 3600
            if (stale_h >= STALE_TRADE_HOURS
                    and time.time() - last_stale_alert >= STALE_REMIND_HOURS*3600):
                why = ""
                if bear_paused and m == 'DOWNTREND':
                    why = "\nBear grid PAUSED (BTC below min ticket)"
                elif m == 'UPTREND' and not MOMENTUM_ENABLED:
                    why = "\nUPTREND hold, momentum off: no trades expected"
                log(f"WATCHDOG: no executed trade for {stale_h:.0f}h | mode {m}")
                telegram(f"WATCHDOG: no trade for {stale_h:.0f}h\n"
                         f"Mode: {m} | BTC: ${price:,.0f}{why}")
                last_stale_alert = time.time()
 
            # Mode switch
            if m != last_mode and last_mode is not None:
                log(f"MODE: {last_mode} → {m}")
                telegram(f"Mode: {last_mode} → {m}\nBTC: ${price:,.0f}")
 
                if NON_LIQUIDATING:
                    # v7.6: KEINE Mass-Liquidation beim Moduswechsel.
                    # Bestehendes BTC bleibt im Wallet (kein erzwungener Verlust);
                    # Momentum-Position wird weiterhin sauber ueber ihren Trail/TP
                    # geschlossen, solange wir im UPTREND sind - nicht hart hier.
                    log("Mode switch: non-liquidating (Positionen werden gehalten)")
                else:
                    if last_mode == 'SIDEWAYS' and btc > 0.00001:
                        sell_all(price, "leaving SIDEWAYS")
                        time.sleep(3); usdc, btc = get_balance()
 
                    if last_mode == 'UPTREND' and mom_on and btc > 0.00001:
                        sell_all(price, "leaving UPTREND")
                        time.sleep(3); usdc, btc = get_balance()
                        mom_on = False; mom_bp = None; mom_ts = None; mom_qty = None
                        mom_cd_until = time.time() + MOM_COOLDOWN_H*3600  # v7.6.9
 
                    if last_mode == 'DOWNTREND' and nsold > 0.00001:
                        cost = nsold * price
                        if usdc >= cost * 1.001:
                            # FIX v7.6: vorher sell_btc(-nsold) -> immer None (Bug);
                            # Rueckkauf direkt als Market-Buy.
                            o = _retry(lambda: exchange.create_market_buy_order(
                                SYMBOL, round(nsold, 5)))
                            if o is not None:
                                log(f"BEAR BUYBACK: {nsold:.5f} BTC")
                                telegram(f"Bear buyback {nsold:.5f} BTC @ ${price:,.0f}")
                                last_trade_ts = time.time()  # v7.6.5
                                nsold = 0; time.sleep(3); usdc, btc = get_balance()
                        else:
                            log(f"WARNING: Can't afford buyback ${cost:,.0f}")
 
                # Momentum-Status zuruecksetzen (keine offene Trail-Verwaltung ausserhalb UPTREND)
                if last_mode == 'UPTREND':
                    if mom_on:
                        # v7.6.9: Position wird als Inventar weitergefuehrt (non-liq),
                        # aber ein neuer Entry ist fuer MOM_COOLDOWN_H gesperrt.
                        mom_cd_until = time.time() + MOM_COOLDOWN_H*3600
                        log(f"MOM abandoned on mode switch -> cooldown {MOM_COOLDOWN_H}h")
                    mom_on = False; mom_bp = None; mom_ts = None; mom_qty = None
 
                bgrid = []; bspent = 0
                ngrid = []; nsold  = 0
 
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
                    log(f"Bull grid @ ${bcenter:,.0f} | {BULL_LEVELS}L | {BULL_SPREAD*100:.2f}%")
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
                    if NON_LIQUIDATING:
                        # v7.6: nicht verkaufen; nur Grid neu zentrieren. Bereits
                        # gekaufte Positionen bleiben im Wallet (kein Zwangsverkauf).
                        log(f"BULL RECENTER (non-liq) @ ${price:,.0f}")
                        bgrid = bull_grid(price); bcenter = price
                        blast = price; bspent = 0; dirty = True
                        telegram(f"Bull recentered (non-liq) ${price:,.0f}")
                    else:
                        log(f"BULL RECENTER @ ${price:,.0f}")
                        sell_all(price, "recenter"); time.sleep(3)
                        usdc, btc = get_balance()
                        bgrid = bull_grid(price); bcenter = price
                        blast = price; bspent = 0; dirty = True
                        telegram(f"Bull recentered ${price:,.0f}")
 
                for lv in bgrid:
                    gp = lv['p']
                    # BUY — RSI filter + v7.6 Regime-Gate + Whipsaw-Cooldown
                    if (price <= gp < blast
                            and lv['st'] == 'ready'
                            and usdc >= ORDER_AMOUNT
                            and bspent < MAX_SPEND
                            and rsi < RSI_BUY_MAX
                            and (not REGIME_FILTER or price >= ind['ereg'])):
                        if _whipsaw_ok() and buy_usdc(ORDER_AMOUNT, price):
                            lv['st'] = 'bought'; lv['bp'] = price
                            lv['q']  = round(ORDER_AMOUNT / price, 5)  # v7.6.8: Menge merken
                            bspent += ORDER_AMOUNT; usdc -= ORDER_AMOUNT
                            last_trade_ts = time.time()  # v7.6.5
                            dirty = True
                    # SELL — v7.6: nur mit Mindestprofit (> Fee-Huerde)
                    elif (price >= gp > blast
                            and lv['st'] == 'bought'
                            and lv['bp']
                            and price >= lv['bp'] * (1 + MIN_PROFIT_PCT)):
                        bp  = lv['bp']
                        # v7.6.8: gespeicherte Menge verkaufen. Levels aus v7.6.7
                        # haben kein 'q' (Ticket war $40) -> auf Bestand kappen,
                        # sonst SELL FAILED (insufficient balance) in Endlosschleife.
                        qty = lv.get('q') or min(ORDER_AMOUNT / bp, tradeable_btc(btc) * 0.999)
                        if sell_btc(qty, price):
                            profit = (price - bp) * qty
                            lv['st'] = 'ready'; lv['bp'] = None; lv['q'] = None
                            last_trade_ts = time.time()  # v7.6.5
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
 
                # v7.6.10: nur der handelbare Teil (Bestand minus Kern) wird
                # auf die Bear-Levels verteilt.
                trd = tradeable_btc(btc)
                bpl = (trd * MAX_BTC_SELL) / BEAR_LEVELS if trd > 0.00001 else 0
 
                # v7.6.4: Anti-Fragmentierung im Bear Grid - keine Orders unter
                # MIN_TICKET_USD. Ist der BTC-Bestand dafuer zu klein, pausiert
                # der Bear-Zyklus (gewolltes Verhalten, kein Fehler).
                if bpl * price < MIN_TICKET_USD:
                    if not bear_paused:
                        log(f"BEAR PAUSED: per-level ${bpl*price:.2f} < min ticket "
                            f"${MIN_TICKET_USD:.0f} | btc {btc:.5f} core {BTC_CORE_MIN:.5f} "
                            f"tradeable {trd:.5f}")
                        bear_paused = True
                    bpl = 0
                else:
                    bear_paused = False
 
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
                            last_trade_ts = time.time()  # v7.6.5
                            log(f"BEAR SELL {bpl:.5f} BTC @ ${price:,.0f}")
                    # BUY BACK cheaper
                    elif (price <= gp < nlast
                            and lv['st'] == 'sold'
                            and lv['sp']):
                        sp   = lv['sp']
                        cost = bpl * price
                        # v7.6.4: Buyback nur wenn Gewinn > Fee-Huerde
                        # (vorher: price < sp -> Cent-Gewinne, Fees fressen alles)
                        if usdc >= cost*1.001 and price <= sp * (1 - MIN_PROFIT_PCT):
                            if buy_usdc(cost, price, is_entry=False):
                                profit = (sp - price) * bpl
                                lv['st'] = 'ready'; lv['sp'] = None
                                last_trade_ts = time.time()  # v7.6.5
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
                        # v7.6.8: gespeicherte Menge; Position aus v7.6.7 (Ticket $40,
                        # kein mom_qty im State) -> auf Bestand kappen
                        qty = mom_qty or min(MOM_ORDER_AMOUNT/mom_bp, tradeable_btc(btc)*0.999)
                        qty = min(qty, btc*0.999)   # eigene Position: Kern gilt hier nicht
                        if sell_btc(qty, price):
                            profit = (price - mom_bp) * qty
                            last_trade_ts = time.time()  # v7.6.5
                            stats['total_profit'] += profit
                            stats['total_cycles'] += 1
                            stats['mom_cycles']   += 1
                            save_stats(stats); dirty = True
                            log(f"MOM SELL ({reason}) {gain*100:.2f}% ${profit:+.4f}")
                            telegram(f"Momentum sell ({reason})\n{gain*100:.2f}% ${profit:+.4f}")
                            mom_on = False; mom_bp = None; mom_ts = None; mom_qty = None
                            mom_cd_until = time.time() + MOM_COOLDOWN_H*3600  # v7.6.9
 
                if MOMENTUM_ENABLED and cup and not mom_on and usdc >= MOM_ORDER_AMOUNT \
                        and time.time() >= mom_cd_until \
                        and (not REGIME_FILTER or price >= ind['ereg']):
                    if _whipsaw_ok() and buy_usdc(MOM_ORDER_AMOUNT, price):
                        mom_on = True; mom_bp = price
                        mom_qty = round(MOM_ORDER_AMOUNT / price, 5)  # v7.6.8
                        mom_ts = price - atr*TRAIL_ATR_MULT
                        last_trade_ts = time.time()  # v7.6.5
                        dirty  = True
                        log(f"MOM BUY ${MOM_ORDER_AMOUNT} @ ${price:,.0f} trail ${mom_ts:,.0f}")
                        telegram(f"Momentum buy ${MOM_ORDER_AMOUNT} @ ${price:,.0f}\nTrail ${mom_ts:,.0f}")
 
            if dirty:
                save_state({
                    'mode': m,
                    'bgrid': bgrid, 'bcenter': bcenter,
                    'blast': blast, 'bspent': bspent,
                    'ngrid': ngrid, 'ncenter': ncenter,
                    'nlast': nlast, 'nsold': nsold,
                    'mom_on': mom_on, 'mom_bp': mom_bp, 'mom_ts': mom_ts,
                    'mom_qty': mom_qty,  # v7.6.8
                    'mom_cd_until': mom_cd_until,  # v7.6.9
                    'last_trade_ts': last_trade_ts,  # v7.6.5
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
        'mom_qty': mom_qty,  # v7.6.8
        'mom_cd_until': mom_cd_until,  # v7.6.9
        'last_trade_ts': last_trade_ts,  # v7.6.5
    })
    return 'shutdown'
 
# ================================================================
# ENTRY POINT
# ================================================================
def main():
    log("=" * 52)
    log(f"ALL-WEATHER BOT {BOT_VERSION}")
    log(f"Symbol  : {SYMBOL}")
    log(f"Order   : grid ${ORDER_AMOUNT} | mom {('$'+str(MOM_ORDER_AMOUNT)) if MOMENTUM_ENABLED else 'OFF'} | Max: ${MAX_SPEND}")
    log(f"v7.6.11 : momentum_enabled={MOMENTUM_ENABLED} bull_spread={BULL_SPREAD*100:.2f}%")
    log(f"Check   : {CHECK_INTERVAL}s")
    log(f"Bull    : {BULL_LEVELS}L @ {BULL_SPREAD*100:.2f}% | RSI<{RSI_BUY_MAX}")
    log(f"Bear    : {BEAR_LEVELS}L @ {BEAR_SPREAD*100:.2f}%")
    log(f"Mom     : EMA{EMA_FAST}/{EMA_SLOW} trail {TRAIL_ATR_MULT}x ATR")
    log(f"ADX     : enter>{ADX_ENTER} exit<{ADX_EXIT}")
    log(f"v7.6    : non_liq={NON_LIQUIDATING} regime(EMA{REGIME_SPAN})={REGIME_FILTER} "
        f"min_ticket=${MIN_TICKET_USD:.0f} min_profit={MIN_PROFIT_PCT*100:.2f}% "
        f"whipsaw={WHIPSAW_MAX_TRADES}/h")
    log(f"v7.6.5  : watchdog alarm >{STALE_TRADE_HOURS}h no-trade, "
        f"remind every {STALE_REMIND_HOURS}h")
    log(f"v7.6.9  : closed_candles={CLOSED_CANDLES_ONLY} trend-flip needs ADX>={ADX_ENTER} "
        f"mom_cooldown={MOM_COOLDOWN_H}h")
    log(f"v7.6.10 : btc_core_min={BTC_CORE_MIN:.5f} BTC (never sold by bot)")
    log(f"Telegram: {'ON' if TELEGRAM_TOKEN else 'OFF'}")
    log("=" * 52)
    telegram(f"ALL-WEATHER BOT {BOT_VERSION} ONLINE")
 
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
 





