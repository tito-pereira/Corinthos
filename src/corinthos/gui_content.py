# SPDX-License-Identifier: MPL-2.0
"""Fresh, bounded project context and informational Markdown views."""

import json
import os
import re
from pathlib import Path

from corinthos.errors import IntegrityError
from corinthos.filesystem import plain_path
from corinthos.workspace import open_project

_FILE_LIMIT = 65_536
_SUPPORT_LIMIT = 65_536
_CONTEXT_LIMIT = 196_608
_STATUSES = {
    "notimplemented": "Not Implemented",
    "inprogress": "In Progress",
    "suspended": "Suspended",
    "implemented": "Implemented",
}
_EXCLUDED = {"corinthos", "history", "archive", "archives", ".git", "build", "node_modules"}


def _root(root):
    return Path(open_project(root)["root"])


def _read(path):
    plain_path(path, file=True)
    try:
        with path.open("rb") as source:
            data = source.read(_FILE_LIMIT + 1)
        if len(data) > _FILE_LIMIT:
            raise IntegrityError(f"Project document exceeds 64 KiB: {path.name}.")
        return data.decode("utf-8-sig").replace("\r\n", "\n")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as error:
        raise IntegrityError(f"Cannot read project document: {path.name}.") from error


def _plain(text):
    text = re.sub(r"!?\[([^]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("\\|", "|")
    return " ".join(text.strip().strip("#>*_` ").split())


def _clip(text, size):
    return text.encode("utf-8")[:size].decode("utf-8", errors="ignore")


def _lines(text):
    fenced = False
    for line in text.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced:
            yield line


def _supporting(root, budget):
    directory = root / "context_agents"
    plain_path(directory)
    candidates = []
    omitted = False

    def failed(error):
        raise IntegrityError("Cannot read supporting project context.") from error

    try:
        for parent, directories, names in os.walk(directory, onerror=failed):
            relative = Path(parent).relative_to(directory)
            directories[:] = sorted(
                name for name in directories if name.casefold() not in _EXCLUDED
            )
            for name in directories:
                plain_path(Path(parent) / name)
            if len(relative.parts) >= 3:
                omitted |= bool(directories)
                directories[:] = []
            for name in sorted(names):
                if name.lower().endswith(".md"):
                    path = Path(parent) / name
                    plain_path(path, file=True)
                    candidates.append(path)
                    if len(candidates) >= 128:
                        omitted = True
                        break
            if len(candidates) >= 128:
                break
    except OSError as error:
        failed(error)
    candidates.sort(
        key=lambda path: (
            path.name.casefold() not in {"current-status.md", "completion-handoff.md"},
            len(path.relative_to(directory).parts),
            path.as_posix().casefold(),
        )
    )
    sections = [
        "## Supporting context_agents context\n"
        "Runtime files and recursive history/archive directories are excluded."
    ]
    omitted_names = []
    for path in candidates:
        name = path.relative_to(root).as_posix()
        try:
            size = path.stat().st_size
        except OSError as error:
            raise IntegrityError(f"Cannot read supporting context: {path.name}.") from error
        if size > _FILE_LIMIT:
            omitted_names.append(name)
            continue
        content = _read(path)
        if content is None:
            omitted_names.append(name)
            continue
        section = f"### {name}\n{content or '[empty]'}"
        if len(("\n\n".join([*sections, section])).encode("utf-8")) <= budget - 768:
            sections.append(section)
        else:
            omitted_names.append(name)
    if not candidates:
        sections.append("No supporting Markdown context is available.")
    if omitted or omitted_names:
        names = _clip(", ".join(omitted_names[:8]), 512)
        sections.append(
            "Supporting context omitted by the file/depth/byte budget"
            + (f": {names}" if names else "")
            + ". Read relevant local files before relying on omitted information."
        )
    result = "\n\n".join(sections)
    if len(result.encode("utf-8")) > budget:
        raise IntegrityError("Project context has no room for its supporting-context notice.")
    return result


def supporting_context(root):
    return _supporting(_root(root), _SUPPORT_LIMIT)


def project_context(root):
    root = _root(root)
    sections = [
        "Read this project's README, context and relevant context_agents before "
        "project guidance/work. Use its local skills; do not substitute another project."
    ]
    names = ["AGENTS.md", "README.md"] + [
        "context/" + name
        for name in ("PROJECT.md", "DECISIONS.md", "ARCHITECTURE.md", "CURRENT_STATE.md")
    ]
    for name in names:
        content = _read(root / name)
        sections.append(
            f"## {name}\n{content if content else '[missing]' if content is None else '[empty]'}"
        )
    library = root / ".agents/skills"
    plain_path(library)
    try:
        entries = sorted(library.iterdir(), key=lambda path: path.name)
    except OSError as error:
        raise IntegrityError("Cannot read this project's local skill library.") from error
    if len(entries) > 128:
        raise IntegrityError("Project-local skill library exceeds 128 entries.")
    skills = []
    for entry in entries:
        plain_path(entry, file=entry.is_file())
        if entry.is_dir() and (entry / "SKILL.md").exists():
            plain_path(entry / "SKILL.md", file=True)
            skills.append(f".agents/skills/{entry.name}/SKILL.md")
    sections.append("## Available local skills\n" + ("\n".join(skills) or "[none]"))
    base = "\n\n".join(sections)
    remaining = _CONTEXT_LIMIT - len(base.encode("utf-8")) - 2
    if remaining < 1024:
        raise IntegrityError("Project context exceeds 192 KiB; compact it before requesting work.")
    return base + "\n\n" + _supporting(root, min(_SUPPORT_LIMIT, remaining))


def _status(value):
    return _STATUSES.get(re.sub(r"[\s_-]+", "", _plain(value)).casefold())


def _group_id(value):
    match = re.fullmatch(r"(?:Group\s+)?([0-9]+)", _plain(value), re.I)
    if match is None or not 1 <= int(match[1]) <= 1_000_000:
        raise IntegrityError("Implementation group has an invalid identifier.")
    return int(match[1])


def pending_groups(root):
    text = _read(_root(root) / "task/implementation-groups.md")
    if not text or not text.strip():
        return []
    lines = list(_lines(text))
    text = "\n".join(lines)
    records = {}
    table = None
    for line in lines:
        if not line.strip().startswith("|"):
            table = None
            continue
        cells = [_plain(cell) for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        header = [cell.casefold() for cell in cells]
        if "group" in header and "status" in header:
            table = (header.index("group"), header.index("status"), header)
            continue
        if table is None or all(re.fullmatch(r":?-+:?", cell) for cell in cells):
            continue
        group_index, status_index, header = table
        if len(cells) != len(header):
            raise IntegrityError("Implementation group table has an incomplete row.")
        identity = _group_id(cells[group_index])
        status = _status(cells[status_index])
        if status is None:
            raise IntegrityError(f"Implementation group {identity} has an unsupported status.")
        title = next(
            (
                cells[header.index(name)]
                for name in ("objective", "title", "description")
                if name in header
            ),
            "",
        )
        row = {"group": identity, "status": status, "objective": title}
        if identity in records and records[identity] != row:
            raise IntegrityError("Implementation group records conflict.")
        records[identity] = row
    if not records:
        headings = list(re.finditer(r"(?m)^#{1,6}\s+Group\s+(\d+)\b[^\n]*", text, re.I))
        for index, match in enumerate(headings):
            section = text[
                match.end() : headings[index + 1].start()
                if index + 1 < len(headings)
                else len(text)
            ]
            statuses = []
            for line in section.splitlines():
                plain = _plain(line)
                explicit = re.match(r"(?:Current\s+)?Status\s*:\s*(.+)", plain, re.I)
                checked = re.match(r"\s*[-*]\s+\[[xX]\]\s+(.+)", line)
                if explicit:
                    value = _status(explicit[1])
                    if value is None:
                        raise IntegrityError("Implementation group status cannot be read.")
                    statuses.append(value)
                elif checked and (value := _status(checked[1])):
                    statuses.append(value)
            if len(set(statuses)) != 1:
                raise IntegrityError("Implementation group has no unambiguous status.")
            identity = _group_id(match[1])
            row = {
                "group": identity,
                "status": statuses[0],
                "objective": _plain(match[0]).partition(":")[2].strip(),
            }
            if identity in records and records[identity] != row:
                raise IntegrityError("Implementation group records conflict.")
            records[identity] = row
        if not headings and re.search(r"(?mi)^\s*(?:[-*]\s+)?Group\s+\d+\b", text):
            raise IntegrityError("Implementation group records cannot be read.")
    return [row for row in records.values() if row["status"] != "Implemented"]


def _ideas(text):
    lines = list(_lines(text))
    for line in lines:
        status = re.match(r"Status\s*:\s*(.+)", _plain(line), re.I)
        if status and _plain(status[1]).casefold().rstrip(".") in {
            "implemented",
            "completed",
            "resolved",
            "no pending ideas",
            "no pending decisions",
        }:
            return []
    items = []
    skipped = False
    for line in lines:
        if not line.strip():
            continue
        value = _plain(re.sub(r"^\s*[-*]\s+\[ \]\s*", "", line))
        if re.match(r"^#{2,6}\s+", line):
            skipped = bool(
                re.match(r"(?:Completed|Implemented|Resolved|Acceptance criteria)\b", value, re.I)
            )
        if skipped or line.startswith("# ") or re.match(r"\s*[-*]\s+\[[xX]\]", line):
            continue
        value = _plain(re.sub(r"^\s*(?:[-*]|\d+[.])\s+", "", value))
        if re.match(r"(?:Status|Decision date|Planned on|Requirements)\s*:", value, re.I):
            continue
        if value.casefold().rstrip(".") in {
            "nothing yet",
            "no pending ideas",
            "no pending decisions",
            "none",
            "future ideas",
            "ideas",
            "pending ideas",
            "requested implementations",
            "decisions",
        }:
            continue
        if value and value not in items:
            items.append(value)
    return items


def summary(root, kind):
    root = _root(root)
    if kind == "tasks":
        items = [
            f"Group {row['group']} — {row['status']}: {row['objective']}".rstrip(": ")
            for row in pending_groups(root)
        ]
    elif kind == "ideas":
        text = _read(root / "context/DECISIONS.md")
        items = _ideas(text) if text else []
    else:
        raise IntegrityError("Select ideas or tasks for this project summary.")
    selected = [_clip(item, 600) for item in items[:12]]
    result = {"kind": kind, "empty": not items, "items": selected}
    while len(json.dumps(result).encode("utf-8")) > 7900:
        selected.pop()
    if len(selected) < len(items):
        selected.append("Additional content omitted; open the local Markdown for full details.")
    return result
