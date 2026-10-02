"""Capability tokens (DESIGN §3.2 ``nour/core/tokens.py``; SPEC §5 §13).

One token per process. A ``DeskToken`` carries the desk that a ``SessionFactory``, a ``PortSet``
and a ``ToolView`` are bound to; the subclasses fix the desk so a function typed
``token: AssistantToken`` (``VaultStore``, ``HandoffQueue.push``) cannot be handed the other desk.
``AuditorToken`` is deliberately **not** a ``DeskToken``: it opens read-only sessions only and
can never drive a desk loop.

``mint`` is the single producer. Every constructor takes the module-private ``_SEAL`` as a
keyword-only argument and raises ``AuthError`` for anything else; ``tests/unit/test_walls.py``
asserts the call ``mint(`` (and any import of ``mint`` or ``_SEAL`` under any alias) appears only
in ``nour/runtime/bootstrap.py``, ``nour/testing/harness.py`` and ``tests/conftest.py``. Tokens
are immutable (``__slots__``, no ``__dict__``, ``__setattr__`` refused after construction) and
cannot be pickled, so one cannot be copied into another process or serialised into a row.

Two more walls, because Python cannot make a constructor private (DESIGN §10):

* **Finality.** ``OperatorToken``, ``AssistantToken``, ``GovernanceToken`` and ``AuditorToken``
  refuse subclasses (``__init_subclass__``), and ``DeskToken`` accepts exactly those three
  subclasses, defined once, in this module: a ``class Evil(OperatorToken)`` that re-pins
  ``_fixed_desk`` is a ``TypeError`` at class creation.
* **The minted registry.** ``mint`` records every token it produces in a module-private
  ``weakref.WeakSet``; :func:`is_minted` / :func:`require_minted` are what every wall that is an
  ``isinstance`` check (``scope_allows``, ``DeskWallGuard``, ``PortSet.for_desk``,
  ``ExecContext``) consults, so an object conjured with ``object.__new__(AssistantToken)`` plus
  ``object.__setattr__`` passes ``isinstance`` but opens nothing. The registry needs a weak
  reference, hence the ``__weakref__`` slot next to ``desk``.
"""

from __future__ import annotations

import weakref
from typing import Any, Literal, overload

from nour.core.errors import AuthError, DeskWallViolation
from nour.core.types import Desk

_SEAL: object = object()
"""Module-private seal: only ``mint`` passes it."""

TokenKind = Literal["operator", "assistant", "governance", "auditor"]

_DESK_SUBCLASSES: frozenset[str] = frozenset({"OperatorToken", "AssistantToken", "GovernanceToken"})
_defined: set[str] = set()


