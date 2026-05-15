"""Tests for the notes plugin + manifest round-trip.

Two layers, mirroring the butter-plugin-clock convention:

- The plugin is unit-tested in isolation against an inline
  `FakePluginContext` — notes owns no database, so every persistence
  call is a recorded `context.call` into `database.*`. Two contracts the
  host's spec §4 makes load-bearing are pinned here: notes addresses its
  table with the **bare** name `"entries"` (the host applies the
  `notes__` prefix), and `created_at` is always written (verbatim from a
  chained value, else self-generated ISO-8601 UTC).
- `manifest.toml` round-trips through butter-agent's own `parse_manifest`
  — the contract check that proves the plugin loads without standing up
  a real host.

End-to-end behaviour (real host executor, gate handler, the
`clock.now → notes.create` chain through the shared database plugin)
lives in the butter-agent integration suite, not here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pytest
from butter_agent.plugin_api import BlastRadius, Plugin, parse_manifest

from butter_plugin_notes import NotesPlugin, NotesPluginError
from butter_plugin_notes.plugin import _COLUMNS

MANIFEST_PATH = Path(__file__).resolve().parent.parent / 'manifest.toml'

# What the host would have rewritten the bare name to in production. The
# plugin must never produce this itself — asserting the recorded call
# carries the *bare* name documents that.
_BARE = 'entries'


@dataclass
class FakePluginContext:
    """Inline stand-in `PluginContext` for unit-testing in isolation.

    Configure `responses` with the canned output for each
    fully-qualified `plugin.capability` ref the plugin under test will
    call. A missing canned response is a test-wiring bug, not a runtime
    condition, so `call` raises `AssertionError` rather than returning an
    empty dict (which would silently mask an unstubbed dependency).
    """

    responses: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    async def call(self, capability: str, inputs: dict[str, object]) -> dict[str, object]:
        self.calls.append((capability, dict(inputs)))
        try:
            return dict(self.responses[capability])
        except KeyError as exc:
            raise AssertionError(
                f'FakePluginContext: no canned response for {capability!r}; configure responses={{{capability!r}: {{...}}}}',
            ) from exc


def _ctx(responses: Mapping[str, Mapping[str, object]] | None = None) -> FakePluginContext:
    """A fake context with canned `database.*` responses.

    `define_table` is always stubbed because every capability ensures the
    table first; callers add `insert` / `select` as needed. `responses`
    is a `Mapping` (covariant) so call sites can pass dict literals with
    narrower value types without tripping dict invariance.
    """
    canned: dict[str, dict[str, object]] = {'database.define_table': {'table': f'notes__{_BARE}'}}
    if responses is not None:
        canned.update({ref: dict(value) for ref, value in responses.items()})
    return FakePluginContext(responses=canned)


# --- create ------------------------------------------------------------------


async def test_create_uses_chained_created_at_verbatim() -> None:
    """A `$t.time` value from a prior clock.now is stored as-is."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.insert': {'id': 7}})

    result = await plugin.execute(
        'create',
        {'content': 'buy butter', 'created_at': '2026-05-14T15:00:00+02:00'},
        ctx,
    )

    assert result == {'note_id': 7, 'created_at': '2026-05-14T15:00:00+02:00'}
    assert ctx.calls == [
        ('database.define_table', {'table': _BARE, 'columns': _COLUMNS}),
        (
            'database.insert',
            {'table': _BARE, 'row': {'content': 'buy butter', 'created_at': '2026-05-14T15:00:00+02:00'}},
        ),
    ]


async def test_create_self_populates_created_at_when_unchained() -> None:
    """Without an upstream clock.now, notes generates a valid ISO-8601 stamp."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.insert': {'id': 1}})

    result = await plugin.execute('create', {'content': 'standalone note'}, ctx)

    stamp = result['created_at']
    assert isinstance(stamp, str)
    parsed = datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    insert_call = next(c for c in ctx.calls if c[0] == 'database.insert')
    assert insert_call[1]['row'] == {'content': 'standalone note', 'created_at': stamp}


@pytest.mark.parametrize('bad', ['', None, 123])
async def test_create_rejects_empty_or_non_string_content(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'content' must be a non-empty string"):
        await plugin.execute('create', {'content': bad}, _ctx())


async def test_create_surfaces_non_integer_id_from_store() -> None:
    """A malformed database.insert result is surfaced, not passed downstream."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.insert': {'id': 'oops'}})
    with pytest.raises(NotesPluginError, match='non-integer id'):
        await plugin.execute('create', {'content': 'x'}, ctx)


