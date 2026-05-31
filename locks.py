"""Per-token scoped lock management with graceful per-persona degradation.

Hermes prevents two gateway processes from using the same bot token via
``gateway.status.acquire_scoped_lock`` / ``release_scoped_lock``. The
multiplexer runs N Discord clients in *one* process, so it must take one lock
per persona token and — critically — must **not** tear down every persona when
a single token is already in use or invalid. It degrades: the conflicting
persona is disabled, the rest stay online.

The acquire/release primitives are injected so this module is testable without
Hermes; :mod:`adapter` wires in the real ones.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

AcquireFn = Callable[[str, str], bool]  # (platform, token) -> acquired?
ReleaseFn = Callable[[str, str], None]


@dataclass(frozen=True)
class PersonaLockResult:
    persona_id: str
    acquired: bool
    error: str | None = None


class ScopedLockManager:
    """Acquire/track/release scoped locks for each persona token.

    Falls back to an in-process registry when no ``acquire`` callable is
    injected (used in tests and when running outside a Hermes gateway).
    """

    def __init__(
        self,
        platform: str,
        acquire: AcquireFn | None = None,
        release: ReleaseFn | None = None,
    ) -> None:
        self.platform = platform
        self._held: dict[str, str] = {}  # persona_id -> token
        self._results: dict[str, PersonaLockResult] = {}
        self._inproc_taken: set[str] = set()
        self._acquire = acquire or self._default_acquire
        self._release = release or self._default_release

    def _default_acquire(self, platform: str, token: str) -> bool:
        key = f"{platform}:{token}"
        if key in self._inproc_taken:
            return False
        self._inproc_taken.add(key)
        return True

    def _default_release(self, platform: str, token: str) -> None:
        self._inproc_taken.discard(f"{platform}:{token}")

    def acquire(self, persona_id: str, token: str) -> PersonaLockResult:
        """Try to lock one persona's token. Never raises — failures degrade."""
        try:
            ok = bool(self._acquire(self.platform, token))
            result = PersonaLockResult(
                persona_id,
                acquired=ok,
                error=None if ok else "token already in use by another profile",
            )
            if ok:
                self._held[persona_id] = token
        except Exception as exc:  # noqa: BLE001 — degrade, don't crash the gateway
            result = PersonaLockResult(persona_id, acquired=False, error=str(exc))
        self._results[persona_id] = result
        return result

    def release_all(self) -> None:
        for persona_id, token in list(self._held.items()):
            try:
                self._release(self.platform, token)
            finally:
                self._held.pop(persona_id, None)

    @property
    def active_personas(self) -> tuple[str, ...]:
        return tuple(pid for pid, r in self._results.items() if r.acquired)

    @property
    def degraded_personas(self) -> tuple[str, ...]:
        return tuple(pid for pid, r in self._results.items() if not r.acquired)

    @property
    def results(self) -> dict[str, PersonaLockResult]:
        return dict(self._results)
