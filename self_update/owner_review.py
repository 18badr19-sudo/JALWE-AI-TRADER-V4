"""Trusted owner approval is bound to one exact PR head and unchanged base."""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap

from self_update.contracts import run_contract
from self_update.policy import TARGET, Rejected, apply_proposal, digest, methods
from self_update.publisher import GitHub, verified_candidate

REPOSITORY = '18badr19-sudo/JALWE-AI-TRADER-V4'
OWNER = '18badr19-sudo'


def proposal_for(source, candidate, base):
    edits = []
    original, changed = methods(source), methods(candidate)
    lines = candidate.splitlines()
    for name,node in changed.items():
        old = original[name]
        old_body = source.splitlines()[old.body[0].lineno-1:old.body[-1].end_lineno]
        body = lines[node.body[0].lineno-1:node.body[-1].end_lineno]
        if old_body != body:
            edits.append(dict(method=name,body=textwrap.dedent('\n'.join(body))))
    proposal = dict(schema_version=1,base_sha=base,rationale='Exact owner-reviewed numeric repair',edits=edits)
    if not edits or apply_proposal(source,proposal,base) != candidate:
        raise Rejected('APPROVED_CHANGE_OUTSIDE_NUMERIC_POLICY')
    return proposal


def inspect_review(api, number):
    if type(number) is not int or number <= 0:
        raise Rejected('INVALID_REVIEW_PR_NUMBER')
    pull = api.request('GET',f'/pulls/{number}')
    if (pull.get('state') != 'open' or pull.get('draft') is not False
        or pull.get('user',{}).get('login') != 'github-actions[bot]'
        or pull.get('base',{}).get('ref') != 'main'
        or pull.get('head',{}).get('repo',{}).get('full_name') != REPOSITORY
        or not re.fullmatch(r'code-agent/numeric-repair-\d+-\d+',pull.get('head',{}).get('ref',''))):
        raise Rejected('PR_NOT_TRUSTED_CODE_AGENT_PROPOSAL')
    head,base = pull['head']['sha'],pull['base']['sha']
    if not all(re.fullmatch(r'[0-9a-f]{40}',value) for value in (head,base)):
        raise Rejected('INVALID_REVIEW_COMMIT')
    if api.request('GET','/git/ref/heads/main')['object']['sha'] != base:
        raise Rejected('OWNER_APPROVAL_BASE_IS_STALE')
    reviews = api.request('GET',f'/pulls/{number}/reviews')
    # Do not accept an early approval while a later review is off-page.
    if not isinstance(reviews,list) or len(reviews) >= 30:
        raise Rejected('REVIEW_HISTORY_UNAVAILABLE_OR_UNBOUNDED')
    owner = [r for r in reviews if r.get('user',{}).get('login') == OWNER
             and r.get('state') in {'APPROVED','CHANGES_REQUESTED','DISMISSED'}]
    latest = max(owner,key=lambda r:int(r['id'])) if owner else None
    if not latest or latest['state'] != 'APPROVED' or latest.get('commit_id') != head:
        raise Rejected('EXACT_HEAD_OWNER_APPROVAL_REQUIRED')
    return pull


def reviewed_candidate(api, number, source, base):
    pull = inspect_review(api,number)
    if pull['base']['sha'] != base:
        raise Rejected('APPROVAL_CHECKOUT_BASE_MISMATCH')
    files = api.request('GET',f'/pulls/{number}/files')
    if (pull.get('changed_files') != 1 or len(files) != 1 or files[0].get('filename') != TARGET
        or files[0].get('status') != 'modified'):
        raise Rejected('APPROVED_FILES_OUTSIDE_POLICY')
    head = pull['head']['sha']
    commit = api.request('GET','/git/commits/'+head)
    if [p['sha'] for p in commit.get('parents',[])] != [base]:
        raise Rejected('APPROVED_HEAD_NOT_EXACT_CHILD')
    sha = commit['tree']['sha']
    for part in TARGET.split('/'):
        tree = api.request('GET','/git/trees/'+sha)
        if tree.get('truncated'):
            raise Rejected('APPROVED_TREE_TRUNCATED')
        entry = next((e for e in tree['tree'] if e['path']==part),None)
        if entry is None:
            raise Rejected('APPROVED_SOURCE_UNAVAILABLE')
        sha = entry['sha']
    blob = api.request('GET','/git/blobs/'+sha)
    if blob.get('encoding') != 'base64' or blob.get('size',100001) > 100000:
        raise Rejected('APPROVED_SOURCE_ENCODING_OR_SIZE')
    candidate = base64.b64decode(blob['content'].replace('\n',''),validate=True).decode('utf-8')
    proposal = proposal_for(source,candidate,base)
    if run_contract(candidate)['failures'] or not run_contract(source)['failures']:
        raise Rejected('APPROVED_REPAIR_CONTRACT_FAILED')
    return pull,candidate,proposal


def prepare(folder, *, api=None, number=None):
    folder = Path(folder); folder.mkdir(parents=True,exist_ok=True)
    api = api or GitHub(REPOSITORY,os.environ.get('CODE_UPDATE_GITHUB_TOKEN',''))
    event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_text()) if number is None else {}
    number = number or event['pull_request']['number']
    base = subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    source = subprocess.check_output(['git','show',f'{base}:{TARGET}'],text=True)
    pull,candidate,proposal = reviewed_candidate(api,number,source,base)
    report = dict(state='CONTRACT_PASSED',base_sha=base,base_sha256=digest(source),
        candidate_sha256=digest(candidate),baseline_contract=run_contract(source),
        candidate_contract=run_contract(candidate),tests_passed=False,
        approved_pr=number,approved_head=pull['head']['sha'])
    (folder/'proposal.json').write_text(json.dumps(proposal))
    (folder/'report.json').write_text(json.dumps(report))
    Path(TARGET).write_text(candidate)


def publish(folder, *, api=None, base=None, source=None, run_id=None, attempt=None):
    folder = Path(folder)
    api = api or GitHub(REPOSITORY,os.environ.get('CODE_UPDATE_GITHUB_TOKEN',''))
    base = base or subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    source = source or subprocess.check_output(['git','show',f'{base}:{TARGET}'],text=True)
    run_id = run_id or os.environ['GITHUB_RUN_ID']; attempt = attempt or os.environ['GITHUB_RUN_ATTEMPT']
    candidate,_,report = verified_candidate(folder,base,source,run_id,attempt)
    pull = inspect_review(api,report['approved_pr'])
    head = pull['head']['sha']
    if head != report['approved_head'] or pull['base']['sha'] != base:
        raise Rejected('REVIEW_CHANGED_AFTER_TESTS')
    _,current,_ = reviewed_candidate(api,report['approved_pr'],source,base)
    if digest(current) != digest(candidate):
        raise Rejected('REVIEWED_BYTES_CHANGED_AFTER_TESTS')
    # Recheck the latest owner review immediately before the guarded write.
    if inspect_review(api,report['approved_pr'])['head']['sha'] != head:
        raise Rejected('REVIEW_CHANGED_BEFORE_PUBLICATION')
    if not api.publish_child(REPOSITORY,base,head):
        return dict(state='APPROVED_PR_AWAITING_GITHUB_REQUIREMENTS',pr=report['approved_pr'])
    if api.request('GET','/git/ref/heads/main')['object']['sha'] != head:
        raise Rejected('APPROVED_PUBLICATION_REF_MISMATCH')
    return dict(state='OWNER_APPROVED_PUBLISHED_FOR_PAPER',pr=report['approved_pr'],sha=head)
