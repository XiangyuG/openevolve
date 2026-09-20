"""
Process-based parallel controller for true parallelism
"""

import asyncio
import json
import logging
import multiprocessing as mp
import pickle
import signal
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openevolve.config import Config
from openevolve.database import Program, ProgramDatabase
from openevolve.review_gate import ReviewDecision
from openevolve.utils.metrics_utils import safe_numeric_average

logger = logging.getLogger(__name__)


@dataclass
class SerializableResult:
    """Result that can be pickled and sent between processes"""

    child_program_dict: Optional[Dict[str, Any]] = None
    parent_id: Optional[str] = None
    iteration_time: float = 0.0
    prompt: Optional[Dict[str, str]] = None
    llm_response: Optional[str] = None
    artifacts: Optional[Dict[str, Any]] = None
    iteration: int = 0
    error: Optional[str] = None
    target_island: Optional[int] = None  # Island where child should be placed
    # Usage for THIS iteration's one LLM call, if the sampled model reported one
    # (see OpenAILLM.last_usage) - None for manual mode or a non-reporting
    # provider. Summed across iterations by the caller for a run total.
    token_usage: Optional[Dict[str, int]] = None


@dataclass
class ProposalResult:
    """
    Result of phase 1 (propose) of the interactive two-phase flow (see
    _run_evolution_interactive / _run_iteration_worker_propose below): a set of
    transformation witnesses proposed by the LLM, with no code generated yet.
    """

    parent_id: Optional[str] = None
    inspiration_ids: List[str] = field(default_factory=list)
    iteration: int = 0
    prompt: Optional[Dict[str, str]] = None
    llm_response: Optional[str] = None
    explanation: str = ""
    witnesses: List[Dict[str, Any]] = field(default_factory=list)
    target_island: Optional[int] = None
    error: Optional[str] = None
    token_usage: Optional[Dict[str, int]] = None


_WIT_FENCE_RE = None  # compiled lazily


def _c2rust_dir() -> "Path":
    """heimdall/c2rust_translation, from $HEIMDALL_ROOT or the sibling checkout."""
    import os

    root = os.environ.get("HEIMDALL_ROOT")
    if root:
        return Path(root) / "c2rust_translation"
    return Path(__file__).resolve().parents[2] / "c2rust_translation"


