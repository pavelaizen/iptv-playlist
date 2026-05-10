from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Sequence

from app.admin_bootstrap import build_admin_context


def run_nightly() -> dict[str, object]:
    context = build_admin_context()
    return context.service.run_serialized_job(lambda: _run_nightly(context))


def _run_nightly(context) -> dict[str, object]:
    reload_results = [
        context.service.reload_epg_source(source.id)
        for source in context.store.list_epg_sources()
        if source.enabled
    ]
    validation = context.service.validate_all(trigger_type="scheduled")
    if validation.get("status") != "ok":
        return {
            "status": "validation_skipped_publish",
            "epg_reload": {"sources": reload_results},
            "validation": validation,
            "publish": {"status": "skipped"},
        }

    publish = context.service.publish_from_cache()
    return {
        "status": "ok",
        "epg_reload": {"sources": reload_results},
        "validation": validation,
        "publish": publish,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run playlist-admin one-shot jobs.")
    parser.add_argument("job", choices=["nightly"])
    args = parser.parse_args(argv)

    if args.job == "nightly":
        result = run_nightly()
    else:  # pragma: no cover - argparse enforces choices.
        raise AssertionError(f"Unhandled job {args.job!r}")

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(main())
