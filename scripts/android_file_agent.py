#!/usr/bin/env python3
"""Android / Termux file agent for Mongoose.

An alternative to syncing files with Syncthing: instead of copying file BYTES to
the Mac, this pushes only the phone's file INDEX (paths, names, and safe text
snippets) to Mongoose. Lighter, and nothing leaves the phone except the index.

Run on the phone in Termux (after `termux-setup-storage`):

  python3 android_file_agent.py \
    --server http://MAC_MINI_IP:8765 \
    --token  YOUR_MONGOOSE_API_TOKEN \
    --root   /sdcard \
    --device phone

It POSTs batches to /device-files/ingest. It does NOT upload full file bytes.
Binary files are indexed by name/path only. Android/data and Android/obb are
skipped (unreachable/private). This is stdlib-only so it runs in a bare Termux.

Adapted from a parallel proposal's agent; it writes into Mongoose's shared file
index (tagged by device) so one search spans phone + Mac.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.request
from pathlib import Path

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".css", ".js", ".ts", ".py", ".sh", ".sql", ".log",
}
# Skip app-private and noise dirs. Android/data and Android/obb are unreachable
# without root anyway; the rest are caches.
SKIP_DIRS = {"Android/data", "Android/obb", ".Trash", ".cache", ".git",
             "node_modules", "__pycache__"}


def is_text(path: Path, mime: str | None) -> bool:
    return (path.suffix.lower() in TEXT_EXTENSIONS
            or bool(mime and (mime.startswith("text/")
                              or mime in {"application/json", "application/xml"})))


def read_text(path: Path, max_bytes: int) -> str:
    try:
        data = path.read_bytes()[:max_bytes]
    except Exception:
        return ""
    if b"\x00" in data[:4096]:  # looks binary
        return ""
    return data.decode("utf-8", errors="replace")


def should_skip(path: Path, root: Path, include_hidden: bool) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except Exception:
        rel_parts = path.parts
    joined = "/".join(rel_parts)
    if not include_hidden and any(part.startswith(".") for part in rel_parts):
        return True
    return (any(part in SKIP_DIRS for part in rel_parts)
            or any(joined.startswith(skip + "/") for skip in SKIP_DIRS))


def post_batch(server: str, token: str | None, device: str, batch: list[dict]) -> dict:
    url = server.rstrip("/") + "/device-files/ingest"
    payload = json.dumps({"device": device, "files": batch}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Mongoose-Token"] = token
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def iter_files(root: Path, limit: int, include_hidden: bool):
    count = 0
    for path in root.rglob("*"):
        if count >= limit:
            break
        if not path.is_file():
            continue
        if should_skip(path, root, include_hidden):
            continue
        yield path
        count += 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Push an Android file index to Mongoose")
    ap.add_argument("--server", required=True, help="Mongoose API URL, e.g. http://192.168.1.x:8765")
    ap.add_argument("--token", default=os.getenv("MONGOOSE_API_TOKEN"), help="X-Mongoose-Token for the remote API")
    ap.add_argument("--root", action="append", default=[], help="Root to scan (repeatable). Default: /sdcard")
    ap.add_argument("--device", default=os.getenv("MONGOOSE_DEVICE_ID") or "phone", help="Device label, e.g. phone")
    ap.add_argument("--limit", type=int, default=100000)
    ap.add_argument("--batch-size", type=int, default=100)
    ap.add_argument("--max-text-bytes", type=int, default=65536)
    ap.add_argument("--include-hidden", action="store_true")
    args = ap.parse_args()

    roots = [Path(r).expanduser() for r in (args.root or ["/sdcard"])]
    total = 0
    batch: list[dict] = []
    started = time.time()
    for root in roots:
        if not root.exists():
            print(f"Skipping missing root: {root}", file=sys.stderr)
            continue
        for path in iter_files(root, args.limit, args.include_hidden):
            try:
                st = path.stat()
            except Exception:
                continue
            mime = mimetypes.guess_type(str(path))[0]
            batch.append({
                "path": str(path),
                "filename": path.name,
                "mtime": st.st_mtime,
                "text": read_text(path, args.max_text_bytes) if is_text(path, mime) else "",
                "device_id": args.device,
            })
            if len(batch) >= args.batch_size:
                out = post_batch(args.server, args.token, args.device, batch)
                total += len(batch)
                print(f"posted {total}: {out}")
                batch = []
    if batch:
        out = post_batch(args.server, args.token, args.device, batch)
        total += len(batch)
        print(f"posted {total}: {out}")
    print(json.dumps({"ok": True, "posted": total, "seconds": round(time.time() - started, 2)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
