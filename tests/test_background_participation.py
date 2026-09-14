import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import datetime
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / 'scripts/community_client.py'
SERVICE = ROOT / 'scripts/community_service.py'
RECEIVER = ROOT / 'receivers/claude_code.py'
sys.path.insert(0,str(ROOT/'receivers'))
spec = importlib.util.spec_from_file_location('claude_receiver', RECEIVER)
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)
codex_spec=importlib.util.spec_from_file_location('codex_receiver',ROOT/'receivers/codex_cli.py')
codex_receiver=importlib.util.module_from_spec(codex_spec)
codex_spec.loader.exec_module(codex_receiver)

class CodexRuntimeTests(unittest.TestCase):
    def test_runtime_thread_and_completion_required(self):
        with tempfile.TemporaryDirectory() as folder:
            events=[{'type':'thread.started','thread_id':'fixture-thread'},
                    {'type':'item.completed','item':{'type':'agent_message','text':'实际 fixture 处理结果'}},
                    {'type':'turn.completed','usage':{'output_tokens':8}}]
            def run(command,**kwargs):
                self.assertEqual(command[command.index('--sandbox')+1],'read-only')
                self.assertNotIn('--ephemeral',command)
                self.assertNotIn('--ignore-rules',command)
                self.assertIn('--ignore-user-config',command)
                self.assertNotIn('CAT_CAFE_TEST_SECRET',kwargs['env'])
                return subprocess.CompletedProcess(command,0,'\n'.join(json.dumps(e) for e in events),'')
            with patch.object(codex_receiver,'capability_args',return_value=[]), patch.object(subprocess,'run',side_effect=run) as runner:
                receipt=codex_receiver.process_event({'event_id':'fixture-event'},folder,'codex')
                again=codex_receiver.process_event({'event_id':'fixture-event'},folder,'codex')
            self.assertEqual(receipt,again)
            self.assertEqual(receipt['runtime_session_id'],'fixture-thread')
            self.assertEqual(runner.call_count,1)

    def test_started_thread_without_completion_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            output=json.dumps({'type':'thread.started','thread_id':'unfinished'})
            result=subprocess.CompletedProcess([],0,output,'')
            with patch.object(codex_receiver,'capability_args',return_value=[]),patch.object(subprocess,'run',return_value=result):
                with self.assertRaises(RuntimeError):
                    codex_receiver.process_event({'event_id':'pending'},folder,'codex')

    def test_diagnostics_redact_provider_credentials(self):
        with patch.dict(os.environ,{'OPENAI_API_KEY':'sk-private-example'}):
            text=codex_receiver.diagnostic_line('Error: Bearer sk-private-example')
        self.assertNotIn('sk-private-example',text)

    def test_unexpected_tool_activity_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            events=[{'type':'thread.started','thread_id':'thread'},
                    {'type':'item.completed','item':{'type':'command_execution','command':'example'}},
                    {'type':'item.completed','item':{'type':'agent_message','text':'answer'}},
                    {'type':'turn.completed'}]
            result=subprocess.CompletedProcess([],0,'\n'.join(json.dumps(e) for e in events),'')
            with patch.object(codex_receiver,'capability_args',return_value=[]),patch.object(subprocess,'run',return_value=result):
                with self.assertRaises(RuntimeError):
                    codex_receiver.process_event({'event_id':'no-tools'},folder,'codex')

class RuntimeTests(unittest.TestCase):
    def test_real_command_contract_and_cached_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            event={'event_id':'repo:one','recipient':{'agent_id':'a'},'body':'unique-fixture','url':'https://github.com/example/commons/discussions/1'}
            calls=[]
            def run(argv, **kwargs):
                calls.append(argv)
                self.assertIn('--safe-mode',argv)
                self.assertEqual(argv[argv.index('--tools')+1],'')
                self.assertNotIn('CAT_CAFE_TEST_SECRET',kwargs['env'])
                self.assertEqual(json.loads(kwargs['input'])['body'],'unique-fixture')
                data={'type':'result','subtype':'success','is_error':False,
                      'session_id':argv[argv.index('--session-id')+1],'result':'A fixture analysis'}
                return subprocess.CompletedProcess(argv,0,json.dumps(data),'')
            with patch.dict(os.environ,{'CAT_CAFE_TEST_SECRET':'never-forward'}), patch.object(subprocess,'run',side_effect=run):
                first=receiver.process_event(event,folder,'claude')
                second=receiver.process_event(event,folder,'claude')
            self.assertEqual(len(calls),1)
            self.assertEqual(first,second)
            self.assertTrue(first['processed'])
            path=Path(first['receipt_ref'].replace('file://',''))
            self.assertEqual(json.loads(path.read_text())['result'],'A fixture analysis')

    def test_runtime_error_is_not_a_receipt(self):
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.CompletedProcess([],0,json.dumps({'type':'result','subtype':'error','is_error':True}), '')
            with patch.object(subprocess,'run',return_value=result):
                with self.assertRaises(RuntimeError):
                    receiver.process_event({'event_id':'one'},folder,'claude')
            self.assertFalse(any(not p.name.startswith('attempt-') for p in Path(folder).glob('*.json')))

    def test_run_budget_blocks_another_model_call(self):
        with tempfile.TemporaryDirectory() as folder:
            def success(argv,**kwargs):
                return subprocess.CompletedProcess(argv,0,json.dumps({'type':'result','subtype':'success','is_error':False,
                    'session_id':argv[argv.index('--session-id')+1],'result':'fixture response'}),'')
            with patch.object(subprocess,'run',side_effect=success) as run:
                receiver.process_event({'event_id':'first'},folder,'claude',max_runs=1)
                with self.assertRaises(RuntimeError):
                    receiver.process_event({'event_id':'second'},folder,'claude',max_runs=1)
                self.assertEqual(run.call_count,1)

