"""Recompute native gates with historical I/O; no recorded gate answers."""
from datetime import timedelta
import threading
from types import SimpleNamespace

import pandas as pd

from core.config import settings
from intelligence import decision_replay
from intelligence.ai_engine import AIEngine
from intelligence.decision_engine import DecisionEngine
from intelligence.feature_engine import FeatureEngine
from intelligence.learning_engine import LearningEngine
from intelligence.opportunity_engine import get_opportunity_engine
from intelligence.strategy_router import get_strategy_router
from intelligence.session_strategy_engine import get_session_strategy_engine
from intelligence.trigger_engine import get_trigger_engine
from intelligence.breakout_confirmation_engine import get_breakout_confirmation_engine
from market.market_context import MarketContextEngine
from market.session_features import SessionFeatureEngine
from trading.risk_engine import get_risk_engine


class UnsupportedBoundary(BaseException):
    """Do not let a native optional-service exception handler hide a lab defect."""


class FrozenLearning(LearningEngine):
    def __init__(self, weights, enabled):
        if set(weights) != set(self.FACTORS):
            raise ValueError('Every learning multiplier must be frozen explicitly')
        self.enabled = enabled
        self._weights = dict(weights)
        self._lock = threading.RLock()

    def _refresh_cache(self, force=False):
        pass  # Never read today's learned model into yesterday's decisions.


class HistoricalData:
    def __init__(self, bundle):
        self.bundle = bundle
        self.now = bundle.start
        self.quotes = {}

    def get_bars(self, symbol, timeframe, limit):
        rows = [row for row in self.bundle.bars.get((symbol, timeframe), [])
                if row['end'] <= self.now and row['available_at'] <= self.now]
        rows = rows[-int(limit):]
        frame = pd.DataFrame([{k: r[k] for k in ('open','high','low','close','volume')}
                              for r in rows], columns=['open','high','low','close','volume'],
                             index=pd.DatetimeIndex([r['at'] for r in rows], tz='UTC'))
        frame.attrs['data_feed'] = self.bundle.feed
        return frame

    def get_latest_quote(self, symbol):
        row = self.quotes.get(symbol)
        if not row or row['at'] > self.now:
            return None
        return dict(bid=row['bid'], ask=row['ask'], timestamp=row['at'], feed=self.bundle.feed)

    def get_last_price(self, symbol):
        row = self.quotes.get(symbol)
        if not row or not timedelta(0) <= self.now - row['at'] <= timedelta(seconds=30):
            raise ValueError('Missing contemporaneous trade observation')
        return row['price']


class ArchivedEnrichment:
    """Only archived, timestamped optional-source scores, never future scores."""
    def __init__(self, data, kind):
        self.data, self.kind = data, kind

    def read(self, symbol):
        candidates = [r for r in self.data.bundle.enrichments
                      if r['symbol'] == symbol and r['kind'] == self.kind
                      and r['at'] <= self.data.now <= r['valid_until']]
        if not candidates:
            raise UnsupportedBoundary('MISSING_POINT_IN_TIME_' + self.kind.upper())
        return max(candidates, key=lambda r:r['at'])

    def apply_to_features(self, features):
        row = self.read(features.symbol)
        setattr(features, 'news_score' if self.kind == 'news' else 'options_flow_score', row['score'])
        features.metadata[self.kind + '_available'] = row['available']
        return None


