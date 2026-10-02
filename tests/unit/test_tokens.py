"""nour/core/tokens.py (DESIGN §3.2; SPEC §5 §13): the DeskToken family, AuditorToken and mint.

Proves (MODULES.md "core"): mint refuses construction without the seal (``DeskToken(Desk.OPERATOR)``
raises; ``AssistantToken`` cannot carry OPERATOR); tokens are immutable and unpicklable; the four
token classes are final; only a token ``mint`` produced counts as minted, so an ``object.__new__``
look-alike opens nothing.

This file tests ``mint`` itself and the seal, so it imports ``mint`` under an alias and reads the
module-private ``_SEAL``; ``tests/unit/test_walls.py`` allows exactly that here and nowhere else.
"""

from __future__ import annotations

import copy
import pickle

import pytest

from nour.core import tokens as token_module
from nour.core.errors import AuthError, DeskWallViolation, NourError
from nour.core.tokens import (
    AnyToken,
    AssistantToken,
    AuditorToken,
    DeskToken,
    GovernanceToken,
    OperatorToken,
    is_minted,
    require_minted,
)
from nour.core.tokens import mint as make_token  # aliased: `mint(` is confined by the AST walls
from nour.core.types import Desk

# The seal is module-private; the test reaches it only to prove the desk check holds even for a
# caller that has it.
_SEAL = token_module._SEAL


# --------------------------------------------------------------------------- refusing construction


def test_desk_token_cannot_be_built_without_the_seal() -> None:
    with pytest.raises(TypeError):
        DeskToken(Desk.OPERATOR)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        DeskToken(Desk.OPERATOR, object())  # type: ignore[call-arg]
    with pytest.raises(AuthError):
        DeskToken(Desk.OPERATOR, _seal=object())
    with pytest.raises(AuthError):
        DeskToken(Desk.OPERATOR, _seal=None)
    with pytest.raises(AuthError):
        DeskToken(Desk.OPERATOR, _seal="_SEAL")


@pytest.mark.parametrize("cls", [OperatorToken, AssistantToken, GovernanceToken])
def test_subclasses_refuse_a_bad_seal(
    cls: type[OperatorToken] | type[AssistantToken] | type[GovernanceToken],
) -> None:
    with pytest.raises(TypeError):
        cls()  # type: ignore[call-arg]
    with pytest.raises(AuthError):
        cls(_seal=object())
    assert issubclass(AuthError, NourError)


def test_auditor_token_refuses_a_bad_seal() -> None:
    with pytest.raises(TypeError):
        AuditorToken()  # type: ignore[call-arg]
    with pytest.raises(AuthError):
        AuditorToken(_seal=object())


def test_a_wrong_seal_leaves_no_half_built_token() -> None:
    try:
        OperatorToken(_seal=object())
    except AuthError:
        pass
    # Nothing to inspect: construction failed before `desk` was assigned.
    with pytest.raises(AttributeError):
        _ = object.__new__(OperatorToken).desk


# --------------------------------------------------------------------------- finality and the minted registry


@pytest.mark.parametrize("cls", [OperatorToken, AssistantToken, GovernanceToken, AuditorToken])
def test_token_classes_are_final(cls: type) -> None:
    with pytest.raises(TypeError, match="final"):
        type("Evil", (cls,), {"_fixed_desk": Desk.ASSISTANT})
    with pytest.raises(TypeError, match="final"):

        class Sneaky(cls):
            pass


def test_desk_token_accepts_only_its_three_subclasses_defined_once() -> None:
    with pytest.raises(TypeError, match="sealed"):
        type("Evil", (DeskToken,), {"_fixed_desk": Desk.ASSISTANT})
    with pytest.raises(TypeError, match="sealed"):  # same name, same module, second definition
        type(
            "OperatorToken",
            (DeskToken,),
            {"__module__": "nour.core.tokens", "_fixed_desk": Desk.ASSISTANT},
        )
    with pytest.raises(TypeError, match="sealed"):

        class AssistantToken2(DeskToken):
            _fixed_desk = Desk.ASSISTANT


def test_only_minted_tokens_are_minted() -> None:
    for kind in ("operator", "assistant", "governance", "auditor"):
        token = make_token(kind)
        assert is_minted(token)
        assert require_minted(token, "test") is token
    forged = object.__new__(AssistantToken)
    object.__setattr__(forged, "desk", Desk.ASSISTANT)
    assert isinstance(forged, AssistantToken) and forged.desk is Desk.ASSISTANT
    assert not is_minted(forged)
    with pytest.raises(AuthError, match="not minted"):
        require_minted(forged, "the vault")
    assert not is_minted(object.__new__(AuditorToken))
    with pytest.raises(AuthError):
        require_minted(object.__new__(AuditorToken))
    # the seal alone does not mint: a token built with it but outside mint is still refused
    assert not is_minted(AssistantToken(_seal=_SEAL))
    assert not is_minted(DeskToken(Desk.OPERATOR, _seal=_SEAL))
    assert not is_minted(object()) and not is_minted("operator") and not is_minted(None)
    with pytest.raises(TypeError):
        require_minted("operator")
    with pytest.raises(TypeError):
        require_minted(object())


