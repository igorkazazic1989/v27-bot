#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
V27-Full-Auto-IEX.py - Helt automatiserad v27 på Alpaca IEX

KÖPER: När igår var röd dag (Close<Open) - v27 red2 = 1.25 trades/vecka +99% backtest
SÄLJER: Automatiskt efter 2 trading-dagar (hold 2d regeln)

Kör den via cron varje dag kl 15:35 ET (21:35 svensk tid, 5 min efter open):
  crontab -e
  35 15 * * 1-5 /usr/bin/python3 /Users/igorkazazic/Desktop/V27-Full-Auto-IEX.py --symbol NVDA >> ~/Desktop/v27_auto.log 2>&1

Eller manuellt:
  python3 ~/Desktop/V27-Full-Auto-IEX.py --symbol NVDA --dry-run  # bara kollar
  python3 ~/Desktop/V27-Full-Auto-IEX.py --symbol NVDA --live     # köper/säljer på riktigt i paper

Den sparar alla trades i ~/Desktop/v27_auto_trades.json så den vet när den ska sälja.
"""

import os, json, argparse
from datetime import datetime, timedelta
from pathlib import Path

TRADES_FILE = Path.home() / "Desktop" / "v27_auto_trades.json"
LOG_FILE = Path.home() / "Desktop" / "v27_auto.log"

def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line+"\n")
    except: pass

def get_keys():
    k=os.getenv("APCA_API_KEY_ID"); s=os.getenv("APCA_API_SECRET_KEY")
    if not k or not s:
        log("Saknar APCA_API_KEY_ID / SECRET")
        return None,None
    return k,s

def get_iex_signal(symbol):
    """Kollar om igår var röd dag på IEX"""
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
    start = end - timedelta(days=10)
    
    req = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, end=end, adjustment="all", feed=DataFeed.IEX)
    try:
        bars = client.get_stock_bars(req)
        df = bars.df
        if len(df) < 2:
            log("För lite IEX data")
            return None
        df = df.reset_index() if hasattr(df,'reset_index') else df
        df = df.sort_values('timestamp')
        y = df.iloc[-2]; t = df.iloc[-1]
        is_red = float(y['close']) < float(y['open'])
        return dict(date_y=str(y['timestamp'].date()), open_y=float(y['open']), close_y=float(y['close']), is_red=is_red, date_t=str(t['timestamp'].date()))
    except Exception as e:
        log(f"IEX signal fel: {e}")
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
    with open(TRADES_FILE, "w") as f:
        json.dump(trades, f, indent=2)

def check_and_sell_if_due(symbol, live=False):
    """Kollar om vi har en position som ska säljas efter 2 dagar"""
    tc = get_trading_client()
    if not tc:
        return False
    try:
        positions = tc.get_all_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if not pos:
            log(f"Ingen öppen position i {symbol}")
            return False
        
        # Hämta vår logg för entry date
        trades = load_trades()
        # hitta senaste BUY som inte har SELL
        open_trades = [t for t in trades if t['symbol']==symbol and t['side']=='BUY' and not t.get('closed')]
        if not open_trades:
            # om ingen logg, kolla position qty och anta att vi ska sälja om 2 dagar passerat sedan senaste order
            # För enkelhet: sälj om vi haft position i >=2 trading days (vi approximerar med 2 kalenderdagar exkl helg)
            log(f"Position finns {pos.qty} st {symbol} men ingen logg - hoppar över auto-sell, sälj manuellt eller radera {TRADES_FILE}")
            return False
        
        for ot in open_trades:
            entry_date = datetime.fromisoformat(ot['entry_date'])
            # räkna 2 trading days = 2 vardagar
            exit_target = entry_date
            added = 0
            while added < 2:
                exit_target += timedelta(days=1)
                if exit_target.weekday() < 5:  # mån-fre
                    added += 1
            now = datetime.now()
            if now >= exit_target:
                log(f"SÄLJ-DAGS: {symbol} köpt {ot['entry_date']} ska säljas nu (2 trading dagar)")
                if live:
                    from alpaca.trading.requests import MarketOrderRequest
                    from alpaca.trading.enums import OrderSide, TimeInForce
                    order = MarketOrderRequest(symbol=symbol, qty=float(pos.qty), side=OrderSide.SELL, time_in_force=TimeInForce.DAY)
                    placed = tc.submit_order(order)
                    log(f"SÅLD: {symbol} {pos.qty} st Order {placed.id} Status {placed.status}")
                    ot['closed']=True
                    ot['exit_date']=now.isoformat()
                    ot['exit_order_id']=str(placed.id)
                    save_trades(trades)
                    return True
                else:
                    log(f"[DRY] Skulle sälja {symbol} {pos.qty} st")
                    return True
            else:
                log(f"HOLD: {symbol} köpt {ot['entry_date']} säljs {exit_target.date()} (idag {now.date()})")
    except Exception as e:
        log(f"Sell-check fel: {e}")
    return False

def buy_if_signal(symbol, notional, live=False):
    sig = get_iex_signal(symbol)
    if not sig:
        log("Ingen signal")
        return False
    log(f"Signal check {symbol}: Igår {sig['date_y']} O:{sig['open_y']:.2f} C:{sig['close_y']:.2f} {'RÖD' if sig['is_red'] else 'GRÖN'}")
    
    if not sig['is_red']:
        log("Ingen köp-signal idag (igår var grön)")
        return False
    
    # kolla om vi redan har position
    tc = get_trading_client()
    if tc:
        try:
            pos = tc.get_open_position(symbol)
            if pos:
                log(f"Har redan position i {symbol} {pos.qty} st - köper inte ny innan sälj")
                return False
        except:
            pass  # ingen position
    
    log(f"=> KÖP-SIGNAL: {symbol} igår röd -> köp idag")
    if live:
        try:
            from alpaca.trading.requests import MarketOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce
            order = MarketOrderRequest(symbol=symbol, notional=notional, side=OrderSide.BUY, time_in_force=TimeInForce.DAY)
            placed = tc.submit_order(order)
            log(f"KÖPT: {symbol} ${notional} Order {placed.id} Status {placed.status}")
            # spara
            trades = load_trades()
            trades.append(dict(symbol=symbol, side='BUY', entry_date=datetime.now().isoformat(), notional=notional, order_id=str(placed.id), closed=False))
            save_trades(trades)
            return True
        except Exception as e:
            log(f"Köp fel: {e}")
            return False
    else:
        log(f"[DRY] Skulle köpa {symbol} för ${notional}")
        return True

def main():
    ap = argparse.ArgumentParser(description="V27 Full Auto IEX - köp röd dag, sälj efter 2d")
    ap.add_argument("--symbol", default="NVDA")
    ap.add_argument("--notional", type=float, default=10000, help="$ per trade")
    ap.add_argument("--dry-run", action="store_true", help="Bara kolla, lägg inga ordrar")
    ap.add_argument("--live", action="store_true", help="Lägg riktiga ordrar i paper")
    args = ap.parse_args()
    
    live = args.live and not args.dry_run
    mode = "LIVE" if live else "DRY-RUN"
    log(f"=== V27 Full Auto {mode} {args.symbol} ${args.notional} ===")
    
    # 1. Sälj först om dags
    check_and_sell_if_due(args.symbol, live=live)
    
    # 2. Köp om signal
    buy_if_signal(args.symbol, args.notional, live=live)
    
    log(f"Klar. Logg: {LOG_FILE} Trades: {TRADES_FILE}")

if __name__ == "__main__":
    main()
