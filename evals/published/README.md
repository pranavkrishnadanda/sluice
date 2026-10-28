# Published eval runs

This directory holds the eval runs whose numbers appear in `evals/RESULTS.md` but cannot be
re-run in CI. Agent runs need model API keys, and their outputs vary from run to run. Fresh
runs go to `evals/runs/`, which is git-ignored. Copy a run here to publish it.

## Layout

Each entry is a run directory copied from `evals/runs/`, for example
`20261101T120000Z-agent-dev/`. Only its `summary.json` is required. `results.jsonl` may also
be committed so the per-case grades can be audited.

## How `sluice results` combines the two directories

Rows are keyed by (system, split, limit). A full-split run and a `--limit N` subset are
separate rows.

- For each key, the newest run in each directory is taken, by directory-name order.
- A published summary replaces the fresh one for the same key, with one exception: the
  full-split `rules` row. CI rebuilds the cases and re-runs that row from scratch
  (`make cases eval-rules`), so when both directories have it, the fresh run wins.
- A key found in only one directory is taken from that directory.

Every summary must match the split hash in `backend/cases/LOCK.json`, published ones too.
Otherwise `sluice results` refuses to render.

## CI

The "Reproduce published results" step re-runs the rules eval, then renders `RESULTS.md` from
the fresh rules run plus this directory. It fails if the committed `RESULTS.md` differs; the
run-date line is ignored. To publish a new run:

1. Copy its directory here.
2. Run `sluice results`.
3. Commit the directory and `RESULTS.md` together.
