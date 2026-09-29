"""Paper-only hard rules, checked mechanically rather than by promise alone:

- No broker/live-order endpoint literal anywhere in research_agent's source, derived
  from the app's own paper constant (never spelled out here either, in either module).
- research_agent imports no broker-order-placing or risk-authorization module from
  catalyst_lab: it is read-only market data plus report building, never an execution
  path, and it never even approaches the one-use authorization gate.
- No token-shaped secret is hardcoded in the package's own source.
"""

import ast
import pathlib

from catalyst_lab.config import PAPER_ENDPOINT

PACKAGE_DIR = pathlib.Path(__file__).resolve().parents[1] / "research_agent"
LIVE_ENDPOINT = PAPER_ENDPOINT.replace("paper-", "")
# Modules that place, amend, cancel or authorize a broker order; research_agent must
# import none of them.
FORBIDDEN_MODULES = frozenset({
    "catalyst_lab.alpaca", "catalyst_lab.crypto_execution", "catalyst_lab.execution",
    "catalyst_lab.managed_broker", "catalyst_lab.managed_execution", "catalyst_lab.authorization",
})


def _source_files():
    files = sorted(PACKAGE_DIR.glob("*.py"))
    assert files, "research_agent package sources not found where expected"
    return files


def test_no_source_file_contains_the_live_broker_endpoint_literal():
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        assert LIVE_ENDPOINT not in text, path
        assert "/v2/orders" not in text, path


def test_no_module_imports_a_broker_order_placing_catalyst_lab_module():
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in FORBIDDEN_MODULES, (path, node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in FORBIDDEN_MODULES, (path, alias.name)


def test_no_hardcoded_credential_shaped_literal_in_source():
    suspicious = ("Bearer sk-", "Bearer ghp_", "xoxb-", "AKIA", "-----BEGIN")
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        for needle in suspicious:
            assert needle not in text, (path, needle)
