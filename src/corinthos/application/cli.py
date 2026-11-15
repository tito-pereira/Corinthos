# SPDX-License-Identifier: MPL-2.0
"""Scriptable commands over shared application operations."""

import json
import sys
from argparse import SUPPRESS, ArgumentParser
from dataclasses import asdict
from pathlib import Path

from corinthos.application import project_status, providers_status, version, workspace
from corinthos.context.redaction import sanitize_text
from corinthos.contracts import ContractError


def configure(parser: ArgumentParser) -> None:
    parser.add_argument("--root", default=Path.cwd(), help="Project directory to inspect.")
    parser.set_defaults(handler=_status)


def _status(arguments) -> int:
    try:
        result = {
            "version": version().version,
            "project": asdict(project_status(arguments.root)),
        }
    except (OSError, ValueError, RuntimeError):
        print("Corinthos status could not read the requested local state.", file=sys.stderr)
        return 1
    try:
        result["providers"] = [asdict(provider) for provider in providers_status(arguments.root)]
    except (ContractError, OSError, ValueError, RuntimeError):
        result["providers"] = None
    print(json.dumps(result, indent=2))
    return 0


def configure_project_actions(actions) -> None:
    for name, operation, help_text in (
        ("summary", workspace.project_summary, "Read the derived project summary."),
        ("open", workspace.project_open, "Open a project and record the access explicitly."),
    ):
        command = actions.add_parser(name, help=help_text)
        command.add_argument("root", nargs="?", default=SUPPRESS)
        command.add_argument("--root", dest="root_option", default=SUPPRESS)
        command.set_defaults(handler=_workspace, operation=operation, arguments=("root",))


def _pagination(command):
    command.add_argument("--limit", type=int, default=10)
    command.add_argument("--offset", type=int, default=0)


def configure_workspace(commands) -> None:
    from corinthos.application.adaptive import work_report

    projects = commands.add_parser("projects", help="Read the local Project Registry.")
    projects_list = projects.add_subparsers(dest="projects_action", required=True).add_parser(
        "list", help="List known and recent projects."
    )
    _pagination(projects_list)
    projects_list.set_defaults(
        handler=_workspace, operation=workspace.projects_list, arguments=("limit", "offset")
    )

    work = commands.add_parser("work", help="Manage planning records for an initialized project.")
    actions = work.add_subparsers(dest="work_action", required=True)
    report = actions.add_parser("report", help="Read a derived Work Item report.")
    report.add_argument("--root", default=str(Path.cwd()))
    report.add_argument("--work", dest="work_item_id", required=True)
    report.set_defaults(
        handler=_workspace, operation=work_report, arguments=("root", "work_item_id")
    )
    for name, operation in (
        ("create", workspace.work_create),
        ("list", workspace.work_list),
        ("show", workspace.work_get),
        ("update", workspace.work_update),
        ("status", workspace.work_status),
        ("tree", workspace.work_tree),
        ("dependencies", workspace.work_dependencies),
    ):
        command = actions.add_parser(name)
        command.add_argument("--root", default=str(Path.cwd()), help="Project directory.")
        fields = ["root"]
        if name in {"show", "update", "status", "dependencies"}:
            command.add_argument("work_item_id")
            fields.append("work_item_id")
        if name == "create":
            command.add_argument("--project-id", default=SUPPRESS)
            fields.append("project_id")
        if name in {"create", "list"}:
            command.add_argument("--scope", dest="scope_level", required=name == "create")
            fields.append("scope_level")
        if name in {"create", "update"}:
            command.add_argument("--objective", required=name == "create", default=SUPPRESS)
            command.add_argument(
                "--criterion",
                dest="acceptance_criteria",
                action="append",
                required=name == "create",
                default=SUPPRESS,
            )
            command.add_argument("--validation-policy", default=SUPPRESS)
            for option, field in (("module", "affected_modules"), ("depends-on", "depends_on")):
                group = command.add_mutually_exclusive_group() if name == "update" else command
                group.add_argument(f"--{option}", dest=field, action="append", default=SUPPRESS)
                if name == "update":
                    clear = "modules" if option == "module" else "dependencies"
                    group.add_argument(
                        f"--clear-{clear}",
                        dest=field,
                        action="store_const",
                        const=(),
                        default=SUPPRESS,
                    )
            fields.extend(
                (
                    "objective",
                    "acceptance_criteria",
                    "validation_policy",
                    "affected_modules",
                    "depends_on",
                )
            )
        if name in {"create", "update", "tree"}:
            parent = command.add_mutually_exclusive_group() if name == "update" else command
            parent.add_argument("--parent", dest="parent_work_item_id", default=SUPPRESS)
            if name == "update":
                parent.add_argument(
                    "--detach",
                    dest="parent_work_item_id",
                    action="store_const",
                    const=None,
                    default=SUPPRESS,
                )
            fields.append("parent_work_item_id")
        if name == "status":
            command.add_argument("status")
            fields.append("status")
        if name == "list":
            command.add_argument("--status")
            fields.append("status")
        if name in {"update", "status"}:
            command.add_argument("--expected-updated-at", default=SUPPRESS)
            fields.append("expected_updated_at")
        if name in {"list", "tree", "dependencies"}:
            _pagination(command)
            fields.extend(("limit", "offset"))
        command.set_defaults(handler=_workspace, operation=operation, arguments=tuple(fields))


def _workspace(arguments) -> int:
    params = {
        key: getattr(arguments, key) for key in arguments.arguments if hasattr(arguments, key)
    }
    if hasattr(arguments, "root_option"):
        params["root"] = arguments.root_option
    try:
        result = arguments.operation(**params)
    except (ContractError, OSError, RuntimeError):
        print("The requested local project state is unavailable or has changed.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(json.dumps({"error": sanitize_text(str(error))}, ensure_ascii=True), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0