class DeskToken:
    """§5 §13: one token per process; carries the desk a Session, a PortSet and a ToolView are bound to."""

    __slots__ = ("desk", "__weakref__")
    desk: Desk
    _fixed_desk: Desk | None = None  # subclasses pin it; the base class accepts any desk

    def __init__(self, desk: Desk, *, _seal: object) -> None:
        """Raises ``AuthError`` unless ``_seal`` is ``tokens._SEAL`` (only ``mint`` has it)."""
        if _seal is not _SEAL:
            raise AuthError(f"{type(self).__name__} must be minted by nour.core.tokens.mint")
        desk = Desk(desk)
        fixed = type(self)._fixed_desk
        if fixed is not None and desk is not fixed:
            raise DeskWallViolation(f"{type(self).__name__} cannot carry desk {desk.value!r}")
        object.__setattr__(self, "desk", desk)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Only the three desk subclasses below, each defined once in this module."""
        super().__init_subclass__(**kwargs)
        name = cls.__name__
        if (
            cls.__module__ != __name__
            or name not in _DESK_SUBCLASSES
            or cls.__mro__[1] is not DeskToken
            or name in _defined
        ):
            raise TypeError(
                f"DeskToken is sealed: {name} may not subclass it (a subclass could re-pin the desk)"
            )
        _defined.add(name)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"{type(self).__name__} is immutable")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError(f"{type(self).__name__} cannot be pickled or copied across processes")

    def __copy__(self) -> DeskToken:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> DeskToken:
        return self

    def __repr__(self) -> str:
        return f"{type(self).__name__}(desk={self.desk.value})"


class OperatorToken(DeskToken):
    """``desk == Desk.OPERATOR``: the outward-facing desk, the injection surface (SPEC §5)."""

    __slots__ = ()
    _fixed_desk = Desk.OPERATOR

    def __init__(self, desk: Desk = Desk.OPERATOR, *, _seal: object) -> None:
        super().__init__(desk, _seal=_seal)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("OperatorToken is final")


class AssistantToken(DeskToken):
    """``desk == Desk.ASSISTANT``: the only token the vault and the owner mailbox accept."""

    __slots__ = ()
    _fixed_desk = Desk.ASSISTANT

    def __init__(self, desk: Desk = Desk.ASSISTANT, *, _seal: object) -> None:
        super().__init__(desk, _seal=_seal)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("AssistantToken is final")


class GovernanceToken(DeskToken):
    """``desk == Desk.GOVERNANCE``: ingress, scheduler, CLI, kill switch."""

    __slots__ = ()
    _fixed_desk = Desk.GOVERNANCE

    def __init__(self, desk: Desk = Desk.GOVERNANCE, *, _seal: object) -> None:
        super().__init__(desk, _seal=_seal)

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("GovernanceToken is final")


class AuditorToken:
    """Deliberately NOT a DeskToken: it can open only read-only sessions and never a desk loop."""

    __slots__ = ("__weakref__",)

    def __init__(self, *, _seal: object) -> None:
        if _seal is not _SEAL:
            raise AuthError("AuditorToken must be minted by nour.core.tokens.mint")

    def __init_subclass__(cls, **kwargs: Any) -> None:
        raise TypeError("AuditorToken is final")

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("AuditorToken is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("AuditorToken is immutable")

    def __reduce__(self) -> str | tuple[object, ...]:
        raise TypeError("AuditorToken cannot be pickled or copied across processes")

    def __copy__(self) -> AuditorToken:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> AuditorToken:
        return self

    def __repr__(self) -> str:
        return "AuditorToken()"


AnyToken = DeskToken | AuditorToken

_MINTED: weakref.WeakSet[DeskToken | AuditorToken] = weakref.WeakSet()
"""Every token ``mint`` produced and that is still alive; nothing else is ever a capability."""


def is_minted(token: object) -> bool:
    """True only for a token ``mint`` produced (SPEC §5 §13): the check behind every wall that
    would otherwise be a bare ``isinstance``."""
    try:
        return token in _MINTED
    except TypeError:  # unhashable / unweakrefable objects are never tokens
        return False


def require_minted(token: object, what: str = "this capability") -> DeskToken | AuditorToken:
    """Return ``token`` if ``mint`` produced it, else raise ``AuthError`` (never a desk or
    scope error: a forged token is an authentication failure, SPEC §13)."""
    if not isinstance(token, DeskToken | AuditorToken):
        raise TypeError(f"{what} takes a DeskToken or AuditorToken, got {type(token).__name__}")
    if not is_minted(token):
        raise AuthError(
            f"{type(token).__name__} was not minted by nour.core.tokens.mint; refused for {what}"
        )
    return token


@overload
def mint(kind: Literal["operator"]) -> OperatorToken: ...
@overload
def mint(kind: Literal["assistant"]) -> AssistantToken: ...
@overload
def mint(kind: Literal["governance"]) -> GovernanceToken: ...
@overload
def mint(kind: Literal["auditor"]) -> AuditorToken: ...
def mint(kind: TokenKind) -> AnyToken:
    """The single producer of capability tokens (SPEC §5 §13; DESIGN §3.2).

    Called only by the process bootstrap (``nour/runtime/bootstrap.py``), the test harness and
    ``tests/conftest.py``; the AST walls test enforces that. Unknown kinds raise ``ValueError``.
    Every token minted here is recorded for :func:`is_minted`.
    """
    token: AnyToken
    if kind == "operator":
        token = OperatorToken(_seal=_SEAL)
    elif kind == "assistant":
        token = AssistantToken(_seal=_SEAL)
    elif kind == "governance":
        token = GovernanceToken(_seal=_SEAL)
    elif kind == "auditor":
        token = AuditorToken(_seal=_SEAL)
    else:
        raise ValueError(f"unknown token kind {kind!r}")
    _MINTED.add(token)
    return token
