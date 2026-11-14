"""Error hierarchy.

Every error the domain/data/services/phase layers raise on purpose descends
from `DBAError`. `bot/` is the only layer allowed to know about discord.py,
so this module stays free of any Discord import — v1's `core/errors.py`
mixed `DBAError` and friends into the same file as discord.py interaction
helpers, which meant importing the error hierarchy pulled in discord.py
transitively. That coupling is exactly what the layering test (Decision 4)
exists to catch.
"""
from __future__ import annotations


class DBAError(Exception):
    """Base for every error dba2 raises intentionally (vs. a bug's exception)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ConfigError(DBAError):
    """Required configuration is missing or invalid."""


class NotFoundError(DBAError):
    """A lookup by id/key found no row."""


class ValidationError(DBAError):
    """Caller-supplied data fails a domain rule."""


class PhaseError(DBAError):
    """A command or transition is invalid in the league's current phase."""


class AuthorizationError(DBAError):
    """The actor lacks permission for the requested action.

    Named to avoid shadowing the builtin `PermissionError`.
    """


class CapError(DBAError):
    """An action would violate salary-cap, dead-cap, or roster-size rules."""


class TransactionError(DBAError):
    """`data.db.transaction()` was called in a way that would silently break
    the atomicity or pool-sizing guarantees it exists to provide — a `Pool`
    forwarded into an already-open `transaction()` block, or a connection
    that could not be acquired from the pool within its timeout.
    """


class ConcurrencyError(DBAError):
    """A roster/contract mutation could not proceed because it lost a race
    with another in-flight mutation.

    Raised by the Roster Mutation Lock Protocol (RMLP, see
    `dba2/services/roster_lock.py`) when a `lock_timeout` expires acquiring
    the tier-0 league gate or a tier-1/tier-2 row lock, or when a deadlock
    (40P01) is detected against a non-participant. Always a clean, honest,
    actionable rejection — never a raw asyncpg error reaching Discord.
    """
