"""Live-gate contract of `python -m leadengine.discovery`, proven without any live execution.

Contract under test
  1. default `run`/`discover`/`enrich` is a DRY RUN: network sources are not run, nothing is constructed that could connect;
  2. `plan` never executes anything and never creates output;
  3. every missing/invalid confirmation or precondition is REFUSED with exit code 2 BEFORE execute(), before an Engine or Fetcher exists,
     before any DNS lookup, connection or HTTP request, and without creating an output directory;
  4. a fully confirmed run proceeds only when EVERY gate passes - and then only to a stub here, never to the real executor;
  5. the confirmed number is bound to what is executed (a changed input is refused);
  6. tests are isolated from the developer's environment: ambient LE_* variables (e.g. LE_DATABASE_URL) are removed.

Why (5)/(6): on a Windows machine with LE_DATABASE_URL set, an earlier version of this suite expected a "no database" refusal that never came,
because every gate had in fact been satisfied; the call proceeded. The gates were right; the test depended on the environment.
All candidate hosts are synthetic (*.fixture.test, or IANA-reserved example.org for a listing page); no real domain appears here.
"""
import json

import pytest

import leadengine.discovery.cli as cli
import leadengine.discovery.pipeline as pl
from leadengine.discovery.discover import discover as real_discover
from tests.netguard import clean_le_env, netguard  # noqa: F401

UA_OK = "FlowHQBot/1.0 (+https://flowhq.example/bot; ops@flowhq.example)"
DB_ARG = ["--database-url", "postgresql://gate:pw@dbhost.invalid:5433/enrich_gate_test"]
LIVE_OK = ["--live", "--confirm-terms-reviewed", "--confirm-count", "2"]


class Harness:
    """CLI test harness: synthetic candidates, a spy on execute(), and tripwires on everything that could reach the network or a database."""

    def __init__(self, monkeypatch, tmp_path, capsys, guard):
        self.mp, self.tmp, self.capsys, self.guard = monkeypatch, tmp_path, capsys, guard
        self.calls: list = []                    # every RunPlan handed to execute()
        self.constructed: list[str] = []         # Engine / Fetcher constructions
        self.real_execute_for_dry_runs = True
        self.csv = tmp_path / "cands.csv"
        self.csv.write_text("Website\nhttp://c1.fixture.test/\nhttp://c2.fixture.test/\n", encoding="utf-8")
        self.out = tmp_path / "out"
        allow = lambda *a, **kw: real_discover(*a, **{**kw, "allow_synthetic": True})      # *.fixture.test stands in for "valid candidates"
        monkeypatch.setattr(cli, "discover", allow)
        monkeypatch.setattr(pl, "discover", allow)
        for name in ("Engine", "Fetcher"):
            monkeypatch.setattr(pl, name, self._tripwire(name))
        real_execute = pl.execute

        async def spy(plan):
            self.calls.append(plan)
            if plan.authorized:                  # a confirmed LIVE plan is never executed by these tests
                return {"out_dir": "(not executed)", "mode": "stub"}
            return await real_execute(plan)      # dry runs: the real offline path, with the tripwires armed
        monkeypatch.setattr(cli, "execute", spy)
        self.spy_real = real_execute

    def _tripwire(self, name):
        def boom(*a, **k):
            self.constructed.append(name)
            raise AssertionError(f"{name} was constructed: something tried to contact the network/database")
        return boom

    def env(self, **kv):
        for k, v in kv.items():
            (self.mp.delenv(k, raising=False) if v is None else self.mp.setenv(k, v))

    def run(self, args):
        rc = cli.main(args)
        cap = self.capsys.readouterr()
        return rc, cap.out, cap.err

    def base(self, cmd="run", db=True):
        return [cmd, "--csv", str(self.csv), "--out-root", str(self.out)] + (DB_ARG if db else [])

    def assert_nothing_ran(self):
        assert self.calls == [], "execute() must not be reached by a refused invocation"
        assert self.constructed == [] and self.guard.attempts == []
        assert not self.out.exists(), "a refused invocation must not create an output directory"


@pytest.fixture
def h(monkeypatch, tmp_path, capsys, netguard, clean_le_env):
    h = Harness(monkeypatch, tmp_path, capsys, netguard)
    h.env(LE_USER_AGENT=UA_OK)
    return h


