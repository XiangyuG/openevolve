# BPF libbpf-tools Performance Evolution

This example wires OpenEvolve to BPF C programs under heimdall-private:

```bash
$HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools
```

(default `HEIMDALL_ROOT=/users/xiang95/heimdall-private`)

The evaluator compiles each candidate with:

```bash
clang -g -O2 -target bpf \
  -D__TARGET_ARCH_x86 \
  -I $LIBBPF_TOOLS_DIR \
  -I /usr/include/x86_64-linux-gnu \
  -c <candidate>.bpf.c \
  -o <temp>.o
```

After compilation succeeds, it starts a workload (fio by default), runs the
matching libbpf runner for the selected tool, and includes the runner's
`run_cnt` / `run_time_ns` / `ns/run` stats in the OpenEvolve artifacts so the
next iteration sees the benchmark feedback.

## Supported tools

Select which BPF program family to evolve with `BPF_TOOL` (default `filetop`):

| `BPF_TOOL`   | initial program (`LIBBPF_TOOLS_DIR/...`) | runner              | output shape |
|--------------|-------------------------------------------|----------------------|--------------|
| `filetop`    | `filetop_op.bpf.c`                        | `filetop_runner`     | table        |
| `cachestat`  | `cachestat_op.bpf.c`                      | `cachestat_runner`   | table        |
| `tcprtt`     | `tcprtt_op.bpf.c`                         | `tcprtt_runner`      | table        |
| `biopattern` | `biopattern_op.bpf.c`                     | `biopattern_runner`  | single delta |
| `biostacks`  | `biostacks_op.bpf.c`                      | `biostacks_runner`   | single delta |
| `bitesize`   | `bitesize_op.bpf.c`                       | `bitesize_runner`    | single delta |
| `wakeuptime` | `wakeuptime_op.bpf.c`                     | `wakeuptime_runner`  | single delta |

Known gaps:
- `biotop`/`biotop_op` and `vfsstat` have runners in `libbpf-tools/` but they
  print continuous per-interval samples, not an aggregate `ns/run` figure, so
  they aren't wired into this evaluator.
- `tcprtt_op.bpf.c` currently fails to compile (`BPF_CORE_READ(sk,
  __sk_common, ...)` misuse) -- this is a pre-existing bug in the starting
  file, not something introduced by this evaluator.
- `bitesize_op.bpf.c` compiles with an `implicit declaration of function
  'bpf_map_lookup_or_try_init'` warning (pre-existing).
- The default fio workload only meaningfully exercises the disk/file-I/O
  triggered tools (`filetop`, `cachestat`, `biopattern`, `bitesize`,
  `biostacks`). `tcprtt` (TCP) and `wakeuptime` (scheduler) need a different
  trigger -- pass one via `BPF_WORKLOAD_CMD`.

## Equivalence checking

After a candidate compiles, the evaluator symbolically checks it against
`TOOL["source"]` (the program this evolution run started from) using
heimdall-private's `verify_mixed_entries.py`, run in its own `c2rust` conda
env. It checks every entry point the tool's runner actually exercises (not
necessarily every `SEC()` in the file -- e.g. `cachestat_op.bpf.c` also has
kprobe/tracepoint fallback variants the runner never attaches), and only maps
whose contents matter for equivalence (`STACK_TRACE` maps are intentionally
skipped, same as the worked example in heimdall-private's own README).

Result goes into `metrics["semantic_equivalent"]` (1.0 only if every checked
entry point comes back equivalent) plus a per-entry `equivalence_detail`
artifact (result type + counter-example when not equivalent). **This is an
informational signal, not a gate** -- it never zeroes the score or discards a
candidate by itself, because the checker itself is approximate. Concretely,
while testing this I confirmed it reliably catches return-value divergences
(gave a clean counter-example for a candidate that always returned 1 instead
of 0), but it did **not** catch a candidate where `vfs_read_entry` had its
`READ`/`WRITE` classification swapped -- because filetop's map update is a
lookup-then-mutate-in-place on the returned pointer (`valuep->reads++`)
rather than an explicit `bpf_map_update_elem()`, and the symbolic engine
doesn't appear to track that pattern as map-value-changing (`map_entries=0`
in its own trace output for that path). This in-place-mutate-via-pointer
idiom is common across these libbpf-tools histogram/counter programs, so
`semantic_equivalent: 1.0` should be read as "no return-value or
explicit-map-write divergence found", not as a real equivalence proof --
exactly why it's a signal for the developer, not an automatic pass.

