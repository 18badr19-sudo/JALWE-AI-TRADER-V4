"""Bounded PAPER decision transcripts and offline coordinator replay.

Replay executes the same DecisionEngine control flow against recorded stage
responses and mutations. It does NOT rerun provider services, component model
internals, an orchestrator, fills or exit management. Missing historical inputs
are never backfilled from today's data. No pickle, saved-code execution or eval.
"""
from contextvars import ContextVar
from contextlib import contextmanager,closing
from dataclasses import fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import inspect
import json
import logging
import math
import os
from pathlib import Path
import platform
import queue
import re
import sqlite3
import threading
import time
import types
import zlib
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

PROTOCOL='native-decision-coordinator-replay-v1'
MAX_BYTES, MAX_COMPRESSED, MAX_EVENTS = 2_000_000, 256_000, 100
MODULES=('core.models','intelligence.ai_engine','intelligence.opportunity_engine',
    'intelligence.strategy_router','market.market_regime',
    'intelligence.session_strategy_engine','intelligence.trigger_engine',
    'intelligence.breakout_confirmation_engine','intelligence.external_research_bridge',
    'market.market_context','market.session_features','intelligence.decision_engine')
COMPONENTS=('market_data','feature_engine','news_engine','options_engine','ai_engine',
    'opportunity_engine','market_context_engine','strategy_router','session_feature_engine',
    'session_strategy_engine','trigger_engine','breakout_engine','risk_engine','external_research_bridge')
SETTINGS=('PAPER_TRADING','ALLOW_LIVE_TRADING','MIN_RVOL','MAX_SPREAD_PCT','MIN_SIGNAL_SCORE')
_context=ContextVar('decision_replay_context',default=None)
LOGGER=logging.getLogger(__name__)


def canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class CaptureUnavailable(ValueError): pass
class ReplayAbort(BaseException): pass


@lru_cache(maxsize=1)
def source_version():
    root=Path(__file__).resolve().parents[1]
    paths={name.replace('.','/')+'.py' for name in MODULES}
    paths.update({'intelligence/decision_replay.py','core/config.py','trading/entry_quality.py'})
    manifest=dict(files={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in sorted(paths)},
        python=platform.python_version(),pandas=pd.__version__,numpy=np.__version__)
    return digest(manifest),manifest


def registry():
    import sys
    result={}
    for name in MODULES:
        module=sys.modules.get(name)
        if module is None: continue
        for cls in vars(module).values():
            if isinstance(cls,type) and cls.__module__ in MODULES and (is_dataclass(cls) or issubclass(cls,Enum)):
                result[cls.__module__+'.'+cls.__qualname__]=cls
    return result


