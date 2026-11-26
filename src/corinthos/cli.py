# SPDX-License-Identifier: MPL-2.0
"""Terminal adapter for project-local skills and shared installation updates."""

import argparse
import json
import subprocess
import sys

from corinthos import __version__
from corinthos.application.service import invoke
from corinthos.errors import CorinthosError
from corinthos.runtime_gate import client_runtime


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="corinthos", description="Local project workspaces and skills."
    )
    parser.add_argument("--version", action="version", version=f"corinthos {__version__}")
    families = parser.add_subparsers(dest="family")
    for family, actions in (
        ("project", ("init", "open", "status", "list")),
        ("skill", ("list", "show", "run")),
        ("run", ("get", "list", "cancel", "integrate", "discard")),
        ("continuity", ("show", "save")),
        ("cleanup", ("reports", "group", "planning")),
        ("provider", ("list", "configure")),
        ("update", ("check", "start", "status", "finish")),
    ):
        parent = families.add_parser(family)
        commands = parent.add_subparsers(dest="action", required=True)
        for action in actions:
            command = commands.add_parser(action)
            method = f"{family}.{action}"
            if family == "project" and action == "list":
                method = "projects.list"
            if family == "provider" and action == "list":
                method = "providers.status"
            command.set_defaults(method=method)
            if family in {"project", "skill", "run", "continuity", "cleanup"} and not (
                family == "project" and action == "list"
            ):
                command.add_argument(
                    "--root", required=True, help="Explicit project workspace root."
                )
            if family == "skill" and action in {"show", "run"}:
                command.add_argument("skill" if action == "run" else "name")
            if family == "skill" and action == "run":
                command.add_argument("--provider", required=True)
                command.add_argument("--group", type=int)
                command.add_argument("--instruction", default="")
                command.add_argument("--claim", action="append", dest="claims", default=[])
                command.add_argument("--timeout", type=float, default=3600)
            if family == "run" and action != "list":
                command.add_argument("run_id")
            if (family == "run" and action == "discard") or family == "cleanup":
                command.add_argument("--summary", required=True)
            if family == "continuity" and action == "save":
                for field in ("completed", "pending", "blockers", "verification", "resume"):
                    command.add_argument("--" + field, default=argparse.SUPPRESS)
            if family == "cleanup":
                command.set_defaults(method="cleanup", kind=action)
                if action in {"reports", "group"}:
                    command.add_argument("--group", type=int, required=True)
                else:
                    command.add_argument("--resolved-decisions", action="store_true")
            if family == "provider" and action == "configure":
                command.add_argument("provider")
                command.add_argument("--command-json", required=True)
            if family == "update" and action in {"start", "status", "finish"}:
                command.add_argument("--operation-id", required=action != "status")
            if family == "update" and action == "finish":
                command.add_argument("--restart-gui", action="store_true")
    args = vars(parser.parse_args(argv))
    method = args.pop("method", None)
    args.pop("family", None)
    args.pop("action", None)
    if method is None:
        parser.print_help()
        return 0
    try:
        if "command_json" in args:
            args["command"] = json.loads(args.pop("command_json"))
        with client_runtime():
            print(json.dumps(invoke(method, args), ensure_ascii=True, indent=2))
    except (CorinthosError, ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0
