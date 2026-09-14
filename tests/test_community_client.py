import copy
import datetime
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import os
import unittest
from unittest.mock import patch

CLIENT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'community_client.py'
spec = importlib.util.spec_from_file_location('community_client', CLIENT)
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)

START = '2026-09-14T10:00:00Z'
NEW = '2026-09-14T10:01:00Z'
OLD = '2026-09-13T10:00:00Z'

def comment(id='C1', author='bob', parent=None, body='A useful reply', date=NEW):
    return {'id': id, 'author': {'login': author}, 'body': body, 'createdAt': date,
            'url': 'https://github.com/example/commons/discussions/1#' + id,
            'replyTo': parent}

def discussion(author='bob', comments=None, date=NEW, labels=None, body='New question'):
    return {'id': 'D1', 'number': 1, 'title': 'A new question', 'author': {'login': author},
            'body': body, 'createdAt': date, 'url': 'https://github.com/example/commons/discussions/1',
            'category': {'name': 'Questions', 'slug': 'questions'},
            'labels': labels or ['memory'], 'comments': comments or []}

class InboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.box = client.Inbox(self.root / 'state.sqlite3', 'example/commons', 'alice', START)

    def tearDown(self):
        self.box.close()
        self.temp.cleanup()

    def ingest(self, docs, topics=None):
        return self.box.ingest(iter(docs), topics or [])

    def test_baseline_does_not_flood_old_history(self):
        self.ingest([discussion(date=OLD, comments=[comment(date=OLD)])])
        self.assertEqual(self.box.pending(), [])

    def test_new_topic_matches_subscription(self):
        self.ingest([discussion()], ['memory'])
        event = self.box.pending()[0]
        self.assertEqual(event['reasons'], ['new_topic'])
        self.assertEqual(event['event_id'], 'example/commons:D1')

    def test_other_topic_is_not_delivered(self):
        self.ingest([discussion()], ['evaluation'])
        self.assertEqual(self.box.pending(), [])

    def test_form_topic_works_without_labels(self):
        doc = discussion(body='### 主题\n\n记忆与上下文, 评测与实验\n\n### 发现\n正文')
        doc['labels'] = []
        self.ingest([doc], ['记忆与上下文'])
        self.assertEqual(len(self.box.pending()), 1)

    def test_reply_to_old_owned_topic_ignores_topic_filter(self):
        self.ingest([discussion(author='alice', date=OLD, comments=[comment()])], ['evaluation'])
        self.assertEqual(self.box.pending()[0]['reasons'], ['reply_to_your_topic'])

    def test_direct_reply_to_owned_comment(self):
        parent = {'id': 'P1', 'author': {'login': 'alice'}}
        self.ingest([discussion(date=OLD, comments=[comment(parent=parent)])], ['evaluation'])
        self.assertEqual(self.box.pending()[0]['reasons'], ['reply_to_your_comment'])

    def test_mention_is_exact_and_case_insensitive(self):
        docs = [discussion(date=OLD, comments=[comment(body='Hello @ALICE, thoughts?')])]
        self.ingest(docs)
        self.assertEqual(self.box.pending()[0]['reasons'], ['mention'])
        docs[0]['comments'] = [comment(id='C2', body='@alice-bot is a different account')]
        self.ingest(docs)
        self.assertEqual(len(self.box.pending()), 1)

    def test_self_messages_are_not_echoed(self):
        self.ingest([discussion(author='alice', comments=[comment(author='alice', body='@alice')])])
        self.assertEqual(self.box.pending(), [])

    def test_repeated_scan_and_restart_do_not_duplicate(self):
        self.ingest([discussion()])
        self.box.close()
        self.box = client.Inbox(self.root / 'state.sqlite3', 'example/commons', 'alice', START)
        self.ingest([discussion()])
        self.assertEqual(len(self.box.pending()), 1)

    def test_partial_scan_rolls_back_without_losing_retry(self):
        def broken():
            yield discussion()
            raise client.ClientError('rate_limit')
        with self.assertRaises(client.ClientError):
            self.box.ingest(broken(), [])
        self.assertEqual(self.box.pending(), [])
        self.ingest([discussion()])
        self.assertEqual(len(self.box.pending()), 1)

    def test_reconnect_catches_new_reply_to_old_topic(self):
        self.ingest([discussion(author='alice', date=OLD)])
        self.ingest([discussion(author='alice', date=OLD, comments=[comment()])])
        self.assertEqual(len(self.box.pending()), 1)

    def test_wrong_identity_cannot_reuse_state(self):
        with self.assertRaises(client.ClientError):
            client.Inbox(self.root / 'state.sqlite3', 'example/commons', 'mallory', START)

    def test_receiver_exit_zero_without_receipt_is_not_ack(self):
        self.ingest([discussion()])
        handler = [sys.executable, '-c', 'print("ok")']
        result = client.deliver(self.box, handler, self.root, 5)
        self.assertEqual(result['accepted'], 0)
        self.assertEqual(len(self.box.pending()), 1)

    def test_exact_receipt_marks_accepted_but_preserves_record(self):
        self.ingest([discussion()])
        handler = [sys.executable, '-c', 'import json,sys; e=json.load(sys.stdin); print(json.dumps({"event_id":e["event_id"],"accepted":True,"receipt_ref":"test:received"}))']
        result = client.deliver(self.box, handler, self.root, 5)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(self.box.pending(), [])
        rows = self.box.records('all')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['delivery']['state'], 'accepted')

    def test_wrong_event_receipt_is_rejected(self):
        self.ingest([discussion()])
        handler = [sys.executable, '-c', 'print(\'{"event_id":"wrong","accepted":true,"receipt_ref":"x"}\')']
        self.assertEqual(client.deliver(self.box, handler, self.root, 5)['accepted'], 0)

    def test_receiver_timeout_retains_event(self):
        self.ingest([discussion()])
        with patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('receiver', 1)):
            self.assertEqual(client.deliver(self.box, ['receiver'], self.root, 1)['accepted'], 0)
        self.assertEqual(len(self.box.pending()), 1)

    def test_payload_remains_data_without_shell_interpretation(self):
        sentinel = self.root / 'must-not-exist'
        text = '$(touch ' + str(sentinel) + '); `touch ' + str(sentinel) + '`'
        self.ingest([discussion(body=text)])
        handler = [sys.executable, '-c', 'import json,sys; e=json.load(sys.stdin); assert "$(touch" in e["body"]; print(json.dumps({"event_id":e["event_id"],"accepted":True,"receipt_ref":"test:data"}))']
        self.assertEqual(client.deliver(self.box, handler, self.root, 5)['accepted'], 1)
        self.assertFalse(sentinel.exists())

    def test_backoff_prevents_immediate_retry(self):
        self.ingest([discussion()])
        handler = [sys.executable, '-c', 'raise SystemExit(2)']
        first = client.deliver(self.box, handler, self.root, 5)
        second = client.deliver(self.box, handler, self.root, 5)
        self.assertEqual(first['attempted'], 1)
        self.assertEqual(second['attempted'], 0)

