"""Tool schemas — the contract the model reads."""

_ROOT_PROP = {
    "type": "string",
    "description": (
        "Absolute path of the repository (or subdirectory) to analyze. "
        "Defaults to the current session working directory when omitted."
    ),
}

MERIDIAN_SCAN = {
    "name": "meridian_scan",
    "description": (
        "Scan a repository and discover the implicit state machines hidden in it: "
        "state-like fields (status/state/phase/stage/...) that are assigned, "
        "compared and switched on — even though nobody wrote a state machine down. "
        "Returns each machine with its states, initial states and every anchored "
        "transition (file:line), plus unreachable and dead states. Use this first, "
        "before proposing fixes or refactors, whenever the user asks about states, "
        "status fields, lifecycles, or why an 'impossible' bug happened. Purely "
        "deterministic: no model calls are made."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root": _ROOT_PROP,
        },
        "required": [],
    },
}

MERIDIAN_VERIFY = {
    "name": "meridian_verify",
    "description": (
        "Verify discovered machines against the repository and, when a "
        ".meridian.yaml spec exists, against its allowed/forbidden transition edges. "
        "Detects: unreachable states (no path from the initial state), dead states "
        "(never written), unguarded writes (no same-function comparison on the "
        "field) and hard spec violations (anchored src->dst edges that are denied "
        "or not allowed). Call this after every edit that touches a machine's "
        "source files — especially when you (or the user) just changed lifecycle "
        "code — and before claiming a bug is fixed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root": _ROOT_PROP,
            "machine_id": {
                "type": "string",
                "description": "Optional machine id to verify (e.g. 'Order.status'). Omit to verify all.",
            },
            "strict": {
                "type": "boolean",
                "description": "When true, unguarded writes count as violations instead of warnings.",
            },
        },
        "required": [],
    },
}

MERIDIAN_DIAGRAM = {
    "name": "meridian_diagram",
    "description": (
        "Render a machine as Mermaid stateDiagram-v2 text, or export it as "
        "XState v5 JSON. Every transition is labeled with the function that "
        "performs it, so the diagram is code-accurate, not aspirational. "
        "Use 'xstate' output when the user wants to wire the machine up; "
        "Mermaid for docs, tickets and PR descriptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root": _ROOT_PROP,
            "machine_id": {
                "type": "string",
                "description": "Machine id (e.g. 'Order.status'). Required — call meridian_scan first to list ids.",
            },
            "format": {
                "type": "string",
                "enum": ["mermaid", "xstate"],
                "description": "Output format. Default: mermaid.",
            },
        },
        "required": ["machine_id"],
    },
}

MERIDIAN_SPEC = {
    "name": "meridian_spec",
    "description": (
        "Work with the .meridian.yaml spec — the enforcement layer. Actions: "
        "'draft' returns the spec Meridian would accept today (every edge is an "
        "observed, anchored transition; you review it with the user before "
        "accepting); 'accept' writes it (refuses to overwrite without force, keeps "
        "a .bak); 'show' prints the current spec; 'check' is the CI primitive: "
        "scan + verify against the accepted spec and report violations with "
        "file:line anchors. A spec is what turns 'this looks wrong' into 'this is "
        "forbidden'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root": _ROOT_PROP,
            "action": {
                "type": "string",
                "enum": ["draft", "accept", "show", "check"],
                "description": "Which spec operation to run.",
            },
            "force": {
                "type": "boolean",
                "description": "For 'accept': overwrite an existing .meridian.yaml (a .bak copy is kept).",
            },
            "strict": {
                "type": "boolean",
                "description": "For 'check': unguarded writes count as violations.",
            },
        },
        "required": ["action"],
    },
}

MERIDIAN_LEDGER = {
    "name": "meridian_ledger",
    "description": (
        "Inspect or verify the evidence ledger: an append-only, SHA-256 "
        "hash-chained audit of every scan, verdict, spec acceptance and check "
        "Meridian performed. 'verify' recomputes the chain and reports the first "
        "broken entry if anybody quietly edited history; 'show' lists recent "
        "entries; 'stats' shows event counts. The ledger records machine ids, "
        "verdict counts and file:line anchors — never file contents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["show", "verify", "stats"],
                "description": "Which ledger operation to run.",
            },
            "limit": {
                "type": "integer",
                "description": "For 'show': maximum entries to return (default 10, max 100).",
            },
        },
        "required": ["action"],
    },
}

MERIDIAN_REPORT = {
    "name": "meridian_report",
    "description": (
        "Produce a self-contained Markdown report of the repository's machines: "
        "summary table, per-machine states/transitions with file:line anchors, "
        "violations, unreachable and dead states, an embedded Mermaid diagram, "
        "and the ledger head so the report can be correlated with the audit "
        "trail. Pass out_file to also write it (default: report text returned "
        "only, nothing written)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "root": _ROOT_PROP,
            "out_file": {
                "type": "string",
                "description": "Optional path to write the report to (e.g. '.meridian/report.md'). Nothing is written when omitted.",
            },
        },
        "required": [],
    },
}

ALL_TOOLS = [MERIDIAN_SCAN, MERIDIAN_VERIFY, MERIDIAN_DIAGRAM, MERIDIAN_SPEC, MERIDIAN_LEDGER, MERIDIAN_REPORT]
