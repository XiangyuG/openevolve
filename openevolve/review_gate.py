"""
Review gate for OpenEvolve interactive mode (human-in-the-loop iteration approval)

Mirrors the manual_mode task-queue pattern in openevolve/llm/openai.py: a review
request is written as a JSON task file, and the caller asynchronously polls for a
matching decision file written by an external reviewer (e.g. the review Web UI in
scripts/review.py).
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from openevolve.database import Program


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _metrics_delta(
    parent_metrics: Dict[str, Any], child_metrics: Dict[str, Any]
) -> Dict[str, float]:
    delta = {}
    for key, child_val in child_metrics.items():
        parent_val = parent_metrics.get(key)
        if isinstance(child_val, (int, float)) and isinstance(parent_val, (int, float)):
            delta[key] = child_val - parent_val
    return delta


@dataclass
class ReviewDecision:
    approved: bool
    feedback: str = ""
    # witness index (str, since it round-trips through JSON) -> developer's
    # approve/reject call for that individual transformation witness. Separate
    # from `approved`, which gates the whole child program; this gates whether
    # each witness's formula is trusted for downstream use (e.g. feeding a
    # semantic checker) regardless of whether the iteration itself was kept.
    witness_decisions: Dict[str, bool] = field(default_factory=dict)


class ReviewGate:
    """Writes per-iteration review tasks and blocks until a developer decides"""

    def __init__(self, queue_dir: str, timeout: Optional[float] = None):
        self.queue_dir = Path(queue_dir).expanduser().resolve()
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout

    async def _write_task_and_await_decision(
        self, iteration: int, task_payload: Dict[str, Any]
    ) -> ReviewDecision:
        """Write a review task and block (via polling) until a decision file appears"""
        review_id = task_payload["id"]
        task_path = self.queue_dir / f"{review_id}.json"
        decision_path = self.queue_dir / f"{review_id}.decision.json"

        _atomic_write_json(task_path, task_payload)

        start = time.time()
        poll_interval = 0.5

        while True:
            if decision_path.exists():
                try:
                    data = json.loads(decision_path.read_text(encoding="utf-8"))
                except Exception:
                    await asyncio.sleep(poll_interval)
                    continue

                raw_witness_decisions = data.get("witness_decisions") or {}
                witness_decisions = {
                    str(key): bool(value) for key, value in raw_witness_decisions.items()
                }

                return ReviewDecision(
                    approved=bool(data.get("approved")),
                    feedback=str(data.get("feedback") or ""),
                    witness_decisions=witness_decisions,
                )

            if self.timeout is not None and (time.time() - start) > float(self.timeout):
                raise asyncio.TimeoutError(
                    f"Review gate timed out after {self.timeout}s waiting for a decision on "
                    f"iteration {iteration} (review_id={review_id})"
                )

            await asyncio.sleep(poll_interval)

    async def request_decision(
        self,
        iteration: int,
        parent: Program,
        child: Program,
        changes_summary: str,
        explanation: str = "",
        witnesses: Optional[List[Dict[str, Any]]] = None,
        child_artifacts: Optional[Dict[str, Any]] = None,
        parent_artifacts: Optional[Dict[str, Any]] = None,
    ) -> ReviewDecision:
        """Write a review task for this iteration's already-generated, already-evaluated
        child and wait for a decision"""
        task_payload = {
            "id": str(uuid.uuid4()),
            "created_at": _iso_now(),
            "iteration": iteration,
            "parent_id": parent.id,
            "child_id": child.id,
            "parent_code": parent.code,
            "child_code": child.code,
            "changes_summary": changes_summary,
            "changes_explanation": explanation,
            "changes_description": child.changes_description,
            "witnesses": witnesses or [],
            "child_artifacts": child_artifacts or {},
            # Artifacts stored on the parent from ITS OWN approval, notably any
            # relaxed re-verification (equivalence_relaxed_detail/relaxed_hints,
            # see evaluator.py's reverify_with_witnesses) that ran after the
            # parent was approved -- surfaced here so the developer isn't
            # reviewing this child blind to what was already relaxed/verified
            # upstream in this lineage.
            "parent_artifacts": parent_artifacts or {},
            "parent_metrics": parent.metrics,
            "child_metrics": child.metrics,
            "metrics_delta": _metrics_delta(parent.metrics, child.metrics),
        }
        return await self._write_task_and_await_decision(iteration, task_payload)

    async def request_witness_review(
        self,
        iteration: int,
        parent: Program,
        explanation: str,
        witnesses: List[Dict[str, Any]],
        parent_artifacts: Optional[Dict[str, Any]] = None,
    ) -> ReviewDecision:
        """
        Write a review task for a proposed set of transformation witnesses BEFORE any
        code has been generated or evaluated for them (see openevolve/process_parallel.py's
        two-phase interactive flow: propose -> this review -> implement -> evaluate).

        Same task/decision JSON schema as request_decision, but with the child-specific
        fields left empty since there is no child yet -- the review UI (scripts/review.py,
        scripts/static/js/review.js) already renders empty/missing child_code and
        child_metrics gracefully.
        """
        task_payload = {
            "id": str(uuid.uuid4()),
            "created_at": _iso_now(),
            "iteration": iteration,
            "parent_id": parent.id,
            "child_id": None,
            "parent_code": parent.code,
            "child_code": "",
            "changes_summary": "",
            "changes_explanation": explanation,
            "changes_description": None,
            "witnesses": witnesses or [],
            "child_artifacts": {},
            "parent_artifacts": parent_artifacts or {},
            "parent_metrics": parent.metrics,
            "child_metrics": {},
            "metrics_delta": {},
        }
        return await self._write_task_and_await_decision(iteration, task_payload)