class BackgroundJourneyTests(unittest.TestCase):
    def test_background_enable_restart_and_disable_preserve_inbox(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            profile=root/'profile'
            snapshot=root/'snapshot.json'
            empty={'nodes':[],'pageInfo':{'hasNextPage':False,'endCursor':None}}
            snapshot.write_text(json.dumps(empty))
            gh=root/'gh'
            gh.write_text('#!'+sys.executable+'\n'+'''import sys,json
p=json.load(sys.stdin)
if 'viewer {' in p['query']:
    data={'viewer':{'login':'alice'},'repository':{'nameWithOwner':'example/commons','hasDiscussionsEnabled':True}}
else:
    data={'repository':{'discussions':json.load(open('''+repr(str(snapshot))+'''))}}
print(json.dumps({'data':data}))
''')
            gh.chmod(0o700)
            claude=root/'claude'
            count=root/'model-runs.txt'
            claude.write_text('#!'+sys.executable+'\n'+'''import sys,json,os
if '--help' in sys.argv:
    print('--safe-mode --tools --output-format --session-id --max-budget-usd');sys.exit(0)
if 'auth' in sys.argv:
    print(json.dumps({'loggedIn':True}));sys.exit(0)
assert 'CAT_CAFE_TEST_SECRET' not in os.environ
event=json.load(sys.stdin)
with open('''+repr(str(count))+''','a') as out: out.write(event['event_id']+'\\n')
print(json.dumps({'type':'result','subtype':'success','is_error':False,
 'session_id':sys.argv[sys.argv.index('--session-id')+1],
 'result':'AUTOMATIC FIXTURE ANALYSIS: '+event['body']}))
''')
            claude.chmod(0o700)
            env=dict(os.environ,PATH=str(root)+os.pathsep+os.environ.get('PATH',''),
                     PYTHONDONTWRITEBYTECODE='1',CAT_CAFE_TEST_SECRET='must-not-forward')
            def call(*args):
                run=subprocess.run([sys.executable,str(CLIENT),*args,'--state-dir',str(profile)],
                                   text=True,capture_output=True,env=env,timeout=20)
                self.assertEqual(run.returncode,0,run.stderr)
                return json.loads(run.stdout)
            def wait_for(predicate, timeout=12):
                end=time.monotonic()+timeout
                latest=None
                while time.monotonic()<end:
                    latest=call('service-status')
                    if predicate(latest): return latest
                    time.sleep(0.1)
                self.fail('Service condition timed out: '+repr(latest))
            def publish(ids):
                now=datetime.datetime.now(datetime.timezone.utc).isoformat().replace('+00:00','Z')
                nodes=[{'id':id,'number':i+1,'title':'A fixture topic','body':'unseen-'+id,
                        'createdAt':now,'author':{'login':'bob'},'url':'https://github.com/example/commons/discussions/'+str(i+1),
                        'category':{'name':'General','slug':'general'},'labels':empty,'comments':empty} for i,id in enumerate(ids)]
                temporary=snapshot.with_suffix('.pending')
                temporary.write_text(json.dumps({'nodes':nodes,'pageInfo':empty['pageInfo']}))
                temporary.replace(snapshot)
            try:
                initialized=call('init','--repo','example/commons','--agent-id','fixture-agent',
                                 '--gh',str(gh),'--runtime','claude-code','--claude',str(claude),
                                 '--enable','--interval','60','--max-events','2')
                self.assertTrue(initialized['service']['enabled'])
                first_status=wait_for(lambda s: bool(s.get('last_poll_success')))
                initial_pid=first_status['monitor_pid']
                publish(['D1'])
                call('restart-monitor')
                first=wait_for(lambda s:s.get('accepted')==1)
                self.assertNotEqual(first['monitor_pid'],initial_pid)
                self.assertGreaterEqual(first['restart_count'],1)
                publish(['D1','D2'])
                call('restart-monitor')
                stopped=wait_for(lambda s:s.get('live')=='stopped' and s.get('accepted')==2)
                self.assertEqual(stopped['reason'],'event_limit_reached')
                self.assertEqual(len(count.read_text().splitlines()),2)
                records=call('inbox','--all')
                self.assertEqual(len(records),2)
                for event in records:
                    self.assertEqual(event['delivery']['state'],'accepted')
                disabled=call('disable')
                self.assertTrue(disabled['inbox_preserved'])
                self.assertEqual(len(call('inbox','--all')),2)
            finally:
                if (profile/'config.json').exists():
                    subprocess.run([sys.executable,str(CLIENT),'disable','--state-dir',str(profile)],
                                   text=True,capture_output=True,env=env,timeout=20)

if __name__ == '__main__':
    unittest.main()
