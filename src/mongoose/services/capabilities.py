from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

# Capability layer.
#
# A "capability" is a user-facing feature (Gmail, document content search, voice)
# plus the concrete things it needs to actually work: importable pip packages,
# system commands on PATH, and sometimes configured env. This module is the single
# place that knows "feature X needs package Y", so setup, doctor, and update can all
# answer: is this capability ready, what's missing, and install the missing bits
# INTO THE INTERPRETER ACTUALLY RUNNING MONGOOSE (not whatever pip is on PATH).
#
# The principle: if Mongoose exposes a feature, it must be able to tell whether the
# feature's dependencies are present, explain what's missing, and offer to fix it.

def _find_manifest() -> Path:
    """Locate configs/capabilities.yaml. It lives at the repo root, but Mongoose can
    be run from various working dirs, so search upward from this file and also try
    the CWD."""
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent.parent / "configs" / "capabilities.yaml",  # repo root from src/mongoose/services/
        here.parent.parent.parent / "configs" / "capabilities.yaml",
        Path.cwd() / "configs" / "capabilities.yaml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


CAPABILITIES_PATH = _find_manifest()

# Map a pip distribution name to the module name you actually import (when they
# differ), so detection checks the right thing.
_PIP_TO_IMPORT = {
    "google-api-python-client": "googleapiclient",
    "google-auth": "google.auth",
    "google-auth-oauthlib": "google_auth_oauthlib",
    "python-docx": "docx",
    "openai-whisper": "whisper",
    "pytest-asyncio": "pytest_asyncio",
    "pypdfium2": "pypdfium2",
    "pynacl": "nacl",
}


def _load_manifest() -> dict[str, dict[str, Any]]:
    """Parse capabilities.yaml. Uses PyYAML if present, else a tiny purpose-built
    parser for this fixed, simple structure (so the capability layer itself never
    depends on an optional library)."""
    text = CAPABILITIES_PATH.read_text(encoding="utf-8") if CAPABILITIES_PATH.exists() else ""
    if not text.strip():
        return {}
    try:
        import yaml  # optional
        data = yaml.safe_load(text)
        return data or {}
    except Exception:
        return _mini_parse(text)


def _mini_parse(text: str) -> dict[str, dict[str, Any]]:
    """Minimal parser for the capabilities manifest: top-level keys, then indented
    `field: value` where value is a scalar or an inline [list]. Sufficient for the
    fixed shape of capabilities.yaml; not a general YAML parser."""
    out: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith(" ") and raw.rstrip().endswith(":"):
            current = raw.rstrip()[:-1].strip()
            out[current] = {}
            continue
        if current and ":" in raw:
            key, _, val = raw.strip().partition(":")
            key = key.strip()
            val = val.strip()
            if val.startswith("[") and val.endswith("]"):
                items = [v.strip() for v in val[1:-1].split(",") if v.strip()]
                out[current][key] = items
            elif val in ("true", "false"):
                out[current][key] = (val == "true")
            else:
                out[current][key] = val
    return out


def _module_present(pip_name: str) -> bool:
    mod = _PIP_TO_IMPORT.get(pip_name, pip_name.replace("-", "_"))
    try:
        # handle dotted modules like google.auth
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def _command_present(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def detect_capability(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Assess one capability: which pip packages and commands are missing, and
    whether the (optional) env hint is set. ready = nothing required is missing."""
    import os
    pip = spec.get("pip", []) or []
    commands = spec.get("commands", []) or []
    env_any = spec.get("env_any", []) or []

    missing_pip = [p for p in pip if not _module_present(p)]
    missing_cmd = [c for c in commands if not _command_present(c)]
    env_ok = (not env_any) or any(os.getenv(e) for e in env_any)

    ready = not missing_pip and not missing_cmd
    return {
        "capability": name,
        "description": spec.get("description", ""),
        "ready": ready,
        "configured": env_ok,
        "missing_pip": missing_pip,
        "missing_commands": missing_cmd,
        "enables": spec.get("enables", ""),
    }


def detect_all() -> list[dict[str, Any]]:
    manifest = _load_manifest()
    return [detect_capability(name, spec) for name, spec in manifest.items()]


def install_capability(name: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Install a capability's MISSING pip packages into the active interpreter.
    Uses sys.executable -m pip so packages land in the venv actually running
    Mongoose, not a different Python on PATH. System commands (e.g. ffmpeg) are
    reported but never auto-installed (that's a package-manager decision for the
    user). Never installs silently: callers gate this behind a prompt or a flag."""
    manifest = _load_manifest()
    spec = manifest.get(name)
    if not spec:
        return {"capability": name, "ok": False, "error": "unknown capability"}
    det = detect_capability(name, spec)
    result: dict[str, Any] = {"capability": name, "installed": [], "skipped_commands": det["missing_commands"]}
    if not det["missing_pip"]:
        result["ok"] = True
        result["note"] = "nothing to install"
        return result
    if dry_run:
        result["ok"] = True
        result["would_install"] = det["missing_pip"]
        return result
    cmd = [sys.executable, "-m", "pip", "install", *det["missing_pip"]]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        ok = proc.returncode == 0
        result["ok"] = ok
        result["installed"] = det["missing_pip"] if ok else []
        if not ok:
            result["error"] = (proc.stderr or proc.stdout or "").strip()[-500:]
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)
    return result


def install_all_missing(*, dry_run: bool = False) -> list[dict[str, Any]]:
    """Install missing pip packages for every not-ready capability, then make sure the
    fuzzy-matching model is present (part of the main install, not a separate action)."""
    out = []
    for det in detect_all():
        if det["missing_pip"]:
            out.append(install_capability(det["capability"], dry_run=dry_run))
    if not dry_run:
        try:
            from .embeddings import ensure_model
            res = ensure_model(quiet=False)
            out.append({"capability": "embeddings_model", "ok": res["ok"],
                        "downloaded": res["downloaded"], "error": res["error"]})
        except Exception as exc:
            out.append({"capability": "embeddings_model", "ok": False, "error": str(exc)})
    return out


def capabilities_report() -> dict[str, Any]:
    """A doctor-friendly summary: ready vs missing, with the exact fix command."""
    dets = detect_all()
    ready = [d["capability"] for d in dets if d["ready"]]
    missing = [d for d in dets if not d["ready"]]
    lines = []
    for d in missing:
        bits = []
        if d["missing_pip"]:
            bits.append("pip: " + ", ".join(d["missing_pip"]))
        if d["missing_commands"]:
            bits.append("commands: " + ", ".join(d["missing_commands"]))
        lines.append({
            "capability": d["capability"],
            "missing": "; ".join(bits),
            "fix": f"mongoose setup --capability {d['capability']}"
                   if d["missing_pip"] else f"install {', '.join(d['missing_commands'])} (system package)",
        })
    return {"ready": ready, "missing": lines, "all": dets}
