"""Capability tokens (DESIGN §3.2 ``nour/core/tokens.py``; SPEC §5 §13).

One token per process. A ``DeskToken`` carries the desk that a ``SessionFactory``, a ``PortSet``
and a ``ToolView`` are bound to; the subclasses fix the desk so a function typed
``token: AssistantToken`` (``VaultStore``, ``HandoffQueue.push``) cannot be handed the other desk.
``AuditorToken`` is deliberately **not** a ``DeskToken``: it opens read-only sessions only and
can never drive a desk loop.

``mint`` is the single producer. Every constructor takes the module-private ``_SEAL`` as a
keyword-only argument and raises ``AuthError`` for anything else; ``tests/unit/test_walls.py``
asserts the call ``mint(`` appears only in ``nour/runtime/bootstrap.py``,
``nour/testing/harness.py`` and ``tests/conftest.py``. Tokens are immutable (``__slots__``, no
``__dict__``, ``__setattr__`` refused after construction) and cannot be pickled, so one cannot be
copied into another process or serialised into a row.
"""

from __future__ import annotations

from typing import Literal, overload

from nour.core.errors import AuthError, DeskWallViolation
from nour.core.types import Desk

_SEAL: object = object()
"""Module-private seal: only ``mint`` passes it."""

TokenKind = Literal["operator", "assistant", "governance", "auditor"]


class DeskToken:
    """§5 §13: one token per process; carries the desk a Session, a PortSet and a ToolView are bound to."""

    __slots__ = ("desk",)
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


class AssistantToken(DeskToken):
    """``desk == Desk.ASSISTANT``: the only token the vault and the owner mailbox accept."""

    __slots__ = ()
    _fixed_desk = Desk.ASSISTANT

    def __init__(self, desk: Desk = Desk.ASSISTANT, *, _seal: object) -> None:
        super().__init__(desk, _seal=_seal)


class GovernanceToken(DeskToken):
    """``desk == Desk.GOVERNANCE``: ingress, scheduler, CLI, kill switch."""

    __slots__ = ()
    _fixed_desk = Desk.GOVERNANCE

    def __init__(self, desk: Desk = Desk.GOVERNANCE, *, _seal: object) -> None:
        super().__init__(desk, _seal=_seal)


class AuditorToken:
    """Deliberately NOT a DeskToken: it can open only read-only sessions and never a desk loop."""

    __slots__ = ()

    def __init__(self, *, _seal: object) -> None:
        if _seal is not _SEAL:
            raise AuthError("AuditorToken must be minted by nour.core.tokens.mint")

    def __setattr__(self, name: str, value: object) -> None:
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
    """
    if kind == "operator":
        return OperatorToken(_seal=_SEAL)
    if kind == "assistant":
        return AssistantToken(_seal=_SEAL)
    if kind == "governance":
        return GovernanceToken(_seal=_SEAL)
    if kind == "auditor":
        return AuditorToken(_seal=_SEAL)
    raise ValueError(f"unknown token kind {kind!r}")