Disable with `BPF_EQUIV_CHECK=0`. Other overrides: `BPF_EQUIV_CONDA_ENV`
(default `c2rust`), `BPF_EQUIV_TIMEOUT` (per entry point, default `90`).

## Transformation witnesses

The prompt (`prompt_templates/full_rewrite_user.txt`) asks the LLM to back
each numbered change with a `Witness:` line (English argument for why that
specific change preserves behavior) and **two** SMT-LIB 2 formulas:
`Formula (pre-transformation)` defines a constant `pre_result` in terms of
declared input variables, modeling what the OLD code computed for this
change; `Formula (post-transformation)` re-declares the same input names and
defines `post_result` the same way for the NEW code. Purely structural
changes (renaming, reordering, comments) get `(assert true)` on both sides
instead of a real model.

OpenEvolve core (`openevolve/utils/code_utils.py`,
`extract_transformation_witnesses` + `validate_transformation_proof`) parses
both formulas and **actually proves the claim**: it combines them with
`(assert (not (= pre_result post_result)))` and checks with `z3-solver` --
`unsat` means pre_result and post_result are provably equal for every input
(not just some input where they happen to agree); `sat` means Z3 found a
concrete counterexample, i.e. the LLM's own formulas disprove its own
equivalence claim. This is real per-witness verification, not just a syntax
check -- but it only proves the *local* claim the LLM chose to model (a few
declared variables), not that the formula matches the program's actual
compiled behavior; that cross-check against heimdall-private's real symbolic
execution (`generate_formula.py`/`ProgramFormula`) is still future work.

