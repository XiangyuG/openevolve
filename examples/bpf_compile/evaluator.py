"""
Compile and workload evaluator for evolving libbpf-tools BPF C programs.

The candidate program is first compiled into a BPF object file. If compilation
succeeds, the evaluator starts a workload (fio by default), runs the matching
libbpf runner for the selected tool (BPF_TOOL), and returns the runner's
runtime statistics as artifacts for the next OpenEvolve iteration.

Supported tools (select via BPF_TOOL, default "filetop"): filetop, cachestat,
tcprtt, biopattern, biostacks, bitesize, wakeuptime. Each tool has its own
runner binary/CLI signature and output format (a per-program stats table for
filetop/cachestat/tcprtt, a single run_cnt/run_time_ns delta block for the
others) -- both are normalized into the same runner_stats shape below.

biotop and vfsstat runners exist but print continuous per-interval samples
rather than an aggregate run_cnt/ns-per-run figure, so they are not wired up
here; wakeuptime/tcprtt are scheduler/network-triggered and are not exercised
by the default fio workload -- override BPF_WORKLOAD_CMD for those.
"""

import os
import json
import re
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from openevolve.evaluation_result import EvaluationResult
from openevolve.utils.code_utils import build_heimdall_witness_file


HEIMDALL_ROOT = Path(os.environ.get("HEIMDALL_ROOT", "/users/xiang95/heimdall-private"))
C2RUST_ROOT = HEIMDALL_ROOT / "c2rust_translation"
LIBBPF_TOOLS_DIR = Path(
    os.environ.get(
        "LIBBPF_TOOLS_DIR",
        str(C2RUST_ROOT / "c_bpf_programs" / "libbpf-tools"),
    )
)
CLANG = os.environ.get("BPF_CLANG", "clang")
TIMEOUT_SECONDS = int(os.environ.get("BPF_COMPILE_TIMEOUT", "30"))
RUN_BENCHMARK = os.environ.get("BPF_RUN_BENCHMARK", "1") != "0"
RUNNER_SECONDS = int(os.environ.get("BPF_RUNNER_SECONDS", "60"))
RUNNER_MAX_ENTRIES = int(os.environ.get("BPF_RUNNER_MAX_ENTRIES", "256"))
RUNNER_TIMEOUT = int(os.environ.get("BPF_RUNNER_TIMEOUT", str(RUNNER_SECONDS + 30)))
FIO = os.environ.get("BPF_FIO", "fio")
FIO_FILENAME = os.environ.get("BPF_FIO_FILENAME", "/tmp/filetop.data")
FIO_SIZE = os.environ.get("BPF_FIO_SIZE", "2G")
FIO_RUNTIME = int(os.environ.get("BPF_FIO_RUNTIME", "600"))
FIO_WARMUP_SECONDS = float(os.environ.get("BPF_FIO_WARMUP_SECONDS", "2"))
WORKLOAD_CMD_OVERRIDE = os.environ.get("BPF_WORKLOAD_CMD")


def _table_args(obj: Path) -> list[str]:
    return [str(obj), str(RUNNER_SECONDS)]


def _filetop_args(obj: Path) -> list[str]:
    return [str(obj), str(RUNNER_SECONDS), str(RUNNER_MAX_ENTRIES)]


def _tcprtt_args(obj: Path) -> list[str]:
    return [str(obj), "fentry", str(RUNNER_SECONDS), str(RUNNER_MAX_ENTRIES)]


def _generic_runner_args(obj: Path) -> list[str]:
    # generic_runner takes the attach-target program names directly on argv
    # (see c_bpf_programs/libbpf-tools/generic_runner.c) -- reuse the
    # currently-selected tool's own equiv_entries so there's exactly one
    # source of truth per tool for "which programs actually get exercised",
    # rather than a second hardcoded list living in a bespoke *_runner.c.
    return [str(obj), str(RUNNER_SECONDS), *TOOL["equiv_entries"]]


