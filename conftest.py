"""Make the flattened plugin importable as a package for the test-suite.

This repo root *is* the plugin package — the canonical Hermes layout (plugin.yaml
+ __init__.py + sibling modules at the dir root; see the "Build a Hermes Plugin"
guide). Hermes loads the installed plugin **directory** as a package
(``hermes_plugins.<slug>`` with ``submodule_search_locations`` set — see
``hermes_cli.plugins._load_directory_module``), which is why the modules use
relative imports. We mirror that here under the real slug so the tests exercise
the package exactly as Hermes loads it — including the ``__init__.py`` ``register``
shim — via ``from hermes_persona_multiplexer import ...``.
"""

import importlib.util
import pathlib
import sys

# The plugin slug Hermes derives from the name ("-" -> "_"); we bridge under the
# same name so the package identity matches production.
_PKG = "hermes_persona_multiplexer"

# The plugin-root __init__.py uses relative imports and is the package entry,
# not a test module — don't let pytest try to collect/import it standalone.
collect_ignore = ["__init__.py"]

_ROOT = pathlib.Path(__file__).parent
if _PKG not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        _PKG,
        _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    _mod.__path__ = [str(_ROOT)]
    sys.modules[_PKG] = _mod
    _spec.loader.exec_module(_mod)
