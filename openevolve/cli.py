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

    print("\nPer-iteration ns/run:")
    for p in sorted(programs, key=lambda p: (getattr(p, "iteration_found", 0), p.timestamp)):
        it = getattr(p, "iteration_found", 0)
        m = p.metrics
        reason = None
        if m.get("compile_success", 0.0) != 1.0:
            reason = "compile failed"
        elif m.get("semantic_equivalent", 0.0) != 1.0:
            reason = "not equivalent"
        elif m.get("runtime_success", 0.0) != 1.0:
            reason = "benchmark failed"
        if reason is not None:
            print(f"  iter {it:>3}: N/A ({reason})")
        else:
            cells = "  ".join(
                f"{k[len('ns_per_run__'):]}={m[k]:.2f}" for k in fn_keys if k in m
            )
            print(f"  iter {it:>3}: {cells or 'N/A (no measurement)'}")


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
