"""
Command-line interface for OpenEvolve
"""

import argparse
import asyncio
import glob
import logging
import os
import sys
from typing import Dict, List, Optional

from openevolve import OpenEvolve
from openevolve.config import Config, load_config

logger = logging.getLogger(__name__)


def _print_per_iteration_ns_summary(openevolve: "OpenEvolve") -> None:
    """After a run, list every program's per-function ns/run (metrics keyed
    `ns_per_run__<fn>`, set by examples/bpf_compile/evaluator.py). A program
    that did not pass -- compile failure, or the symbolic equivalence check
    failed, or the benchmark itself failed -- is shown as N/A with the reason.
    No-op for runs whose evaluator never emits `ns_per_run__*` metrics.
    """
    try:
        programs = list(openevolve.database.programs.values())
    except Exception:
        return
    fn_keys = sorted(
        {k for p in programs for k in p.metrics if str(k).startswith("ns_per_run__")}
    )
    if not fn_keys:
        return

    def _row(label: str, m: dict) -> str:
        reason = None
        if m.get("compile_success", 0.0) != 1.0:
            reason = "compile failed"
        elif m.get("semantic_equivalent", 0.0) != 1.0:
            reason = "not equivalent"
        elif m.get("runtime_success", 0.0) != 1.0:
            reason = "benchmark failed"
        if reason is not None:
            return f"  {label}: N/A ({reason})"
        cells = "  ".join(
            f"{k[len('ns_per_run__'):]}={m[k]:.2f}" for k in fn_keys if k in m
        )
        return f"  {label}: {cells or 'N/A (no measurement)'}"

    print("\nPer-iteration ns/run:")
    # iter 0 = the original program. The database tracks its id directly
    # (openevolve.database.initial_program_id) so it's found even if it has
    # since been displaced from every MAP-Elites cell/island; fall back to the
    # old parent-id-is-None heuristic for checkpoints saved before that field
    # existed.
    seen = set()
    initial_id = getattr(openevolve.database, "initial_program_id", None)
    root = openevolve.database.programs.get(initial_id) if initial_id else None
    if root is None:
        roots = [p for p in programs if not getattr(p, "parent_id", None)]
        root = min(roots, key=lambda p: p.timestamp) if roots else None
    if root:
        seen.add(root.id)
        print(_row("iter   0 (original)", root.metrics))
    else:
        print("  iter   0 (original): unavailable (initial program not retained)")
    for p in sorted(
        (p for p in programs if p.id not in seen),
        key=lambda p: (getattr(p, "iteration_found", 0), p.timestamp),
    ):
        print(_row(f"iter {getattr(p, 'iteration_found', 0):>3}", p.metrics))


def _summarize_step(program) -> List[str]:
    """One or more human-readable "what changed" lines for a single program,
    best-effort across however this run was configured:
      1. Approved witnesses (interactive mode) - each one's own summary, since
         that's the most precise record of what was actually implemented (a
         proposal can include changes that were NOT approved).
      2. A diff-based changes_summary, when it's not the no-op "Full rewrite"
         placeholder full-rewrite mode always uses.
      3. The first line of the LLM's free-text proposal explanation, truncated.
    Returns an empty list if nothing usable is recorded for this program.
    """
    metadata = getattr(program, "metadata", None) or {}

    witnesses = metadata.get("witnesses") or []
    approved = [w for w in witnesses if w.get("developer_approved") is True]
    if approved:
        return [w.get("summary") or "(approved change, no summary recorded)" for w in approved]

    changes = metadata.get("changes")
    if changes and changes != "Full rewrite":
        return [changes]

    explanation = (metadata.get("explanation") or "").strip()
    if explanation:
        first_line = explanation.splitlines()[0].strip()
        if first_line:
            return [first_line[:200]]

    return []


