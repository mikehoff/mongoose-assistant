"""Install (pull) an Ollama model from the cockpit, with progress the UI can poll.

The owner clicks 'Install' next to a role that points at a model not yet on the machine. That
kicks off a background pull against Ollama's /api/pull, and this module tracks per-model
progress in memory so a status endpoint can report it. In-memory is deliberate: a pull that
outlives a server restart loses only its progress bar, not correctness (the next installed-model
check tells the truth), and nothing here is load-bearing state."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

# model tag -> {model, state, pct, status_text, error, started}
# state: 'running' | 'done' | 'error'
_PROGRESS: dict[str, dict[str, Any]] = {}


def install_state(model: str) -> dict[str, Any]:
    """Current progress for one model, or an idle marker if it was never started here."""
    return _PROGRESS.get(model) or {"model": model, "state": "idle"}


def all_installs() -> dict[str, dict[str, Any]]:
    return dict(_PROGRESS)


async def _run_pull(model: str) -> None:
    import httpx
    from ..settings import get_settings
    s = get_settings()
    st = _PROGRESS[model]
    try:
        async with httpx.AsyncClient(timeout=None) as c:
            async with c.stream("POST", f"{s.ollama_base_url}/api/pull",
                                 json={"model": model, "stream": True}) as resp:
                if resp.status_code >= 400:
                    st.update(state="error", error=f"Ollama returned HTTP {resp.status_code}")
                    return
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except Exception:
                        continue
                    if msg.get("error"):
                        st.update(state="error", error=str(msg["error"]))
                        return
                    status = msg.get("status", "")
                    total, completed = msg.get("total"), msg.get("completed")
                    if total and completed:
                        try:
                            st["pct"] = round(float(completed) * 100.0 / float(total), 1)
                        except Exception:
                            pass
                    if status:
                        st["status_text"] = status
                    if status == "success":
                        st.update(state="done", pct=100.0, status_text="installed")
                        return
        # stream ended without an explicit 'success' but without error either
        st.setdefault("state", "done")
        if st.get("state") == "running":
            st["state"] = "done"
    except Exception as exc:  # noqa: BLE001
        st.update(state="error", error=f"{type(exc).__name__}: {exc}")


async def _run_embeddings_install() -> None:
    """One-click semantic-search install: pip-install the small CPU runtime libs
    (onnxruntime, tokenizers) if missing, then download the model files (idempotent, skips
    what is already on disk). The model is often already present from setup; usually only the
    libraries are missing."""
    import asyncio as _a
    import importlib
    import importlib.util
    import sys
    st = _PROGRESS["embeddings"]
    try:
        need_libs = (importlib.util.find_spec("onnxruntime") is None
                     or importlib.util.find_spec("tokenizers") is None)
        if need_libs:
            st["status_text"] = "installing libraries (onnxruntime, tokenizers)"
            proc = await _a.create_subprocess_exec(
                sys.executable, "-m", "pip", "install", "onnxruntime", "tokenizers",
                stdout=_a.subprocess.PIPE, stderr=_a.subprocess.STDOUT)
            out, _err = await proc.communicate()
            if proc.returncode != 0:
                tail = (out or b"").decode("utf-8", "replace")[-300:]
                st.update(state="error", error=f"pip install failed: {tail}")
                return
            importlib.invalidate_caches()
        from .embeddings import ensure_model, available
        loop = _a.get_running_loop()
        av = available()
        # Only download if the files are genuinely missing. The 449MB model is usually already
        # on disk from setup, so a re-download would be wasteful and, if auto-download was opted
        # out, would false-error even though nothing needs fetching.
        if not av.get("ok") and not av.get("files"):
            st["status_text"] = "downloading model files (~500MB)"
            import os
            # An explicit one-click install overrides the auto-download opt-out for this call
            # only (the user asked for it); the env is restored afterwards.
            prev = os.environ.get("MONGOOSE_AUTO_EMBEDDINGS")
            os.environ["MONGOOSE_AUTO_EMBEDDINGS"] = "1"
            try:
                res = await loop.run_in_executor(None, ensure_model)
            finally:
                if prev is None:
                    os.environ.pop("MONGOOSE_AUTO_EMBEDDINGS", None)
                else:
                    os.environ["MONGOOSE_AUTO_EMBEDDINGS"] = prev
            if not res.get("ok"):
                st.update(state="error", error=str(res.get("error") or "download failed"))
                return
            av = available()
        if av.get("ok"):
            st.update(state="done", status_text="installed", pct=100.0)
        else:
            # libs installed but a compiled extension may need a fresh process to import cleanly
            st.update(state="done", status_text="installed; restart Mongoose to activate", pct=100.0)
    except Exception as exc:  # noqa: BLE001
        st.update(state="error", error=f"{type(exc).__name__}: {exc}")


def start_embeddings_install() -> dict[str, Any]:
    """Begin (or report a running) one-click embeddings install. Progress is polled via
    install_state('embeddings') / the pull-status endpoint with model=embeddings."""
    cur = _PROGRESS.get("embeddings")
    if cur and cur.get("state") == "running":
        return {"ok": True, "already_running": True, "state": cur}
    _PROGRESS["embeddings"] = {"model": "embeddings", "state": "running", "pct": 0.0,
                               "status_text": "starting", "started": time.time()}
    try:
        asyncio.get_running_loop().create_task(_run_embeddings_install())
    except RuntimeError:
        asyncio.run(_run_embeddings_install())
    return {"ok": True, "state": _PROGRESS["embeddings"]}


# --- Local voicemail transcriber (whisper.cpp + ggml model) ---------------------------------------
# The voicemail feature depends on a local speech-to-text stack that the installer historically did
# not provision, so every voicemail read "no transcript available". This makes provisioning a
# one-click owner action (and a status the cockpit can show), matching the embeddings install pattern.

def _brew_path() -> str | None:
    import os as _os
    from .voicemail import _which
    return _which("brew") or next(
        (p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if _os.path.isfile(p)), None)


async def _brew_install(pkg: str) -> tuple[int, str]:
    brew = _brew_path()
    if not brew:
        return 1, "Homebrew not found (install from https://brew.sh)"
    proc = await asyncio.create_subprocess_exec(
        brew, "install", pkg, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode or 0, (out or b"").decode("utf-8", "replace")


async def _download_ggml_model(model_name: str, st: dict[str, Any]) -> tuple[bool, str]:
    import os as _os
    import httpx
    dest_dir = _os.path.expanduser("~/.mongoose/data/models")
    _os.makedirs(dest_dir, exist_ok=True)
    fname = f"ggml-{model_name}.bin"
    url = f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{fname}"
    tmp = _os.path.join(dest_dir, fname + ".part")
    final = _os.path.join(dest_dir, fname)
    try:
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as c:
            async with c.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    return False, f"model download HTTP {resp.status_code} for {fname}"
                total = int(resp.headers.get("content-length") or 0)
                got = 0
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        if total:
                            st["pct"] = round(50.0 + got * 45.0 / total, 1)
        _os.replace(tmp, final)
        return True, "ok"
    except Exception as exc:
        try:
            _os.remove(tmp)
        except Exception:
            pass
        return False, f"model download failed: {exc}"


def transcriber_status() -> dict[str, Any]:
    """Report whether local voicemail transcription is usable, and why not if it isn't."""
    from .voicemail import _which, _resolve_ggml_model, _auto_transcribe_cmd
    from ..settings import get_settings
    ffmpeg = _which("ffmpeg")
    binary = binary_path = None
    for b in ("whisper-cli", "whisper-cpp", "whisper", "whisper-ctranslate2", "main"):
        p = _which(b)
        if p:
            binary, binary_path = b, p
            break
    try:
        model_name = getattr(get_settings(), "stt_model", "base.en") or "base.en"
    except Exception:
        model_name = "base.en"
    model_path = _resolve_ggml_model(model_name)
    cmd = _auto_transcribe_cmd()
    sentinel = isinstance(cmd, str) and cmd.startswith("__")
    ready = bool(cmd) and not sentinel
    detail = None
    if not ready:
        detail = ({"__WHISPER_NO_FFMPEG__": "Whisper installed but ffmpeg missing",
                   "__WHISPER_CPP_NO_MODEL__": f"whisper.cpp installed but no ggml-{model_name}.bin model"}.get(cmd)
                  if sentinel else "no local transcriber installed")
    return {"ready": ready, "binary": binary, "binary_path": binary_path, "ffmpeg": bool(ffmpeg),
            "model": model_name, "model_path": model_path, "detail": detail}


