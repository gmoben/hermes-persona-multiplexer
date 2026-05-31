"""Tests for scoped lock management + graceful per-persona degradation.

Generic demo squad (alex/sam/max) only — no downstream project names.
"""

from hermes_persona_multiplexer.locks import ScopedLockManager


def test_inproc_acquire_and_conflict():
    mgr = ScopedLockManager("discord_personas")
    a = mgr.acquire("alex", "tok-A")
    b = mgr.acquire("sam", "tok-B")
    assert a.acquired and b.acquired
    assert set(mgr.active_personas) == {"alex", "sam"}
    assert mgr.degraded_personas == ()

    # re-using a token within one manager conflicts -> the persona degrades,
    # the others stay online.
    dup = mgr.acquire("max", "tok-A")
    assert dup.acquired is False
    assert "in use" in (dup.error or "")
    assert "max" in mgr.degraded_personas


def test_release_all_frees_tokens():
    mgr = ScopedLockManager("discord_personas")
    mgr.acquire("alex", "tok-A")
    mgr.release_all()
    # token is free again after release
    again = ScopedLockManager("discord_personas")  # fresh in-proc set
    assert again.acquire("alex", "tok-A").acquired is True


def test_injected_acquire_failure_degrades():
    def deny(_platform, _token):
        return False

    mgr = ScopedLockManager("discord_personas", acquire=deny)
    res = mgr.acquire("alex", "tok-A")
    assert res.acquired is False
    assert mgr.active_personas == ()
    assert "alex" in mgr.degraded_personas


def test_injected_acquire_exception_degrades_without_raising():
    def boom(_platform, _token):
        raise RuntimeError("lock backend down")

    mgr = ScopedLockManager("discord_personas", acquire=boom)
    res = mgr.acquire("alex", "tok-A")  # must not raise
    assert res.acquired is False
    assert res.error == "lock backend down"
    assert "alex" in mgr.degraded_personas


def test_release_invokes_injected_release():
    released = []
    mgr = ScopedLockManager(
        "discord_personas",
        acquire=lambda p, t: True,
        release=lambda p, t: released.append((p, t)),
    )
    mgr.acquire("alex", "tok-A")
    mgr.acquire("sam", "tok-B")
    mgr.release_all()
    assert ("discord_personas", "tok-A") in released
    assert ("discord_personas", "tok-B") in released
