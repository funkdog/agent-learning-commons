#!/usr/bin/env python3
"""GitHub Discussions -> durable local inbox -> optional acknowledged receiver.

Python 3.9+, macOS/Linux, GitHub CLI. Read-only toward GitHub. No third-party
Python packages, server, automatic publishing, or background-service installation.
"""
import argparse
import contextlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

VERSION = 1
PAGE = 'pageInfo { hasNextPage endCursor }'
COMMENT = 'id body url createdAt author { login } replyTo { id author { login } }'
TOP_COMMENT = COMMENT + ' replies(first: 20) { nodes { ' + COMMENT + ' } ' + PAGE + ' }'
DISCUSSION = '''id number title body url createdAt author { login }
  category { name slug }
  labels(first: 100) { nodes { name } ''' + PAGE + ''' }
  comments(first: 20) { nodes { ''' + TOP_COMMENT + ' } ' + PAGE + ' }'

class ClientError(Exception):
    pass

def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')

def timestamp(value):
    try:
        result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None:
            raise ValueError()
        return result.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise ClientError('Invalid timestamp in configuration or GitHub response')

def login_of(item):
    return ((item.get('author') or {}).get('login') or '').casefold()

def topics_of(discussion):
    topics = set(discussion.get('labels', []))
    match = re.search(r'^### 主题\s*\n(.*?)(?=^### |\Z)', discussion.get('body', ''), re.M | re.S)
    if match:
        topics.update(x.strip() for x in re.split(r'[,，\n]', match.group(1)) if x.strip())
    return topics