# --- list --------------------------------------------------------------------


async def test_list_passes_rows_through_oldest_first() -> None:
    plugin = NotesPlugin()
    rows = [
        {'id': 1, 'content': 'a', 'created_at': '2026-05-14T10:00:00+00:00'},
        {'id': 2, 'content': 'b', 'created_at': '2026-05-14T11:00:00+00:00'},
    ]
    ctx = _ctx({'database.select': {'rows': rows}})

    result = await plugin.execute('list', {}, ctx)

    assert result == {'notes': rows}
    assert ('database.select', {'table': _BARE, 'order_by': 'id'}) in ctx.calls


async def test_list_empty_is_not_an_error() -> None:
    """An empty notes table is a valid result, never a NotesPluginError."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': []}})
    assert await plugin.execute('list', {}, ctx) == {'notes': []}


async def test_list_forwards_valid_limit() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': []}})
    await plugin.execute('list', {'limit': 5}, ctx)
    select_call = next(c for c in ctx.calls if c[0] == 'database.select')
    assert select_call[1] == {'table': _BARE, 'order_by': 'id', 'limit': 5}


@pytest.mark.parametrize('bad', [-1, True, 'lots'])
async def test_list_rejects_invalid_limit(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'limit' must be a non-negative integer"):
        await plugin.execute('list', {'limit': bad}, _ctx({'database.select': {'rows': []}}))


# --- read --------------------------------------------------------------------


async def test_read_returns_single_note() -> None:
    plugin = NotesPlugin()
    ctx = _ctx(
        {'database.select': {'rows': [{'id': 3, 'content': 'hello', 'created_at': '2026-05-14T12:00:00+00:00'}]}},
    )

    result = await plugin.execute('read', {'note_id': 3}, ctx)

    assert result == {'content': 'hello', 'created_at': '2026-05-14T12:00:00+00:00'}
    assert ('database.select', {'table': _BARE, 'where': {'id': 3}}) in ctx.calls


async def test_read_unknown_id_raises() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': []}})
    with pytest.raises(NotesPluginError, match='no note with id 99'):
        await plugin.execute('read', {'note_id': 99}, ctx)


@pytest.mark.parametrize('bad', ['3', None, True])
async def test_read_rejects_non_integer_note_id(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'note_id' must be an integer"):
        await plugin.execute('read', {'note_id': bad}, _ctx())


async def test_read_surfaces_incomplete_row_as_descriptive_error() -> None:
    """A row missing the plugin's own columns is a store contract break.

    It must surface as a descriptive NotesPluginError, not an opaque
    KeyError the executor would record verbatim as the failure_reason.
    """
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': [{'id': 3}]}})
    with pytest.raises(NotesPluginError, match='incomplete row'):
        await plugin.execute('read', {'note_id': 3}, ctx)


# --- delete ------------------------------------------------------------------


async def test_delete_removes_note_by_id() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.delete': {'deleted': 1}})

    result = await plugin.execute('delete', {'note_id': 4}, ctx)

    assert result == {'note_id': 4}
    assert ('database.delete', {'table': _BARE, 'where': {'id': 4}}) in ctx.calls


async def test_delete_unknown_id_raises() -> None:
    """deleted == 0 means the id never existed — same stance as read."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.delete': {'deleted': 0}})
    with pytest.raises(NotesPluginError, match='no note with id 99'):
        await plugin.execute('delete', {'note_id': 99}, ctx)


