# SPDX-License-Identifier: MPL-2.0
"""Scriptable session commands over the shared application layer."""

import json
import sys
from pathlib import Path

from corinthos.context.redaction import sanitize_text
from corinthos.contracts import ContractError


def configure(commands):
    command = commands.add_parser("session", help="Supervise a native agent for a Work Item.")
    actions = command.add_subparsers(dest="session_action", required=True)
    for action in ("start", "list", "show", "events", "cancel", "recover"):
        parser = actions.add_parser(action)
        parser.add_argument("--root", default=str(Path.cwd()))
        if action in {"show", "events", "cancel", "recover"}:
            parser.add_argument("session_id")
        if action == "start":
            parser.add_argument("--work", dest="work_item_id", required=True)
            parser.add_argument("--provider", required=True)
            parser.add_argument("--timeout", dest="timeout_seconds", type=int, default=120)
            parser.add_argument("--read-only", action="store_true")
        if action in {"start", "recover"}:
            parser.add_argument("--instruction", default="")
            parser.add_argument("--executable", help="Explicit local native executable override.")
        if action in {"list", "events"}:
            parser.add_argument("--limit", type=int, default=10)
        if action == "list":
            parser.add_argument("--offset", type=int, default=0)
        if action == "cancel":
            parser.add_argument("--generation", type=int)
        if action == "events":
            parser.add_argument("--after", dest="after_sequence", type=int, default=0)
        parser.set_defaults(handler=_run)


def _run(arguments):
    from corinthos.application.sessions import SessionApplication, session_view

    app = SessionApplication()
    params = vars(arguments).copy()
    action = params.pop("session_action")
    params.pop("command")
    params.pop("handler")
    operation = getattr(app, "get" if action == "show" else action)
    try:
        result = operation(**params)
        if action in {"start", "recover"}:
            # This terminal owns its worker until it exits. A second terminal
            # can inspect the persisted session and request cancellation.
            print(json.dumps(result), flush=True)
            result = session_view(app.supervisor.wait(params["root"], result["session_id"]))
        print(json.dumps(result), flush=True)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ContractError, OSError, RuntimeError):
        print("The requested local session operation is unavailable.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(json.dumps({"error": sanitize_text(str(error))}), file=sys.stderr)
        return 1
    finally:
        app.supervisor.close()
