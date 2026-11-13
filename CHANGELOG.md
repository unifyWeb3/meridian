# Changelog

## 1.0.4

### Security

- Spec acceptance no longer follows repository-controlled symlinks. The live
  `.meridian.yaml` and its `.bak` are checked without following links, and
  both are replaced atomically so a symlink or hard link is never written
  through. Spec reads, tool and CLI reports, source scanning and the ledger
  reject symlink targets; the ledger also rejects hard links.

### Correctness and reliability

- The ledger now serializes stable reads, rejects malformed JSON objects,
  ties verification caching to the exact file snapshot that was checked,
  fails loudly when no lock primitive is available, and restores a private
  `0600` mode after repair. Conflicting Python enum definitions are no
  longer resolved by last-one-wins.

### Performance and maintainability

- Reports reuse the verification scan instead of scanning the repository a
  second time; the advisory hook groups edits by repository, performs one
  bounded walk and analysis per repository, and protects shared note state
  with a lock. Tool and CLI spec acceptance now share one merge/write path.

### Tests

- 95 stdlib unit tests, `hermes plugins doctor . --ci` and
  `hermes plugins validate . --install-deps` green.

## 1.0.3

Review fixes (the maintainer's changes-requested review, plus the hardening
and false-green work that came with it).

### Security

- The tool-level `root` argument is confined to the agent's working
  directory: resolved with realpath, then checked with `Path.is_relative_to`.
  `$HOME`, `/` and the Hermes home are refused as the working directory, and
  `out_file` must resolve inside the confined root. The `hermes meridian` CLI
  keeps its operator-supplied `--root` unconfined.

### Correctness

- Hook findings are stored and read under the session id, and the shared
  `"local"` bucket is gone: a finding reaches the next turn of its own
  session, and no other.
- `spec accept` merges into an existing `.meridian.yaml`: every deny rule and
  every authored edge survives, only newly discovered machines and edges are
  added, and `force` is a no-op.
- An absent `allow` key now means "no policy for this machine" (reported as
  unenforced); an explicit empty list means "no transition is allowed".
- Coverage counts the machines the spec states a rule for, reports
  `enforced / total`, and lists the machines that are mentioned but
  unenforced.
- Strict mode applies in every tool that runs a check, including
  `meridian_report` and `hermes meridian report`.

### Output and docs

- `check` reports a status instead of a bare "OK": `healthy` (no violations,
  nothing outstanding), `spec-clean` (no violations, something outstanding) or
  `violations`.
- The ledger is documented as what it is: an append-only, unkeyed SHA-256
  hash-chained log of event names, machine ids and aggregate counts - no
  verdicts, no file:line anchors, no file contents.
- `docs/smoke-output.txt` holds a real end-to-end CLI run.

### Tests

74 stdlib unit tests, `hermes plugins doctor . --ci` and
`hermes plugins validate . --install-deps` green.
