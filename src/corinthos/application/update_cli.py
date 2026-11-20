# SPDX-License-Identifier: MPL-2.0
"""Explicit installed-product updates through the shared application service."""

import json
import sys

from corinthos.application import updates


def configure(commands):
    parser = commands.add_parser(
        "update", help="Check, stage and explicitly finish a signed product update."
    )
    actions = parser.add_subparsers(dest="update_action")
    parser.set_defaults(handler=_run, update_action="status", operation_id=None)
    for name in ("check", "status", "start", "finish"):
        action = actions.add_parser(name)
        if name != "check":
            action.add_argument("operation_id", nargs="?" if name == "status" else None)
        action.set_defaults(handler=_run)


def _run(arguments):
    try:
        operation = getattr(updates, f"update_{arguments.update_action}")
        result = (
            operation() if arguments.update_action == "check" else operation(arguments.operation_id)
        )
    except (OSError, RuntimeError, ValueError):
        print(
            "The product update could not proceed safely. Check the installation and updater state.",
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return int(
        result["error"] is not None
        or result["phase"] in {"FAILED", "RECOVERY_REQUIRED", "ROLLED_BACK", "UNSUPPORTED"}
    )