class ProviderTests(unittest.TestCase):
    def test_missing_page_info_boolean_is_not_completion(self):
        api = client.GitHub('gh', 10)
        with self.assertRaises(client.ClientError):
            list(api.pages(lambda after: {'nodes':[], 'pageInfo':{}}))

    def test_comments_and_replies_follow_their_own_cursors(self):
        def connection(nodes, cursor=None):
            return {'nodes':nodes, 'pageInfo':{'hasNextPage':bool(cursor), 'endCursor':cursor}}
        first_comment = comment('C1', 'alice', date=OLD)
        first_comment['replies'] = connection([], 'r-next')
        doc = discussion(date=OLD)
        doc['labels'] = connection([{'name':'memory'}], 'l-next')
        doc['comments'] = connection([first_comment], 'c-next')
        second_comment = comment('C2')
        second_comment['replies'] = connection([])
        calls = []
        api = client.GitHub('gh', 10)
        def query(query, variables):
            calls.append(variables)
            if 'owner' in variables:
                return {'repository':{'discussions':connection([copy.deepcopy(doc)])}}
            after = variables['after']
            if after == 'l-next':
                return {'node':{'labels':connection([{'name':'evaluation'}])}}
            if after == 'c-next':
                return {'node':{'comments':connection([second_comment])}}
            if after == 'r-next':
                return {'node':{'replies':connection([comment('R1', parent={'id':'C1','author':{'login':'alice'}})])}}
            self.fail('Unexpected cursor')
        with patch.object(api, 'query', side_effect=query):
            result = list(api.scan('example/commons'))
        self.assertEqual(result[0]['labels'], ['memory','evaluation'])
        self.assertEqual([x['id'] for x in result[0]['comments']], ['C1','R1','C2'])
        self.assertEqual(len(calls), 4)

    def test_pagination_follows_every_page(self):
        api = client.GitHub('gh', 10)
        pages = [
            {'nodes': [{'id':'one'}], 'pageInfo': {'hasNextPage':True, 'endCursor':'next'}},
            {'nodes': [{'id':'two'}], 'pageInfo': {'hasNextPage':False, 'endCursor':None}}
        ]
        calls = []
        def fetch(after):
            calls.append(after)
            return pages[len(calls)-1]
        self.assertEqual([x['id'] for x in api.pages(fetch)], ['one','two'])
        self.assertEqual(calls, [None,'next'])

    def test_missing_pagination_cursor_fails_closed(self):
        api = client.GitHub('gh', 10)
        with self.assertRaises(client.ClientError):
            list(api.pages(lambda after: {'nodes':[], 'pageInfo':{'hasNextPage':True, 'endCursor':None}}))

    def test_query_budget_fails_before_network(self):
        api = client.GitHub('gh', 0)
        with patch.object(subprocess, 'run') as run:
            with self.assertRaises(client.ClientError):
                api.query('query { viewer { login } }', {})
            run.assert_not_called()

    def test_graphql_partial_errors_are_not_accepted(self):
        api = client.GitHub('gh', 10)
        result = subprocess.CompletedProcess([], 0, '{"data":{},"errors":[{"message":"no"}]}', '')
        with patch.object(subprocess, 'run', return_value=result):
            with self.assertRaises(client.ClientError):
                api.query('query { viewer { login } }', {})

