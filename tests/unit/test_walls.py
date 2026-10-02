"""AST walls (DESIGN §7.3, §4, §10 "Python cannot make a constructor private"): sealed
constructors, privileged calls and the private sentinels are confined to single files.

Every wall walks the AST (``ast.Call`` / ``ast.Attribute`` / ``ast.Name`` / imports), so comments
and docstrings never count and a mention in prose never trips a wall. Each file's import aliases
are resolved first (``import nour.core.tokens as tk``, ``from nour.core.tokens import mint as
make_token``, ``from datetime import datetime as dt``), so a wall matches the *thing*, not one
spelling of it. Files that do not exist yet are simply not scanned, so each wall passes on
today's tree and fails on the first violation (the self-tests prove the latter on a temporary
tree, including the aliased forms).

What the walls confine (allowed sites in ``WALLS``):

* ``mint`` (``nour.core.tokens.mint``): called or referenced only by the bootstrap, the harness
  and ``tests/conftest.py`` (plus ``test_tokens.py``, which tests ``mint`` itself).
* the private sentinels ``_SEAL``, ``_MINT``, ``_VALUE_SEAL``, ``_WITNESS_SEAL`` and the minted
  registries ``_MINTED`` / ``_MINTED_WITNESSES``: imported, read or named only in the module that
  defines them, the one module DESIGN names as the caller, and the test that proves the seal.
* the sealed constructors ``RenderWitness(``, ``Tier2Value(``, ``SafeStr(`` (under any import
  alias), and ``__new__`` on any sealed class (``str.__new__(SafeStr, …)``,
  ``object.__new__(RenderWitness)``, ``AssistantToken.__new__``) — the latter allowed only in
  the tests that forge an object to prove it is refused.
* ``.decrypt(`` / ``._decrypt(`` calls and the ``_decrypt`` / ``_ciphertext`` attributes.
* ``datetime.now(`` / ``utcnow(`` and ``time.time(`` / ``monotonic(``: only the clock reads the
  wall clock (``perf_counter`` stays free for the ``@wallclock`` test).
* pydantic ``model_construct(``: a validation bypass that would feed a ``SafeStr`` sink plain
  text; never used.
* ``ToolExecutor.mint`` / ``executor.mint`` references and ``OwnerMailboxPort`` references
  (DESIGN §4a, §4c).

Scope: walls over ``nour/`` by default; the ones a test could abuse (``mint``, the sentinels,
``__new__``, the Tier 2 attributes, ``model_construct``) also cover ``tests/``. Tests that prove
a seal holds build the sealed object through a *local* alias (``_value_ctor = Tier2Value``),
which is deliberate: the walls resolve import aliases, not assignments.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

SEALED_CLASSES: frozenset[str] = frozenset(
    {
        "SafeStr",
        "RenderWitness",
        "Tier2Value",
        "DeskToken",
        "OperatorToken",
        "AssistantToken",
        "GovernanceToken",
        "AuditorToken",
    }
)
TIER2_PRIVATE_ATTRS: frozenset[str] = frozenset({"_decrypt", "_ciphertext"})
WALL_CLOCK_CALLS: frozenset[str] = frozenset(
    {
        "datetime.datetime.now",
        "datetime.datetime.utcnow",
        "time.time",
        "time.time_ns",
        "time.monotonic",
        "time.monotonic_ns",
    }
)


# --------------------------------------------------------------------------- file context (import aliases)


class FileContext:
    """The import aliases of one module, so ``tk.mint`` resolves to ``nour.core.tokens.mint``."""

    def __init__(self, tree: ast.AST) -> None:
        self.aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        self.aliases[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        self.aliases[head] = head
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    if alias.name != "*":
                        self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def resolve(self, node: ast.AST | None) -> str | None:
        """Dotted name of a ``Name`` / ``Attribute`` chain with import aliases expanded."""
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        parts.append(node.id)
        parts.reverse()
        head = self.aliases.get(parts[0], parts[0])
        return ".".join([head, *parts[1:]])


Matcher = Callable[[ast.AST, FileContext], bool]


def _ident(node: ast.AST | None) -> str | None:
    """The final identifier of a Name / Attribute chain (``a.b.c`` → ``c``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _last(qualified: str | None) -> str | None:
    return qualified.rsplit(".", 1)[-1] if qualified else None


def _base_ident(node: ast.AST) -> str | None:
    """For ``x.y`` the identifier of ``x`` (``self._executor.mint`` → ``_executor``)."""
    if isinstance(node, ast.Attribute):
        return _ident(node.value)
    return None


def _imports_from(node: ast.AST, module: str, name: str) -> bool:
    if not isinstance(node, ast.ImportFrom) or not any(a.name == name for a in node.names):
        return False
    source = node.module or ""
    if node.level == 0:
        return source == module
    return module.endswith("." + source) if source else False  # relative import inside the package


# --------------------------------------------------------------------------- matchers


def is_mint_call(node: ast.AST, ctx: FileContext) -> bool:
    """``mint(...)`` under any spelling that resolves to ``nour.core.tokens.mint``: the bare name,
    ``tokens.mint`` / ``nour.core.tokens.mint``, or an import alias; plus any import binding or
    bare reference of it (``from nour.core.tokens import mint as m``; ``tk.mint`` passed on)."""
    if _imports_from(node, "nour.core.tokens", "mint"):
        return True
    if isinstance(node, ast.Call):
        target = node.func
        if isinstance(target, ast.Name) and target.id == "mint":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "mint":
            if _ident(target.value) == "tokens":
                return True
        return ctx.resolve(target) == "nour.core.tokens.mint"
    if isinstance(node, ast.Attribute) and node.attr == "mint":
        return ctx.resolve(node) == "nour.core.tokens.mint"
    return False


def is_sentinel_reference(module: str, name: str) -> Matcher:
    """An import of, attribute reference to or bare load of a module-private sentinel."""

    target = f"{module}.{name}"

    def matcher(node: ast.AST, ctx: FileContext) -> bool:
        if _imports_from(node, module, name):
            return True
        if isinstance(node, ast.Attribute):
            return node.attr == name or ctx.resolve(node) == target
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            return node.id == name or ctx.resolve(node) == target
        return False

    return matcher


def is_call_named(name: str) -> Matcher:
    """A call whose callee is ``name`` under any import alias (``tier2.RenderWitness(`` and
    ``R(`` after ``from nour.core.tier2 import RenderWitness as R`` both count)."""

    def matcher(node: ast.AST, ctx: FileContext) -> bool:
        if not isinstance(node, ast.Call):
            return False
        return _ident(node.func) == name or _last(ctx.resolve(node.func)) == name

    return matcher


def is_decrypt_call(node: ast.AST, ctx: FileContext) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"decrypt", "_decrypt"}
    )


