"""Scenario g: task leases -- expiry, takeover, and fencing of the old owner."""

from __future__ import annotations

import pytest

from app.durable.errors import LeaseConflictError, LeaseFencingError

from tests.durable.conftest import FakeClock, Stores
from tests.durable.scenarios import scenario_lease_takeover


def test_scenario_g_lease_takeover(tmp_path) -> None:
    detail = scenario_lease_takeover(tmp_path)
    assert detail["fenced"] is True  # worker-A's run aborted without committing s2
    assert detail["original_owner_rejected"] is True
    assert detail["run_status"] == "succeeded"  # worker-B continued to completion
    assert detail["s1_calls"] == 1  # single continuation: no duplicated work
    assert detail["s2_calls"] == 1


def test_acquire_conflict_while_holder_alive(tmp_path) -> None:
    stores = Stores(tmp_path / "conflict.db")
    stores.leases.acquire("run-1", "alice", ttl_s=10.0)
    with pytest.raises(LeaseConflictError):
        stores.leases.acquire("run-1", "bob", ttl_s=10.0)
    stores.close()


def test_takeover_bumps_fencing_token(tmp_path) -> None:
    clock = FakeClock()
    stores = Stores(tmp_path / "token.db", clock)
    first = stores.leases.acquire("run-1", "alice", ttl_s=10.0)
    clock.advance(11.0)
    second = stores.leases.acquire("run-1", "bob", ttl_s=10.0)
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(LeaseFencingError):
        stores.leases.validate("run-1", "alice", first.fencing_token)
    stores.leases.validate("run-1", "bob", second.fencing_token)
    stores.close()


def test_expired_lease_cannot_commit_even_for_rightful_owner(tmp_path) -> None:
    clock = FakeClock()
    stores = Stores(tmp_path / "expired.db", clock)
    lease = stores.leases.acquire("run-1", "alice", ttl_s=5.0)
    clock.advance(6.0)
    with pytest.raises(LeaseFencingError, match="expired"):
        stores.leases.validate("run-1", "alice", lease.fencing_token)
    # Same owner can re-acquire (renew) with a fresh token.
    renewed = stores.leases.acquire("run-1", "alice", ttl_s=5.0)
    assert renewed.fencing_token == lease.fencing_token + 1
    stores.leases.validate("run-1", "alice", renewed.fencing_token)
    stores.close()


def test_stale_token_rejected(tmp_path) -> None:
    stores = Stores(tmp_path / "stale.db")
    lease = stores.leases.acquire("run-1", "alice", ttl_s=10.0)
    stores.leases.acquire("run-1", "alice", ttl_s=10.0)  # renew -> token 2
    with pytest.raises(LeaseFencingError, match="stale token"):
        stores.leases.validate("run-1", "alice", lease.fencing_token)
    stores.close()


def test_force_takeover_of_live_lease(tmp_path) -> None:
    stores = Stores(tmp_path / "force.db")
    stores.leases.acquire("run-1", "alice", ttl_s=100.0)
    lease = stores.leases.acquire("run-1", "bob", ttl_s=10.0, force=True)
    assert lease.owner == "bob"
    with pytest.raises(LeaseFencingError):
        stores.leases.validate("run-1", "alice", 1)
    stores.close()
