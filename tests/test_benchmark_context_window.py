"""The benchmark must measure the model, not a truncated prompt.

Real bug: _timed_chat set only num_predict, so Ollama used its own small default context (~2k). The
planner system prompt plus the tool catalogue is ~6.7k tokens, so the tool list was silently cut away
and every model replied "I can't control smart home devices" , reporting tool-calls 0/5 for ALL FIVE
models. That looked like a fleet of bad models; it was the harness. Tool calls worked fine in normal
use because the real chat path (llm.py) sets num_ctx explicitly.
"""
import inspect

from mongoose import benchmark as B


def test_timed_chat_sets_an_explicit_context_window():
    src = inspect.getsource(B._timed_chat)
    assert "num_ctx" in src, (
        "the benchmark must set num_ctx explicitly; without it Ollama truncates the ~6.7k-token "
        "planner prompt and the tool catalogue disappears")


def test_benchmark_context_matches_the_real_chat_path():
    """A benchmark measured at a different context size than production is not measuring production."""
    from mongoose.settings import get_settings
    src = inspect.getsource(B._timed_chat)
    assert "settings.num_ctx" in src or "getattr(settings" in src, \
        "num_ctx should come from settings (the same source llm.py uses), not a separate literal"
    assert int(getattr(get_settings(), "num_ctx", 0)) >= 4096, \
        "the configured context must comfortably exceed the planner prompt"


def test_planner_prompt_still_fits_the_configured_context():
    """If the catalogue grows past the context again, the same silent-truncation bug returns."""
    from mongoose.settings import get_settings
    approx_tokens = (len(B._PLANNER_SYSTEM) + len(B._tool_catalogue())) // 4
    num_ctx = int(getattr(get_settings(), "num_ctx", 8192))
    assert approx_tokens < num_ctx, (
        f"planner prompt is ~{approx_tokens} tokens but num_ctx is {num_ctx}: the tool catalogue "
        "would be truncated and every tool-call score would collapse to zero again")


def test_the_home_control_tool_is_actually_advertised():
    """The scored task asks for a control_home plan, so the catalogue must offer that tool."""
    assert "control_home" in B._tool_catalogue()
