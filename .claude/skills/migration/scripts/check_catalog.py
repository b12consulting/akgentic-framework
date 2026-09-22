"""Resolve every team namespace of a YAML catalog exactly as the server does on POST /teams.

Usage: uv run python .claude/skills/migration/scripts/check_catalog.py [data/catalog]

A namespace that fails here is the one that answers 409 on team creation. Library
namespaces holding no team entry (e.g. ``global``) are reported as SKIP, not failures.
Exit code 1 when at least one team namespace fails to load.
"""

import sys
from pathlib import Path

from akgentic.catalog import Catalog, YamlEntryRepository

root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/catalog")
catalog = Catalog(repository=YamlEntryRepository(root=root))

failed = 0
for ns in sorted(p.name for p in root.iterdir() if p.is_dir()):
    if not any((root / ns / "team").glob("*.yaml")):
        print(f"SKIP  {ns} (no team entry — library namespace)")
        continue
    try:
        catalog.load_team(ns)
        print(f"OK    {ns}")
    except Exception as exc:  # report every failure, keep going
        failed += 1
        print(f"FAIL  {ns}: {type(exc).__name__}: {exc}")
        for err in getattr(exc, "errors", None) or []:
            print(f"      - {err}")

sys.exit(1 if failed else 0)
