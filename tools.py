"""Tool handlers for Meridian. Every handler returns a JSON string and
never raises; analyzer failures come back as `{"ok": false, "error": ...}`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .analyzer import graph, ledger, repo as repo_mod
from .analyzer.model import cap_list, dumps
from .analyzer.scan import scan_root
from .analyzer.spec import SpecError, accept_spec, draft_spec, load_spec


class RootError(ValueError):
    """The requested root is outside the tree Meridian may touch."""


def _agent_cwd() -> Path:
    """The active agent working directory - the only tree Meridian may read.

    Uses the plugin's own seam (`agent.runtime_cwd` when Hermes provides it,
    otherwise the configured terminal cwd, otherwise the process cwd), so the
    sandbox follows the session and not the model.
    """
    try:
        from . import _session_cwd  # plugin seam: agent.runtime_cwd, else cwd

        return Path(_session_cwd()).resolve()
    except Exception:
        pass
    scoped = (os.environ.get("TERMINAL_CWD") or "").strip()
    if scoped:
        candidate = Path(scoped).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
    return Path(os.getcwd()).resolve()


def _hermes_home() -> Path:
    raw = os.environ.get("HERMES_HOME") or ""
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(os.path.expanduser("~")).resolve() / ".hermes"


def _guard_cwd(base: Path) -> None:
    """Refuse a working directory that is too broad to confine anything.

    Confinement is only a sandbox when the base is a project directory: at
    the home directory or at the filesystem root it would allow exactly what
    it is meant to prevent.
    """
    home = Path.home().resolve()
    if base == Path("/") or home.is_relative_to(base):
        raise RootError(
            f"the working directory {base} is too broad to confine Meridian - "
            "run it from a project directory")
    for protected in (_hermes_home(), repo_mod.plugin_data_dir()):
        protected = Path(protected).resolve()
        if base == protected or base.is_relative_to(protected):
            raise RootError(
                f"the working directory {base} is the Hermes home - "
                "run it from a project directory")


def _confine_root(root: Path) -> Path:
    """Pin a root to the active working directory.

    `root` is a model-controlled tool argument, so "the out_file must stay
    inside root" is not a sandbox on its own: the root itself has to live
    inside the session's working directory before anything is read or written.
    """
    base = _agent_cwd()
    _guard_cwd(base)
    if not root.is_relative_to(base):
        raise RootError(
            f"root must stay inside the working directory {base} - refusing {root}")
    for protected in (_hermes_home(), repo_mod.plugin_data_dir()):
        if root.is_relative_to(Path(protected).resolve()):
            raise RootError(f"refusing to work inside the Hermes home {protected}")
    return root


def _tool_root(args: dict) -> Path:
    raw = (args or {}).get("root") or os.environ.get("MERIDIAN_ROOT") or ""
    if raw:
        path = Path(raw).expanduser()
    else:
        path = _agent_cwd()
    return _confine_root(path.resolve())


_OUTSTANDING_LABELS = (
    ("warnings", "warning"),
    ("unguarded_writes", "unguarded write"),
    ("dead_states", "dead state"),
    ("unenforced_machines", "unenforced machine"),
)


def _outstanding_text(outstanding: dict) -> str:
    """Human-readable list of what is still outstanding, e.g. '1 unguarded write'."""
    parts = []
    for key, label in _OUTSTANDING_LABELS:
        count = int(outstanding.get(key, 0) or 0)
        if count:
            parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    return ", ".join(parts)


def _err(message: str, **extra: Any) -> str:
    return json.dumps({"ok": False, "error": message, **extra})


def _ok(payload: dict[str, Any]) -> str:
    payload = {"ok": True, **payload}
    return dumps(payload)


def _scan_payload(result, *, full: bool) -> dict[str, Any]:
    machines = []
    for machine in result.machines:
        verdict = graph.analyze_machine(machine)
        base = {
            "id": machine.id,
            "owner": machine.owner,
            "language": machine.language,
            "confidence": machine.confidence,
            "states": machine.states,
            "initial": machine.initial,
            "transitions": len(machine.transitions),
            "reachable": len(verdict.reachable),
            "unreachable": verdict.unreachable,
            "dead": verdict.dead,
            "terminal": verdict.terminal,
            "notes": machine.notes,
            "sources": machine.sources,
        }
        if full:
            transitions, extra = cap_list([t.to_dict() for t in machine.transitions], 40)
            base["transitions_detail"] = transitions
            if extra:
                base["transitions_truncated"] = extra
        machines.append(base)
    return {
        "root": result.root,
        "files_scanned": result.files_scanned,
        "files_skipped": result.files_skipped,
        "elapsed_ms": result.elapsed_ms,
        "warnings": result.warnings,
        "machine_count": len(machines),
        "machines": machines,
    }


def _ledger_event(event: str, data: dict) -> None:
    try:
        ledger.append(event, data)
    except Exception:
        # the ledger must never take the tool down; the caller still gets
        # the analysis, and `meridian_ledger verify` surfaces the break.
        pass


# ---------------------------------------------------------------- scan
def meridian_scan(args: dict, **kwargs: Any) -> str:
    try:
        root = _tool_root(args)
        result = scan_root(root)
        payload = _scan_payload(result, full=len(result.machines) <= 12)
        _ledger_event("scan", {
            "root": result.root, "files": result.files_scanned,
            "machines": [m.id for m in result.machines],
        })
        return _ok({"scan": payload, "hint": "Run meridian_verify for edge-level verdicts, meridian_spec draft to start enforcement, meridian_diagram for a picture."})
    except RootError as exc:
        return _err(str(exc))
    except Exception as exc:  # total containment
        return _err(f"scan failed: {exc}")


# -------------------------------------------------------------- verify
def _verify_all_with_result(root: Path, *, machine_id: str | None, strict: bool,
                            allow_uncovered: bool = False) -> tuple[dict, Any]:
    result = scan_root(root)
    spec_path = repo_mod.find_spec(root)
    spec = load_spec(spec_path) if spec_path else None
    machines = result.machines
    if machine_id:
        machines = [m for m in machines if m.id == machine_id]
        if not machines:
            return {"error": f"machine not found: {machine_id}",
                    "known": [m.id for m in result.machines][:30]}, result

    def _enum_only(m) -> bool:
        return not m.transitions and not m.initial and m.id.startswith("enum:")

    if spec:
        spec_ids = spec.ids()
        # Being named in the spec is not being enforced: a machine counts as
        # covered only when the spec states at least one rule for it.
        covered = sorted(m.id for m in result.machines
                         if m.id in spec_ids and not _enum_only(m)
                         and spec.machine(m.id).enforced)
        unenforced = sorted(m.id for m in result.machines
                            if m.id in spec_ids and not _enum_only(m)
                            and not spec.machine(m.id).enforced)
        uncovered = sorted(m.id for m in result.machines
                           if m.id not in spec_ids and not _enum_only(m))
        # "covers nothing" stays about naming: a spec that names a machine but
        # states no rule for it is a coverage failure (exit 1), not a spec
        # that was never written (exit 2).
        spec_covers_nothing = not any(not _enum_only(m) and m.id in spec_ids
                                      for m in result.machines)
    else:
        covered, uncovered, unenforced, spec_covers_nothing = [], [], [], False

    verdicts = []
    all_violations = []
    for machine in machines:
        spec_machine = spec.machine(machine.id) if spec else None
        verdict = graph.analyze_machine(machine, spec_machine, strict=strict)
        transitions, extra = cap_list([t.to_dict() for t in machine.transitions], 40)
        verdict_dict = verdict.to_dict()
        verdict_dict["confidence"] = machine.confidence
        verdict_dict["states"] = machine.states
        verdict_dict["initial"] = machine.initial
        verdict_dict["transitions"] = transitions
        if extra:
            verdict_dict["transitions_truncated"] = extra
        verdicts.append(verdict_dict)
        all_violations.extend(verdict_dict["violations"])
    unfound = sorted(spec.ids() - {m.id for m in result.machines}) if spec else []
    violations_total = len(all_violations)
    shown_violations = all_violations[:50]
    # An `--allow-uncovered` waive covers uncovered machines, never a spec
    # that enforces nothing.
    not_enforced = sorted(set(uncovered) | set(unenforced))
    uncovered_enforced = [] if allow_uncovered else not_enforced
    relevant_total = sum(1 for m in result.machines if not _enum_only(m))
    # Two distinct verdicts, so "no violations" is never read as "nothing is
    # left to look at": spec-clean means no violation, healthy means no
    # violation and nothing outstanding.
    outstanding = {
        "warnings": sum(len(v.get("warnings", [])) for v in verdicts),
        "unguarded_writes": sum(len(v.get("unguarded_writes", [])) for v in verdicts),
        "dead_states": sum(len(v.get("dead", [])) for v in verdicts),
        "unenforced_machines": len(not_enforced),
    }
    if all_violations:
        status = "violations"
    elif any(outstanding.values()):
        status = "spec-clean"
    else:
        status = "healthy"
    payload = {
        "root": result.root,
        "spec": str(spec_path) if spec_path else None,
        "spec_version": spec.version if spec else None,
        "strict": strict,
        "allow_uncovered": allow_uncovered,
        "status": status,
        "outstanding": outstanding,
        "coverage": {
            "spec_present": spec is not None,
            "covered_machines": covered,
            "uncovered_machines": uncovered,
            "unenforced_machines": unenforced,
            "enforced": len(covered),
            "total": relevant_total,
            "spec_covers_nothing": spec_covers_nothing,
            "spec_machines_not_found": unfound,
        },
        "ok": not all_violations and not uncovered_enforced and not spec_covers_nothing,
        "violation_count": violations_total,
        "violations_truncated": max(0, violations_total - len(shown_violations)),
        "violations": shown_violations,
        "verdicts": verdicts,
        "files_scanned": result.files_scanned,
        "elapsed_ms": result.elapsed_ms,
    }
    return payload, result


def _verify_all(root: Path, *, machine_id: str | None, strict: bool,
                allow_uncovered: bool = False) -> dict:
    """Compatibility wrapper for callers that only need the JSON payload."""
    payload, _ = _verify_all_with_result(
        root, machine_id=machine_id, strict=strict,
        allow_uncovered=allow_uncovered,
    )
    return payload


def meridian_verify(args: dict, **kwargs: Any) -> str:
    try:
        root = _tool_root(args)
        machine_id = (args or {}).get("machine_id") or None
        strict = bool((args or {}).get("strict", False))
        payload = _verify_all(root, machine_id=machine_id, strict=strict)
        if "error" in payload:
            return _err(payload["error"], known=payload.get("known", []))
        _ledger_event("verify", {
            "root": payload["root"],
            "machine_id": machine_id,
            "spec": bool(payload["spec"]),
            "violation_count": payload["violation_count"],
            "ok": payload["ok"],
        })
        return _ok(payload)
    except RootError as exc:
        return _err(str(exc))
    except SpecError as exc:
        return _err(f"spec error: {exc}")
    except Exception as exc:
        return _err(f"verify failed: {exc}")


# ------------------------------------------------------------- diagram
def _xstate_id(raw: str) -> str:
    cleaned = "".join(c if c.isalnum() else "_" for c in raw)
    return cleaned.strip("_") or "machine"


_MERMAID_KEYWORDS = {"note", "loop", "end", "state", "direction", "as", "class", "click"}


def _mermaid_name(state: str, used: dict[str, str]) -> str:
    if state not in used:
        safe = state if state.replace("_", "").replace("-", "").replace(".", "").isalnum() and state.lower() not in _MERMAID_KEYWORDS else "s_" + re.sub(r"\W", "_", state)
        if safe in used.values():
            safe = f"{safe}_{len(used)}"
        used[state] = safe
    return used[state]


def _mermaid(machine) -> str:
    used: dict[str, str] = {}

    def sid(state: str) -> str:
        return _mermaid_name(state, used)

    lines = ["stateDiagram-v2"]
    for initial in machine.initial:
        lines.append(f"    [*] --> {sid(initial)}")
    for t in machine.transitions:
        if t.src:
            label = (t.via or "write").replace('"', "'")
            lines.append(f"    {sid(t.src)} --> {sid(t.dst)} : {label}")
    for state in machine.states:
        if state not in {t.dst for t in machine.transitions} and state not in machine.initial:
            lines.append(f"    note right of {sid(state)}: never assigned (dead)")
    return "\n".join(lines)


def _xstate(machine) -> dict:
    states: dict[str, Any] = {}
    incoming: dict[str, list[tuple[str, str]]] = {}
    for t in machine.transitions:
        if t.src:
            incoming.setdefault(t.src, []).append((t.dst, t.via))
    for state in machine.states:
        node: dict[str, Any] = {}
        edges = incoming.get(state, [])
        if edges:
            on: dict[str, Any] = {}
            used: set[str] = set()
            for dst, via in edges:
                event = f"TO_{dst.upper()}"
                count = 2
                candidate = event
                while candidate in used:  # disambiguate same-target edges
                    candidate = f"{event}_{count}"
                    count += 1
                used.add(candidate)
                on[candidate] = {"target": dst, "description": f"via {via}()"}
            node["on"] = on
        states[state] = node
    initial = machine.initial[0] if machine.initial else (machine.states[0] if machine.states else None)
    machine_json: dict[str, Any] = {
        "id": _xstate_id(machine.id),
        "states": states,
        "x_meridian": {
            "note": "Event names are generated placeholders; wire your own events before using this in production.",
            "source_machine": machine.id,
            "evidence": [t.anchor() for t in machine.transitions],
        },
    }
    if initial:
        machine_json["initial"] = initial
    return machine_json


def meridian_diagram(args: dict, **kwargs: Any) -> str:
    try:
        root = _tool_root(args)
        machine_id = (args or {}).get("machine_id")
        fmt = (args or {}).get("format") or "mermaid"
        if not machine_id:
            return _err("machine_id is required", hint="call meridian_scan first to list ids")
        if fmt not in ("mermaid", "xstate"):
            return _err("format must be 'mermaid' or 'xstate'")
        result = scan_root(root)
        machine = next((m for m in result.machines if m.id == machine_id), None)
        if machine is None:
            return _err(f"machine not found: {machine_id}", known=[m.id for m in result.machines][:30])
        _ledger_event("diagram", {"root": result.root, "machine_id": machine_id, "format": fmt})
        if fmt == "mermaid":
            return _ok({"format": "mermaid", "machine_id": machine_id, "diagram": _mermaid(machine)})
        return _ok({"format": "xstate", "machine_id": machine_id, "machine": _xstate(machine)})
    except RootError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"diagram failed: {exc}")


# ---------------------------------------------------------------- spec
def meridian_spec(args: dict, **kwargs: Any) -> str:
    try:
        args = args or {}
        root = _tool_root(args)
        action = args.get("action")
        if action not in ("draft", "accept", "show", "check"):
            return _err("action must be one of: draft, accept, show, check")
        if action in ("draft", "accept"):
            result = scan_root(root)
            content = draft_spec(result.machines)
            if action == "draft":
                _ledger_event("spec_draft", {"root": result.root, "machines": len(result.machines)})
                return _ok({"spec_yaml": content, "machines": len(result.machines),
                            "note": "Review every edge, then 'accept' to enforce."})
            # Accepting is not a rewrite: an accepted spec is merged into, so
            # every deny rule and authored edge the reviewer approved survives.
            try:
                path, merged = accept_spec(root, result.machines)
            except SpecError as exc:
                return _err(str(exc))
            _ledger_event("spec_accept", {"root": str(root), "path": str(path),
                                          "machines": len(result.machines)})
            return _ok({"written": str(path), "machines": len(result.machines),
                        "merged": merged,
                        "note": ("Spec accepted (merged into the existing one - deny rules kept). "
                                 "`hermes meridian check` (or meridian_spec check) now enforces it.")})
        spec_path = repo_mod.find_spec(root)
        if action == "show":
            if not spec_path:
                return _err(f"no .meridian.yaml found at or above {root}")
            try:
                content = repo_mod.read_text(spec_path)
            except repo_mod.UnsafePathError as exc:
                raise SpecError(str(exc)) from exc
            return _ok({"path": str(spec_path), "content": content})
        # check
        if not spec_path:
            return _err(f"no .meridian.yaml at or above {root}", hint="run action=draft first")
        strict = bool(args.get("strict", False))
        allow_uncovered = bool(args.get("allow_uncovered", False))
        payload = _verify_all(root, machine_id=None, strict=strict, allow_uncovered=allow_uncovered)
        _ledger_event("check", {"root": payload["root"], "spec": payload["spec"],
                                "violation_count": payload["violation_count"], "ok": payload["ok"]})
        return _ok({"check": payload})
    except RootError as exc:
        return _err(str(exc))
    except SpecError as exc:
        return _err(f"spec error: {exc}")
    except Exception as exc:
        return _err(f"spec {args.get('action') if args else '?'} failed: {exc}")


# -------------------------------------------------------------- ledger
def meridian_ledger(args: dict, **kwargs: Any) -> str:
    try:
        args = args or {}
        action = args.get("action")
        if action == "show":
            limit = max(1, min(int(args.get("limit", 10)), 100))
            entries = ledger.read_entries()
            return _ok({"entries": entries[-limit:], "total": len(entries)})
        if action == "verify":
            return _ok(ledger.verify())
        if action == "stats":
            return _ok(ledger.stats())
        if action == "repair":
            return _ok(ledger.repair())
        return _err("action must be one of: show, verify, stats, repair")
    except Exception as exc:
        return _err(f"ledger failed: {exc}")


# -------------------------------------------------------------- report
def _report_markdown(root: Path, payload: dict, machines: list[Any] | None = None) -> str:
    lines = [
        "# Meridian Report",
        "",
        f"- Repository: `{root}`",
        f"- Files scanned: {payload.get('files_scanned', 0)}",
        f"- Machines found: {len(payload.get('verdicts', []))}",
        f"- Spec: {payload.get('spec') or 'none (discovery only)'}",
        f"- Status: {payload.get('status', 'spec-clean')} - {payload.get('violation_count', 0)} violation(s)",
        f"- Outstanding: {_outstanding_text(payload.get('outstanding', {})) or 'none'}",
        "",
        "## Machines",
        "",
        "| Machine | Confidence | States | Unreachable | Dead | Violations |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for verdict in payload.get("verdicts", []):
        lines.append(
            f"| `{verdict['machine_id']}` | {verdict.get('confidence', '?')} "
            f"| {len(verdict.get('states', []))} "
            f"| {', '.join(verdict.get('unreachable', [])) or '-'} "
            f"| {', '.join(verdict.get('dead', [])) or '-'} "
            f"| {len(verdict.get('violations', []))} |"
        )
    # One diagram up front: the first machine that has real evidence. The
    # caller normally supplies the scan result so report generation does not
    # walk and parse the repository a second time.
    if machines is None:
        machines = scan_root(root).machines
    diagram_machine = next((m for m in machines if m.transitions), None)

    for verdict in payload.get("verdicts", []):
        lines += ["", f"## `{verdict['machine_id']}`", ""]
        lines.append(f"- States: {', '.join(verdict.get('states', [])) or '-'}")
        lines.append(f"- Initial: {', '.join(verdict.get('initial', [])) or 'not discovered'}")
        lines.append(f"- Reachable: {', '.join(verdict.get('reachable', [])) or '-'}")
        if verdict.get("unreachable"):
            lines.append(f"- **Unreachable states:** {', '.join(verdict['unreachable'])}")
        if verdict.get("dead"):
            lines.append(f"- **Dead states (never assigned):** {', '.join(verdict['dead'])}")
        if verdict.get("terminal"):
            lines.append(f"- Terminal states: {', '.join(verdict['terminal'])}")
        lines += ["", "Transitions:", ""]
        for t in verdict.get("transitions", []):
            src = t["src"] if t["src"] else "*"
            guard = "guarded" if t["guarded"] else "UNGUARDED"
            lines.append(f"- `{src}` → `{t['dst']}` via `{t['via']}()` - {t['file']}:{t['line']} ({guard})")
        if verdict.get("transitions_truncated"):
            lines.append(
                f"- _{verdict['transitions_truncated']} additional transition(s) omitted from this report._")
        if verdict.get("violations"):
            lines += ["", "**Violations:**", ""]
            for v in verdict["violations"]:
                lines.append(f"- `{v['anchor']}` - {v['detail']}")
        for note in verdict.get("notes", []):
            lines.append(f"- note: {note}")
    head = ledger.verify()
    lines += [
        "",
        "## Diagrams",
        "",
    ]
    if diagram_machine:
        lines += ["```mermaid", _mermaid(diagram_machine), "```"]
    else:
        lines.append("_No machine had anchored transitions to diagram._")
    lines += [
        "",
        "## Evidence ledger",
        "",
        f"- Entries: {head['entries']}",
        f"- Chain valid: {'yes' if head['ok'] else 'NO - ' + '; '.join(head['problems'][:3])}",
        f"- Head: `{head['head']}`",
        "",
        "_Generated by Meridian. Every path:line anchor above is anchored in source; nothing in this report is model-asserted._",
    ]
    return "\n".join(lines) + "\n"


def meridian_report(args: dict, **kwargs: Any) -> str:
    try:
        args = args or {}
        root = _tool_root(args)
        strict = bool(args.get("strict", False))
        payload, scan_result = _verify_all_with_result(
            root, machine_id=None, strict=strict)
        if "error" in payload:
            return _err(payload["error"])
        report = _report_markdown(root, payload, scan_result.machines)
        out = args.get("out_file")
        written = None
        if out:
            try:
                # Resolve parent components, but inspect the final component
                # without following it. Resolving the whole path would turn a
                # repository symlink into its target and overwrite that file.
                target = repo_mod.confined_output_path(root, out)
                repo_mod.atomic_write_text(target, report)
            except repo_mod.UnsafePathError as exc:
                return _err(f"out_file is unsafe: {exc}")
            except OSError as exc:
                return _err(f"out_file cannot be written: {exc}")
            written = str(target)
        _ledger_event("report", {"root": str(root), "machines": len(payload.get("verdicts", [])),
                                 "out": written})
        return _ok({"report_markdown": report, "written": written,
                    "violation_count": payload.get("violation_count", 0)})
    except RootError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"report failed: {exc}")
