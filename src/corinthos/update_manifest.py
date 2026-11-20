# SPDX-License-Identifier: MPL-2.0
"""Authenticate the exact release manifest and its complete Windows payload."""

import base64
import json
import re
import stat
import zipfile
from hashlib import file_digest
from importlib.resources import files
from pathlib import Path

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from corinthos._paths import windows_component_is_reserved
from corinthos.contracts import ContractStore
from corinthos.runtime_gate import plain_path

MAX_MANIFEST = 4 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_PAYLOAD = 1024 * 1024 * 1024
MAX_FILES = 10000
_VERSION = re.compile(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})")
_HASH = re.compile(r"[0-9a-f]{64}")


class UpdateError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def strict_json(data: bytes, limit=MAX_MANIFEST):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key.")
            result[key] = value
        return result

    if len(data) > limit:
        raise UpdateError("INVALID_RELEASE")
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
        )
    except (ValueError, UnicodeError, RecursionError) as error:
        raise UpdateError("INVALID_RELEASE") from error


def version_tuple(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise UpdateError("INVALID_RELEASE")
    return tuple(map(int, value.split(".")))


def read_bounded(path: Path, limit=MAX_MANIFEST):
    plain_path(path, file=True)
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("INVALID_RELEASE")
    return data


def public_key() -> bytes:
    value = strict_json(
        files("corinthos.resources").joinpath("update-public-key.json").read_bytes()
    )
    try:
        if (
            set(value) != {"schema", "algorithm", "public_key"}
            or value["schema"] != 1
            or value["algorithm"] != "Ed25519"
        ):
            raise ValueError()
        key = base64.b64decode(value["public_key"], validate=True)
        if len(key) != 32:
            raise ValueError()
        return key
    except (TypeError, ValueError) as error:
        raise UpdateError("VERIFICATION_FAILED") from error


def relative_path(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 240 or "\\" in value:
        raise UpdateError("INVALID_RELEASE")
    for part in value.split("/"):
        if (
            part.casefold() in {"", ".", "..", ".corinthos", ".nukytera", ".agentforge"}
            or any(ord(char) < 32 or char in ':*?"<>|' for char in part)
            or part.endswith((".", " "))
            or windows_component_is_reserved(part)
        ):
            raise UpdateError("INVALID_RELEASE")
    return value


def _length(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise UpdateError("INVALID_RELEASE")
    return value


def _digest(value):
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise UpdateError("INVALID_RELEASE")
    return value


def verify_manifest(data: bytes, signature: bytes, installed_version: str, *, key=None):
    if len(data) > MAX_MANIFEST or len(signature) != 64:
        raise UpdateError("VERIFICATION_FAILED")
    try:
        Ed25519PublicKey.from_public_bytes(public_key() if key is None else key).verify(
            signature, data
        )
    except (ValueError, InvalidSignature, UnsupportedAlgorithm) as error:
        raise UpdateError("VERIFICATION_FAILED") from error
    value = strict_json(data)
    if (
        not isinstance(value, dict)
        or set(value)
        != {"schema", "product", "version", "platform", "storage_schema", "artifact", "payload"}
        or type(value["schema"]) is not int
        or value["schema"] != 1
        or value["product"] != "corinthos-core"
        or value["platform"] != "windows-x64"
    ):
        raise UpdateError("INVALID_RELEASE")
    if (
        type(value["storage_schema"]) is not int
        or value["storage_schema"] != ContractStore.current_schema_version()
    ):
        raise UpdateError("INCOMPATIBLE_SCHEMA")
    if version_tuple(value["version"]) <= version_tuple(installed_version):
        raise UpdateError("INVALID_RELEASE")
    artifact = value["artifact"]
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"name", "bytes", "sha256"}
        or artifact["name"] != f"Corinthos-{value['version']}-windows-x64.zip"
    ):
        raise UpdateError("INVALID_RELEASE")
    _length(artifact["bytes"], MAX_ARCHIVE)
    _digest(artifact["sha256"])
    payload = value["payload"]
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_FILES:
        raise UpdateError("INVALID_RELEASE")
    seen = set()
    total = 0
    for entry in payload:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256"}:
            raise UpdateError("INVALID_RELEASE")
        path = relative_path(entry["path"])
        if path.casefold() in seen:
            raise UpdateError("INVALID_RELEASE")
        seen.add(path.casefold())
        total += _length(entry["bytes"], MAX_ARCHIVE)
        _digest(entry["sha256"])
    if total > MAX_PAYLOAD:
        raise UpdateError("INVALID_RELEASE")
    return value


def hash_file(path: Path):
    plain_path(path, file=True)
    before = path.stat()
    with path.open("rb") as source:
        digest = file_digest(source, "sha256").hexdigest()
    after = path.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise UpdateError("VERIFICATION_FAILED")
    return digest


def verify_payload(directory: Path, manifest):
    plain_path(directory)
    expected = {entry["path"]: entry for entry in manifest["payload"]}
    parents = {
        str(parent).replace("\\", "/")
        for name in expected
        for parent in Path(name).parents
        if str(parent) != "."
    }
    actual = set()
    for path in directory.rglob("*"):
        relative = path.relative_to(directory).as_posix()
        if path.is_dir():
            plain_path(path)
            if relative not in parents:
                raise UpdateError("VERIFICATION_FAILED")
            continue
        plain_path(path, file=True)
        if relative not in expected:
            raise UpdateError("VERIFICATION_FAILED")
        entry = expected[relative]
        if path.stat().st_size != entry["bytes"] or hash_file(path) != entry["sha256"]:
            raise UpdateError("VERIFICATION_FAILED")
        actual.add(relative)
    if actual != expected.keys():
        raise UpdateError("VERIFICATION_FAILED")
    try:
        release = strict_json(read_bounded(directory / "release-manifest.json"))
        marker = strict_json(read_bounded(directory / "installation.json", 256 * 1024), 256 * 1024)
        if (
            release["product_version"] != manifest["version"]
            or release["python_package_version"] != manifest["version"]
            or release["sqlite_schema"] != manifest["storage_schema"]
            or release["rpc_protocol"] != 2
            or release["desktop_identifier"] != "org.corinthos.desktop"
            or release["build_platform"] != "windows"
            or release["architecture"] != "x86_64-pc-windows-msvc"
            or release["python_runtime"] != "3.14.10"
            or marker["schema"] != 1
            or marker["owner"] != "corinthos-core"
            or marker["state"] != "staged"
            or marker["version"] != manifest["version"]
        ):
            raise ValueError()
        inventory = {"file:" + path.replace("/", "\\") for path in expected}
        inventory |= {"directory:" + path.replace("/", "\\") for path in parents}
        if len(marker["entries"]) != len(inventory) or set(marker["entries"]) != inventory:
            raise ValueError()
    except (KeyError, TypeError, ValueError, OSError) as error:
        raise UpdateError("VERIFICATION_FAILED") from error


def extract_payload(archive: Path, directory: Path, manifest):
    if (
        archive.stat().st_size != manifest["artifact"]["bytes"]
        or hash_file(archive) != manifest["artifact"]["sha256"]
    ):
        raise UpdateError("VERIFICATION_FAILED")
    expected = {entry["path"]: entry for entry in manifest["payload"]}
    try:
        with zipfile.ZipFile(archive) as source:
            entries = source.infolist()
            if len(entries) != len(expected):
                raise UpdateError("VERIFICATION_FAILED")
            seen = set()
            for item in entries:
                name = relative_path(item.filename)
                mode = stat.S_IFMT(item.external_attr >> 16)
                if (
                    name not in expected
                    or name.casefold() in seen
                    or item.is_dir()
                    or mode not in {0, stat.S_IFREG}
                    or item.flag_bits & 1
                    or item.file_size != expected[name]["bytes"]
                ):
                    raise UpdateError("VERIFICATION_FAILED")
                seen.add(name.casefold())
            directory.mkdir(mode=0o700)
            for item in entries:
                target = directory / item.filename
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                plain_path(target.parent)
                remaining = expected[item.filename]["bytes"]
                with source.open(item) as incoming, target.open("xb") as outgoing:
                    while block := incoming.read(min(1024 * 1024, remaining + 1)):
                        remaining -= len(block)
                        if remaining < 0:
                            raise UpdateError("VERIFICATION_FAILED")
                        outgoing.write(block)
                if remaining:
                    raise UpdateError("VERIFICATION_FAILED")
    except (zipfile.BadZipFile, RuntimeError, OSError) as error:
        if isinstance(error, UpdateError):
            raise
        raise UpdateError("VERIFICATION_FAILED") from error
    verify_payload(directory, manifest)
