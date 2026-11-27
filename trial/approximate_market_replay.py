"""Approximate OHLCV research baseline; NOT the native JALWE entry strategy."""
import json, math
from datetime import datetime, timezone
from pathlib import Path
import yfinance as yf

SYMBOLS = ['AAPL', 'MSFT', 'NVDA', 'AMD', 'TSLA', 'AMZN', 'META', 'GOOGL', 'PLTR', 'SOFI']
OUT = Path('market-simulation')
OUT.mkdir(exist_ok=True)
now = datetime.now(timezone.utc)
report = dict(kind='REAL_BARS_APPROXIMATE_REPLAY', observed_at=now.isoformat(),
    execution_authority=False, native_strategy_validated=False,
    untested=['native news/options gates','APEX universe','order book liquidity','queue priority','native T1/T2/T3/runner'],
    assumptions=dict(universe=SYMBOLS, entry='close above previous 20 highs; next contiguous bar open',
        stop_pct=2, target_pct=4, half_spread_bps=5, slippage_bps=5, fee_bps_per_side=10,
        same_bar='STOP_FIRST', position='one share per symbol; no leverage',
        horizon='downloaded 5-day replay; overlapping runs are NOT independent samples'),
    symbols={}, trades=[])
for symbol in SYMBOLS:
    try:
        frame = yf.Ticker(symbol).history(period='5d', interval='5m', auto_adjust=False,
            back_adjust=False, prepost=False, actions=False, raise_errors=True)
        rows = []
        for at, row in frame.iterrows():
            t = at.to_pydatetime().astimezone(timezone.utc)
            vals = [float(row[k]) for k in ['Open','High','Low','Close','Volume']]
            if t.timestamp()+300 > now.timestamp() or not all(math.isfinite(x) for x in vals):
                continue
            o,h,l,c,v = vals
            if min(o,h,l,c)<=0 or v<=0 or not l<=min(o,c)<=max(o,c)<=h:
                continue
            rows.append(dict(t=t.isoformat(), ts=t.timestamp(), o=o,h=h,l=l,c=c,v=v))
        if len(rows)<22:
            raise ValueError('INSUFFICIENT_VALID_BARS')
        (OUT / (symbol+'.json')).write_text(json.dumps(rows,indent=2))
        position = None
        for i in range(21,len(rows)):
            b, previous = rows[i],rows[i-1]
            contiguous = b['ts']-previous['ts']==300
            if position:
                if not contiguous:
                    position['gap_in_path']=True
                if b['l']<=position['stop'] or b['h']>=position['target']:
                    stop = b['l']<=position['stop']
                    price = min(b['o'],position['stop']) if stop else position['target']
                    price *= .999
                    report['trades'].append(dict(symbol=symbol,**position,exit_at=b['t'],
                        exit_price=price,reason='STOP' if stop else 'TARGET',
                        ambiguous=stop and b['h']>=position['target'],
                        net=price-position['entry']-.001*(price+position['entry'])))
                    position=None
                    continue
            if position is None and contiguous and previous['c']>max(r['h'] for r in rows[i-21:i-1]):
                entry=b['o']*1.001
                # Exit evaluation begins on next bar; entry bar range is deliberately unmodelled.
                position=dict(entry_at=b['t'],entry=entry,stop=entry*.98,target=entry*1.04,
                    gap_in_path=False,entry_bar_path_unmodelled=True)
        report['symbols'][symbol]=dict(bars=len(rows),latest=rows[-1]['t'],
            age_seconds=now.timestamp()-rows[-1]['ts']-300,
            open_virtual_position=position, status='OBSERVED')
    except Exception as exc:
        report['symbols'][symbol]=dict(status='UNAVAILABLE',error_type=type(exc).__name__)
report['covered_symbols']=sum(v['status']=='OBSERVED' for v in report['symbols'].values())
report['closed_virtual_trades']=len(report['trades'])
report['state']='DATA_COLLECTED_APPROXIMATE_REPLAY' if report['covered_symbols'] else 'DATA_UNAVAILABLE'
(OUT/'report.json').write_text(json.dumps(report,indent=2,allow_nan=False))
print(json.dumps({k:report[k] for k in ['state','covered_symbols','closed_virtual_trades','observed_at']}))
if not report['covered_symbols']:
    raise SystemExit(2)
