#!/usr/bin/env python3
"""Prove that the release code + a site profile behaves exactly like a running install.

Takes a consistent read-only snapshot of the install's brain, gives each side its own copy (an APFS
clone, so no extra disk until written), and runs tools/equivalence/probe.py twice:

  A. the install's current code            (e.g. ~/.mongoose/app/src)
  B. the release code + the private profile (profile.json placed next to B's brain copy)

Both sides run with a throwaway HOME, the SAME explicit config file (the install's .env with every
credential value redacted, so both see identical settings and no secret enters the sandbox), the egress
guard forced off and every HTTP request sent to a dead local proxy: nothing can leave the machine and
nothing touches the real brain. A side that writes a .env into its sandbox HOME fails the run. Outputs
hold real data, so they go to a private folder (~/.mongoose/equivalence-runs, mode 700). The two JSON
fingerprints are compared field by field. Exit 0 = equivalent.

Usage:
  tools/equivalence/check.py --live-src ~/.mongoose/app/src --brain ~/.mongoose/data/mongoose.sqlite3 \\
      --data-src ~/.mongoose/data --profile ~/.mongoose/rtm-config/profile.json [--python PY] [--keep]
"""
from __future__ import annotations

import argparse
import re
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASE_SRC = HERE.parent.parent / "src"
PRIVATE_DATA = ("rtm", "rtm_templates")  # the data-dir config the RTM seam reads


def snapshot(brain: Path, dest: Path) -> None:
    src = sqlite3.connect(f"file:{brain}?mode=ro", uri=True, timeout=30)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst, pages=4096)
    finally:
        dst.close()
        src.close()


def clone(src: Path, dst: Path) -> None:
    # APFS copy-on-write clone where available, plain copy otherwise.
    if subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True).returncode != 0:
        shutil.copy2(src, dst)


SECRET_KEY = re.compile(r"PASS|TOKEN|SECRET|KEY|CREDENTIAL|CLIENT_ID|PRIVATE|SALT|COOKIE|AUTH", re.I)


def redacted_env(src: Path, dest: Path) -> None:
    """The install's .env with every credential value replaced. Both sides load exactly this file, so they
    see the same configuration (which jobs and features are enabled) without any secret entering the
    sandbox. Passing it explicitly also stops any code from 'migrating' a real .env into the fake HOME."""
    lines = []
    if src.exists():
        for line in src.read_text(errors="ignore").splitlines():
            m = re.match(r"\s*(export\s+)?([A-Za-z0-9_]+)\s*=\s*(.*)$", line)
            if m and SECRET_KEY.search(m.group(2)) and m.group(3).strip().strip("'\""):
                line = f"{m.group(2)}=redacted-for-equivalence"
            lines.append(line)
    dest.write_text("\n".join(lines) + "\n")
    dest.chmod(0o600)


def side_env(root: Path, src: Path, site_packages: str, env_file: Path) -> dict[str, str]:
    dead = "http://127.0.0.1:9"
    return {
        "MONGOOSE_ENV_FILE": str(env_file), "MONGOOSE_CONFIG_DIR": str(root / "home" / ".mongoose"),
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin",
        "HOME": str(root / "home"), "TMPDIR": str(root / "tmp"),
        "PYTHONPATH": f"{src}:{site_packages}",
        "MONGOOSE_DB_PATH": str(root / "data" / "mongoose.sqlite3"),
        "MONGOOSE_DATA_DIR": str(root / "data"),
        "MONGOOSE_ENV": "sandbox", "MONGOOSE_EGRESS": "off",
        "MONGOOSE_TTS_CMD": "mongoose-equivalence-no-tts",
        "MONGOOSE_INPROCESS_SCHEDULER": "false", "MONGOOSE_RUNTIME_WATCHDOG": "false", "MONGOOSE_NO_WATCHDOG": "1",
        "HTTP_PROXY": dead, "HTTPS_PROXY": dead, "ALL_PROXY": dead,
        "http_proxy": dead, "https_proxy": dead, "all_proxy": dead, "NO_PROXY": "", "no_proxy": "",
        "OLLAMA_HOST": dead, "TZ": "Europe/London",
    }


