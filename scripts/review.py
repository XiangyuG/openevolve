"""
Interactive review UI/API for the OpenEvolve visualizer

This module exposes:
  - GET  /review                          (review tasks page)
  - GET  /review/api/tasks                (list tasks, pending only)
  - GET  /review/api/tasks/<id>
  - POST /review/api/tasks/<id>/decision

Task queue directory is assumed to be:
  <run_root>/review_queue

where run_root corresponds to the OpenEvolve run output directory (typically "openevolve_output")
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from flask import Blueprint, jsonify, render_template, request

QUEUE_DIRNAME = "review_queue"


def _resolve_run_root(path_str: str) -> Path:
    """
    Resolve run root from a path that may point to:
      - <run_root>
      - <run_root>/checkpoints
      - <run_root>/checkpoints/checkpoint_123
      - <run_root>/checkpoint_123

    Returns:
      Path to <run_root>
    """
    p = Path(path_str).expanduser().resolve()

    if p.name.startswith("checkpoint_"):
        if p.parent.name == "checkpoints":
            return p.parent.parent
        return p.parent

    if p.name == "checkpoints":
        return p.parent

    return p


def _queue_dir(run_root: Path) -> Path:
    return run_root / QUEUE_DIRNAME


@dataclass
class ReviewTaskItem:
    id: str
    created_at: str
    iteration: Optional[int]
    parent_id: Optional[str]
    child_id: Optional[str]


def _list_tasks(qdir: Path) -> List[ReviewTaskItem]:
    if not qdir.exists():
        return []

    tasks: List[ReviewTaskItem] = []

    for p in sorted(qdir.glob("*.json")):
        if p.name.startswith("."):
            continue
        if p.name.endswith(".decision.json"):
            continue

        task_id = p.stem
        decision = qdir / f"{task_id}.decision.json"
        if decision.exists():
            continue

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        tasks.append(
            ReviewTaskItem(
                id=task_id,
                created_at=str(data.get("created_at") or ""),
                iteration=data.get("iteration"),
                parent_id=data.get("parent_id"),
                child_id=data.get("child_id"),
            )
        )

    return tasks


def _read_task(qdir: Path, task_id: str) -> Optional[Dict]:
    p = qdir / f"{task_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_decision(
    qdir: Path,
    task_id: str,
    approved: bool,
    feedback: str,
    witness_decisions: Optional[Dict[str, bool]] = None,
) -> None:
    qdir.mkdir(parents=True, exist_ok=True)
    out = qdir / f"{task_id}.decision.json"
    tmp = qdir / f".{task_id}.decision.json.tmp"

    payload = {
        "id": task_id,
        "approved": approved,
        "feedback": feedback,
        "witness_decisions": witness_decisions or {},
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out)


def create_review_blueprint(get_visualizer_path: Callable[[], str]) -> Blueprint:
    bp = Blueprint("review", __name__, url_prefix="/review")

    @bp.route("", methods=["GET"], strict_slashes=False)
    @bp.route("/", methods=["GET"], strict_slashes=False)
    def review_page():
        return render_template("review_page.html")

    @bp.get("/api/tasks")
    def api_tasks():
        run_root = _resolve_run_root(get_visualizer_path())
        qdir = _queue_dir(run_root)
        items = _list_tasks(qdir)
        data = [
            {
                "id": t.id,
                "created_at": t.created_at,
                "iteration": t.iteration,
                "parent_id": t.parent_id,
                "child_id": t.child_id,
            }
            for t in items
        ]
        return jsonify({"tasks": data})

    @bp.get("/api/tasks/<task_id>")
    def api_task_detail(task_id: str):
        run_root = _resolve_run_root(get_visualizer_path())
        qdir = _queue_dir(run_root)
        data = _read_task(qdir, task_id)
        if data is None:
            return ("Task not found", 404)

        return jsonify(data)

    @bp.post("/api/tasks/<task_id>/decision")
    def api_task_decision(task_id: str):
        run_root = _resolve_run_root(get_visualizer_path())
        qdir = _queue_dir(run_root)

        if not (qdir / f"{task_id}.json").exists():
            return ("Task not found", 404)

        body = request.get_json(silent=True)
        if body is not None:
            approved = bool(body.get("approved"))
            feedback = str(body.get("feedback") or "").strip()
            raw_witness_decisions = body.get("witness_decisions")
            witness_decisions = (
                {str(k): bool(v) for k, v in raw_witness_decisions.items()}
                if isinstance(raw_witness_decisions, dict)
                else {}
            )
        else:
            approved = (request.form.get("approved") or "").lower() in ("1", "true", "yes")
            feedback = (request.form.get("feedback") or "").strip()
            witness_decisions = {}

        if not approved and not feedback:
            return ("Feedback is required when rejecting an iteration", 400)

        _write_decision(qdir, task_id, approved, feedback, witness_decisions)
        return jsonify({"ok": True})

    return bp