class Codec:
    def __init__(self,blobs=None): self.blobs=blobs if blobs is not None else {}

    def encode(self,value,depth=0):
        if depth>35: raise CaptureUnavailable('DEPTH_LIMIT')
        enc=lambda v:self.encode(v,depth+1)
        if isinstance(value,Enum):
            key=type(value).__module__+'.'+type(value).__qualname__
            if key not in registry(): raise CaptureUnavailable('UNSUPPORTED_ENUM')
            return {'@':'enum','type':key,'value':enc(value.value)}
        if type(value) is str and re.search(r'(?i)(bearer\s|api[_-]?key[=:]|secret[=:]|authorization[=:]|sk-[a-z0-9]{15}|AIza[a-z0-9_-]{20})',value):
            raise CaptureUnavailable('SENSITIVE_VALUE')
        if value is None or type(value) in (str,bool,int): return value
        if type(value) is float:
            return value if math.isfinite(value) else {'@':'float','value':str(value)}
        if value is pd.NA: return {'@':'pd_na'}
        if isinstance(value,np.generic): return enc(value.item())
        if isinstance(value,datetime): return {'@':'datetime','value':value.isoformat()}
        if isinstance(value,timedelta): return {'@':'timedelta','value':value.total_seconds()}
        if isinstance(value,pd.DataFrame):
            if len(value)>10000 or len(value.columns)>80 or not isinstance(value.index,pd.DatetimeIndex):
                raise CaptureUnavailable('UNSUPPORTED_FRAME')
            frame={'@':'frame','index':[x.isoformat() for x in value.index],
                'tz':str(value.index.tz) if value.index.tz is not None else None,
                'index_name':enc(value.index.name),'columns':enc(list(value.columns)),
                'dtypes':[str(x) for x in value.dtypes],
                'data':enc(value.to_numpy(dtype=object).tolist()),'attrs':enc(value.attrs)}
            key=digest(frame); self.blobs[key]=frame
            return {'@':'ref','key':key}
        if is_dataclass(value) and not isinstance(value,type):
            key=type(value).__module__+'.'+type(value).__qualname__
            if key not in registry(): raise CaptureUnavailable('UNSUPPORTED_DATACLASS')
            return {'@':'dataclass','type':key,'fields':enc({f.name:getattr(value,f.name) for f in fields(value)})}
        if isinstance(value,dict):
            if len(value)>20000: raise CaptureUnavailable('CONTAINER_LIMIT')
            if any(isinstance(k,str) and re.search(r'(?i)(api[_-]?key|secret|authorization|password|credential|access_token)',k) for k in value):
                raise CaptureUnavailable('SENSITIVE_FIELD')
            return {'@':'dict','items':[[enc(k),enc(v)] for k,v in value.items()]}
        if isinstance(value,(list,tuple)):
            if len(value)>100000: raise CaptureUnavailable('CONTAINER_LIMIT')
            return {'@':'tuple' if isinstance(value,tuple) else 'list','items':[enc(v) for v in value]}
        raise CaptureUnavailable('UNSUPPORTED_VALUE')

    def decode(self,value,depth=0):
        if depth>40: raise ReplayAbort('DEPTH_LIMIT')
        if value is None or type(value) in (str,bool,int,float): return value
        if not isinstance(value,dict): raise ReplayAbort('INVALID_ENCODING')
        dec=lambda v:self.decode(v,depth+1)
        kind=value.get('@')
        if kind=='ref':
            body=self.blobs[value['key']]
            if digest(body)!=value['key']: raise ReplayAbort('CORRUPT_BLOB')
            return dec(body)
        if kind=='float':
            if value['value'] not in {'nan','inf','-inf'}: raise ReplayAbort('INVALID_FLOAT')
            return float(value['value'])
        if kind=='pd_na': return pd.NA
        if kind=='datetime': return datetime.fromisoformat(value['value'])
        if kind=='timedelta': return timedelta(seconds=value['value'])
        if kind=='dict': return {dec(k):dec(v) for k,v in value['items']}
        if kind in {'list','tuple'}:
            result=[dec(v) for v in value['items']]
            return tuple(result) if kind=='tuple' else result
        if kind in {'enum','dataclass'}:
            cls=registry().get(value['type'])
            if cls is None: raise ReplayAbort('UNAPPROVED_CLASS')
            return cls(dec(value['value'])) if kind=='enum' else cls(**dec(value['fields']))
        if kind=='frame':
            data=pd.DataFrame(dec(value['data']),index=pd.DatetimeIndex(value['index'],tz=value['tz']),columns=dec(value['columns']))
            for column,dtype in zip(data.columns,value['dtypes']):
                if dtype not in {'float64','float32','int64','int32','bool','object'}:
                    raise ReplayAbort('UNAPPROVED_DTYPE')
                data[column]=data[column].astype(dtype)
            data.index.name=dec(value['index_name']); data.attrs=dec(value['attrs'])
            return data
        raise ReplayAbort('INVALID_ENCODING_KIND')


def projection(decision):
    values={f.name:getattr(decision,f.name) for f in fields(decision) if f.name!='metadata'}
    values['control_evidence']={k:decision.metadata[k] for k in
        ('entry_quality','trigger_watch','strategy_selection','latched_breakout_completed') if k in decision.metadata}
    return values