# Each entry: initial-program filename (also the equivalence-check baseline --
# "the original program" this evolution run started from), runner binary name
# in LIBBPF_TOOLS_DIR, an argv builder, the output shape the runner prints
# ("table" for multi-program stats rows, "single" for a one-block run_cnt/
# run_time_ns delta), and the equivalence-check scope: equiv_entries are the
# program symbols the runner actually exercises (not necessarily every SEC()
# in the file -- e.g. cachestat_op.bpf.c also has kprobe/tracepoint fallback
# variants of the same probes that the runner never attaches), and equiv_maps
# are "name:type" specs for verify_mixed_entries.py (type must be "hash" or
# "array"; PERCPU_*/LRU_* variants are approximated as their base type by the
# checker itself, and STACK_TRACE maps are intentionally omitted -- same as
# the worked example in heimdall-private's README -- since they hold raw,
# environment-dependent stack IDs rather than meaningfully comparable state).
TOOLS = {
    "filetop": {
        "source": "filetop_op.bpf.c",
        "runner": "filetop_runner",
        "args": _filetop_args,
        "parser": "table",
        "equiv_entries": ["vfs_read_entry", "vfs_write_entry"],
        "equiv_maps": ["entries:hash"],
    },
    "cachestat": {
        "source": "cachestat_op.bpf.c",
        "runner": "cachestat_runner",
        "args": _table_args,
        "parser": "table",
        "equiv_entries": [
            "fentry_add_to_page_cache_lru",
            "fentry_mark_page_accessed",
            "fentry_account_page_dirtied",
            "fentry_mark_buffer_dirty",
        ],
        "equiv_maps": ["stats:array"],
    },
    "tcprtt": {
        "source": "tcprtt_op.bpf.c",
        "runner": "tcprtt_runner",
        "args": _tcprtt_args,
        "parser": "table",
        # tcprtt_op.bpf.c currently fails to compile (pre-existing bug, see
        # README) so this never gets exercised; best-effort until it's fixed.
        "equiv_entries": ["tcp_rcv"],
        "equiv_maps": ["hists:hash"],
    },
    "biopattern": {
        "source": "biopattern_op.bpf.c",
        "runner": "biopattern_runner",
        "args": _table_args,
        "parser": "single",
        "equiv_entries": ["handle__block_rq_complete"],
        "equiv_maps": ["counters:hash"],
    },
    "biostacks": {
        "source": "biostacks_op.bpf.c",
        "runner": "biostacks_runner",
        "args": _table_args,
        "parser": "single",
        "equiv_entries": [
            "blk_account_io_merge_bio",
            "blk_account_io_start",
            "blk_account_io_done",
        ],
        "equiv_maps": ["rqinfos:hash", "hists:hash"],
    },
    "bitesize": {
        "source": "bitesize_op.bpf.c",
        "runner": "bitesize_runner",
        "args": _table_args,
        "parser": "single",
        "equiv_entries": ["block_rq_issue"],
        "equiv_maps": ["hists:hash"],
    },
    "wakeuptime": {
        "source": "wakeuptime_op.bpf.c",
        "runner": "wakeuptime_runner",
        "args": _table_args,
        "parser": "single",
        "equiv_entries": ["sched_switch", "sched_wakeup"],
        "equiv_maps": ["counts:hash", "start:hash"],
    },
    # The tools below share one generic_runner (see generic_runner.c) instead
    # of a bespoke *_runner.c each -- it attaches whatever program names its
    # argv is given (here, always equiv_entries) via plain
    # bpf_program__attach(), tolerating individual attach failures (expected
    # for CO-RE tp_btf/raw_tp fallback pairs, where usually only one side is
    # supported by a given kernel), and reads the kernel's own per-program
    # bpf_prog_info counters -- no tool-specific map-reading code needed.
    # "workload" (interpreter, script path relative to LIBBPF_TOOLS_DIR) is
    # the default workload for that tool absent a BPF_WORKLOAD_CMD override;
    # tools without one fall back to the fio default, which already suits
    # plain file-I/O-triggered tools.
    "drsnoop": {
        "source": "drsnoop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["direct_reclaim_begin_btf", "direct_reclaim_end_btf", "direct_reclaim_begin", "direct_reclaim_end"],
        "equiv_maps": ["start:hash"],
    },
    "fsdist": {
        "source": "fsdist.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["file_read_entry", "file_read_exit", "file_write_entry", "file_write_exit", "file_open_entry", "file_open_exit", "file_sync_entry", "file_sync_exit", "getattr_entry", "getattr_exit", "file_read_fentry", "file_read_fexit", "file_write_fentry", "file_write_fexit", "file_open_fentry", "file_open_fexit", "file_sync_fentry", "file_sync_fexit", "getattr_fentry", "getattr_fexit"],
        "equiv_maps": ["starts:hash"],
    },
    "fsslower": {
        "source": "fsslower.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["file_read_entry", "file_read_exit", "file_write_entry", "file_write_exit", "file_open_entry", "file_open_exit", "file_sync_entry", "file_sync_exit", "file_read_fentry", "file_read_fexit", "file_write_fentry", "file_write_fexit", "file_open_fentry", "file_open_fexit", "file_sync_fentry", "file_sync_fexit"],
        "equiv_maps": ["starts:hash"],
    },
    "futexctn": {
        "source": "futexctn.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        # NOTE: heimdall's symbolic execution on "futex_exit" alone (self-vs-
        # self) still hadn't resolved after 320s in testing -- likely path
        # explosion in the histogram-bucketing logic, not a wiring issue
        # (BTF/entry name/map spec all check out; "futex_enter" alone is
        # fast). semantic_equivalent for this tool will likely show
        # result_type "timeout" (handled gracefully, not a crash) rather than
        # a real true/false verdict until this is looked at specifically.
        "equiv_entries": ["futex_enter", "futex_exit"],
        "equiv_maps": ["start:hash", "hists:hash"],
        "workload": ("python3", "workloads/futex_contention.py"),
    },
    "mdflush": {
        "source": "mdflush.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        # No dedicated workload yet -- the default fio workload doesn't
        # reliably trigger a real block-device flush; confirmed 0 events
        # end-to-end in testing. Equivalence checking is unaffected.
        "equiv_entries": ["md_flush_request", "kprobe_md_flush_request"],
        "equiv_maps": [],
    },
    "mountsnoop": {
        "source": "mountsnoop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["mount_entry", "mount_exit", "umount_entry", "umount_exit", "fsopen_entry", "fsopen_exit", "fsconfig_entry", "fsconfig_exit", "fsmount_entry", "fsmount_exit", "move_mount_entry", "move_mount_exit"],
        "equiv_maps": ["heap:array", "args:hash"],
        "workload": ("sudo_shell", "workloads/mount_loop.sh"),
    },
    "numamove": {
        "source": "numamove.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        # Needs real multi-NUMA-node memory pressure to ever fire -- best
        # effort. Equivalence checking (below) doesn't depend on the
        # workload ever triggering it; it's a static/symbolic check of the
        # compiled object, not runtime events.
        "equiv_entries": ["fentry_migrate_misplaced_page", "fentry_migrate_misplaced_folio", "kprobe_migrate_misplaced_page", "kprobe_migrate_misplaced_folio", "fexit_migrate_misplaced_page_exit", "fexit_migrate_misplaced_folio_exit", "kretprobe_migrate_misplaced_page_exit", "kretprobe_migrate_misplaced_folio_exit"],
        "equiv_maps": ["start:hash"],
    },
    "offcputime": {
        "source": "offcputime.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["sched_switch", "sched_switch_raw"],
        "equiv_maps": ["start:hash", "info:hash", "tgids:hash", "pids:hash"],
        "workload": ("python3", "workloads/sched_stress.py"),
    },
    "oomkill": {
        "source": "oomkill.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        # Deliberately no dedicated workload -- triggering this means an
        # actual OOM kill on the box. Zero events is the expected, accepted
        # outcome (see evaluator.py's "average execution: unavailable"
        # handling); equivalence checking doesn't need it to ever fire.
        "equiv_entries": ["oom_kill_process"],
        "equiv_maps": ["heap:array"],
    },
    "runqslower": {
        "source": "runqslower.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["sched_wakeup", "sched_wakeup_new", "sched_switch", "handle_sched_wakeup", "handle_sched_wakeup_new", "handle_sched_switch"],
        "equiv_maps": ["start:hash"],
        "workload": ("python3", "workloads/sched_stress.py"),
    },
    "sigsnoop": {
        "source": "sigsnoop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        # NOTE: fails to LOAD on this box's kernel/libbpf combo -- "libbpf:
        # extern weak function bpf_task_from_pid is unsupported" (the source
        # guards its use with bpf_ksym_exists(), but even referencing it as
        # weak trips up this libbpf version at bpf_object__open_file() time).
        # Confirmed 0 runtime events end-to-end. heimdall's equivalence check
        # is unaffected (angr's static ELF/BTF parsing doesn't go through
        # libbpf's real kernel-loading path at all).
        "equiv_entries": ["kill_entry", "kill_exit", "tkill_entry", "tkill_exit", "tgkill_entry", "tgkill_exit", "sig_trace"],
        "equiv_maps": ["values:hash"],
        "workload": ("python3", "workloads/signal_loop.py"),
    },
    "slabratetop": {
        "source": "slabratetop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["kmem_cache_alloc", "kmem_cache_alloc_noprof"],
        "equiv_maps": ["slab_entries:hash"],
    },
    "softirqs": {
        "source": "softirqs.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["softirq_entry_btf", "softirq_exit_btf", "softirq_entry", "softirq_exit"],
        "equiv_maps": ["start:array"],
        "workload": ("python3", "workloads/sched_stress.py"),
    },
    "solisten": {
        "source": "solisten.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["inet_listen_entry", "inet_listen_exit", "inet_listen_fexit"],
        "equiv_maps": ["values:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "statsnoop": {
        "source": "statsnoop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["handle_statfs_entry", "handle_statfs_return", "handle_newstat_entry", "handle_newstat_return", "handle_statx_entry", "handle_statx_return", "handle_newfstat_entry", "handle_newfstat_return", "handle_newfstatat_entry", "handle_newfstatat_return", "handle_newlstat_entry", "handle_newlstat_return"],
        "equiv_maps": ["values:hash"],
    },
    "syncsnoop": {
        "source": "syncsnoop.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["tracepoint__syscalls__sys_enter_sync", "tracepoint__syscalls__sys_enter_fsync", "tracepoint__syscalls__sys_enter_fdatasync", "tracepoint__syscalls__sys_enter_msync", "tracepoint__syscalls__sys_enter_sync_file_range", "tracepoint__syscalls__sys_enter_sync_file_range2", "tracepoint__syscalls__sys_enter_arm_sync_file_range", "tracepoint__syscalls__sys_enter_syncfs"],
        "equiv_maps": [],
    },
    "tcpconnect": {
        "source": "tcpconnect.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["tcp_v4_connect", "tcp_v4_connect_ret", "tcp_v6_connect", "tcp_v6_connect_ret"],
        "equiv_maps": ["sockets:hash", "ipv4_count:hash", "ipv6_count:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "tcpconnlat": {
        "source": "tcpconnlat.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["tcp_v4_connect", "tcp_v6_connect", "tcp_rcv_state_process", "tcp_destroy_sock", "fentry_tcp_v4_connect", "fentry_tcp_v6_connect", "fentry_tcp_rcv_state_process"],
        "equiv_maps": ["start:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "tcplife": {
        "source": "tcplife.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["inet_sock_set_state"],
        "equiv_maps": ["birth:hash", "idents:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "tcpstates": {
        "source": "tcpstates.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["handle_set_state"],
        "equiv_maps": ["sports:hash", "dports:hash", "timestamps:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "tcpsynbl": {
        "source": "tcpsynbl.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["tcp_v4_syn_recv_kprobe", "tcp_v6_syn_recv_kprobe", "tcp_v4_syn_recv", "tcp_v6_syn_recv"],
        "equiv_maps": ["hists:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    "tcptracer": {
        "source": "tcptracer.bpf.c",
        "runner": "generic_runner",
        "args": _generic_runner_args,
        "parser": "table",
        "equiv_entries": ["tcp_v4_connect", "tcp_v4_connect_ret", "tcp_v6_connect", "tcp_v6_connect_ret", "entry_trace_close", "enter_tcp_set_state", "exit_inet_csk_accept"],
        "equiv_maps": ["tuplepid:hash", "sockets:hash"],
        "workload": ("python3", "workloads/network_loop.py"),
    },
    # Deferred (not in this batch): ksnoop (dynamic runtime-configured
    # kprobe target, doesn't fit "attach by fixed name"), llcstat/runqlen/
    # profile (perf_event/PMU attach, different mechanism), bashreadline/
    # gethostlatency/memleak/javagc (uprobe/USDT on an external process).
    # filelife/opensnoop/readahead/tcpktlat don't compile in this dataset
    # (missing path_helpers.bpf.h/compat.bpf.h headers, or a struct folio/
    # vmlinux.h kernel-version mismatch) -- same class of pre-existing gap
    # as tcprtt_op/bitesize_op noted in the README.
}

BPF_TOOL = os.environ.get("BPF_TOOL", "filetop")
if BPF_TOOL not in TOOLS:
    raise ValueError(f"BPF_TOOL={BPF_TOOL!r} is not one of {sorted(TOOLS)}")
TOOL = TOOLS[BPF_TOOL]
RUNNER = Path(os.environ.get("BPF_RUNNER", str(LIBBPF_TOOLS_DIR / TOOL["runner"])))

# Every candidate this evaluator sees gets a permanent copy on disk, regardless
# of whether it compiles, benchmarks well, or is later approved/rejected in
# interactive review -- OpenEvolve itself only keeps the population/checkpoints,
# not a full history of every generated candidate.
SAVE_PROGRAMS = os.environ.get("BPF_SAVE_PROGRAMS", "1") != "0"
SAVE_DIR = Path(os.environ.get("BPF_SAVE_DIR", str(Path("generated_programs") / BPF_TOOL)))

# Symbolic-execution equivalence check (heimdall-private's verify_mixed_entries.py,
# run in its own "c2rust" conda env) comparing each candidate against the tool's
# baseline (TOOL["source"] -- the program this evolution run started from). This
# is an informational signal, not a hard gate: it feeds semantic_equivalent into
# metrics/artifacts for the developer to weigh, but never zeroes out the score or
# discards a candidate on its own -- the checker itself is an approximation (see
# TOOLS comment above) and false negatives would otherwise silently kill good
# candidates.
EQUIV_CHECK = os.environ.get("BPF_EQUIV_CHECK", "1") != "0"
EQUIV_CONDA_ENV = os.environ.get("BPF_EQUIV_CONDA_ENV", "c2rust")
EQUIV_TIMEOUT = int(os.environ.get("BPF_EQUIV_TIMEOUT", "90"))
EQUIV_VERIFIER = HEIMDALL_ROOT / "c2rust_translation" / "verify_mixed_entries.py"
BTF_PARSER = HEIMDALL_ROOT / "c2rust_translation" / "btf_parser.py"


RUNNER_ROW_PATTERN = re.compile(
    r"^(?P<program>\S+)\s+"
    r"(?P<run_cnt>\d+)\s+"
    r"(?P<run_time_ns>\d+)\s+"
    r"(?P<ns_per_run>(?:\d+(?:\.\d+)?|n/a))\s*$"
)

SINGLE_RUN_CNT_PATTERN = re.compile(r"run_cnt delta:\s*(\d+)")
SINGLE_RUN_TIME_PATTERN = re.compile(r"run_time_ns delta:\s*(\d+)")
SINGLE_AVG_PATTERN = re.compile(r"average execution:\s*([\d.]+)\s*ns/run")

EQUIV_RESULT_PATTERN = re.compile(r"^equivalent:\s*(True|False)\s*$", re.MULTILINE)
EQUIV_RESULT_TYPE_PATTERN = re.compile(r"^result_type:\s*(\S+)\s*$", re.MULTILINE)
EQUIV_COUNTEREXAMPLE_PATTERN = re.compile(r"^counter_example:\s*(.*)", re.MULTILINE | re.DOTALL)


def _save_candidate(source: str, metrics: dict) -> None:
    if not SAVE_PROGRAMS:
        return
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    (SAVE_DIR / f"{stamp}.bpf.c").write_text(source, encoding="utf-8")
    (SAVE_DIR / f"{stamp}.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8"
    )


_UNSET = object()
_baseline_object_cache: object = _UNSET


def _baseline_object() -> Path | None:
    """Compile TOOL["source"] (this run's starting program) once per worker
    process and cache the result -- it's the fixed ground truth every
    candidate gets equivalence-checked against."""
    global _baseline_object_cache
    if _baseline_object_cache is not _UNSET:
        return _baseline_object_cache

    baseline_source = LIBBPF_TOOLS_DIR / TOOL["source"]
    baseline_object = Path(tempfile.gettempdir()) / f"openevolve_bpf_baseline_{BPF_TOOL}.o"

    cmd = [
        CLANG,
        "-g",
        "-O2",
        "-target",
        "bpf",
        "-D__TARGET_ARCH_x86",
        "-I",
        str(LIBBPF_TOOLS_DIR),
        "-I",
        "/usr/include/x86_64-linux-gnu",
        "-c",
        str(baseline_source),
        "-o",
        str(baseline_object),
    ]
    try:
        result = subprocess.run(
            cmd, cwd=str(HEIMDALL_ROOT), capture_output=True, text=True, timeout=TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        _baseline_object_cache = None
        return None

    _baseline_object_cache = (
        baseline_object if result.returncode == 0 and baseline_object.exists() else None
    )
    return _baseline_object_cache


def _map_value_sizes(obj: Path) -> dict[str, int]:
    """Real BTF value_size (bytes) per map in `obj`, via btf_parser.py --json
    in the c2rust conda env. Empty dict on any failure (missing BTF, timeout,
    bad JSON) -- callers must treat that as "unknown", not "zero-size"."""
    try:
        result = subprocess.run(
            ["conda", "run", "-n", EQUIV_CONDA_ENV, "python", str(BTF_PARSER), str(obj), "--json"],
            cwd=str(HEIMDALL_ROOT / "c2rust_translation"),
            capture_output=True,
            text=True,
            timeout=EQUIV_TIMEOUT,
        )
        parsed = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return {}
    return {name: meta["value_size"] for name, meta in parsed.items() if "value_size" in meta}


_baseline_map_value_sizes_cache: object = _UNSET


def _baseline_map_value_sizes() -> dict[str, int]:
    """_map_value_sizes() for the baseline object, cached like _baseline_object()
    -- the baseline is fixed for this whole run, so its real per-map value
    sizes never change either."""
    global _baseline_map_value_sizes_cache
    if _baseline_map_value_sizes_cache is not _UNSET:
        return _baseline_map_value_sizes_cache
    baseline_object = _baseline_object()
    _baseline_map_value_sizes_cache = (
        _map_value_sizes(baseline_object) if baseline_object is not None else {}
    )
    return _baseline_map_value_sizes_cache


def _correct_map_width_change_hints(
    witnesses: "list[dict]", candidate_object: Path
) -> "list[dict]":
    """Replace each witness's self-reported map_width_change (old_bytes,
    new_bytes) with the REAL BTF value_size of that map in (respectively) the
    baseline object and this specific candidate object, whenever both are
    known.

    Why: heimdall (verify_equivalence.py) only honors a --relax-map-value-width
    hint when it EXACTLY equals the two binaries' real BTF value_size -- by
    design, since old_bytes also drives how the truncated-equality comparison
    itself is built, not just a sanity gate. A witness only ever describes the
    diff against its own immediate PARENT, though, not against the run's fixed
    original baseline heimdall actually compares against. First-generation
    narrowings happen to have parent == baseline, so they match by accident;
    any later generation narrowing an already-narrowed map states an old_bytes
    that reflects its parent's size, not the baseline's, and heimdall silently
    (and correctly) rejects it as stale, re-reporting the exact same "BTF
    MISMATCH" the relaxation was supposed to avoid.

    Substituting ground truth read directly from BTF -- instead of composing
    or trusting any self-reported number -- fixes this regardless of how many
    generations deep the narrowing chain is, and is immune to an LLM's own
    arithmetic being slightly off (e.g. padding/alignment miscounted).

    Witnesses without a map_width_change, or where either side's real size
    can't be determined, are returned unchanged -- heimdall's own exact-match
    gate is the backstop either way, so an uncorrected/wrong hint can only
    ever fall back to the strict check, never produce an unsound accept."""
    baseline_sizes = _baseline_map_value_sizes()
    if not baseline_sizes:
        return witnesses
    candidate_sizes = _map_value_sizes(candidate_object)
    if not candidate_sizes:
        return witnesses

    corrected = []
    for w in witnesses:
        mwc = w.get("map_width_change")
        if not mwc:
            corrected.append(w)
            continue
        map_name = mwc.get("map")
        real_old = baseline_sizes.get(map_name)
        real_new = candidate_sizes.get(map_name)
        if real_old is None or real_new is None:
            corrected.append(w)
            continue
        w = dict(w)
        w["map_width_change"] = {"map": map_name, "old_bytes": real_old, "new_bytes": real_new}
        corrected.append(w)
    return corrected


def _witness_file_args(
    witnesses: "list[dict] | None", tmp_dir: str
) -> tuple[list[str], "Path | None"]:
    """Write developer-approved witnesses (see
    openevolve/utils/code_utils.py's extract_transformation_witnesses) out as a
    heimdall-private --witness-file JSON (build_heimdall_witness_file) and
    return the ["--witness-file", path] flag for verify_mixed_entries.py,
    plus the path itself (so the caller can leave it in tmp_dir's lifetime).

    --witness-file lets heimdall derive BOTH --relax-map-value-width hints
    (from each witness's "map_width_change") AND map-fusion groups (from
    "map_fusion") itself, and cross-validate them against the object files'
    real BTF metadata / its own extracted formulas before relaxing anything or
    treating two maps as merged -- a wrong or stale hint here can't force a
    false "equivalent" verdict.

    Returns ([], None) if there are no qualifying witnesses (nothing to
    write)."""
    witness_file_data = build_heimdall_witness_file(witnesses or [])
    if not witness_file_data["witnesses"]:
        return [], None

    witness_file_path = Path(tmp_dir) / "witnesses.json"
    witness_file_path.write_text(json.dumps(witness_file_data, indent=2))
    return ["--witness-file", str(witness_file_path)], witness_file_path


def _check_equivalence(
    candidate_object: Path,
    witnesses: "list[dict] | None" = None,
    metric_key: str = "semantic_equivalent",
    detail_key: str = "equivalence_detail",
) -> tuple[dict, dict]:
    """Symbolically check candidate_object against the baseline for every entry
    point in TOOL["equiv_entries"]. Returns (metrics, artifacts); metrics always
    contains metric_key (defaulting to 0.0/"unknown" when the checker itself
    couldn't be run) so it's safe to use as a config feature_dimension --
    OpenEvolve requires every feature_dimension to be present in every program's
    metrics, so this key must never be conditionally omitted.

    witnesses (only passed by reverify_with_witnesses(), never by the
    unconditional baseline check in evaluate()) narrows specific hard
    structural gates in heimdall's checker -- e.g. the BTF value_size
    equality check, or requiring two maps to still be BTF-distinct -- for
    changes a developer has already approved; see _witness_file_args. When
    witnesses are passed, each entry's result also carries a
    "witness_verification" list (heimdall's OWN per-witness cross-check of
    each map_width_change/map_fusion claim against its real extracted
    formulas -- see verify_equivalence.py's run_witness_verification -- not
    just the witness's self-reported Z3 proof), if heimdall produced one."""
    entries = TOOL.get("equiv_entries") or []
    maps = TOOL.get("equiv_maps") or []
    if not entries:
        return {metric_key: 0.0}, {}

    baseline_object = _baseline_object()
    if baseline_object is None:
        return (
            {metric_key: 0.0},
            {f"{detail_key}_error": "baseline object unavailable (compile failed)"},
        )

    per_entry: dict[str, dict] = {}
    all_equivalent = True

    with tempfile.TemporaryDirectory(prefix="openevolve_bpf_witness_") as witness_tmp_dir:
        witness_args, _ = _witness_file_args(witnesses, witness_tmp_dir)

        for entry in entries:
            safe_entry = re.sub(r"[^A-Za-z0-9_.-]", "_", entry)
            json_output_path = Path(witness_tmp_dir) / f"result_{safe_entry}.json"
            cmd = [
                "conda",
                "run",
                "-n",
                EQUIV_CONDA_ENV,
                "python",
                str(EQUIV_VERIFIER),
                str(baseline_object),
                str(candidate_object),
                entry,
                entry,
                *maps,
                *witness_args,
                "--json-output",
                str(json_output_path),
            ]
            try:
                result = subprocess.run(
                    cmd,
                    cwd=str(HEIMDALL_ROOT / "c2rust_translation"),
                    capture_output=True,
                    text=True,
                    timeout=EQUIV_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                per_entry[entry] = {
                    "equivalent": None,
                    "result_type": "timeout",
                    "counter_example": None,
                }
                all_equivalent = False
                continue

            json_result = None
            if json_output_path.exists():
                try:
                    json_result = json.loads(json_output_path.read_text())
                except (json.JSONDecodeError, OSError):
                    json_result = None

            if json_result is not None:
                # Prefer the checker's own structured output over regex-scraping
                # stdout -- "detail" fields on witness_results can contain
                # multi-line z3 model text that isn't safe to line-match.
                equivalent = json_result.get("equivalent")
                per_entry[entry] = {
                    "equivalent": equivalent,
                    "result_type": json_result.get("result_type"),
                    "counter_example": json_result.get("counter_example") or None,
                }
                if json_result.get("witness_results"):
                    per_entry[entry]["witness_verification"] = json_result["witness_results"]
            else:
                # Fall back to scraping stdout (e.g. heimdall crashed before
                # writing --json-output, or is an older checkout without it).
                output = result.stdout
                eq_match = EQUIV_RESULT_PATTERN.search(output)
                type_match = EQUIV_RESULT_TYPE_PATTERN.search(output)
                ce_match = EQUIV_COUNTEREXAMPLE_PATTERN.search(output)

                equivalent = (eq_match.group(1) == "True") if eq_match else None
                per_entry[entry] = {
                    "equivalent": equivalent,
                    "result_type": type_match.group(1) if type_match else None,
                    "counter_example": ce_match.group(1).strip() if ce_match else None,
                }
                if not eq_match:
                    # Nothing recognizable in stdout -- the verifier likely errored out
                    # before printing a result (e.g. conda env / entry symbol / map spec
                    # problem). Keep the raw output so this is debuggable from the
                    # artifacts instead of silently reading as "unknown".
                    per_entry[entry]["raw_output"] = (result.stdout + result.stderr)[-2000:]
            if equivalent is not True:
                all_equivalent = False

    metrics = {metric_key: 1.0 if all_equivalent else 0.0}
    artifacts = {detail_key: json.dumps(per_entry, indent=2, sort_keys=True)}
    return metrics, artifacts


def reverify_with_witnesses(program_path: str, witnesses: list) -> EvaluationResult:
    """Optional second-pass equivalence re-check, called only by OpenEvolve's
    interactive review flow (openevolve/process_parallel.py's
    _reverify_approved_witnesses) after a developer has approved specific
    transformation witnesses claiming a BPF map's value type was narrowed
    and/or two or more maps were merged.

    `witnesses` is a list of developer-approved, self-proven witness dicts,
    each carrying a "map_width_change" and/or "map_fusion" hint (see
    extract_transformation_witnesses). This recompiles the candidate and
    re-runs the same heimdall checker as the unconditional evaluate() path,
    but with a --witness-file (see _witness_file_args) that lets it skip the
    exact BTF value_size equality gate for narrowed map(s) and treat declared
    source maps as merged into their target -- everything else about the
    check (the rest of the symbolic proof) is unchanged, and the checker
    itself is expected to verify each hint against the object files' real BTF
    metadata before honoring it. Result is stored under a distinct metric
    (semantic_equivalent_relaxed), never overwriting the unconditional
    semantic_equivalent from evaluate()."""
    source_path = Path(program_path)

    if not witnesses:
        return EvaluationResult(
            metrics={"semantic_equivalent_relaxed": 0.0},
            artifacts={"error": "reverify_with_witnesses called with no witnesses"},
        )

    with tempfile.TemporaryDirectory(prefix="openevolve_bpf_reverify_") as tmp_dir:
        output_path = Path(tmp_dir) / f"{source_path.stem}.o"
        cmd = [
            CLANG,
            "-g",
            "-O2",
            "-target",
            "bpf",
            "-D__TARGET_ARCH_x86",
            "-I",
            str(LIBBPF_TOOLS_DIR),
            "-I",
            "/usr/include/x86_64-linux-gnu",
            "-c",
            str(source_path),
            "-o",
            str(output_path),
        ]

        try:
            result = subprocess.run(
                cmd, cwd=str(HEIMDALL_ROOT), capture_output=True, text=True, timeout=TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired:
            return EvaluationResult(
                metrics={"semantic_equivalent_relaxed": 0.0},
                artifacts={"error": f"clang timed out after {TIMEOUT_SECONDS}s (reverify)"},
            )

        if result.returncode != 0 or not output_path.exists():
            return EvaluationResult(
                metrics={"semantic_equivalent_relaxed": 0.0},
                artifacts={
                    "error": "clang compilation failed (reverify)",
                    "stderr": result.stderr[-4000:],
                },
            )

        # Replace each witness's self-reported map_width_change with the REAL
        # baseline-vs-this-candidate BTF value_size (see
        # _correct_map_width_change_hints) -- this is what makes a witness
        # narrowing an already-narrowed map (i.e. anything past the first
        # generation of a given map's narrowing) actually match heimdall's
        # exact-equality hint gate instead of silently falling back to the
        # strict check every time.
        witnesses = _correct_map_width_change_hints(witnesses, output_path)

        metrics, artifacts = _check_equivalence(
            output_path,
            witnesses=witnesses,
            metric_key="semantic_equivalent_relaxed",
            detail_key="equivalence_relaxed_detail",
        )
        artifacts["relaxed_hints"] = json.dumps(
            build_heimdall_witness_file(witnesses), sort_keys=True
        )
        return EvaluationResult(metrics=metrics, artifacts=artifacts)


def _parse_runner_stats(output: str) -> dict[str, dict[str, float | int | None]]:
    if TOOL["parser"] == "table":
        return _parse_table_stats(output)
    return _parse_single_stats(output)


def _parse_table_stats(output: str) -> dict[str, dict[str, float | int | None]]:
    stats: dict[str, dict[str, float | int | None]] = {}
    for line in output.splitlines():
        match = RUNNER_ROW_PATTERN.match(line.strip())
        if not match:
            continue

        ns_per_run_text = match.group("ns_per_run")
        stats[match.group("program")] = {
            "run_cnt": int(match.group("run_cnt")),
            "run_time_ns": int(match.group("run_time_ns")),
            "ns_per_run": None if ns_per_run_text == "n/a" else float(ns_per_run_text),
        }
    return stats


def _parse_single_stats(output: str) -> dict[str, dict[str, float | int | None]]:
    run_cnt_match = SINGLE_RUN_CNT_PATTERN.search(output)
    run_time_match = SINGLE_RUN_TIME_PATTERN.search(output)
    avg_match = SINGLE_AVG_PATTERN.search(output)

    if not run_cnt_match or not run_time_match:
        return {}

    return {
        BPF_TOOL: {
            "run_cnt": int(run_cnt_match.group(1)),
            "run_time_ns": int(run_time_match.group(1)),
            "ns_per_run": float(avg_match.group(1)) if avg_match else None,
        }
    }


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except Exception:
            pass


def _run_workload_benchmark(object_path: Path) -> tuple[dict[str, float], dict[str, str]]:
    if WORKLOAD_CMD_OVERRIDE:
        import shlex

        workload_cmd = shlex.split(WORKLOAD_CMD_OVERRIDE)
    elif TOOL.get("workload"):
        # Per-tool default (see the generic_runner-based TOOLS entries above)
        # so each tool works out of the box without the caller having to set
        # BPF_WORKLOAD_CMD to match whichever tool BPF_TOOL happens to be.
        kind, rel_path = TOOL["workload"]
        script = str(LIBBPF_TOOLS_DIR / rel_path)
        if kind == "sudo_shell":
            workload_cmd = ["sudo", "-n", script]
        else:
            workload_cmd = [kind, script]
    else:
        workload_cmd = [
            FIO,
            "--name=bpf-eval-test",
            f"--filename={FIO_FILENAME}",
            f"--size={FIO_SIZE}",
            "--rw=randrw",
            "--rwmixread=50",
            "--bs=4k",
            "--direct=0",
            "--ioengine=sync",
            "--time_based",
            f"--runtime={FIO_RUNTIME}",
            "--numjobs=4",
            "--group_reporting",
        ]
    runner_cmd = ["sudo", "-n", str(RUNNER), *TOOL["args"](object_path)]

    workload_process = subprocess.Popen(
        workload_cmd,
        cwd=str(HEIMDALL_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )

    try:
        time.sleep(FIO_WARMUP_SECONDS)

        if workload_process.poll() is not None:
            workload_stdout, workload_stderr = workload_process.communicate(timeout=1)
            return (
                {"runtime_success": 0.0},
                {
                    "error": "workload exited before runner started",
                    "workload_cmd": " ".join(workload_cmd),
                    "workload_stdout": workload_stdout[-4000:],
                    "workload_stderr": workload_stderr[-4000:],
                    "workload_returncode": str(workload_process.returncode),
                },
            )

        runner_result = subprocess.run(
            runner_cmd,
            cwd=str(HEIMDALL_ROOT),
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            {"runtime_success": 0.0},
            {
                "error": f"runner timed out after {RUNNER_TIMEOUT}s",
                "workload_cmd": " ".join(workload_cmd),
                "runner_cmd": " ".join(runner_cmd),
                "runner_stdout": exc.stdout or "",
                "runner_stderr": exc.stderr or "",
            },
        )
    finally:
        _stop_process(workload_process)

    workload_stdout, workload_stderr = workload_process.communicate(timeout=5)
    runner_output = f"{runner_result.stdout}\n{runner_result.stderr}"
    runner_stats = _parse_runner_stats(runner_output)

    ns_values = [
        values["ns_per_run"]
        for values in runner_stats.values()
        if values.get("ns_per_run") is not None and values.get("run_cnt", 0) > 0
    ]
    avg_ns_per_run = float(sum(ns_values) / len(ns_values)) if ns_values else 0.0
    total_run_cnt = float(sum(values.get("run_cnt", 0) or 0 for values in runner_stats.values()))
    runtime_success = 1.0 if runner_result.returncode == 0 and ns_values else 0.0

    metrics = {
        "runtime_success": runtime_success,
        "avg_ns_per_run": avg_ns_per_run,
        "total_run_cnt": total_run_cnt,
    }
    if avg_ns_per_run > 0:
        metrics["runtime_score"] = 1.0 / (1.0 + avg_ns_per_run / 1000.0)
    else:
        metrics["runtime_score"] = 0.0

    artifacts = {
        "tool": BPF_TOOL,
        "workload_cmd": " ".join(workload_cmd),
        "runner_cmd": " ".join(runner_cmd),
        "runner_stdout": runner_result.stdout[-8000:],
        "runner_stderr": runner_result.stderr[-8000:],
        "runner_returncode": str(runner_result.returncode),
        "runner_stats": json.dumps(runner_stats, sort_keys=True),
        "workload_stdout": workload_stdout[-4000:],
        "workload_stderr": workload_stderr[-4000:],
        "workload_returncode": str(workload_process.returncode),
    }

    if runtime_success == 0.0:
        artifacts["error"] = "runner benchmark failed or produced no ns/run measurements"
        artifacts["runner_output"] = runner_output[-8000:]

    return metrics, artifacts


def evaluate(program_path: str) -> EvaluationResult:
    source_path = Path(program_path)
    source = source_path.read_text(encoding="utf-8", errors="replace")

    with tempfile.TemporaryDirectory(prefix="openevolve_bpf_") as tmp_dir:
        output_path = Path(tmp_dir) / f"{source_path.stem}.o"
        cmd = [
            CLANG,
            "-g",
            "-O2",
            "-target",
            "bpf",
            "-D__TARGET_ARCH_x86",
            "-I",
            str(LIBBPF_TOOLS_DIR),
            "-I",
            "/usr/include/x86_64-linux-gnu",
            "-c",
            str(source_path),
            "-o",
            str(output_path),
        ]

        try:
            result = subprocess.run(
                cmd,
                cwd=str(HEIMDALL_ROOT),
                capture_output=True,
                text=True,
                timeout=TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            timeout_metrics = {
                "score": 0.0,
                "combined_score": 0.0,
                "compile_success": 0.0,
                "semantic_equivalent": 0.0,
            }
            _save_candidate(source, timeout_metrics)
            return EvaluationResult(
                metrics=timeout_metrics,
                artifacts={
                    "error": f"clang timed out after {TIMEOUT_SECONDS}s",
                    "cmd": " ".join(cmd),
                    "stdout": exc.stdout or "",
                    "stderr": exc.stderr or "",
                },
            )

        compile_success = 1.0 if result.returncode == 0 and output_path.exists() else 0.0
        object_size = output_path.stat().st_size if output_path.exists() else 0

        metrics = {
            "score": compile_success,
            "combined_score": compile_success,
            "compile_success": compile_success,
            "object_bytes": float(object_size),
            # Default so this key exists even when compilation fails (below)
            # and _check_equivalence() never runs; overwritten with the real
            # result once the equivalence check actually runs.
            "semantic_equivalent": 0.0,
        }

        artifacts = {
            "tool": BPF_TOOL,
            "cmd": " ".join(cmd),
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-8000:],
            "returncode": str(result.returncode),
            "object_path": str(output_path) if output_path.exists() else "",
        }

        if compile_success == 0.0:
            artifacts["error"] = "clang compilation failed"
            _save_candidate(source, metrics)
            return EvaluationResult(metrics=metrics, artifacts=artifacts)

        if EQUIV_CHECK:
            equiv_metrics, equiv_artifacts = _check_equivalence(output_path)
            metrics.update(equiv_metrics)
            artifacts.update(equiv_artifacts)

        if RUN_BENCHMARK:
            runtime_metrics, runtime_artifacts = _run_workload_benchmark(output_path)
            metrics.update(runtime_metrics)
            artifacts.update(runtime_artifacts)
            metrics["score"] = (
                compile_success
                * metrics.get("runtime_success", 0.0)
                * (0.5 + 0.5 * metrics.get("runtime_score", 0.0))
            )
            metrics["combined_score"] = metrics["score"]

        _save_candidate(source, metrics)
        return EvaluationResult(metrics=metrics, artifacts=artifacts)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("usage: python evaluator.py <candidate.bpf.c>")

    evaluation = evaluate(sys.argv[1])
    print(json.dumps({"metrics": evaluation.metrics, "artifacts": evaluation.artifacts}, indent=2))
