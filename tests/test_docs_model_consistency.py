from pathlib import Path


def test_active_docs_do_not_recommend_superseded_qwen_ladder():
    docs = [
        Path("docs/SPEC.md"),
        Path("docs/MONGOOSE_FULL_SPEC.md"),
        Path("docs/CONFIGURATION.md"),
        Path("docs/QA_AUDIT.md"),
        Path("docs/IMPLEMENTATION_STEPS.md"),
    ]
    combined = "\n".join(p.read_text() for p in docs)
    assert "qwen3:8b" not in combined
    assert "qwen3:14b" not in combined
    assert "OLLAMA_MODEL=qwen3:8b" not in combined
    assert "8B to 14B model sweet spot" not in combined


def test_active_docs_name_current_role_models():
    docs = [
        Path("docs/SPEC.md"),
        Path("docs/MONGOOSE_FULL_SPEC.md"),
        Path("docs/CONFIGURATION.md"),
    ]
    combined = "\n".join(p.read_text() for p in docs)
    assert "gemma4:26b-a4b-it-q4_K_M" in combined
    assert "gpt-oss:20b" in combined
    assert "qwen2.5-coder:14b-instruct-q4_K_M" in combined
    assert "OLLAMA_KEEP_ALIVE=-1" in combined


def test_docs_do_not_state_backwards_keepalive_or_gptoss_default():
    """Semantic guard: active docs must not claim KEEP_ALIVE=-1 unloads, nor
    present gpt-oss as the tool DEFAULT (it is an optional override)."""
    from pathlib import Path
    docs = [
        Path("README.md"),
        Path("docs/CONFIGURATION.md"),
        Path("docs/SPEC.md"),
        Path("docs/MONGOOSE_FULL_SPEC.md"),
        Path("configs/mongoose.yaml.example"),
    ]
    combined = "\n".join(p.read_text() for p in docs if p.exists()).lower()
    # backwards keep-alive claim: -1 keeps resident, it does NOT unload.
    # (Saying "0 unloads" is correct, so only flag the -1 form.)
    assert "-1 unloads" not in combined
    assert "keep_alive=-1 unloads" not in combined
    assert "keep_alive=-1` unloads" not in combined
    assert "role models unload between" not in combined
    # gpt-oss must not be described as THE tool/reasoning model (prose form)
    assert "gpt-oss:20b is the tool" not in combined
    # gpt-oss must only appear as an override/optional, never an uncommented default
    for p in docs:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("OLLAMA_TOOL_MODEL=gpt-oss"):
                raise AssertionError(f"{p} sets gpt-oss as the tool default (should be commented/override)")
