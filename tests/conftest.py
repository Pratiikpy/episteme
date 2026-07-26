"""pytest bootstrap — makes top-level episteme modules importable and isolates artifacts/keys."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Isolate signing key + artifact dir into a temp location for tests.
_tmp = tempfile.mkdtemp(prefix="episteme_test_")
os.environ.setdefault("EPISTEME_SIGNING_KEY", str(Path(_tmp) / "test_ed25519.key"))
os.environ.setdefault("EPISTEME_ARTIFACT_DIR", str(Path(_tmp) / "artifacts"))

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def registry():
    from nodes import build_registry
    return build_registry()


@pytest.fixture(scope="session")
def runtime(registry):
    from runtime import Runtime
    return Runtime(registry)