def is_tier2_private_attribute(node: ast.AST, ctx: FileContext) -> bool:
    """``x._decrypt`` / ``x._ciphertext`` in any context (load, store, call)."""
    return isinstance(node, ast.Attribute) and node.attr in TIER2_PRIVATE_ATTRS


def is_wall_clock_call(node: ast.AST, ctx: FileContext) -> bool:
    """``datetime.now(...)`` / ``utcnow(...)`` / ``time.time(...)`` / ``monotonic(...)`` under any
    import alias, resolved per file; ``clock.now()`` and ``process_now()`` never match."""
    if not isinstance(node, ast.Call):
        return False
    if ctx.resolve(node.func) in WALL_CLOCK_CALLS:
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr in {"now", "utcnow"}:
        return _ident(node.func.value) == "datetime"
    return False


def is_sealed_new(node: ast.AST, ctx: FileContext) -> bool:
    """``<sealed>.__new__(...)``, ``str.__new__(<sealed>, ...)``, ``object.__new__(<sealed>)``:
    every route around a sealed constructor's ``__init__``/``__new__`` check."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    if node.func.attr != "__new__":
        return False
    if _last(ctx.resolve(node.func.value)) in SEALED_CLASSES:
        return True
    return bool(node.args) and _last(ctx.resolve(node.args[0])) in SEALED_CLASSES


def is_model_construct_call(node: ast.AST, ctx: FileContext) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "model_construct"
    )


def is_executor_mint_reference(node: ast.AST, ctx: FileContext) -> bool:
    """``ToolExecutor.mint`` / ``executor.mint`` / ``self._executor.mint`` in any context."""
    if not isinstance(node, ast.Attribute) or node.attr != "mint":
        return False
    base = _base_ident(node)
    return base is not None and (base == "ToolExecutor" or base.lower().endswith("executor"))


def is_owner_mailbox_reference(node: ast.AST, ctx: FileContext) -> bool:
    if isinstance(node, ast.Name):
        return node.id == "OwnerMailboxPort"
    if isinstance(node, ast.Attribute):
        return node.attr == "OwnerMailboxPort"
    if isinstance(node, ast.alias):
        return node.name.split(".")[-1] == "OwnerMailboxPort"
    return False


# --------------------------------------------------------------------------- walls as data


@dataclass(frozen=True)
class Wall:
    name: str
    matcher: Matcher
    allowed: tuple[str, ...]  # repo-relative files, or directories ending with "/"
    sample: str  # a violating source, for the self-test
    roots: tuple[str, ...] = ("nour",)

    @property
    def key(self) -> str:
        return self.name.split(" — ")[0].split(" / ")[0]

    def allows(self, relative: str) -> bool:
        return any(
            relative == entry or (entry.endswith("/") and relative.startswith(entry))
            for entry in self.allowed
        )


TEST_SITES = ("nour", "tests")

WALLS: tuple[Wall, ...] = (
    Wall(
        "mint( — capability tokens are minted by the bootstrap, the harness and tests/conftest.py",
        is_mint_call,
        (
            "nour/runtime/bootstrap.py",
            "nour/testing/harness.py",
            "tests/conftest.py",
            "tests/unit/test_tokens.py",
        ),
        "t = mint('operator')\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_SEAL — the token seal never leaves nour/core/tokens.py",
        is_sentinel_reference("nour.core.tokens", "_SEAL"),
        ("nour/core/tokens.py", "tests/unit/test_tokens.py"),
        "from nour.core.tokens import _SEAL\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_MINTED — the minted-token registry is written by mint only",
        is_sentinel_reference("nour.core.tokens", "_MINTED"),
        ("nour/core/tokens.py",),
        "from nour.core import tokens\ntokens._MINTED.add(x)\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_MINT — the SafeStr mint sentinel is passed by LeakGuard only",
        is_sentinel_reference("nour.core.types", "_MINT"),
        (
            "nour/core/types.py",
            "nour/core/leakguard.py",
            "tests/unit/test_leakguard.py",
            "tests/unit/test_core_types.py",
        ),
        "from nour.core.types import _MINT\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_VALUE_SEAL — the Tier2Value seal is passed by the vault store only",
        is_sentinel_reference("nour.core.tier2", "_VALUE_SEAL"),
        (
            "nour/core/tier2.py",
            "nour/vault/store.py",
            "tests/unit/test_tier2_value.py",
            "tests/unit/test_ports_shapes.py",
        ),
        "import nour.core.tier2 as t2\ns = t2._VALUE_SEAL\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_WITNESS_SEAL — the RenderWitness seal is passed by the document renderer only",
        is_sentinel_reference("nour.core.tier2", "_WITNESS_SEAL"),
        ("nour/core/tier2.py", "nour/vault/renderer.py", "tests/unit/test_tier2_value.py"),
        "from nour.core.tier2 import _WITNESS_SEAL as W\n",
        roots=TEST_SITES,
    ),
    Wall(
        "_MINTED_WITNESSES — the minted-witness registry is written by the constructor only",
        is_sentinel_reference("nour.core.tier2", "_MINTED_WITNESSES"),
        ("nour/core/tier2.py",),
        "from nour.core import tier2\ntier2._MINTED_WITNESSES.add(w)\n",
        roots=TEST_SITES,
    ),
    Wall(
        "RenderWitness( — constructed only inside the document renderer",
        is_call_named("RenderWitness"),
        ("nour/vault/renderer.py",),
        "w = RenderWitness('p', None, 's', _seal=S)\n",
    ),
    Wall(
        "Tier2Value( — constructed only by the vault store",
        is_call_named("Tier2Value"),
        ("nour/vault/store.py",),
        "v = Tier2Value(ref, ct, dec, _seal=S)\n",
    ),
    Wall(
        "SafeStr( — minted only by LeakGuard",
        is_call_named("SafeStr"),
        ("nour/core/leakguard.py",),
        "s = SafeStr(text, _minted_by=M)\n",
    ),
    Wall(
        "__new__ — no sealed class is instantiated around its constructor",
        is_sealed_new,
        (
            "tests/unit/test_tokens.py",
            "tests/unit/test_tier2_value.py",
            "tests/unit/test_contracts.py",
            "tests/unit/test_ports_shapes.py",
            "tests/unit/test_db_session.py",
        ),
        "s = str.__new__(SafeStr, 'raw')\n",
        roots=TEST_SITES,
    ),
    Wall(
        "model_construct( — pydantic validation is never bypassed",
        is_model_construct_call,
        (),
        "m = M.model_construct(text='raw')\n",
        roots=TEST_SITES,
    ),
    Wall(
        ".decrypt( — decryption happens only under nour/vault/ (and Tier2Value.write_into)",
        is_decrypt_call,
        ("nour/vault/", "nour/core/tier2.py"),
        "p = cipher.decrypt(blob, aad)\n",
    ),
    Wall(
        "_decrypt / _ciphertext — the Tier 2 parts are touched only inside nour/core/tier2.py",
        is_tier2_private_attribute,
        ("nour/core/tier2.py",),
        "p = value._decrypt(value._ciphertext)\n",
        roots=TEST_SITES,
    ),
    Wall(
        "datetime.now( / utcnow( / time.time( / monotonic( — only the clock reads the wall clock",
        is_wall_clock_call,
        ("nour/core/clock.py",),
        "from datetime import datetime\nn = datetime.now()\n",
    ),
    Wall(
        "ToolExecutor.mint / executor.mint — release tokens are minted by the gate and injected into the approvals queue by the bootstrap",
        is_executor_mint_reference,
        ("nour/agent/dispatcher.py", "nour/runtime/bootstrap.py"),
        "f = executor.mint\n",
    ),
    Wall(
        "OwnerMailboxPort — referenced only where the Assistant process is built",
        is_owner_mailbox_reference,
        (
            "nour/core/ports.py",
            "nour/fakes/",
            "nour/tools/assistant.py",
            "nour/runtime/bootstrap.py",
            "nour/testing/",
        ),
        "from nour.core.ports import OwnerMailboxPort\n",
    ),
)
WALL_IDS = [wall.key for wall in WALLS]


def python_files(repo: Path, roots: tuple[str, ...]) -> Iterator[Path]:
    for root in roots:
        base = repo / root
        if not base.is_dir():
            continue
        yield from sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def wall_violations(wall: Wall, repo: Path) -> list[str]:
    """``<relative path>:<line>`` for every node the wall confines, outside its allowed files
    (one entry per line, however many nodes on it match)."""
    violations: list[str] = []
    for path in python_files(repo, wall.roots):
        relative = path.relative_to(repo).as_posix()
        if wall.allows(relative):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        ctx = FileContext(tree)
        lines: set[int] = set()
        for node in ast.walk(tree):
            if wall.matcher(node, ctx):
                lines.add(getattr(node, "lineno", 0))
        violations.extend(f"{relative}:{line}" for line in sorted(lines))
    return violations


# --------------------------------------------------------------------------- the real tree


@pytest.mark.parametrize("wall", WALLS, ids=WALL_IDS)
def test_wall_holds_on_the_real_tree(wall: Wall) -> None:
    assert wall_violations(wall, REPO) == [], wall.name


def test_allowed_sites_exist_or_are_future_files() -> None:
    """Every allowed site is a DESIGN §8 file (present or still to come), never a typo."""
    known_future = {
        "nour/runtime/bootstrap.py",
        "nour/testing/harness.py",
        "nour/vault/renderer.py",
        "nour/vault/store.py",
        "nour/vault/",
        "nour/agent/dispatcher.py",
        "nour/fakes/",
        "nour/tools/assistant.py",
        "nour/testing/",
    }
    for wall in WALLS:
        for entry in wall.allowed:
            assert (REPO / entry).exists() or entry in known_future, entry
    assert (REPO / "tests/conftest.py").exists() and (REPO / "nour/core/clock.py").exists()


# --------------------------------------------------------------------------- matchers


def _hits(matcher: Matcher, source: str) -> int:
    tree = ast.parse(source)
    ctx = FileContext(tree)
    return sum(1 for node in ast.walk(tree) if matcher(node, ctx))


def test_file_context_resolves_import_aliases() -> None:
    ctx = FileContext(
        ast.parse(
            "import datetime as d\nfrom datetime import datetime as dt\nimport nour.core.tokens\n"
            "from nour.core import tokens as tk\nfrom nour.core.tokens import mint as make_token\n"
            "import time\n"
        )
    )

    def expr(source: str) -> ast.AST:
        statement = ast.parse(source).body[0]
        assert isinstance(statement, ast.Expr)
        return statement.value

    assert ctx.resolve(expr("d.datetime.now")) == "datetime.datetime.now"
    assert ctx.resolve(expr("dt.utcnow")) == "datetime.datetime.utcnow"
    assert ctx.resolve(expr("nour.core.tokens.mint")) == "nour.core.tokens.mint"
    assert ctx.resolve(expr("tk.mint")) == "nour.core.tokens.mint"
    assert ctx.resolve(expr("make_token")) == "nour.core.tokens.mint"
    assert ctx.resolve(expr("time.monotonic")) == "time.monotonic"
    assert ctx.resolve(expr("clock.now")) == "clock.now"
    assert ctx.resolve(expr("f()")) is None


def test_mint_matcher() -> None:
    assert _hits(is_mint_call, "token = mint('operator')\n") == 1
    assert _hits(is_mint_call, "from nour.core import tokens\nt = tokens.mint('auditor')\n") == 2
    assert _hits(is_mint_call, "import nour.core.tokens\nt = nour.core.tokens.mint('x')\n") == 2
    assert _hits(is_mint_call, "import nour.core.tokens as tk\nt = tk.mint('operator')\n") == 2
    assert _hits(is_mint_call, "from nour.core.tokens import mint as make_token\n") == 1
    assert (
        _hits(is_mint_call, "from nour.core.tokens import mint as make_token\nmake_token('x')\n")
        == 2
    )
    assert _hits(is_mint_call, "from nour.core import tokens as tk\nq = Queue(tk.mint)\n") == 1
    assert _hits(is_mint_call, "release = self._executor.mint(p, tier, None, 'gate')\n") == 0
    assert _hits(is_mint_call, "x = make_token('operator')\n") == 0  # not imported from tokens
    assert _hits(is_mint_call, '"""mint( in a docstring"""\n# mint( in a comment\n') == 0
    assert _hits(is_mint_call, "def mint(kind): ...\n") == 0
    assert _hits(is_mint_call, "approvals = ApprovalsQueue(mint=ToolExecutor.mint)\n") == 0


def test_sentinel_matchers() -> None:
    seal = is_sentinel_reference("nour.core.tokens", "_SEAL")
    assert _hits(seal, "from nour.core.tokens import _SEAL\n") == 1
    assert _hits(seal, "from nour.core.tokens import _SEAL as S\n") == 1
    assert _hits(seal, "from nour.core.tokens import OperatorToken, _SEAL\n") == 1
    assert _hits(seal, "from .tokens import _SEAL\n") == 1
    assert (
        _hits(seal, "from nour.core import tokens\nt = tokens.OperatorToken(_seal=tokens._SEAL)\n")
        == 1
    )
    assert _hits(seal, "import nour.core.tokens as tk\ns = tk._SEAL\n") == 1
    assert _hits(seal, "x = _SEAL\n") == 1  # a star import or a local copy
    assert _hits(seal, "from nour.core.tokens import _SEAL as S\nt = Token(_seal=S)\n") == 2
    assert _hits(seal, "_SEAL = object()\n") == 0  # the definition itself is a Store
    assert _hits(seal, "from nour.core.tokens import mint\n") == 0
    assert _hits(seal, "t = Token(_seal=seal)\n") == 0
    mint = is_sentinel_reference("nour.core.types", "_MINT")
    assert (
        _hits(mint, "from nour.core import types as T\ns = SafeStr('x', _minted_by=T._MINT)\n") == 1
    )
    assert _hits(mint, "from nour.core.types import SafeStr\n") == 0


def test_named_call_matchers() -> None:
    for name in ("RenderWitness", "Tier2Value", "SafeStr"):
        matcher = is_call_named(name)
        assert _hits(matcher, f"x = {name}('a', _seal=s)\n") == 1
        assert _hits(matcher, f"x = tier2.{name}('a')\n") == 1
        assert _hits(matcher, f"from nour.core.tier2 import {name} as Z\nx = Z('a')\n") == 1
        assert _hits(matcher, f"import nour.core.tier2 as t2\nx = t2.{name}('a')\n") == 1
        assert _hits(matcher, f"class {name}(str): ...\n") == 0
        assert _hits(matcher, f"ok = isinstance(x, {name})\n") == 0
        assert _hits(matcher, f'"""{name}( in prose"""\n') == 0
        assert _hits(matcher, f"_ctor = {name}\nx = _ctor('a')\n") == 0  # local alias: deliberate


def test_sealed_new_and_model_construct_matchers() -> None:
    assert _hits(is_sealed_new, "s = str.__new__(SafeStr, 'raw')\n") == 1
    assert _hits(is_sealed_new, "s = SafeStr.__new__(SafeStr, 'raw')\n") == 1
    assert _hits(is_sealed_new, "w = object.__new__(RenderWitness)\n") == 1
    assert _hits(is_sealed_new, "v = object.__new__(Tier2Value)\n") == 1
    assert _hits(is_sealed_new, "t = object.__new__(AssistantToken)\n") == 1
    assert (
        _hits(
            is_sealed_new, "from nour.core import tokens\nt = object.__new__(tokens.AuditorToken)\n"
        )
        == 1
    )
    assert (
        _hits(
            is_sealed_new, "from nour.core.types import SafeStr as S\ns = str.__new__(S, 'raw')\n"
        )
        == 1
    )
    assert _hits(is_sealed_new, "x = super().__new__(cls, text)\n") == 0
    assert _hits(is_sealed_new, "x = object.__new__(Plain)\n") == 0
    assert _hits(is_sealed_new, "x = Money.__new__(Money)\n") == 0
    assert _hits(is_model_construct_call, "m = M.model_construct(text='raw')\n") == 1
    assert _hits(is_model_construct_call, "m = OutboundWhatsApp.model_construct(**raw)\n") == 1
    assert _hits(is_model_construct_call, "m = M.model_validate(raw)\n") == 0


def test_decrypt_attribute_and_clock_matchers() -> None:
    assert _hits(is_decrypt_call, "plain = cipher.decrypt(blob, aad)\n") == 1
    assert _hits(is_decrypt_call, "plain = AESGCM(key).decrypt(nonce, data, aad)\n") == 1
    assert _hits(is_decrypt_call, "plain = value._decrypt(value._ciphertext)\n") == 1
    assert _hits(is_decrypt_call, "def decrypt(self, blob): ...\n") == 0
    assert _hits(is_decrypt_call, "x = decrypt(blob)\n") == 0  # bare name: not the method wall
    assert _hits(is_tier2_private_attribute, "plain = value._decrypt(value._ciphertext)\n") == 2
    assert _hits(is_tier2_private_attribute, "f = object.__getattribute__(v, '_decrypt')\n") == 0
    assert _hits(is_tier2_private_attribute, "x = value._ref\n") == 0
    assert (
        _hits(is_wall_clock_call, "from datetime import datetime\nnow = datetime.now(tz=UTC)\n")
        == 1
    )
    assert _hits(is_wall_clock_call, "import datetime\nnow = datetime.datetime.utcnow()\n") == 1
    assert _hits(is_wall_clock_call, "from datetime import datetime as dt\nnow = dt.now()\n") == 1
    assert _hits(is_wall_clock_call, "import datetime as d\nnow = d.datetime.now()\n") == 1
    assert _hits(is_wall_clock_call, "import time\nt = time.time()\n") == 1
    assert _hits(is_wall_clock_call, "from time import monotonic\nt = monotonic()\n") == 1
    assert _hits(is_wall_clock_call, "import time as tm\nt = tm.monotonic_ns()\n") == 1
    assert _hits(is_wall_clock_call, "import time\nt = time.perf_counter()\n") == 0
    assert _hits(is_wall_clock_call, "now = clock.now()\n") == 0
    assert _hits(is_wall_clock_call, "now = process_now()\n") == 0
    assert _hits(is_wall_clock_call, "now = self._clock.now()\n") == 0


def test_executor_mint_and_owner_mailbox_matchers() -> None:
    assert _hits(is_executor_mint_reference, "release = executor.mint(p)\n") == 1
    assert (
        _hits(is_executor_mint_reference, "approvals = ApprovalsQueue(mint=ToolExecutor.mint)\n")
        == 1
    )
    assert _hits(is_executor_mint_reference, "r = self._executor.mint(p)\n") == 1
    assert _hits(is_executor_mint_reference, "r = self._mint(p)\n") == 0
    assert _hits(is_executor_mint_reference, "t = tokens.mint('operator')\n") == 0
    assert _hits(is_owner_mailbox_reference, "from nour.core.ports import OwnerMailboxPort\n") == 1
    assert _hits(is_owner_mailbox_reference, "port: ports.OwnerMailboxPort | None = None\n") == 1
    assert _hits(is_owner_mailbox_reference, "class FakeMailbox(OwnerMailboxPort): ...\n") == 1
    assert _hits(is_owner_mailbox_reference, "x = CoatMailboxPort\n") == 0


# --------------------------------------------------------------------------- self-test on a tree


def _write(repo: Path, relative: str, source: str) -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


@pytest.mark.parametrize("wall", WALLS, ids=WALL_IDS)
def test_each_wall_fails_on_a_violation_and_allows_its_sites(wall: Wall, tmp_path: Path) -> None:
    source = wall.sample
    prose = '"""' + source + '"""\n' + "".join(f"# {line}\n" for line in source.splitlines())
    _write(tmp_path, "nour/__init__.py", "")
    _write(tmp_path, "nour/records/prose.py", prose)  # prose only: not a violation
    _write(tmp_path, "nour/records/offender.py", source)  # the violation
    for site in wall.allowed:
        _write(tmp_path, site + "inside.py" if site.endswith("/") else site, source)  # allowed
    if "tests" in wall.roots:
        _write(tmp_path, "tests/unit/test_offender.py", source)
    violations = wall_violations(wall, tmp_path)
    line = len(source.strip().splitlines())  # the violating statement is the last line
    expected = [f"nour/records/offender.py:{line}"]
    if "tests" in wall.roots:
        expected.append(f"tests/unit/test_offender.py:{line}")
    assert violations == expected, (wall.name, violations)


BYPASSES: list[tuple[str, str]] = [
    ("mint(", "from nour.core.tokens import mint as make_token\nt = make_token('operator')\n"),
    ("mint(", "from nour.core import tokens as tk\nt = tk.mint('operator')\n"),
    ("mint(", "import nour.core.tokens as T\nt = T.mint('x')\n"),
    ("mint(", "from nour.core.tokens import *\nt = mint('x')\n"),
    ("_SEAL", "from nour.core import tokens\nt = tokens.OperatorToken(_seal=tokens._SEAL)\n"),
    (
        "_SEAL",
        "from nour.core.tokens import OperatorToken, _SEAL\nt = OperatorToken(_seal=_SEAL)\n",
    ),
    ("_MINTED", "import nour.core.tokens as tk\ntk._MINTED.add(forged)\n"),
    ("_MINT", "from nour.core.types import SafeStr as S, _MINT\ns = S('x', _minted_by=_MINT)\n"),
    (
        "_VALUE_SEAL",
        "from nour.core.tier2 import Tier2Value as T, _VALUE_SEAL\nv = T(r, c, d, _seal=_VALUE_SEAL)\n",
    ),
    (
        "_WITNESS_SEAL",
        "from nour.core import tier2\nw = tier2.RenderWitness('p', None, 's', _seal=tier2._WITNESS_SEAL)\n",
    ),
    ("_MINTED_WITNESSES", "from nour.core.tier2 import _MINTED_WITNESSES as reg\nreg.add(w)\n"),
    (
        "RenderWitness(",
        "from nour.core.tier2 import RenderWitness as R\nw = R('p', None, 's', _seal=X)\n",
    ),
    ("Tier2Value(", "import nour.core.tier2 as t2\nv = t2.Tier2Value(r, c, d, _seal=X)\n"),
    ("SafeStr(", "from nour.core.types import SafeStr as S\ns = S('x', _minted_by=M)\n"),
    ("__new__", "w = object.__new__(RenderWitness)\n"),
    ("__new__", "from nour.core.types import SafeStr\ns = str.__new__(SafeStr, 'raw')\n"),
    ("__new__", "from nour.core import tokens\nt = object.__new__(tokens.AssistantToken)\n"),
    (
        "model_construct(",
        "m = OutboundWhatsApp.model_construct(line_id='l', to='t', text='<IBAN>')\n",
    ),
    (".decrypt(", "p = value._decrypt(ct)\n"),
    ("_decrypt", "ct = object.__getattribute__(v, '_ref') and v._ciphertext\n"),
    ("datetime.now(", "from datetime import datetime as dt\nn = dt.now()\n"),
    ("datetime.now(", "import time\nn = time.time()\n"),
    ("datetime.now(", "from time import monotonic\nn = monotonic()\n"),
]


@pytest.mark.parametrize(
    ("key", "source"), BYPASSES, ids=[f"{k}:{i}" for i, (k, _) in enumerate(BYPASSES)]
)
def test_aliased_bypasses_are_caught(key: str, source: str, tmp_path: Path) -> None:
    wall = next(w for w in WALLS if w.key == key)
    _write(tmp_path, "nour/__init__.py", "")
    _write(tmp_path, "nour/records/offender.py", source)
    violations = wall_violations(wall, tmp_path)
    assert f"nour/records/offender.py:{len(source.splitlines())}" in violations  # the use
    assert all(v.startswith("nour/records/offender.py:") for v in violations)