# ------------------------------------------------------------------ 1. default is a dry run
@pytest.mark.parametrize("cmd", ["run", "enrich"])
def test_default_run_and_enrich_are_dry_runs_that_construct_no_network_object(h, cmd):
    args = h.base("run") if cmd == "run" else ["enrich", "--candidates", str(h.csv), "--out-root", str(h.out)] + DB_ARG
    rc, out, err = h.run(args)
    assert rc == 0 and len(h.calls) == 1 and h.calls[0].authorized is False
    assert "DRY RUN complete" in err and "--live --confirm-terms-reviewed --confirm-count 2" in (
        next(h.out.iterdir()) / "discovery_report.md").read_text(encoding="utf-8")
    assert h.constructed == [] and h.guard.attempts == []
    d = next(h.out.iterdir())
    assert "Dry run: no website was contacted" in (d / "discovery_report.md").read_text(encoding="utf-8")
    assert not (d / "fetch_log.jsonl").exists() and not (d / "discovery_fetch_log.jsonl").exists()
    assert not (d / "enriched_leads.csv").read_text(encoding="utf-8-sig").strip().splitlines()[1:], "no company was enriched"


def test_offline_only_discovery_has_nothing_to_confirm_and_contacts_nothing(h):
    rc, out, err = h.run(["discover", "--csv", str(h.csv), "--out-root", str(h.out)])
    assert rc == 0 and h.calls[0].sources and not any(s.needs_network for s in h.calls[0].sources)
    assert h.constructed == [] and h.guard.attempts == []


def test_a_dry_run_with_network_sources_lists_them_and_runs_none(h):
    rc, out, err = h.run(["discover", "--seed-url", "http://listing.example.org/ai", "--osm", "--location", "Pune", "--out-root", str(h.out)])
    assert rc == 0 and h.calls[0].authorized is False and "DRY RUN complete" in err and "Targets a live run would contact: 2" in err
    rep = (next(h.out.iterdir()) / "discovery_report.md").read_text(encoding="utf-8")
    assert "NOT RUN (dry run)" in rep and "listing.example.org/ai" in rep and "Dry run: no website was contacted" in rep
    assert h.constructed == [] and h.guard.attempts == []