async def _run_transcriber_install() -> None:
    st = _PROGRESS["transcriber"]
    from .voicemail import _which, _resolve_ggml_model
    from ..settings import get_settings
    try:
        if not _which("ffmpeg"):
            st.update(status_text="installing ffmpeg (brew)", pct=5.0)
            rc, out = await _brew_install("ffmpeg")
            if rc != 0 and not _which("ffmpeg"):
                st.update(state="error", error=f"brew install ffmpeg failed: {out[-300:]}")
                return
        if not any(_which(b) for b in ("whisper-cli", "whisper-cpp", "whisper", "whisper-ctranslate2")):
            st.update(status_text="installing whisper.cpp (brew)", pct=20.0)
            rc, out = await _brew_install("whisper-cpp")
            if rc != 0 and not any(_which(b) for b in ("whisper-cli", "whisper-cpp")):
                st.update(state="error", error=f"brew install whisper-cpp failed: {out[-300:]}")
                return
        try:
            model_name = getattr(get_settings(), "stt_model", "base.en") or "base.en"
        except Exception:
            model_name = "base.en"
        if not _resolve_ggml_model(model_name):
            st.update(status_text=f"downloading whisper model {model_name} (~150 MB)", pct=50.0)
            ok, msg = await _download_ggml_model(model_name, st)
            if not ok:
                st.update(state="error", error=msg)
                return
        st.update(state="done", pct=100.0, status_text="ready")
    except Exception as exc:
        st.update(state="error", error=str(exc))


