"""Files you paste or drop into a reply, kept privately beside the inbox.

The agent gets each file's full path in the reply text, so Claude Code or Codex can open
the image or document itself, whatever route delivers the reply. Files live in the inbox's
own folder (mode 0700) and never leave this machine through HQ.
"""
import json
import os
from pathlib import Path
import re
import time
import uuid

LIMIT = 25 * 1024 * 1024
TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
         "image/heic": ".heic", "application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md",
         "text/csv": ".csv", "application/json": ".json"}
# What each type's file must begin with, so a mislabelled upload is refused.
MAGIC = {"image/png": (b"\x89PNG\r\n\x1a\n",), "image/jpeg": (b"\xff\xd8\xff",), "image/gif": (b"GIF87a", b"GIF89a"),
         "application/pdf": (b"%PDF-",)}
ID = re.compile(r"^[0-9a-f]{32}$")
MOST = 10


def folder(base):
    path = Path(base) / "attachments"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def clean_name(name, extension):
    name = Path(str(name or "")).name
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "", name).strip()[:120]
    return name or f"pasted{extension}"


def check(data, kind):
    if kind not in TYPES:
        raise ValueError("HQ takes images (PNG, JPEG, GIF, WebP, HEIC), PDFs and plain text files")
    if not data:
        raise ValueError("The file is empty")
    if len(data) > LIMIT:
        raise ValueError("Files can be at most 25 MB")
    if kind in MAGIC and not data.startswith(MAGIC[kind]):
        raise ValueError(f"That file is not really {kind}")
    if kind == "image/webp" and not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
        raise ValueError("That file is not really image/webp")
    if kind == "image/heic" and data[4:8] != b"ftyp":
        raise ValueError("That file is not really image/heic")
    if kind.startswith("text/") or kind == "application/json":
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Text files must be UTF-8") from None


def save(base, data, kind, name=""):
    kind = (kind or "").split(";")[0].strip().lower()
    check(data, kind)
    extension = TYPES[kind]
    id = uuid.uuid4().hex
    root = folder(base)
    path = root / f"{id}{extension}"
    meta = {"id": id, "name": clean_name(name, extension), "type": kind, "size": len(data), "path": str(path)}
    for target, content in ((path, data), (root / f"{id}.json", json.dumps(meta).encode())):
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(content)
    return meta


def find(base, id):
    if not isinstance(id, str) or not ID.match(id):
        raise ValueError("Unknown attachment")
    try:
        meta = json.loads((folder(base) / f"{id}.json").read_text())
    except (OSError, ValueError):
        raise ValueError("Unknown attachment") from None
    if not Path(meta["path"]).is_file():
        raise ValueError("That attachment's file is gone")
    return meta


def resolve(base, ids):
    """The attachments a reply names, checked, in order and without repeats."""
    if ids is None:
        return []
    if not isinstance(ids, list) or len(ids) > MOST:
        raise ValueError(f"Attach at most {MOST} files")
    seen, found = set(), []
    for id in ids:
        if id not in seen:
            seen.add(id)
            found.append(find(base, id))
    return found


def describe(found):
    """The lines an agent reads: where each file is, so it can open it."""
    if not found:
        return ""
    size = lambda n: f"{n / 1024 / 1024:.1f} MB" if n >= 1024 * 1024 else f"{max(1, round(n / 1024))} KB"
    lines = [f"- {a['path']} ({a['name']}, {a['type']}, {size(a['size'])})" for a in found]
    return "Attached by the user. Open each file to see it:\n" + "\n".join(lines)


def sweep(base, keep, older_than=86400, now=None):
    """Remove files that were uploaded but never sent, once they are a day old."""
    now, root, removed = now or time.time(), folder(base), 0
    for meta_path in root.glob("*.json"):
        id = meta_path.stem
        if id in keep or now - meta_path.stat().st_mtime < older_than:
            continue
        try:
            Path(json.loads(meta_path.read_text())["path"]).unlink(missing_ok=True)
        except (OSError, ValueError, KeyError):
            pass
        meta_path.unlink(missing_ok=True)
        removed += 1
    return removed
