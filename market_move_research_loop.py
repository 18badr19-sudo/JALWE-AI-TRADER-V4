"""Independent managed worker. It has read endpoints and an advisory outbox only."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import time

from core.config import settings
from market_move_provider import MoveProvider
from market_move_research import (MoveStore, PROTOCOL, daily_features, choose_pairs, bucket,
                                 utc, observe_session, default_path, stable_key)

logger = logging.getLogger(__name__)


class MoveResearchWorker:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider

    def prepare(self, now, sessions, symbols, *, clock=None):
        closed = [s for s in sessions if utc(s['close'])+timedelta(minutes=16) <= now]
        if len(closed) < 3:
            raise ValueError('INSUFFICIENT_COMPLETED_SESSIONS')
        latest, prior, older = closed[-3:][::-1]
        # Request enough frozen prior-day information to study the latest session.
        recent = closed[-5:]
        cache_key = latest['date']
        cache = self.store.state('daily_cache')
        if cache is None or cache.get('date') != cache_key:
            daily = self.provider.daily(symbols,recent,now)
            cache = dict(date=cache_key, rows=daily, universe_count=len(symbols))
            self.store.state('daily_cache',cache)
        else:
            daily = cache['rows']
        features_before, features_latest, gains = [], [], []
        for symbol in symbols:
            bars = daily.get(symbol, [])
            try:
                before = daily_features(symbol,bars,prior,older)
                current = daily_features(symbol,bars,latest,prior)
            except (ValueError, KeyError, TypeError):
                # A corrupt symbol cannot suppress the remaining universe.
                continue
            if before is not None:
                features_before.append(before)
            if current is not None:
                features_latest.append(current)
                if (current['daily_return_pct'] >= 8 and before is not None
                        and 0.5 <= before['close'] <= 100 and before['dollar_volume'] >= 100000):
                    gains.append((current['daily_return_pct'],before))
        # Retrospective examples are explicitly excluded from forward learning.
        history_mark = 'hot:'+latest['date']
        if self.store.state(history_mark) is None:
            for _, feat in sorted(gains,key=lambda x:(-x[0],x[1]['symbol']))[:5]:
                if not 0.5 <= feat['close'] <= 100 or feat['dollar_volume'] < 100000:
                    continue
                pair = stable_key(feat['symbol']+'|retrospective',latest['date'])
                self.store.add(feat,latest,'HOT_CASE',pair,now,prospective=False)
                matches = sorted((f for f in features_before if f['symbol'] != feat['symbol']
                                  and bucket(f) == bucket(feat)),key=lambda f:stable_key(f['symbol'],latest['date']))
                if matches:
                    self.store.add(matches[0],latest,'CONTROL',pair,now,prospective=False)
            self.store.state(history_mark,{'created_at':now.isoformat(),'selection':'POST_SESSION_CASE_STUDY'})
        # Prepare the next session's cohorts before its open. No same-day retrofit.
        future = next((s for s in sessions if utc(s['open']) > now), None)
        preceding = sessions[sessions.index(future)-1] if future is not None and sessions.index(future) else None
        if (future is not None and preceding is not None and preceding['date'] == latest['date']
                and self.store.state('frozen:'+future['date']) is None):
            inserted = 0
            for case, control in choose_pairs(features_latest,future['date']):
                pair = stable_key(case['symbol']+'|prospective',future['date'])
                for features, role in ((case,'ACTIVITY'),(control,'CONTROL')):
                    features = dict(features)
                    try:
                        features['pre_session_news'] = self.provider.news_before(features['symbol'],now-timedelta(minutes=16))
                        features['news_evidence_available'] = features['pre_session_news']['status'] == 'AVAILABLE'
                    except Exception as exc:
                        features['pre_session_news'] = {'status':'UNAVAILABLE','error_type':type(exc).__name__}
                    frozen_at = clock() if clock else now
                    if frozen_at >= utc(future['open']):
                        break
                    inserted += self.store.add(features,future,role,pair,frozen_at,prospective=True)
            if inserted:
                self.store.state('frozen:'+future['date'],{'created_at':now.isoformat(),'prior_day':latest['date']})
        return dict(state='READY',universe_count=len(symbols),daily_symbols=len(daily),
                    completed_session=latest['date'],eligible_prior_features=len(features_latest),
                    historical_bars_delayed=True,selection_scope='ALPACA_ACTIVE_US_EQUITY_CATALOG')

    def cycle(self, now=None):
        supplied_now = now is not None
        now = now or datetime.now(timezone.utc)
        health = {'state':'STARTING'}
        try:
            # Process-unique worker and durable run markers protect restart deduplication.
            sessions = self.provider.calendar(now)
            symbols = self.store.state('universe_cache')
            if symbols is None or symbols.get('date') != now.date().isoformat():
                symbols = dict(date=now.date().isoformat(),symbols=self.provider.assets())
                self.store.state('universe_cache',symbols)
            health = self.prepare(now,sessions,symbols['symbols'],
                                  clock=None if supplied_now else lambda: datetime.now(timezone.utc))
            due = [r for r in self.store.episodes() if r['status'] == 'PENDING'
                   and utc(r['session']['close'])+timedelta(minutes=16) <= now]
            for row in due[:8]:
                try:
                    data = self.provider.intraday(row['symbol'],row['session'],now)
                    result = observe_session(data,row['session'])
                    if result['status'] == 'COMPLETE':
                        cutoff = utc(result['up_at']) if result.get('up_at') else utc(row['session']['open'])
                        try:
                            result['pre_move_news'] = self.provider.news_before(row['symbol'],cutoff)
                        except Exception as exc:
                            result['pre_move_news'] = {'status':'UNAVAILABLE','error_type':type(exc).__name__}
                except Exception as exc:
                    result = dict(status='DATA_UNAVAILABLE',outcome=None,error_type=type(exc).__name__)
                self.store.settle(row['episode_key'],result,now)
        except Exception as exc:
            # Data errors cannot interrupt JALWE or publish false success labels.
            health = dict(state='DATA_UNAVAILABLE',error_type=type(exc).__name__,
                          reason=str(exc) if str(exc).startswith(('HTTP_','WAITING_')) else 'RESEARCH_READ_FAILED')
        if health.get('state') == 'READY':
            health['completed_session_pending'] = sum(r['status'] == 'PENDING'
                and r['session_day'] <= health['completed_session'] for r in self.store.episodes())
        key, packet = self.store.publish(now if supplied_now else datetime.now(timezone.utc),health)
        logger.info('MARKET_MOVE_PACKET %s',json.dumps(dict(packet_id=key,health=health,
            counts=packet['counts'],prospective_episodes=packet['prospective_episodes'],
            hot_case_episodes=packet['hot_case_episodes'],comparison=packet['comparison'])))
        return packet


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(levelname)s | %(message)s')
    if not settings.PAPER_TRADING or settings.ALLOW_LIVE_TRADING:
        raise RuntimeError('Research worker requires the existing PAPER-only configuration')
    provider = MoveProvider(settings.ALPACA_API_KEY,settings.ALPACA_SECRET_KEY)
    worker = MoveResearchWorker(MoveStore(default_path()),provider)
    logger.info('MARKET_MOVE_WORKER_STARTED protocol=%s read_only_provider=true broker_orders=false',PROTOCOL)
    while True:
        worker.cycle()
        time.sleep(900)


if __name__ == '__main__':
    main()
