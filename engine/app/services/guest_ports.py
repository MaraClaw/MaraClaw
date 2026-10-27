import secrets

from pywhales.exceptions import DockerException

PORT_MIN = 18789
PORT_MAX = 28788
START_ATTEMPTS = 8


def guest_port_candidates(preferred: int | None) -> tuple[int, ...]:
    candidates = [preferred] if preferred is not None and PORT_MIN <= preferred <= PORT_MAX else []
    while len(candidates) < START_ATTEMPTS:
        # Draw without replacement, including when a patched RNG repeatedly returns the same number.
        remaining = [port for port in range(PORT_MIN, PORT_MAX + 1) if port not in candidates]
        candidates.append(remaining[secrets.randbelow(len(remaining))])
    return tuple(candidates)


def is_port_bind_conflict(error: DockerException) -> bool:
    stderr = (error.stderr or "").lower()
    return "port is already allocated" in stderr or "address already in use" in stderr
