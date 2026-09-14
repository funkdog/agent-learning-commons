#!/usr/bin/env python3
"""Optional durable file receiver. This stores events; it does NOT wake an Agent."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', required=True)
    args = parser.parse_args()
    event = json.load(sys.stdin)
    if not isinstance(event, dict) or not isinstance(event.get('event_id'), str) or not event['event_id']:
        raise ValueError('A nonempty event_id is required')
    directory = Path(args.directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identity = json.dumps([event['event_id'], event.get('recipient')], sort_keys=True)
    name = hashlib.sha256(identity.encode()).hexdigest() + '.json'
    target = directory / name
    if target.exists():
        existing = json.loads(target.read_text(encoding='utf8'))
        if existing.get('event_id') != event['event_id']:
            raise ValueError('Existing receipt file has a different event identity')
    else:
        temporary = directory / ('.' + uuid.uuid4().hex + '.pending')
        with temporary.open('x', encoding='utf8') as stream:
            os.chmod(temporary, 0o600)
            json.dump(event, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    print(json.dumps({'event_id':event['event_id'], 'accepted':True,
                      'receipt_ref':target.as_uri(), 'receiver_kind':'file_inbox',
                      'agent_woken':False}, ensure_ascii=False))

if __name__ == '__main__':
    main()
