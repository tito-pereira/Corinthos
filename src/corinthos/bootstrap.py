# SPDX-License-Identifier: MPL-2.0
"""Installed initialization and explicit native capability discovery."""

import json
import os
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from hashlib import sha256
from importlib.resources import as_file, files
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import SchemaError as InvalidSchema

from corinthos.adapters.service import AdapterManager
from corinthos.contracts import ConflictError, ContractError, IntegrityError
from corinthos.contracts.artifacts import is_link, private_directory
from corinthos.contracts.model import canonical_json
from corinthos.project_manager.presentation import render_fields, safe_text
from corinthos.providers import SUPPORTED_PROVIDERS, configured_executables, refresh_providers
from corinthos.standard import load_standard

_PROVIDERS = SUPPORTED_PROVIDERS
_MAX_PROFILE = 128 * 1024


def _root(value, *, create=False, missing_ok=False):
    raw = Path(value)
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    part = Path(raw.anchor)
    for name in raw.parts[1:]:
        part = part.parent if name == ".." else part / name
        try:
            info = part.lstat()
        except FileNotFoundError:
            if create:
                part.mkdir(mode=0o700)
                info = part.lstat()
            elif missing_ok:
                return None
            else:
                raise IntegrityError("The authorized workspace does not exist.") from None
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise IntegrityError("The workspace cannot contain directory links or redirections.")
    path = Path(os.path.abspath(part))
    if path.resolve(strict=True) != path:
        raise IntegrityError("The workspace path changed during initialization.")
    return path


def _commands(commands, *, require_present=False):
    if not isinstance(commands, dict) or set(commands) - set(_PROVIDERS):
        raise IntegrityError("Configure only supported native adapter commands.")
    result = {}
    for provider, command in commands.items():
        if not isinstance(command, (list, tuple)) or len(command) != 1:
            raise IntegrityError("A configured adapter requires one explicit native executable.")
        path = Path(command[0]) if isinstance(command[0], str) else None
        if (
            path is None
            or not path.is_absolute()
            or (require_present and not path.is_file())
            or (os.name == "nt" and path.suffix.lower() != ".exe")
        ):
            raise IntegrityError(
                "Configure an absolute native executable path that currently exists."
            )
        result[provider] = [str(path)]
    return result


def _profile(value):
    if not isinstance(value, dict) or set(value) != {
        "version",
        "standard_id",
        "standard",
        "standard_sha256",
        "commands",
    }:
        raise IntegrityError("The saved bootstrap profile has an unsupported shape.")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["standard_id"] != "nukytera-standard"
    ):
        raise IntegrityError("The saved bootstrap profile has an unsupported version.")
    schema = json.loads(
        files("corinthos.resources").joinpath("standard.schema.json").read_text("utf-8")
    )
    try:
        Draft202012Validator(schema).validate(value["standard"])
    except ValidationError:
        raise IntegrityError("The bootstrap Standard must retain mandatory M1-M5.") from None
    expected = sha256(canonical_json(value["standard"]).encode("utf-8")).hexdigest()
    if expected != value["standard_sha256"]:
        raise IntegrityError("The bootstrap Standard digest does not match its content.")
    _commands(value["commands"])
    return value


