---
name: migration
description: Migrate a deployed akgentic store after upgrading the packages (submodule tag bump or PyPI release) — the team event store (migrate_yaml / migrate_mongo / migrate_postgres) and the YAML catalog (removed or moved cards, 409 on team creation). Use after a package upgrade, when teams report "Team {id} not found", or when creating a team fails with a catalog conflict.
argument-hint: [event store path or backend] [catalog root]
allowed-tools: Bash, Read, Grep, Glob, Write, Edit
---

# Akgentic Store Migration

An upgrade touches **two stores**, and each breaks with a symptom that does not name its cause:

| Store | Where (community tier) | Symptom when unmigrated |
|---|---|---|
| Team event store | `data/event_store` (`AKGENTIC_EVENT_STORE_PATH`) | Existing teams answer **`Team {id} not found`** / vanish from `list_teams` — they are **not** deleted |
| Catalog | `data/catalog` (`AKGENTIC_CATALOG_PATH`) | `POST /teams` answers **409** — a card class named by the catalog no longer imports |

Do both, in the order below. Every step says how to verify it; do not skip a verification.

---

## 0. Know what changed

1. List the version jump per package: `git submodule status` before/after, or
   `git -C packages/<pkg> log --oneline <old-tag>..<new-tag>`.
2. Read the migration sections of the upgraded packages — **they are the source of truth,
   this skill only sequences them**:
   - `packages/akgentic-team/README.md` → *Upgrading a deployed store* (event store scripts)
   - `packages/akgentic-tool/README.md` → *Migration: moved import paths* (withdrawn / removed
     cards, with what to use instead) and *Deprecating a card*
   - `packages/akgentic-catalog/README.md`, `packages/akgentic-infra/README.md` — grep `-i migrat`

## 1. Stop everything that uses the stores

The event-store migration is **not** a rolling step: old code cannot read a migrated document
and new code cannot resume an unmigrated one. Stop the server **and every worker** first. A
fleet upgraded replica by replica gives "some teams resume, some do not" with no error.

## 2. Back up

```bash
cp -R data/event_store data/event_store.vN      # next free N — earlier backups exist as .v1, .v2, …
```

The catalog (`data/catalog`, `data/catalog-import`) is tracked in git — `git stash` / `git
checkout` is its backup. Start from a clean `git status data/`.

## 3. Migrate the event store

Pick the script for the backend (all three are idempotent — re-running skips converted docs):

| Backend | Command |
|---|---|
| YAML | `uv run python -m akgentic.team.scripts.migrate_yaml --data-dir data/event_store` |
| MongoDB | `uv run python -m akgentic.team.scripts.migrate_mongo` (`MONGO_URI`, `MONGO_DB`, or `--mongo-uri` / `--mongo-db`) |
| PostgreSQL | `uv run python -m akgentic.team.scripts.init_db` **first** if the store predates the card table, then `uv run python -m akgentic.team.scripts.migrate_postgres` (`DB_CONN_STRING_PERSISTENCE` or `--conn-string`) |

`--data-dir` must be the directory the server's `YamlEventStore` uses — check
`AKGENTIC_EVENT_STORE_PATH`; the default is `data/event_store`.

Exit codes: **0** all converted or skipped · **1** some documents failed (each logged with its
`team_id`; everything else was converted — fix and re-run) · **2** configuration missing.

Run `--help` first when the package version is new to you: the scripts restate their own
ordering constraints there, and newer releases may add scripts not listed here.

## 4. Migrate the catalog

### 4.1 Find what is broken

```bash
uv run python .claude/skills/migration/scripts/check_model_types.py data/catalog
uv run python .claude/skills/migration/scripts/check_catalog.py data/catalog
```

- `check_model_types.py` lists every `model_type` that no longer imports, and the entries
  naming it. **Read the error text**: a removed card's `ImportError` names its replacement.
- `check_catalog.py` resolves every team namespace exactly as `POST /teams` does. A `FAIL`
  here is a 409 in the UI. `SKIP` for `global` (no team entry) is expected.

Then find every consumer of a broken entry id — including cross-namespace refs:

```bash
grep -rnE "__ref__: (global\.)?<entry_id>$" data/catalog
```

### 4.2 Fix `data/catalog` — the rules

`data/catalog/` (file per entry) is what the server reads. Edit it; the bundles are
regenerated from it in 4.3, never edited by hand.

1. **Never widen what an agent can do.** Replacement cards often default capabilities to ON
   (`WorkspaceTool` turns every file operation on by default). Fold a removed capability into
   an existing entry only when **every** agent referencing that entry had the capability
   before. Otherwise create a dedicated copy of the entry for the agents that had it, and
   point only them at it.
2. **Carry the configuration across**: `mode`, `instructions`, limits — the old payload's
   settings move into the new param, not dropped.
3. **Delete tracked entries with `git rm`**, not `rm` — reversible, and it stages the removal.
4. **Update the prose that names the removed card**: agent prompts that tell the model to call
   the old tool name (`exec_command` → `workspace_exec`), entry `description`s, and the
   namespace `meta/_meta.yaml`. A prompt naming a tool that no longer exists makes the agent
   call a missing function. Historical notes that stay true may remain.
5. Re-run both checks until `check_model_types.py` reports 0 broken and every team is `OK`.
6. Confirm the new params were actually **resolved**, not silently ignored: load the team
   and inspect the tool object (e.g. `WorkspaceTool.workspace_exec` is a `WorkspaceExec`
   with your `instructions`), rather than trusting a clean validate.

### 4.3 Regenerate and validate the bundles

```bash
for ns in $(ls data/catalog); do
  uv run ak-catalog --root data/catalog export --namespace "$ns" > "data/catalog-import/catalog.$ns.yaml"
  uv run ak-catalog --root data/catalog validate "data/catalog-import/catalog.$ns.yaml"
done
uv run ak-catalog --root data/catalog validate --namespace <ns>   # per namespace, strict
```

(Claude Code: the `$(...)` loop needs approval — iterate the namespaces with backticks or
one command per namespace instead.)

The export is the canonical form: if `data/catalog-import` was stale before the migration the
diff will include those earlier catalog changes too — say so when reporting.

### 4.4 Known card migrations

| Removed | Release | Replacement |
|---|---|---|
| `akgentic.tool.vector_store.VectorStoreTool` | tool 1.10 | **No card.** Delete the entry and every `__ref__` to it. `PlanningTool`, `KnowledgeGraphTool` and `WorkspaceTool` each carry their own `vector_store: VectorStoreParam` (`false` = keyword-only); the backend follows `AKGENTIC_WEAVIATE_URL` |
| `akgentic.tool.sandbox.ExecTool` | tool 1.10 | `WorkspaceTool(workspace_exec=WorkspaceExec(mode=…, instructions=…))` on the agent's workspace entry — same directory, same sandbox backends. Exec defaults **off**; the tool the model calls is `workspace_exec` (+ `workspace_exec_result`), no longer `exec_command`. Rule 1 applies: a workspace entry shared with agents that had no exec gets a dedicated copy |

Add a row here whenever a migration teaches a new mapping.

## 5. Restart and verify

The YAML catalog repository **caches each namespace in memory** — a running server does not
see catalog edits. Start the whole fleet with the new version, then:

- existing teams are listed and resume (event store migrated),
- creating a team from each namespace returns 201 (catalog migrated).

## 6. Report and record

Report per store: what ran, exit code / check output, files changed. The catalog changes are
a normal git change in this repo — commit on a branch linked to an issue (see CLAUDE.md), with
the `git rm`s, the edited entries and the regenerated bundles together. Do not commit the
event store or its backups.
