# SPDX-License-Identifier: MPL-2.0
"""Offline release signing; this tool never publishes or deploys a release."""

import argparse
import base64
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from corinthos.runtime_gate import plain_path
from corinthos.update_manifest import (
    MAX_FILES,
    hash_file,
    public_key,
    relative_path,
    strict_json,
    verify_manifest,
    verify_payload,
    version_tuple,
)


def create_bundle(candidate: Path, key: Ed25519PrivateKey, output: Path):
    """Sign a complete staged, independently health-checked Windows candidate."""
    plain_path(candidate)
    plain_path(output)
    if output.resolve().is_relative_to(candidate.resolve()):
        raise ValueError("Keep release outputs outside the installation payload.")
    release = strict_json((candidate / "release-manifest.json").read_bytes())
    version = release["product_version"]
    version_tuple(version)
    payload = []
    for path in sorted(candidate.rglob("*")):
        if path.is_dir():
            plain_path(path)
            continue
        plain_path(path, file=True)
        name = relative_path(path.relative_to(candidate).as_posix())
        payload.append({"path": name, "bytes": path.stat().st_size, "sha256": hash_file(path)})
        if len(payload) > MAX_FILES:
            raise ValueError("The candidate exceeds the supported file inventory.")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = output / f"Corinthos-{version}-windows-x64.zip"
    # Exclusive output avoids replacing an already reviewed release artifact.
    with (
        archive.open("xb") as destination,
        zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as bundle,
    ):
        for entry in payload:
            bundle.write(candidate / entry["path"], entry["path"])
    value = {
        "schema": 2,
        "product": "corinthos-core",
        "version": version,
        "platform": "windows-x64",
        "workspace_format": release["workspace_format"],
        "artifact": {
            "name": archive.name,
            "bytes": archive.stat().st_size,
            "sha256": hash_file(archive),
        },
        "payload": payload,
    }
    data = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()
    signature = key.sign(data)
    # A synthetic predecessor verifies the contract without permitting same-version installation.
    verify_manifest(data, signature, "0.0.0", key=key.public_key().public_bytes_raw())
    verify_payload(candidate, value)
    for name, content in (("corinthos-update.json", data), ("corinthos-update.sig", signature)):
        with (output / name).open("xb") as destination:
            destination.write(content)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    generate = actions.add_parser("generate-key")
    generate.add_argument("--private-key", type=Path, required=True)
    generate.add_argument("--public-key", type=Path, required=True)
    sign = actions.add_parser("bundle")
    sign.add_argument("--candidate", type=Path, required=True)
    sign.add_argument("--private-key", type=Path, required=True)
    sign.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        plain_path(args.private_key, file=True)
        if args.action == "generate-key":
            if args.private_key.resolve().is_relative_to(Path.cwd().resolve()):
                raise ValueError(
                    "Keep the private signing key outside the checkout and build outputs."
                )
            if (
                args.private_key.exists()
                or args.public_key.exists()
                or any(args.private_key.parent.iterdir())
            ):
                raise ValueError(
                    "Use an empty dedicated key directory and a new public-key output."
                )
            if os.name == "nt":
                script = """$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().User
$directory = $env:CORINTHOS_SIGNING_KEY_DIRECTORY
$targets = @($directory)
if (Test-Path -LiteralPath $env:CORINTHOS_SIGNING_KEY_FILE) { $targets += $env:CORINTHOS_SIGNING_KEY_FILE }
foreach ($target in $targets) {
$item = Get-Item -LiteralPath $target -Force
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The signing path is redirected.' }
$acl = Get-Acl -LiteralPath $target
if ($acl.Owner -ne $identity.Translate([Security.Principal.NTAccount]).Value) {
    throw 'The dedicated signing paths must belong to the current user.'
}
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleSpecific($rule) }
if ($item.PSIsContainer) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    [IO.Directory]::SetAccessControl($target, $acl)
} else {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'Allow'))
    [IO.File]::SetAccessControl($target, $acl)
}
$actual = Get-Acl -LiteralPath $target
if (!$actual.AreAccessRulesProtected -or @($actual.Access | Where-Object {
    $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Translate(
        [Security.Principal.SecurityIdentifier]).Value -ne $identity.Value
}).Count) { throw 'The signing path ACL is not restricted to its owner.' }
}
"""
                acl_command = [
                    str(
                        Path(os.environ["SystemRoot"])
                        / "System32/WindowsPowerShell/v1.0/powershell.exe"
                    ),
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    base64.b64encode(script.encode("utf-16le")).decode(),
                ]
                acl_environment = {
                    **os.environ,
                    "CORINTHOS_SIGNING_KEY_DIRECTORY": str(args.private_key.parent),
                    "CORINTHOS_SIGNING_KEY_FILE": str(args.private_key),
                }
                subprocess.run(
                    acl_command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=acl_environment,
                )
            else:
                args.private_key.parent.chmod(0o700)
            key = Ed25519PrivateKey.generate()
            descriptor = os.open(args.private_key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as destination:
                destination.write(
                    key.private_bytes(
                        serialization.Encoding.Raw,
                        serialization.PrivateFormat.Raw,
                        serialization.NoEncryption(),
                    )
                )
            if os.name == "nt":
                subprocess.run(
                    acl_command,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=acl_environment,
                )
            value = {
                "schema": 1,
                "algorithm": "Ed25519",
                "public_key": base64.b64encode(key.public_key().public_bytes_raw()).decode(),
            }
            with args.public_key.open("x", encoding="utf-8") as destination:
                json.dump(value, destination, indent=2)
                destination.write("\n")
            print(
                "Created the dedicated signing key and public trust input. Keep the private key offline and backed up securely."
            )
        else:
            if any(
                args.private_key.resolve().is_relative_to(path.resolve())
                for path in (args.candidate, args.output)
            ):
                raise ValueError(
                    "Keep the private signing key outside candidate and release outputs."
                )
            key = Ed25519PrivateKey.from_private_bytes(args.private_key.read_bytes())
            if key.public_key().public_bytes_raw() != public_key():
                raise ValueError(
                    "The signing key does not match the application-pinned public key."
                )
            create_bundle(args.candidate, key, args.output)
            print(
                "Created the signed manifest, signature and complete Windows bundle. Nothing was published."
            )
        return 0
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Release signing stopped: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
