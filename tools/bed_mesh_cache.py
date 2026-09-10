"""Local content-addressed mesh audit/result cache. Never executable cache data.

Entries are immutable, exclusively created, hash-checked records. This detects
accidental corruption, not malicious rewriting by someone controlling the cache.
"""

import hashlib
import json
from pathlib import Path
import shutil
import stat

from package_portable import sha256


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def key_for(inputs):
    return hashlib.sha256(canonical(inputs)).hexdigest()


def regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Cache file must be a regular, non-reparse file")
    return info


class AssetCache:
    def __init__(self, root, reject_path):
        self.root = Path(root)
        self.reject_path = reject_path
        reject_path(self.root)
        if self.root.exists() and not self.root.is_dir():
            raise ValueError("Asset cache root must be a directory")
        self.hits = self.misses = self.invalid = 0

    def read(self, inputs, maximum_payload_bytes, validate_body=lambda body, payload: True):
        entry = self.root / key_for(inputs)
        report = entry / "record.json"
        try:
            self.reject_path(report)
            info = regular(report)
            if info.st_size > 8 * 1024**2:
                raise ValueError("Oversized cache report")
            value = json.loads(report.read_bytes())
            if not isinstance(value, dict) or not isinstance(value.get("body"), dict):
                raise ValueError("Malformed cache record")
            if value["inputs"] != inputs or value["body_sha256"] != key_for(value["body"]):
                raise ValueError("Cache identity/checksum mismatch")
            payload = None
            if value["body"].get("selected_payload"):
                details = value["body"]["selected_payload"]
                payload = entry / "candidate.stl"
                self.reject_path(payload)
                if (not 0 < details["bytes"] <= maximum_payload_bytes or regular(payload).st_size != details["bytes"]
                        or sha256(payload) != details["sha256"]):
                    raise ValueError("Cache payload checksum/size mismatch")
            if not validate_body(value["body"], payload):
                raise ValueError("Cache result is inconsistent with its recorded source/policy")
            self.hits += 1
            return value["body"], payload
        except FileNotFoundError:
            self.misses += 1
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            self.invalid += 1
            self.misses += 1
        return None

    def write(self, inputs, body, payload=None):
        # Pre-serialize before touching cache directories. A completed report
        # is the commit marker; incomplete/corrupt entries are never overwritten.
        record = canonical({"inputs": inputs, "body": body, "body_sha256": key_for(body)})
        self.reject_path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        entry = self.root / key_for(inputs)
        try:
            entry.mkdir()
        except FileExistsError:
            return
        self.reject_path(entry)
        if payload is not None:
            destination = entry / "candidate.stl"
            with Path(payload).open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst, length=1024*1024)
            if sha256(destination) != body["selected_payload"]["sha256"]:
                raise RuntimeError("Candidate changed while caching")
        with (entry / "record.json").open("xb") as stream:
            stream.write(record)
