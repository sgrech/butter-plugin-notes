"""butter-plugin-notes — persistent free-form note capture.

The `NotesPlugin` class is the manifest's declared entrypoint. Importing
it from the package root keeps the `module:Class` path short
(`butter_plugin_notes:NotesPlugin`). Keep `__version__` in lock-step with
`[plugin].version` in `manifest.toml`.
"""

from __future__ import annotations

from butter_plugin_notes.plugin import NotesPlugin, NotesPluginError

__all__ = ['NotesPlugin', 'NotesPluginError']
__version__ = '0.3.0'
