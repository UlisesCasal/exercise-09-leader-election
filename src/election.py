import os
import threading
import time
from typing import Optional

import requests

NODE_ID = int(os.environ.get("NODE_ID", 1))

# PEERS format: "id:url,id:url" e.g. "2:http://node-2:8080,3:http://node-3:8080"
PEERS: dict[int, str] = {}
for _entry in os.environ.get("PEERS", "").split(","):
    _entry = _entry.strip()
    if not _entry:
        continue
    try:
        _peer_id, _peer_url = _entry.split(":", 1)
        PEERS[int(_peer_id)] = _peer_url
    except (ValueError, TypeError):
        pass

_current_leader: Optional[int] = None
_election_in_progress = False
_state_lock = threading.Lock()

ELECTION_TIMEOUT = 3.0
HEARTBEAT_INTERVAL = 5.0


def start_election() -> None:
    global _current_leader, _election_in_progress

    with _state_lock:
        if _election_in_progress:
            return
        _election_in_progress = True

    # Send ELECTION to all nodes with higher ID
    higher_peers = {pid: url for pid, url in PEERS.items() if pid > NODE_ID}
    got_ok = False

    for _pid, url in higher_peers.items():
        try:
            r = requests.post(
                f"{url}/api/election/message",
                json={"sender_id": NODE_ID},
                timeout=ELECTION_TIMEOUT,
            )
            if r.status_code == 200:
                got_ok = True
        except Exception:
            pass

    if not got_ok:
        # No higher node responded → we are the winner
        declare_victory()
    else:
        # A higher node is alive; it will eventually send COORDINATOR.
        # If it never does (it crashes too), the heartbeat loop will re-trigger.
        threading.Timer(ELECTION_TIMEOUT * 2, _clear_election_flag).start()


def _clear_election_flag() -> None:
    global _election_in_progress
    with _state_lock:
        _election_in_progress = False


def handle_election_message(sender_id: int) -> None:
    """Received ELECTION from a lower-ID node. Respond OK (caller returns 200)
    and start our own election in the background."""
    threading.Thread(target=start_election, daemon=True).start()


def declare_victory() -> None:
    global _current_leader, _election_in_progress
    with _state_lock:
        _current_leader = NODE_ID
        _election_in_progress = False

    # Broadcast COORDINATOR to all peers
    for _pid, url in PEERS.items():
        try:
            requests.post(
                f"{url}/api/coordinator",
                json={"leader_id": NODE_ID},
                timeout=2.0,
            )
        except Exception:
            pass


def set_leader(leader_id: int) -> None:
    global _current_leader, _election_in_progress
    with _state_lock:
        _current_leader = leader_id
        _election_in_progress = False


def get_leader() -> Optional[int]:
    with _state_lock:
        return _current_leader


def _clear_leader() -> None:
    global _current_leader
    with _state_lock:
        _current_leader = None


def heartbeat_loop() -> None:
    # Stagger startup so not all nodes trigger elections simultaneously
    time.sleep(2.0 + NODE_ID * 0.5)
    threading.Thread(target=start_election, daemon=True).start()

    while True:
        time.sleep(HEARTBEAT_INTERVAL)
        leader = get_leader()

        if leader is None:
            threading.Thread(target=start_election, daemon=True).start()
            continue

        if leader == NODE_ID:
            continue  # We are the leader, nothing to check

        leader_url = PEERS.get(leader)
        if not leader_url:
            _clear_leader()
            threading.Thread(target=start_election, daemon=True).start()
            continue

        try:
            r = requests.get(f"{leader_url}/health", timeout=2.0)
            if r.status_code != 200:
                raise Exception("unhealthy")
        except Exception:
            _clear_leader()
            threading.Thread(target=start_election, daemon=True).start()