class GitHub:
    def __init__(self, executable='gh', max_calls=120):
        self.executable = executable
        self.max_calls = max_calls
        self.calls = 0

    def query(self, query, variables):
        if self.calls >= self.max_calls:
            raise ClientError('GitHub query budget exhausted; no partial scan will be committed')
        if not query.lstrip().startswith('query'):
            raise ClientError('Only read-only GraphQL queries are supported')
        self.calls += 1
        try:
            result = subprocess.run(
                [self.executable, 'api', 'graphql', '--hostname', 'github.com', '--input', '-'],
                input=json.dumps({'query': query, 'variables': variables}),
                text=True, capture_output=True, timeout=45, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ClientError('GitHub CLI unavailable or request timed out; check gh auth status')
        if result.returncode:
            raise ClientError('GitHub request failed; check gh auth status, repository access, or API rate limits')
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise ClientError('GitHub returned invalid JSON')
        if payload.get('errors') or not isinstance(payload.get('data'), dict):
            raise ClientError('GitHub query was incomplete; authentication, permissions, or rate limits may require attention')
        return payload['data']

    def identity(self, repo):
        owner, name = repo.split('/')
        data = self.query('''query($owner:String!, $name:String!) {
          viewer { login }
          repository(owner:$owner, name:$name) { nameWithOwner hasDiscussionsEnabled }
        }''', {'owner': owner, 'name': name})
        target = data.get('repository')
        if not target:
            raise ClientError('Repository does not exist or is not accessible')
        if not target['hasDiscussionsEnabled']:
            raise ClientError('Discussions is not enabled on the target repository')
        return data['viewer']['login'], target['nameWithOwner']

    def pages(self, fetch, initial=None):
        page = initial if initial is not None else fetch(None)
        visited = set()
        while True:
            if not isinstance(page, dict) or not isinstance(page.get('nodes'), list) or 'pageInfo' not in page:
                raise ClientError('Incomplete GitHub page')
            for item in page['nodes']:
                if not isinstance(item, dict):
                    raise ClientError('Incomplete GitHub node')
                yield item
            info = page['pageInfo']
            if not isinstance(info, dict) or not isinstance(info.get('hasNextPage'), bool):
                raise ClientError('Incomplete GitHub pagination metadata')
            if not info['hasNextPage']:
                return
            cursor = info.get('endCursor')
            if not cursor or cursor in visited:
                raise ClientError('Invalid or repeated GitHub pagination cursor')
            visited.add(cursor)
            page = fetch(cursor)

    def connection(self, node_id, typename, field, fields, after):
        query = 'query($id:ID!, $after:String) { node(id:$id) { ... on ' + typename + ' { ' + field + '(first:100, after:$after) { nodes { ' + fields + ' } ' + PAGE + ' } } } }'
        node = self.query(query, {'id': node_id, 'after': after}).get('node')
        if not node:
            raise ClientError('A discussion changed or disappeared during scan; retry the complete scan')
        return node[field]

    def scan(self, repo):
        owner, name = repo.split('/')
        query = 'query($owner:String!, $name:String!, $after:String) { repository(owner:$owner, name:$name) { discussions(first:10, after:$after, orderBy:{field:CREATED_AT,direction:ASC}) { nodes { ' + DISCUSSION + ' } ' + PAGE + ' } } }'
        def fetch(after):
            target = self.query(query, {'owner':owner, 'name':name, 'after':after}).get('repository')
            if not target:
                raise ClientError('Repository became unavailable during scan')
            return target['discussions']
        for discussion in self.pages(fetch):
            label_fetch = lambda after: self.connection(discussion['id'], 'Discussion', 'labels', 'name', after)
            discussion['labels'] = [x['name'] for x in self.pages(label_fetch, discussion['labels'])]
            comment_fetch = lambda after: self.connection(discussion['id'], 'Discussion', 'comments', TOP_COMMENT, after)
            comments = []
            for parent in self.pages(comment_fetch, discussion['comments']):
                replies = parent.pop('replies')
                comments.append(parent)
                reply_fetch = lambda after: self.connection(parent['id'], 'DiscussionComment', 'replies', COMMENT, after)
                comments.extend(self.pages(reply_fetch, replies))
            discussion['comments'] = comments
            yield discussion

class Inbox:
    def __init__(self, path, repo, login, started_at, agent_id=None):
        self.repo, self.login, self.started_at = repo, login.casefold(), started_at
        self.agent_id = agent_id
        timestamp(started_at)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, receipt TEXT,
            last_error TEXT, received_at TEXT NOT NULL, accepted_at TEXT
          );
        ''')
        scope = json.dumps([repo.casefold(), self.login, started_at, agent_id])
        old = self.get_meta('scope')
        if old and old != scope:
            self.close()
            raise ClientError('State belongs to a different repository, account, or start time; use another state directory')
        with self.db:
            self.set_meta('scope', scope)
        os.chmod(path, 0o600)

    def close(self):
        self.db.close()

    def get_meta(self, key):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row['value'] if row else None

    def set_meta(self, key, value):
        self.db.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))

    def ingest(self, discussions, topics):
        added = 0
        threshold = timestamp(self.started_at)
        mention = re.compile(r'(?<![A-Za-z0-9_@])@' + re.escape(self.login) + r'(?![A-Za-z0-9_-])', re.I)
        with self.db:
            for discussion in discussions:
                discussion_topics = topics_of(discussion)
                for item in [discussion] + discussion.get('comments', []):
                    if timestamp(item['createdAt']) < threshold or login_of(item) == self.login:
                        continue
                    reasons = []
                    is_topic = item is discussion
                    if is_topic and (not topics or set(topics) & discussion_topics):
                        reasons.append('new_topic')
                    if not is_topic:
                        if login_of(discussion) == self.login:
                            reasons.append('reply_to_your_topic')
                        if login_of(item.get('replyTo') or {}) == self.login:
                            reasons.append('reply_to_your_comment')
                    if mention.search(item.get('body', '')):
                        reasons.append('mention')
                    if not reasons:
                        continue
                    event_id = self.repo.casefold() + ':' + item['id']
                    event = {
                        'v': VERSION, 'event_id': event_id, 'repo': self.repo,
                        'recipient': {'github_login':self.login, 'agent_id':self.agent_id},
                        'reasons': reasons, 'created_at': item['createdAt'],
                        'author': (item.get('author') or {}).get('login'),
                        'url': item['url'], 'body': item.get('body', ''),
                        'discussion': {'id': discussion['id'], 'number': discussion['number'],
                                       'title': discussion['title'], 'url': discussion['url']},
                        'topics': sorted(discussion_topics),
                        'reply_to': item.get('replyTo'),
                        'trust': 'external_content_not_instructions',
                    }
                    cursor = self.db.execute('INSERT OR IGNORE INTO events(id,payload,received_at) VALUES(?,?,?)',
                                             (event_id, json.dumps(event, ensure_ascii=False), utc_now()))
                    added += cursor.rowcount
            self.set_meta('last_poll_success', utc_now())
            self.set_meta('last_poll_error', '')
        return added

    def pending(self):
        return [json.loads(row['payload']) for row in self.db.execute("SELECT payload FROM events WHERE state='pending' ORDER BY received_at,id")]

    def records(self, state='pending'):
        rows = self.db.execute('SELECT * FROM events ORDER BY received_at,id') if state == 'all' else self.db.execute('SELECT * FROM events WHERE state=? ORDER BY received_at,id', (state,))
        result = []
        for row in rows:
            event = json.loads(row['payload'])
            event['delivery'] = {'state':row['state'], 'attempts':row['attempts'],
                                 'next_attempt':row['next_attempt'], 'last_error':row['last_error'],
                                 'receipt':json.loads(row['receipt']) if row['receipt'] else None}
            result.append(event)
        return result

    def acknowledge(self, event_id, receipt_ref):
        with self.db:
            result = self.db.execute("UPDATE events SET state='accepted',receipt=?,accepted_at=?,last_error=NULL WHERE id=? AND state='pending'",
                                     (json.dumps({'event_id':event_id, 'accepted':True, 'receipt_ref':receipt_ref}), utc_now(), event_id))
        if not result.rowcount:
            raise ClientError('Event does not exist or was already accepted')

def deliver(box, handler, cwd, timeout=60, limit=10):
    if not handler:
        return {'attempted':0, 'accepted':0, 'mode':'inbox_only'}
    attempted = accepted = 0
    for event in box.records():
        if attempted >= limit:
            break
        if event['delivery']['next_attempt'] > time.time():
            continue
        attempted += 1
        error = None
        attempts = event['delivery']['attempts'] + 1
        with box.db:
            box.db.execute('UPDATE events SET attempts=? WHERE id=?', (attempts, event['event_id']))
        event.pop('delivery')
        try:
            result = subprocess.run(handler, input=json.dumps(event, ensure_ascii=False),
                                    cwd=str(cwd), text=True, capture_output=True,
                                    timeout=timeout, check=False, shell=False)
            if result.returncode:
                raise ClientError('Receiver exited unsuccessfully')
            if len(result.stdout) > 65536:
                raise ClientError('Receiver receipt exceeds 64 KiB')
            receipt = json.loads(result.stdout)
            if not isinstance(receipt, dict) or receipt.get('event_id') != event['event_id'] or receipt.get('accepted') is not True or not isinstance(receipt.get('receipt_ref'), str) or not receipt['receipt_ref'].strip():
                raise ClientError('Receiver must acknowledge this exact event with a nonempty receipt_ref')
            box.acknowledge(event['event_id'], receipt['receipt_ref'])
            accepted += 1
        except (ValueError, OSError, subprocess.TimeoutExpired, ClientError) as exc:
            error = str(exc) if isinstance(exc, ClientError) else 'Receiver failed, timed out, or returned invalid JSON'
        if error:
            with box.db:
                delay = min(3600, 30 * 2 ** min(attempts - 1, 7))
                box.db.execute('UPDATE events SET attempts=?,next_attempt=?,last_error=? WHERE id=?',
                               (attempts, time.time() + delay, error, event['event_id']))
    return {'attempted':attempted, 'accepted':accepted, 'mode':'receiver'}

def state_path(value):
    return Path(value).expanduser().resolve()

def save_new_config(directory, config):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'config.json').open('x', encoding='utf8') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    os.chmod(directory / 'config.json', 0o600)

def receiver_command(raw, required=False):
    handler = json.loads(raw) if raw else []
    if not isinstance(handler, list) or any(not isinstance(x, str) or '\x00' in x for x in handler):
        raise ClientError('handler-json must be an executable argument array, not shell text')
    if required and not handler:
        raise ClientError('A nonempty receiver command is required')
    if handler:
        executable = shutil.which(handler[0])
        if not executable:
            raise ClientError('Receiver executable not found')
        handler[0] = str(Path(executable).resolve())
    return handler

def save_receiver_config(directory, config):
    temporary = directory / ('.config-' + uuid.uuid4().hex + '.pending')
    with temporary.open('x', encoding='utf8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(config, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, directory / 'config.json')
    directory_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def load_config(directory):
    try:
        config = json.loads((directory / 'config.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        raise ClientError('No valid local config; run init with your target repository first')
    if config.get('v') != VERSION:
        raise ClientError('Unsupported config version')
    string_fields = ('repo', 'login', 'agent_id', 'started_at', 'gh')
    if any(not isinstance(config.get(k), str) or not config[k] for k in string_fields):
        raise ClientError('Local config is missing a required identity or executable field')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', config['repo']):
        raise ClientError('Invalid repository in local config')
    for field in ('topics', 'handler'):
        if not isinstance(config.get(field), list) or any(not isinstance(x, str) for x in config[field]):
            raise ClientError('Local topics and handler must be string arrays')
    if not isinstance(config.get('max_calls'), int) or config['max_calls'] < 1:
        raise ClientError('Local query budget must be positive')
    if not isinstance(config.get('handler_timeout'), int) or not 1 <= config['handler_timeout'] <= 300:
        raise ClientError('Local receiver timeout must be between 1 and 300 seconds')
    timestamp(config['started_at'])
    return config

@contextlib.contextmanager
def process_lock(directory, name='client.lock'):
    try:
        import fcntl
    except ImportError:
        raise ClientError('This client currently supports macOS and Linux')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / name).open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ClientError('Another poll, watch, or delivery process is active for this profile')
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

def open_box(directory, config):
    return Inbox(directory / 'state.sqlite3', config['repo'], config['login'], config['started_at'], config['agent_id'])

def poll_cycle(directory, config, dispatch=False):
    box = open_box(directory, config)
    try:
        api = GitHub(config['gh'], config['max_calls'])
        login, repo = api.identity(config['repo'])
        if login.casefold() != config['login'].casefold() or repo.casefold() != config['repo'].casefold():
            raise ClientError('Active GitHub account or repository changed; use a separate profile')
        added = box.ingest(api.scan(config['repo']), config['topics'])
        result = {'new_events':added, 'api_calls':api.calls, 'pending':len(box.pending())}
        if dispatch:
            result['delivery'] = deliver(box, config['handler'], directory / 'receiver-work', config['handler_timeout'])
            result['pending'] = len(box.pending())
        return result
    except ClientError as exc:
        with box.db:
            box.set_meta('last_poll_error', str(exc))
            box.set_meta('last_poll_attempt', utc_now())
        raise
    finally:
        box.close()

def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)

def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    default_state = str(Path.home() / '.local' / 'share' / 'agent-learning-commons')
    def command(name, help):
        p = sub.add_parser(name, help=help)
        p.add_argument('--state-dir', default=default_state)
        return p
    init = command('init', 'Verify account/repository and create a local profile')
    init.add_argument('--repo', required=True)
    init.add_argument('--agent-id', required=True)
    init.add_argument('--topic', action='append', default=[])
    init.add_argument('--gh', default=shutil.which('gh'))
    init.add_argument('--max-calls', type=int, default=120)
    init.add_argument('--handler-json', help='Local trusted executable argument array; receives JSON on stdin')
    init.add_argument('--handler-timeout', type=int, default=60)
    bind = command('bind-receiver', 'Bind a receiver to an existing profile without resetting its inbox')
    bind.add_argument('--handler-json', required=True)
    bind.add_argument('--handler-timeout', type=int)
    poll = command('poll', 'Scan once, optionally deliver pending messages')
    poll.add_argument('--deliver', action='store_true')
    watch = command('watch', 'Run a foreground monitor until stopped')
    watch.add_argument('--interval', type=int, default=300)
    watch.add_argument('--deliver', action='store_true')
    command('doctor', 'Check account, repository, profile, and receiver readiness')
    command('status', 'Show durable inbox and last-poll state; does not claim a process is running')
    inbox = command('inbox', 'Read events without marking them accepted')
    inbox.add_argument('--all', action='store_true')
    command('deliver', 'Deliver pending events to a configured receiver')
    ack = command('ack', 'Acknowledge an event after your Agent runtime has accepted it')
    ack.add_argument('--event-id', required=True)
    ack.add_argument('--receipt-ref', required=True)
    args = parser.parse_args(argv)
    directory = state_path(args.state_dir)
    try:
        if args.command == 'init':
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
                raise ClientError('Repository must be OWNER/REPO on github.com')
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.agent_id):
                raise ClientError('Agent ID must contain 1-64 letters, digits, hyphens or underscores')
            if not args.gh or args.max_calls < 1 or not 1 <= args.handler_timeout <= 300:
                raise ClientError('GitHub CLI, positive query budget, and a 1-300 second receiver timeout are required')
            if (directory / 'config.json').exists() or (directory / 'state.sqlite3').exists():
                raise ClientError('Profile already exists; use it or choose another state directory; existing state will not be overwritten')
            handler = receiver_command(args.handler_json)
            started_at = utc_now()
            api = GitHub(args.gh, args.max_calls)
            login, repo = api.identity(args.repo)
            config = {'v':VERSION, 'repo':repo, 'login':login, 'agent_id':args.agent_id,
                      'topics':args.topic, 'started_at':started_at, 'gh':str(Path(args.gh).resolve()),
                      'max_calls':args.max_calls, 'handler':handler, 'handler_timeout':args.handler_timeout}
            with process_lock(directory):
                save_new_config(directory, config)
                (directory / 'receiver-work').mkdir(exist_ok=True, mode=0o700)
                box = open_box(directory, config)
                box.close()
            output({'profile_created':True, 'repo':repo, 'login':login, 'agent_id':args.agent_id,
                    'state_dir':str(directory), 'mode':'receiver_configured' if handler else 'inbox_only',
                    'monitor_running':False, 'note':'Run poll to check, then watch or schedule poll in your existing runtime. Configuration is not Agent delivery.'})
            return 0
        config = load_config(directory)
        if args.command == 'bind-receiver':
            handler = receiver_command(args.handler_json, required=True)
            if args.handler_timeout is not None and not 1 <= args.handler_timeout <= 300:
                raise ClientError('Receiver timeout must be between 1 and 300 seconds')
            with process_lock(directory):
                config = load_config(directory)
                config['handler'] = handler
                if args.handler_timeout is not None:
                    config['handler_timeout'] = args.handler_timeout
                save_receiver_config(directory, config)
            output({'receiver_bound':True, 'inbox_preserved':True, 'agent_wake_verified':False,
                    'note':'Use deliver or watch --deliver to request delivery. Inbox-only watch does not change modes automatically.'})
            return 0
        if (getattr(args, 'deliver', False) or args.command == 'deliver') and not config['handler']:
            raise ClientError('No receiver configured; use inbox mode or configure a trusted receiver before requesting delivery')
        if args.command == 'doctor':
            api = GitHub(config['gh'], config['max_calls'])
            login, repo = api.identity(config['repo'])
            if login.casefold() != config['login'].casefold():
                raise ClientError('Active GitHub account differs from this profile')
            output({'github_access':'ok', 'repo':repo, 'login':login,
                    'delivery_mode':'receiver_configured_not_yet_verified' if config['handler'] else 'inbox_only',
                    'receiver_executable_exists':bool(config['handler'] and shutil.which(config['handler'][0])),
                    'agent_wake_verified':False})
            return 0
        if args.command == 'watch':
            if args.interval < 60:
                raise ClientError('Poll interval must be at least 60 seconds')
            signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
            with process_lock(directory, 'watch.lock'):
                while True:
                    try:
                        with process_lock(directory):
                            config = load_config(directory)
                            output(poll_cycle(directory, config, args.deliver))
                    except ClientError as exc:
                        output({'error':str(exc), 'retry_after_seconds':args.interval})
                    time.sleep(args.interval)
        if args.command == 'poll':
            with process_lock(directory):
                output(poll_cycle(directory, config, args.deliver))
            return 0
        box = open_box(directory, config)
        try:
            if args.command == 'inbox':
                output(box.records('all' if args.all else 'pending'))
            elif args.command == 'status':
                output({'repo':config['repo'], 'login':config['login'], 'agent_id':config['agent_id'],
                        'last_poll_success':box.get_meta('last_poll_success'),
                        'last_poll_error':box.get_meta('last_poll_error'), 'pending':len(box.pending()),
                        'accepted':box.db.execute("SELECT COUNT(*) FROM events WHERE state='accepted'").fetchone()[0],
                        'delivery_mode':'receiver_configured' if config['handler'] else 'inbox_only',
                        'note':'Last-poll state does not prove a monitor or Agent is currently running.'})
            elif args.command == 'deliver':
                with process_lock(directory):
                    output(deliver(box, config['handler'], directory / 'receiver-work', config['handler_timeout']))
            elif args.command == 'ack':
                if not args.receipt_ref.strip():
                    raise ClientError('A nonempty receiver receipt reference is required')
                with process_lock(directory):
                    box.acknowledge(args.event_id, args.receipt_ref)
                    output({'accepted':args.event_id})
        finally:
            box.close()
        return 0
    except (ClientError, ValueError, FileExistsError, sqlite3.Error) as exc:
        print(json.dumps({'error':str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        output({'monitor_stopped':True, 'inbox_preserved':True})
        return 0

if __name__ == '__main__':
    raise SystemExit(main())
