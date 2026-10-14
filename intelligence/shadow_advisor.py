"""Evidence-bound model reviews, isolated from all trading authority.

Reviews are asynchronous observations of an already recorded native decision.
They cannot hold, approve, submit, resize, or modify an order or configuration.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger(__name__)
PROTOCOL = 'jalwe-shadow-reasoning-v1'
GEMINI_FREE_MODEL = 'gemini-3.5-flash-lite'
FEATURES = ('price', 'return_1', 'return_5', 'rvol', 'dollar_volume',
    'volume_acceleration', 'liquidity_score', 'distance_from_vwap_pct', 'above_vwap',
    'atr_pct', 'rsi_14', 'momentum_score', 'distance_to_high_20_pct',
    'range_compression', 'news_score', 'options_flow_score')
AGREEMENTS = ['AGREE', 'DISAGREE', 'INSUFFICIENT_EVIDENCE']
SCENARIOS = ['BREAKOUT_CONTINUES', 'FALSE_BREAKOUT', 'MOVE_EXHAUSTED']
ASSESSMENTS = ['SUPPORTED', 'WEAK', 'UNKNOWN']
SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'agreement': {'type': 'string', 'enum': AGREEMENTS},
    'summary_ar': {'type': 'string'},
    'scenarios': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
        'properties': {'scenario': {'type': 'string', 'enum': SCENARIOS},
            'assessment': {'type': 'string', 'enum': ASSESSMENTS},
            'reason_ar': {'type': 'string'},
            'evidence_refs': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['scenario', 'assessment', 'reason_ar', 'evidence_refs']}},
    'missing_evidence_ar': {'type': 'array', 'items': {'type': 'string'}},
    'next_check_ar': {'type': 'string'}},
    'required': ['agreement', 'summary_ar', 'scenarios', 'missing_evidence_ar', 'next_check_ar']}
INSTRUCTIONS = '''Review a recorded JALWE PAPER decision using only the supplied facts.
You are an independent shadow analyst, with no tools or trading authority.
Explain in Arabic what supports or contradicts the native decision. Distinguish
observations from hypotheses. Assess all three scenarios. Cite only non-null
fact IDs as evidence_refs. Scores are uncalibrated, not win probabilities.
Do not invent prices, catalysts, news, missing data, returns, or statistical edges.
A news score does not verify an actual catalyst. This record follows the native
decision and cannot alter that decision. Suggest a future observation only;
never commands, code changes, orders, position sizing, stops or price targets.
Insufficient evidence means INSUFFICIENT_EVIDENCE, not a confident guess.
Be concise: summary and each scenario reason under 150 Arabic characters,
at most three missing facts, and one short next observation.'''


def bounded_int(value, default, ceiling):
    try:
        return max(1, min(int(value), ceiling))
    except (ValueError, TypeError):
        return default


@dataclass(frozen=True)
class AdvisorConfig:
    enabled: bool = False
    paper_only: bool = False
    api_key: str = field(default='', repr=False)
    model: str = 'gpt-6-astra'
    daily_limit: int = 10
    provider: str = 'openai'
    free_tier_confirmed: bool = False

    @classmethod
    def environment(cls):
        from core.config import settings
        provider = os.getenv('JALWE_SHADOW_AI_PROVIDER', 'openai').strip().lower()
        gemini = provider == 'gemini'
        return cls(os.getenv('JALWE_SHADOW_AI_ENABLED', '').lower() == 'true',
            bool(settings.PAPER_TRADING and not settings.ALLOW_LIVE_TRADING),
            os.getenv('GEMINI_API_KEY' if gemini else 'OPENAI_API_KEY', '').strip(),
            os.getenv('JALWE_SHADOW_AI_GEMINI_MODEL' if gemini else 'JALWE_SHADOW_AI_MODEL',
                      GEMINI_FREE_MODEL if gemini else 'gpt-6-astra').strip(),
            bounded_int(os.getenv('JALWE_SHADOW_AI_DAILY_LIMIT', '3' if gemini else '10'), 3 if gemini else 10, 30),
            provider, os.getenv('JALWE_GEMINI_FREE_TIER_CONFIRMED', '').lower() == 'true')

    def state(self):
        if not self.paper_only:
            return 'BLOCKED_NON_PAPER'
        if self.provider not in {'openai', 'gemini'}:
            return 'INVALID_PROVIDER'
        if not self.enabled:
            return 'DISABLED'
        if self.provider == 'gemini':
            if self.model != GEMINI_FREE_MODEL:
                return 'BLOCKED_GEMINI_MODEL'
            if not self.free_tier_confirmed:
                return 'WAITING_FREE_TIER_CONFIRMATION'
        return 'SHADOW_ONLY' if self.api_key else 'WAITING_API_KEY'


def frozen_evidence(payload, now):
    decision = payload.get('jalwe') or {}
    metadata = decision.get('metadata') or {}
    features = metadata.get('decision_features') or {}
    timeframe = str((metadata.get('quality_inputs') or {}).get('timeframe', ''))
    # Provider timestamps mark the START of a completed interval. A 5m bar
    # normally starts more than five minutes ago; use its native 15m ceiling.
    max_age = {'1m': 300, '5m': 900}.get(timeframe)
    state = str(decision.get('state', ''))
    if state not in {'WATCHING', 'READY_FOR_PAPER_EXECUTION'} or max_age is None:
        return None
    if features.get('data_quality_ok') is not True or features.get('data_is_stale') is not False:
        return None
    try:
        stamp = datetime.fromisoformat(str(features['timestamp']).replace('Z', '+00:00'))
        if stamp.tzinfo is None or not 0 <= now - stamp.timestamp() <= max_age:
            return None
    except (ValueError, KeyError, TypeError):
        return None
    symbol = str(payload.get('symbol', '')).upper()
    if not re.fullmatch(r'[A-Z0-9.^/-]{1,20}', symbol):
        return None
    facts = {'native_state': state, 'feature_timestamp': stamp.isoformat(), 'feature_timeframe': timeframe,
             'data_quality_ok': True, 'data_is_stale': False}
    for name in FEATURES:
        value = features.get(name)
        # Text, metadata, arbitrary errors/news and secrets never enter the model.
        facts[name] = value if (name == 'above_vwap' and type(value) is bool or type(value) in {int, float}
                               and abs(value) <= 1e20 and math.isfinite(value)) else None
    feed = str(metadata.get('decision_data_feed', '')).lower()
    facts['decision_feed'] = feed if feed in {'iex', 'sip', 'delayed_sip'} else None
    for name in ('entry_price', 'stop_loss', 'target_1', 'target_2', 'target_3'):
        value = decision.get(name)
        facts['native_' + name] = value if type(value) in {int, float} and abs(value) <= 1e20 and math.isfinite(value) else None
    return {'protocol': PROTOCOL, 'symbol': symbol, 'facts': facts,
            'position_in_flow': 'AFTER_NATIVE_DECISION', 'execution_authority': False}


def validate_review(review, evidence):
    if not isinstance(review, dict) or set(review) != set(SCHEMA['required']):
        raise ValueError('INVALID_REVIEW_FIELDS')
    if review['agreement'] not in AGREEMENTS:
        raise ValueError('INVALID_AGREEMENT')
    for field in ('summary_ar', 'next_check_ar'):
        if not isinstance(review[field], str) or not 1 <= len(review[field]) <= 700:
            raise ValueError('INVALID_REVIEW_TEXT')
    missing = review['missing_evidence_ar']
    if not isinstance(missing, list) or len(missing) > 8 or any(
            not isinstance(x, str) or not 1 <= len(x) <= 200 for x in missing):
        raise ValueError('INVALID_MISSING_EVIDENCE')
    scenarios = review['scenarios']
    if not isinstance(scenarios, list) or len(scenarios) != 3:
        raise ValueError('MISSING_SCENARIOS')
    seen = set()
    for item in scenarios:
        if not isinstance(item, dict) or set(item) != {'scenario', 'assessment', 'reason_ar', 'evidence_refs'}:
            raise ValueError('INVALID_SCENARIO_FIELDS')
        if item['scenario'] not in SCENARIOS or item['assessment'] not in ASSESSMENTS:
            raise ValueError('INVALID_SCENARIO')
        seen.add(item['scenario'])
        if not isinstance(item['reason_ar'], str) or not 1 <= len(item['reason_ar']) <= 500:
            raise ValueError('INVALID_SCENARIO_REASON')
        refs = item['evidence_refs']
        if not isinstance(refs, list) or len(refs) > 10 or any(
                not isinstance(ref, str) or evidence['facts'].get(ref) is None for ref in refs):
            raise ValueError('UNSUPPORTED_EVIDENCE_REFERENCE')
        if item['assessment'] == 'SUPPORTED' and not refs:
            raise ValueError('SUPPORTED_WITHOUT_EVIDENCE')
    if seen != set(SCENARIOS):
        raise ValueError('DUPLICATED_SCENARIOS')
    return review


class ModelClient:
    def __init__(self, config):
        self.config = config

    def review(self, evidence):
        if self.config.provider != 'openai':
            raise ValueError('OPENAI_PROVIDER_NOT_SELECTED')
        request = {'model': self.config.model, 'instructions': INSTRUCTIONS,
            'input': json.dumps(evidence, ensure_ascii=False, allow_nan=False), 'store': False,
            'reasoning': {'effort': 'low'}, 'max_output_tokens': 2000,
            'text': {'format': {'type': 'json_schema', 'name': 'jalwe_shadow_review',
                                'schema': SCHEMA, 'strict': True}}}
        req = urllib.request.Request('https://api.openai.com/v1/responses', method='POST',
            data=json.dumps(request).encode(), headers={'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + self.config.api_key})
        # No redirects may forward a credential to another host.
        from self_update.publisher import NoRedirect
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
            raw = response.read(65537)
        if len(raw) > 65536:
            raise ValueError('OVERSIZED_MODEL_RESPONSE')
        body = json.loads(raw)
        if body.get('status') != 'completed':
            raise ValueError('INCOMPLETE_MODEL_RESPONSE')
        texts = [c['text'] for item in body.get('output', []) if item.get('type') == 'message'
                 for c in item.get('content', []) if c.get('type') == 'output_text']
        if len(texts) != 1:
            raise ValueError('MODEL_REFUSAL_OR_MISSING_OUTPUT')
        return json.loads(texts[0]), body.get('usage', {})


class GeminiQuotaExhausted(RuntimeError):
    pass


class GeminiClient:
    """Native Interactions REST transport. No OpenAI or paid-model fallback."""
    def __init__(self, config):
        self.config = config

    def review(self, evidence):
        if self.config.provider != 'gemini' or self.config.state() != 'SHADOW_ONLY':
            raise ValueError('GEMINI_CONFIGURATION_NOT_APPROVED')
        request = {'model': self.config.model, 'system_instruction': INSTRUCTIONS,
            'input': json.dumps(evidence, ensure_ascii=False, allow_nan=False), 'store': False,
            'generation_config': {'max_output_tokens': 2000, 'thinking_level': 'low'},
            'response_format': {'type': 'text', 'mime_type': 'application/json', 'schema': SCHEMA}}
        req = urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/interactions',
            method='POST', data=json.dumps(request).encode(), headers={
                'Content-Type': 'application/json', 'x-goog-api-key': self.config.api_key})
        from self_update.publisher import NoRedirect
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=15) as response:
                raw = response.read(65537)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise GeminiQuotaExhausted('GEMINI_QUOTA_PAUSED') from None
            raise ValueError('GEMINI_HTTP_' + str(exc.code)) from None
        if len(raw) > 65536:
            raise ValueError('OVERSIZED_GEMINI_RESPONSE')
        body = json.loads(raw)
        if body.get('status') != 'completed':
            raise ValueError('INCOMPLETE_GEMINI_RESPONSE')
        texts = [part['text'] for step in body.get('steps', []) if step.get('type') == 'model_output'
                 for part in step.get('content', []) if part.get('type') == 'text']
        if len(texts) != 1:
            raise ValueError('GEMINI_REFUSAL_OR_MISSING_OUTPUT')
        usage = body.get('usage') or {}
        return json.loads(texts[0]), {
            'input_tokens': usage.get('total_input_tokens'),
            'output_tokens': usage.get('total_output_tokens'), 'total_tokens': usage.get('total_tokens')}


class ShadowAdvisor:
    def __init__(self, path, *, config=None, client=None, clock=time.time):
        self.path, self.config, self.clock = str(path), config or AdvisorConfig.environment(), clock
        self.client = client or (GeminiClient(self.config) if self.config.provider == 'gemini' else ModelClient(self.config))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute('''CREATE TABLE IF NOT EXISTS shadow_reviews (
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, session TEXT NOT NULL,
                created REAL NOT NULL, state TEXT NOT NULL, evidence TEXT NOT NULL,
                result TEXT, usage TEXT, error_kind TEXT, finished REAL)''')
            # Existing evidence stays attributed to its original OpenAI provider.
            columns = {r['name'] for r in conn.execute('PRAGMA table_info(shadow_reviews)')}
            if 'provider' not in columns:
                conn.execute("ALTER TABLE shadow_reviews ADD COLUMN provider TEXT NOT NULL DEFAULT 'openai'")
            if 'model' not in columns:
                conn.execute("ALTER TABLE shadow_reviews ADD COLUMN model TEXT NOT NULL DEFAULT 'legacy'")
            conn.execute('CREATE TABLE IF NOT EXISTS shadow_advisor_state (key TEXT PRIMARY KEY,value REAL NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS shadow_review_session ON shadow_reviews(session)')
            conn.execute("DELETE FROM shadow_reviews WHERE created < ? AND state != 'RUNNING'", (clock()-90*86400,))

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=.2)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def submit(self, payload):
        if self.mode() != 'SHADOW_ONLY':
            return self.mode()
        now = self.clock()
        evidence = frozen_evidence(payload, now)
        if evidence is None:
            return 'INELIGIBLE_EVIDENCE'
        content = json.dumps(evidence, sort_keys=True, ensure_ascii=False, allow_nan=False)
        key = hashlib.sha256((self.config.provider+'|'+self.config.model+'|'+content).encode()).hexdigest()
        session = datetime.fromtimestamp(now, ZoneInfo('America/New_York')).date().isoformat()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute('SELECT 1 FROM shadow_reviews WHERE id=?', (key,)).fetchone():
                return 'DUPLICATE'
            count = conn.execute('SELECT COUNT(*) FROM shadow_reviews WHERE session=?', (session,)).fetchone()[0]
            if count >= min(30, max(1, self.config.daily_limit)):
                return 'DAILY_LIMIT'
            conn.execute('INSERT INTO shadow_reviews (id,symbol,session,created,state,evidence,provider,model) VALUES (?,?,?,?,?,?,?,?)',
                (key, evidence['symbol'], session, now, 'PENDING', content, self.config.provider, self.config.model))
        return 'QUEUED'

    def process_one(self):
        if self.mode() != 'SHADOW_ONLY':
            return False
        now = self.clock()
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.execute("UPDATE shadow_reviews SET state='EXPIRED',finished=? WHERE state='PENDING' AND created<?", (now, now-120))
            row = conn.execute("SELECT * FROM shadow_reviews WHERE state='PENDING' AND provider=? AND model=? ORDER BY created,id LIMIT 1",
                (self.config.provider, self.config.model)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE shadow_reviews SET state='RUNNING' WHERE id=?", (row['id'],))
        evidence = json.loads(row['evidence'])
        try:
            result, usage = self.client.review(evidence)
            validate_review(result, evidence)
            # Keep only accounting fields; arbitrary provider content never enters logs.
            usage = {k: v for k, v in usage.items() if k in {'input_tokens', 'output_tokens', 'total_tokens'} and type(v) is int}
            state, error = 'ANALYZED', None
        except GeminiQuotaExhausted:
            # Persist across restarts; never retry quota failures or switch to paid.
            with self.connect() as conn:
                conn.execute('INSERT OR REPLACE INTO shadow_advisor_state VALUES (?,?)',
                    ('gemini_quota_pause_until', self.clock()+86400))
            result, usage, state, error = None, {}, 'QUOTA_EXHAUSTED', 'GeminiQuotaExhausted'
        except Exception as exc:
            result, usage, state, error = None, {}, 'FAILED', type(exc).__name__
        with self.connect() as conn:
            conn.execute('UPDATE shadow_reviews SET state=?,result=?,usage=?,error_kind=?,finished=? WHERE id=?',
                (state, json.dumps(result, ensure_ascii=False), json.dumps(usage), error, self.clock(), row['id']))
        LOGGER.info('SHADOW_AI_REVIEW %s', json.dumps({'id': row['id'], 'symbol': row['symbol'],
            'state': state, 'error_kind': error, 'provider': row['provider'], 'model': row['model'], 'execution_authority': False}))
        return True

    def start(self):
        # A killed in-flight call is not replayed, avoiding an unbounded API retry.
        with self.connect() as conn:
            conn.execute("UPDATE shadow_reviews SET state='INTERRUPTED',finished=? WHERE state='RUNNING'", (self.clock(),))
            conn.execute("UPDATE shadow_reviews SET state='CONFIG_CHANGED',finished=? WHERE state='PENDING' AND (provider!=? OR model!=?)",
                (self.clock(), self.config.provider, self.config.model))
        def run():
            while True:
                try:
                    if self.process_one():
                        continue
                except Exception as exc:
                    LOGGER.warning('SHADOW_AI_WORKER_UNAVAILABLE %s', type(exc).__name__)
                time.sleep(2)
        if self.config.state() == 'SHADOW_ONLY':
            threading.Thread(target=run, name='jalwe-shadow-advisor', daemon=True).start()

    def status(self, symbol=None):
        with self.connect() as conn:
            counts = {r['state']: r['n'] for r in conn.execute('SELECT state,COUNT(*) n FROM shadow_reviews GROUP BY state')}
            row = conn.execute('SELECT * FROM shadow_reviews WHERE symbol=? ORDER BY created DESC LIMIT 1',
                (str(symbol or '').upper(),)).fetchone() if symbol else None
        return {'protocol': PROTOCOL, 'mode': self.mode(), 'key_present': bool(self.config.api_key),
            'provider': self.config.provider, 'free_tier_confirmed': self.config.free_tier_confirmed,
            'quota_pause_until': self.quota_pause_until() if self.config.provider == 'gemini' else None,
            'model': self.config.model, 'daily_limit': min(30, max(1, self.config.daily_limit)),
            'counts': counts, 'latest': dict(row) if row else None, 'execution_authority': False}

    def quota_pause_until(self):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM shadow_advisor_state WHERE key='gemini_quota_pause_until'").fetchone()
        return row['value'] if row else 0

    def mode(self):
        state = self.config.state()
        return 'GEMINI_QUOTA_PAUSED' if (state == 'SHADOW_ONLY' and self.config.provider == 'gemini'
            and self.clock() < self.quota_pause_until()) else state


_advisor = None


def submit_shadow_review(payload):
    global _advisor
    try:
        if _advisor is None:
            from core.storage import data_directory
            _advisor = ShadowAdvisor(data_directory() / 'shadow_ai_reviews.db')
            _advisor.start()
            LOGGER.info('SHADOW_AI_STATUS %s', json.dumps(_advisor.status()))
        return _advisor.submit(payload)
    except Exception as exc:
        LOGGER.warning('SHADOW_AI_SUBMISSION_UNAVAILABLE %s', type(exc).__name__)
        return 'UNAVAILABLE'


def advisor_status_text(symbol=None):
    from core.storage import data_directory
    status = ShadowAdvisor(data_directory() / 'shadow_ai_reviews.db').status(symbol)
    lines = ['🧠 تحليل الذكاء الاصطناعي — مراقبة PAPER', 'الحالة: ' + status['mode'],
        'المزود: ' + status['provider'],
        'النموذج: ' + status['model'], 'حد التحليلات اليومي: ' + str(status['daily_limit']),
        'السجلات: ' + json.dumps(status['counts'], ensure_ascii=False)]
    if status['provider'] == 'gemini':
        lines.append('تأكيد مشروع Free Tier: ' + ('نعم' if status['free_tier_confirmed'] else 'ينتظر'))
        if status['mode'] == 'GEMINI_QUOTA_PAUSED':
            until = datetime.fromtimestamp(status['quota_pause_until'], ZoneInfo('Asia/Riyadh'))
            lines.append('إيقاف طلبات الحصة حتى: ' + until.isoformat())
    row = status['latest']
    if row:
        lines += [row['symbol'] + ': ' + row['state'], 'مزود هذا السجل: ' + row['provider'] + ' / ' + row['model'], 'وقت الأدلة: ' +
            json.loads(row['evidence'])['facts']['feature_timestamp']]
        if row['state'] == 'ANALYZED':
            result = json.loads(row['result'])
            lines += [result['agreement'], result['summary_ar']]
            lines += [x['scenario'] + ': ' + x['reason_ar'] for x in result['scenarios']]
            lines += ['ما ينقص: ' + '؛ '.join(result['missing_evidence_ar']), 'الفحص التالي: ' + result['next_check_ar']]
    elif symbol:
        lines.append('لا يوجد تحليل محفوظ لهذا السهم.')
    lines.append('تفسير بحثي بعد القرار الأصلي؛ لا ينفذ أوامر ولا يغير الدخول أو الوقف، ودقته على السوق لم تثبت بعد.')
    return '\n'.join(lines)[:3900]
