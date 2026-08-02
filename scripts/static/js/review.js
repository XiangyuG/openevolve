const $ = (sel) => document.querySelector(sel);

const listEl = $("#taskList");
const overlay = $("#modalOverlay");
const modal = $("#reviewModal");
const closeModalBtn = $("#closeModal");
const refreshBtn = $("#refreshBtn");
const notifyBtn = $("#notifyBtn");
const approveBtn = $("#approveBtn");
const rejectBtn = $("#rejectBtn");
const feedbackInput = $("#feedbackInput");

const POLL_INTERVAL_MS = 4000;
const baseTitle = document.title;

let knownTaskIds = null; // null until the first successful fetch
let notifyEnabled = "Notification" in window && Notification.permission === "granted";
let flashTimer = null;

function updateNotifyBtnLabel() {
  notifyBtn.textContent = notifyEnabled ? "🔔 Alerts on" : "🔔 Enable alerts";
}

function startTitleFlash() {
  if (flashTimer) return;
  let toggle = false;
  flashTimer = setInterval(() => {
    document.title = toggle ? baseTitle : "🔔 New iteration to review";
    toggle = !toggle;
  }, 1000);
}

function stopTitleFlash() {
  if (!flashTimer) return;
  clearInterval(flashTimer);
  flashTimer = null;
  document.title = baseTitle;
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.value = 880;
    gain.gain.value = 0.1;
    osc.start();
    setTimeout(() => {
      osc.stop();
      ctx.close();
    }, 200);
  } catch (e) {
    // Audio not available (e.g. autoplay blocked before any user gesture) - ignore
  }
}

function announceNewTasks(newTasks) {
  beep();
  if (!document.hasFocus()) startTitleFlash();

  if (notifyEnabled) {
    const t = newTasks[0];
    const n = new Notification("OpenEvolve: iteration ready for review", {
      body: `Iteration ${t.iteration ?? "?"} (${newTasks.length} pending)`,
      tag: "openevolve-review",
    });
    n.onclick = () => {
      window.focus();
      n.close();
    };
  }
}

window.addEventListener("focus", stopTitleFlash);

const reviewTitle = $("#reviewTitle");
const consistencyBanner = $("#consistencyBanner");
const metricsTable = $("#metricsTable");
const changesExplanation = $("#changesExplanation");
const changesSummary = $("#changesSummary");
const changesDescription = $("#changesDescription");
const changesDescriptionField = $("#changesDescriptionField");
const equivalenceField = $("#equivalenceField");
const equivalenceTable = $("#equivalenceTable");
const witnessesField = $("#witnessesField");
const witnessesList = $("#witnessesList");
const artifactsField = $("#artifactsField");
const artifactsDump = $("#artifactsDump");
const parentCode = $("#parentCode");
const childCode = $("#childCode");

let currentTaskId = null;
// witness index (string) -> true/false. Absent key means "not reviewed yet";
// only entries the developer actually clicked are sent with the decision.
let witnessDecisions = {};

// Relative base: when opened at /review, "api/..." resolves to /review/api/...
const API_BASE = `${window.location.pathname.replace(/\/$/, "")}/api`;

async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function taskCardHtml(t) {
  const shortParent = (t.parent_id || "").slice(0, 8);
  const shortChild = (t.child_id || "").slice(0, 8);
  const card = document.createElement("div");
  card.className = "task-card";
  card.dataset.id = t.id;

  const summary = document.createElement("div");
  summary.className = "task-summary";

  const iterLine = document.createElement("div");
  iterLine.className = "task-iteration";
  iterLine.textContent = `Iteration ${t.iteration ?? "?"}`;
  summary.appendChild(iterLine);

  const idLine = document.createElement("div");
  idLine.className = "task-id";
  idLine.textContent = `parent #${shortParent} → child #${shortChild}`;
  summary.appendChild(idLine);

  const createdLine = document.createElement("div");
  createdLine.className = "task-created";
  createdLine.textContent = t.created_at || "";
  summary.appendChild(createdLine);

  card.appendChild(summary);

  const btnWrap = document.createElement("div");
  const btn = document.createElement("button");
  btn.className = "primary review-btn";
  btn.textContent = "Review";
  btnWrap.appendChild(btn);
  card.appendChild(btnWrap);

  btn.addEventListener("click", () => openTask(t.id));

  return card;
}

