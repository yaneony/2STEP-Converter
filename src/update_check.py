from dataclasses import dataclass
import json
import math
import re
import threading
from urllib.parse import quote
from urllib.request import Request, urlopen

from version import VERSION

RELEASES_URL = "https://github.com/yaneony/2STEP-Converter/releases"
LATEST_RELEASE_API = "https://api.github.com/repos/yaneony/2STEP-Converter/releases/latest"
CHECK_TIMEOUT_SECONDS = 2.0
_MAX_RESPONSE_BYTES = 256 * 1024
_VERSION_PATTERN = re.compile(
    r"[vV]?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)


def _parse_version(value):
    if not isinstance(value, str):
        return None
    match = _VERSION_PATTERN.fullmatch(value)
    if match is None:
        return None
    try:
        core = tuple(int(match[i]) for i in (1, 2, 3))
    except ValueError:
        return None
    prerelease = tuple(match[4].split(".")) if match[4] else ()
    if any(item.isdigit() and len(item) > 1 and item.startswith("0") for item in prerelease):
        return None
    return core, prerelease


def _is_newer(candidate, installed):
    remote, local = _parse_version(candidate), _parse_version(installed)
    if remote is None or local is None:
        return False
    if remote[0] != local[0]:
        return remote[0] > local[0]
    remote_pre, local_pre = remote[1], local[1]
    if not remote_pre or not local_pre:
        return bool(local_pre) and not remote_pre
    for right, left in zip(remote_pre, local_pre):
        if right == left:
            continue
        if right.isdigit() and left.isdigit():
            return (len(right), right) > (len(left), left)
        if right.isdigit() != left.isdigit():
            return not right.isdigit()
        return right > left
    return len(remote_pre) > len(local_pre)


@dataclass(frozen=True)
class Release:
    version: str
    url: str


@dataclass(frozen=True)
class UpdateResult:
    status: str
    release: Release | None = None


def _fetch_release(timeout):
    request = Request(LATEST_RELEASE_API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"2STEP-Converter/{VERSION}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urlopen(request, timeout=timeout) as response:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
    if len(body) > _MAX_RESPONSE_BYTES:
        return None
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        return None
    tag = data.get("tag_name")
    parsed = _parse_version(tag)
    if parsed is None or parsed[1]:
        return None
    return Release(tag.lstrip("vV"), f"{RELEASES_URL}/tag/{quote(tag, safe='')}")


def check_update_status(installed=VERSION, *, timeout=CHECK_TIMEOUT_SECONDS):
    try:
        valid_timeout = (not isinstance(timeout, bool) and isinstance(timeout, (int, float))
                         and math.isfinite(timeout) and timeout > 0)
    except OverflowError:
        valid_timeout = False
    if _parse_version(installed) is None or not valid_timeout:
        return UpdateResult('unavailable')
    timeout = min(timeout, CHECK_TIMEOUT_SECONDS)
    finished = threading.Event()
    result = []

    def fetch():
        try:
            result.append(_fetch_release(timeout))
        except (OSError, ValueError, TypeError):
            pass
        finally:
            finished.set()

    worker = threading.Thread(target=fetch, name="release-check", daemon=True)
    try:
        worker.start()
    except RuntimeError:
        return UpdateResult('unavailable')
    if not finished.wait(timeout) or not result or result[0] is None:
        return UpdateResult('unavailable')
    release = result[0]
    status = 'update' if _is_newer(release.version, installed) else 'current'
    return UpdateResult(status, release)
