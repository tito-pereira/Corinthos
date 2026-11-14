# SPDX-License-Identifier: MPL-2.0
"""Scriptable explicit import of existing local Git repositories."""

import json
import sys
from pathlib import Path

from corinthos.application import external_import as operations
from corinthos.application.cli import _pagination
from corinthos.contracts import ContractError


def configure(commands):
    command = commands.add_parser(
        "import", help="Inspect or import an existing local Git repository."
    )
    actions = command.add_subparsers(dest="import_action", required=True)
    for name in ("inspect", "commit", "status", "refresh", "files"):
        parser = actions.add_parser(name)
        if name in {"inspect", "commit"}:
            parser.add_argument("root", type=Path)
        else:
            parser.add_argument("--root", default=str(Path.cwd()))
        fields = ["root"]
        if name in {"inspect", "files"}:
            _pagination(parser)
            fields += ["limit", "offset"]
        if name in {"commit", "refresh"}:
            parser.add_argument("--digest", dest="discovery_digest", required=True)
            parser.add_argument("--confirm", dest="confirmations", action="append", default=[])
            fields += ["discovery_digest", "confirmations"]
        if name == "refresh":
            parser.add_argument("--expected-snapshot-digest", required=True)
            fields.append("expected_snapshot_digest")
        if name == "files":
            parser.add_argument("--snapshot-digest")
            fields.append("snapshot_digest")
        parser.set_defaults(handler=_run, fields=fields)


def _run(arguments):
    operation = getattr(operations, f"import_{arguments.import_action}")
    try:
        result = operation(**{field: getattr(arguments, field) for field in arguments.fields})
    except (ContractError, OSError, ValueError, RuntimeError):
        print("The local Git import operation could not be completed safely.", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return int(
        result.get("status") in {"REJECTED", "INVALID", "INCOMPLETE"}
        or result.get("error") is not None
    )
