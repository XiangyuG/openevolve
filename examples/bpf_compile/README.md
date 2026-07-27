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
```
