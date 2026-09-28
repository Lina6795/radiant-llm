"""S2-8: SSE resume contract -- Last-Event-ID replay is gapless and
duplicate-free, and extra client connections never start a second run.
"""

from __future__ import annotations

import threading

from conftest import wait_run_state


def _stream_events(client, run_id, last_event_id=None):
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id is not None else {}
    events = []
    with client.stream("GET", f"/runs/{run_id}/events", headers=headers) as resp:
        current = {}
        for line in resp.iter_lines():
            if not line:
                if current:
                    events.append(current)
                    current = {}
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(": ")
            current[key] = value
            if key == "event" and value == "end":
                break
    return events


def _create_terminal_run(client):
    run_id = client.post(
        "/runs", json={"goal": "search evidence for reactor safety"}
    ).json()["run_id"]
    wait_run_state(client, run_id, {"succeeded", "failed"})
    return run_id


def test_sse_replay_gapless_no_duplicates(client):
    run_id = _create_terminal_run(client)
    full = _stream_events(client, run_id)
    all_ids = [int(e["id"]) for e in full if e.get("id")]
    assert all_ids == sorted(set(all_ids))  # strictly increasing, unique
    assert len(all_ids) >= 4

    # replaying after several cut points must return EXACTLY the later suffix
    for cut in {1, all_ids[len(all_ids) // 2], all_ids[-2]}:
        replayed = _stream_events(client, run_id, last_event_id=cut)
        replay_ids = [int(e["id"]) for e in replayed if e.get("id")]
        expected = [i for i in all_ids if i > cut]
        assert replay_ids == expected, f"cut={cut}: {replay_ids} != {expected}"


def test_duplicate_client_connections_do_not_start_second_run(client):
    run_id = _create_terminal_run(client)

    results = {}
    def consume(name):
        results[name] = _stream_events(client, run_id)

    t1 = threading.Thread(target=consume, args=("a",))
    t2 = threading.Thread(target=consume, args=("b",))
    t1.start(); t2.start(); t1.join(); t2.join()

    ids_a = [int(e["id"]) for e in results["a"] if e.get("id")]
    ids_b = [int(e["id"]) for e in results["b"] if e.get("id")]
    assert ids_a == ids_b  # both connections saw the same single sequence

    listing = client.get("/runs", params={"limit": 1000}).json()
    assert sum(1 for item in listing["items"] if item["run_id"] == run_id) == 1
    snap = client.get(f"/runs/{run_id}").json()
    assert snap["state"] == "succeeded"
