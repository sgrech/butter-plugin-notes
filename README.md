# butter-plugin-notes

Persistent free-form note capture for [butter-agent](https://github.com/sgrech/butter-agent). Stdlib-only at runtime — persistence goes through the host's shared `database` plugin, not a private file.

This is the worked example a `local-write` plugin author copies: it declares `requires`, is gated by the host's `confirm` path, and participates in the variable-pool chain (`clock.now → notes.create`).

## Capabilities

| Name | Inputs | Output | Notes |
|------|--------|--------|-------|
| `create` | `content`, optional `created_at` | `{note_id, created_at}` | `created_at` is used verbatim when chained from `clock.now`; self-generated (ISO-8601 UTC) otherwise. |
| `list` | optional `limit` | `{notes: [{id, content, created_at}]}` | Oldest-first. |
| `read` | `note_id` | `{content, created_at}` | Raises if the id is unknown. |

`blast_radius = "local-write"`. `requires = ["database.define_table", "database.insert", "database.select"]` — notes never touches SQL; the host's `database` plugin owns the file and core namespaces the table to `notes__entries` (the plugin only ever passes the bare name `entries`).

## Installing into a butter-agent host

External plugins are **opt-in** — declare it in your `config.toml`. Production (pinned ref):

```toml
[[plugin]]
source = "github.com/sgrech/butter-plugin-notes@v0.1.0"
```

Local development (no fetch, used as-is):

```toml
[[plugin]]
path = "~/Workspace/butter-plugin-notes"
```

The host registers its built-in `database` plugin before external plugins, so `notes`'s `requires` resolve at registry build. No data migration is needed if you previously ran notes as a host built-in — the namespace (owner identity = manifest `name` = `notes`) is unchanged, so the existing `notes__entries` table is read identically.

## Repo layout (reference scaffold for plugin authors)

```
butter-plugin-notes/
├── pyproject.toml          # uv + hatchling. butter-agent is a *dev* dep
│                           # only (typing + manifest validator); runtime
│                           # is stdlib.
├── manifest.toml           # Parsed by butter-agent at startup.
├── src/butter_plugin_notes/
│   ├── __init__.py         # Re-exports the entrypoint class.
│   └── plugin.py           # NotesPlugin satisfies the Plugin Protocol.
├── tests/test_plugin.py    # pytest + asyncio_mode=auto; inline fake context.
├── justfile                # Quality gates: ruff, mypy --strict, pytest.
├── CLAUDE.md               # Project instructions for AI sessions.
├── README.md               # This file.
└── .gitignore
```

The contract: butter-agent loads `manifest.toml` from the repo root, resolves `entrypoint = "module:Class"` to a callable whose instances have `async execute(self, capability, inputs, context) -> dict` (structural Protocol — no inheritance), and rejects the plugin if its declared `blast_radius` exceeds the host's `core.max_blast_radius` or its `requires` don't resolve to `internal` capabilities.

> **Protocol note:** `execute` takes three args (`capability, inputs, context`). The `context` is the host-injected `PluginContext`; a write plugin calls `await context.call("database.insert", {...})` through it. (The older two-arg `execute(self, capability, inputs)` form is not supported by current butter-agent.)

## Development

```bash
uv sync                     # install dev deps (incl. butter-agent editable)
just check                  # ruff + mypy --strict + pytest
just fix                    # auto-fix ruff issues
```

## License

MIT.
