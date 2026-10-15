"""Read-only access to the car-park camera recordings.

A separate recorder process writes the files; Mongoose only lists and serves them. Nothing here
writes, moves or deletes anything under the camera folder.

Layout under the base dir (env MONGOOSE_CCTV_DIR, default ~/.mongoose/camera):
  recordings_smart/continuous/smart_YYYYMMDD_HHMMSS.mp4   15 minute 720p segments (newest still growing)
  recordings_smart/events/person_YYYYMMDD_HHMMSS.mp4      person event clips
  recordings_smart/events/person_<stamp>_stillN[_body|_face].jpg
  recordings_smart/events/motion_YYYYMMDD_HHMMSS.mp4      motion-only event clips (+ motion_<stamp>_stillN.jpg)
  recordings/jo_YYYYMMDD_HHMMSS.mp4                      older recordings (retired recorder)
  recordings_smart/.live/live.m3u8, init.mp4, seg_NNNNN.m4s, latest.jpg   live feed

File resolution is strict: every requested name must match the exact pattern for its kind, is
looked up as a direct child of the right folder, and must resolve (after symlinks) inside it.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_STAMP = r"(\d{8})_(\d{6})"
_RE_CONT = re.compile(r"^smart_" + _STAMP + r"\.mp4$")
# Event files: "person" (a person was confirmed) or "motion" (strong local movement the person
# detector did not confirm; stills only, no body or face crops).
_RE_EVENT = re.compile(r"^(person|motion)_" + _STAMP + r"\.mp4$")
_RE_STILL = re.compile(r"^(person|motion)_" + _STAMP + r"_still(\d{1,3})(?:_(body|face))?\.jpg$")
_RE_OLDER = re.compile(r"^jo_" + _STAMP + r"\.mp4$")
_RE_LIVE = re.compile(r"^(live\.m3u8|init\.mp4|latest\.jpg|seg_\d{1,9}\.m4s)$")
_RE_DAY = re.compile(r"^\d{8}$")

# kind -> (folder relative to base, name regex)
_KINDS = {
    "continuous": ("recordings_smart/continuous", _RE_CONT),
    "event": ("recordings_smart/events", _RE_EVENT),
    "still": ("recordings_smart/events", _RE_STILL),
    "older": ("recordings", _RE_OLDER),
    "live": ("recordings_smart/.live", _RE_LIVE),
}

# A continuous segment modified within this many seconds is treated as still being written.
_GROWING_SECS = 90
# Upper bound for a segment's estimated length (they are 15 minutes; older ones may be longer).
_MAX_SEGMENT_SECS = 3600
# The smart recorder keeps this many seconds before a person appears (event stamp = first seen).
_EVENT_PRE_ROLL_S = 10
_RE_SEEN = re.compile(r"event " + _STAMP + r": [^;\n]{1,80}? for about (\d+) s; saved (person|motion)_")


def base_dir() -> Path:
    raw = os.environ.get("MONGOOSE_CCTV_DIR", "").strip()
    return Path(raw).expanduser() if raw else Path.home() / ".mongoose" / "camera"


def _folder(kind: str) -> Path:
    return base_dir() / _KINDS[kind][0]


def _parse(day: str, hms: str) -> Optional[datetime]:
    try:
        return datetime.strptime(day + hms, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _names(folder: Path) -> list[str]:
    try:
        return [e.name for e in os.scandir(folder) if e.is_file()]
    except OSError:
        return []


def _info(folder: Path, name: str) -> tuple[int, float]:
    try:
        st = (folder / name).stat()
        return st.st_size, st.st_mtime
    except OSError:
        return 0, 0.0


def status() -> dict[str, Any]:
    base = base_dir()
    live = _folder("live")
    _, jpg_m = _info(live, "latest.jpg")
    _, m3u_m = _info(live, "live.m3u8")
    now = time.time()
    return {
        "ok": True,
        "base_exists": base.is_dir(),
        "live_image_age_s": round(now - jpg_m, 1) if jpg_m else None,
        "live_stream_age_s": round(now - m3u_m, 1) if m3u_m else None,
        "live_ok": bool(m3u_m and now - m3u_m < 30),
        "image_ok": bool(jpg_m and now - jpg_m < 30),
    }


def list_days() -> list[str]:
    """Days (YYYYMMDD) that have any recording, newest first."""
    return [d["day"] for d in list_days_info()]


def list_days_info() -> list[dict[str, Any]]:
    """Days with any recording, newest first, each with counts of person events and motion-only
    events (distinct event stamps, clip or stills), continuous segments and older recordings. One directory scan per folder."""
    days: dict[str, dict[str, Any]] = {}
    stamps: dict[str, set[str]] = {}

    def slot(d: str) -> dict[str, Any]:
        return days.setdefault(d, {"day": d, "events": 0, "motion": 0, "continuous": 0, "older": 0})

    for kind in ("continuous", "older"):
        rx = _KINDS[kind][1]
        for n in _names(_folder(kind)):
            m = rx.match(n)
            if m and _parse(m.group(1), m.group(2)):
                slot(m.group(1))[kind] += 1
    for n in _names(_folder("event")):
        m = _RE_EVENT.match(n) or _RE_STILL.match(n)
        if m and _parse(m.group(2), m.group(3)):
            slot(m.group(2))
            stamps.setdefault(m.group(2), set()).add(m.group(1) + "_" + m.group(3))
    for d, st in stamps.items():
        days[d]["events"] = sum(1 for k in st if k.startswith("person_"))
        days[d]["motion"] = len(st) - days[d]["events"]
    return [days[d] for d in sorted(days, reverse=True)]


def _midnight(day: str) -> float:
    return datetime.strptime(day, "%Y%m%d").timestamp()


def _video_list(kind: str, day: str) -> list[dict[str, Any]]:
    if not _RE_DAY.match(day or ""):
        return []
    folder = _folder(kind)
    rx = _KINDS[kind][1]
    now = time.time()
    base_ts = _midnight(day)
    out = []
    for n in _names(folder):
        m = rx.match(n)
        if not m or m.group(1) != day:
            continue
        when = _parse(m.group(1), m.group(2))
        if not when:
            continue
        size, mtime = _info(folder, n)
        start_s = when.hour * 3600 + when.minute * 60 + when.second
        out.append({"name": n, "kind": kind, "time": when.strftime("%H:%M:%S"),
                    "iso": when.isoformat(), "size": size, "start_s": start_s,
                    "_mtime": mtime,
                    "growing": bool(kind == "continuous" and mtime and now - mtime < _GROWING_SECS)})
    out.sort(key=lambda r: r["iso"])
    # End estimate: the file's last write (the recorder stops writing when the segment ends), never
    # past the next segment's start and never more than an hour. A growing segment ends "now".
    for i, r in enumerate(out):
        mtime = r.pop("_mtime")
        end = (now if r["growing"] else mtime) - base_ts if mtime else r["start_s"] + 1
        end = min(end, r["start_s"] + _MAX_SEGMENT_SECS)
        if i + 1 < len(out):
            end = min(end, out[i + 1]["start_s"])
        r["end_s"] = int(max(end, r["start_s"] + 1))
    return out


def list_continuous(day: str) -> list[dict[str, Any]]:
    return _video_list("continuous", day)


def list_older(day: str) -> list[dict[str, Any]]:
    return _video_list("older", day)


def list_events(day: str) -> list[dict[str, Any]]:
    """Person and motion-only events for a day, each with its stills (and body/face crops when
    present), oldest first. "type" is "person" or "motion". An event whose clip is missing but
    whose stills exist is still listed (clip null)."""
    if not _RE_DAY.match(day or ""):
        return []
    folder = _folder("event")
    events: dict[str, dict[str, Any]] = {}

    def ev(kind: str, d: str, t: str) -> Optional[dict[str, Any]]:
        when = _parse(d, t)
        if not when:
            return None
        key = kind + "_" + d + "_" + t
        if key not in events:
            events[key] = {"type": kind, "stamp": d + "_" + t, "time": when.strftime("%H:%M:%S"),
                           "iso": when.isoformat(),
                           "start_s": when.hour * 3600 + when.minute * 60 + when.second,
                           "seen_s": None, "pre_roll_s": _EVENT_PRE_ROLL_S,
                           "clip": None, "size": 0, "stills": {}}
        return events[key]

    for n in _names(folder):
        m = _RE_EVENT.match(n)
        if m and m.group(2) == day:
            e = ev(m.group(1), m.group(2), m.group(3))
            if e is not None:
                e["clip"] = n
                e["size"] = _info(folder, n)[0]
            continue
        m = _RE_STILL.match(n)
        if m and m.group(2) == day:
            e = ev(m.group(1), m.group(2), m.group(3))
            if e is not None:
                idx = int(m.group(4))
                slot = e["stills"].setdefault(idx, {"index": idx, "full": None, "body": None, "face": None})
                slot[m.group(5) or "full"] = n
    seen = _seen_durations() if events else {}
    out = []
    for key, e in events.items():
        e["seen_s"] = seen.get(key)
        e["stills"] = [e["stills"][k] for k in sorted(e["stills"])]
        out.append(e)
    out.sort(key=lambda r: (r["iso"], r["type"]))
    return out


def _seen_durations() -> dict[str, int]:
    """"<type>_<stamp>" -> seconds the person (or movement) was seen, from the smart recorder's log
    ("event <stamp>: ... for about N s; saved <type>_<stamp>.mp4"). Read-only, last 1 MB only;
    empty when the log is missing."""
    path = base_dir() / "smart_recorder.log"
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 1_048_576))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    return {m.group(4) + "_" + m.group(1) + "_" + m.group(2): int(m.group(3))
            for m in _RE_SEEN.finditer(text)}


_THUMB_CACHE: dict[tuple[str, float], bytes] = {}
_THUMB_MAX_ENTRIES = 300
_THUMB_WIDTH = 480


def thumbnail(path: Path) -> Optional[bytes]:
    """A small JPEG (480 px wide) of a still, kept in a bounded in-memory cache, so the event grid
    stays light over the tunnel. None when Pillow is missing or the image will not decode (the
    caller then serves the original). Never writes anything to disk."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = (path.name, mtime)
    hit = _THUMB_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        import io
        from PIL import Image
        with Image.open(path) as img:
            img.draft("RGB", (_THUMB_WIDTH, _THUMB_WIDTH))
            img = img.convert("RGB")
            if img.width > _THUMB_WIDTH:
                img = img.resize((_THUMB_WIDTH, max(1, round(img.height * _THUMB_WIDTH / img.width))))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=78)
    except Exception:
        return None
    data = buf.getvalue()
    if len(_THUMB_CACHE) >= _THUMB_MAX_ENTRIES:
        _THUMB_CACHE.pop(next(iter(_THUMB_CACHE)))
    _THUMB_CACHE[key] = data
    return data