def test_network_source_with_nothing_to_contact_is_not_authorized_either(h):
    """--osm without --location plans zero requests; it must not be treated as 'nothing to confirm, go ahead'."""
    rc, out, err = h.run(["run", "--osm", "--out-root", str(h.out)])
    assert rc == 0 and h.calls[0].authorized is False
    assert h.constructed == [] and h.guard.attempts == []
    assert "nothing to contact" in (next(h.out.iterdir()) / "discovery_report.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ 2. plan
def test_plan_never_executes_never_creates_output_and_ignores_live_flags(h):
    for extra in ([], LIVE_OK, ["--confirm-count", "7"]):
        rc, out, err = h.run(["plan", "--csv", str(h.csv), "--seed-url", "http://listing.example.org/ai", "--osm", "--location", "Pune",
                              "--out-root", str(h.out)] + extra)
        assert rc == 0 and "Nothing was contacted" in out and "--confirm-count" in out
    h.assert_nothing_ran()


# ------------------------------------------------------------------ 3. every refusal: exit 2, before anything runs
REFUSALS = [
    ("live_without_terms", ["--live"], {}, "confirm-terms-reviewed"),
    ("live_terms_without_count", ["--live", "--confirm-terms-reviewed"], {}, "must equal"),
    ("count_too_low", ["--live", "--confirm-terms-reviewed", "--confirm-count", "1"], {}, "must equal"),
    ("count_too_high", ["--live", "--confirm-terms-reviewed", "--confirm-count", "3"], {}, "must equal"),
    ("count_zero", ["--live", "--confirm-terms-reviewed", "--confirm-count", "0"], {}, "must equal"),
    ("count_negative", ["--live", "--confirm-terms-reviewed", "--confirm-count", "-2"], {}, "must equal"),
    ("terms_without_live", ["--confirm-terms-reviewed"], {}, "only apply together"),
    ("count_without_live", ["--confirm-count", "2"], {}, "only apply together"),
    ("terms_and_count_without_live", ["--confirm-terms-reviewed", "--confirm-count", "2"], {}, "only apply together"),
    ("user_agent_missing", LIVE_OK, {"LE_USER_AGENT": None}, "LE_USER_AGENT"),
    ("user_agent_without_contact", LIVE_OK, {"LE_USER_AGENT": "Bot/1.0"}, "LE_USER_AGENT"),
    ("user_agent_placeholder", LIVE_OK, {"LE_USER_AGENT": "LeadEnrichmentBot/0.1 (+https://example.invalid/bot; set LE_USER_AGENT)"}, "LE_USER_AGENT"),
    ("host_overrides_set", LIVE_OK, {"LE_HOST_OVERRIDES": "*.x=127.0.0.1"}, "LE_HOST_OVERRIDES"),
]


@pytest.mark.parametrize("cmd", ["run", "enrich"])
@pytest.mark.parametrize("case,extra,env,msg", REFUSALS, ids=[r[0] for r in REFUSALS])
def test_every_refusal_returns_2_before_execute_engine_dns_or_output(h, cmd, case, extra, env, msg):
    h.env(**env)
    args = h.base("run") if cmd == "run" else ["enrich", "--candidates", str(h.csv), "--out-root", str(h.out)] + DB_ARG
    rc, out, err = h.run(args + extra)
    assert rc == 2, (case, out, err)
    assert msg in err, err
    h.assert_nothing_ran()


def test_no_database_is_refused_before_anything_runs_when_the_environment_has_none(h):
    rc, out, err = h.run(h.base(db=False) + LIVE_OK)
    assert rc == 2 and "no database" in err
    h.assert_nothing_ran()


def test_no_source_and_unparseable_count_are_refused(h, capsys):
    rc, out, err = h.run(["run", "--out-root", str(h.out)] + DB_ARG)
    assert rc == 2 and "no discovery source" in err
    with pytest.raises(SystemExit) as ei:                                     # argparse rejects a non-integer count itself
        cli.main(h.base() + ["--live", "--confirm-terms-reviewed", "--confirm-count", "two"])
    assert ei.value.code == 2
    capsys.readouterr()
    h.assert_nothing_ran()


@pytest.mark.parametrize("extra,msg", [(["--live"], "confirm-terms-reviewed"), (["--live", "--confirm-terms-reviewed"], "must equal"),
                                       (LIVE_OK, "must equal"), (["--confirm-count", "1"], "only apply together")])
def test_discovery_of_network_sources_is_gated_the_same_way(h, extra, msg):
    """One seed page = one target: count 2 is wrong, 1 would be right; --live alone is incomplete."""
    rc, out, err = h.run(["discover", "--seed-url", "http://listing.example.org/ai", "--out-root", str(h.out)] + extra)
    assert rc == 2 and msg in err
    h.assert_nothing_ran()


# ------------------------------------------------------------------ 4. a fully confirmed run proceeds only when every gate passes
def test_only_a_fully_confirmed_run_reaches_execute_with_forced_limits_and_a_visible_credential_free_database_target(h):
    h.env(LE_DATABASE_URL="postgresql://gate:s3cretpw@dbhost.invalid:5433/enrich_from_env")      # supplied by the ENVIRONMENT, not --database-url
    rc, out, err = h.run(["run", "--csv", str(h.csv), "--out-root", str(h.out)] + LIVE_OK)
    assert rc == 0 and len(h.calls) == 1
    plan = h.calls[0]
    assert plan.authorized is True and plan.max_company_targets == 2
    s = plan.settings
    assert (s.respect_robots, s.host_overrides, s.browser_enabled, s.ai_provider, s.allowed_ports) == (True, "", False, "", "80,443")
    assert s.per_domain_min_delay >= 2.0 and s.global_concurrency <= 4 and s.max_pages_per_company <= 3
    assert "LIVE run confirmed: up to 2 target(s)" in err and "dbhost.invalid:5433/enrich_from_env" in err
    assert "s3cretpw" not in err and "s3cretpw" not in out, "credentials must never be printed"
    assert h.constructed == [] and h.guard.attempts == [] and not h.out.exists()                # the stub executed nothing


def test_this_is_why_the_old_test_failed_the_environment_database_satisfies_the_last_gate(h):
    """Identical arguments, the only difference is LE_DATABASE_URL: unset -> refused (2); set -> every gate satisfied -> proceeds (0)."""
    args = ["run", "--csv", str(h.csv), "--out-root", str(h.out)] + LIVE_OK
    rc_unset, _, err_unset = h.run(args)
    assert rc_unset == 2 and "no database" in err_unset and h.calls == []
    h.env(LE_DATABASE_URL="postgresql://u@dbhost.invalid:5432/enrich_x")
    rc_set, _, _ = h.run(args)
    assert rc_set == 0 and len(h.calls) == 1 and h.calls[0].authorized is True


def test_database_target_helper_never_contains_credentials():
    from leadengine.discovery.safety import database_target
    from pydantic import SecretStr
    for url in ("postgresql://user:pw@h.invalid:5433/db1", SecretStr("postgresql://user:pw@h.invalid:5433/db1")):
        t = database_target(url)
        assert t == "h.invalid:5433/db1" and "pw" not in t and "user" not in t
    assert database_target(None) == "(none)" and database_target("postgresql://h.invalid/x") == "h.invalid:5432/x"


# ------------------------------------------------------------------ 5. the confirmed number is bound to what is executed
def test_input_that_grows_after_confirmation_is_refused_before_the_engine_or_any_contact(h):
    real = pl.execute

    async def tamper_then_real_execute(plan):                    # the CSV gains a row between counting (gate) and executing
        h.calls.append(plan)
        h.csv.write_text(h.csv.read_text() + "http://c3.fixture.test/\n", encoding="utf-8")
        return await real(plan)
    h.mp.setattr(cli, "execute", tamper_then_real_execute)
    h.env(LE_DATABASE_URL="postgresql://u@dbhost.invalid:5432/enrich_x")
    rc, out, err = h.run(["run", "--csv", str(h.csv), "--out-root", str(h.out)] + LIVE_OK)
    assert rc == 2 and "would contact 3 company site(s) but the confirmation covered 2" in err
    assert len(h.calls) == 1 and h.calls[0].authorized is True
    assert h.constructed == [] and h.guard.attempts == []
    assert not h.out.exists() or not any(h.out.iterdir()), "no output directory is left behind by the refusal"


def test_run_config_records_the_credential_free_database_target(h, tmp_path):
    """Non-live run through the real offline path: the configuration block names the database without credentials."""
    rc, out, err = h.run(h.base() + [])
    cfg = json.loads((next(h.out.iterdir()) / "run_config.json").read_text())
    assert cfg["database"] == "dbhost.invalid:5433/enrich_gate_test (credentials not recorded)" and "pw" not in json.dumps(cfg)


# ------------------------------------------------------------------ protected databases (e.g. a live pilot database)
@pytest.mark.parametrize("how", ["flag", "env", "env_list_mixed_case"])
def test_a_protected_database_is_refused_before_anything_runs_even_with_every_confirmation(h, how):
    url = "postgresql://gate:pw@dbhost.invalid:5433/Flowhq_Live_Pilot"
    extra = ["--protect-database", "flowhq_live_pilot"] if how == "flag" else []
    if how == "env":
        h.env(LE_DISCOVERY_PROTECTED_DATABASES="flowhq_live_pilot")
    if how == "env_list_mixed_case":
        h.env(LE_DISCOVERY_PROTECTED_DATABASES="other_db, FLOWHQ_LIVE_PILOT")
    rc, out, err = h.run(["run", "--csv", str(h.csv), "--out-root", str(h.out), "--database-url", url] + LIVE_OK + extra)
    assert rc == 2 and "protected list" in err and "pw" not in err
    h.assert_nothing_ran()


def test_the_protected_list_does_not_block_other_databases_and_comes_from_the_environment_database_too(h):
    h.env(LE_DISCOVERY_PROTECTED_DATABASES="flowhq_live_pilot", LE_DATABASE_URL="postgresql://u@dbhost.invalid:5432/enrich_discovery")
    rc, out, err = h.run(["run", "--csv", str(h.csv), "--out-root", str(h.out)] + LIVE_OK)
    assert rc == 0 and h.calls[0].authorized is True and "enrich_discovery" in err
    h.calls.clear()
    h.env(LE_DATABASE_URL="postgresql://u@dbhost.invalid:5432/flowhq_live_pilot")              # the AMBIENT database is the protected one
    rc, out, err = h.run(["run", "--csv", str(h.csv), "--out-root", str(h.out)] + LIVE_OK)
    assert rc == 2 and "protected list" in err and h.calls == []
