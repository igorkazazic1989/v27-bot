#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V29-ASI-Self-Improving.py - Självförbättrande ASI baserad på bästa regeln

BÄSTA REGEL från brute-force på din 3-årsdata:
  Two down days hold 3d: +238.1% 83 trades WR 59% 2.2/mån 0.55/vecka
  (mest lönsam av alla högfrekventa)

Detta är V27 fast:
1. Köper när 2 dagar i rad är ner (Close < föregående Close) - inte bara 1 röd
2. Håller 3 trading-dagar istället för 2
3. Självförbättrande: loggar alla trades i v29_asi_trades.json och räknar WR/PF
   Om senaste 10 trades WR < 50%, skärper den automatiskt med RSI<55 filter

Kör lokalt:
  python3 ~/Desktop/V29-ASI-Self-Improving.py --symbol NVDA --dry-run
  python3 ~/Desktop/V29-ASI-Self-Improving.py --symbol NVDA --live --notional 10000

Kör på GitHub Actions (samma som V27):
  python V29-ASI-Self-Improving.py --symbol NVDA --live --notional 10000
"""

import os, json, argparse, math
from datetime import datetime, timedelta
from pathlib import Path
import numpy as np

TRADES_FILE = Path.home() / "Desktop" / "v29_asi_trades.json"
# För GitHub Actions runner:
if not TRADES_FILE.parent.exists():
    TRADES_FILE = Path("/home/runner/Desktop/v29_asi_trades.json")
LOG_FILE = Path.home() / "Desktop" / "v29_asi.log"
if not LOG_FILE.parent.exists():
    LOG_FILE = Path("/home/runner/Desktop/v29_asi.log")

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        TRADES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line+"\n")
    except: pass

def get_keys():
    k=os.getenv("APCA_API_KEY_ID"); s=os.getenv("APCA_API_SECRET_KEY")
    if not k or not s:
        log("Saknar APCA_API_KEY_ID / SECRET")
        return None,None
    return k,s

def rsi_wilder(closes, n=14):
    closes = np.array(closes, float)
    if len(closes) < n+1:
        return 50
    deltas = np.diff(closes, prepend=closes[0])
    up = np.where(deltas>0, deltas, 0)
    down = np.where(deltas<0, -deltas, 0)
    up_ema = np.zeros(len(closes)); down_ema = np.zeros(len(closes))
    up_ema[n-1] = np.mean(up[:n]); down_ema[n-1] = np.mean(down[:n])
    for i in range(n, len(closes)):
        up_ema[i] = (up_ema[i-1]*(n-1)+up[i])/n
        down_ema[i] = (down_ema[i-1]*(n-1)+down[i])/n
    rs = up_ema[-1]/(down_ema[-1]+1e-9)
    return 100 - 100/(1+rs)

def get_two_down_signal(symbol):
    """Kollar om senaste 2 dagarna var ner-dagar på IEX"""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from alpaca.data.enums import DataFeed
    except:
        log("pip install alpaca-py behövs")
        return None
    
    k,s = get_keys()
    if not k: return None
    client = StockHistoricalDataClient(k,s)
    end = datetime.now() - timedelta(minutes=20)
    start = end - timedelta(days=20)  # 20 dagar för att få RSI också
    
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, adjustment="all", feed=DataFeed.IEX)
    try:
        bars = client.get_stock_bars(req)
        df = bars.df
        if len(df) < 5:
            log("För lite IEX data för two_down")
            return None
        df = df.reset_index() if hasattr(df,'reset_index') else df
        df = df.sort_values('timestamp')
        # Ta senaste 5 dagarna
        last = df.tail(5)
        closes = [float(x) for x in last['close'].values]
        opens = [float(x) for x in last['open'].values]
        dates = [str(x.date()) for x in last['timestamp']]
        
        # Ret = close vs föregående close
        ret1 = closes[-2] / closes[-3] - 1 if len(closes)>=3 else 0
        ret2 = closes[-1] / closes[-2] - 1 if len(closes)>=2 else 0
        
        is_two_down = ret1 < 0 and ret2 < 0
        # RSI för self-improving filter
        all_closes = [float(x) for x in df['close'].values]
        rsi = rsi_wilder(all_closes, 14)
        
        return dict(
            date_2=str(dates[-3]), close_2=closes[-3],
            date_1=str(dates[-2]), close_1=closes[-2], ret1=ret1,
            date_0=str(dates[-1]), close_0=closes[-1], ret2=ret2,
            is_two_down=is_two_down,
            rsi=rsi,
            closes=closes
        )
    except Exception as e:
        log(f"Two-down signal fel: {e}")
        return None

def get_trading_client():
    try:
        from alpaca.trading.client import TradingClient
    except:
        log("pip install alpaca-py")
        return None
    k,s = get_keys()
    if not k: return None
    return TradingClient(k,s,paper=True)

def load_trades():
    if not TRADES_FILE.exists():
        return []
    try:
        with open(TRADES_FILE) as f:
            return json.load(f)
    except:
        return []

def save_trades(trades):
    try:
        with open(TRADES_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        log(f"Kunde inte spara trades: {e}")

def compute_self_improving_stats():
    trades = load_trades()
    if len(trades) < 5:
        return dict(n=len(trades), wr=0.6, need_filter=False, reason="För få trades, kör utan extra filter")
    
    # Kolla senaste 10 trades
    recent = trades[-10:]
    # Vi har bara BUY loggat, vi behöver räkna ut vinst efter sell - förenklad WR baserad på om vi loggat exit
    closed = [t for t in trades if t.get('closed')]
    if len(closed) < 5:
        return dict(n=len(trades), wr=0.59, need_filter=False, reason="För få stängda, kör grundregel")
    
    # Om vi har exit_ret sparat
    rets = [t.get('net_ret', 0) for t in closed[-10:] if 'net_ret' in t]
    if not rets:
        return dict(n=len(trades), wr=0.59, need_filter=False, reason="Ingen net_ret ännu")
    
    wr = np.mean([r>0 for r in rets])
    pf = sum([r for r in rets if r>0]) / abs(sum([r for r in rets if r<0])) if any(r<0 for r in rets) else 10.0
    
    need_filter = wr < 0.5 or pf < 1.0
    reason = f"Senaste 10 WR {wr*100:.1f}% PF {pf:.2f} - {'SKÄRPER med RSI<55' if need_filter else 'KÖR grundregel'}"
    return dict(n=len(trades), wr=wr, pf=pf, need_filter=need_filter, reason=reason, recent_rets=rets)

def check_and_sell_if_due(symbol, live=False):
    """Säljer efter 3 trading-dagar (two_down3 regeln)"""
    tc = get_trading_client()
    if not tc:
        return False
    try:
        positions = tc.get_all_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if not pos:
            log(f"Ingen öppen position i {symbol}")
            return False
        
        trades = load_trades()
        open_trades = [t for t in trades if t['symbol']==symbol and t['side']=='BUY' and not t.get('closed')]
        if not open_trades:
            log(f"Position finns {pos.qty} st {symbol} men ingen logg - hoppar över auto-sell")
            return False
        
        for ot in open_trades:
            entry_date = datetime.fromisoformat(ot['entry_date'])
            exit_target = entry_date
            added = 0
            while added < 3:  # 3 dagar för V29
                exit_target += timedelta(days=1)
                if exit_target.weekday() < 5:
                    added += 1
            now = datetime.now()
            if now >= exit_target:
                log(f"SÄLJ-DAGS V29: {symbol} köpt {ot['entry_date']} ska säljas nu (3 trading dagar, two_down3)")
                if live:
                    from alpaca.trading.requests import MarketOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    order = MarketOrderRequest(symbol=symbol, qty=float(pos.qty), side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                    placed = tc.submit_order(order)
                    log(f"SÅLD V29: {symbol} {pos.qty} st Order {placed.id} Status {placed.status}")
                    ot['closed']=True
                    ot['exit_date']=now.isoformat()
                    ot['exit_order_id']=str(placed.id)
                    # Försök räkna ut ret om vi har entry price
                    try:
                        # hämta senaste pris för att uppskatta ret
                        from alpaca.data.historical import StockHistoricalDataClient
                        from alpaca.data.requests import StockLatestTradeRequest
                        k,s = get_keys()
                        dc = StockHistoricalDataClient(k,s)
                        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
                        trade = dc.get_stock_latest_trade(req)
                        exit_price = float(trade[symbol].price)
                        entry_price = ot.get('entry_price', exit_price)
                        net = exit_price/entry_price - 1 - 0.001
                        ot['net_ret']=net
                        log(f"Trade resultat ca {net*100:+.2f}%")
                    except:
                        pass
                    save_trades(trades)
                    return True
                else:
                    log(f"[DRY] Skulle sälja {symbol} {pos.qty} st")
                    return True
            else:
                log(f"HOLD V29: {symbol} köpt {ot['entry_date']} säljs {exit_target.date()} (idag {now.date()})")
    except Exception as e:
        log(f"Sell-check V29 fel: {e}")
    return False

def buy_if_signal(symbol, notional, live=False):
    sig = get_two_down_signal(symbol)
    if not sig:
        log("Ingen signal V29")
        return False
    
    stats = compute_self_improving_stats()
    log(f"Self-improving stats: {stats['reason']}")
    log(f"Signal check V29 {symbol}: {sig['date_2']} C:{sig['close_2']:.2f} -> {sig['date_1']} C:{sig['close_1']:.2f} ret {sig['ret1']*100:+.2f}% -> {sig['date_0']} C:{sig['close_0']:.2f} ret {sig['ret2']*100:+.2f}% | TwoDown={sig['is_two_down']} RSI={sig['rsi']:.1f}")
    
    if not sig['is_two_down']:
        log("Ingen köp-signal V29 idag (inte 2 dagar ner i rad)")
        return False
    
    # Självförbättrande filter
    if stats['need_filter']:
        if sig['rsi'] >= 55:
            log(f"FILTER AKTIV: RSI {sig['rsi']:.1f} >=55, hoppar över trots two-down (WR låg)")
            return False
        else:
            log(f"FILTER PASS: RSI {sig['rsi']:.1f} <55 trots låg WR, köper ändå")
    
    tc = get_trading_client()
    if tc:
        try:
            pos = tc.get_open_position(symbol)
            if pos:
                log(f"Har redan position i {symbol} {pos.qty} st - köper inte ny innan sälj")
                return False
        except:
            pass
    
    log(f"=> KÖP-SIGNAL V29: Two down days -> köp {symbol} hold 3d (bästa +238%)")
    if live:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            order = MarketOrderRequest(symbol=symbol, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            placed = tc.submit_order(order)
            log(f"KÖPT V29: {symbol} ${notional} Order {placed.id} Status {placed.status}")
            trades = load_trades()
            # Försök få entry price
            entry_price = sig['close_0']
            try:
                entry_price = float(placed.filled_avg_price) if placed.filled_avg_price else sig['close_0']
            except:
                pass
            trades.append(dict(symbol=symbol, side='BUY', entry_date=datetime.now().isoformat(), entry_price=entry_price, notional=notional, order_id=str(placed.id), closed=False, rsi_at_entry=sig['rsi'], signal=sig))
            save_trades(trades)
            return True
        except Exception as e:
            log(f"Köp V29 fel: {e}")
            return False
    else:
        log(f"[DRY] Skulle köpa V29 {symbol} för ${notional} (two_down3)")
        return True

def main():
    ap = argparse.ArgumentParser(description="V29 Self-Improving ASI - two down hold 3d +238%")
    ap.add_argument("--symbol", default="NVDA")
    ap.add_argument("--notional", type=float, default=10000, help="$ per trade")
    ap.add_argument("--dry-run", action="store_true", help="Bara kolla, lägg inga ordrar")
    ap.add_argument("--live", action="store_true", help="Lägg riktiga ordrar i paper")
    args = ap.parse_args()
    
    live = args.live and not args.dry_run
    mode = "LIVE" if live else "DRY-RUN"
    log(f"=== V29 Self-Improving ASI {mode} {args.symbol} ${args.notional} ===")
    log(f"Regel: Two down days (ret<0 två dagar i rad) hold 3d - bästa +238% i backtest")
    
    check_and_sell_if_due(args.symbol, live=live)
    buy_if_signal(args.symbol, args.notional, live=live)
    
    stats = compute_self_improving_stats()
    log(f"Klar. Trades totalt: {stats['n']} Logg: {LOG_FILE} Trades: {TRADES_FILE}")

if __name__ == "__main__":
    main()
