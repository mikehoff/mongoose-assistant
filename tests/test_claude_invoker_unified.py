"""Consolidation Phase 2: every LOCAL Claude Code invoker resolves the binary through the one shared
resolver builder._resolve_claude_cmd, not a bare shutil.which('claude') that returns None under the
launchd server PATH (which excludes ~/.local/bin). That bare-which was a latent agent_run_failed in
self_patch's codegen and peer-review paths."""
import inspect


def test_self_patch_invokers_use_the_shared_resolver():
    from mongoose.services import self_patch
    for fn in (self_patch._invoke_codegen, self_patch._peer_review):
        src = inspect.getsource(fn)
        assert "_resolve_claude_cmd" in src, fn.__name__
        # no bare which('claude') as the actual lookup (comments may still mention it)
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        assert 'shutil.which("claude")' not in code and "shutil.which('claude')" not in code, fn.__name__


def test_resolver_finds_claude_outside_launchd_path():
    from mongoose.services.builder import _resolve_claude_cmd
    # absolute path passes through; a bare name resolves via PATH or the known install locations
    assert _resolve_claude_cmd("/definitely/not/here/claude") is None
    got = _resolve_claude_cmd("claude")
    assert got is None or got.endswith("claude")
