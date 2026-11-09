"""Meridian — implicit state machines in your codebase, verified for you.

register(ctx): tools + hooks + slash command + `hermes meridian` CLI.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from . import schemas, tools
from .analyzer import ledger, repo as repo_mod
from .analyzer.scan import scan_root
from .analyzer import graph

logger = logging.getLogger(__name__)

_WRITE_HINTS = ("write", "edit", "patch")
_SUPPORTED_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_MAX_RESCAN_FILES = 400
_NOTE_TTL_SECONDS = 1200
_PENDING_NOTES: dict[str, list[tuple[float, str]]] = {}

# replaced during register() from ctx.get_config(...)
_AUTO_VERIFY = True
_STRICT = False


# ------------------------------------------------------------ hooks
def _changed_files(args: dict) -> list[Path]:
    found: list[Path] = []
    if not isinstance(args, dict):
        return found
    candidates: list[str] = []
    for value in args.values():
        if isinstance(value, str):
            candidates.append(value)
        elif isinstance(value, dict):
            for inner in value.values():
                if isinstance(inner, str):
                    candidates.append(inner)
    try:
        base = _session_cwd()
    except Exception:
        base = Path(os.getcwd())
    for raw in candidates:
        raw = raw.strip()
        if not raw or len(raw) > 4096:
            continue
        if not raw.lower().endswith(_SUPPORTED_SUFFIXES):
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = base / path
        if path.is_file():
            try:
                found.append(path.resolve())
            except OSError:
                continue
    # de-dupe, keep order
    seen: set[str] = set()
    unique: list[Path] = []
    for path in found:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique[:10]


def _session_cwd() -> Path:
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # public seam

        resolved = resolve_agent_cwd()
        if resolved:
            return Path(str(resolved)).resolve()
    except Exception:
        pass
    return Path(os.getcwd())


def _store_note(key: str, text: str) -> None:
    bucket = _PENDING_NOTES.setdefault(key, [])
    bucket.append((time.time(), text))
    del bucket[:-3]
    # global sweep of stale notes
    now = time.time()
    for k in list(_PENDING_NOTES):
        _PENDING_NOTES[k] = [(ts, t) for ts, t in _PENDING_NOTES[k] if now - ts < _NOTE_TTL_SECONDS]
        if not _PENDING_NOTES[k]:
            del _PENDING_NOTES[k]


def _take_notes(key: str) -> list[str]:
    notes = [t for _, t in _PENDING_NOTES.pop(key, [])]
    local = [t for _, t in _PENDING_NOTES.pop("local", [])]
    return notes + local


def _on_post_tool_call(tool_name, args, result, task_id=None, **kwargs):
    """Re-verify the touched repo right after the agent edits it. Never blocks,
    never raises: worst case it logs and the next turn carries no note."""
    try:
        if not _AUTO_VERIFY:
            return None
        lowered = (tool_name or "").lower()
        if not any(hint in lowered for hint in _WRITE_HINTS):
            return None
        changed = _changed_files(args or {})
        if not changed:
            return None
        notes_for_key: list[str] = []
        for changed_file in changed:
            spec_path = repo_mod.find_spec(changed_file.parent)
            if spec_path is None:
                continue
            root = spec_path.parent.resolve()
            try:
                result_scan = scan_root(root)
            except Exception:
                continue
            if result_scan.files_scanned > _MAX_RESCAN_FILES:
                note = (f"meridian: {root.name} has {result_scan.files_scanned} source files; "
                        f"auto-verify skipped — run meridian_verify manually")
                notes_for_key.append(note)
                continue
            machine_files = {(root / s).resolve() for machine in result_scan.machines for s in machine.sources}
            if changed_file not in machine_files:
                continue  # the edit didn't touch a tracked machine's files
            try:
                spec = load_spec_safe(spec_path)
            except Exception:
                continue
            for machine in result_scan.machines:
                spec_machine = spec.machine(machine.id) if spec else None
                if spec_machine is None:
                    continue
                verdict = graph.analyze_machine(machine, spec_machine, strict=_STRICT)
                if verdict.violations:
                    for v in verdict.violations[:5]:
                        notes_for_key.append(
                            f"meridian: {v.file}:{v.line} — {v.src} -> {v.dst} via {v.via}() "
                            f"violates {machine.id} ({v.detail})")
            try:
                ledger.append("auto_reverify", {
                    "root": str(root),
                    "file": str(changed_file.relative_to(root)),
                    "violations": sum(1 for n in notes_for_key if "violates" in n),
                })
            except Exception:
                pass
        if notes_for_key:
            text = ("Meridian — this edit touched state-machine code:\n"
                    + "\n".join(f"  - {n}" for n in notes_for_key[:8])
                    + "\nRun meridian_verify for full details.")
            _store_note(str(task_id or "local"), text)
        return None
    except Exception:
        logger.debug("meridian auto-reverify failed", exc_info=True)
        return None


def load_spec_safe(spec_path: Path):
    from .analyzer.spec import load_spec

    return load_spec(spec_path)


def _on_pre_llm_call(session_id=None, user_message="", **kwargs):
    try:
        key = str(session_id or "local")
        notes = _take_notes(key)
        if not notes:
            return None
        text = "\n".join(notes)[:1500]
        return {"context": text}
    except Exception:
        logger.debug("meridian pre_llm_call failed", exc_info=True)
        return None


# ------------------------------------------------------------ slash
def _handle_meridian(raw_args: str) -> str:
    args = (raw_args or "").strip().split()
    sub = args[0].lower() if args else "status"
    root = _session_cwd()
    if sub in ("help", "-h", "--help"):
        return ("/meridian [status|scan|verify|check|ledger]\n"
                "  status — machines found here + spec state\n"
                "  scan   — discover machines in this repo\n"
                "  verify — edge verdicts (unreachable/dead/unguarded)\n"
                "  check  — enforce .meridian.yaml (CI primitive)\n"
                "  ledger — ledger integrity summary")
    try:
        result = scan_root(root)
    except Exception as exc:
        return f"meridian: scan failed: {exc}"
    if sub in ("status", "scan"):
        if not result.machines:
            return f"meridian: no implicit state machines found in {root} ({result.files_scanned} files)"
        lines = [f"meridian: {len(result.machines)} machine(s) in {root} ({result.files_scanned} files):"]
        for machine in result.machines[:12]:
            lines.append(f"  - {machine.id} [{machine.confidence}] "
                         f"{len(machine.states)} states, {len(machine.transitions)} transitions")
        spec = repo_mod.find_spec(root)
        lines.append(f"  spec: {spec if spec else 'none — `hermes meridian spec draft` to start'}")
        return "\n".join(lines)
    if sub in ("verify", "check"):
        payload = tools._verify_all(root, machine_id=None, strict=_STRICT)
        if payload.get("error"):
            return f"meridian: {payload['error']}"
        if payload["violation_count"]:
            lines = [f"meridian: {payload['violation_count']} violation(s):"]
            for v in payload["violations"][:10]:
                lines.append(f"  - {v['anchor']} {v['src']} -> {v['dst']} ({v['via']})")
            return "\n".join(lines)
        return f"meridian: OK — {len(payload['verdicts'])} machine(s) verified, 0 violations"
    if sub == "ledger":
        try:
            status = ledger.verify()
            return (f"meridian ledger: {'valid' if status['ok'] else 'BROKEN'} — "
                    f"{status['entries']} entries"
                    + ("" if status["ok"] else f" — {status['problems'][:2]}"))
        except Exception as exc:
            return f"meridian ledger: {exc}"
    return f"meridian: unknown subcommand '{sub}' — try /meridian help"


# ----------------------------------------------------------- cli
def _cli_main(args):
    cmd = getattr(args, "meridian_command", None) or "help"
    root = Path(getattr(args, "root", ".") or ".").expanduser().resolve()
    if cmd == "scan":
        result = scan_root(root)
        if getattr(args, "json", False):
            print(json.dumps(tools._scan_payload(result, full=True), indent=2))
            return
        print(f"meridian scan — {result.files_scanned} files, {len(result.machines)} machine(s), {result.elapsed_ms}ms")
        for machine in result.machines:
            print(f"  {machine.id:32} conf={machine.confidence:6} states={len(machine.states)} transitions={len(machine.transitions)}")
        return
    if cmd == "verify":
        payload = tools._verify_all(root, machine_id=getattr(args, "machine", None),
                                    strict=getattr(args, "strict", False) or _STRICT)
        print(json.dumps(payload, indent=2))
        return
    if cmd == "check":
        payload = tools._verify_all(root, machine_id=None,
                                    strict=getattr(args, "strict", False) or _STRICT)
        if payload.get("error"):
            print(f"meridian check: {payload['error']}")
            return 2
        if not payload.get("spec"):
            # A CI gate that passes because the spec is missing is a false green.
            print(f"meridian check: no .meridian.yaml at or above {root} — nothing to enforce (exit 2)")
            return 2
        if payload["violation_count"]:
            print(f"meridian check FAILED — {payload['violation_count']} violation(s):")
            for v in payload["violations"]:
                print(f"  {v['anchor']}  {v['src']} -> {v['dst']}  ({v['via']})  [{v['rule']}]")
            try:
                ledger.append("check", {"root": str(root), "ok": False,
                                        "violation_count": payload["violation_count"]})
            except Exception:
                pass
            return 1
        print(f"meridian check OK — {len(payload['verdicts'])} machine(s), 0 violations")
        try:
            ledger.append("check", {"root": str(root), "ok": True, "violation_count": 0})
        except Exception:
            pass
        return 0
    if cmd == "report":
        payload = tools._verify_all(root, machine_id=None, strict=False)
        report = tools._report_markdown(root, payload)
        out = getattr(args, "out", None)
        if out:
            target = Path(out).expanduser()
            if not target.is_absolute():
                target = root / out
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report, encoding="utf-8")
            print(f"wrote {target}")
        else:
            print(report)
        return
    if cmd == "diagram":
        machine_id = args.machine_id
        result = scan_root(root)
        machine = next((m for m in result.machines if m.id == machine_id), None)
        if machine is None:
            print(f"machine not found: {machine_id}")
            return 2
        fmt = getattr(args, "format", "mermaid")
        if fmt == "mermaid":
            print(tools._mermaid(machine))
        else:
            print(json.dumps(tools._xstate(machine), indent=2))
        return
    if cmd == "spec":
        action = args.spec_action
        if action == "draft":
            result = scan_root(root)
            from .analyzer.spec import draft_spec

            print(draft_spec(result.machines))
            return
        if action == "accept":
            result = scan_root(root)
            from .analyzer.spec import draft_spec, write_spec

            try:
                path = write_spec(root, draft_spec(result.machines), force=getattr(args, "force", False))
            except Exception as exc:
                print(f"meridian spec: {exc}")
                return 2
            print(f"wrote {path}")
            return
        if action == "show":
            spec = repo_mod.find_spec(root)
            if not spec:
                print(f"no .meridian.yaml at or above {root}")
                return 2
            print(spec.read_text(encoding="utf-8"))
            return
    if cmd == "ledger":
        action = args.ledger_action
        if action == "show":
            for entry in ledger.read_entries()[-getattr(args, "limit", 10):]:
                if entry.get("_corrupt"):
                    print("  [corrupt entry]")
                else:
                    print(f"  #{entry.get('seq')} {entry.get('ts')} {entry.get('event')}")
            return
        if action == "verify":
            status = ledger.verify()
            if status["ok"]:
                print(f"meridian ledger OK — {status['entries']} entries, head {status['head']}")
            else:
                print(f"meridian ledger BROKEN — {status['entries']} entries:")
                for problem in status["problems"]:
                    print(f"  - {problem}")
                return 1
            return
        if action == "stats":
            print(json.dumps(ledger.stats(), indent=2))
            return
    print("usage: hermes meridian {scan|verify|check|report|diagram|spec|ledger} --help")


def _setup_cli(subparser) -> None:
    subs = subparser.add_subparsers(dest="meridian_command")

    def add_root(p):
        p.add_argument("--root", default=".", help="repository root (default: cwd)")

    p = subs.add_parser("scan", help="discover implicit state machines")
    add_root(p)
    p.add_argument("--json", action="store_true", help="full JSON output")

    p = subs.add_parser("verify", help="edge verdicts as JSON")
    add_root(p)
    p.add_argument("--machine", default=None, help="verify one machine id")
    p.add_argument("--strict", action="store_true", help="unguarded writes count as violations")

    p = subs.add_parser("check", help="enforce .meridian.yaml; exit 1 on violations (CI)")
    add_root(p)
    p.add_argument("--strict", action="store_true")

    p = subs.add_parser("report", help="markdown report to stdout or --out")
    add_root(p)
    p.add_argument("--out", default=None)

    p = subs.add_parser("diagram", help="mermaid or xstate for one machine")
    add_root(p)
    p.add_argument("machine_id")
    p.add_argument("--format", choices=["mermaid", "xstate"], default="mermaid")

    p = subs.add_parser("spec", help="draft / accept / show the .meridian.yaml spec")
    spec_subs = p.add_subparsers(dest="spec_action")
    for name, help_text in (("draft", "print the spec Meridian would accept"),
                            ("accept", "write .meridian.yaml (needs --force to overwrite)"),
                            ("show", "print current spec")):
        sp = spec_subs.add_parser(name, help=help_text)
        add_root(sp)
        if name == "accept":
            sp.add_argument("--force", action="store_true")

    p = subs.add_parser("ledger", help="evidence ledger: show / verify / stats")
    ledger_subs = p.add_subparsers(dest="ledger_action")
    lp = ledger_subs.add_parser("show", help="recent entries")
    lp.add_argument("--limit", type=int, default=10)
    ledger_subs.add_parser("verify", help="recompute the hash chain")
    ledger_subs.add_parser("stats", help="event counts")

    subparser.set_defaults(func=_cli_main)


# --------------------------------------------------------- register
def register(ctx) -> None:
    global _AUTO_VERIFY, _STRICT
    try:
        _AUTO_VERIFY = bool(ctx.get_config("auto_verify", default=True))
    except Exception:
        _AUTO_VERIFY = True
    try:
        _STRICT = bool(ctx.get_config("strict", default=False))
    except Exception:
        _STRICT = False

    tool_specs = [
        ("meridian_scan", schemas.MERIDIAN_SCAN, tools.meridian_scan),
        ("meridian_verify", schemas.MERIDIAN_VERIFY, tools.meridian_verify),
        ("meridian_diagram", schemas.MERIDIAN_DIAGRAM, tools.meridian_diagram),
        ("meridian_spec", schemas.MERIDIAN_SPEC, tools.meridian_spec),
        ("meridian_ledger", schemas.MERIDIAN_LEDGER, tools.meridian_ledger),
        ("meridian_report", schemas.MERIDIAN_REPORT, tools.meridian_report),
    ]
    for name, schema, handler in tool_specs:
        ctx.register_tool(name=name, toolset="meridian", schema=schema, handler=handler)

    ctx.register_hook("post_tool_call", _on_post_tool_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)

    ctx.register_command(
        "meridian",
        handler=_handle_meridian,
        description="Meridian: implicit state machines in this repo",
        args_hint="[status|scan|verify|check|ledger]",
    )

    ctx.register_cli_command(
        name="meridian",
        help="Meridian — find and verify implicit state machines",
        setup_fn=_setup_cli,
        handler_fn=_cli_main,
    )

    skill_md = Path(__file__).parent / "skills" / "audit" / "SKILL.md"
    if skill_md.is_file():
        ctx.register_skill("audit", skill_md)