class DeploymentJourneyTests(unittest.TestCase):
    def test_inbox_only_profile_cannot_claim_delivery(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            config = {'v':1,'repo':'example/commons','login':'alice','agent_id':'one',
                      'started_at':START,'gh':'gh','topics':[],'handler':[],
                      'max_calls':10,'handler_timeout':60}
            client.save_new_config(directory, config)
            with patch.object(sys, 'stderr'):
                self.assertEqual(client.main(['poll','--deliver','--state-dir',temp]), 1)
            self.assertFalse((directory/'state.sqlite3').exists())

    def test_new_consumer_setup_poll_deliver_restart_and_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            # The executable and snapshot are test fixtures; no remote API is contacted.
            snapshot = root / 'snapshot.json'
            fake_gh = root / 'gh'
            fake_gh.write_text('#!' + sys.executable + '''
import json,os,sys
p=json.load(sys.stdin)
assert p['query'].lstrip().startswith('query')
if 'viewer {' in p['query']:
    data={'viewer':{'login':'alice'},'repository':{'nameWithOwner':'example/commons','hasDiscussionsEnabled':True}}
else:
    docs=json.load(open(os.environ['ALC_TEST_SNAPSHOT']))
    data={'repository':{'discussions':{'nodes':docs,'pageInfo':{'hasNextPage':False,'endCursor':None}}}}
print(json.dumps({'data':data}))
''')
            fake_gh.chmod(0o700)
            profile, received = root / 'profile', root / 'received'
            receiver = CLIENT.parents[1] / 'receivers' / 'file_inbox.py'
            env = dict(os.environ, ALC_TEST_SNAPSHOT=str(snapshot), PYTHONDONTWRITEBYTECODE='1',
                       PATH=str(root) + os.pathsep + os.environ.get('PATH', ''))
            handler = [sys.executable, str(receiver), '--directory', str(received)]
            def run(*args, success=True):
                result = subprocess.run([sys.executable,str(CLIENT),*args,'--state-dir',str(profile)],
                                        text=True,capture_output=True,env=env,timeout=10)
                self.assertEqual(result.returncode, 0 if success else 1, result.stderr)
                return json.loads(result.stdout if success else result.stderr)
            setup = CLIENT.with_name('setup-client.sh')
            setup_result = subprocess.run(['sh',str(setup),'--repo','example/commons','--agent-id','willow',
                                           '--gh',str(fake_gh),'--handler-json',json.dumps(handler),
                                           '--state-dir',str(profile)], text=True,capture_output=True,env=env,timeout=10)
            self.assertEqual(setup_result.returncode, 0, setup_result.stderr)
            init = json.loads(setup_result.stdout)
            self.assertFalse(init['monitor_running'])
            self.assertEqual(run('doctor')['delivery_mode'], 'receiver_configured_not_yet_verified')
            def wire(comments):
                node = discussion(author='alice', date=OLD)
                node['labels']={'nodes':[{'name':'memory'}],'pageInfo':{'hasNextPage':False,'endCursor':None}}
                node['comments']={'nodes':comments,'pageInfo':{'hasNextPage':False,'endCursor':None}}
                snapshot.write_text(json.dumps([node]))
            wire([])
            self.assertEqual(run('poll')['new_events'], 0)
            reply = comment(date=client.utc_now())
            reply['replies']={'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}
            wire([reply])
            polled = run('poll','--deliver')
            self.assertEqual(polled['new_events'], 1)
            self.assertEqual(polled['delivery']['accepted'], 1)
            self.assertEqual(run('status')['accepted'], 1)
            self.assertEqual(run('inbox'), [])
            all_events = run('inbox','--all')
            self.assertEqual(all_events[0]['recipient']['agent_id'], 'willow')
            self.assertEqual(len(list(received.glob('*.json'))), 1)
            self.assertEqual(run('poll','--deliver')['new_events'], 0)
            self.assertEqual(len(run('inbox','--all')), 1)
            self.assertIn('already exists', run('init','--repo','example/commons','--agent-id','other','--gh',str(fake_gh),success=False)['error'])
            self.assertEqual(run('status')['accepted'], 1)

    def test_two_monitors_cannot_use_one_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = pathlib.Path(temp)
            with client.process_lock(directory):
                with self.assertRaises(client.ClientError):
                    with client.process_lock(directory):
                        pass

    def test_distinct_recipient_file_inboxes_do_not_collide(self):
        receiver = CLIENT.parents[1] / 'receivers' / 'file_inbox.py'
        with tempfile.TemporaryDirectory() as temp:
            for agent in ['one','two','one']:
                event = {'event_id':'same-source-event','recipient':{'agent_id':agent},'body':'Fixture'}
                result = subprocess.run([sys.executable,str(receiver),'--directory',temp],input=json.dumps(event),text=True,capture_output=True,check=True)
                self.assertFalse(json.loads(result.stdout)['agent_woken'])
            self.assertEqual(len(list(pathlib.Path(temp).glob('*.json'))), 2)

if __name__ == '__main__':
    unittest.main()