@pytest.mark.parametrize('bad', ['4', None, True])
async def test_delete_rejects_non_integer_note_id(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'note_id' must be an integer"):
        await plugin.execute('delete', {'note_id': bad}, _ctx())


async def test_delete_surfaces_non_integer_count_from_store() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.delete': {'deleted': 'oops'}})
    with pytest.raises(NotesPluginError, match='non-integer count'):
        await plugin.execute('delete', {'note_id': 1}, ctx)


# --- search ------------------------------------------------------------------


def _rows() -> list[dict[str, object]]:
    return [
        {'id': 1, 'content': 'buy butter', 'created_at': '2026-05-14T10:00:00+00:00'},
        {'id': 2, 'content': 'call Rick', 'created_at': '2026-05-14T11:00:00+00:00'},
        {'id': 3, 'content': 'Butter run again', 'created_at': '2026-05-14T12:00:00+00:00'},
    ]


async def test_search_matches_case_insensitive_substring_oldest_first() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': _rows()}})

    result = await plugin.execute('search', {'query': 'butter'}, ctx)

    assert result == {'notes': [_rows()[0], _rows()[2]]}
    # Selects the full table oldest-first; the substring filter is in Python.
    assert ('database.select', {'table': _BARE, 'order_by': 'id'}) in ctx.calls


async def test_search_no_match_is_empty_not_an_error() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': _rows()}})
    assert await plugin.execute('search', {'query': 'zzz'}, ctx) == {'notes': []}


async def test_search_limit_caps_matches() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': _rows()}})
    result = await plugin.execute('search', {'query': 'butter', 'limit': 1}, ctx)
    assert result == {'notes': [_rows()[0]]}


async def test_search_limit_zero_returns_no_matches() -> None:
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': _rows()}})
    assert await plugin.execute('search', {'query': 'butter', 'limit': 0}, ctx) == {'notes': []}


@pytest.mark.parametrize('bad', ['', None, 123])
async def test_search_rejects_empty_or_non_string_query(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'query' must be a non-empty string"):
        await plugin.execute('search', {'query': bad}, _ctx({'database.select': {'rows': []}}))


@pytest.mark.parametrize('bad', [-1, True, 'lots'])
async def test_search_rejects_invalid_limit(bad: object) -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match="'limit' must be a non-negative integer"):
        await plugin.execute('search', {'query': 'x', 'limit': bad}, _ctx({'database.select': {'rows': []}}))


async def test_search_surfaces_incomplete_row_as_descriptive_error() -> None:
    """A scanned row missing `content` is a store contract break."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.select': {'rows': [{'id': 1}]}})
    with pytest.raises(NotesPluginError, match='incomplete row'):
        await plugin.execute('search', {'query': 'x'}, ctx)


# --- table lifecycle ---------------------------------------------------------


async def test_table_defined_exactly_once_across_calls() -> None:
    """`define_table` is asserted once per process, not per operation."""
    plugin = NotesPlugin()
    ctx = _ctx({'database.insert': {'id': 1}, 'database.select': {'rows': []}})

    await plugin.execute('create', {'content': 'one'}, ctx)
    await plugin.execute('list', {}, ctx)
    await plugin.execute('create', {'content': 'two'}, ctx)

    define_calls = [c for c in ctx.calls if c[0] == 'database.define_table']
    assert len(define_calls) == 1


async def test_unknown_capability_raises() -> None:
    plugin = NotesPlugin()
    with pytest.raises(NotesPluginError, match='unknown capability'):
        await plugin.execute('purge', {'note_id': 1}, _ctx())


# --- Manifest contract -------------------------------------------------------


def test_manifest_round_trips_through_butter_validator() -> None:
    manifest = parse_manifest(MANIFEST_PATH.read_text())
    assert manifest.name == 'notes'
    assert manifest.blast_radius is BlastRadius.LOCAL_WRITE
    assert manifest.entrypoint == 'butter_plugin_notes:NotesPlugin'
    assert {cap.name for cap in manifest.capabilities} == {'create', 'list', 'read', 'delete', 'search'}
    # User-facing: notes capabilities appear in the planner menu (unlike
    # the host's internal database.*).
    assert all(not cap.internal for cap in manifest.capabilities)
    # Declares exactly the internal database capabilities it reaches via
    # ctx.call — the host's registry builder rejects a call to anything
    # absent here.
    assert manifest.requires == (
        'database.define_table',
        'database.insert',
        'database.select',
        'database.delete',
    )


def test_notesplugin_satisfies_protocol_structurally() -> None:
    # Structural Protocol check — having `execute` with the right shape
    # is enough; NotesPlugin doesn't inherit from Plugin.
    plugin: Plugin = NotesPlugin()
    assert plugin is not None
