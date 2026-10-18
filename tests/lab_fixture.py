"""Synthetic engineering data only. Never represent these as market results."""
from datetime import timedelta
import math

import pandas as pd

from intelligence.learning_engine import LearningEngine
from research_lab.native import configuration
from research_lab.simulator import PROTOCOL, utc


def fixture():
    now=utc('2026-10-16T15:00:00Z')
    bars=[]
    for i in range(70):
        at=now-timedelta(minutes=5*(70-i))
        close=10+math.sin(i)*.008
        opened=close-.005
        high=close+.01
        low=opened-.01
        volume=100_000
        if i==69:
            opened,close,high,low,volume=10.04,10.12,10.121,10.035,1_000_000
        bars.append(dict(symbol='TEST',timeframe='5m',at=at.isoformat(),
            end=(at+timedelta(minutes=5)).isoformat(),available_at=(at+timedelta(minutes=5)).isoformat(),
            open=opened,high=high,low=low,close=close,volume=volume))
    for i,day in enumerate(pd.bdate_range(end='2026-10-15',periods=70,tz='UTC')):
        price=400+i
        bars.append(dict(symbol='SPY',timeframe='1d',at=(day+timedelta(hours=4)).isoformat(),
            end=(day+timedelta(hours=20)).isoformat(),available_at=(day+timedelta(hours=20)).isoformat(),
            open=price-.2,high=price+.5,low=price-1,close=price,volume=100_000_000))
    session=dict(date='2026-10-16',open='2026-10-16T13:30:00Z',close='2026-10-16T20:00:00Z')
    prior=dict(session=session,prepared_at='2026-10-16T12:00:00Z',features=dict(symbol='TEST',
        evidence_day='2026-10-15',available_after='2026-10-15T20:16:00Z',source_feed='sip',
        range_pct=10.,volume_ratio_previous_day=3.,daily_return_pct=4.))
    observations=[]
    for seconds,price in [(0,10.12),(1,10.12),(2,10.5),(3,10.5),(4,10.7),
                           (5,10.7),(6,10.9),(7,10.9),(8,10.1),(9,10.09),(10,10.08)]:
        observations.append(dict(symbol='TEST',at=(now+timedelta(seconds=seconds)).isoformat(),
            bid=price-.001,ask=price,price=price,bid_size=100,ask_size=100))
    frozen='2026-10-15T21:00:00Z'
    return dict(protocol=PROTOCOL,start=now.isoformat(),end=(now+timedelta(seconds=10)).isoformat(),
        feed='sip',strategy_selection='NATIVE_SCORE_BASELINE',configuration=configuration(),
        configuration_frozen_at=frozen,learning=dict(frozen_at=frozen,enabled=False,
            weights={key:1. for key in LearningEngine.FACTORS}),capital=100.,scan_seconds=1.,
        management_seconds=0.,costs=dict(slippage_bps=0.,fee_bps=0.,per_share_fee=0.),
        provenance=dict(kind='SYNTHETIC',source='UNIT_TEST_GENERATOR',unadjusted_prices=True,
                        universe='ALL_APEX_REPORTS',coverage='COMPLETE'),
        sessions=[session],bars=bars,observations=observations,
        batches=[dict(id='SYNTHETIC-BATCH',at=now.isoformat(),
                      reports=[dict(symbol='TEST',published_at=now.isoformat(),report_id='TEST-REPORT')],
                      snapshots={'TEST':prior})],
        enrichments=[dict(symbol='TEST',kind=k,at=now.isoformat(),
            valid_until=(now+timedelta(minutes=30)).isoformat(),available=True,score=90.) for k in ('news','options')])


def ready_fixture():
    """Two native decisions: latch crossed trigger, then confirm a closed bar."""
    from copy import deepcopy
    raw=fixture(); now=utc(raw['start'])
    raw['observations']=[]
    for seconds,price in [(0,10.14),(300,10.22),(301,10.22),(302,10.8),(303,10.8),
                           (304,11.1),(305,11.1),(306,11.4),(307,11.4),(308,9.8),(309,9.7)]:
        raw['observations'].append(dict(symbol='TEST',at=(now+timedelta(seconds=seconds)).isoformat(),
            bid=price-.001,ask=price,price=price,bid_size=100,ask_size=100))
    raw['end']=(now+timedelta(seconds=309)).isoformat()
    raw['bars'].append(dict(symbol='TEST',timeframe='5m',at=now.isoformat(),
        end=(now+timedelta(minutes=5)).isoformat(),available_at=(now+timedelta(minutes=5)).isoformat(),
        open=10.04,high=10.201,low=10.03,close=10.2,volume=2_000_000))
    second=deepcopy(raw['batches'][0]); second['id']='SECOND'
    second['at']=(now+timedelta(minutes=5)).isoformat()
    second['reports'][0]['published_at']=second['at']
    raw['batches'].append(second)
    return raw
