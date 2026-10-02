"""Import waves (DESIGN §8 "Import graph", §7.3): every file under ``nour/`` imports ``nour.*``
packages only from an earlier layer (or its own package).

The layer list is DESIGN §8 verbatim, bottom up. Siblings in one layer never import each other;
``nour.core`` imports no other ``nour`` package; ``nour.config`` imports only ``nour.core``.
Imports under ``if TYPE_CHECKING:`` are excluded (``pyproject.toml`` sets
``exclude_type_checking_imports = true`` for the same contract), function-level imports count
(a lazy import is still a dependency), relative imports resolve to the current package.

``check_tree`` runs on the real tree and, in the self-test, on a temporary tree with deliberate
violations, so the test proves it fails on the first violation rather than passing vacuously.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NOUR_ROOT = REPO / "nour"

LAYERS: tuple[frozenset[str], ...] = (
    frozenset({"nour.core"}),
    frozenset({"nour.config"}),
    frozenset({"nour.db", "nour.fakes", "nour.language"}),
    frozenset({"nour.audit", "nour.auth", "nour.vault", "nour.records", "nour.events"}),
    frozenset({"nour.policy", "nour.governance"}),
    frozenset({"nour.agent", "nour.tools"}),
    frozenset({"nour.ingress", "nour.adapters"}),
    frozenset({"nour.runtime"}),
    frozenset({"nour.testing"}),
    frozenset({"nour.cli"}),
)
"""DESIGN §8: ``nour.cli`` → ``nour.testing`` → ``nour.runtime`` → {ingress, adapters} →
{agent | tools} → {policy | governance} → {audit | auth | vault | records | events} →
{db | fakes | language} → ``nour.config`` → ``nour.core``."""

LAYER_OF: dict[str, int] = {pkg: index for index, layer in enumerate(LAYERS) for pkg in layer}
ROOT_PACKAGE = "nour"


@dataclass(frozen=True)
class ImportEdge:
    module: str  # importing module, e.g. "nour.db.session"
    target: str  # imported module, e.g. "nour.core.tokens"
    lineno: int


def module_name(path: Path, root: Path) -> str:
    """``<root>/db/session.py`` → ``nour.db.session``; ``<root>/db/__init__.py`` → ``nour.db``."""
    relative = path.resolve().relative_to(root.resolve())
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([ROOT_PACKAGE, *parts])


def package_of(module: str) -> str:
    """``nour.db.session`` → ``nour.db``; ``nour.cli`` → ``nour.cli``; ``nour`` → ``nour``."""
    parts = module.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


def _is_type_checking_guard(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _resolve_relative(module: str, is_package: bool, level: int, name: str | None) -> str:
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    if level > 1:
        base = base[: len(base) - (level - 1)]
    if name:
        base = [*base, *name.split(".")]
    return ".".join(base)


def import_edges(source: str, module: str, *, is_package: bool) -> list[ImportEdge]:
    """Every ``nour.*`` import in ``source`` outside ``if TYPE_CHECKING:`` blocks."""
    tree = ast.parse(source)
    edges: list[ImportEdge] = []

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.If) and _is_type_checking_guard(node.test):
            for child in node.orelse:
                visit(child)
            return
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == ROOT_PACKAGE or alias.name.startswith(ROOT_PACKAGE + "."):
                    edges.append(ImportEdge(module, alias.name, node.lineno))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target = _resolve_relative(module, is_package, node.level, node.module)
                for alias in node.names:
                    suffix = "" if alias.name == "*" else f".{alias.name}"
                    edges.append(ImportEdge(module, target + suffix, node.lineno))
            elif node.module and (
                node.module == ROOT_PACKAGE or node.module.startswith(ROOT_PACKAGE + ".")
            ):
                for alias in node.names:
                    # `from nour.core import tokens` imports the submodule; keep the package edge
                    # (the first two components decide the layer either way).
                    edges.append(ImportEdge(module, f"{node.module}.{alias.name}", node.lineno))
        for sub in ast.iter_child_nodes(node):
            visit(sub)

    visit(tree)
    return edges


def edge_violation(edge: ImportEdge) -> str | None:
    """The rule an edge breaks, or ``None`` when it is allowed."""
    source_pkg = package_of(edge.module)
    target_pkg = package_of(edge.target)
    if target_pkg == ROOT_PACKAGE:
        return None  # `import nour` (the version string) is always fine
    if source_pkg == target_pkg:
        return None  # own package
    if source_pkg == ROOT_PACKAGE:
        return f"{edge.module}:{edge.lineno}: the root package imports {edge.target}"
    if source_pkg not in LAYER_OF:
        return (
            f"{edge.module}:{edge.lineno}: package {source_pkg} is not in the DESIGN §8 layer list"
        )
    if target_pkg not in LAYER_OF:
        return (
            f"{edge.module}:{edge.lineno}: imports {edge.target}, not in the DESIGN §8 layer list"
        )
    source_layer, target_layer = LAYER_OF[source_pkg], LAYER_OF[target_pkg]
    if target_layer == source_layer:
        return f"{edge.module}:{edge.lineno}: imports sibling package {target_pkg} (same wave)"
    if target_layer > source_layer:
        return f"{edge.module}:{edge.lineno}: imports {target_pkg} from a later wave"
    return None


def python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def check_tree(root: Path) -> list[str]:
    """Every violation under ``root`` (a ``nour`` package directory), as readable strings."""
    violations: list[str] = []
    for path in python_files(root):
        module = module_name(path, root)
        source = path.read_text(encoding="utf-8")
        try:
            edges = import_edges(source, module, is_package=path.name == "__init__.py")
        except SyntaxError as exc:
            violations.append(f"{module}: cannot parse: {exc}")
            continue
        pkg = package_of(module)
        if pkg != ROOT_PACKAGE and pkg not in LAYER_OF:
            violations.append(f"{module}: package {pkg} is not in the DESIGN §8 layer list")
            continue
        for edge in edges:
            reason = edge_violation(edge)
            if reason is not None:
                violations.append(reason)
    return violations


# --------------------------------------------------------------------------- the real tree


def test_real_tree_respects_the_waves() -> None:
    assert NOUR_ROOT.is_dir()
    assert check_tree(NOUR_ROOT) == []


def test_core_imports_no_other_nour_package() -> None:
    core = NOUR_ROOT / "core"
    for path in python_files(core):
        module = module_name(path, NOUR_ROOT)
        for edge in import_edges(
            path.read_text("utf-8"), module, is_package=path.name == "__init__.py"
        ):
            assert package_of(edge.target) in {"nour", "nour.core"}, edge


def test_config_imports_only_core() -> None:
    for path in python_files(NOUR_ROOT / "config"):
        module = module_name(path, NOUR_ROOT)
        for edge in import_edges(
            path.read_text("utf-8"), module, is_package=path.name == "__init__.py"
        ):
            assert package_of(edge.target) in {"nour", "nour.core", "nour.config"}, edge


def test_layer_list_matches_design() -> None:
    assert LAYERS[0] == {"nour.core"} and LAYERS[1] == {"nour.config"}
    assert LAYERS[-1] == {"nour.cli"}
    assert len(LAYER_OF) == sum(len(layer) for layer in LAYERS)  # no package in two layers


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("db/session.py", "nour.db.session"),
        ("db/__init__.py", "nour.db"),
        ("__init__.py", "nour"),
        ("cli.py", "nour.cli"),
        ("audit/auditor/checks.py", "nour.audit.auditor.checks"),
    ],
)
def test_module_name(path: str, expected: str) -> None:
    assert module_name(NOUR_ROOT / path, NOUR_ROOT) == expected
    assert package_of(expected) == ".".join(expected.split(".")[:2])


def test_relative_imports_resolve_to_the_package() -> None:
    edges = import_edges(
        "from . import base\nfrom .base import Base\nfrom ..core import types\n",
        "nour.db.session",
        is_package=False,
    )
    assert [e.target for e in edges] == ["nour.db.base", "nour.db.base.Base", "nour.core.types"]
    pkg_edges = import_edges("from .base import Base\n", "nour.db", is_package=True)
    assert [e.target for e in pkg_edges] == ["nour.db.base.Base"]
    assert [edge_violation(e) for e in edges] == [None, None, None]


def test_type_checking_imports_are_excluded_but_lazy_imports_count() -> None:
    source = (
        "from typing import TYPE_CHECKING\n"
        "import typing\n"
        "if TYPE_CHECKING:\n    from nour.config.schema import CoatConfig\n"
        "if typing.TYPE_CHECKING:\n    from nour.db.base import Base\nelse:\n    import nour.core.types\n"
        "def f():\n    from nour.policy import registry\n    return registry\n"
    )
    edges = import_edges(source, "nour.core.contracts", is_package=False)
    assert [e.target for e in edges] == ["nour.core.types", "nour.policy.registry"]
    assert edge_violation(edges[0]) is None
    assert edge_violation(edges[1]) is not None and "later wave" in edge_violation(edges[1])  # type: ignore[operator]


# --------------------------------------------------------------------------- self-test on a bad tree


def _write(root: Path, relative: str, source: str = "") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_check_tree_fails_on_a_violating_tree(tmp_path: Path) -> None:
    root = tmp_path / "nour"
    _write(root, "__init__.py")
    _write(root, "core/__init__.py")
    _write(root, "core/types.py", "x = 1\n")
    _write(root, "core/bad.py", "from nour.config.schema import NourConfig\n")  # core → config
    _write(root, "config/__init__.py")
    _write(root, "config/ok.py", "from nour.core.types import x\nimport nour\n")  # allowed
    _write(root, "config/bad.py", "import nour.db.base\n")  # config → db (later wave)
    _write(root, "fakes/__init__.py")
    _write(root, "fakes/sibling.py", "import nour.db.base\n")  # sibling in the same wave
    _write(root, "db/__init__.py")
    _write(root, "db/base.py", "from nour.core.types import x\n")
    _write(root, "db/fine.py", "from .base import x\nfrom nour.core import types\n")
    _write(
        root,
        "db/typed.py",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from nour.config.schema import NourConfig\n",
    )
    _write(
        root, "db/lazy.py", "def f():\n    from nour.audit.log import AuditLog\n"
    )  # later wave, lazily
    _write(root, "weird/__init__.py")  # not a DESIGN §8 package
    _write(root, "cli.py", "from nour.testing.harness import Harness\n")  # top layer: fine
    _write(root, "runtime/__init__.py")
    _write(root, "runtime/bootstrap.py", "from nour.cli import app\n")  # runtime → cli (later)

    violations = check_tree(root)
    offenders = {v.split(":")[0] for v in violations}
    assert offenders == {
        "nour.core.bad",
        "nour.config.bad",
        "nour.fakes.sibling",
        "nour.db.lazy",
        "nour.weird",
        "nour.runtime.bootstrap",
    }, violations
    assert any("sibling" in v for v in violations)
    assert any("later wave" in v for v in violations)
    assert any("layer list" in v for v in violations)


def test_check_tree_passes_a_clean_tree(tmp_path: Path) -> None:
    root = tmp_path / "nour"
    _write(root, "__init__.py", '__version__ = "0"\n')
    _write(root, "core/__init__.py")
    _write(root, "core/types.py", "from nour.core.errors import E\n")
    _write(root, "core/errors.py", "class E(Exception): ...\n")
    _write(root, "config/schema.py", "from nour.core.types import E\nimport nour\n")
    _write(root, "db/session.py", "from nour.db.base import B\nfrom nour.core.tokens import T\n")
    _write(root, "db/base.py", "B = 1\n")
    _write(
        root,
        "policy/tiering.py",
        "from nour.records.crm import C\nfrom nour.config.schema import S\n",
    )
    assert check_tree(root) == []


def test_syntax_errors_are_reported_not_ignored(tmp_path: Path) -> None:
    root = tmp_path / "nour"
    _write(root, "__init__.py")
    _write(root, "core/broken.py", "def (:\n")
    violations = check_tree(root)
    assert len(violations) == 1 and "cannot parse" in violations[0]