class NativeDecision:
    def __init__(self, data, wallet, weights):
        self.data, self.wallet = data, wallet
        self.calls = []
        self.last_session = None
        # Explicit dependencies avoid the SDK/database-owning live constructor.
        e = self.engine = object.__new__(DecisionEngine)
        e._breakout_latches = {}
        e.market_data = data
        e.feature_engine = FeatureEngine()
        e.news_engine = ArchivedEnrichment(data, 'news')
        e.options_engine = ArchivedEnrichment(data, 'options')
        e.ai_engine = object.__new__(AIEngine)
        e.ai_engine.minimum_watch_score = 60.
        e.ai_engine.minimum_buy_score = settings.MIN_SIGNAL_SCORE
        e.ai_engine.learning = FrozenLearning(weights, data.bundle.learning_enabled)
        e.opportunity_engine = get_opportunity_engine()
        e.strategy_router = get_strategy_router()
        e.session_feature_engine = SessionFeatureEngine()
        e.session_strategy_engine = get_session_strategy_engine()
        e.trigger_engine = get_trigger_engine()
        e.breakout_engine = get_breakout_confirmation_engine()
        context = object.__new__(MarketContextEngine)
        context.market_data = data
        e.market_context_engine = context
        risk = get_risk_engine()
        e.risk_engine = SimpleNamespace(evaluate=lambda *a, **k: risk.evaluate(
            *a, **k, account_provider=lambda:wallet, position_reader=wallet.position_rows))
        e.external_research_bridge = SimpleNamespace(get_latest_research=lambda symbol:None)

    def observed(self, label, callback, args, kwargs):
        self.calls.append(label)
        if label == 'clock':
            return self.data.now
        if label in {'read_packet','feature_snapshot_store'}:
            return None
        if label == 'strategy_selection':
            # Bundle explicitly declares score-based selection; activated learned
            # strategy preferences are unsupported, not silently substituted.
            return self.last_session, {'mode':'FROZEN_NATIVE_SCORE_BASELINE'}
        allowed = {
            'market_data.get_bars','market_data.get_last_price','market_data.get_latest_quote',
            'feature_engine.diagnose_input','feature_engine.build',
            'external_research_bridge.get_latest_research','news_engine.apply_to_features',
            'options_engine.apply_to_features','ai_engine.evaluate','opportunity_engine.evaluate',
            'market_context_engine.get_regime_result','strategy_router.route',
            'session_feature_engine.build','session_strategy_engine.evaluate',
            'trigger_engine.evaluate','breakout_engine.evaluate','risk_engine.evaluate','assess_entry'}
        if label not in allowed:
            raise UnsupportedBoundary('UNSUPPORTED_NATIVE_BOUNDARY:' + label)
        value = callback(*args, **kwargs)
        if label == 'session_strategy_engine.evaluate':
            self.last_session = value
        if label == 'feature_engine.diagnose_input' and len(args[0]):
            value['latest_bar_age_minutes'] = (self.data.now - args[0].index[-1]).total_seconds()/60
        return value

    def analyze(self, symbol):
        self.calls = []
        # Missing archived inputs are not counted as native strategy rejections.
        if len(self.data.get_bars(symbol,'5m',500)) < 51:
            raise UnsupportedBoundary('INSUFFICIENT_ARCHIVED_SYMBOL_WARMUP')
        if len(self.data.get_bars('SPY','1d',70)) < 52:
            raise UnsupportedBoundary('INSUFFICIENT_ARCHIVED_SPY_WARMUP')
        quote = self.data.get_latest_quote(symbol)
        if quote is None or not timedelta(0) <= self.data.now-quote['timestamp'] <= timedelta(seconds=30):
            raise UnsupportedBoundary('MISSING_CONTEMPORANEOUS_QUOTE_AND_TRADE')
        self.engine.news_engine.read(symbol)
        self.engine.options_engine.read(symbol)
        token = decision_replay._context.set(self)
        try:
            return self.engine._analyze_native(symbol,
                strategy_equity=self.wallet.strategy_equity,
                daily_start_equity=self.wallet.day_start,
                timeframe='5m', bar_limit=500)
        finally:
            decision_replay._context.reset(token)


def configuration():
    keys = ('MIN_SIGNAL_SCORE','MIN_OPPORTUNITY_SCORE','MIN_SESSION_STRATEGY_SCORE',
            'MIN_BREAKOUT_SCORE','MIN_BREAKOUT_VOLUME_RATIO','MAX_BREAKOUT_UPPER_WICK_RATIO',
            'WAITING_ZONE_PCT','MAX_CHASE_PCT','MIN_RVOL','MAX_SPREAD_PCT',
            'STRATEGY_STARTING_CAPITAL','RISK_PER_TRADE_PCT','MAX_RISK_PER_TRADE_PCT',
            'MAX_DAILY_LOSS_PCT','MAX_OPEN_POSITIONS','MAX_POSITION_ALLOCATION_PCT',
            'ALLOCATION_B_PCT','ALLOCATION_A_PCT','ALLOCATION_A_PLUS_PCT',
            'MIN_STOCK_PRICE','MAX_STOCK_PRICE','DEFAULT_STOP_ATR_MULTIPLIER',
            'MAX_ENTRY_SLIPPAGE_PCT')
    return {key:getattr(settings,key) for key in keys}
