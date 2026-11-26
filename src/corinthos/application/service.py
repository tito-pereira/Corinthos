# SPDX-License-Identifier: MPL-2.0
"""Explicit operations; no built-in development stages or project governance."""

import inspect
import json
import platform
from pathlib import Path

from corinthos import __version__, continuity, gui, providers, runtime, updater, workspace
from corinthos.errors import IntegrityError
from corinthos.skills import list_skills, show_skill
from corinthos.user_state import read_state, update_state


def projects_list():
    return {
        "projects": read_state("recent-projects.json", {"version": 1, "projects": []}).get(
            "projects", []
        )
    }


def _remember(project):
    def transform(value):
        rows = value.get("projects", [])
        rows = [row for row in rows if isinstance(row, dict) and row.get("root") != project["root"]]
        hint = {
            key: project[key]
            for key in ("root", "project_id", "workspace_format", "name")
            if key in project
        }
        result = {"version": 1, "projects": [hint, *rows][:64]}
        while len(json.dumps(result).encode()) > 120000:
            result["projects"].pop()
        return result

    update_state("recent-projects.json", {"version": 1, "projects": []}, transform)
    return project


def project_init(root):
    return _remember(workspace.initialize(root))


def project_open(root):
    return _remember(workspace.open_project(root))


def diagnostics():
    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "workspace_format": workspace.WORKSPACE_FORMAT,
        "gui_pages": ["main", "project"],
    }


OPERATIONS = {
    "system.version": lambda: {"version": __version__},
    "system.diagnostics": diagnostics,
    "project.init": project_init,
    "project.open": project_open,
    "project.status": workspace.status,
    "projects.list": projects_list,
    "providers.status": lambda root=None: {"providers": providers.show_providers(root)},
    "provider.configure": providers.configure_provider,
    "skill.list": lambda root: {"skills": list_skills(root)},
    "skill.show": show_skill,
    "skill.run": runtime.start,
    "run.get": runtime.get,
    "run.list": lambda root: {"runs": runtime.list_runs(root)},
    "run.cancel": runtime.cancel,
    "run.integrate": runtime.integrate,
    "run.discard": runtime.discard,
    "continuity.show": continuity.show,
    "continuity.save": continuity.save,
    "cleanup": continuity.cleanup,
    "update.check": updater.update_check,
    "update.start": updater.update_start,
    "update.status": updater.update_status,
    "update.finish": updater.update_finish,
    "gui.projects": gui.projects,
    "gui.create": gui.create,
    "gui.open": gui.open_selected,
    "gui.import": gui.import_selected,
    "gui.operation.get": gui.operation_get,
    "gui.operation.cancel": gui.operation_cancel,
    "gui.conversation.open": gui.conversation_open,
    "gui.conversation.get": gui.conversation_get,
    "gui.conversation.send": gui.conversation_send,
    "gui.conversation.output": gui.conversation_output,
    "gui.summary": gui.summary,
    "gui.execute": gui.execute_tasks,
    "gui.run.action": gui.run_action,
    "gui.run.get": gui.run_get,
    "gui.runs": gui.runs,
    "gui.provider.configure": lambda provider, command: (
        providers.configure_provider(provider, command),
        {"providers": providers.show_providers()},
    )[1],
}


def invoke(method, params):
    if method not in OPERATIONS:
        raise KeyError(method)
    if not isinstance(params, dict):
        raise IntegrityError("Operation parameters must be an object.")
    operation = OPERATIONS[method]
    try:
        inspect.signature(operation).bind(**params)
    except TypeError as error:
        raise IntegrityError("Invalid operation parameters.") from error
    if "root" in params:
        if not isinstance(params["root"], str) or not params["root"] or len(params["root"]) > 4096:
            raise IntegrityError("Supply a bounded explicit project root.")
        params = {**params, "root": str(Path(params["root"]).absolute())}
    if method.startswith("gui."):
        try:
            return {"ok": True, "value": operation(**params)}
        except (IntegrityError, ValueError, OSError, RuntimeError) as error:
            return {"ok": False, "error": str(error)[:2048]}
    return operation(**params)
