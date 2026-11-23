"""Read immutable local evidence and compute its content identity."""
import hashlib
import json
from pathlib import Path


def read_source(file):
    raw = Path(file).read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    payload = json.loads(raw)
    if not isinstance(payload, list) or not payload:
        raise ValueError('source must contain a nonempty event list')
    return raw, sha, payload
