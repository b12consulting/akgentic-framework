"""List every catalog ``model_type`` that no longer imports under the installed packages.

Usage: uv run python .claude/skills/migration/scripts/check_model_types.py [data/catalog]

A card removed or moved between releases leaves catalog rows naming a class that is gone.
The import error usually names the replacement — read it, it is the migration instruction.
Exit code 1 when at least one model_type is broken.
"""

import importlib
import sys
from collections import defaultdict
from pathlib import Path

import yaml

root = Path(sys.argv[1] if len(sys.argv) > 1 else "data/catalog")
uses: dict[str, list[str]] = defaultdict(list)
for f in sorted(root.rglob("*.yaml")):
    doc = yaml.safe_load(f.read_text()) or {}
    mt = doc.get("model_type") if isinstance(doc, dict) else None
    if mt:
        uses[mt].append(str(f.relative_to(root)))

broken = 0
for mt, files in sorted(uses.items()):
    mod, _, name = mt.rpartition(".")
    try:
        getattr(importlib.import_module(mod), name)
        continue
    except Exception as exc:  # any failure to resolve is a finding
        broken += 1
        print(f"BROKEN {mt}\n    {type(exc).__name__}: {exc}")
    for f in files:
        print(f"    - {f}")

print(f"{len(uses)} model types checked, {broken} broken")
sys.exit(1 if broken else 0)
