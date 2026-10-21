import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from self_update import owner_review as approval
from self_update.policy import Rejected, TARGET, digest
from self_update.telegram_review import ReviewUI
import test_code_updater as fixtures
import railway_controller as controller


def pull():
    return dict(number=41,state='open',draft=False,changed_files=1,title='Numeric repair',
        user={'login':'github-actions[bot]'},
        head=dict(sha=fixtures.HEAD,ref='code-agent/numeric-repair-123-1',repo={'full_name':approval.REPOSITORY}),
        base=dict(sha=fixtures.BASE,ref='main'),body='Independent validation: 16480 numeric cases and 450 PAPER tests passed. '
        'Exact tested SHA256: '+('a'*64)+'.\nGenerator rationale (untrusted text): Handle nonfinite inputs.')


class FakeAPI(fixtures.FakeGitHub):
    def __init__(self,candidate,*,token='test',reviews=None):
        super().__init__(); self.token=token; self.pull=pull(); self.candidate=candidate
        self.reviews = reviews if reviews is not None else [dict(id=1,state='APPROVED',commit_id=fixtures.HEAD,user={'login':approval.OWNER})]
        self.actor=approval.OWNER; self.files=[dict(filename=TARGET,status='modified')]

    def request(self,method,path,data=None):
        self.calls.append((method,path,data))
        if path=='/user': return dict(login=self.actor)
        if path=='/pulls?state=open&per_page=20': return [copy.deepcopy(self.pull)]
        if path=='/pulls/41': return copy.deepcopy(self.pull)
        if path=='/pulls/41/files': return copy.deepcopy(self.files)
        if path=='/pulls/41/reviews':
            if method=='POST':
                self.reviews.append(dict(id=len(self.reviews)+1,state='APPROVED' if data['event']=='APPROVE' else 'CHANGES_REQUESTED',
                    commit_id=data['commit_id'],user={'login':self.actor}))
            return copy.deepcopy(self.reviews)
        if path=='/commits/'+fixtures.HEAD+'/statuses':
            return [dict(state='success',context='JALWE bounded code validation',
                creator={'login':'github-actions[bot]'},target_url=f'https://github.com/{approval.REPOSITORY}/actions/runs/123')]
        if path=='/git/commits/'+fixtures.HEAD:
            return dict(tree={'sha':'root'},parents=[{'sha':fixtures.BASE}])
        if path=='/git/trees/root': return dict(tree=[dict(path='intelligence',sha='sub')])
        if path=='/git/trees/sub': return dict(tree=[dict(path='session_strategy_engine.py',sha='blob')])
        if path=='/git/blobs/blob':
            return dict(encoding='base64',size=len(self.candidate.encode()),content=base64.b64encode(self.candidate.encode()).decode())
        if path=='/git/ref/heads/main': return dict(object={'sha':self.current})
        raise AssertionError((method,path))


class OwnerPublicationTests(unittest.TestCase):
    setUp = fixtures.CodeUpdaterTests.setUp
    bundle = fixtures.CodeUpdaterTests.bundle
    def test_approval_requires_owner_latest_review_exact_head_and_unchanged_base(self):
        api=FakeAPI(self.candidate)
        self.assertEqual(approval.inspect_review(api,41)['head']['sha'],fixtures.HEAD)
        for change in ('other_owner','rejected','wrong_head','stale','dismissed'):
            api=FakeAPI(self.candidate)
            if change=='other_owner': api.reviews[0]['user']['login']='other'
            elif change=='rejected': api.reviews.append(dict(id=2,state='CHANGES_REQUESTED',commit_id=fixtures.HEAD,user={'login':approval.OWNER}))
            elif change=='wrong_head': api.reviews[0]['commit_id']='c'*40
            elif change=='stale': api.current='c'*40
            else: api.reviews[0]['state']='DISMISSED'
            with self.assertRaises(Rejected): approval.inspect_review(api,41)
            self.assertEqual(api.child_calls,[])

    def test_approved_candidate_cannot_change_rules_tests_or_other_files(self):
        for suffix in ('\n# unapproved extra text\n','\nDANGER = 1\n'):
            api=FakeAPI(self.candidate+suffix)
            with self.assertRaises(Rejected): approval.reviewed_candidate(api,41,self.source,fixtures.BASE)
        api=FakeAPI(self.candidate); api.files.append(dict(filename='core/config.py',status='modified'))
        with self.assertRaises(Rejected): approval.reviewed_candidate(api,41,self.source,fixtures.BASE)

    def test_only_retested_exact_owner_approved_head_can_be_applied(self):
        report=self.bundle(); report.update(approved_pr=41,approved_head=fixtures.HEAD)
        (self.folder/'report.json').write_text(json.dumps(report))
        api=FakeAPI(self.candidate)
        result=approval.publish(self.folder,api=api,base=fixtures.BASE,source=self.source,run_id='123',attempt='1')
        self.assertEqual(result['state'],'OWNER_APPROVED_PUBLISHED_FOR_PAPER')
        self.assertEqual(api.child_calls,[(approval.REPOSITORY,fixtures.BASE,fixtures.HEAD)])
        api=FakeAPI(self.candidate,reviews=[])
        with self.assertRaises(Rejected): approval.publish(self.folder,api=api,base=fixtures.BASE,source=self.source,run_id='123',attempt='1')
        self.assertEqual(api.child_calls,[])


class TelegramReviewTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.api=FakeAPI('unused',reviews=[])
        self.ui=ReviewUI(Path(tmp.name)/'review.db','123',api=self.api,clock=lambda:1000.)
        self.sent=[]; self.ui.poll(self.sent.append)
        with self.ui.connect() as conn:
            self.row=dict(conn.execute('SELECT * FROM code_owner_reviews').fetchone())

    def callback(self,action='approve',actor='123'):
        return self.ui.callback('code-'+action+':'+self.row['request_id'],actor)

    def test_request_is_durable_and_deduplicated_and_only_owner_can_click(self):
        self.assertEqual(len(self.sent),1)
        restarted=ReviewUI(self.ui.path,'123',api=self.api,clock=lambda:2000.)
        restarted.poll(self.sent.append); self.assertEqual(len(self.sent),1)
        with self.assertRaises(ValueError): self.callback(actor='other')
        self.assertFalse(any(m=='POST' for m,_,_ in self.api.calls))

    def test_failed_notification_remains_pending_for_next_poll(self):
        with self.ui.connect() as conn:
            conn.execute('UPDATE code_owner_reviews SET notified=0')
        self.ui.clock=lambda:2000.
        self.ui.poll(lambda message: False)
        with self.ui.connect() as conn:
            self.assertEqual(conn.execute('SELECT notified FROM code_owner_reviews').fetchone()[0],0)
        self.ui.clock=lambda:3000.
        self.ui.poll(self.sent.append)
        with self.ui.connect() as conn:
            self.assertEqual(conn.execute('SELECT notified FROM code_owner_reviews').fetchone()[0],1)

    def test_approval_posts_exact_head_review_and_repeat_does_not_post_again(self):
        self.assertIn('ستعاد الاختبارات',self.callback())
        self.callback()
        posts=[d for m,p,d in self.api.calls if m=='POST']
        self.assertEqual(len(posts),1); self.assertEqual(posts[0]['commit_id'],fixtures.HEAD)
        self.assertEqual(posts[0]['event'],'APPROVE')

    def test_rejection_and_changed_head_never_apply_or_approve(self):
        self.assertIn('رُفض',self.callback('reject'))
        self.assertEqual(self.api.reviews[-1]['state'],'CHANGES_REQUESTED')
        self.assertEqual(self.api.child_calls,[])
        with self.ui.connect() as conn:
            conn.execute("UPDATE code_owner_reviews SET decision='PENDING'")
        self.api.pull['head']['sha']='c'*40
        with self.assertRaises(ValueError): self.callback()
        self.assertEqual(len([m for m,_,_ in self.api.calls if m=='POST']),1)

    def test_missing_key_requires_explicit_github_confirmation_and_never_applies(self):
        self.api.token=''
        reply=self.callback()
        self.assertIn('لم يُطبّق التعديل',reply['text'])
        self.assertEqual(self.api.child_calls,[])
        self.assertFalse(any(m=='POST' for m,_,_ in self.api.calls))

    def test_controller_routes_owner_review_callback_and_poll_receives_buttons(self):
        event=dict(callback_query=dict(id='cb',data='code-approve:'+self.row['request_id'],
            message={'chat':{'id':123}},**{'from':{'id':123}}))
        with patch.object(controller,'TELEGRAM_CHAT_ID','123'),patch.object(controller,'code_review_ui',return_value=self.ui), \
             patch.object(controller,'tg_call'),patch.object(controller,'send_message') as send:
            self.assertTrue(controller.process_telegram_update(event)); send.assert_called_once()
        source=Path(controller.__file__).read_text()
        self.assertIn('["message", "callback_query"]',source)

    def test_callback_network_failure_is_safe_and_expired_consent_cannot_approve(self):
        event=dict(callback_query=dict(id='cb',data='code-approve:'+self.row['request_id'],
            message={'chat':{'id':123}},**{'from':{'id':123}}))
        with patch.object(controller,'TELEGRAM_CHAT_ID','123'),patch.object(controller,'code_review_ui') as ui, \
             patch.object(controller,'tg_call'),patch.object(controller,'send_message') as send:
            ui.return_value.callback.side_effect=RuntimeError('secret must not be sent')
            self.assertTrue(controller.process_telegram_update(event))
            self.assertNotIn('secret',send.call_args.args[0])
        self.ui.clock=lambda:1000+86401
        with self.assertRaises(ValueError): self.callback()
        self.assertFalse(any(m=='POST' for m,_,_ in self.api.calls))