# --------------------------------------------------------------------------- fixed desks


def test_subclasses_fix_their_desk_even_with_the_real_seal() -> None:
    with pytest.raises(DeskWallViolation):
        AssistantToken(Desk.OPERATOR, _seal=_SEAL)
    with pytest.raises(DeskWallViolation):
        OperatorToken(Desk.ASSISTANT, _seal=_SEAL)
    with pytest.raises(DeskWallViolation):
        GovernanceToken(Desk.OPERATOR, _seal=_SEAL)
    with pytest.raises(DeskWallViolation):
        AssistantToken("operator", _seal=_SEAL)  # type: ignore[arg-type]
    assert AssistantToken(Desk.ASSISTANT, _seal=_SEAL).desk is Desk.ASSISTANT
    assert AssistantToken(_seal=_SEAL).desk is Desk.ASSISTANT
    assert DeskToken(Desk.ASSISTANT, _seal=_SEAL).desk is Desk.ASSISTANT  # base class: any desk


def test_base_desk_token_validates_the_desk_value() -> None:
    with pytest.raises(ValueError):
        DeskToken("vault", _seal=_SEAL)  # type: ignore[arg-type]
    assert DeskToken("governance", _seal=_SEAL).desk is Desk.GOVERNANCE  # type: ignore[arg-type]


# --------------------------------------------------------------------------- mint


@pytest.mark.parametrize(
    ("kind", "cls", "desk"),
    [
        ("operator", OperatorToken, Desk.OPERATOR),
        ("assistant", AssistantToken, Desk.ASSISTANT),
        ("governance", GovernanceToken, Desk.GOVERNANCE),
    ],
)
def test_mint_desk_tokens(kind: str, cls: type[DeskToken], desk: Desk) -> None:
    token = make_token(kind)  # type: ignore[call-overload]
    assert type(token) is cls
    assert isinstance(token, DeskToken)
    assert isinstance(token, AnyToken)
    assert token.desk is desk
    assert not isinstance(token, AuditorToken)
    assert repr(token) == f"{cls.__name__}(desk={desk.value})"


def test_mint_auditor_token_is_not_a_desk_token() -> None:
    token = make_token("auditor")
    assert type(token) is AuditorToken
    assert not isinstance(token, DeskToken)
    assert isinstance(token, AnyToken)
    assert not hasattr(token, "desk")
    assert repr(token) == "AuditorToken()"


def test_mint_rejects_unknown_kinds() -> None:
    with pytest.raises(ValueError):
        make_token("vault")  # type: ignore[call-overload]
    with pytest.raises(ValueError):
        make_token("")  # type: ignore[call-overload]


def test_each_mint_is_a_fresh_identity() -> None:
    a, b = make_token("operator"), make_token("operator")
    assert a is not b and a != b  # identity, never value, equality
    assert a.desk is b.desk


# --------------------------------------------------------------------------- immutability


@pytest.mark.parametrize("kind", ["operator", "assistant", "governance", "auditor"])
def test_tokens_are_immutable_slotted_and_unpicklable(kind: str) -> None:
    token = make_token(kind)  # type: ignore[call-overload]
    with pytest.raises(AttributeError):
        token.desk = Desk.ASSISTANT
    with pytest.raises(AttributeError):
        token.extra = 1
    with pytest.raises(AttributeError):
        _ = token.__dict__
    if isinstance(token, DeskToken):
        with pytest.raises(AttributeError):
            del token.desk
        assert token.desk is Desk(kind)
    with pytest.raises(TypeError):
        pickle.dumps(token)
    assert copy.copy(token) is token
    assert copy.deepcopy(token) is token


def test_slots_declared_on_every_class() -> None:
    assert DeskToken.__slots__ == ("desk", "__weakref__")  # __weakref__ for the minted registry
    for cls in (OperatorToken, AssistantToken, GovernanceToken):
        assert cls.__slots__ == ()
    assert AuditorToken.__slots__ == ("__weakref__",)
    for kind in ("operator", "auditor"):
        token = make_token(kind)
        with pytest.raises(AttributeError):
            _ = token.__dict__