class Transcript:
    def __init__(self,engine,symbol,arguments):
        self.codec=Codec(); self.events=[]; self.failure=None; self.depth=0
        self.encoding_seconds=0.; self.cursor=0; self.replaying=False
        self.header=dict(protocol=PROTOCOL,symbol=symbol,created=time.time(),
            code_key=source_version()[0],manifest=source_version()[1],
            arguments=self.codec.encode(arguments),
            settings={key:getattr(__import__('core.config',fromlist=['settings']).settings,key) for key in SETTINGS},
            attributes={key:{'minimum_score':getattr(getattr(engine,key),'minimum_score',None)}
                        for key in ('opportunity_engine','session_strategy_engine')},
            latch=self.codec.encode(getattr(engine,'_breakout_latches',{}).get(symbol)),
            scope='RECORDED_STAGE_RESPONSE_COORDINATOR_REPLAY_NOT_COMPONENT_RECOMPUTATION',
            execution_authority=False)
        self.codec.encode(self.header['attributes'])

    def observed(self,label,callback,args,kwargs):
        if self.depth: return callback(*args,**kwargs)
        if self.replaying:
            if self.cursor>=len(self.events): raise ReplayAbort('UNRECORDED_CALL')
            row=self.events[self.cursor]; self.cursor+=1
            if row['label']!=label or row['input_hash']!=digest(self.codec.encode([args,kwargs])):
                raise ReplayAbort('CALL_INPUT_MISMATCH')
            for kind,key,encoded in row['mutations']:
                original=args[key] if kind=='arg' else kwargs[key]
                replacement=self.codec.decode(encoded)
                if isinstance(original,dict) and isinstance(replacement,dict): original.clear(); original.update(replacement)
                elif isinstance(original,list) and isinstance(replacement,list): original[:]=replacement
                elif is_dataclass(original) and type(original) is type(replacement):
                    for f in fields(original): setattr(original,f.name,getattr(replacement,f.name))
                else: raise ReplayAbort('UNSUPPORTED_MUTATION')
            return self.codec.decode(row['output'])
        if self.failure: return callback(*args,**kwargs)
        before=time.perf_counter()
        try:
            encoded=self.codec.encode([args,kwargs]); input_hash=digest(encoded)
            pre_args=encoded['items'][0]['items']
            pre_kwargs=dict(encoded['items'][1]['items'])
            self.encoding_seconds+=time.perf_counter()-before
        except Exception as exc:
            self.failure=str(exc) if isinstance(exc,CaptureUnavailable) else type(exc).__name__
            return callback(*args,**kwargs)
        self.depth+=1
        try: output=callback(*args,**kwargs)
        except BaseException:
            self.failure='NATIVE_STAGE_EXCEPTION'; raise
        finally: self.depth-=1
        before=time.perf_counter()
        try:
            mutations=[]
            for kind,values,previous in (('arg',dict(enumerate(args)),dict(enumerate(pre_args))),('kwarg',kwargs,pre_kwargs)):
                for key,value in values.items():
                    after=self.codec.encode(value)
                    if after!=previous[key]: mutations.append([kind,key,after])
            self.events.append(dict(label=label,input_hash=input_hash,output=self.codec.encode(output),mutations=mutations))
            self.encoding_seconds+=time.perf_counter()-before
            if len(self.events)>MAX_EVENTS or self.encoding_seconds>.075:
                self.failure='CAPTURE_BUDGET'
        except Exception as exc:
            self.failure=str(exc) if isinstance(exc,CaptureUnavailable) else type(exc).__name__
        return output


def observe(label,callback,*args,**kwargs):
    context=_context.get()
    return context.observed(label,callback,args,kwargs) if context else callback(*args,**kwargs)


def capture(engine,callback,*args,sink=None,**kwargs):
    from core.config import settings
    if (not settings.PAPER_TRADING or settings.ALLOW_LIVE_TRADING
            or os.getenv('JALWE_DECISION_REPLAY_ENABLED','true').lower() not in {'true','1','yes','on'}):
        return callback(*args,**kwargs)
    symbol=str(args[0] if args else kwargs.get('symbol','')).strip().upper()
    try:
        arguments=dict(inspect.signature(callback).bind(*args,**kwargs).arguments)
        bound=inspect.signature(callback).bind(*args,**kwargs); bound.apply_defaults(); arguments=dict(bound.arguments)
        context=Transcript(engine,symbol,arguments)
    except Exception:
        return callback(*args,**kwargs)
    token=_context.set(context)
    try: decision=callback(*args,**kwargs)
    finally: _context.reset(token)
    try:
        if context.failure:
            LOGGER.info('DECISION_REPLAY_CAPTURE_SKIPPED %s',canonical(dict(symbol=symbol,reason=context.failure)))
            return decision
        capsule=dict(context.header,events=context.events,blobs=context.codec.blobs,
            expected=context.codec.encode(projection(decision)),
            final_latch=context.codec.encode(engine._breakout_latches.get(symbol)))
        capsule['id']=digest(capsule)
        raw=canonical(capsule).encode()
        if len(raw)>MAX_BYTES: raise CaptureUnavailable('CAPSULE_SIZE_LIMIT')
        (sink or enqueue)(capsule)
        decision.replay_id=capsule['id']
    except Exception as exc:
        LOGGER.info('DECISION_REPLAY_CAPTURE_SKIPPED %s',canonical(dict(symbol=symbol,reason=type(exc).__name__)))
    return decision


