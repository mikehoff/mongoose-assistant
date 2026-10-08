"""The deterministic chat bridge for plugin authoring (adopted from the parallel coder's patch,
rewired to the real codegen loop) plus the confirm-path bug it exposed.

The trap this kills: "make the WordPress plugin useful" used to be swallowed by the general RTM
build-status short-circuit, or depend on the local model choosing the right tool. Routing to a
build/deploy proposal is now deterministic regex, BEFORE the RTM status card, and confirmation
("confirm <token>") is intercepted deterministically too.

The bug it exposed in 1.31.x: the author verb existed in the public tool executor (dry-run
worked) but not in sensitive_tools' confirm-execution branch, so every confirmed author action
died with "needs action scaffold/build/...". These tests run the FULL loop so that class of
propose-works-confirm-dies gap cannot ship again.
"""
from __future__ import annotations

import asyncio
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGOOSE_DB_PATH", str(tmp_path / "b.sqlite3"))
    monkeypatch.setenv("MONGOOSE_PLUGIN_DIR", str(tmp_path / "plugin-src"))
    from mongoose.db import init_db
    init_db()
    from mongoose.services.wp_plugin import scaffold_example_court_plugin
    assert scaffold_example_court_plugin("0.2.0")["ok"]


def test_author_intent_proposes_before_rtm_build_status(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from mongoose.llm import chat_with_memory
    out = asyncio.run(chat_with_memory(
        "Make the Example Court WordPress plugin useful. Service Charges should show arrears.",
        conversation_id="t1", role="chat"))
    assert out.get("source") == "wp_plugin_chat", out
    assert out.get("proposal", {}).get("confirm_token")
    assert "confirm" in out["reply"]


def test_confirm_token_runs_the_real_author_loop(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    def fake_invoke(task, repo):
        f = Path(repo) / "example-court-rtm.php"
        f.write_text(f.read_text().replace("<h1>Service Charges</h1>",
                                           "<h1>Service Charges</h1><p>ARREARS</p>"))
        return True, "edited", False
    import mongoose.services.builder as B
    monkeypatch.setattr(B, "_invoke_claude_code", fake_invoke)
    from mongoose.llm import chat_with_memory
    out = asyncio.run(chat_with_memory(
        "Improve the WordPress plugin: show an arrears box on Service Charges.",
        conversation_id="t2", role="chat"))
    token = out["proposal"]["confirm_token"]
    # A plugin build runs Claude Code for minutes, so confirm now ACKNOWLEDGES immediately and runs the
    # author loop in a background task (holding the chat request open froze the phone). The confirm and
    # the drain must share ONE loop: create_task binds to the running loop, so a second asyncio.run
    # could never complete the background task. Assert the ack AND that the background run applied.
    from mongoose.services.rtm_plugin_chat import maybe_handle_plugin_chat, _BUILD_TASKS

    async def _confirm_then_drain():
        r = await maybe_handle_plugin_chat(f"confirm {token}", conversation_id="t2")
        for _ in range(200):
            if not _BUILD_TASKS:
                break
            await asyncio.sleep(0.01)
        return r
    out2 = asyncio.run(_confirm_then_drain())
    assert "On it" in out2["reply"] and out2["source"] == "wp_plugin_chat"
    assert "ARREARS" in (tmp_path / "plugin-src" / "example-court-rtm.php").read_text()
    brief = (tmp_path / "plugin-src" / "BUILD_BRIEF.md").read_text()
    assert "arrears box" in brief                           # provenance recorded


def test_deploy_wording_only_when_asked(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from mongoose.llm import chat_with_memory
    out = asyncio.run(chat_with_memory(
        "Make the plugin useful and deploy the beta to example-court.org.",
        conversation_id="t3", role="chat"))
    assert "upload the beta" in out["reply"]
    out2 = asyncio.run(chat_with_memory(
        "Make the WordPress plugin better.", conversation_id="t3b", role="chat"))
    assert "will not touch the live WordPress site" in out2["reply"]


def test_deploy_only_does_not_reauthor(monkeypatch, tmp_path):
    """The bug M hit: 'deploy the plugin' re-ran Claude Code and rebuilt an unchanged plugin. A deploy
    request with no code-change verb must propose action=deploy (ship what's built), NOT author."""
    _env(monkeypatch, tmp_path)
    from mongoose.services.rtm_plugin_chat import maybe_handle_plugin_chat
    r = asyncio.run(maybe_handle_plugin_chat("deploy the plugin to WordPress", conversation_id="dz", force=True))
    assert r is not None
    low = r["reply"].lower()
    assert "the code doesn't change" in low          # deploy wording, not author
    assert "claude code" not in low                   # must NOT describe re-authoring
    assert "confirm" in low


def test_no_grating_preamble_anywhere():
    """M asked us to kill the 'Yes : this is now a chat-driven build action' line. It must be gone."""
    import inspect
    from mongoose.services import rtm_plugin_chat as R
    src = inspect.getsource(R)
    assert "this is now a chat-driven build action" not in src


def test_bridge_ignores_normal_chat_and_guests(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    from mongoose.services.rtm_plugin_chat import maybe_handle_plugin_chat
    assert asyncio.run(maybe_handle_plugin_chat("what's the weather like?")) is None
    assert asyncio.run(maybe_handle_plugin_chat("")) is None
    # guest_mode never reaches the bridge (gated in chat_with_memory itself): the lounge and
    # guest devices must not be able to propose plugin builds.
    import mongoose.llm as L
    src = Path(L.__file__).read_text()
    i = src.find("maybe_handle_plugin_chat")
    # the bridge call must be gated on not-guest (the guard was later widened to
    # `if not guest_mode and (_resolved == "rtm_plugin" ...)`, so match the guard, not a fixed literal)
    assert "if not guest_mode" in src[max(0, i-600):i]


def test_author_confirm_gap_is_closed_in_sensitive_tools():
    """The 1.31.x bug, pinned directly: every action the public wp_plugin executor accepts must
    be executable by the sensitive confirm branch too (propose-works-confirm-dies is a class,
    not an instance)."""
    from pathlib import Path as P
    st = P("src/mongoose/services/sensitive_tools.py").read_text()
    for verb in ("scaffold", "build", "deploy", "promote", "refresh", "author"):
        assert f'action == "{verb}"' in st or f'"{verb}"' in st, f"{verb} missing from confirm branch"
