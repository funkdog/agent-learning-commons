"""Small shared persistence primitives for local runtime receivers."""
import json
import os
import uuid

def atomic_json(path, value):
    temporary = path.with_name('.' + uuid.uuid4().hex + '.pending')
    with temporary.open('x', encoding='utf8') as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
