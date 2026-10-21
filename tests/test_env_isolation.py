"""The test suite must not depend on (or be broken by) the developer's own configuration.

Regression for a Windows-only full-suite failure: tests/test_hardening.py::test_placeholder_user_agent_refuses_to_start_without_overrides
builds Settings() from class defaults and expects the placeholder user agent to be refused. pydantic-settings resolves values as
kwargs > LE_* environment > .env file > defaults, so a session that exported LE_USER_AGENT (needed for live runs), LE_HOST_OVERRIDES, or had a
.env file replaced the placeholder, nothing was refused, and the test failed with "DID NOT RAISE ConfigError". The application's validation
was never broken. conftest.py now applies tests.netguard.clean_le_env to every test.
"""
import os
import subprocess
import sys
from pathlib import Path

from leadengine.config import Settings

ROOT = Path(__file__).resolve().parent.parent
UA = "FlowHQBot/1.0 (+https://flowhq.example/bot; ops@flowhq.example)"
HOSTILE_TEST = "tests/test_hardening.py::test_placeholder_user_agent_refuses_to_start_without_overrides"


def test_settings_defaults_inside_a_test_are_independent_of_the_ambient_environment():
    s = Settings()
    assert "example.invalid" in s.user_agent and s.host_overrides == ""
    assert Settings.model_config["env_file"] is None, ".env must not be read by tests"
    assert not [k for k in os.environ if k.startswith("LE_") and not k.startswith("LE_TEST_")]


def test_the_previously_failing_test_passes_under_a_hostile_session_environment_and_dotenv(tmp_path):
    """Run it in a child pytest whose environment exports LE_USER_AGENT / LE_HOST_OVERRIDES / LE_BUDGET_PAGES and whose working directory
    contains a .env that sets LE_USER_AGENT too. (Values are dummies; nothing here is a secret.)"""
    (tmp_path / ".env").write_text(f"LE_USER_AGENT={UA}\nLE_HOST_OVERRIDES=*.x=127.0.0.1\n", encoding="utf-8")
    env = {**os.environ, "LE_USER_AGENT": UA, "LE_HOST_OVERRIDES": "*.x=127.0.0.1", "LE_BUDGET_PAGES": "5",
           "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    env.pop("PYTEST_CURRENT_TEST", None)
    r = subprocess.run([sys.executable, "-m", "pytest", str(ROOT / HOSTILE_TEST), "-q", "-p", "no:cacheprovider",
                        "-c", str(ROOT / "pyproject.toml"), "--rootdir", str(ROOT)],
                       cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "1 passed" in r.stdout, r.stdout[-1500:] + r.stderr[-500:]
