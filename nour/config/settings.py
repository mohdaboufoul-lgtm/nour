"""Process settings from the environment (DESIGN §3.6 ``nour/config/settings.py``; SPEC §13 §16).

``Settings`` reads ``NOUR_*`` variables (prefix ``NOUR_``, case-insensitive) through
pydantic-settings; init keyword arguments win over the environment, which is how tests build a
hermetic instance. Unknown ``NOUR_*`` variables are ignored (docker-compose and ``.env.example``
carry infrastructure knobs — ``NOUR_TIMEZONE``, ``NOUR_CARD_ISSUER`` … — that the brain does
not read here).

The owner's passphrase is deliberately not a setting and never will be: it is stored only as an
argon2 hash in the ``owner`` row and verified server-side at ingress (SPEC §6 §13). ``.env.example``
says the same; ``NOUR_PASSPHRASE`` in the environment is ignored like any unknown variable.

One vocabulary (DESIGN §3.6): ``NOUR_ENV`` is ``test`` | ``staging`` | ``prod`` and
``NOUR_SECRETS_BACKEND`` is ``fake`` | ``aws`` | ``azure``; ``.env.example``, ``docker-compose.yml``
and the CI workflow spell them the same way. There are no aliases: an old spelling such as
``development`` or ``env`` is a loud ``ValidationError`` at start-up, never a silent remap.
``prod`` with the fake secrets backend is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nour.core.types import Desk


class Settings(BaseSettings):
    """§16: runtime knobs per process, env prefix ``NOUR_``.

    ``dry_run`` defaults to true (§8): drafts and logs, no sends, no spend; production flips it per
    the playbook once the phase 0 gate passes. ``desk`` is set only for ``nour desk <desk>``.
    """

    model_config = SettingsConfigDict(
        env_prefix="NOUR_",
        case_sensitive=False,
        extra="ignore",
        frozen=True,
        env_file=None,
    )

    env: Literal["test", "staging", "prod"] = "test"
    database_url: str = "sqlite+pysqlite:///nour.db"
    config_dir: Path = Path("config")
    prompts_dir: Path = Path("prompts")
    desk: Desk | None = None
    region: str = "me-central-1"
    secrets_backend: Literal["fake", "aws", "azure"] = "fake"
    dry_run: bool = True
    stamp_key_name: str = "auth/stamp-key"
    leakguard_key_name: str = "governance/leakguard-key"
    moona_database_url: str = "sqlite+pysqlite:///moona.sqlite3"
    """Moona's own store (docs/MOONA.md §7): never the brain's database, so the desks' Alembic
    chain sees no drift and his tables stay his alone."""

    @field_validator("env", "secrets_backend", mode="before")
    @classmethod
    def _trim(cls, value: Any) -> Any:
        """Whitespace and case are forgiven; the word itself is not (no aliases)."""
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("desk", mode="before")
    @classmethod
    def _empty_desk_is_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _production_never_fakes_secrets(self) -> Settings:
        if self.env == "prod" and self.secrets_backend == "fake":
            raise ValueError(
                "NOUR_SECRETS_BACKEND must name a real secrets manager in prod (SPEC §13)"
            )
        return self

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")