def _read_profile(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        is_link(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > _MAX_PROFILE
    ):
        raise IntegrityError("The bootstrap profile must be a bounded ordinary private file.")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise IntegrityError("The bootstrap profile changed while opening it.")
        data = stream.read(_MAX_PROFILE + 1)
    try:
        value = json.loads(data)
        if canonical_json(value).encode("utf-8") != data:
            raise ValueError
        return _profile(value)
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise IntegrityError(
            "The bootstrap profile is invalid; preserve it for inspection."
        ) from None


def _save_profile(root, value):
    private = private_directory(root, create=True)
    path = private / "bootstrap.json"
    existing = _read_profile(path)
    if existing is not None:
        if existing != value:
            raise ConflictError("Initialization conflicts with the saved bootstrap profile.")
        return
    parent_info = private.stat()
    descriptor, name = tempfile.mkstemp(prefix=".bootstrap-", dir=private)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical_json(value).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        current = private_directory(root)
        if not os.path.samestat(parent_info, current.stat()):
            raise IntegrityError("Private storage changed during initialization.")
        try:
            if os.name == "nt":
                os.rename(temporary, path)
            else:
                os.link(temporary, path)
        except FileExistsError:
            if _read_profile(path) != value:
                raise ConflictError(
                    "Another initialization saved different configuration."
                ) from None
    finally:
        temporary.unlink(missing_ok=True)


def _read_adapter(path, provider):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (
        is_link(info)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_size > _MAX_PROFILE
    ):
        raise IntegrityError("Adapter configuration must be a bounded ordinary private file.")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise IntegrityError("Adapter configuration changed while opening it.")
        data = stream.read(_MAX_PROFILE + 1)
    try:
        value = json.loads(data)
        if (
            len(data) > _MAX_PROFILE
            or canonical_json(value).encode("utf-8") != data
            or not isinstance(value, dict)
            or set(value) != {"version", "provider", "executable"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["provider"] != provider
        ):
            raise ValueError
        if value["executable"] is not None:
            _commands({provider: [value["executable"]]})
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise IntegrityError(
            "Adapter configuration is invalid; preserve it for inspection."
        ) from None


def _save_adapter(root, provider, executable):
    private = private_directory(root)
    path = private / f"adapter-{provider}.json"
    existing = _read_adapter(path, provider)
    parent_info = private.stat()
    descriptor, name = tempfile.mkstemp(prefix=f".adapter-{provider}-", dir=private)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                canonical_json(
                    {"version": 1, "provider": provider, "executable": executable}
                ).encode("utf-8")
            )
            stream.flush()
            os.fsync(stream.fileno())
        if not os.path.samestat(parent_info, private_directory(root).stat()):
            raise IntegrityError("Private storage changed while configuring the adapter.")
        if _read_adapter(path, provider) != existing:
            raise ConflictError("Adapter configuration changed before publication.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _configured(policy_path=None, schema_path=None, commands=None):
    with ExitStack() as stack:
        resource = files("corinthos.resources")
        policy = policy_path or stack.enter_context(as_file(resource.joinpath("standard.yaml")))
        schema = schema_path or stack.enter_context(
            as_file(resource.joinpath("standard.schema.json"))
        )
        for path in (Path(policy), Path(schema)):
            info = path.lstat()
            if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_PROFILE:
                raise IntegrityError(
                    "Standard configuration requires bounded ordinary policy and schema files."
                )
        try:
            standard = load_standard(policy, schema)
        except (
            ValueError,
            TypeError,
            RecursionError,
            UnicodeError,
            yaml.YAMLError,
            ValidationError,
            InvalidSchema,
        ):
            raise IntegrityError(
                "The supplied Standard policy or schema is invalid; mandatory M1-M5 remain required."
            ) from None
    value = {
        "version": 1,
        "standard_id": "nukytera-standard",
        "standard": standard,
        "standard_sha256": sha256(canonical_json(standard).encode("utf-8")).hexdigest(),
        "commands": _commands(commands or {}, require_present=True),
    }
    return _profile(value)


def _choices(private, saved, providers):
    choices = []
    global_choices = None
    for name in providers:
        command = saved["commands"].get(name) if saved is not None else None
        operational = _read_adapter(private / f"adapter-{name}.json", name) if private else None
        if operational is not None:
            executable, configuration = operational["executable"], "operational"
        elif command:
            executable, configuration = command[0], "initial"
        else:
            if global_choices is None:
                global_choices = configured_executables()
            executable = global_choices.get(name)
            configuration = "global" if executable else "default"
        choices.append(
            {
                "provider": name,
                "executable": executable,
                "configuration": configuration,
                "selection": "explicit" if executable else "autodiscovery",
            }
        )
    return choices


def _manager(profile, commands, store=None):
    with tempfile.TemporaryDirectory(prefix="corinthos-standard-") as directory:
        policy = Path(directory) / "standard.json"
        policy.write_text(canonical_json(profile["standard"]), encoding="utf-8")
        with as_file(files("corinthos.resources").joinpath("standard.schema.json")) as schema:
            return AdapterManager(policy, schema, store=store, commands=commands)


def adapter_manager(
    root, *, policy_path=None, schema_path=None, commands=None, store=None, operation_commands=None
):
    """Reconstruct validated installed configuration without native work or writes."""
    root = _root(root)
    private = root / ".corinthos"
    saved = None
    if private.exists() or private.is_symlink():
        private = private_directory(root)
        saved = _read_profile(private / "bootstrap.json")
    else:
        private = None
    profile = saved if saved is not None else _configured(policy_path, schema_path)
    if saved is not None and (policy_path is not None or schema_path is not None):
        supplied = _configured(policy_path, schema_path)
        if any(saved[key] != supplied[key] for key in ("standard", "standard_sha256")):
            raise ConflictError(
                "Explicit Standard settings conflict with the saved bootstrap profile."
            )
    selected = {
        choice["provider"]: [choice["executable"]]
        for choice in _choices(private, saved, _PROVIDERS)
        if choice["executable"] is not None
    }
    if commands is not None:
        selected.update(_commands(commands, require_present=True))
    if operation_commands is not None:
        selected.update(operation_commands)
    return _manager(profile, selected, store)


def show_adapters(root, *, provider=None):
    """Read saved adapter choices without executing an external program."""
    if provider is not None and provider not in _PROVIDERS:
        raise IntegrityError("Select the codex or claude adapter.")
    root = _root(root)
    private = root / ".corinthos"
    saved = None
    if private.exists() or private.is_symlink():
        private = private_directory(root)
        saved = _read_profile(private / "bootstrap.json")
    else:
        private = None
    return _choices(private, saved, (provider,) if provider else _PROVIDERS)


def configure_adapter(root, provider, executable=None):
    """Persist one validated native choice, or reset it to automatic discovery."""
    if provider not in _PROVIDERS:
        raise IntegrityError("Select the codex or claude adapter.")
    root = _root(root)
    private = private_directory(root)
    if _read_profile(private / "bootstrap.json") is None:
        raise IntegrityError("Initialize this workspace before configuring adapters.")
    _read_adapter(private / f"adapter-{provider}.json", provider)
    if executable is not None:
        command = _commands({provider: [str(executable)]}, require_present=True)
        capabilities = adapter_manager(root, commands=command).discover(provider)
        if not (capabilities.available and capabilities.json_events and capabilities.stdin_prompt):
            raise IntegrityError(
                "The native executable did not verify the required JSON/stdin interface."
            )
        executable = command[provider][0]
    _save_adapter(root, provider, executable)
    return show_adapters(root, provider=provider)[0]


def _initialize_profile(root, *, policy_path=None, schema_path=None, commands=None):
    private = root / ".corinthos"
    saved = _read_profile(private_directory(root) / "bootstrap.json") if private.exists() else None
    explicit = policy_path is not None or schema_path is not None or commands is not None
    profile = (
        _configured(policy_path, schema_path, commands) if saved is None or explicit else saved
    )
    if saved is not None and saved != profile:
        raise ConflictError("Initialization conflicts with the saved bootstrap profile.")
    _save_profile(root, profile)
    return profile


def initialize_private(root):
    """Save the installed Standard without preparing any provider instructions."""
    _initialize_profile(_root(root))


def initialize(root, *, policy_path=None, schema_path=None, commands=None):
    """Prepare equivalent managed instructions without creating contract authority."""
    root = _root(root, create=True)
    profile = _initialize_profile(
        root, policy_path=policy_path, schema_path=schema_path, commands=commands
    )
    manager = _manager(profile, profile["commands"])
    prepared = [manager.prepare(provider, root) for provider in _PROVIDERS]
    return {
        "root": str(root),
        "standard_sha256": manager.standard_sha256,
        "instructions": [
            {**asdict(item), "instruction_file": str(item.instruction_file)} for item in prepared
        ],
    }


def _options(parser):
    parser.add_argument("--policy", type=Path, help="Explicit Standard policy override.")
    parser.add_argument("--policy-schema", type=Path, help="Additional Standard schema validation.")
    for provider in _PROVIDERS:
        parser.add_argument(
            "--" + provider + "-executable", type=Path, help="Absolute native executable."
        )


def add_cli(subparsers):
    init = subparsers.add_parser(
        "init", help="Initialize installed Standard and managed instructions."
    )
    init.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    _options(init)
    init.set_defaults(handler=run, bootstrap_action="init")
    adapters = subparsers.add_parser("adapters", help="Inspect and configure native adapters.")
    actions = adapters.add_subparsers(dest="bootstrap_action", required=True)
    capabilities = actions.add_parser(
        "capabilities", help="Run bounded native version/help discovery."
    )
    capabilities.add_argument("--root", type=Path, default=Path.cwd())
    capabilities.add_argument("--provider", choices=_PROVIDERS)
    _options(capabilities)
    capabilities.set_defaults(handler=run)
    show = actions.add_parser("show", help="Show saved adapter choices without running a provider.")
    show.add_argument("--root", type=Path, default=Path.cwd())
    show.add_argument("--provider", choices=_PROVIDERS)
    show.set_defaults(handler=run)
    actions.add_parser(
        "refresh", help="Refresh bounded per-user provider observations without opening a project."
    ).set_defaults(handler=run)
    for action in ("configure", "reset"):
        configure = actions.add_parser(
            action, help=f"{action.capitalize()} one saved adapter choice."
        )
        configure.add_argument("provider", choices=_PROVIDERS)
        configure.add_argument("--root", type=Path, default=Path.cwd())
        if action == "configure":
            configure.add_argument("--executable", type=Path, required=True)
        configure.set_defaults(handler=run)


def run(arguments):
    try:
        if arguments.bootstrap_action == "refresh":
            print(render_fields(refresh_providers()))
            return 0
        if arguments.bootstrap_action == "show":
            print(render_fields(show_adapters(arguments.root, provider=arguments.provider)))
            return 0
        if arguments.bootstrap_action in {"configure", "reset"}:
            print(
                render_fields(
                    configure_adapter(
                        arguments.root, arguments.provider, getattr(arguments, "executable", None)
                    )
                )
            )
            return 0
        commands = {
            provider: [str(getattr(arguments, provider + "_executable"))]
            for provider in _PROVIDERS
            if getattr(arguments, provider + "_executable") is not None
        } or None
        options = {
            "policy_path": arguments.policy,
            "schema_path": arguments.policy_schema,
            "commands": commands,
        }
        if arguments.bootstrap_action == "init":
            print(render_fields(initialize(arguments.root, **options)))
        else:
            manager = adapter_manager(arguments.root, **options)
            providers = (arguments.provider,) if arguments.provider else _PROVIDERS
            print(render_fields([asdict(manager.discover(provider)) for provider in providers]))
        return 0
    except (ContractError, OSError, ValueError) as error:
        print("Initialization or adapter operation failed. " + safe_text(error), file=sys.stderr)
        return 1
