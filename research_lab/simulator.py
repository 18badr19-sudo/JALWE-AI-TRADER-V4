"""Event-driven, regular-session native laboratory; hypothetical fills only.

No bar-high/low path is invented. Execution consumes later timestamped
trade/quote observations with displayed sizes. The fill, latency and fee
model is an explicit assumption, not a replica of exchange queue priority.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import platform
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from broker.entry_pricing import protected_entry_limit
from core.config import settings
from core.models import BrokerOrder, OrderStatus, TradeSide
from intelligence.learning_engine import LearningEngine
from market_move_trial import snapshot_match
from trading.recovery_engine import RecoveryEngine
from trading.trade_manager import TradeManager, TradeAction, EXIT_ACTIONS
from research_lab.native import HistoricalData, NativeDecision, UnsupportedBoundary, configuration

PROTOCOL = 'native-event-execution-lab-v1'
NY = ZoneInfo('America/New_York')


def utc(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Timezone required')
    return dt.astimezone(timezone.utc)


def number(value, *, minimum=0., maximum=1e15, integer=False):
    if type(value) not in (int,float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError('Invalid finite numeric evidence')
    if integer and not float(value).is_integer():
        raise ValueError('Whole-share size required')
    return int(value) if integer else float(value)


def symbol(value):
    if not isinstance(value,str) or not value or len(value)>20 or value!=value.upper():
        raise ValueError('Canonical symbol required')
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',',':'), allow_nan=False,
                      default=lambda x:x.isoformat() if isinstance(x,datetime) else str(x))


class Bundle:
    def __init__(self, raw):
        self.raw = deepcopy(raw)
        if raw.get('protocol') != PROTOCOL:
            raise ValueError('Unsupported evidence protocol')
        self.start, self.end = utc(raw['start']), utc(raw['end'])
        if not self.start < self.end or self.end-self.start > timedelta(days=366):
            raise ValueError('Invalid horizon')
        self.feed = raw['feed']
        if self.feed not in {'iex','sip'}:
            raise ValueError('A single explicit feed is required')
        if raw['strategy_selection'] != 'NATIVE_SCORE_BASELINE':
            raise ValueError('Learned strategy-selection policies need a separate adapter')
        if raw['configuration'] != configuration():
            raise ValueError('Configuration does not match this isolated code profile')
        if utc(raw['configuration_frozen_at']) > self.start or utc(raw['learning']['frozen_at']) > self.start:
            raise ValueError('Future configuration/learning leakage')
        self.weights = raw['learning']['weights']
        self.learning_enabled = raw['learning']['enabled']
        if type(self.learning_enabled) is not bool:
            raise ValueError('Frozen learning availability flag required')
        if set(self.weights) != set(LearningEngine.FACTORS):
            raise ValueError('Incomplete frozen learning profile')
        for value in self.weights.values():
            number(value, minimum=.75, maximum=1.25)
        self.capital = number(raw['capital'], minimum=.01)
        self.scan_seconds = number(raw['scan_seconds'], minimum=0, maximum=300)
        self.management_seconds = number(raw['management_seconds'], minimum=0, maximum=300)
        self.slippage = number(raw['costs']['slippage_bps'], maximum=500)/10000
        self.fee_bps = number(raw['costs']['fee_bps'], maximum=500)/10000
        self.per_share_fee = number(raw['costs']['per_share_fee'], maximum=10)
        self.provenance = raw['provenance']
        if self.provenance['kind'] not in {'SYNTHETIC','ARCHIVED_MARKET'}:
            raise ValueError('Explicit evidence provenance required')
        if self.provenance.get('unadjusted_prices') is not True:
            raise ValueError('Adjusted prices cannot be mixed with executable quotes')
        self.sessions = []
        for session in raw['sessions']:
            opened, closed = utc(session['open']), utc(session['close'])
            if (opened >= closed or opened.astimezone(NY).date()!=closed.astimezone(NY).date()
                    or session['date'] != opened.astimezone(NY).date().isoformat()):
                raise ValueError('Invalid explicit exchange session')
            if self.sessions and opened <= self.sessions[-1]['close']:
                raise ValueError('Overlapping or unsorted sessions')
            self.sessions.append(dict(date=session['date'],open=opened,close=closed))
        if not self.sessions:
            raise ValueError('Exchange calendar evidence required')
        self.bars = {}
        seen = set()
        for item in raw['bars']:
            row = dict(item)
            row['symbol'] = symbol(row['symbol'])
            for key in ('at','end','available_at'):
                row[key] = utc(row[key])
            if row['timeframe'] not in {'5m','1d'} or not row['at'] < row['end'] <= row['available_at']:
                raise ValueError('Incomplete/future bar availability')
            if row['timeframe']=='5m' and row['end']-row['at'] != timedelta(minutes=5):
                raise ValueError('Invalid 5-minute interval')
            key = (row['symbol'],row['timeframe'],row['at'])
            if key in seen:
                raise ValueError('Duplicate bar')
            seen.add(key)
            for col in ('open','high','low','close'):
                row[col] = number(row[col],minimum=1e-8)
            row['volume'] = number(row['volume'])
            if not row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']:
                raise ValueError('Invalid OHLC geometry')
            self.bars.setdefault(key[:2],[]).append(row)
        for rows in self.bars.values():
            rows.sort(key=lambda r:r['at'])
            if any(a['end']>b['at'] for a,b in zip(rows,rows[1:])):
                raise ValueError('Overlapping bars')
        self.observations = []
        seen = set()
        for item in raw['observations']:
            row = dict(item,at=utc(item['at']),symbol=symbol(item['symbol']))
            key = (row['symbol'],row['at'])
            if key in seen or not self.start <= row['at'] <= self.end:
                raise ValueError('Duplicate/out-of-range observation')
            seen.add(key)
            if self.session(row['at']) is None:
                raise ValueError('Extended-session execution is unsupported')
            for col in ('bid','ask','price'):
                row[col] = number(row[col],minimum=1e-8)
            if row['ask'] < row['bid']:
                raise ValueError('Crossed quote')
            for col in ('bid_size','ask_size'):
                row[col] = number(row[col],integer=True)
            self.observations.append(row)
        self.enrichments = []
        seen = set()
        for item in raw['enrichments']:
            row = dict(item, at=utc(item['at']),valid_until=utc(item['valid_until']))
            symbol(row['symbol'])
            if row['kind'] not in {'news','options'} or type(row['available']) is not bool:
                raise ValueError('Invalid enrichment source')
            if row['valid_until']<row['at'] or row['valid_until']-row['at']>timedelta(hours=1):
                raise ValueError('Invalid archived enrichment lifetime')
            key=(row['symbol'],row['kind'],row['at'])
            if key in seen:
                raise ValueError('Duplicate enrichment')
            seen.add(key)
            if row['score'] is not None:
                number(row['score'],maximum=100)
            if row['available'] and row['score'] is None:
                raise ValueError('Available enrichment requires its observed score')
            self.enrichments.append(row)
        self.batches = []
        seen=set()
        for item in raw['batches']:
            at=utc(item['at'])
            if item['id'] in seen or not self.start <= at <= self.end or not self.session(at):
                raise ValueError('Invalid batch identity/session')
            seen.add(item['id'])
            reports=[]
            for report in item['reports']:
                symbol(report['symbol'])
                published=utc(report['published_at'])
                if not timedelta(0) <= at-published <= timedelta(minutes=30):
                    raise ValueError('Future/stale APEX membership')
                reports.append(dict(report,published_at=published))
            self.batches.append(dict(item,at=at,reports=reports))

    def session(self, at):
        return next((s for s in self.sessions if s['open'] <= at < s['close']),None)

    def fee(self, quantity, price):
        return quantity * (price*self.fee_bps+self.per_share_fee)


class Wallet:
    def __init__(self, bundle):
        self.bundle=bundle
        self.cash=bundle.capital
        self.gross=0.
        self.fees=0.
        self.day_start=bundle.capital
        self.day=None
        self.manager=TradeManager()
        self.trades=[]
        self.entries={}
        self.exits={}
        self.quotes={}
        self.fills=[]
        self.actions=[]
        self.issues=[]
        self.last_poll={}
        self.highs={}
        self.counter=0

    @property
    def strategy_equity(self):
        return self.bundle.capital+self.gross-self.fees

    def active(self):
        return [row for row in self.trades if row['trade'].remaining_quantity>0]

    def reserved(self):
        return sum(p['decision'].quantity*p['limit'] + self.bundle.fee(p['decision'].quantity,p['limit'])
                   for p in self.entries.values())

    def position_rows(self):
        rows=[dict(remaining_quantity=r['trade'].remaining_quantity,entry_price=r['trade'].entry_price)
              for r in self.active()]
        rows.extend(dict(remaining_quantity=p['decision'].quantity,entry_price=p['limit'])
                    for p in self.entries.values())
        return rows

    def get_all_positions(self):
        return [SimpleNamespace(symbol=r['trade'].symbol) for r in self.active()] + [
            SimpleNamespace(symbol=s) for s in self.entries]

    def marked_equity(self, at=None):
        value=self.cash
        for row in self.active():
            t=row['trade']; q=self.quotes.get(t.symbol)
            if not q or (at is not None and not timedelta(0)<=at-q['at']<=timedelta(seconds=30)):
                return None
            value+=t.remaining_quantity*q['bid']
        return value

    def get_account_snapshot(self):
        marked=self.marked_equity()
        return dict(equity=marked if marked is not None else 0.,cash=max(0.,self.cash-self.reserved()))

    def advance(self, at):
        day=at.astimezone(NY).date().isoformat()
        if day != self.day:
            self.day,self.day_start=day,self.strategy_equity
        for sym,pending in list(self.entries.items()):
            if at-pending['at']>timedelta(seconds=30) or not self.bundle.session(at):
                self.actions.append(dict(at=at,symbol=sym,action='ENTRY_EXPIRED'))
                del self.entries[sym]

    def submit_entry(self, decision, at):
        if decision.symbol in self.entries or any(r['trade'].symbol==decision.symbol for r in self.active()):
            raise ValueError('Duplicate virtual position')
        limit=protected_entry_limit(decision.entry_price,settings.MAX_ENTRY_SLIPPAGE_PCT)
        reserve=decision.quantity*limit+self.bundle.fee(decision.quantity,limit)
        if reserve>self.cash-self.reserved()+1e-8:
            self.actions.append(dict(at=at,symbol=decision.symbol,action='ENTRY_FEE_CASH_BLOCK'))
            return
        self.entries[decision.symbol]=dict(decision=decision,at=at,limit=limit)

    def _entry(self, quote):
        sym,at=quote['symbol'],quote['at']
        pending=self.entries.get(sym)
        if pending is None or at<=pending['at']:
            return
        del self.entries[sym]  # Conservative one-observation fill; remainder canceled.
        d=pending['decision']; price=quote['ask']*(1+self.bundle.slippage)
        qty=min(d.quantity,quote['ask_size'])
        if not qty or price>pending['limit']+1e-10 or price<=d.stop_price:
            self.actions.append(dict(at=at,symbol=sym,action='ENTRY_NOT_FILLED'))
            return
        targets=RecoveryEngine._targets_from_actual_fill(price,d.stop_price)
        trade=self.manager.create_trade(sym,price,qty,d.stop_price,*targets)
        self.counter+=1
        row=dict(id=f'VIRTUAL-{self.counter}',trade=trade,entered_at=at,gross=0.,fees=0.,closed_at=None)
        self.trades.append(row)
        fee=self.bundle.fee(qty,price)
        self.cash-=qty*price+fee; self.fees+=fee; row['fees']+=fee
        self.fills.append(dict(trade_id=row['id'],at=at,symbol=sym,side='BUY',quantity=qty,price=price,fee=fee))
        self.last_poll[row['id']]=at
        self.highs[row['id']]=quote['price']

    def _cancel_exit(self, row, at):
        pending=self.exits.pop(row['id'])
        order=deepcopy(pending['order']); order.status=OrderStatus.CANCELED
        self.manager.apply_exit_reconciliation(row['trade'],order)
        self.actions.append(dict(at=at,symbol=row['trade'].symbol,action='VIRTUAL_CANCEL_TARGET_FOR_STOP'))

    def _submit_exit(self, row, decision, at):
        self.counter+=1
        order=BrokerOrder(symbol=row['trade'].symbol,side=TradeSide.SELL,
            quantity=decision.quantity,order_id=f'VIRTUAL-EXIT-{self.counter}',status=OrderStatus.ACCEPTED)
        self.manager.register_exit_order(row['trade'],decision,order)
        self.exits[row['id']]=dict(at=at,order=order,notional=0.,action=decision.action)

    def _fill_exit(self, row, quote):
        pending=self.exits.get(row['id'])
        if not pending or quote['at']<=pending['at']:
            return
        order=pending['order']
        qty=min(order.quantity-order.filled_quantity,quote['bid_size'])
        if not qty:
            return
        price=quote['bid']*(1-self.bundle.slippage)
        pending['notional']+=qty*price
        order.filled_quantity+=qty
        order.filled_price=pending['notional']/order.filled_quantity
        order.status=OrderStatus.FILLED if order.filled_quantity==order.quantity else OrderStatus.PARTIALLY_FILLED
        result=self.manager.apply_exit_reconciliation(row['trade'],order)
        pnl=result['realized_pnl_increment']; fee=self.bundle.fee(qty,price)
        self.cash+=qty*price-fee; self.gross+=pnl; self.fees+=fee
        row['gross']+=pnl; row['fees']+=fee
        self.fills.append(dict(trade_id=row['id'],at=quote['at'],symbol=quote['symbol'],side='SELL',
            quantity=qty,price=price,fee=fee,action=pending['action'].value))
        if order.status==OrderStatus.FILLED:
            del self.exits[row['id']]
        if row['trade'].remaining_quantity==0:
            row['closed_at']=quote['at']

    def observe(self, quote):
        sym,at=quote['symbol'],quote['at']
        self.advance(at)
        self.quotes[sym]=quote
        self._entry(quote)
        for row in self.active():
            trade=row['trade']
            if trade.symbol!=sym:
                continue
            self.highs[row['id']]=max(self.highs.get(row['id'],quote['price']),quote['price'])
            pending=self.exits.get(row['id'])
            # Fixed conservative ordering if a target is outstanding on a stop
            # observation: cancel its unfilled remainder, then stop the rest.
            if quote['price']<=trade.current_stop and pending and pending['action'] in {
                    TradeAction.TAKE_PROFIT_1,TradeAction.TAKE_PROFIT_2,TradeAction.TAKE_PROFIT_3}:
                self._cancel_exit(row,at)
            self._fill_exit(row,quote)
            if not trade.remaining_quantity:
                continue
            due=at-self.last_poll[row['id']]>=timedelta(seconds=self.bundle.management_seconds)
            if not trade.has_pending_exit and (due or quote['price']<=trade.current_stop):
                decision=self.manager.evaluate(trade,quote['price'],observed_high=self.highs[row['id']])
                self.last_poll[row['id']]=at; self.highs[row['id']]=quote['price']
                if decision.action!=TradeAction.HOLD:
                    self.actions.append(dict(at=at,symbol=sym,action=decision.action.value,stop=trade.current_stop))
                if decision.action in EXIT_ACTIONS:
                    self._submit_exit(row,decision,at)

    def result(self, at):
        marked=self.marked_equity(at)
        closed=[r for r in self.trades if r['closed_at'] is not None]
        trades=[dict(id=r['id'],symbol=r['trade'].symbol,entered_at=r['entered_at'],closed_at=r['closed_at'],
            entry=r['trade'].entry_price,initial_quantity=r['trade'].initial_quantity,
            remaining=r['trade'].remaining_quantity,stop=r['trade'].current_stop,
            targets=[r['trade'].target_1,r['trade'].target_2,r['trade'].target_3],
            gross_realized=r['gross'],fees=r['fees'],net_realized=r['gross']-r['fees']) for r in self.trades]
        return dict(starting_capital=self.bundle.capital,cash=self.cash,gross_realized=self.gross,
            fees=self.fees,net_realized=self.gross-self.fees,marked_equity=marked,
            marked_return_pct=(marked/self.bundle.capital-1)*100 if marked is not None else None,
            closed_trades=len(closed),open_trades=len(self.active()),
            winning_closed_trades=sum(r['gross']-r['fees']>0 for r in closed),
            pending_entries=len(self.entries),pending_exits=len(self.exits),
            trades=trades,fills=self.fills,actions=self.actions)


def run_arm(bundle, *, researcher, decision_factory=NativeDecision):
    wallet=Wallet(bundle); data=HistoricalData(bundle)
    native=decision_factory(data,wallet,bundle.weights)
    events=[(q['at'],0,i,'quote',q) for i,q in enumerate(bundle.observations)]
    schedule=[]; issues=[]; counter=len(events)
    for batch in bundle.batches:
        reports=list(batch['reports'])
        matches={}
        for report in reports:
            sym=report['symbol']
            matches[sym]=snapshot_match(batch.get('snapshots',{}).get(sym,{}),sym,batch['at'])
            if matches[sym] is None:
                issues.append(dict(batch=batch['id'],symbol=sym,reason='MISSING_ELIGIBLE_PRIOR_DAY_SNAPSHOT'))
        if researcher:
            reports.sort(key=lambda r:matches[r['symbol']] is not True)
        for rank,report in enumerate(reports):
            at=batch['at']+timedelta(seconds=rank*bundle.scan_seconds)
            schedule.append(dict(batch=batch['id'],symbol=report['symbol'],rank=rank+1,at=at))
            counter+=1
            events.append((at,1,counter,'decision',report))
    decisions=[]; curve=[]
    for at,_,__,kind,value in sorted(events):
        if at>bundle.end or not bundle.session(at):
            if kind=='decision':
                issues.append(dict(symbol=value['symbol'],reason='SCHEDULE_OUTSIDE_HORIZON_OR_SESSION'))
            continue
        data.now=at; wallet.advance(at)
        if kind=='quote':
            data.quotes[value['symbol']]=value
            wallet.observe(value)
        else:
            sym=value['symbol']
            # Staleness can differ after priority scheduling. Never reuse an old
            # report merely because it was fresh at the start of the batch.
            if at-value['published_at']>timedelta(minutes=30):
                decisions.append(dict(at=at,symbol=sym,state='STALE_APEX_REPORT'))
                continue
            try:
                decision=native.analyze(sym)
            except UnsupportedBoundary as exc:
                issues.append(dict(at=at,symbol=sym,reason=str(exc)))
                decisions.append(dict(at=at,symbol=sym,state='DATA_UNAVAILABLE'))
                continue
            decisions.append(dict(at=at,symbol=sym,state=decision.state.value,reason=decision.reason,
                gates=decision.gates,quantity=decision.quantity,calls=list(native.calls)))
            if decision.ready_for_execution:
                wallet.submit_entry(decision,at)
        curve.append(dict(at=at,equity=wallet.marked_equity(at)))
    wallet.advance(bundle.end)
    result=wallet.result(bundle.end)
    marks=[r['equity'] for r in curve if r['equity'] is not None]
    peak=bundle.capital; drawdown=0.
    for mark in marks:
        peak=max(peak,mark); drawdown=max(drawdown,100*(peak-mark)/peak)
    result.update(decisions=decisions,decision_counts=dict(Counter(r['state'] for r in decisions)),
        schedule=schedule,issues=issues,equity_curve=curve,
        observed_drawdown_pct=drawdown,unmarked_observations=sum(r['equity'] is None for r in curve),
        drawdown_scope='OBSERVED_QUOTES_ONLY_NOT_CONTINUOUS_MARKET_MAXIMUM')
    daily={}
    for point in curve:
        daily[point['at'].astimezone(NY).date().isoformat()] = point
    result['daily_last_observed_equity'] = daily
    return result


def source_manifest():
    root=Path(__file__).resolve().parents[1]
    paths=list((root/'research_lab').glob('*.py'))
    paths+=list((root/'intelligence').glob('*.py'))
    paths+=list((root/'trading').glob('*.py'))
    paths+=list((root/'market').glob('*.py'))
    paths+=[root/'core/config.py',root/'core/models.py',root/'broker/entry_pricing.py',root/'market_move_trial.py']
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)}


def compare(raw):
    bundle=Bundle(raw)
    baseline=run_arm(bundle,researcher=False)
    researcher=run_arm(bundle,researcher=True)
    incomplete=bool(baseline['issues'] or researcher['issues'] or
                    not bundle.batches or not baseline['decisions'] or not researcher['decisions'] or
                    baseline['marked_equity'] is None or researcher['marked_equity'] is None or
                    baseline['unmarked_observations'] or researcher['unmarked_observations'] or
                    bundle.provenance.get('coverage')!='COMPLETE' or
                    bundle.provenance.get('universe')!='ALL_APEX_REPORTS')
    synthetic=bundle.provenance['kind']=='SYNTHETIC'
    difference=(researcher['marked_equity']-baseline['marked_equity']
                if not incomplete else None)
    report = dict(protocol=PROTOCOL,
        state='SYNTHETIC_ENGINEERING_RUN' if synthetic else 'INCOMPLETE_EVIDENCE' if incomplete else 'HISTORICAL_SIMULATION_ONLY',
        execution_authority=False,policy_promotion_allowed=False,
        start=bundle.start.isoformat(),end=bundle.end.isoformat(),
        data_sha256=hashlib.sha256(canonical(raw).encode()).hexdigest(),
        source_manifest=source_manifest(),configuration=configuration(),
        runtime=dict(python=platform.python_version(),pandas=version('pandas'),numpy=version('numpy')),
        native_components=['DecisionEngine','FeatureEngine','AIEngine','OpportunityEngine','StrategyRouter',
            'SessionFeatureEngine','SessionStrategyEngine','TriggerEngine','BreakoutConfirmationEngine',
            'RiskEngine','RecoveryEngine._targets_from_actual_fill','TradeManager'],
        baseline=baseline,researcher=researcher,
        paired_marked_equity_difference=difference,
        evidence_complete=not incomplete,
        provenance=bundle.provenance,
        assumptions=dict(costs=raw['costs'],scan_seconds=bundle.scan_seconds,
            management_seconds=bundle.management_seconds,
            entry='NEXT_OBSERVATION_SIZE_CAPPED_LIMIT_ONE_ATTEMPT_REMAINDER_CANCELED',
            exit='NEXT_OBSERVATION_BID_MINUS_SLIPPAGE_SIZE_CAPPED_PARTIAL_FILLS',
            stops='OBSERVED_TRADE_PRICE_TRIGGER_NOT_EXCHANGE_ELECTION_REPLICA',
            cancel='ZERO_LATENCY_CANCEL_TARGET_REMAINDER_BEFORE_STOP',
            sessions='SUPPLIED_EXCHANGE_CALENDAR_REGULAR_ONLY',
            learning='FROZEN_WEIGHTS_NOT_RETRAINED',
            optional_sources='ARCHIVED_NEWS_OPTIONS_SCORES_NOT_RECOMPUTED_FROM_RAW_CONTENT',
            baseline_order='SUPPLIED_APEX_BATCH_ORDER_BEFORE_OWNER_TRIAL',
            challenger='FIXED_OWNER_THREE_FACTOR_HYPOTHESIS_NOT_AN_ACTIVATED_LEARNED_POLICY'),
        limitations=['Sparse observations cannot prove the continuous market path.',
            'No queue priority, halts, corporate actions, live order routing or crash recovery is simulated.',
            'End positions remain open and are marked separately; no forced closing profit.',
            'A single selected sample/day cannot establish predictive advantage.',
            'No automatic strategy preference, stop, entry gate or broker setting changes.'])
    # Optional predeclared, disjoint evaluation windows; no best-window selection
    # or parameter optimization. Warmup remains point-in-time within each window.
    report['holdout_windows'] = []
    previous = None
    for window in raw.get('holdout_windows',[]):
        start,end,training_end = map(utc,(window['start'],window['end'],window['training_end']))
        if not bundle.start <= start < end <= bundle.end or training_end >= start or (previous and start <= previous):
            raise ValueError('Holdouts must be chronological, disjoint, after training, and inside the horizon')
        if utc(raw['learning']['frozen_at']) > training_end or utc(raw['configuration_frozen_at']) > training_end:
            raise ValueError('Holdout policy was not frozen before its training cutoff')
        if utc(window['declared_at']) > start:
            raise ValueError('Holdout selected after its start')
        previous=end
        subset=deepcopy(raw)
        subset.update(start=start.isoformat(),end=end.isoformat(),holdout_windows=[])
        subset['observations']=[r for r in raw['observations'] if start <= utc(r['at']) <= end]
        subset['batches']=[r for r in raw['batches'] if start <= utc(r['at']) <= end]
        result=compare(subset)
        report['holdout_windows'].append(dict(start=start,end=end,training_end=training_end,
            mode='FIXED_FROZEN_POLICY_NO_RETRAINING_OR_TUNING_CAPITAL_RESET_PER_WINDOW',
            state=result['state'],evidence_complete=result['evidence_complete'],
            paired_marked_equity_difference=result['paired_marked_equity_difference'],
            baseline_closed=result['baseline']['closed_trades'],researcher_closed=result['researcher']['closed_trades']))
    return report