def diff(a, b, path="", out=None, limit=200):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append({"path": path, "live": a, "release": b})
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append({"path": f"{path}.{k}", "live": a.get(k, "<missing>"), "release": b.get(k, "<missing>")})
            else:
                diff(a[k], b[k], f"{path}.{k}", out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append({"path": path, "live": f"{len(a)} items", "release": f"{len(b)} items"})
        for i, (x, y) in enumerate(zip(a, b)):
            diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append({"path": path, "live": a, "release": b})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live-src", required=True, type=Path)
    ap.add_argument("--release-src", default=RELEASE_SRC, type=Path)
    ap.add_argument("--brain", required=True, type=Path)
    ap.add_argument("--data-src", required=True, type=Path, help="install data dir holding rtm/ and rtm_templates/")
    ap.add_argument("--profile", required=True, type=Path)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--site-packages", default="", help="dependencies for both sides (default: --python's own)")
    ap.add_argument("--env-file", type=Path, default=Path.home() / ".mongoose" / ".env",
                    help="install .env; only a credential-redacted copy is used")
    ap.add_argument("--work", type=Path, default=None,
                    help="default: a private folder under ~/.mongoose/equivalence-runs (outputs hold real data)")
    ap.add_argument("--keep", action="store_true", help="keep the sandbox (brain copies) for inspection")
    a = ap.parse_args()

    brain = a.brain.expanduser().resolve()
    runs = Path.home() / ".mongoose" / "equivalence-runs"
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o700)
    work = (a.work or Path(tempfile.mkdtemp(prefix="run-", dir=runs))).resolve()
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o700)
    if str(work).startswith(str(brain.parent)):
        sys.exit("refusing: the sandbox must not live inside the install's data dir")
    sp = a.site_packages or subprocess.run([a.python, "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                                           capture_output=True, text=True).stdout.strip()
    t0 = time.time()
    base = work / "snapshot.sqlite3"
    env_file = work / "redacted.env"
    redacted_env(a.env_file.expanduser(), env_file)
    print(f"snapshotting {brain} (read-only) ...", flush=True)
    snapshot(brain, base)
    results = {}
    try:
        for name, src in (("live", a.live_src.expanduser()), ("release", a.release_src.expanduser())):
            root = work / name
            for d in ("home", "tmp", "data"):
                (root / d).mkdir(parents=True, exist_ok=True)
            clone(base, root / "data" / "mongoose.sqlite3")
            for d in PRIVATE_DATA:
                if (a.data_src / d).exists():
                    shutil.copytree(a.data_src / d, root / "data" / d, dirs_exist_ok=True)
            if name == "release":
                prof = root / "data" / "profile.json"
                shutil.copy2(a.profile.expanduser(), prof)
                prof.chmod(0o600)
            print(f"probing {name} code: {src}", flush=True)
            r = subprocess.run([a.python, "-S", str(HERE / "probe.py")], cwd=root, env=side_env(root, src, sp, env_file),
                               capture_output=True, text=True, timeout=1800)
            if r.returncode != 0 or not r.stdout.strip().startswith("{"):
                print(r.stderr[-3000:])
                sys.exit(f"probe failed on the {name} side")
            results[name] = json.loads(r.stdout)
            leaked = [p for p in (root / "home").rglob(".env")]
            if leaked:
                for p in leaked:
                    p.unlink()
                sys.exit(f"isolation breach: the {name} side wrote a .env into its sandbox HOME (deleted)")
            (work / f"{name}.json").write_text(r.stdout)
    finally:
        if not a.keep:
            env_file.unlink(missing_ok=True)
            for p in [base, work / "live" / "data" / "mongoose.sqlite3", work / "release" / "data" / "mongoose.sqlite3"]:
                for suffix in ("", "-wal", "-shm"):
                    Path(str(p) + suffix).unlink(missing_ok=True)

    live, rel = results["live"]["results"], results["release"]["results"]
    report = {"probes": {}, "seconds": round(time.time() - t0, 1)}
    for probe in sorted(set(live) | set(rel)):
        d = diff(live.get(probe), rel.get(probe), probe)
        report["probes"][probe] = {"equal": not d, "differences": d}
    (work / "report.json").write_text(json.dumps(report, indent=1, default=str))
    bad = [p for p, v in report["probes"].items() if not v["equal"]]
    for p, v in report["probes"].items():
        err_l = results["live"]["errors"].get(p)
        err_r = results["release"]["errors"].get(p)
        note = f"  (live error: {err_l})" if err_l else ""
        note += f"  (release error: {err_r})" if err_r else ""
        print(f"{'EQUAL ' if v['equal'] else 'DIFFER'} {p}{note}")
        for d in v["differences"][:5]:
            print(f"         {d['path']}: live={str(d['live'])[:90]!r} release={str(d['release'])[:90]!r}")
    print(f"\n{len(report['probes']) - len(bad)}/{len(report['probes'])} probes equal; report: {work / 'report.json'}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
