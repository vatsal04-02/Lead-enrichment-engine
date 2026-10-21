"""python -m leadengine.profiles INPUT.csv [-o OUT.csv]      (also: python -m leadengine profile ...)

Reads company names + websites from a CSV, enriches each with a local Ollama model through ScrapeGraphAI, writes a NEW CSV. The input is never
modified, no database is used, nothing leaves your machine except the polite requests to the company websites in your CSV (robots.txt respected).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from ..backends import BackendConfigError, BackendUnavailable
from ..compat import configure_event_loop
from ..discovery.doctor import run_doctor
from ..discovery.safety import DiscoveryError
from .runner import DEFAULT_MODEL, DEFAULT_URL, ProfileConfig, ProfileError, run_profiles


def configure_logging(log_file: str | None) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root = logging.getLogger("leadengine.profiles")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(fmt)
    root.addHandler(h)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m leadengine.profiles", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("input", nargs="?", help="CSV with a company name column and a website column (headers are auto-detected)")
    p.add_argument("-o", "--output", help="new CSV to write (default: profile_out/<input>.enriched-<time>.csv); never overwrites, never the input")
    p.add_argument("--mapping", help='column mapping JSON if auto-detection fails, e.g. {"website":"Company URL","name":"Company"}')
    p.add_argument("--model", default=DEFAULT_MODEL); p.add_argument("--ollama-url", default=DEFAULT_URL)
    p.add_argument("--sg-python", default=os.environ.get("LE_SCRAPEGRAPH_PYTHON", ""),
                   help="Python >= 3.12 that has scrapegraphai installed (default: env LE_SCRAPEGRAPH_PYTHON, else this interpreter)")
    p.add_argument("--model-timeout", type=float, default=300.0, help="seconds per model call (CPU inference of a 4B model is slow)")
    p.add_argument("--retries", type=int, default=1, help="model retries after a timeout/error (0-2)")
    p.add_argument("--max-companies", type=int, default=25, help="safety cap on companies processed in one run (default 25; raise it deliberately)")
    p.add_argument("--min-delay", type=float, default=3.0, help="seconds between requests to one website (minimum enforced: 2)")
    p.add_argument("--max-pages", type=int, default=4, help="pages per company: Home + About + Services + Contact (max 4)")
    p.add_argument("--log-file", help="also write the log to this file")
    p.add_argument("--dry-run", action="store_true", help="validate the CSV and print what would be fetched; contacts nothing")
    p.add_argument("--check", action="store_true", help="only verify Ollama connectivity and that the model is installed (loopback only), then exit")
    p.add_argument("--smoke", action="store_true", help="with --check: also run one extraction on a built-in synthetic page")
    return p


def main(argv=None) -> int:
    a = build_parser().parse_args(argv)
    configure_event_loop()
    configure_logging(a.log_file)
    try:
        if a.check:
            r = run_doctor(model=a.model, base_url=a.ollama_url, python=a.sg_python, smoke=a.smoke)
            print(json.dumps(r, indent=2))
            return 0 if r["ready_for_scrapegraph_ollama"] else 1
        if not a.input:
            raise ProfileError("give an input CSV, or use --check")
        cfg = ProfileConfig(input_path=a.input, output_path=a.output, mapping=json.loads(a.mapping) if a.mapping else None, model=a.model,
                            base_url=a.ollama_url, sg_python=a.sg_python, model_timeout=a.model_timeout, retries=a.retries,
                            max_companies=a.max_companies, min_delay=a.min_delay, max_pages=a.max_pages, dry_run=a.dry_run)
        res = asyncio.run(run_profiles(cfg))
        print(json.dumps(res, indent=2))
        return 1 if res.get("model_unavailable_mid_run") else 0
    except (ProfileError, DiscoveryError) as e:
        print(e, file=sys.stderr)
        return 2
    except (BackendUnavailable, BackendConfigError) as e:
        print(f"error: the local model is not usable, so no website was contacted: {e}\n"
              f"Check it with:  python -m leadengine.profiles --check --model {a.model} --ollama-url {a.ollama_url}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