In non-interactive mode (`config.yaml`), the review UI (used only for
inspecting saved runs, not for gating anything) cross-references this
per-witness proof against heimdall's real equivalence-check result and shows
a banner when they disagree: if a witness's own formulas produce a
counterexample, or if every witness claims "purely structural" but the
equivalence checker (see above) found a real divergence anyway, that
conflict is called out explicitly. In interactive mode (see "Human-in-the-loop
review" below), witnesses are approved *before* any code is generated or
compiled, so heimdall's result doesn't exist yet at approval time and this
cross-check banner can't inform the decision -- it's only available
afterward, in the stored artifacts of the resulting candidate.

Smoke-test the evaluator directly with a short runtime:

```bash
BPF_RUNNER_SECONDS=5 BPF_RUNNER_TIMEOUT=20 BPF_FIO_RUNTIME=20 \
  python examples/bpf_compile/evaluator.py \
  $HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools/filetop_op.bpf.c
```

Run OpenEvolve on one BPF program:

```bash
python openevolve-run.py \
  $HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools/filetop_op.bpf.c \
  examples/bpf_compile/evaluator.py \
  --config examples/bpf_compile/config.yaml \
  --output openevolve_output/bpf_filetop \
  --iterations 50
```

To evolve a different tool, set `BPF_TOOL` and point at its initial program:

```bash
BPF_TOOL=cachestat python openevolve-run.py \
  $HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools/cachestat_op.bpf.c \
  examples/bpf_compile/evaluator.py \
  --config examples/bpf_compile/config.yaml \
  --output openevolve_output/bpf_cachestat \
  --iterations 50
```

## Saved candidates

Every candidate the evaluator sees gets a permanent copy on disk, regardless
of whether it compiles, how it benchmarks, or whether it's later
approved/rejected -- OpenEvolve's own population/checkpoints only keep what
survives, not a full history of every generated program.

Written to `$BPF_SAVE_DIR` (default `./generated_programs/<BPF_TOOL>`,
relative to wherever you invoke `openevolve-run.py`) as a pair of files per
candidate: `<timestamp>_<id>.bpf.c` (the source) and a matching `.json`
sidecar with that candidate's metrics. Set `BPF_SAVE_PROGRAMS=0` to disable.

## Human-in-the-loop review

`config_interactive.yaml` is the same setup as `config.yaml` plus an
`interactive:` block. Each iteration now runs in two LLM phases, with the
developer gate *between* them, before any code exists:

1. **Propose**: the LLM is asked only to describe its proposed change(s) as
   numbered transformation witnesses (`Witness:`/`Formula (pre/post-transformation):`,
   see above) -- no code, no diff. These are Z3-proven exactly as in
   non-interactive mode.
2. **Review**: the developer opens `/review` and sees the proposed witnesses
   (with their Z3 proof status) against the *parent's* code, with no child
   code or metrics yet -- there's nothing to compile or evaluate until
   something is approved. Each witness gets its own approve/reject button
   in addition to the overall approve/reject decision for the whole proposal.
3. **Implement**: only if the proposal is approved with at least one approved
   witness, a second LLM call is asked to implement *exactly* the approved
   witnesses (and none of the rejected/unreviewed ones). The resulting
   program is then compiled and evaluated exactly like non-interactive mode,
   and added to the population -- there is no further review round after
   this; the human gate is before code generation, not after evaluation.

If the proposal is rejected outright, or approved with zero individual
witnesses approved, it's treated as a rejection: the evaluator never runs,
and rejection feedback is fed back into the next proposal attempt for that
lineage. A lineage rejected `max_rejections_per_parent` times in a row is
abandoned in favor of normal island sampling.

1. Start the evolution run (blocks after each candidate until reviewed):

```bash
python openevolve-run.py \
  $HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools/filetop_op.bpf.c \
  examples/bpf_compile/evaluator.py \
  --config examples/bpf_compile/config_interactive.yaml \
  --output openevolve_output/bpf_filetop_interactive \
  --iterations 20
```

2. In another shell, start the review UI pointed at the same output dir:

```bash
python scripts/visualizer.py --path openevolve_output/bpf_filetop_interactive --port 8080
```

3. Open `http://127.0.0.1:8080/review` (or `http://<host>:8080/review` if
   running on a remote box you're forwarding/tunneling into) and
   approve/reject each pending proposal's witnesses. The page shows the
   parent code, the LLM's plain-English witnesses (child code and metrics
   stay empty until you approve -- see above), and takes an optional
   feedback note (required when rejecting) that's passed to the LLM for the
   next attempt on that lineage, or to the implement step as developer notes
   when approving.

Swap `BPF_TOOL`/the initial program the same way as the non-interactive runs
to review a different tool's evolution.

## Environment overrides

```bash
export HEIMDALL_ROOT=/users/xiang95/heimdall-private
export LIBBPF_TOOLS_DIR=$HEIMDALL_ROOT/c2rust_translation/c_bpf_programs/libbpf-tools
export BPF_TOOL=filetop
export BPF_CLANG=clang
export BPF_COMPILE_TIMEOUT=30
export BPF_RUN_BENCHMARK=1
export BPF_RUNNER=$LIBBPF_TOOLS_DIR/filetop_runner   # defaults to $LIBBPF_TOOLS_DIR/<tool>_runner
export BPF_RUNNER_SECONDS=60
export BPF_RUNNER_MAX_ENTRIES=256                    # used by filetop/tcprtt only
export BPF_WORKLOAD_CMD=                              # override the default fio invocation entirely
export BPF_FIO_RUNTIME=600
export BPF_SAVE_PROGRAMS=1                            # save every candidate to BPF_SAVE_DIR
export BPF_SAVE_DIR=./generated_programs/$BPF_TOOL
export BPF_EQUIV_CHECK=1                              # symbolic equivalence check vs TOOL["source"]
export BPF_EQUIV_CONDA_ENV=c2rust
export BPF_EQUIV_TIMEOUT=90                           # per entry point
```
