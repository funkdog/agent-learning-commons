#!/usr/bin/env python3
"""Opt-in per-user background supervisor; no OS startup files are modified."""
import argparse
import contextlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid

import community_client as client

INFO = 'service-state.json'

def read_info(directory):
    try:
        data = json.loads((directory / INFO).read_text())
        return data if isinstance(data,dict) else {}
    except (OSError,ValueError):
        return {}

def save_info(directory, data):
    temporary = directory / ('.service-' + uuid.uuid4().hex + '.pending')
    with temporary.open('x',encoding='utf8') as stream:
        json.dump(data,stream,ensure_ascii=False,indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,directory / INFO)

def counters(directory):
    try:
        with contextlib.closing(sqlite3.connect((directory/'state.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=0.2)) as db:
            pending = db.execute("SELECT COUNT(*) FROM events WHERE state='pending'").fetchone()[0]
            accepted = db.execute("SELECT COUNT(*) FROM events WHERE state='accepted'").fetchone()[0]
            failed = db.execute("SELECT COUNT(*) FROM events WHERE state='pending' AND last_error IS NOT NULL").fetchone()[0]
            meta = dict(db.execute('SELECT key,value FROM meta'))
            return {'pending':pending,'accepted':accepted,'failed_delivery':failed,
                    'last_poll_success':meta.get('last_poll_success'),
                    'last_poll_error':meta.get('last_poll_error')}
    except sqlite3.Error:
        return {'database':'temporarily_unavailable'}

def request(directory, command='status'):
    info = read_info(directory)
    if not info.get('socket') or not info.get('nonce'):
        return {'enabled':False,'live':'stopped','reason':'not_enabled'}
    try:
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:
            connection.settimeout(10)
            connection.connect(info['socket'])
            connection.sendall(json.dumps({'command':command,'nonce':info['nonce']}).encode()+b'\n')
            received = b''
            while b'\n' not in received and len(received) < 16384:
                part = connection.recv(4096)
                if not part:
                    break
                received += part
            response = json.loads(received)
            if response.get('nonce') != info['nonce']:
                raise ValueError('Control response identity mismatch')
            return response
    except (OSError,ValueError,KeyError):
        return {'enabled':bool(info.get('enabled')),'live':'stopped',
                'reason':info.get('stop_reason') or 'control_unavailable',
                'restart_count':info.get('restart_count',0),**counters(directory)}

def enable(directory, interval=300, inbox_only=False, max_events=0):
    if interval < 60 or max_events < 0:
        raise client.ClientError('Interval must be at least 60 seconds; max-events must be nonnegative')
    config = client.load_config(directory)
    if not inbox_only and not config['handler']:
        raise client.ClientError('Bind a receiver before enable, or explicitly choose --inbox-only')
    api = client.GitHub(config['gh'],config['max_calls'])
    login, repo = api.identity(config['repo'])
    if login.casefold() != config['login'].casefold() or repo.casefold() != config['repo'].casefold():
        raise client.ClientError('The active GitHub identity differs from this profile')
    running = request(directory)
    if running.get('live') == 'running':
        if running.get('interval') != interval or running.get('inbox_only') != inbox_only or running.get('max_events') != max_events:
            raise client.ClientError('Service is running with different options; disable it before changing launch options')
        return {**running,'already_running':True}
    old = read_info(directory)
    accepted = counters(directory).get('accepted',0)
    ceiling = accepted + max_events if max_events else 0
    if old.get('enabled') and old.get('max_events') == max_events:
        ceiling = old.get('accepted_ceiling',ceiling)
    log = (directory/'service.log').open('a',encoding='utf8')
    command = [sys.executable,str(Path(__file__).resolve()),'_run','--state-dir',str(directory),
               '--interval',str(interval),'--max-events',str(max_events),'--accepted-ceiling',str(ceiling)]
    if inbox_only:
        command.append('--inbox-only')
    with log:
        child = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                 start_new_session=True,close_fds=True)
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        status = request(directory)
        if status.get('live') == 'running':
            return {**status,'agent_processed':False,
                    'note':'Background listener started; an actual event is still required to verify Agent processing.'}
        if child.poll() is not None:
            raise client.ClientError('Supervisor did not start; inspect this profile service.log')
        time.sleep(0.1)
    raise client.ClientError('Service startup is unconfirmed; inspect service-status and service.log')

def stop_owned(child):
    if child is None:
        return
    # Each monitor is created in a new session, so this group is exclusively
    # the owned monitor and its receivers, never the launcher or its parent.
    try:
        os.killpg(child.pid,signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        child.poll()
        try:
            os.killpg(child.pid,0)
        except ProcessLookupError:
            child.wait()
            return
        time.sleep(0.05)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(child.pid,signal.SIGKILL)
    child.wait(timeout=5)

def run_supervisor(directory, interval, inbox_only, max_events, accepted_ceiling):
    # The only process we terminate is the Popen child created here.
    with client.process_lock(directory,'service.lock'):
        nonce = uuid.uuid4().hex
        address = '/tmp/alc-' + str(os.getuid()) + '-' + nonce[:16] + '.sock'
        info = {'v':1,'enabled':True,'supervisor_pid':os.getpid(),'nonce':nonce,
                'socket':address,'interval':interval,'inbox_only':inbox_only,
                'max_events':max_events,'accepted_ceiling':accepted_ceiling,
                'restart_count':0,'started_at':client.utc_now()}
        child = None
        stopping = False
        stop_reason = 'disabled'
        def handle_stop(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM,handle_stop)
        signal.signal(signal.SIGINT,handle_stop)
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as server, (directory/'monitor.log').open('a',encoding='utf8') as log:
            server.bind(address)
            os.chmod(address,0o600)
            server.listen(4)
            server.settimeout(0.25)
            command = [sys.executable,str(Path(__file__).with_name('community_client.py')),
                       'watch','--state-dir',str(directory),'--interval',str(interval)]
            if not inbox_only:
                command.append('--deliver')
            restart_at = 0
            try:
                while not stopping:
                    snapshot = counters(directory)
                    if accepted_ceiling and snapshot.get('accepted',0) >= accepted_ceiling:
                        stop_reason = 'event_limit_reached'
                        break
                    if child is not None and child.poll() is not None:
                        info['last_monitor_exit'] = child.returncode
                        stop_owned(child)
                        info['restart_count'] += 1
                        child = None
                        restart_at = time.monotonic()+min(30,2 ** min(info['restart_count']-1,5))
                        save_info(directory,info)
                    if child is None and time.monotonic() >= restart_at:
                        child = subprocess.Popen(command,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,
                                                 close_fds=True,start_new_session=True)
                        info['monitor_pid'] = child.pid
                        save_info(directory,info)
                    try:
                        connection,_ = server.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1)
                        try:
                            raw = b''
                            while b'\n' not in raw and len(raw) < 4096:
                                part = connection.recv(4096)
                                if not part:
                                    break
                                raw += part
                            message = json.loads(raw)
                            if message.get('nonce') != nonce:
                                response = {'error':'wrong_service_identity','nonce':nonce}
                            else:
                                method = message.get('command')
                                if method == 'disable':
                                    stopping = True
                                elif method == 'restart-monitor':
                                    stop_owned(child)
                                elif method != 'status':
                                    raise ValueError('Unknown service command')
                                response = {**info,'enabled':not stopping,'live':'stopping' if stopping else 'running',
                                            'monitor_live':bool(child is not None and child.poll() is None),
                                            **counters(directory)}
                            connection.sendall(json.dumps(response,ensure_ascii=False).encode()+b'\n')
                        except (OSError,ValueError,TypeError):
                            continue
            finally:
                stop_owned(child)
                info.update({'enabled':False,'stopped_at':client.utc_now(),'stop_reason':stop_reason})
                save_info(directory,info)
                # Ephemeral owned IPC socket only; all profiles, events and results stay.
                os.unlink(address)

def disable(directory):
    status = request(directory,'disable')
    if status.get('live') == 'stopped':
        info = read_info(directory)
        if info:
            info.update({'enabled':False,'stop_reason':'disabled','stopped_at':client.utc_now()})
            save_info(directory,info)
        return {**status,'enabled':False,'inbox_preserved':True}
    deadline = time.monotonic()+10
    while time.monotonic() < deadline:
        status = request(directory)
        if status.get('live') == 'stopped':
            return {**status,'enabled':False,'inbox_preserved':True}
        time.sleep(0.1)
    raise client.ClientError('Stop requested but not confirmed; inspect service-status')

def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['enable','disable','service-status','restart-monitor','_run'])
    parser.add_argument('--state-dir',required=True)
    parser.add_argument('--interval',type=int,default=300)
    parser.add_argument('--inbox-only',action='store_true')
    parser.add_argument('--max-events',type=int,default=0)
    parser.add_argument('--accepted-ceiling',type=int,default=0)
    args = parser.parse_args(argv)
    directory = client.state_path(args.state_dir)
    try:
        if args.command == '_run':
            run_supervisor(directory,args.interval,args.inbox_only,args.max_events,args.accepted_ceiling)
            return 0
        if args.command == 'enable':
            result = enable(directory,args.interval,args.inbox_only,args.max_events)
        elif args.command == 'disable':
            result = disable(directory)
        else:
            result = request(directory,'status' if args.command == 'service-status' else args.command)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        return 0
    except (client.ClientError,OSError,ValueError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False),file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
