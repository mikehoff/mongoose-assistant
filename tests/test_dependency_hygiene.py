"""Dependency hygiene: every third-party import in src must be installable from BOTH
declaration files.

The external audit's release-blocker: rtm_trust.py imports reportlab and pypdf (the RTM
receipt/evidence money path), but neither was declared anywhere, so a fresh install crashed on
receipt rendering. Worse, pyproject.toml and requirements.txt had drifted (cryptography, psutil,
pynacl were pyproject-only), so `pip install -r requirements.txt` was not a reliable install
route. This gate pins the whole class: it scans src for third-party imports and asserts each is
declared in pyproject's dependencies (core or optional) AND in requirements.txt.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

SRC = pathlib.Path("src/mongoose")

# distribution name (declared) -> import name (in code)
_DIST_TO_IMPORT = {
    "pyyaml": "yaml", "python-dotenv": "dotenv", "pynacl": "nacl",
    "google-api-python-client": "googleapiclient", "google-auth": "google",
    "google-auth-oauthlib": "google_auth_oauthlib", "python-docx": "docx",
    "uvicorn[standard]": "uvicorn", "pypdfium2": "pypdfium2",
}

# Imports that are deliberately optional AND the code degrades gracefully without them, or that
# refer to sibling local packages/dev tooling. Every entry needs a justification.
_ALLOWED_UNDECLARED = {
    "qrcode", "zeroconf",       # declared under [project.optional-dependencies].devices; soft
    "pytest",                   # test-time only
    "websockets",               # optional transport; guarded import with fallback
    "PIL",                      # only via optional qrcode[pil] path
    "mcp",                      # MCP SDK: only needed to RUN the MCP server; guarded import,
                                # tool registry imports fine without it (comment in mcp_server.py)
    "onnxruntime", "tokenizers", "numpy",  # optional [embeddings] extra; embeddings.py degrades
                                # to None and every caller falls back (Jaccard, filename search)
}


def _declared() -> tuple[set[str], set[str]]:
    py = pathlib.Path("pyproject.toml").read_text()
    reqs = pathlib.Path("requirements.txt").read_text()
    def names(text: str) -> set[str]:
        # Match every requirement-shaped token anywhere (pyproject lists several per line in
        # optional-dependency groups, so a line-anchored pattern misses all but the first).
        out = set()
        for m in re.finditer(r'(?:^|["\s])([A-Za-z0-9][A-Za-z0-9_.\[\]-]*?)\s*(?:>=|==|<=|~=)', text, re.M):
            out.add(m.group(1).lower())
        return out
    return names(py), names(reqs)


def _import_name_to_dist(name: str, declared: set[str]) -> bool:
    n = name.lower()
    if n in declared:
        return True
    for dist in declared:
        if _DIST_TO_IMPORT.get(dist) == name or dist.replace("-", "_") == n:
            return True
    return False


def _third_party_imports() -> set[str]:
    stdlib = set(sys.stdlib_module_names)
    found = set()
    for p in SRC.rglob("*.py"):
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods = [node.module.split(".")[0]]
            for m in mods:
                if m and m not in stdlib and m != "mongoose":
                    found.add(m)
    return found


def test_every_third_party_import_is_declared_in_both_files():
    py_deps, req_deps = _declared()
    missing = []
    for mod in sorted(_third_party_imports()):
        if mod in _ALLOWED_UNDECLARED:
            continue
        in_py = _import_name_to_dist(mod, py_deps)
        in_req = _import_name_to_dist(mod, req_deps)
        if not (in_py and in_req):
            missing.append(f"{mod} (pyproject={'yes' if in_py else 'NO'}, "
                           f"requirements={'yes' if in_req else 'NO'})")
    assert not missing, (
        "Third-party imports not declared in BOTH pyproject.toml and requirements.txt. "
        "A fresh install will crash on these paths, exactly like the reportlab/pypdf "
        f"receipt bug: {missing}")


def test_the_original_audit_findings_stay_fixed():
    py = pathlib.Path("pyproject.toml").read_text()
    req = pathlib.Path("requirements.txt").read_text()
    for dep in ("reportlab", "pypdf", "cryptography", "psutil", "pynacl"):
        assert dep in py, f"{dep} missing from pyproject"
        assert dep in req, f"{dep} missing from requirements.txt"



def test_capabilities_yaml_qa_matches_pyproject_dev_deps():
    """The QA capability tells the installer which pip packages make the test suite runnable.
    The audit found pytest-timeout missing there while the suite uses --timeout everywhere,
    so a capability-driven install produced 'unknown option' warnings. The three manifests
    (pyproject dev group, capabilities.yaml qa pips) must tell the same story."""
    import pathlib as _pl
    import re as _re
    caps = _pl.Path("configs/capabilities.yaml").read_text()
    m = _re.search(r"qa:.*?pip:\s*\[([^\]]+)\]", caps, _re.S)
    assert m, "qa pip list not found in capabilities.yaml"
    qa_pips = {x.strip() for x in m.group(1).split(",")}
    py = _pl.Path("pyproject.toml").read_text()
    for pkg in qa_pips:
        assert pkg in py, f"{pkg} in capabilities.yaml qa but not declared in pyproject.toml"
    assert "pytest-timeout" in qa_pips, "the suite uses --timeout; QA capability must install it"