class DeniedComponent:
    def __init__(self,attributes=None): self.__dict__.update(attributes or {})
    def __getattr__(self,name):
        def denied(*args,**kwargs): raise ReplayAbort('UNRECORDED_COMPONENT_CALL')
        return denied


def replay(capsule):
    """Offline, read-only coordinator execution. No fallback to live dependencies."""
    try:
        if len(canonical(capsule).encode())>MAX_BYTES or len(capsule.get('events',[]))>MAX_EVENTS:
            return dict(state='INVALID_CAPSULE',execution_authority=False)
        if capsule.get('protocol')!=PROTOCOL or capsule.get('execution_authority') is not False:
            return dict(state='INVALID_CAPSULE')
        if digest({k:v for k,v in capsule.items() if k!='id'})!=capsule['id']:
            return dict(state='INVALID_CAPSULE')
        if capsule['code_key']!=source_version()[0] or capsule['manifest']!=source_version()[1]:
            return dict(state='VERSION_UNAVAILABLE',execution_authority=False)
        from intelligence.decision_engine import DecisionEngine
        # Clone function globals, never patch process-wide clocks/settings in a live worker.
        namespace={}
        for name,member in DecisionEngine.__dict__.items():
            static=isinstance(member,staticmethod); fn=member.__func__ if static else member
            if not isinstance(fn,types.FunctionType): continue
            globals_={**fn.__globals__,'settings':types.SimpleNamespace(**capsule['settings'])}
            clone=types.FunctionType(fn.__code__,globals_,fn.__name__,fn.__defaults__,fn.__closure__)
            clone.__kwdefaults__=fn.__kwdefaults__
            namespace[name]=staticmethod(clone) if static else clone
        cls=type('RecordedDecisionEngine',(DecisionEngine,),namespace)
        engine=object.__new__(cls)
        for name in COMPONENTS: setattr(engine,name,DeniedComponent(capsule['attributes'].get(name)))
        context=object.__new__(Transcript); context.codec=Codec(dict(capsule['blobs']))
        context.events=capsule['events']; context.cursor=0; context.depth=0; context.replaying=True
        engine._breakout_latches={capsule['symbol']:context.codec.decode(capsule['latch'])} if capsule['latch'] is not None else {}
        # An encoded None is the literal None, not an empty synthetic latch.
        if engine._breakout_latches.get(capsule['symbol']) is None: engine._breakout_latches={}
        arguments=context.codec.decode(capsule['arguments'])
        token=_context.set(context)
        try: decision=engine._analyze_native(**arguments)
        finally: _context.reset(token)
        actual=context.codec.encode(projection(decision))
        matched=(actual==capsule['expected'] and context.cursor==len(context.events)
                 and context.codec.encode(engine._breakout_latches.get(capsule['symbol']))==capsule['final_latch'])
        return dict(state='MATCH' if matched else 'MISMATCH',calls=context.cursor,
            recorded_calls=len(context.events),native_state=decision.state.value,
            reason=decision.reason,gates=decision.gates,
            scope=capsule['scope'],execution_authority=False)
    except ReplayAbort as exc:
        return dict(state='UNREPLAYABLE',reason=str(exc),execution_authority=False)
    except Exception as exc:
        return dict(state='UNREPLAYABLE',reason=type(exc).__name__,execution_authority=False)


def default_path():
    from core.storage import data_directory
    return data_directory()/'decision_replays.db'


