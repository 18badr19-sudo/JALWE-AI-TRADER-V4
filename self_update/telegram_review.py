"""Owner-only Telegram decisions on tested, staged GitHub proposals."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.request

from self_update.owner_review import OWNER, REPOSITORY


class ReviewAPI:
    def __init__(self, token=None):
        self.token = token if token is not None else os.getenv('JALWE_CODE_REVIEW_GITHUB_TOKEN','').strip()

    def request(self, method, path, payload=None):
        if not path.startswith('/') or '..' in path or '://' in path:
            raise ValueError('Invalid review API path')
        url = 'https://api.github.com' + (path if path == '/user' else '/repos/'+REPOSITORY+path)
        headers = {'Accept':'application/vnd.github+json','User-Agent':'jalwe-owner-review',
                   'Content-Type':'application/json','X-GitHub-Api-Version':'2022-11-28'}
        if self.token:
            headers['Authorization'] = 'Bearer '+self.token
        request = urllib.request.Request(url,method=method,headers=headers,
            data=json.dumps(payload).encode() if payload is not None else None)
        # Do not forward an authorization header through a redirect.
        from self_update.publisher import NoRedirect
        with urllib.request.build_opener(NoRedirect()).open(request,timeout=10) as response:
            return json.load(response)


class ReviewUI:
    def __init__(self, path, chat_id, *, api=None, clock=time.time):
        self.path,self.chat_id,self.api,self.clock = str(path),str(chat_id),api or ReviewAPI(),clock
        Path(path).parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS code_owner_reviews (
                request_id TEXT PRIMARY KEY, pr INTEGER NOT NULL, base TEXT NOT NULL, head TEXT NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL, notified INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL DEFAULT 'PENDING', decided_by TEXT, UNIQUE(pr,head,base))''')
        self.last_poll = 0

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path,timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def trusted_pull(self, pull):
        return (pull.get('state')=='open' and pull.get('draft') is False
            and pull.get('user',{}).get('login')=='github-actions[bot]'
            and pull.get('base',{}).get('ref')=='main'
            and pull.get('head',{}).get('repo',{}).get('full_name')==REPOSITORY
            and re.fullmatch(r'code-agent/numeric-repair-\d+-\d+',pull.get('head',{}).get('ref',''))
            and all(re.fullmatch(r'[0-9a-f]{40}',pull.get(k,{}).get('sha','')) for k in ('base','head')))

    def poll(self, send):
        now = self.clock()
        if now-self.last_poll < 300:
            return
        self.last_poll = now
        pulls = self.api.request('GET','/pulls?state=open&per_page=20')
        if not isinstance(pulls,list):
            raise ValueError('Unavailable PR listing')
        main = None
        for pull in [p for p in pulls if self.trusted_pull(p)][:3]:
            if main is None:
                main = self.api.request('GET','/git/ref/heads/main')['object']['sha']
            if pull['base']['sha'] != main:
                continue
            head = pull['head']['sha']
            statuses = self.api.request('GET','/commits/'+head+'/statuses')
            proofs = [s for s in statuses if s.get('context')=='JALWE bounded code validation']
            if not proofs:
                continue
            proof = proofs[0]  # API returns latest statuses first.
            if (proof.get('state')!='success' or proof.get('creator',{}).get('login')!='github-actions[bot]'
                or not re.fullmatch(r'https://github.com/'+re.escape(REPOSITORY)+r'/actions/runs/\d+',proof.get('target_url',''))):
                continue
            body = str(pull.get('body') or '')
            trusted = body.split('Generator rationale (untrusted text):',1)[0]
            match = re.search(r'(\d+) numeric cases and (\d+) PAPER tests passed\. Exact tested SHA256: ([0-9a-f]{64})',trusted)
            if not match or int(match[2])<190:
                continue
            payload = dict(title=str(pull.get('title',''))[:160],tests=int(match[2]),
                numeric_cases=int(match[1]),candidate_hash=match[3],
                rationale=body.split('Generator rationale (untrusted text):',1)[-1][:700],
                evidence=proof['target_url'])
            with self.connect() as conn:
                conn.execute('''INSERT OR IGNORE INTO code_owner_reviews
                    (request_id,pr,base,head,payload,created) VALUES (?,?,?,?,?,?)''',
                    (secrets.token_hex(12),int(pull['number']),main,head,json.dumps(payload),now))
                row = conn.execute('SELECT * FROM code_owner_reviews WHERE pr=? AND head=? AND base=?',
                                   (pull['number'],head,main)).fetchone()
            if row['decision']!='PENDING' or row['notified'] or now-row['created']>86400:
                continue
            send(self.prompt(row))
            with self.connect() as conn:
                conn.execute('UPDATE code_owner_reviews SET notified=1 WHERE request_id=?',(row['request_id'],))

    @staticmethod
    def prompt(row):
        payload = json.loads(row['payload']); url = f"https://github.com/{REPOSITORY}/pull/{row['pr']}"
        return dict(text='🧠 طلب موافقة على تعديل ذاتي — PAPER\n\n'
            f"{payload['title']}\nالنطاق: إصلاح _num/_clamp فقط؛ تغيير شروط التداول خارج هذا المحدث.\n"
            f"سبب الاقتراح (من مولد الكود): {payload['rationale']}\n"
            f"الاختبارات: {payload['tests']} PAPER و{payload['numeric_cases']} حالة حسابية اجتازت.\n"
            f"نسخة التغيير: {row['head'][:12]}\nSHA256 المختبر: {payload['candidate_hash']}\n"
            'لن يُطبّق قبل موافقتك وإعادة الاختبار. إذا تغيرت النسخة تلزم موافقة جديدة.',
            reply_markup={'inline_keyboard':[[
                {'text':'✅ موافقة','callback_data':'code-approve:'+row['request_id']},
                {'text':'❌ رفض','callback_data':'code-reject:'+row['request_id']}],
                [{'text':'عرض التغيير والاختبارات','url':url}]]})

    def callback(self, data, actor):
        if not self.chat_id.isdigit() or str(actor)!=self.chat_id:
            raise ValueError('الموافقة على التعديل متاحة لمالك المحادثة الخاصة فقط.')
        match = re.fullmatch(r'code-(approve|reject):([0-9a-f]{24})',data)
        if not match:
            raise ValueError('طلب موافقة غير صالح.')
        action,key = match.groups()
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM code_owner_reviews WHERE request_id=?',(key,)).fetchone()
        if row is None or self.clock()-row['created']>86400:
            raise ValueError('انتهت صلاحية الطلب؛ يلزم طلب حديث.')
        if row['decision'] in {'APPROVED','REJECTED','STALE'}:
            return 'تم تسجيل القرار سابقًا: '+row['decision']
        pull = self.api.request('GET',f"/pulls/{row['pr']}")
        main = self.api.request('GET','/git/ref/heads/main')['object']['sha']
        if (not self.trusted_pull(pull) or pull['head']['sha']!=row['head']
            or pull['base']['sha']!=row['base'] or main!=row['base']):
            self.decide(key,'STALE',actor)
            raise ValueError('تغيرت النسخة أو أساسها؛ لم يُطبّق شيء ويلزم طلب جديد.')
        if not self.api.token:
            if action=='reject':
                self.decide(key,'REJECTED',actor)
                return '❌ رُفض هذا الطلب. لن يطبقه البوت تلقائيًا.'
            self.decide(key,'WAITING_GITHUB_CONFIRMATION',actor)
            return dict(text='سُجلت رغبتك بالموافقة، لكن مفتاح ربط مراجعات GitHub غير مضاف إلى البوت. '
                'لم يُطبّق التعديل. افتح الطلب واختر Review changes ثم Approve من حسابك لتأكيد الموافقة، '
                'أو أضف JALWE_CODE_REVIEW_GITHUB_TOKEN في Railway لتعمل الموافقة مباشرة من Telegram.',
                reply_markup={'inline_keyboard':[[{'text':'تأكيد الموافقة في GitHub',
                    'url':f"https://github.com/{REPOSITORY}/pull/{row['pr']}/files"}]]})
        if self.api.request('GET','/user').get('login')!=OWNER:
            raise ValueError('مفتاح GitHub لا يخص مالك المستودع؛ لم يُطبّق شيء.')
        reviews = self.api.request('GET',f"/pulls/{row['pr']}/reviews")
        if len(reviews)>=30:
            raise ValueError('سجل المراجعات يحتاج مراجعة يدوية؛ لم يُطبّق شيء.')
        desired = 'APPROVED' if action=='approve' else 'CHANGES_REQUESTED'
        owner = [r for r in reviews if r.get('user',{}).get('login')==OWNER
                 and r.get('state') in {'APPROVED','CHANGES_REQUESTED','DISMISSED'}]
        last = max(owner,key=lambda r:int(r['id'])) if owner else {}
        if last.get('state')!=desired or last.get('commit_id')!=row['head']:
            self.api.request('POST',f"/pulls/{row['pr']}/reviews",dict(commit_id=row['head'],
                event='APPROVE' if action=='approve' else 'REQUEST_CHANGES',
                body='Owner decision through the private JALWE Telegram controller; exact tested head only.'))
        self.decide(key,'APPROVED' if action=='approve' else 'REJECTED',actor)
        return ('✅ وافقت على النسخة المحددة. ستعاد الاختبارات قبل التطبيق؛ الموافقة ليست تأكيد نشر.'
                if action=='approve' else '❌ رُفض التعديل على GitHub. لن يُطبّق.')

    def decide(self, key, decision, actor):
        with self.connect() as conn:
            conn.execute('UPDATE code_owner_reviews SET decision=?,decided_by=? WHERE request_id=?',
                         (decision,str(actor),key))
