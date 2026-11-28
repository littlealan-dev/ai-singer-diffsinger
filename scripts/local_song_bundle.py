"""The on-disk format shared by the local Song export and import commands."""

import base64
import hashlib
import json
import math
from datetime import datetime, timezone

from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.cloud.firestore import GeoPoint

VERSION = 1
_TYPE = "$firestore"


def path_component(value):
    if (not isinstance(value, str) or value in {"", ".", ".."} or "/" in value
            or "\\" in value or "\x00" in value):
        raise ValueError("Invalid Song, owner or job identifier.")
    return value


def bundle_path(root, relative):
    """Resolve an object within its bundle directory."""
    if not isinstance(relative, str):
        raise ValueError("Invalid bundle path.")
    parts = [path_component(part) for part in relative.split("/")]
    path = root.joinpath(*parts).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Bundle path escapes its directory.")
    return path


def file_integrity(path):
    digest = hashlib.md5(usedforsecurity=False)
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"size": size, "md5": digest.hexdigest()}


def encode(value):
    if isinstance(value, datetime):
        if isinstance(value, DatetimeWithNanoseconds):
            timestamp = value.rfc3339()
        else:
            timestamp = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {_TYPE: "timestamp", "value": timestamp}
    if isinstance(value, bytes):
        return {_TYPE: "bytes", "value": base64.b64encode(value).decode("ascii")}
    if isinstance(value, GeoPoint):
        return {_TYPE: "geopoint", "value": [value.latitude, value.longitude]}
    if isinstance(value, float) and not math.isfinite(value):
        return {_TYPE: "float", "value": str(value)}
    if isinstance(value, dict):
        encoded = {key: encode(item) for key, item in value.items()}
        return {_TYPE: "map", "value": encoded} if _TYPE in encoded else encoded
    if isinstance(value, list):
        return [encode(item) for item in value]
    return value


def decode(value):
    if isinstance(value, list):
        return [decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _TYPE not in value:
        return {key: decode(item) for key, item in value.items()}
    kind, payload = value[_TYPE], value["value"]
    if kind == "timestamp":
        return DatetimeWithNanoseconds.from_rfc3339(payload)
    if kind == "bytes":
        return base64.b64decode(payload, validate=True)
    if kind == "geopoint":
        return GeoPoint(*payload)
    if kind == "float":
        return float(payload)
    if kind == "map":
        return {key: decode(item) for key, item in payload.items()}
    raise ValueError("Unknown Firestore type in bundle.")


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                    encoding="utf-8")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def storage_prefix(uid, sid):
    return f"sessions/{path_component(uid)}/{path_component(sid)}/"


def vocal_job_ids(player):
    return dict.fromkeys(path_component(track["sourceJobId"]) for track in player.get("vocals", []))


def owned_job(data, uid, sid):
    return data.get("userId") == uid and data.get("sessionId") == sid


def require_song(data):
    if not data or data.get("deletedAt") or not data.get("songCreatedAt"):
        raise ValueError("The session must be an undeleted Song.")
