"""Small, dependency-free client for the public Ctrlka release API."""

from __future__ import annotations

import json
from typing import Any
from urllib.request import Request, urlopen


UPDATE_URL = "https://shotsync.ru/ctrlka/api/version/"


def version_key(value: str) -> tuple[int, ...]:
    """Turn a dotted numeric version into a comparable value."""
    parts = value.strip().lstrip("v").split(".")
    if not parts or any(not part.isdigit() for part in parts):
        raise ValueError(f"Invalid version: {value!r}")
    return tuple(int(part) for part in parts)


def is_newer(candidate: str, current: str) -> bool:
    candidate_key, current_key = version_key(candidate), version_key(current)
    width = max(len(candidate_key), len(current_key))
    return candidate_key + (0,) * (width - len(candidate_key)) > current_key + (0,) * (width - len(current_key))


def fetch_release_info(current_version: str, *, url: str = UPDATE_URL, timeout: float = 8) -> dict[str, Any]:
    """Fetch release metadata. Network and payload errors deliberately propagate."""
    separator = "&" if "?" in url else "?"
    request = Request(
        f"{url}{separator}current={current_version}",
        headers={"Accept": "application/json", "User-Agent": f"Ctrlka/{current_version}"},
    )
    with urlopen(request, timeout=timeout) as response:  # nosec B310: fixed HTTPS URL
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("latest"), dict):
        raise ValueError("Invalid update server response")
    return payload