async function loadTasks() {
  const data = await fetchJSON(`${API_BASE}/tasks`);

  if (knownTaskIds !== null) {
    const newTasks = data.tasks.filter((t) => !knownTaskIds.has(t.id));
    if (newTasks.length > 0) announceNewTasks(newTasks);
  }
  knownTaskIds = new Set(data.tasks.map((t) => t.id));

  listEl.innerHTML = "";
  if (!data.tasks.length) {
    const empty = document.createElement("div");
    empty.className = "task-card";
    empty.textContent = "No pending iterations to review.";
    listEl.appendChild(empty);
    return;
  }
  data.tasks
    .sort((a, b) => (a.iteration ?? 0) - (b.iteration ?? 0))
    .forEach((t) => listEl.appendChild(taskCardHtml(t)));
}

function formatMetric(v) {
  if (typeof v === "number") return v.toFixed(4);
  return String(v);
}

function renderMetricsTable(parentMetrics, childMetrics, delta) {
  metricsTable.innerHTML = "";
  const addCell = (text, cls) => {
    const el = document.createElement("div");
    if (cls) el.className = cls;
    el.textContent = text;
    metricsTable.appendChild(el);
  };

  addCell("metric", "header");
  addCell("parent", "header");
  addCell("child", "header");
  addCell("delta", "header");

  const keys = Array.from(
    new Set([...Object.keys(parentMetrics || {}), ...Object.keys(childMetrics || {})])
  );

  keys.forEach((key) => {
    addCell(key);
    addCell(key in (parentMetrics || {}) ? formatMetric(parentMetrics[key]) : "-");
    addCell(key in (childMetrics || {}) ? formatMetric(childMetrics[key]) : "-");
    if (delta && key in delta) {
      const d = delta[key];
      addCell(`${d >= 0 ? "+" : ""}${formatMetric(d)}`, d >= 0 ? "delta-pos" : "delta-neg");
    } else {
      addCell("-");
    }
  });
}

function verdictLabel(v) {
  if (v === true) return "✓ equivalent";
  if (v === false) return "✗ NOT equivalent";
  return "? unknown";
}

function verdictClass(v) {
  if (v === true) return "delta-pos";
  if (v === false) return "delta-neg";
  return "";
}

function renderEquivalence(childArtifacts) {
  equivalenceTable.innerHTML = "";
  const raw = childArtifacts && childArtifacts.equivalence_detail;
  if (!raw) {
    equivalenceField.classList.add("hidden");
    return;
  }

  let detail;
  try {
    detail = JSON.parse(raw);
  } catch (e) {
    equivalenceField.classList.add("hidden");
    return;
  }

  const entries = Object.keys(detail);
  if (!entries.length) {
    equivalenceField.classList.add("hidden");
    return;
  }

  entries.forEach((entry) => {
    const row = detail[entry] || {};
    const card = document.createElement("div");
    card.className = "witness-card";

    const head = document.createElement("div");
    head.className = "witness-summary";
    head.textContent = entry;
    const badge = document.createElement("span");
    badge.className = `equiv-badge ${verdictClass(row.equivalent)}`;
    badge.textContent = ` — ${verdictLabel(row.equivalent)} (${row.result_type || "?"})`;
    head.appendChild(badge);
    card.appendChild(head);

    if (row.counter_example) {
      const ce = document.createElement("pre");
      ce.className = "code-viewer readonly witness-formula";
      ce.textContent = row.counter_example;
      card.appendChild(ce);
    }

    equivalenceTable.appendChild(card);
  });

  equivalenceField.classList.remove("hidden");
}

function proofLabel(proof) {
  const status = (proof && proof.status) || "unavailable";
  switch (status) {
    case "trivial":
      return "— structural, no proof needed";
    case "proven_equivalent":
      return "✓ proven equivalent (Z3: unsat)";
    case "counterexample_found":
      return `✗ COUNTEREXAMPLE FOUND: ${proof.detail || ""}`;
    case "parse_error":
      return `✗ does not parse: ${proof.detail || ""}`;
    case "malformed":
      return `✗ malformed: ${proof.detail || ""}`;
    case "unknown":
      return "? solver returned unknown";
    default:
      return "? not validated (z3-solver unavailable)";
  }
}

function proofClass(proof) {
  const status = (proof && proof.status) || "unavailable";
  if (status === "proven_equivalent") return "delta-pos";
  if (status === "counterexample_found" || status === "parse_error" || status === "malformed") {
    return "delta-neg";
  }
  return "";
}

let currentWitnesses = [];

