"""NotesPlugin — persistent free-form note capture for butter-agent.

The first `local-write` plugin pattern and the worked example a write
plugin copies. Notes owns no SQLite file and never sees raw SQL: it
persists entirely through the host's shared `database` plugin via
`PluginContext.call` into `database.{define_table,insert,select}`,
passing the **bare** table name `"entries"`. The host's core rewrites it
to `notes__entries` before dispatch (invariant #6 — namespace isolation
is core's, not this module's; the owner identity is this plugin's
manifest `name`, applied in core, never an argument this code sets).

The plugin satisfies `butter_agent.plugin_api.Plugin` structurally. It is
not typed against that Protocol explicitly and imports `PluginContext`
only under `TYPE_CHECKING`, so the package can be loaded into a
butter-agent install without importing butter at runtime — runtime stays
stdlib-only (`asyncio`, `datetime`).

Persistence contract this module is built to (a `database` plugin
contract, not a notes concern — see butter-agent
`specs/development/notes-plugin.md` §4):

- The `table` input is a single key carrying the bare name; never `name`,
  never prefixed.
- A column `default` is advisory only — it is NOT emitted into DDL.
  `created_at` therefore has no DB-level default: this plugin always
  supplies it on insert (from the variable pool when the plan chained
  `clock.now → notes.create`, else self-generated). `datetime` columns
  are stored as ISO-8601 TEXT.
- `database.select`'s `where` is equality-only AND; reads here only ever
  filter by the surrogate `id`. `notes.search` does NOT use `select` —
  it delegates to the host's FTS5-backed `database.search` (porter
  stemming, prefix terms, bm25 relevance). The plugin owns no matching
  logic; `database.define_fts` (called in `_ensure_table`) sets up the
  content index and the host's triggers keep it live.
- `database.delete`'s `where` is the same equality-only AND, required and
  non-empty (a predicate-less DELETE would wipe the namespace); `notes.delete`
  only ever deletes by the surrogate `id`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from butter_agent.plugin_api import PluginContext

#: Bare table name passed to every `database.*` call. Core rewrites it to
#: `notes__entries`; this module never constructs or sees the prefix.
_TABLE: Final = 'entries'

#: Column schema for the notes table. `not_null` IS emitted into DDL by
#: the database plugin, so every insert must carry both columns —
#: `created_at` is self-populated precisely because no DB default exists.
_COLUMNS: Final[dict[str, object]] = {
    'content': {'type': 'text', 'not_null': True},
    'created_at': {'type': 'datetime', 'not_null': True},
}


class NotesPluginError(Exception):
    """Raised on any malformed `notes.*` call.

    Propagates out of `execute`; the host's task executor catches it on
    its broad plugin-failure path and records it as the step's
    `failure_reason` (invariant #6 — plugin code may raise for any reason
    and must not tear the loop down). Not raised for an empty
    `notes.list` (an empty list is a valid result, not a failure); is
    raised for `notes.read` of an unknown id (the caller asked for a
    specific note that does not exist).
    """


class NotesPlugin:
    """`Plugin` Protocol implementation backed by the host's shared `database` plugin.

    Holds no database handle: persistence is entirely via `context.call`
    into `database.*`. The notes table is created lazily on first use and
    only once per process — `define_table` is idempotent (`CREATE TABLE
    IF NOT EXISTS`), so the guard is an optimisation plus a single
    well-defined point where the schema is asserted, not a correctness
    requirement.
    """

    def __init__(self) -> None:
        self._table_ready = False
        # Plan steps execute sequentially in the host executor, but a
        # single plan can legitimately invoke notes twice (e.g. create
        # then list); the lock keeps the check-then-define critical
        # section atomic so the schema is asserted exactly once.
        self._table_lock = asyncio.Lock()

    async def execute(
        self,
        capability: str,
        inputs: dict[str, object],
        context: PluginContext,
    ) -> dict[str, object]:
        if capability == 'create':
            return await self._create(inputs, context)
        if capability == 'list':
            return await self._list(inputs, context)
        if capability == 'read':
            return await self._read(inputs, context)
        if capability == 'delete':
            return await self._delete(inputs, context)
        if capability == 'search':
            return await self._search(inputs, context)
        raise NotesPluginError(
            f'unknown capability {capability!r} (expected one of: create, list, read, delete, search)',
        )

    async def _ensure_table(self, context: PluginContext) -> None:
        """Create the notes table + content FTS index, once per process.

        `define_fts` is established here (not lazily in `_search`) so the
        host's sync triggers are live before the first `create` — every
        note is indexed as it is written, not only those present at the
        first search. Both calls are idempotent; the host's `define_fts`
        requires the base table first, hence the strict order.
        """
        if self._table_ready:
            return
        async with self._table_lock:
            if self._table_ready:
                return
            await context.call(
                'database.define_table',
                {'table': _TABLE, 'columns': _COLUMNS},
            )
            await context.call(
                'database.define_fts',
                {'table': _TABLE, 'columns': ['content']},
            )
            self._table_ready = True

    async def _create(self, inputs: dict[str, object], context: PluginContext) -> dict[str, object]:
        content = inputs.get('content')
        if not isinstance(content, str) or not content:
            raise NotesPluginError(f"input 'content' must be a non-empty string, got {content!r}")

        # `created_at` is optional: present when the plan chained
        # `clock.now → notes.create` (the `$t.time` variable-pool value),
        # absent for a bare "save a note" plan. Either way the column is
        # NOT NULL with no DB default, so a value is always written.
        created_at = _resolve_created_at(inputs.get('created_at'))

        await self._ensure_table(context)
        inserted = await context.call(
            'database.insert',
            {'table': _TABLE, 'row': {'content': content, 'created_at': created_at}},
        )
        note_id = inserted.get('id')
        if not isinstance(note_id, int):
            # database.insert returns the surrogate rowid; anything else
            # is a contract break in the store, surfaced rather than
            # silently returning a malformed note_id downstream.
            raise NotesPluginError(f'database.insert returned a non-integer id {note_id!r}')
        return {'note_id': note_id, 'created_at': created_at}

    async def _list(self, inputs: dict[str, object], context: PluginContext) -> dict[str, object]:
        await self._ensure_table(context)
        select: dict[str, object] = {'table': _TABLE, 'order_by': 'id'}
        limit = inputs.get('limit')
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
                raise NotesPluginError(f"input 'limit' must be a non-negative integer, got {limit!r}")
            select['limit'] = limit
        result = await context.call('database.select', select)
        # The store returns rows already shaped {id, content, created_at}
        # — exactly the documented `notes` element shape — so pass them
        # straight through rather than re-projecting field by field.
        return {'notes': result.get('rows', [])}

    async def _read(self, inputs: dict[str, object], context: PluginContext) -> dict[str, object]:
        note_id = inputs.get('note_id')
        if not isinstance(note_id, int) or isinstance(note_id, bool):
            raise NotesPluginError(f"input 'note_id' must be an integer, got {note_id!r}")

        await self._ensure_table(context)
        result = await context.call(
            'database.select',
            {'table': _TABLE, 'where': {'id': note_id}},
        )
        rows = result.get('rows')
        if not isinstance(rows, list) or not rows:
            raise NotesPluginError(f'no note with id {note_id}')
        row = rows[0]
        if not isinstance(row, dict):
            # database.select rows are dict-shaped per its contract;
            # anything else is a store contract break, surfaced rather
            # than indexed blindly (same stance as _create's id check).
            raise NotesPluginError(f'database.select returned a non-mapping row {row!r}')
        content = row.get('content')
        created_at = row.get('created_at')
        if not isinstance(content, str) or not isinstance(created_at, str):
            # The row exists but is missing the columns this plugin
            # defined. Surface it with the same descriptive contract-break
            # error rather than letting a bare KeyError / malformed value
            # escape (the executor would otherwise record an opaque
            # `KeyError: 'content'` as the failure_reason).
            raise NotesPluginError(f'database.select returned an incomplete row {row!r}')
        return {'content': content, 'created_at': created_at}

    async def _delete(self, inputs: dict[str, object], context: PluginContext) -> dict[str, object]:
        note_id = inputs.get('note_id')
        if not isinstance(note_id, int) or isinstance(note_id, bool):
            raise NotesPluginError(f"input 'note_id' must be an integer, got {note_id!r}")

        await self._ensure_table(context)
        result = await context.call(
            'database.delete',
            {'table': _TABLE, 'where': {'id': note_id}},
        )
        deleted = result.get('deleted')
        if not isinstance(deleted, int) or isinstance(deleted, bool):
            # database.delete returns the affected row count; anything
            # else is a store contract break, surfaced rather than
            # silently reported as a successful delete (same stance as
            # _create's id check).
            raise NotesPluginError(f'database.delete returned a non-integer count {deleted!r}')
        if deleted == 0:
            # The caller asked to remove a specific note that does not
            # exist — the same "asked for a missing id" condition _read
            # raises on, not the empty-result case _list tolerates.
            raise NotesPluginError(f'no note with id {note_id}')
        return {'note_id': note_id}

    async def _search(self, inputs: dict[str, object], context: PluginContext) -> dict[str, object]:
        query = inputs.get('query')
        if not isinstance(query, str) or not query:
            raise NotesPluginError(f"input 'query' must be a non-empty string, got {query!r}")
        limit = inputs.get('limit')
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise NotesPluginError(f"input 'limit' must be a non-negative integer, got {limit!r}")

        await self._ensure_table(context)
        # Matching is delegated to the host's FTS5-backed
        # `database.search` (porter-stemmed prefix terms, bm25
        # relevance) rather than a local substring scan. `order='rank'`
        # => most-relevant first; the store builds the injection-safe
        # MATCH expression from `query` treated as natural text. `limit`
        # caps matches at the store, not here.
        request: dict[str, object] = {'table': _TABLE, 'query': query, 'order': 'rank'}
        if limit is not None:
            request['limit'] = limit
        result = await context.call('database.search', request)
        rows = result.get('rows')
        if not isinstance(rows, list):
            # database.search's contract is {rows: [...]}; anything else
            # is a store contract break, surfaced rather than returned
            # blindly (same stance as _list's rows handling).
            raise NotesPluginError(f'database.search returned a non-list rows {rows!r}')
        return {'notes': rows}


def _resolve_created_at(value: object) -> str:
    """Return the timestamp to store: a supplied ISO-8601 string, or now.

    A `$t.time` from a prior `clock.now` step arrives as a non-empty
    string and is used verbatim — the plugin trusts the chained value
    rather than re-deriving it (the whole point of the variable-pool
    channel). Absent or empty means an un-chained plan: generate a
    timezone-aware ISO-8601 UTC stamp (the column stores TEXT).
    """
    if isinstance(value, str) and value:
        return value
    return datetime.now(UTC).isoformat()
