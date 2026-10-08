# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
# ]
# ///
"""Bring the DSF rule overview up to date.

    uv run update.py                  # download new minutes, analyse what changed, render regelsaet/
    uv run update.py --offline        # skip styrke.dk, use the files already in referater/
    uv run update.py --render-only    # only rebuild the Markdown from data/

Steps: 1) scrape.py downloads new documents and writes data/manifest.json.
2) analyze.py asks Claude (via the `claude` CLI) to extract decisions from new or changed
documents and to consolidate them per category into rule histories; both are cached in data/.
3) render.py writes regelsaet/<år>.md, regelsaet/beslutningslog.md and regelsaet/README.md.
"""

from __future__ import annotations

import argparse
import logging
import re
from datetime import date

import analyze
import render
import scrape


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="spring download over")
    parser.add_argument("--render-only", action="store_true", help="kun Markdown ud fra data/")
    parser.add_argument("--only", metavar="REGEX", help="udtræk kun dokumenter hvis id matcher (til test)")
    parser.add_argument("--extract-model", default="sonnet", help="model til udtræk pr. dokument (default: sonnet)")
    parser.add_argument("--consolidate-model", default="opus", help="model til konsolidering (default: opus)")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], help="effort til Claude-kald")
    parser.add_argument("--workers", type=int, default=4, help="parallelle Claude-kald (default: 4)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    docs = scrape.load_manifest() if args.offline or args.render_only else scrape.sync()

    failures = 0
    if not args.render_only:
        targets = [d for d in docs if re.search(args.only, d.id)] if args.only else docs
        failures += analyze.extract(targets, model=args.extract_model, effort=args.effort, workers=args.workers)
        failures += analyze.consolidate(
            analyze.load_decisions(docs),
            {d.id: d.organ_label for d in docs},
            model=args.consolidate_model,
            effort=args.effort,
            workers=args.workers,
        )

    pages = render.render(docs, analyze.load_decisions(docs), analyze.load_rules(),
                          analyze.missing_extractions(docs), date.today())
    logging.info("Skrev %d sider i %s", len(pages), render.OUT_DIR.relative_to(scrape.ROOT))
    if failures:
        # Partial results are cached and the pages are written; fail so CI reports it.
        raise SystemExit(f"{failures} Claude-kald fejlede – kør igen for at prøve dem igen.")


if __name__ == "__main__":
    main()
