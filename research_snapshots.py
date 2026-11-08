"""Immutable deduplicated research aggregates; no production migration."""
import copy,hashlib,json
FIELDS=('factor_benchmark','historical_jump_profiles')
def compact(conn,payload):
    p=copy.deepcopy(payload)
    r=p
    for key in ('jalwe','metadata','market_move_research'):
        r=r.get(key) if isinstance(r,dict) else None
    if not isinstance(r,dict):return p
    conn.execute('CREATE TABLE IF NOT EXISTS research_aggregate_snapshots (snapshot_id TEXT PRIMARY KEY, body_json TEXT NOT NULL)')
    for key in FIELDS:
        if key not in r:continue
        body=json.dumps(r[key],sort_keys=True,default=str,ensure_ascii=False,separators=(',',':'))
        ident=hashlib.sha256(body.encode()).hexdigest()
        conn.execute('INSERT OR IGNORE INTO research_aggregate_snapshots VALUES (?,?)',(ident,body))
        saved=conn.execute('SELECT body_json FROM research_aggregate_snapshots WHERE snapshot_id=?',(ident,)).fetchone()[0]
        if saved!=body:raise ValueError('Research snapshot collision')
        r[key]={'__research_snapshot_v1__':ident}
    return p

def expand(conn,payload):
    p=copy.deepcopy(payload);r=p
    for key in ('jalwe','metadata','market_move_research'):
        r=r.get(key) if isinstance(r,dict) else None
    if not isinstance(r,dict):return p
    for key in FIELDS:
        value=r.get(key)
        if not isinstance(value,dict) or set(value)!={'__research_snapshot_v1__'}:continue
        ident=value['__research_snapshot_v1__']
        row=conn.execute('SELECT body_json FROM research_aggregate_snapshots WHERE snapshot_id=?',(ident,)).fetchone()
        if not row or hashlib.sha256(row[0].encode()).hexdigest()!=ident:raise ValueError('Missing or corrupt research snapshot')
        r[key]=json.loads(row[0])
    return p

def load(db,raw):
    with db.connection() as conn:return expand(conn,json.loads(raw))