function witnessDecisionLabel(state) {
  if (state === true) return "Approved for next-stage checker";
  if (state === false) return "Rejected -- excluded from next-stage checker";
  return "Not reviewed yet";
}

function setWitnessDecision(index, value) {
  const key = String(index);
  if (witnessDecisions[key] === value) {
    delete witnessDecisions[key]; // clicking the active choice again clears it
  } else {
    witnessDecisions[key] = value;
  }
  renderWitnesses(currentWitnesses);
}

function renderWitnesses(witnesses) {
  currentWitnesses = witnesses || [];
  witnessesList.innerHTML = "";
  if (!witnesses || !witnesses.length) {
    witnessesField.classList.add("hidden");
    return;
  }

  witnesses.forEach((w, i) => {
    const index = w.index ?? i;
    const state = witnessDecisions[String(index)];

    const card = document.createElement("div");
    card.className = "witness-card";

    const summary = document.createElement("div");
    summary.className = "witness-summary";
    summary.textContent = w.summary || "(change)";
    card.appendChild(summary);

    if (w.detail) {
      const detail = document.createElement("div");
      detail.className = "witness-example";
      detail.textContent = w.detail;
      card.appendChild(detail);
    }

    if (w.witness) {
      const witnessText = document.createElement("div");
      witnessText.className = "witness-text";
      witnessText.textContent = w.witness;
      card.appendChild(witnessText);
    }

    if (w.pre_formula || w.post_formula) {
      const preLabel = document.createElement("div");
      preLabel.className = "witness-formula-label";
      preLabel.textContent = "Pre-transformation:";
      card.appendChild(preLabel);

      const pre = document.createElement("pre");
      pre.className = "code-viewer readonly witness-formula";
      pre.textContent = w.pre_formula || "";
      card.appendChild(pre);

      const postLabel = document.createElement("div");
      postLabel.className = "witness-formula-label";
      postLabel.textContent = "Post-transformation:";
      card.appendChild(postLabel);

      const post = document.createElement("pre");
      post.className = "code-viewer readonly witness-formula";
      post.textContent = w.post_formula || "";
      card.appendChild(post);

      const badge = document.createElement("span");
      badge.className = `equiv-badge ${proofClass(w.proof)}`;
      badge.textContent = proofLabel(w.proof);
      card.appendChild(badge);
    }

    const decisionRow = document.createElement("div");
    decisionRow.className = "witness-decision-row";

    const approveWitnessBtn = document.createElement("button");
    approveWitnessBtn.type = "button";
    approveWitnessBtn.className = `witness-decision-btn approve${state === true ? " active" : ""}`;
    approveWitnessBtn.textContent = "✓ Approve witness";
    approveWitnessBtn.addEventListener("click", () => setWitnessDecision(index, true));
    decisionRow.appendChild(approveWitnessBtn);

    const rejectWitnessBtn = document.createElement("button");
    rejectWitnessBtn.type = "button";
    rejectWitnessBtn.className = `witness-decision-btn reject${state === false ? " active" : ""}`;
    rejectWitnessBtn.textContent = "✗ Reject witness";
    rejectWitnessBtn.addEventListener("click", () => setWitnessDecision(index, false));
    decisionRow.appendChild(rejectWitnessBtn);

    const decisionLabel = document.createElement("span");
    decisionLabel.className = "witness-decision-label";
    decisionLabel.textContent = witnessDecisionLabel(state);
    decisionRow.appendChild(decisionLabel);

    card.appendChild(decisionRow);

    witnessesList.appendChild(card);
  });

  witnessesField.classList.remove("hidden");
}

function computeConsistencyMessages(witnesses, childMetrics) {
  const messages = [];
  if (!witnesses || !witnesses.length) return messages;

  const anyCounterexample = witnesses.some(
    (w) => w.proof && w.proof.status === "counterexample_found"
  );
  const allTrivial = witnesses.every(
    (w) =>
      (w.pre_formula || "").trim() === "(assert true)" &&
      (w.post_formula || "").trim() === "(assert true)"
  );
  const anyProvenNonTrivial = witnesses.some(
    (w) => w.proof && w.proof.status === "proven_equivalent"
  );

  let heimdallEquivalent = null;
  if (childMetrics && typeof childMetrics.semantic_equivalent === "number") {
    heimdallEquivalent = childMetrics.semantic_equivalent === 1;
  }

  if (anyCounterexample) {
    messages.push({
      level: "bad",
      text:
        "A witness's own pre/post-transformation formulas produced a Z3 counterexample -- " +
        "the LLM's equivalence claim for that change is disproven by its own formula.",
    });
  }
  if (allTrivial && heimdallEquivalent === false) {
    messages.push({
      level: "bad",
      text:
        "The LLM described every change as purely structural, but the equivalence checker " +
        "(heimdall symbolic execution) found a real divergence. The LLM's self-assessment is " +
        "likely wrong here -- read the Equivalence check counterexample below.",
    });
  }
  if (!allTrivial && heimdallEquivalent === true && anyProvenNonTrivial) {
    messages.push({
      level: "good",
      text:
        "At least one non-trivial witness was proven equivalent by Z3, and the equivalence " +
        "checker independently agrees this candidate is equivalent.",
    });
  }
  return messages;
}