def start_transcriber_install() -> dict[str, Any]:
    """One-click install of the local voicemail transcriber (ffmpeg + whisper.cpp + ggml model).
    Progress polled via install_state('transcriber') / pull-status endpoint with model=transcriber."""
    cur = _PROGRESS.get("transcriber")
    if cur and cur.get("state") == "running":
        return {"ok": True, "already_running": True, "state": cur}
    st = transcriber_status()
    if st.get("ready"):
        _PROGRESS["transcriber"] = {"model": "transcriber", "state": "done", "pct": 100.0,
                                    "status_text": "already installed", "started": time.time()}
        return {"ok": True, "already_installed": True, "state": _PROGRESS["transcriber"]}
    _PROGRESS["transcriber"] = {"model": "transcriber", "state": "running", "pct": 0.0,
                                "status_text": "starting", "started": time.time()}
    try:
        asyncio.get_running_loop().create_task(_run_transcriber_install())
    except RuntimeError:
        asyncio.run(_run_transcriber_install())
    return {"ok": True, "state": _PROGRESS["transcriber"]}


def start_install(model: str) -> dict[str, Any]:
    """Begin (or report an already-running) background pull of `model`. Returns immediately."""
    model = (model or "").strip()
    if not model:
        return {"ok": False, "error": "model required"}
    if len(model) > 200 or any(ch.isspace() or ord(ch) < 32 for ch in model):
        return {"ok": False, "error": "invalid model tag"}
    cur = _PROGRESS.get(model)
    if cur and cur.get("state") == "running":
        return {"ok": True, "already_running": True, "state": cur}
    _PROGRESS[model] = {"model": model, "state": "running", "pct": 0.0,
                        "status_text": "starting", "started": time.time()}
    try:
        asyncio.get_running_loop().create_task(_run_pull(model))
    except RuntimeError:
        # no running loop (called outside the server): run it to completion synchronously
        asyncio.run(_run_pull(model))
    return {"ok": True, "state": _PROGRESS[model]}