def resolve(kind: str, name: str) -> Optional[Path]:
    """The absolute path for a requested file, or None if the name is not exactly a valid
    recording name for that kind, or the file is missing or resolves outside its folder."""
    spec = _KINDS.get(kind)
    if not spec or not isinstance(name, str) or len(name) > 80:
        return None
    if "/" in name or "\\" in name or "\x00" in name or not spec[1].match(name):
        return None
    folder = base_dir() / spec[0]
    try:
        root = folder.resolve(strict=True)
        path = (folder / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if path.parent != root or not path.is_file():
        return None
    return path


def live_playlist(token_query: str = "") -> Optional[str]:
    """The live HLS playlist text. With token_query (e.g. 'token=abc'), every relative URI in it
    (segments and the EXT-X-MAP init) gets that query appended, so a client that cannot send
    headers (Home Assistant) can fetch the parts with the same token."""
    p = resolve("live", "live.m3u8")
    if p is None:
        return None
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not token_query:
        return text
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and _RE_LIVE.match(s):
            line = s + "?" + token_query
        elif s.startswith("#EXT-X-MAP:"):
            line = re.sub(r'URI="([^"?]+)"',
                          lambda m: 'URI="' + m.group(1) + "?" + token_query + '"', s)
        out.append(line)
    return "\n".join(out) + "\n"