def _print_optimization_lineage(openevolve: "OpenEvolve", best_program) -> None:
    """After a run, print the chain of changes from the earliest still-known
    ancestor down to the best program found, so the summary answers "what did
    the best result actually DO" and not just its metrics.

    Best-effort: an ancestor that was pruned from the population before the run
    ended (database.py protects the initial/best/Pareto-front programs, but not
    every intermediate ancestor) breaks the chain there - this says so rather
    than silently presenting a partial history as if it were complete.
    """
    try:
        programs = openevolve.database.programs
    except Exception:
        return

    chain = []
    seen_ids = set()
    node = best_program
    truncated = False
    while node is not None:
        if node.id in seen_ids:
            break  # defensive: never loop forever on a corrupt parent chain
        chain.append(node)
        seen_ids.add(node.id)
        parent_id = getattr(node, "parent_id", None)
        if not parent_id:
            node = None
        elif parent_id in programs:
            node = programs[parent_id]
        else:
            truncated = True
            node = None
    chain.reverse()  # earliest known ancestor -> best program

    if len(chain) <= 1:
        return  # best program IS the earliest known ancestor - nothing to narrate

    print(
        f"\nBest program's optimization path "
        f"({len(chain) - 1} step(s) from its earliest known ancestor):"
    )
    if truncated:
        print(
            "  (an earlier ancestor was pruned during the run; history starts "
            "here, not necessarily from iter 0)"
        )
    for program in chain[1:]:
        label = f"iter {getattr(program, 'iteration_found', '?')}"
        steps = _summarize_step(program)
        if not steps:
            print(f"  {label}: (no recorded description)")
        elif len(steps) == 1:
            print(f"  {label}: {steps[0]}")
        else:
            print(f"  {label}:")
            for step in steps:
                print(f"    - {step}")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments"""
    parser = argparse.ArgumentParser(description="OpenEvolve - Evolutionary coding agent")

    parser.add_argument("initial_program", help="Path to the initial program file")

    parser.add_argument(
        "evaluation_file", help="Path to the evaluation file containing an 'evaluate' function"
    )

    parser.add_argument("--config", "-c", help="Path to configuration file (YAML)", default=None)

    parser.add_argument("--output", "-o", help="Output directory for results", default=None)

    parser.add_argument(
        "--iterations", "-i", help="Maximum number of iterations", type=int, default=None
    )

    parser.add_argument(
        "--target-score", "-t", help="Target score to reach", type=float, default=None
    )

    parser.add_argument(
        "--log-level",
        "-l",
        help="Logging level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default=None,
    )

    parser.add_argument(
        "--checkpoint",
        help="Path to checkpoint directory to resume from (e.g., openevolve_output/checkpoints/checkpoint_50)",
        default=None,
    )

    parser.add_argument("--api-base", help="Base URL for the LLM API", default=None)

    parser.add_argument("--primary-model", help="Primary LLM model name", default=None)

    parser.add_argument("--secondary-model", help="Secondary LLM model name", default=None)

    return parser.parse_args()


async def main_async() -> int:
    """
    Main asynchronous entry point

    Returns:
        Exit code
    """
    args = parse_args()

    # Check if files exist
    if not os.path.exists(args.initial_program):
        print(f"Error: Initial program file '{args.initial_program}' not found")
        return 1

    if not os.path.exists(args.evaluation_file):
        print(f"Error: Evaluation file '{args.evaluation_file}' not found")
        return 1

    # Load base config from file or defaults
    config = load_config(args.config)

    # Create config object with command-line overrides
    if args.api_base or args.primary_model or args.secondary_model:
        # Apply command-line overrides
        if args.api_base:
            config.llm.api_base = args.api_base
            print(f"Using API base: {config.llm.api_base}")

        if args.primary_model:
            config.llm.primary_model = args.primary_model
            print(f"Using primary model: {config.llm.primary_model}")

        if args.secondary_model:
            config.llm.secondary_model = args.secondary_model
            print(f"Using secondary model: {config.llm.secondary_model}")

        # Rebuild models list to apply CLI overrides
        if args.primary_model or args.secondary_model:
            config.llm.rebuild_models()
            print(f"Applied CLI model overrides - active models:")
            for i, model in enumerate(config.llm.models):
                print(f"  Model {i+1}: {model.name} (weight: {model.weight})")

    # Initialize OpenEvolve
    try:
        openevolve = OpenEvolve(
            initial_program_path=args.initial_program,
            evaluation_file=args.evaluation_file,
            config=config,
            output_dir=args.output,
        )

        # Load from checkpoint if specified
        if args.checkpoint:
            if not os.path.exists(args.checkpoint):
                print(f"Error: Checkpoint directory '{args.checkpoint}' not found")
                return 1
            print(f"Loading checkpoint from {args.checkpoint}")
            openevolve.database.load(args.checkpoint)
            print(
                f"Checkpoint loaded successfully (iteration {openevolve.database.last_iteration})"
            )

        # Override log level if specified
        if args.log_level:
            logging.getLogger().setLevel(getattr(logging, args.log_level))

        # Run evolution
        best_program = await openevolve.run(
            iterations=args.iterations,
            target_score=args.target_score,
            checkpoint_path=args.checkpoint,
        )

        # Get the checkpoint path saved by this run (not just the highest-numbered
        # checkpoint directory on disk, which may be left over from a previous run
        # that reused the same output directory)
        checkpoint_dir = os.path.join(openevolve.output_dir, "checkpoints")
        latest_checkpoint = None
        if openevolve.last_checkpoint_iteration is not None:
            matches = glob.glob(
                os.path.join(checkpoint_dir, f"checkpoint_*_{openevolve.last_checkpoint_iteration}")
            )
            if matches:
                latest_checkpoint = max(matches, key=os.path.getmtime)

        if latest_checkpoint is None and os.path.exists(checkpoint_dir):
            checkpoints = [
                os.path.join(checkpoint_dir, d)
                for d in os.listdir(checkpoint_dir)
                if os.path.isdir(os.path.join(checkpoint_dir, d))
            ]
            if checkpoints:
                latest_checkpoint = sorted(
                    checkpoints, key=lambda x: int(x.split("_")[-1]) if "_" in x else 0
                )[-1]

        print(f"\nEvolution complete!")
        print(f"Best program metrics:")
        for name, value in best_program.metrics.items():
            # Handle mixed types: format numbers as floats, others as strings
            if isinstance(value, (int, float)):
                print(f"  {name}: {value:.4f}")
            else:
                print(f"  {name}: {value}")

        _print_per_iteration_ns_summary(openevolve)
        _print_optimization_lineage(openevolve, best_program)

        if latest_checkpoint:
            print(f"\nLatest checkpoint saved at: {latest_checkpoint}")
            print(f"To resume, use: --checkpoint {latest_checkpoint}")

        return 0

    except Exception as e:
        print(f"Error: {str(e)}")
        import traceback

        traceback.print_exc()
        return 1


def main() -> int:
    """
    Main entry point

    Returns:
        Exit code
    """
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
