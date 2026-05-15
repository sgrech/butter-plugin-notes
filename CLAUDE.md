# butter-plugin-notes

Persistent free-form note capture for [butter-agent](https://github.com/sgrech/butter-agent). The reference `local-write` plugin.

This file is loaded into every Claude Code session here. Keep it short — the user's global `~/.claude/CLAUDE.md` covers session-start, work-loop, git/PR attribution, and MCP usage. Only project-specific guidance belongs here.

## The plugin contract

butter-agent loads `manifest.toml` from this repo's root and resolves `entrypoint = "module:Class"` to a class whose instances satisfy the `Plugin` Protocol (`butter_agent.plugin_api.Plugin`):

```python
class Plugin(Protocol):
    async def execute(self, capability: str, inputs: dict[str, object], context: PluginContext) -> dict[str, object]: ...
```

Three args after `self` — `capability, inputs, context`. The host's loader rejects any other arity (the legacy two-arg form is unsupported). The Protocol is structural — `NotesPlugin` does not inherit from it. `butter_agent.plugin_api` is imported only under `TYPE_CHECKING` (for `PluginContext`) and at test time (`parse_manifest`, `Plugin`); runtime stays stdlib-only.

## Architecture invariants

1. **Local-write blast radius via the shared store.** Notes owns no SQLite file. It persists by calling the host's `database` plugin through `context.call("database.{define_table,insert,select}", ...)`. `manifest.toml` declares `blast_radius = "local-write"` and `requires` those three internal capabilities — the host rejects a call to anything not in `requires`.
2. **Never sees the namespace prefix.** Notes passes the **bare** table name `entries`. The host's core rewrites it to `notes__entries` using the manifest `name` as owner identity (invariant #6). Do not construct, send, or parse the `notes__` prefix here.
3. **`created_at` is always written.** The `database` plugin does not emit column defaults into DDL and `created_at` is `NOT NULL`. The plugin supplies it: verbatim when the plan chained `clock.now → notes.create` (the `$t.time` variable-pool value), self-generated ISO-8601 UTC otherwise.
4. **Contract breaks surface as `NotesPluginError`.** A malformed `database.*` result (non-int id, non-mapping/incomplete row) raises a descriptive `NotesPluginError`, never a bare `KeyError`. The host records it as the step `failure_reason`; the loop still synthesises.
5. **Manifest is the source of truth for shape.** Capability names/inputs/outputs and `requires` must round-trip through the host's `parse_manifest` (`tests/test_plugin.py::test_manifest_round_trips_through_butter_validator`).

## Capability set

- `create(content, created_at?)` → `{note_id, created_at}` — gated `confirm` by the planner per step (the manifest carries no gate; gate is a plan concern enforced by the host's core).
- `list(limit?)` → `{notes: [{id, content, created_at}]}` — oldest-first.
- `read(note_id)` → `{content, created_at}` — raises on unknown id.
- `delete(note_id)` → `{note_id}` — raises on unknown id (same stance as `read`); calls `database.delete`.
- `search(query, limit?)` → `{notes: [{id, content, created_at}]}` — case-insensitive substring of `content`, oldest-first; `limit` caps matches. Filtered in Python (`database.select` `where` is equality-only).

New capabilities follow the same shape: declare in `manifest.toml`, implement `_<name>(inputs, context)`, branch in `execute`, add tests covering happy path + at least one input-validation failure + (if it persists) the `database.*` call shape.

## Development workflow

- `uv sync` — install dev deps (editable butter-agent for the Protocol types + manifest validator).
- `just check` — ruff (lint + format check) + mypy --strict + pytest.
- `just fix` — auto-fix ruff.

CI parity = `just check` green locally before pushing. No separate CI config.

## Versioning

Semver. Three version sources must move together in one commit: `[plugin].version` in `manifest.toml`, `__version__` in `__init__.py`, and `[project].version` in `pyproject.toml`. (Manifest/`__version__` drive the host's plugin contract; `pyproject.toml` drives the built wheel/sdist — leaving it stale publishes a mislabelled artifact.) The host pins `[[plugin]] source = "...@vX.Y.Z"` against it.