function renderConsistencyBanner(witnesses, childMetrics) {
  const messages = computeConsistencyMessages(witnesses, childMetrics);
  consistencyBanner.innerHTML = "";
  if (!messages.length) {
    consistencyBanner.classList.add("hidden");
    return;
  }

  messages.forEach((m) => {
    const line = document.createElement("div");
    line.className = `consistency-line consistency-${m.level}`;
    line.textContent = m.text;
    consistencyBanner.appendChild(line);
  });
  consistencyBanner.classList.remove("hidden");
}

const ARTIFACT_SKIP_KEYS = new Set(["equivalence_detail"]);

function renderArtifacts(childArtifacts) {
  const entries = Object.entries(childArtifacts || {}).filter(
    ([k]) => !ARTIFACT_SKIP_KEYS.has(k)
  );
  if (!entries.length) {
    artifactsField.classList.add("hidden");
    return;
  }

  const lines = entries.map(([k, v]) => {
    const text = typeof v === "string" ? v : JSON.stringify(v);
    return `# ${k}\n${text}`;
  });
  artifactsDump.textContent = lines.join("\n\n");
  artifactsField.classList.remove("hidden");
}

async function openTask(taskId) {
  stopTitleFlash();
  const data = await fetchJSON(`${API_BASE}/tasks/${taskId}`);
  currentTaskId = data.id;
  witnessDecisions = {};

  reviewTitle.textContent = `Iteration ${data.iteration ?? "?"}`;
  renderMetricsTable(data.parent_metrics || {}, data.child_metrics || {}, data.metrics_delta || {});
  changesExplanation.textContent = data.changes_explanation || "(none)";
  changesSummary.textContent = data.changes_summary || "(none)";
  changesDescriptionField.classList.toggle("hidden", !data.changes_description);
  changesDescription.textContent = data.changes_description || "";
  renderEquivalence(data.child_artifacts || {});
  renderWitnesses(data.witnesses || []);
  renderConsistencyBanner(data.witnesses || [], data.child_metrics || {});
  renderArtifacts(data.child_artifacts || {});
  parentCode.textContent = data.parent_code || "";
  childCode.textContent = data.child_code || "";
  feedbackInput.value = "";

  overlay.classList.remove("hidden");
  modal.classList.remove("hidden");
}

function closeModal() {
  currentTaskId = null;
  overlay.classList.add("hidden");
  modal.classList.add("hidden");
}

async function submitDecision(approved) {
  const feedback = feedbackInput.value.trim();
  if (!approved && !feedback) {
    alert("Feedback is required when rejecting an iteration.");
    return;
  }

  const r = await fetch(`${API_BASE}/tasks/${currentTaskId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved, feedback, witness_decisions: witnessDecisions }),
  });
  if (!r.ok) {
    alert("Submit failed: " + (await r.text()));
    return;
  }
  closeModal();
  await loadTasks();
}

closeModalBtn.addEventListener("click", closeModal);
overlay.addEventListener("click", closeModal);
refreshBtn.addEventListener("click", loadTasks);
approveBtn.addEventListener("click", () => submitDecision(true));
rejectBtn.addEventListener("click", () => submitDecision(false));

notifyBtn.addEventListener("click", async () => {
  if (!("Notification" in window)) {
    alert("This browser does not support notifications.");
    return;
  }
  const perm = await Notification.requestPermission();
  notifyEnabled = perm === "granted";
  updateNotifyBtnLabel();
});

updateNotifyBtnLabel();
window.addEventListener("DOMContentLoaded", loadTasks);
setInterval(loadTasks, POLL_INTERVAL_MS);
