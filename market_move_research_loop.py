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
from market_move_diagnostics import VERSION, observation_due, diagnose_gaps, emit_evidence, recheck_absent_bars
from market_jump_paths import VERSION as JUMP_VERSION, SCAN_VERSION, LOOKBACK_DAYS, candidates, jump_path
from market_jump_requests import queue_requested, requested_keys, requested_bounds, schedule_studies

logger = logging.getLogger(__name__)


def session_backlog(rows, day):
    current = [r for r in rows if r['session_day'] == day]
    forward = [r for r in current if r['prospective'] and r['role'] in {'ACTIVITY', 'CONTROL'}]
    return dict(completed_session_pending=sum(r['status'] == 'PENDING' for r in current),
        completed_session_forward_total=len(forward),
        completed_session_forward_pending=sum(r['status'] == 'PENDING' for r in forward),
        completed_session_historical_pending=sum(r['status'] == 'PENDING' and not r['prospective'] for r in current))


class MoveResearchWorker:
    def __init__(self, store, provider):
        self.store, self.provider = store, provider

    def prepare(self, now, sessions, symbols, *, clock=None):
        self.store.state('jump_session_calendar',dict(sessions=sessions))
        from market_move_patterns import definition, PROTOCOL as PATTERN_PROTOCOL, SPEC
        pattern_definition = definition(self.store,now)
        closed = [s for s in sessions if utc(s['close'])+timedelta(minutes=16) <= now]
        requested = queue_requested(self.store,sessions,now)
        logger.info('MARKET_JUMP_REQUEST_QUEUED %s',json.dumps(requested))
        if len(closed) < 3:
            raise ValueError('INSUFFICIENT_COMPLETED_SESSIONS')
        latest, prior, older = closed[-3:][::-1]
        # Request enough frozen prior-day information to study the latest session.
        recent = [s for s in closed if utc(s['close']) >= now-timedelta(days=LOOKBACK_DAYS+3)]
        if len(recent) < 5:
            recent = closed[-5:]
        cache_key = latest['date']
        cache = self.store.state('daily_cache')
        if (cache is None or cache.get('date') != cache_key or cache.get('jump_version') != JUMP_VERSION
                or cache.get('selection_version') != SCAN_VERSION):
            try:
                daily = self.provider.daily(symbols,recent,now)
            except Exception as exc:
                # Explicit intraday requests remain independent of a failed
                # full-universe daily read. No forward cohort is fabricated.
                return dict(state='DATA_UNAVAILABLE',error_type=type(exc).__name__,
                            reason='DAILY_READ_FAILED_REQUESTED_STUDIES_CONTINUE')
            cache = dict(date=cache_key, rows=daily, universe_count=len(symbols), jump_version=JUMP_VERSION,
                         selection_version=SCAN_VERSION)
            self.store.state('daily_cache',cache)
        else:
            daily = cache['rows']
        logger.info('MARKET_JUMP_HISTORY_LOADED %s',json.dumps(dict(
            latest_session=latest['date'],daily_symbols=len(daily),lookback_days=LOOKBACK_DAYS)))
        # This scheduling-only overlay repairs requests seeded without daily
        # metadata. It never changes the original episode features/results.
        request_priority = requested_bounds(daily,closed,now)
        self.store.state('jump_request_priority',dict(session=latest['date'],bounds=request_priority))
        logger.info('MARKET_JUMP_REQUEST_PRIORITY %s',json.dumps(dict(
            session=latest['date'],bounds=request_priority,scope='RETROSPECTIVE_READ_ORDER_ONLY')))
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
        if future is not None and preceding is not None and preceding['date']==latest['date']:
            self.store.save_pattern_features(features_latest,future,now,clock=clock)
        if (future is not None and preceding is not None and preceding['date'] == latest['date']
                and self.store.state('frozen:'+future['date']) is None):
            prepared_pairs = []
            for case, control in choose_pairs(features_latest,future['date']):
                prepared = []
                for features, role in ((case,'ACTIVITY'),(control,'CONTROL')):
                    features = dict(features)
                    features.update(pattern_protocol=PATTERN_PROTOCOL,pattern_spec_hash=SPEC,
                                    pattern_defined_at=pattern_definition['defined_at'])
                    try:
                        features['pre_session_news'] = self.provider.news_before(features['symbol'],now-timedelta(minutes=16))
                        features['news_evidence_available'] = features['pre_session_news']['status'] == 'AVAILABLE'
                    except Exception as exc:
                        features['pre_session_news'] = {'status':'UNAVAILABLE','error_type':type(exc).__name__}
                    prepared.append(features)
                prepared_pairs.append(tuple(prepared))
            frozen_at = clock() if clock else now
            if frozen_at < utc(future['open']):
                self.store.freeze_cohort(prepared_pairs,future,frozen_at,clock=clock)
        # Queue every screened historical symbol/day, independent of ownership.
        # Separate role and keys preserve all existing frozen forward cohorts.
        jump_mark = 'jump_scan:'+SCAN_VERSION+':'+latest['date']
        jump_candidates = list(candidates(symbols,daily,closed,now))
        discovery_bounds = {f['symbol']+'|'+s['date']:f['daily_up_bound_pct'] for f,s in jump_candidates}
        self.store.state('jump_discovery_priority',dict(session=latest['date'],bounds=discovery_bounds))
        logger.info('MARKET_JUMP_DISCOVERY_PRIORITY %s',json.dumps(dict(session=latest['date'],
            candidates=len(discovery_bounds),large_upward_bounds=sum(v>=25 for v in discovery_bounds.values()),
            scope='DAILY_UPWARD_BOUND_READ_ORDER_NOT_CONFIRMED_JUMP',limit=12)))
        manifest = self.store.enqueue_jumps(jump_candidates,
                                           jump_mark,now,LOOKBACK_DAYS)
        logger.info('MARKET_JUMP_HISTORY_QUEUED %s',json.dumps(manifest))
        return dict(state='READY',universe_count=len(symbols),daily_symbols=len(daily),
                    completed_session=latest['date'],eligible_prior_features=len(features_latest),
                    historical_bars_delayed=True,selection_scope='ALPACA_ACTIVE_US_EQUITY_CATALOG',
                    jump_lookback_days=LOOKBACK_DAYS,jump_selection='ALL_DAILY_RANGE_OR_GAP_CANDIDATES')

    def cycle(self, now=None):
        supplied_now = now is not None
        now = now or datetime.now(timezone.utc)
        health = {'state':'STARTING'}
        try:
            # Process-unique worker and durable run markers protect restart deduplication.
            sessions = self.provider.calendar(now)
            symbols = self.store.state('universe_cache')
            if (symbols is None or symbols.get('date') != now.date().isoformat()
                    or symbols.get('selection_version') != SCAN_VERSION):
                symbols = dict(date=now.date().isoformat(),symbols=self.provider.assets(),selection_version=SCAN_VERSION)
                self.store.state('universe_cache',symbols)
            health = self.prepare(now,sessions,symbols['symbols'],
                                  clock=None if supplied_now else lambda: datetime.now(timezone.utc))
            due = [r for r in self.store.episodes() if observation_due(r, now)]
            # First observations precede retries; bounded work cannot starve a new cohort.
            # Historical backlog cannot starve newly matured prospective cohorts.
            priority = self.store.state('jump_request_priority') or {}
            discovery = self.store.state('jump_discovery_priority') or {}
            selected = schedule_studies(due,requested_keys(sessions,now),bounds=priority.get('bounds',{}),
                discovery_bounds=discovery.get('bounds',{}))
            logger.info('MARKET_JUMP_STUDY_BATCH %s',json.dumps(dict(
                selected=[dict(symbol=r['symbol'],session_day=r['session_day'],role=r['role'],
                               prospective=bool(r['prospective'])) for r in selected],limit=12)))
            frames, samples = {}, {}
            for row in selected:
                try:
                    frame_key = (row['symbol'],row['session']['open'],row['session']['close'])
                    if frame_key not in frames:
                        raw = self.provider.intraday(row['symbol'],row['session'],now)
                        initial = observe_session(raw,row['session'])
                        if row['status'] != 'PENDING' and initial['status'] == 'PARTIAL_DATA':
                            raw = recheck_absent_bars(self.provider,row['symbol'],raw,row['session'],initial,now)
                        frames[frame_key] = raw
                    data = frames[frame_key]
                    result = observe_session(data,row['session'])
                    if row['role'] == 'JUMP_CASE':
                        result['jump_path'] = jump_path(data,row['session'],
                                                       full_session_complete=result['status']=='COMPLETE')
                    if result['status'] == 'PARTIAL_DATA':
                        cached = {s['minute']:s for s in row['result'].get('data_diagnostics', {}).get('gap_trade_evidence', [])
                                  if s.get('complete') and s.get('minute')}
                        cached.update({minute:value for (symbol,minute),value in samples.items() if symbol==row['symbol']})
                        probed = diagnose_gaps(self.provider,row['symbol'],result,now,cached=cached)
                        samples.update({(row['symbol'],s['minute']):s for s in probed if s.get('complete') and s.get('minute')})
                    if result['status'] == 'COMPLETE':
                        path = result.get('jump_path',{})
                        cutoff = utc(path['start_at']) if path.get('start_at') else (
                            utc(result['up_at']) if result.get('up_at') else utc(row['session']['open']))
                        try:
                            result['pre_move_news'] = self.provider.news_before(row['symbol'],cutoff)
                        except Exception as exc:
                            result['pre_move_news'] = {'status':'UNAVAILABLE','error_type':type(exc).__name__}
                    if row['role']=='JUMP_CASE' and result.get('jump_path',{}).get('profile',{}).get('status')=='AVAILABLE':
                        from market_jump_common import enrich_profile
                        cache = self.store.state('daily_cache') or {}
                        evidence_row = dict(row,result=result)
                        result['jump_path']['profile'] = enrich_profile(result['jump_path']['profile'],
                            evidence_row,cache.get('rows',{}),sessions)
                except Exception as exc:
                    from market.research_diagnostics import error_diagnostics
                    result = dict(status='DATA_UNAVAILABLE',outcome=None,error_type=type(exc).__name__,
                                  diagnostic_version=VERSION,data_diagnostics=error_diagnostics(exc))
                self.store.settle(row['episode_key'],result,now,retry=row['status'] != 'PENDING')
                if row['role']=='JUMP_CASE':
                    path = result.get('jump_path',{})
                    logger.info('MARKET_JUMP_STUDY_RESULT %s',json.dumps(dict(
                        episode_key=row['episode_key'],symbol=row['symbol'],session_day=row['session_day'],
                        requested=(row['symbol'],row['session_day']) in requested_keys(sessions,now),
                        status=result['status'],missing_minutes=result.get('missing_minutes'),
                        error_type=result.get('error_type'),jump_path={k:v for k,v in path.items() if k in {
                            'status','strong_jump','start_at','start_price','peak_at','peak_price',
                            'observed_rise_pct','duration_minutes','phase_volume'}})))
        except Exception as exc:
            # Data errors cannot interrupt JALWE or publish false success labels.
            health = dict(state='DATA_UNAVAILABLE',error_type=type(exc).__name__,
                          reason=str(exc) if str(exc).startswith(('HTTP_','WAITING_')) else 'RESEARCH_READ_FAILED')
        if health.get('state') == 'READY':
            health.update(session_backlog(self.store.episodes(), health['completed_session']))
        published_at = now if supplied_now else datetime.now(timezone.utc)
        try:
            from market_jump_profiles import upgrade_saved_profiles
            logger.info('MARKET_JUMP_PROFILE_UPGRADE %s',json.dumps(upgrade_saved_profiles(self.store,published_at)))
        except Exception as exc:
            logger.warning('MARKET_JUMP_PROFILE_UNAVAILABLE %s',type(exc).__name__)
        try:
            from market_move_patterns import update
            patterns = update(self.store,self.store.episodes(),published_at)
        except Exception as exc:
            patterns = {'state':'UNAVAILABLE','error_type':type(exc).__name__}
            logger.warning('MARKET_MOVE_PATTERNS_UNAVAILABLE %s',type(exc).__name__)
        key, packet = self.store.publish(published_at,health,patterns=patterns)
        try:
            emit_evidence(self.store,self.store.episodes(),logger)
        except Exception as exc:
            logger.warning('MARKET_MOVE_DIAGNOSTIC_LOG_UNAVAILABLE %s',type(exc).__name__)
        logger.info('MARKET_MOVE_PACKET %s',json.dumps(dict(packet_id=key,health=health,
            counts=packet['counts'],prospective_episodes=packet['prospective_episodes'],
            hot_case_episodes=packet['hot_case_episodes'],comparison=packet['comparison'],
            historical_jumps=packet['historical_jumps'],
            requested_jumps=packet['requested_jumps'],
            historical_jump_profiles=packet['historical_jump_profiles'],
            strongest_completed_jumps=[dict(symbol=c['symbol'],session_day=c['session_day'],
                observed_rise_pct=c['path']['observed_rise_pct']) for c in packet['strongest_completed_jumps']],
            diagnostics=packet['diagnostics'],patterns=packet['patterns'])))
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
