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

function renderWitnesses(witnesses) {
  witnessesList.innerHTML = "";
  if (!witnesses || !witnesses.length) {
    witnessesField.classList.add("hidden");
    return;
  }

  witnesses.forEach((w) => {
    const card = document.createElement("div");
    card.className = "witness-card";

    const summary = document.createElement("div");
    summary.className = "witness-summary";
    summary.textContent = w.summary || "(change)";
    card.appendChild(summary);

    if (w.example) {
      const example = document.createElement("div");
      example.className = "witness-example";
      example.textContent = w.example;
      card.appendChild(example);
    }

    if (w.witness) {
      const witnessText = document.createElement("div");
      witnessText.className = "witness-text";
      witnessText.textContent = w.witness;
      card.appendChild(witnessText);
    }

    if (w.formula) {
      const formula = document.createElement("pre");
      formula.className = "code-viewer readonly witness-formula";
      formula.textContent = w.formula;
      card.appendChild(formula);

      const v = w.validation || {};
      const badge = document.createElement("span");
      let label;
      let cls;
      if (v.parses === "true") {
        label = `✓ parses (${v.check || "unknown"})`;
        cls = v.check === "unsat" ? "delta-neg" : "delta-pos";
      } else if (v.parses === "false") {
        label = `✗ does not parse${v.error ? ": " + v.error : ""}`;
        cls = "delta-neg";
      } else {
        label = "? not validated (z3-solver unavailable)";
        cls = "";
      }
      badge.className = `equiv-badge ${cls}`;
      badge.textContent = label;
      card.appendChild(badge);
    }

    witnessesList.appendChild(card);
  });

  witnessesField.classList.remove("hidden");
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

  reviewTitle.textContent = `Iteration ${data.iteration ?? "?"}`;
  renderMetricsTable(data.parent_metrics || {}, data.child_metrics || {}, data.metrics_delta || {});
  changesExplanation.textContent = data.changes_explanation || "(none)";
  changesSummary.textContent = data.changes_summary || "(none)";
  changesDescriptionField.classList.toggle("hidden", !data.changes_description);
  changesDescription.textContent = data.changes_description || "";
  renderEquivalence(data.child_artifacts || {});
  renderWitnesses(data.witnesses || []);
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
    body: JSON.stringify({ approved, feedback }),
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