def _write_and_check_wit(text: str, wit_path: "Path") -> Dict[str, Any]:
    """Write `text` to `wit_path` and syntax-check it with
    `python3 -m witness_dsl` (grammar: c2rust_translation/witness_dsl/GRAMMAR.bnf).
    Returns {"path", "ok", "output"}; ok is None when the checker could not be
    run. Never raises."""
    import subprocess
    import sys

    # Resolve to an absolute path FIRST. The checker subprocess below runs
    # with cwd=_c2rust_dir() (so `import witness_dsl` works), not this
    # process's cwd -- if `wit_path` were left relative (e.g. the default
    # BPF_SAVE_DIR="generated_programs/<tool>"), the write lands relative to
    # here but the checker would then look for it relative to
    # _c2rust_dir() instead and report "No such file or directory" even
    # though the file was just written successfully.
    wit_path = Path(wit_path).resolve()
    try:
        wit_path.parent.mkdir(parents=True, exist_ok=True)
        wit_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
    except OSError as e:
        return {"path": None, "ok": None, "output": f"could not write .wit: {e}"}
    try:
        r = subprocess.run(
            [sys.executable, "-m", "witness_dsl", str(wit_path)],
            cwd=str(_c2rust_dir()),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return {
            "path": str(wit_path),
            "ok": r.returncode == 0,
            "output": (r.stdout + r.stderr).strip(),
        }
    except Exception as e:  # checker missing, timeout, ...
        return {"path": str(wit_path), "ok": None, "output": f"witness_dsl not run: {e}"}


def _save_and_check_wit(llm_response: str, program_id: str) -> Optional[Dict[str, Any]]:
    """Pull the ```witness DSL block out of an LLM response, write it to
    <BPF_SAVE_DIR>/<tool>/witnesses/<program_id>.wit, and syntax-check it.

    Returns {"path", "ok", "output"} -- or None if the response has no
    ```witness block. Best-effort: never raises.
    """
    import os
    import re

    global _WIT_FENCE_RE
    if _WIT_FENCE_RE is None:
        _WIT_FENCE_RE = re.compile(
            r"```[ \t]*witness[ \t]*\r?\n(.*?)\r?\n?```", re.DOTALL | re.IGNORECASE
        )
    m = _WIT_FENCE_RE.search(llm_response or "")
    if not m:
        return None

    save_root = os.environ.get("BPF_SAVE_DIR") or os.path.join(
        "generated_programs", os.environ.get("BPF_TOOL", "filetop")
    )
    return _write_and_check_wit(
        m.group(1).strip() + "\n", Path(save_root) / "witnesses" / f"{program_id}.wit"
    )


def _worker_init(config_dict: dict, evaluation_file: str, parent_env: dict = None) -> None:
    """Initialize worker process with necessary components"""
    import os

    # Set environment from parent process
    if parent_env:
        os.environ.update(parent_env)

    global _worker_config
    global _worker_evaluation_file
    global _worker_evaluator
    global _worker_llm_ensemble
    global _worker_prompt_sampler

    # Store config for later use
    # Reconstruct Config object from nested dictionaries
    from openevolve.config import (
        Config,
        DatabaseConfig,
        EvaluatorConfig,
        LLMConfig,
        LLMModelConfig,
        PromptConfig,
    )

    # Reconstruct model objects
    models = [LLMModelConfig(**m) for m in config_dict["llm"]["models"]]
    evaluator_models = [LLMModelConfig(**m) for m in config_dict["llm"]["evaluator_models"]]

    # Create LLM config with models
    llm_dict = config_dict["llm"].copy()
    llm_dict["models"] = models
    llm_dict["evaluator_models"] = evaluator_models
    llm_config = LLMConfig(**llm_dict)

    # Create other configs
    prompt_config = PromptConfig(**config_dict["prompt"])
    database_config = DatabaseConfig(**config_dict["database"])
    evaluator_config = EvaluatorConfig(**config_dict["evaluator"])

    _worker_config = Config(
        llm=llm_config,
        prompt=prompt_config,
        database=database_config,
        evaluator=evaluator_config,
        **{
            k: v
            for k, v in config_dict.items()
            if k not in ["llm", "prompt", "database", "evaluator"]
        },
    )
    _worker_evaluation_file = evaluation_file

    # These will be lazily initialized on first use
    _worker_evaluator = None
    _worker_llm_ensemble = None
    _worker_prompt_sampler = None


def _lazy_init_worker_components():
    """Lazily initialize expensive components on first use"""
    global _worker_evaluator
    global _worker_llm_ensemble
    global _worker_prompt_sampler

    if _worker_llm_ensemble is None:
        from openevolve.llm.ensemble import LLMEnsemble

        _worker_llm_ensemble = LLMEnsemble(_worker_config.llm.models)

    if _worker_prompt_sampler is None:
        from openevolve.prompt.sampler import PromptSampler

        _worker_prompt_sampler = PromptSampler(_worker_config.prompt)

    if _worker_evaluator is None:
        from openevolve.evaluator import Evaluator
        from openevolve.llm.ensemble import LLMEnsemble
        from openevolve.prompt.sampler import PromptSampler

        # Create evaluator-specific components
        evaluator_llm = LLMEnsemble(_worker_config.llm.evaluator_models)
        evaluator_prompt = PromptSampler(_worker_config.prompt)
        evaluator_prompt.set_templates("evaluator_system_message")

        _worker_evaluator = Evaluator(
            _worker_config.evaluator,
            _worker_evaluation_file,
            evaluator_llm,
            evaluator_prompt,
            database=None,  # No shared database in worker
            suffix=getattr(_worker_config, "file_suffix", ".py"),
        )


def _run_iteration_worker(
    iteration: int, db_snapshot: Dict[str, Any], parent_id: str, inspiration_ids: List[str]
) -> SerializableResult:
    """Run a single iteration in a worker process"""
    try:
        # Lazy initialization
        _lazy_init_worker_components()

        # Reconstruct programs from snapshot
        programs = {pid: Program(**prog_dict) for pid, prog_dict in db_snapshot["programs"].items()}

        parent = programs[parent_id]
        inspirations = [programs[pid] for pid in inspiration_ids if pid in programs]

        # Get parent artifacts if available
        parent_artifacts = db_snapshot["artifacts"].get(parent_id)

        # Get developer feedback from a previously rejected attempt on this parent, if any
        # (interactive mode only; see ReviewGate / _run_evolution_interactive)
        developer_feedback = db_snapshot.get("developer_feedback", {}).get(parent_id)

        # Get island-specific programs for context
        parent_island = parent.metadata.get("island", db_snapshot["current_island"])
        island_programs = [
            programs[pid] for pid in db_snapshot["islands"][parent_island] if pid in programs
        ]

        # Sort by metrics for top programs
        island_programs.sort(
            key=lambda p: p.metrics.get("combined_score", safe_numeric_average(p.metrics)),
            reverse=True,
        )

        # Use config values for limits instead of hardcoding
        # Programs for LLM display (includes both top and diverse for inspiration)
        programs_for_prompt = island_programs[
            : _worker_config.prompt.num_top_programs + _worker_config.prompt.num_diverse_programs
        ]
        # Best programs only (for previous attempts section, focused on top performers)
        best_programs_only = island_programs[: _worker_config.prompt.num_top_programs]

        # Build prompt
        if _worker_config.prompt.programs_as_changes_description:
            parent_changes_desc = (
                parent.changes_description or _worker_config.prompt.initial_changes_description
            )
            child_changes_desc = parent_changes_desc
        else:
            parent_changes_desc = None
            child_changes_desc = None

        prompt = _worker_prompt_sampler.build_prompt(
            current_program=parent.code,
            parent_program=parent.code,
            program_metrics=parent.metrics,
            previous_programs=[p.to_dict() for p in best_programs_only],
            top_programs=[p.to_dict() for p in programs_for_prompt],
            inspirations=[p.to_dict() for p in inspirations],
            language=_worker_config.language,
            evolution_round=iteration,
            diff_based_evolution=_worker_config.diff_based_evolution,
            program_artifacts=parent_artifacts,
            feature_dimensions=db_snapshot.get("feature_dimensions", []),
            current_changes_description=parent_changes_desc,
            developer_feedback=developer_feedback,
        )

        iteration_start = time.time()

        # Generate code modification (sync wrapper for async)
        try:
            llm_response = asyncio.run(
                _worker_llm_ensemble.generate_with_context(
                    system_message=prompt["system"],
                    messages=[{"role": "user", "content": prompt["user"]}],
                )
            )
        except Exception as e:
            logger.error(f"LLM generation failed: {e}")
            return SerializableResult(error=f"LLM generation failed: {str(e)}", iteration=iteration)
        llm_token_usage = _worker_llm_ensemble.last_usage

        # Check for None response
        if llm_response is None:
            return SerializableResult(
                error="LLM returned None response", iteration=iteration, token_usage=llm_token_usage
            )

        # Parse response based on evolution mode
        if _worker_config.diff_based_evolution:
            from openevolve.utils.code_utils import (
                apply_diff,
                apply_diff_blocks,
                extract_diffs,
                format_diff_summary,
                split_diffs_by_target,
            )

            diff_blocks = extract_diffs(llm_response, _worker_config.diff_pattern)
            if not diff_blocks:
                return SerializableResult(
                    error="No valid diffs found in response",
                    iteration=iteration,
                    token_usage=llm_token_usage,
                )

            if _worker_config.prompt.programs_as_changes_description:
                try:
                    code_blocks, desc_blocks, _unmatched = split_diffs_by_target(
                        diff_blocks,
                        code_text=parent.code,
                        changes_description_text=parent_changes_desc,
                    )
                except Exception as e:
                    return SerializableResult(
                        error=str(e), iteration=iteration, token_usage=llm_token_usage
                    )

                child_code, _ = apply_diff_blocks(parent.code, code_blocks)
                child_changes_desc, desc_applied = apply_diff_blocks(
                    parent_changes_desc, desc_blocks
                )

                # Must update the previous changes description
                if (
                    desc_applied == 0
                    or not child_changes_desc.strip()
                    or child_changes_desc.strip() == parent_changes_desc.strip()
                ):
                    return SerializableResult(
                        error="changes_description was not updated or empty, program is discarded",
                        iteration=iteration,
                        token_usage=llm_token_usage,
                    )

                changes_summary = format_diff_summary(
                    code_blocks,
                    max_line_len=_worker_config.prompt.diff_summary_max_line_len,
                    max_lines=_worker_config.prompt.diff_summary_max_lines,
                )
            else:
                # All diffs applied only to code
                child_code = apply_diff(parent.code, llm_response, _worker_config.diff_pattern)
                changes_summary = format_diff_summary(
                    diff_blocks,
                    max_line_len=_worker_config.prompt.diff_summary_max_line_len,
                    max_lines=_worker_config.prompt.diff_summary_max_lines,
                )
        else:
            from openevolve.utils.code_utils import parse_full_rewrite

            new_code = parse_full_rewrite(llm_response, _worker_config.language)
            if not new_code:
                return SerializableResult(
                    error=f"No valid code found in response",
                    iteration=iteration,
                    token_usage=llm_token_usage,
                )

            child_code = new_code
            changes_summary = "Full rewrite"

        from openevolve.utils.code_utils import (
            extract_change_explanation,
            extract_transformation_witnesses,
            validate_transformation_proof,
        )

        # Extract witnesses from the RAW response, before any code-fence stripping --
        # extract_change_explanation() indiscriminately deletes every ``` ... ``` block
        # (meant for a full program's code fence), which would silently eat the
        # required ```witness DSL fence too, leaving every witness's "wit" empty even
        # when the model wrote it correctly.
        change_witnesses = extract_transformation_witnesses(llm_response)
        change_explanation = extract_change_explanation(llm_response, _worker_config.diff_pattern)
        for index, witness in enumerate(change_witnesses):
            # Stable per-child index so the developer can approve/reject witnesses
            # individually in the review UI (openevolve/review_gate.py) and have
            # that decision line back up to this exact witness later.
            witness["index"] = index
            witness["proof"] = validate_transformation_proof(
                witness["pre_formula"], witness["post_formula"]
            )

        # Check code length
        if len(child_code) > _worker_config.max_code_length:
            return SerializableResult(
                error=f"Generated code exceeds maximum length ({len(child_code)} > {_worker_config.max_code_length})",
                iteration=iteration,
                token_usage=llm_token_usage,
            )

        # Evaluate the child program
        import uuid

        child_id = str(uuid.uuid4())
        child_metrics = asyncio.run(
            _worker_evaluator.evaluate_program(child_code, child_id, iteration=iteration)
        )

        # Get artifacts
        artifacts = _worker_evaluator.get_pending_artifacts(child_id)

        # Save the LLM's transformation-witness DSL block as a .wit file and
        # syntax-check it against GRAMMAR.bnf (informational -- the actual
        # --witness file is still built elsewhere).
        wit_check = _save_and_check_wit(llm_response, child_id)
        if wit_check is None:
            logger.info("Iteration %d: no ```witness block in the LLM response", iteration)
        else:
            logger.info(
                "Iteration %d: wrote %s, witness_dsl syntax ok=%s",
                iteration,
                wit_check.get("path"),
                wit_check.get("ok"),
            )
            if wit_check.get("ok") is not True and wit_check.get("output"):
                logger.info("witness_dsl: %s", wit_check["output"])
                # Feed the diagnostic back: it becomes an artifact on this child,
                # so the next prompt sampled from this lineage shows the error
                # and the model can correct the witness. Non-blocking -- the
                # child is still scored on its code.
                if artifacts is None:
                    artifacts = {}
                artifacts["witness_dsl_syntax_error"] = (
                    "The ```witness block in your last response did NOT conform to "
                    "the transformation-witness DSL grammar (GRAMMAR.bnf). Produce "
                    "a corrected ```witness block this time; the code change itself "
                    "was fine. Checker output:\n" + str(wit_check.get("output") or "")
                )

        # Create child program
        child_program = Program(
            id=child_id,
            code=child_code,
            changes_description=child_changes_desc,
            language=_worker_config.language,
            parent_id=parent.id,
            generation=parent.generation + 1,
            metrics=child_metrics,
            iteration_found=iteration,
            metadata={
                "changes": changes_summary,
                "explanation": change_explanation,
                "witnesses": change_witnesses,
                "wit": wit_check,
                "parent_metrics": parent.metrics,
                "island": parent_island,
            },
        )

        iteration_time = time.time() - iteration_start

        # Get target island from snapshot (where child should be placed)
        target_island = db_snapshot.get("sampling_island")

        return SerializableResult(
            child_program_dict=child_program.to_dict(),
            parent_id=parent.id,
            iteration_time=iteration_time,
            prompt=prompt,
            llm_response=llm_response,
            artifacts=artifacts,
            iteration=iteration,
            target_island=target_island,
            token_usage=llm_token_usage,
        )

    except Exception as e:
        logger.exception(f"Error in worker iteration {iteration}")
        return SerializableResult(error=str(e), iteration=iteration)


def _combine_feedback(
    known_compile_failures: List[str], parent_feedback: Optional[str]
) -> Optional[str]:
    """Merge the run-wide known-compile-failures list with this parent's own
    specific feedback (a rejection reason, or its own last compile error) into
    the single string the propose prompt's {developer_feedback} section shows.
    The global list comes first so it reads as standing context ("these don't
    work, anywhere, this run") ahead of anything specific to this one parent."""
    parts = []
    if known_compile_failures:
        parts.append(
            "Known compile-time errors already hit this run (from other lineages "
            "too) - avoid triggering these again:\n"
            + "\n".join(f"- {e}" for e in known_compile_failures)
        )
    if parent_feedback:
        parts.append(parent_feedback)
    return "\n\n".join(parts) if parts else None


def _build_worker_prompt(
    iteration: int,
    db_snapshot: Dict[str, Any],
    parent_id: str,
    inspiration_ids: List[str],
    template_key: str,
    extra_kwargs: Optional[Dict[str, Any]] = None,
) -> Tuple[Dict[str, str], Program, int, Optional[str]]:
    """
    Build a prompt for one phase of the interactive two-phase flow
    (_run_iteration_worker_propose / _run_iteration_worker_implement below).

    This reproduces the parent/inspiration/evolution-history context assembly that
    _run_iteration_worker (the non-interactive worker) does inline -- duplicated rather
    than shared so that function, and the tests exercising it, are never touched by the
    interactive two-phase flow.

    Returns: (prompt, parent, parent_island, parent_changes_desc)
    """
    programs = {pid: Program(**prog_dict) for pid, prog_dict in db_snapshot["programs"].items()}
    parent = programs[parent_id]
    inspirations = [programs[pid] for pid in inspiration_ids if pid in programs]

    parent_artifacts = db_snapshot["artifacts"].get(parent_id)
    developer_feedback = _combine_feedback(
        db_snapshot.get("known_compile_failures") or [],
        db_snapshot.get("developer_feedback", {}).get(parent_id),
    )

    parent_island = parent.metadata.get("island", db_snapshot["current_island"])
    island_programs = [
        programs[pid] for pid in db_snapshot["islands"][parent_island] if pid in programs
    ]
    island_programs.sort(
        key=lambda p: p.metrics.get("combined_score", safe_numeric_average(p.metrics)),
        reverse=True,
    )

    programs_for_prompt = island_programs[
        : _worker_config.prompt.num_top_programs + _worker_config.prompt.num_diverse_programs
    ]
    best_programs_only = island_programs[: _worker_config.prompt.num_top_programs]

    if _worker_config.prompt.programs_as_changes_description:
        parent_changes_desc = (
            parent.changes_description or _worker_config.prompt.initial_changes_description
        )
    else:
        parent_changes_desc = None

    prompt = _worker_prompt_sampler.build_prompt(
        current_program=parent.code,
        parent_program=parent.code,
        program_metrics=parent.metrics,
        previous_programs=[p.to_dict() for p in best_programs_only],
        top_programs=[p.to_dict() for p in programs_for_prompt],
        inspirations=[p.to_dict() for p in inspirations],
        language=_worker_config.language,
        evolution_round=iteration,
        diff_based_evolution=_worker_config.diff_based_evolution,
        program_artifacts=parent_artifacts,
        feature_dimensions=db_snapshot.get("feature_dimensions", []),
        current_changes_description=parent_changes_desc,
        developer_feedback=developer_feedback,
        template_key=template_key,
        **(extra_kwargs or {}),
    )

    return prompt, parent, parent_island, parent_changes_desc


def _run_iteration_worker_propose(
    iteration: int, db_snapshot: Dict[str, Any], parent_id: str, inspiration_ids: List[str]
) -> ProposalResult:
    """
    Phase 1 of the interactive two-phase flow (see ProcessParallelController.
    _run_evolution_interactive): ask the LLM to propose transformation witnesses ONLY,
    with no code. The developer reviews and approves/rejects each witness individually
    (openevolve/review_gate.py's request_witness_review) before any code is generated --
    see _run_iteration_worker_implement for phase 2.
    """
    try:
        _lazy_init_worker_components()

        template_key = (
            "diff_user_propose"
            if _worker_config.diff_based_evolution
            else "full_rewrite_user_propose"
        )
        prompt, parent, _parent_island, _parent_changes_desc = _build_worker_prompt(
            iteration, db_snapshot, parent_id, inspiration_ids, template_key
        )

        try:
            llm_response = asyncio.run(
                _worker_llm_ensemble.generate_with_context(
                    system_message=prompt["system"],
                    messages=[{"role": "user", "content": prompt["user"]}],
                )
            )
        except Exception as e:
            logger.error(f"LLM generation failed (propose): {e}")
            return ProposalResult(
                error=f"LLM generation failed: {str(e)}", iteration=iteration, parent_id=parent_id
            )
        llm_token_usage = _worker_llm_ensemble.last_usage

        if llm_response is None:
            return ProposalResult(
                error="LLM returned None response",
                iteration=iteration,
                parent_id=parent_id,
                token_usage=llm_token_usage,
            )

        from openevolve.utils.code_utils import (
            extract_change_explanation,
            extract_transformation_witnesses,
            validate_transformation_proof,
        )

        # Extract witnesses from the RAW response, before any code-fence stripping --
        # extract_change_explanation() indiscriminately deletes every ``` ... ``` block
        # (meant for a full program's code fence in the non-interactive path), which
        # would silently eat the required ```witness DSL fence too, leaving every
        # witness's "wit" empty even when the model wrote it correctly.
        witnesses = extract_transformation_witnesses(llm_response)
        for index, witness in enumerate(witnesses):
            witness["index"] = index
            witness["proof"] = validate_transformation_proof(
                witness["pre_formula"], witness["post_formula"]
            )

        # The propose prompt asks for no code, but strip any diff/code fences the model
        # might still emit anyway, same defensive extraction as the non-interactive path --
        # safe to run AFTER witness extraction since each witness's "wit" text is already
        # captured above, and the review UI renders it separately (see review.js).
        explanation = extract_change_explanation(llm_response, _worker_config.diff_pattern)

        return ProposalResult(
            parent_id=parent.id,
            inspiration_ids=inspiration_ids,
            iteration=iteration,
            prompt=prompt,
            llm_response=llm_response,
            explanation=explanation,
            witnesses=witnesses,
            target_island=db_snapshot.get("sampling_island"),
            token_usage=llm_token_usage,
        )

    except Exception as e:
        logger.exception(f"Error in worker iteration {iteration} (propose)")
        return ProposalResult(error=str(e), iteration=iteration, parent_id=parent_id)


def _run_iteration_worker_implement(
    iteration: int,
    db_snapshot: Dict[str, Any],
    parent_id: str,
    inspiration_ids: List[str],
    explanation: str,
    witnesses: List[Dict[str, Any]],
    developer_notes: str,
) -> SerializableResult:
    """
    Phase 2 of the interactive two-phase flow: given the phase-1 witnesses (already
    stamped with the developer's per-witness "developer_approved" call, see
    ProcessParallelController._run_evolution_interactive), ask the LLM to implement ONLY
    the approved ones, then evaluate the resulting program exactly like the
    non-interactive path (_run_iteration_worker) does.
    """
    try:
        _lazy_init_worker_components()

        from openevolve.utils.code_utils import format_witness_decisions_for_prompt

        template_key = (
            "diff_user_implement"
            if _worker_config.diff_based_evolution
            else "full_rewrite_user_implement"
        )
        approved_changes_section = format_witness_decisions_for_prompt(witnesses)
        prompt, parent, parent_island, parent_changes_desc = _build_worker_prompt(
            iteration,
            db_snapshot,
            parent_id,
            inspiration_ids,
            template_key,
            extra_kwargs={
                "approved_changes_section": approved_changes_section or "(none)",
                "developer_notes": developer_notes or "(none)",
            },
        )

        iteration_start = time.time()

        try:
            llm_response = asyncio.run(
                _worker_llm_ensemble.generate_with_context(
                    system_message=prompt["system"],
                    messages=[{"role": "user", "content": prompt["user"]}],
                )
            )
        except Exception as e:
            logger.error(f"LLM generation failed (implement): {e}")
            return SerializableResult(error=f"LLM generation failed: {str(e)}", iteration=iteration)
        llm_token_usage = _worker_llm_ensemble.last_usage

        if llm_response is None:
            return SerializableResult(
                error="LLM returned None response", iteration=iteration, token_usage=llm_token_usage
            )

        if _worker_config.diff_based_evolution:
            from openevolve.utils.code_utils import (
                apply_diff,
                apply_diff_blocks,
                extract_diffs,
                format_diff_summary,
                split_diffs_by_target,
            )

            diff_blocks = extract_diffs(llm_response, _worker_config.diff_pattern)
            if not diff_blocks:
                return SerializableResult(
                    error="No valid diffs found in response",
                    iteration=iteration,
                    token_usage=llm_token_usage,
                )

            if _worker_config.prompt.programs_as_changes_description:
                try:
                    code_blocks, desc_blocks, _unmatched = split_diffs_by_target(
                        diff_blocks,
                        code_text=parent.code,
                        changes_description_text=parent_changes_desc,
                    )
                except Exception as e:
                    return SerializableResult(
                        error=str(e), iteration=iteration, token_usage=llm_token_usage
                    )

                child_code, _ = apply_diff_blocks(parent.code, code_blocks)
                child_changes_desc, desc_applied = apply_diff_blocks(
                    parent_changes_desc, desc_blocks
                )

                if (
                    desc_applied == 0
                    or not child_changes_desc.strip()
                    or child_changes_desc.strip() == parent_changes_desc.strip()
                ):
                    return SerializableResult(
                        error="changes_description was not updated or empty, program is discarded",
                        iteration=iteration,
                        token_usage=llm_token_usage,
                    )

                changes_summary = format_diff_summary(
                    code_blocks,
                    max_line_len=_worker_config.prompt.diff_summary_max_line_len,
                    max_lines=_worker_config.prompt.diff_summary_max_lines,
                )
            else:
                child_code = apply_diff(parent.code, llm_response, _worker_config.diff_pattern)
                child_changes_desc = None
                changes_summary = format_diff_summary(
                    diff_blocks,
                    max_line_len=_worker_config.prompt.diff_summary_max_line_len,
                    max_lines=_worker_config.prompt.diff_summary_max_lines,
                )
        else:
            from openevolve.utils.code_utils import parse_full_rewrite

            new_code = parse_full_rewrite(llm_response, _worker_config.language)
            if not new_code:
                return SerializableResult(
                    error="No valid code found in response",
                    iteration=iteration,
                    token_usage=llm_token_usage,
                )

            child_code = new_code
            child_changes_desc = None
            changes_summary = "Full rewrite"

        if len(child_code) > _worker_config.max_code_length:
            return SerializableResult(
                error=f"Generated code exceeds maximum length ({len(child_code)} > {_worker_config.max_code_length})",
                iteration=iteration,
                token_usage=llm_token_usage,
            )

        import uuid

        child_id = str(uuid.uuid4())

        # Combine this iteration's developer-approved witnesses (plus everything
        # approved anywhere in this program's ancestry) into the structured hints
        # and the .wit DSL file evaluate() needs to do a relaxed check in the SAME
        # pass as the unconditional one - see openevolve/evaluator.py's EQUIV_MODE.
        # Ancestry is propagated so a descendant is checked against the run's
        # fixed baseline with the full set of claims its lineage relies on, not
        # just this iteration's delta.
        import tempfile

        from openevolve.utils.code_utils import combine_witness_dsl_blocks, merge_witnesses

        newly_approved = [
            w
            for w in witnesses
            if w.get("developer_approved") is True
            and (w.get("map_width_change") or w.get("map_key_width_change") or w.get("map_fusion"))
        ]
        inherited = parent.metadata.get("approved_witnesses") or []
        accumulated = merge_witnesses(inherited, newly_approved)

        # The .wit DSL text itself is NOT ancestry-propagated (same as before this
        # refactor) - only this iteration's own witnesses contribute to it.
        wit_path = None
        wit_text = None
        wit_check = None
        combined_wit = combine_witness_dsl_blocks(witnesses, only_approved=True)
        if combined_wit.strip():
            wit_text = combined_wit
            wit_dest = Path(tempfile.mkdtemp(prefix="openevolve_bpf_wit_")) / f"{child_id}.wit"
            wit_check = _write_and_check_wit(combined_wit, wit_dest)
            logger.info(
                "Iteration %d: combined .wit built, syntax ok=%s", iteration, wit_check.get("ok")
            )
            if wit_check.get("ok") is True:
                wit_path = wit_check["path"]
            elif wit_check.get("output"):
                logger.info("witness_dsl: %s", wit_check["output"])

        child_metrics = asyncio.run(
            _worker_evaluator.evaluate_program(
                child_code, child_id, witnesses=accumulated, wit_path=wit_path, iteration=iteration
            )
        )

        artifacts = _worker_evaluator.get_pending_artifacts(child_id)

        child_program = Program(
            id=child_id,
            code=child_code,
            changes_description=child_changes_desc,
            language=_worker_config.language,
            parent_id=parent.id,
            generation=parent.generation + 1,
            metrics=child_metrics,
            iteration_found=iteration,
            metadata={
                "changes": changes_summary,
                "explanation": explanation,
                "witnesses": witnesses,
                "approved_witnesses": accumulated,
                "wit": wit_check,
                "wit_text": wit_text,
                "parent_metrics": parent.metrics,
                "island": parent_island,
            },
        )

        iteration_time = time.time() - iteration_start

        return SerializableResult(
            child_program_dict=child_program.to_dict(),
            parent_id=parent.id,
            iteration_time=iteration_time,
            prompt=prompt,
            llm_response=llm_response,
            artifacts=artifacts,
            iteration=iteration,
            target_island=db_snapshot.get("sampling_island"),
            token_usage=llm_token_usage,
        )

    except Exception as e:
        logger.exception(f"Error in worker iteration {iteration} (implement)")
        return SerializableResult(error=str(e), iteration=iteration)


def _wait_for_processes(processes: tuple[mp.Process, ...], timeout: float) -> list[mp.Process]:
    """Wait for process handles to observe worker exits without blocking indefinitely."""
    deadline = time.monotonic() + timeout
    alive = list(processes)
    while alive:
        next_alive = []
        for process in alive:
            try:
                process.join(timeout=0)
                if process.is_alive():
                    next_alive.append(process)
            except (AssertionError, ValueError):
                continue
        alive = next_alive
        remaining = deadline - time.monotonic()
        if not alive or remaining <= 0:
            break
        time.sleep(min(0.001, remaining))
    return alive


def _terminate_process_pool(executor: ProcessPoolExecutor) -> None:
    """Cancel queued work and ensure all process-pool workers have exited."""
    # Python < 3.14 has no public force-shutdown API. Capture only this
    # executor's workers before shutdown clears its private process mapping.
    process_map = getattr(executor, "_processes", None) or {}
    processes = tuple(process_map.copy().values())
    terminate_workers = getattr(executor, "terminate_workers", None)

    if callable(terminate_workers):
        terminate_workers()
    else:
        executor.shutdown(wait=False, cancel_futures=True)
        for process in processes:
            try:
                if process.is_alive():
                    process.terminate()
            except (ProcessLookupError, ValueError):
                continue

    surviving_processes = _wait_for_processes(processes, timeout=1.0)
    for process in surviving_processes:
        try:
            process.kill()
        except (ProcessLookupError, ValueError):
            continue

    surviving_processes = _wait_for_processes(tuple(surviving_processes), timeout=1.0)
    if surviving_processes:
        logger.warning(
            "Process-pool workers did not exit: %s",
            [process.pid for process in surviving_processes],
        )


class ProcessParallelController:
    """Controller for process-based parallel evolution"""

    def __init__(
        self,
        config: Config,
        evaluation_file: str,
        database: ProgramDatabase,
        evolution_tracer=None,
        file_suffix: str = ".py",
        review_gate=None,
    ):
        self.config = config
        self.evaluation_file = evaluation_file
        self.database = database
        self.evolution_tracer = evolution_tracer
        self.file_suffix = file_suffix
        self.review_gate = review_gate

        self.executor: Optional[ProcessPoolExecutor] = None
        self.shutdown_event = mp.Event()
        self.early_stopping_triggered = False

        # Number of worker processes
        self.num_workers = config.evaluator.parallel_evaluations
        self.num_islands = config.database.num_islands

        # Interactive review mode (see review_gate.py): feedback from rejected
        # attempts, keyed by parent_id, and how many times each parent has been
        # rejected in a row (used to give up on a stuck lineage)
        self.interactive_enabled = bool(config.interactive.enabled) and review_gate is not None
        self._pending_feedback: Dict[str, str] = {}
        self._rejection_counts: Dict[str, int] = {}
        # Separate from _rejection_counts (which resets the moment a proposal is
        # approved, before implementation even runs): how many times IN A ROW an
        # approved proposal for this parent has failed to compile. Also feeds
        # _pending_feedback (same channel the next propose call reads either way),
        # kept in its own dict purely so the two failure kinds don't reset each
        # other's streak.
        self._compile_failure_counts: Dict[str, int] = {}
        # Unlike _pending_feedback (per-parent, cleared once that lineage moves on),
        # this is run-wide: every distinct compile error seen from ANY parent this
        # run, shown to EVERY propose call regardless of which parent it targets.
        # Without this, giving up on a stuck lineage (see max_rejections_per_parent)
        # throws away everything learned about why it kept failing - a fresh parent
        # can, and in practice does, re-discover the exact same environment
        # restriction (e.g. clang's BPF backend rejecting memset()) from scratch.
        # Capped to the most recent N distinct errors so the prompt doesn't grow
        # unboundedly over a long run.
        self._known_compile_failures: List[str] = []
        self._known_compile_failures_cap = 10

        # Run-wide LLM token usage, summed across every completed iteration's
        # single LLM call (propose-phase + implement-phase in interactive mode,
        # or the one call per iteration in non-interactive mode). Each worker
        # reports its own call's usage as a delta on its result dataclass (see
        # SerializableResult.token_usage / ProposalResult.token_usage) rather
        # than a cumulative snapshot, so summing deltas here is correct
        # regardless of how many worker processes ran them. Read by cli.py at
        # the end of the run via OpenEvolve.parallel_controller.
        self.total_token_usage: Dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        # The most recently completed iteration's implement+evaluate result (see
        # _run_evolution_interactive below), shown alongside the NEXT witness
        # proposal's review task so the developer can see what their last approval
        # actually produced -- there is no separate review round for it, so without
        # this the compile/equivalence/performance results computed for every
        # iteration would never reach the browser at all. None until the first
        # witness is approved and implemented; persists across intervening
        # rejections (still the most recent real result), tagged with its own
        # iteration number so that's never ambiguous.
        self._last_iteration_result: Optional[Dict[str, Any]] = None

        logger.info(f"Initialized process parallel controller with {self.num_workers} workers")

    def _dump_witness_file(
        self,
        iteration: int,
        child_program: Program,
        witnesses: List[Dict[str, Any]],
        qualifying: List[Dict[str, Any]],
    ) -> None:
        """Write this child's exact --witness-file payload -- and, for every
        witness, why it did or didn't qualify -- to a plain JSON file in a
        witness_files/ subdirectory next to the review-queue task files, so a
        developer can inspect it directly on disk instead of reconstructing it
        from the raw task JSON by hand. Deliberately NOT written directly into
        queue_dir itself: scripts/review.py's _list_tasks() lists every
        *.json file there that lacks a matching *.decision.json sibling as a
        pending review task, and this dump has neither an "iteration" key nor
        the witnesses/child_code shape a task needs -- co-locating it there
        made it show up in the review UI as a bogus task ("Iteration ?").

        Filenames are prefixed with the zero-padded iteration number (e.g.
        "0007_<child_id>.json") so `ls` sorts them in run order and a
        developer can tell which iteration produced which file without
        cross-referencing the log -- the child UUID alone doesn't say that.
        The ".wit" copy is written unconditionally, even when it's empty --
        see the comment above its write call.

        "heimdall_witness_file" is exactly what evaluate()'s relaxed check will
        (or would) hand heimdall as --witness-file for this child; it's the
        empty {"witnesses": []} shape whenever `qualifying` is empty, which
        itself is useful signal (the
        approval didn't produce anything for heimdall to check). The
        per-witness breakdown makes it obvious WHY a witness is or isn't in
        that payload -- e.g. developer_approved is True but proof_status is
        "parse_error", so it never reached qualifying.
        """
        from openevolve.utils.code_utils import build_heimdall_witness_file

        qualifying_indices = {w.get("index") for w in qualifying}
        dump = {
            "child_id": child_program.id,
            "parent_id": child_program.parent_id,
            "heimdall_witness_file": build_heimdall_witness_file(qualifying),
            "witnesses": [
                {
                    "index": w.get("index"),
                    "summary": w.get("summary"),
                    "developer_approved": w.get("developer_approved"),
                    "proof_status": (w.get("proof") or {}).get("status"),
                    "proof_detail": (w.get("proof") or {}).get("detail"),
                    "wit": w.get("wit") or "",
                    "map_width_change": w.get("map_width_change"),
                    "map_fusion": w.get("map_fusion"),
                    "variable_width_change": w.get("variable_width_change"),
                    "qualifies_for_reverify": w.get("index") in qualifying_indices,
                }
                for w in witnesses
            ],
        }
        witness_files_dir = self.review_gate.queue_dir / "witness_files"
        stem = f"{iteration:04d}_{child_program.id}"
        path = witness_files_dir / f"{stem}.json"
        try:
            witness_files_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(dump, indent=2))
        except OSError as e:
            logger.warning(f"Could not write witness file dump to {path}: {e}")

        # Persist the combined .wit the worker already syntax-checked (see
        # _run_iteration_worker_implement) as a durable, developer-facing copy -
        # the worker's own copy lives in a throwaway temp dir that may not
        # outlive the call. Always written, even when empty (every approved
        # witness left both blocks blank, or nothing was approved) -- an
        # absent .wit file reads as "did this even run?"; a present one with
        # an explanatory comment reads as "confirmed empty, here's why".
        wit_text = child_program.metadata.get("wit_text")
        content = wit_text or (
            "// No assumption/binding statements: every approved witness left both\n"
            "// blocks empty (a purely structural change), or nothing was approved.\n"
        )
        wit_path = witness_files_dir / f"{stem}.wit"
        try:
            wit_path.write_text(content if content.endswith("\n") else content + "\n")
        except OSError as e:
            logger.warning(f"Could not write .wit copy to {wit_path}: {e}")

    def _serialize_config(self, config: Config) -> dict:
        """Serialize config object to a dictionary that can be pickled"""
        # Manual serialization to handle nested objects properly

        # The asdict() call itself triggers the deepcopy which tries to serialize novelty_llm. Remove it first.
        config.database.novelty_llm = None

        return {
            "llm": {
                "models": [asdict(m) for m in config.llm.models],
                "evaluator_models": [asdict(m) for m in config.llm.evaluator_models],
                "api_base": config.llm.api_base,
                "api_key": config.llm.api_key,
                "temperature": config.llm.temperature,
                "top_p": config.llm.top_p,
                "max_tokens": config.llm.max_tokens,
                "timeout": config.llm.timeout,
                "retries": config.llm.retries,
                "retry_delay": config.llm.retry_delay,
            },
            "prompt": asdict(config.prompt),
            "database": asdict(config.database),
            "evaluator": asdict(config.evaluator),
            "max_iterations": config.max_iterations,
            "checkpoint_interval": config.checkpoint_interval,
            "log_level": config.log_level,
            "log_dir": config.log_dir,
            "random_seed": config.random_seed,
            "diff_based_evolution": config.diff_based_evolution,
            "max_code_length": config.max_code_length,
            "language": config.language,
            "file_suffix": self.file_suffix,
        }

    def start(self) -> None:
        """Start the process pool"""
        # Convert config to dict for pickling
        # We need to be careful with nested dataclasses
        config_dict = self._serialize_config(self.config)

        # Pass current environment to worker processes
        import os
        import sys

        current_env = dict(os.environ)

        executor_kwargs = {
            "max_workers": self.num_workers,
            "initializer": _worker_init,
            "initargs": (config_dict, self.evaluation_file, current_env),
        }
        if sys.version_info >= (3, 11):
            logger.info(f"Set max {self.config.max_tasks_per_child} tasks per child")
            executor_kwargs["max_tasks_per_child"] = self.config.max_tasks_per_child
        elif self.config.max_tasks_per_child is not None:
            logger.warn(
                "max_tasks_per_child is only supported in Python 3.11+. "
                "Ignoring max_tasks_per_child and using spawn start method."
            )
            executor_kwargs["mp_context"] = mp.get_context("spawn")

        # Create process pool with initializer
        self.executor = ProcessPoolExecutor(**executor_kwargs)
        logger.info(f"Started process pool with {self.num_workers} processes")

    def stop(self) -> None:
        """Stop the process pool"""
        self.shutdown_event.set()

        executor = self.executor
        self.executor = None
        if executor:
            _terminate_process_pool(executor)

        logger.info("Stopped process pool")

    def request_shutdown(self) -> None:
        """Request graceful shutdown"""
        logger.info("Graceful shutdown requested...")
        self.shutdown_event.set()

    def _create_database_snapshot(self) -> Dict[str, Any]:
        """Create a serializable snapshot of the database state"""
        # Only include necessary data for workers
        snapshot = {
            "programs": {pid: prog.to_dict() for pid, prog in self.database.programs.items()},
            "islands": [list(island) for island in self.database.islands],
            "current_island": self.database.current_island,
            "feature_dimensions": self.database.config.feature_dimensions,
            "artifacts": {},  # Will be populated selectively
            "developer_feedback": dict(self._pending_feedback),
            "known_compile_failures": list(self._known_compile_failures),
        }

        # Include artifacts for programs that might be selected
        # This limits artifacts (execution outputs/errors) to avoid large snapshot sizes.
        # This does NOT affect program code - all programs are fully serialized above.
        # With max_artifact_bytes=20KB and population_size=1000, artifacts could be 20MB total,
        # which would significantly slow worker process initialization. The default limit of 100
        # keeps artifact data under 2MB while still providing execution context for recent programs.
        # Workers can still evolve properly as they have access to ALL program code.
        # Configure via database.max_snapshot_artifacts (None for unlimited).
        max_artifacts = self.database.config.max_snapshot_artifacts
        program_ids = list(self.database.programs.keys())
        if max_artifacts is not None:
            program_ids = program_ids[:max_artifacts]
        for pid in program_ids:
            artifacts = self.database.get_artifacts(pid)
            if artifacts:
                snapshot["artifacts"][pid] = artifacts

        return snapshot

    def _accumulate_token_usage(self, usage: Optional[Dict[str, int]]) -> None:
        """Add one iteration's LLM call usage (may be None -- manual mode, a
        non-reporting provider, or an iteration that never reached the LLM
        call) into the run-wide total."""
        if not usage:
            return
        for key in self.total_token_usage:
            self.total_token_usage[key] += usage.get(key, 0) or 0

    async def run_evolution(
        self,
        start_iteration: int,
        max_iterations: int,
        target_score: Optional[float] = None,
        checkpoint_callback=None,
    ):
        """Run evolution with process-based parallelism"""
        if not self.executor:
            raise RuntimeError("Process pool not started")

        if self.interactive_enabled:
            return await self._run_evolution_interactive(
                start_iteration, max_iterations, target_score, checkpoint_callback
            )

        total_iterations = start_iteration + max_iterations

        logger.info(
            f"Starting process-based evolution from iteration {start_iteration} "
            f"for {max_iterations} iterations (total: {total_iterations})"
        )

        # Track pending futures by island to maintain distribution
        pending_futures: Dict[int, Future] = {}
        island_pending: Dict[int, List[int]] = {i: [] for i in range(self.num_islands)}
        batch_size = min(self.num_workers * 2, max_iterations)

        # Submit initial batch - distribute across islands
        batch_per_island = max(1, batch_size // self.num_islands) if batch_size > 0 else 0
        current_iteration = start_iteration

        # Round-robin distribution across islands
        for island_id in range(self.num_islands):
            for _ in range(batch_per_island):
                if current_iteration < total_iterations:
                    future = self._submit_iteration(current_iteration, island_id)
                    if future:
                        pending_futures[current_iteration] = future
                        island_pending[island_id].append(current_iteration)
                    current_iteration += 1

        next_iteration = current_iteration
        completed_iterations = 0

        # Early stopping tracking
        early_stopping_enabled = self.config.early_stopping_patience is not None
        if early_stopping_enabled:
            best_score = float("-inf")
            iterations_without_improvement = 0
            if self.config.early_stopping_patience < 0:
                logger.info(
                    f"Early stopping patience is set to a negative value, running event-based early-stopping, "
                    f"Early stop when metric '{self.config.early_stopping_metric}' reaches {self.config.convergence_threshold}"
                )
            else:
                logger.info(
                    f"Early stopping enabled: patience={self.config.early_stopping_patience}, "
                    f"threshold={self.config.convergence_threshold}, "
                    f"metric={self.config.early_stopping_metric}"
                )
        else:
            logger.info("Early stopping disabled")

        # Process results as they complete
        while (
            pending_futures
            and completed_iterations < max_iterations
            and not self.shutdown_event.is_set()
        ):
            # Find completed futures
            completed_iteration = None
            for iteration, future in list(pending_futures.items()):
                if future.done():
                    completed_iteration = iteration
                    break

            if completed_iteration is None:
                await asyncio.sleep(0.01)
                continue

            # Process completed result
            future = pending_futures.pop(completed_iteration)

            try:
                # Use evaluator timeout + buffer to gracefully handle stuck processes
                timeout_seconds = self.config.evaluator.timeout + 30
                result = future.result(timeout=timeout_seconds)
                self._accumulate_token_usage(result.token_usage)

                if result.error:
                    logger.warning(f"Iteration {completed_iteration} error: {result.error}")
                elif result.child_program_dict:
                    # Reconstruct program from dict
                    child_program = Program(**result.child_program_dict)

                    # Add to database with explicit target_island to ensure proper island placement
                    # This fixes issue #391: children should go to the target island, not inherit
                    # from the parent (which may be from a different island due to fallback sampling)
                    self.database.add(
                        child_program,
                        iteration=completed_iteration,
                        target_island=result.target_island,
                    )

                    # Store artifacts
                    if result.artifacts:
                        self.database.store_artifacts(child_program.id, result.artifacts)

                    # Log evolution trace
                    if self.evolution_tracer:
                        # Retrieve parent program for trace logging
                        parent_program = (
                            self.database.get(result.parent_id) if result.parent_id else None
                        )
                        if parent_program:
                            # Determine island ID
                            island_id = child_program.metadata.get(
                                "island", self.database.current_island
                            )

                            self.evolution_tracer.log_trace(
                                iteration=completed_iteration,
                                parent_program=parent_program,
                                child_program=child_program,
                                prompt=result.prompt,
                                llm_response=result.llm_response,
                                artifacts=result.artifacts,
                                island_id=island_id,
                                metadata={
                                    "iteration_time": result.iteration_time,
                                    "changes": child_program.metadata.get("changes", ""),
                                },
                            )

                    # Log prompts
                    if result.prompt:
                        self.database.log_prompt(
                            template_key=(
                                "full_rewrite_user"
                                if not self.config.diff_based_evolution
                                else "diff_user"
                            ),
                            program_id=child_program.id,
                            prompt=result.prompt,
                            responses=[result.llm_response] if result.llm_response else [],
                        )

                    # Island management
                    # get current program island id
                    island_id = child_program.metadata.get("island", self.database.current_island)
                    # use this to increment island generation
                    self.database.increment_island_generation(island_idx=island_id)

                    # Check migration
                    if self.database.should_migrate():
                        logger.info(f"Performing migration at iteration {completed_iteration}")
                        self.database.migrate_programs()
                        self.database.log_island_status()

                    # Log progress
                    logger.info(
                        f"Iteration {completed_iteration}: "
                        f"Program {child_program.id} "
                        f"(parent: {result.parent_id}) "
                        f"completed in {result.iteration_time:.2f}s"
                    )

                    if child_program.metrics:
                        metrics_str = ", ".join(
                            [
                                f"{k}={v:.4f}" if isinstance(v, (int, float)) else f"{k}={v}"
                                for k, v in child_program.metrics.items()
                            ]
                        )
                        logger.info(f"Metrics: {metrics_str}")

                        # Check if this is the first program without combined_score
                        if not hasattr(self, "_warned_about_combined_score"):
                            self._warned_about_combined_score = False

                        if (
                            "combined_score" not in child_program.metrics
                            and not self._warned_about_combined_score
                        ):
                            avg_score = safe_numeric_average(child_program.metrics)
                            logger.warning(
                                f"⚠️  No 'combined_score' metric found in evaluation results. "
                                f"Using average of all numeric metrics ({avg_score:.4f}) for evolution guidance. "
                                f"For better evolution results, please modify your evaluator to return a 'combined_score' "
                                f"metric that properly weights different aspects of program performance."
                            )
                            self._warned_about_combined_score = True

                    # Check for new best
                    if self.database.best_program_id == child_program.id:
                        logger.info(
                            f"🌟 New best solution found at iteration {completed_iteration}: "
                            f"{child_program.id}"
                        )

                    # Checkpoint callback
                    # Don't checkpoint at iteration 0 (that's just the initial program)
                    if (
                        completed_iteration > 0
                        and completed_iteration % self.config.checkpoint_interval == 0
                    ):
                        logger.info(
                            f"Checkpoint interval reached at iteration {completed_iteration}"
                        )
                        self.database.log_island_status()
                        if checkpoint_callback:
                            checkpoint_callback(completed_iteration)

                    # Check target score
                    if target_score is not None and child_program.metrics:
                        if (
                            "combined_score" in child_program.metrics
                            and child_program.metrics["combined_score"] >= target_score
                        ):
                            logger.info(
                                f"Target score {target_score} reached at iteration {completed_iteration}"
                            )
                            break

                    # Check early stopping
                    if early_stopping_enabled and child_program.metrics:
                        # Get the metric to track for early stopping
                        current_score = None
                        if self.config.early_stopping_metric in child_program.metrics:
                            current_score = child_program.metrics[self.config.early_stopping_metric]
                        elif self.config.early_stopping_metric == "combined_score":
                            # Default metric not found, use safe average (standard pattern)
                            current_score = safe_numeric_average(child_program.metrics)
                        else:
                            # User specified a custom metric that doesn't exist
                            logger.warning(
                                f"Early stopping metric '{self.config.early_stopping_metric}' not found, using safe numeric average"
                            )
                            current_score = safe_numeric_average(child_program.metrics)

                        if current_score is not None and isinstance(current_score, (int, float)):
                            # Check for improvement
                            if self.config.early_stopping_patience > 0:
                                improvement = current_score - best_score
                                if improvement >= self.config.convergence_threshold:
                                    best_score = current_score
                                    iterations_without_improvement = 0
                                    logger.debug(
                                        f"New best score: {best_score:.4f} (improvement: {improvement:+.4f})"
                                    )
                                else:
                                    iterations_without_improvement += 1
                                    logger.debug(
                                        f"No improvement: {iterations_without_improvement}/{self.config.early_stopping_patience}"
                                    )

                                # Check if we should stop
                                if (
                                    iterations_without_improvement
                                    >= self.config.early_stopping_patience
                                ):
                                    self.early_stopping_triggered = True
                                    logger.info(
                                        f"🛑 Early stopping triggered at iteration {completed_iteration}: "
                                        f"No improvement for {iterations_without_improvement} iterations "
                                        f"(best score: {best_score:.4f})"
                                    )
                                    break

                            else:
                                # Event-based early stopping
                                if current_score == self.config.convergence_threshold:
                                    best_score = current_score
                                    logger.info(
                                        f"🛑 Early stopping (event-based) triggered at iteration {completed_iteration}: "
                                        f"Task successfully solved with score {best_score:.4f}."
                                    )
                                    self.early_stopping_triggered = True
                                    break

            except FutureTimeoutError:
                logger.error(
                    f"⏰ Iteration {completed_iteration} timed out after {timeout_seconds}s "
                    f"(evaluator timeout: {self.config.evaluator.timeout}s + 30s buffer). "
                    f"Canceling future and continuing with next iteration."
                )
                # Cancel the future to clean up the process
                future.cancel()
            except Exception as e:
                logger.error(f"Error processing result from iteration {completed_iteration}: {e}")

            completed_iterations += 1

            # Remove completed iteration from island tracking
            for island_id, iteration_list in island_pending.items():
                if completed_iteration in iteration_list:
                    iteration_list.remove(completed_iteration)
                    break

            # Submit next iterations maintaining island balance
            for island_id in range(self.num_islands):
                if (
                    len(island_pending[island_id]) < batch_per_island
                    and next_iteration < total_iterations
                    and not self.shutdown_event.is_set()
                ):
                    future = self._submit_iteration(next_iteration, island_id)
                    if future:
                        pending_futures[next_iteration] = future
                        island_pending[island_id].append(next_iteration)
                        next_iteration += 1
                        break  # Only submit one iteration per completion to maintain balance

        # Handle shutdown
        if self.shutdown_event.is_set():
            logger.info("Shutdown requested, canceling remaining evaluations...")
            for future in pending_futures.values():
                future.cancel()

        # Log completion reason
        if self.early_stopping_triggered:
            logger.info("✅ Evolution completed - Early stopping triggered due to convergence")
        elif self.shutdown_event.is_set():
            logger.info("✅ Evolution completed - Shutdown requested")
        else:
            logger.info("✅ Evolution completed - Maximum iterations reached")

        return self.database.get_best_program()

    async def _run_evolution_interactive(
        self,
        start_iteration: int,
        max_iterations: int,
        target_score: Optional[float] = None,
        checkpoint_callback=None,
    ):
        """
        Run evolution one iteration at a time, two phases per iteration:

          1. Propose: the LLM proposes transformation witnesses only (no code). The
             developer reviews and approves/rejects each witness individually via
             self.review_gate -- BEFORE any code is generated or evaluated.
          2. Implement: only if approved (with at least one approved witness), a second
             LLM call implements just the approved witnesses; the resulting program is
             then evaluated exactly like the non-interactive path. There is no further
             review after this -- the human gate is before code generation, not after.

        Runs single-flight (no batching across islands) because the parent sampled for
        iteration N+1 depends on whether iteration N's proposal was accepted.
        """
        total_iterations = start_iteration + max_iterations
        logger.info(
            f"Starting interactive (human-reviewed) evolution from iteration {start_iteration} "
            f"for {max_iterations} iterations (total: {total_iterations})"
        )

        current_iteration = start_iteration
        completed_iterations = 0
        island_id = 0
        forced_parent_id: Optional[str] = None

        early_stopping_enabled = self.config.early_stopping_patience is not None
        best_score = float("-inf")
        iterations_without_improvement = 0

        loop = asyncio.get_event_loop()
        timeout_seconds = self.config.evaluator.timeout + 30

        while (
            current_iteration < total_iterations
            and completed_iterations < max_iterations
            and not self.shutdown_event.is_set()
        ):
            target_island = island_id % self.num_islands

            # Sample once per iteration attempt and reuse for both phases below, so the
            # LLM sees identical parent/evolution-history context in both calls.
            parent, inspirations = self._sample_parent_and_inspirations(
                target_island, forced_parent_id
            )
            inspiration_ids = [insp.id for insp in inspirations]

            # --- Phase 1: propose ---
            proposal_future = self._submit_proposal(
                current_iteration, target_island, parent.id, inspiration_ids
            )
            if proposal_future is None:
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                forced_parent_id = None
                continue

            try:
                # Offload the blocking wait onto a thread so the event loop (and
                # the review gate's own polling) keeps running.
                proposal = await loop.run_in_executor(
                    None, proposal_future.result, timeout_seconds
                )
                self._accumulate_token_usage(proposal.token_usage)
            except FutureTimeoutError:
                logger.error(
                    f"⏰ Iteration {current_iteration} proposal timed out after "
                    f"{timeout_seconds}s. Canceling future and continuing with next iteration."
                )
                proposal_future.cancel()
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                forced_parent_id = None
                continue
            except Exception as e:
                logger.error(
                    f"Error processing proposal from iteration {current_iteration}: {e}"
                )
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                forced_parent_id = None
                continue

            if proposal.error:
                logger.warning(f"Iteration {current_iteration} proposal error: {proposal.error}")
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                forced_parent_id = None
                continue

            if not proposal.witnesses:
                # Nothing parseable to review or implement -- treat like a rejection so
                # the same parent gets retried with feedback, same bookkeeping (and the
                # same eventual give-up) as any other rejected proposal.
                logger.info(
                    f"Iteration {current_iteration}: no transformation witnesses found in "
                    f"proposal; retrying with feedback"
                )
                feedback = (
                    "No transformation witnesses were found in your proposal. Follow the "
                    "required numbered '(1) .../Witness:/Formula (pre-transformation):/"
                    "Formula (post-transformation):' format exactly for every proposed change."
                )
                self._pending_feedback[parent.id] = feedback
                self._rejection_counts[parent.id] = self._rejection_counts.get(parent.id, 0) + 1
                if (
                    self._rejection_counts[parent.id]
                    >= self.config.interactive.max_rejections_per_parent
                ):
                    logger.info(
                        f"Parent {parent.id} rejected {self._rejection_counts[parent.id]} "
                        f"times in a row; giving up on this lineage and sampling a new parent next"
                    )
                    del self._rejection_counts[parent.id]
                    del self._pending_feedback[parent.id]
                    forced_parent_id = None
                else:
                    forced_parent_id = parent.id
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            # Ask the developer to review the proposed witnesses -- BEFORE any code
            # exists - unless auto_approve is on, in which case every witness is
            # approved without a human ever seeing it (see InteractiveConfig.auto_approve
            # for the trust-gate trade-off this makes).
            if self.config.interactive.auto_approve:
                decision = ReviewDecision(
                    approved=True,
                    feedback="",
                    witness_decisions={
                        str(w.get("index")): True for w in proposal.witnesses
                    },
                )
            else:
                decision = await self.review_gate.request_witness_review(
                    iteration=current_iteration,
                    parent=parent,
                    explanation=proposal.explanation,
                    witnesses=proposal.witnesses,
                    parent_artifacts=self.database.get_artifacts(parent.id),
                    previous_result=self._last_iteration_result,
                )

            # Stamp each witness with the developer's per-witness call (True/False).
            # Undecided witnesses (developer didn't touch that control) get None, not
            # False, so downstream consumers can tell "explicitly rejected" apart from
            # "never reviewed".
            for witness in proposal.witnesses:
                witness["developer_approved"] = decision.witness_decisions.get(
                    str(witness.get("index"))
                )

            approved_witnesses = [
                w for w in proposal.witnesses if w.get("developer_approved") is True
            ]

            if not decision.approved or not approved_witnesses:
                if decision.approved and not approved_witnesses:
                    logger.info(
                        f"Iteration {current_iteration}: proposal approved but no individual "
                        f"witness was approved; treating as rejected"
                    )
                else:
                    logger.info(
                        f"Iteration {current_iteration}: proposal rejected by developer"
                    )
                self._pending_feedback[parent.id] = decision.feedback
                self._rejection_counts[parent.id] = self._rejection_counts.get(parent.id, 0) + 1

                if (
                    self._rejection_counts[parent.id]
                    >= self.config.interactive.max_rejections_per_parent
                ):
                    logger.info(
                        f"Parent {parent.id} rejected "
                        f"{self._rejection_counts[parent.id]} times in a row; "
                        f"giving up on this lineage and sampling a new parent next"
                    )
                    del self._rejection_counts[parent.id]
                    del self._pending_feedback[parent.id]
                    forced_parent_id = None
                else:
                    forced_parent_id = parent.id

                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            # Approved: implement only the approved witnesses, then evaluate
            logger.info(
                f"Iteration {current_iteration}: {len(approved_witnesses)} witness(es) "
                f"approved by developer; implementing"
            )
            self._pending_feedback.pop(parent.id, None)
            self._rejection_counts.pop(parent.id, None)
            forced_parent_id = None

            # --- Phase 2: implement ---
            implement_future = self._submit_implementation(
                current_iteration,
                target_island,
                parent.id,
                inspiration_ids,
                proposal.explanation,
                proposal.witnesses,
                decision.feedback,
            )
            if implement_future is None:
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            try:
                result = await loop.run_in_executor(None, implement_future.result, timeout_seconds)
                self._accumulate_token_usage(result.token_usage)
            except FutureTimeoutError:
                logger.error(
                    f"⏰ Iteration {current_iteration} implementation timed out after "
                    f"{timeout_seconds}s (evaluator timeout: {self.config.evaluator.timeout}s "
                    f"+ 30s buffer). Canceling future and continuing with next iteration."
                )
                implement_future.cancel()
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue
            except Exception as e:
                logger.error(
                    f"Error processing implementation from iteration {current_iteration}: {e}"
                )
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            if result.error:
                logger.warning(f"Iteration {current_iteration} implement error: {result.error}")
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            if not result.child_program_dict:
                current_iteration += 1
                completed_iterations += 1
                island_id += 1
                continue

            child_program = Program(**result.child_program_dict)

            # From here on, same bookkeeping as the standard (non-interactive) path --
            # no further review round, the human gate already happened before phase 2.
            # The witness combining + evaluate() call already happened together, in
            # the worker (_run_iteration_worker_implement) - this is purely a
            # developer-facing diagnostic dump of what was already decided there.
            self._dump_witness_file(
                current_iteration,
                child_program,
                child_program.metadata.get("witnesses") or [],
                child_program.metadata.get("approved_witnesses") or [],
            )

            self.database.add(
                child_program, iteration=current_iteration, target_island=result.target_island
            )

            combined_artifacts = dict(result.artifacts or {})
            if combined_artifacts:
                self.database.store_artifacts(child_program.id, combined_artifacts)

            # An approved witness can still fail to compile - the developer never
            # saw the actual code, only the proposed change description. Without
            # this, a later proposal from the SAME parent has no way to know a
            # sibling attempt already hit this error and can repeat it indefinitely
            # (e.g. proposing memset(), which clang's BPF backend rejects, over and
            # over from a parent that itself compiles fine). Mirrors the existing
            # developer-rejection retry-with-feedback pattern above, including the
            # same give-up-after-N-in-a-row safety valve, but tracked separately
            # (_compile_failure_counts) so an approval doesn't reset a streak that
            # hasn't actually been resolved yet.
            if child_program.metrics.get("compile_success", 1.0) != 1.0:
                stderr_text = (combined_artifacts.get("stderr") or combined_artifacts.get("error") or "").strip()
                first_err = stderr_text.splitlines()[0] if stderr_text else "(no diagnostic)"
                self._pending_feedback[parent.id] = (
                    f"Your last approved implementation for this program failed to compile: "
                    f"{first_err}\nPropose a different change that avoids this error."
                )
                # Run-wide, not per-parent (see _known_compile_failures' docstring in
                # __init__): every propose call sees this, regardless of which parent
                # it targets, so giving up on this lineage doesn't lose the lesson.
                if first_err not in self._known_compile_failures:
                    self._known_compile_failures.append(first_err)
                    del self._known_compile_failures[: -self._known_compile_failures_cap]
                self._compile_failure_counts[parent.id] = (
                    self._compile_failure_counts.get(parent.id, 0) + 1
                )
                if (
                    self._compile_failure_counts[parent.id]
                    >= self.config.interactive.max_rejections_per_parent
                ):
                    logger.info(
                        f"Parent {parent.id} failed to compile "
                        f"{self._compile_failure_counts[parent.id]} times in a row; giving up "
                        f"on this lineage and sampling a new parent next"
                    )
                    del self._compile_failure_counts[parent.id]
                    self._pending_feedback.pop(parent.id, None)
                    forced_parent_id = None
                else:
                    forced_parent_id = parent.id
            else:
                self._compile_failure_counts.pop(parent.id, None)

            # Surface this iteration's compile/equivalence/performance result on the
            # NEXT witness-review task -- see _last_iteration_result's docstring.
            self._last_iteration_result = {
                "iteration": current_iteration,
                "child_id": child_program.id,
                "explanation": proposal.explanation,
                "code": child_program.code,
                "metrics": child_program.metrics,
                "artifacts": combined_artifacts,
            }

            if self.evolution_tracer:
                trace_island_id = child_program.metadata.get(
                    "island", self.database.current_island
                )
                self.evolution_tracer.log_trace(
                    iteration=current_iteration,
                    parent_program=parent,
                    child_program=child_program,
                    prompt=result.prompt,
                    llm_response=result.llm_response,
                    artifacts=result.artifacts,
                    island_id=trace_island_id,
                    metadata={
                        "iteration_time": result.iteration_time,
                        "changes": child_program.metadata.get("changes", ""),
                        "developer_approved": True,
                        "proposal_prompt": proposal.prompt,
                        "proposal_llm_response": proposal.llm_response,
                    },
                )

            propose_template_key = (
                "diff_user_propose" if self.config.diff_based_evolution else "full_rewrite_user_propose"
            )
            implement_template_key = (
                "diff_user_implement" if self.config.diff_based_evolution else "full_rewrite_user_implement"
            )
            if proposal.prompt:
                self.database.log_prompt(
                    template_key=propose_template_key,
                    program_id=child_program.id,
                    prompt=proposal.prompt,
                    responses=[proposal.llm_response] if proposal.llm_response else [],
                )
            if result.prompt:
                self.database.log_prompt(
                    template_key=implement_template_key,
                    program_id=child_program.id,
                    prompt=result.prompt,
                    responses=[result.llm_response] if result.llm_response else [],
                )

            db_island_id = child_program.metadata.get("island", self.database.current_island)
            self.database.increment_island_generation(island_idx=db_island_id)

            if self.database.should_migrate():
                logger.info(f"Performing migration at iteration {current_iteration}")
                self.database.migrate_programs()
                self.database.log_island_status()

            logger.info(
                f"Iteration {current_iteration}: "
                f"Program {child_program.id} "
                f"(parent: {result.parent_id}) "
                f"completed in {result.iteration_time:.2f}s"
            )

            if child_program.metrics:
                metrics_str = ", ".join(
                    f"{k}={v:.4f}" if isinstance(v, (int, float)) else f"{k}={v}"
                    for k, v in child_program.metrics.items()
                )
                logger.info(f"Metrics: {metrics_str}")

            if self.database.best_program_id == child_program.id:
                logger.info(
                    f"🌟 New best solution found at iteration {current_iteration}: "
                    f"{child_program.id}"
                )

            if (
                current_iteration > 0
                and current_iteration % self.config.checkpoint_interval == 0
            ):
                logger.info(f"Checkpoint interval reached at iteration {current_iteration}")
                self.database.log_island_status()
                if checkpoint_callback:
                    checkpoint_callback(current_iteration)

            stop_reason = None

            if target_score is not None and child_program.metrics:
                if (
                    "combined_score" in child_program.metrics
                    and child_program.metrics["combined_score"] >= target_score
                ):
                    logger.info(
                        f"Target score {target_score} reached at iteration {current_iteration}"
                    )
                    stop_reason = "target_score"

            if stop_reason is None and early_stopping_enabled and child_program.metrics:
                if self.config.early_stopping_metric in child_program.metrics:
                    current_score = child_program.metrics[self.config.early_stopping_metric]
                else:
                    current_score = safe_numeric_average(child_program.metrics)

                if isinstance(current_score, (int, float)):
                    if self.config.early_stopping_patience > 0:
                        improvement = current_score - best_score
                        if improvement >= self.config.convergence_threshold:
                            best_score = current_score
                            iterations_without_improvement = 0
                        else:
                            iterations_without_improvement += 1

                        if iterations_without_improvement >= self.config.early_stopping_patience:
                            self.early_stopping_triggered = True
                            logger.info(
                                f"🛑 Early stopping triggered at iteration {current_iteration}: "
                                f"No improvement for {iterations_without_improvement} iterations "
                                f"(best score: {best_score:.4f})"
                            )
                            stop_reason = "early_stopping"
                    else:
                        if current_score == self.config.convergence_threshold:
                            best_score = current_score
                            self.early_stopping_triggered = True
                            logger.info(
                                f"🛑 Early stopping (event-based) triggered at iteration "
                                f"{current_iteration}: Task successfully solved with score "
                                f"{best_score:.4f}."
                            )
                            stop_reason = "early_stopping"

            current_iteration += 1
            completed_iterations += 1
            island_id += 1

            if stop_reason:
                break

        if self.shutdown_event.is_set():
            logger.info("✅ Evolution completed - Shutdown requested")
        elif self.early_stopping_triggered:
            logger.info("✅ Evolution completed - Early stopping triggered due to convergence")
        else:
            logger.info("✅ Evolution completed - Maximum iterations reached")

        return self.database.get_best_program()

    def _sample_parent_and_inspirations(
        self, island_id: int, forced_parent_id: Optional[str] = None
    ) -> Tuple[Program, List[Program]]:
        """
        Sample a parent + inspirations from an island, or reuse a forced parent (used by
        interactive review mode to retry the exact same parent, with fresh inspirations,
        after a rejection instead of re-sampling).
        """
        forced_parent = self.database.get(forced_parent_id) if forced_parent_id else None
        if forced_parent is not None:
            # Keep the same parent, but still draw fresh inspirations
            _, inspirations = self.database.sample_from_island(
                island_id=island_id,
                num_inspirations=self.config.prompt.num_diverse_programs,
            )
            return forced_parent, inspirations

        # Use thread-safe sampling that doesn't modify shared state
        # This fixes the race condition from GitHub issue #246
        # Inspirations are the diverse/creative examples; size them by
        # num_diverse_programs (not num_top_programs) so the config parameter
        # actually controls the inspiration count (GitHub issue #452).
        return self.database.sample_from_island(
            island_id=island_id,
            num_inspirations=self.config.prompt.num_diverse_programs,
        )

    def _submit_iteration(
        self,
        iteration: int,
        island_id: Optional[int] = None,
        forced_parent_id: Optional[str] = None,
    ) -> Optional[Future]:
        """
        Submit an iteration to the process pool, optionally pinned to a specific island

        forced_parent_id: used by interactive review mode to retry the exact same
        parent (with developer feedback) after a rejection, instead of re-sampling.
        """
        try:
            # Use specified island or current island
            target_island = island_id if island_id is not None else self.database.current_island

            parent, inspirations = self._sample_parent_and_inspirations(
                target_island, forced_parent_id
            )

            # Create database snapshot
            db_snapshot = self._create_database_snapshot()
            db_snapshot["sampling_island"] = target_island  # Mark which island this is for

            # Submit to process pool
            future = self.executor.submit(
                _run_iteration_worker,
                iteration,
                db_snapshot,
                parent.id,
                [insp.id for insp in inspirations],
            )

            return future

        except Exception as e:
            logger.error(f"Error submitting iteration {iteration}: {e}")
            return None

    def _submit_proposal(
        self, iteration: int, target_island: int, parent_id: str, inspiration_ids: List[str]
    ) -> Optional[Future]:
        """
        Submit phase 1 (propose) of the interactive two-phase flow: parent/inspirations
        are passed explicitly (not resampled here) so _submit_implementation can reuse
        the exact same context for phase 2.
        """
        try:
            db_snapshot = self._create_database_snapshot()
            db_snapshot["sampling_island"] = target_island

            return self.executor.submit(
                _run_iteration_worker_propose,
                iteration,
                db_snapshot,
                parent_id,
                inspiration_ids,
            )
        except Exception as e:
            logger.error(f"Error submitting proposal for iteration {iteration}: {e}")
            return None

    def _submit_implementation(
        self,
        iteration: int,
        target_island: int,
        parent_id: str,
        inspiration_ids: List[str],
        explanation: str,
        witnesses: List[Dict[str, Any]],
        developer_notes: str,
    ) -> Optional[Future]:
        """
        Submit phase 2 (implement) of the interactive two-phase flow, using the exact
        same parent/inspirations as phase 1 and the developer-approved witnesses.
        """
        try:
            db_snapshot = self._create_database_snapshot()
            db_snapshot["sampling_island"] = target_island

            return self.executor.submit(
                _run_iteration_worker_implement,
                iteration,
                db_snapshot,
                parent_id,
                inspiration_ids,
                explanation,
                witnesses,
                developer_notes,
            )
        except Exception as e:
            logger.error(f"Error submitting implementation for iteration {iteration}: {e}")
            return None