class ReplayStore:
    def __init__(self,path=None):
        self.path=Path(path or default_path()); self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS replay_sources (id TEXT PRIMARY KEY,manifest TEXT NOT NULL,coordinator_source TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS decision_replays (id TEXT PRIMARY KEY,symbol TEXT NOT NULL,created REAL NOT NULL,session TEXT NOT NULL,code_key TEXT NOT NULL,state TEXT NOT NULL,capsule BLOB,result TEXT NOT NULL,ready INTEGER NOT NULL)')
            conn.execute('CREATE INDEX IF NOT EXISTS replay_symbol_time ON decision_replays(symbol,created)')

    @contextmanager
    def connect(self):
        conn=sqlite3.connect(self.path,timeout=.5)
        try:
            with conn: yield conn
        finally: conn.close()

    def save(self,capsule):
        packed=zlib.compress(canonical(capsule).encode())
        if len(packed)>MAX_COMPRESSED: return dict(state='COMPRESSED_SIZE_LIMIT')
        stamp=capsule['created']; session=datetime.fromtimestamp(stamp,ZoneInfo('America/New_York')).date().isoformat()
        codec=Codec(capsule['blobs']); expected=codec.decode(capsule['expected'])
        ready=expected['state'].value=='READY_FOR_PAPER_EXECUTION'
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            existing=conn.execute('SELECT result FROM decision_replays WHERE id=?',(capsule['id'],)).fetchone()
            if existing: return json.loads(existing[0])
            conn.execute('DELETE FROM decision_replays WHERE created<?',(time.time()-14*86400,))
            used=conn.execute('SELECT COUNT(*) FROM decision_replays WHERE session=? AND ready=?',(session,int(ready))).fetchone()[0]
            if used>=(20 if ready else 80): return dict(state='DAILY_CAPTURE_LIMIT')
            result=replay(capsule)
            conn.execute('INSERT OR IGNORE INTO replay_sources VALUES (?,?,?)',
                (capsule['code_key'],canonical(capsule['manifest']),(Path(__file__).parent/'decision_engine.py').read_text()))
            conn.execute('INSERT OR IGNORE INTO decision_replays VALUES (?,?,?,?,?,?,?,?,?)',
                (capsule['id'],capsule['symbol'],stamp,session,capsule['code_key'],result['state'],packed,canonical(result),int(ready)))
            conn.execute('DELETE FROM replay_sources WHERE id NOT IN (SELECT DISTINCT code_key FROM decision_replays)')
        return result


_queue=queue.Queue(maxsize=16)
_worker=None
_lock=threading.Lock()


def enqueue(capsule):
    global _worker
    with _lock:
        if _worker is None:
            def run():
                store=None
                while True:
                    item=_queue.get()
                    try:
                        if store is None: store=ReplayStore()
                        result=store.save(item)
                        LOGGER.info('DECISION_REPLAY_RESULT %s',canonical(dict(id=item['id'],symbol=item['symbol'],**result)))
                    except Exception as exc: LOGGER.warning('DECISION_REPLAY_UNAVAILABLE %s',type(exc).__name__)
                    finally: _queue.task_done()
            _worker=threading.Thread(target=run,name='decision-replay-audit',daemon=True); _worker.start()
    try: _queue.put_nowait(capsule)
    except queue.Full: raise CaptureUnavailable('AUDIT_QUEUE_FULL')


def status_text(symbol=None,path=None):
    path=Path(path or default_path())
    lines=['🔁 إعادة تشغيل مسار القرار — بحث PAPER',
        'استجابات المراحل محفوظة؛ يعاد تشغيل منسق القرار فقط، دون مزوّد بيانات أو أوامر تداول.']
    if not path.exists(): return '\n'.join(lines+['الحالة: ينتظر أول قرار جديد قابل للحفظ. السجلات القديمة بلا مدخلات لا يعاد تركيبها.'])
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as conn:
        counts=dict(conn.execute('SELECT state,COUNT(*) FROM decision_replays GROUP BY state'))
        row=conn.execute('SELECT symbol,created,state,result,id FROM decision_replays '+
            ('WHERE symbol=? ' if symbol else '')+'ORDER BY created DESC LIMIT 1',
            (str(symbol).upper(),) if symbol else ()).fetchone()
    lines.append('السجلات: '+json.dumps(counts,ensure_ascii=False))
    if row:
        result=json.loads(row[3]); lines.extend([row[0]+' | '+row[2],
            'وقت القرار: '+datetime.fromtimestamp(row[1],ZoneInfo('Asia/Riyadh')).isoformat(),
            'القرار: '+str(result.get('native_state','غير متاح')),
            'السبب: '+str(result.get('reason','غير متاح')), 'معرّف: '+row[4]])
    lines.append('تطابق الإعادة لا يثبت ربحية القرار؛ التنفيذ والخروج يحتاجان أدلة منفصلة.')
    return '\n'.join(lines)[:3800]
