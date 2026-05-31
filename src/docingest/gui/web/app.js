/*
 * Frontend controller — screen state machine + bridge wrappers.
 *
 * Screens are pen top-level frames; one is shown at a time via .is-active.
 * The home screen folds pen 01 (empty) + 02 (selected) into one screen whose
 * sub-state is set with setHomeState(). The Python js_api is reached via
 * window.pywebview.api.<name>(...) (names exposed verbatim — snake_case stays
 * snake_case). Long tasks (ingest) don't return through the Promise; they push
 * to window.__onIngest* hooks defined below.
 *
 * Thin and declarative: wire DOM → bridge, render bridge → DOM. Per-screen
 * rendering is filled in as each screen is built out.
 */

"use strict";

// ---- tiny DOM helpers ----------------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

// A lucide icon placeholder; renderIcons() swaps it to an <svg> later.
function icon(name, className) {
  const i = document.createElement("i");
  i.setAttribute("data-lucide", name);
  if (className) i.className = className;
  return i;
}

function basename(p) {
  return String(p).split(/[\\/]/).pop() || String(p);
}

function extOf(p) {
  const base = basename(p);
  const dot = base.lastIndexOf(".");
  return dot > 0 ? base.slice(dot + 1).toLowerCase() : "";
}

// ---- icons ---------------------------------------------------------------
// lucide replaces <i data-lucide="name"> with an <svg>. Re-run after any DOM
// injection so freshly added icons render too.
function renderIcons() {
  if (window.lucide && typeof window.lucide.createIcons === "function") {
    window.lucide.createIcons();
  }
}

// ---- screen navigation ----------------------------------------------------

function showScreen(name) {
  document.querySelectorAll(".screen").forEach((el) => {
    el.classList.toggle("is-active", el.dataset.screen === name);
  });
}

function showOverlay(name) {
  document.querySelector(`.overlay[data-overlay="${name}"]`)?.classList.add("is-active");
}

function hideOverlay(name) {
  document.querySelector(`.overlay[data-overlay="${name}"]`)?.classList.remove("is-active");
}

// Home sub-state ("empty" | "selected"): show only data-when elements that
// match, hide the rest. Shown elements keep their own display (block/flex).
function setHomeState(stateName) {
  document.body.dataset.homeState = stateName;
  document.querySelectorAll("[data-when]").forEach((el) => {
    el.classList.toggle("is-hidden", el.dataset.when !== stateName);
  });
}

// data-go="<screen>" navigates declaratively. Some screens load data on entry.
document.addEventListener("click", (e) => {
  const go = e.target.closest("[data-go]");
  if (!go) return;
  showScreen(go.dataset.go);
  onScreenEnter(go.dataset.go);
});

// Per-screen entry hook: screens that need fresh backend data load it here.
function onScreenEnter(name) {
  if (name === "settings-env") renderEnvCheck();
  if (name === "settings-model") renderModelSettings();
  if (name === "settings-cost") renderCostSettings();
  if (name === "library") renderLibraryList();
}

// data-back="<screen>" goes back, with optional confirm (data-back-confirm).
// Every screen except home has a back button (home is the root). Going back to
// home from a finished/aborted flow resets the selection so it's a clean start.
document.addEventListener("click", (e) => {
  const back = e.target.closest("[data-back]");
  if (!back) return;
  const confirmMsg = back.dataset.backConfirm;
  if (confirmMsg && !window.confirm(confirmMsg)) return;
  const target = back.dataset.back;
  if (target === "home") resetToHome();
  else showScreen(target);
});

// Reset selection + return to a clean home (used by back-to-home).
function resetToHome() {
  state.paths = [];
  state.inspectRows = [];
  state.inspectMeta = null;
  refreshHomeFromSelection();
  showScreen("home");
}

// ---- bridge --------------------------------------------------------------
// Thin wrappers so screens never touch the raw bridge shape.

const api = {
  pickFiles: () => window.pywebview.api.pick_files(),
  inspect: (paths) => window.pywebview.api.inspect(paths),
  startIngest: (paths, name, options, ack) =>
    window.pywebview.api.start_ingest(paths, name, options || null, !!ack),
  listLibraries: () => window.pywebview.api.list_libraries(),
  getSummary: (dir) => window.pywebview.api.get_summary(dir),
  previewMarkdown: (dir, file) => window.pywebview.api.preview_markdown(dir, file),
  startRefine: (dir, files, skill) => window.pywebview.api.start_refine(dir, files, skill),
  doctor: () => window.pywebview.api.doctor(),
  getSettings: () => window.pywebview.api.get_settings(),
  saveSettings: (s) => window.pywebview.api.save_settings(s),
  openFolder: (path) => window.pywebview.api.open_folder(path),
  graphStatus: (dir) => window.pywebview.api.graph_status(dir),
  startBuildGraph: (dir) => window.pywebview.api.start_build_graph(dir),
};

// ---- app state -----------------------------------------------------------

const state = {
  paths: [],             // selected file paths / URLs
  inspectRows: [],       // inspect().files — per-file rows (+ violations)
  inspectMeta: null,     // {totals, run_violations} from the last inspect()
  libraryName: "",       // user-given name for the new library
  currentLibraryDir: "", // dir of the library shown on the done screen
  // Live status tally accumulated from 03 progress events. The backend stats
  // has no "cached" count, so the done screen (04) reads these front-end
  // tallies instead of inventing a backend field. Reset at ingest start.
  tally: { done: 0, cached: 0, failed: 0, skipped: 0 },
};

// ---- home (01/02) wiring -------------------------------------------------
// Selecting files moves home empty → selected and enables the primary button.
// (File picker is wired through the bridge in the next pass; here the dropzone
// and primary button logic + state transition are in place.)

const btnPrimary = document.getElementById("btn-primary");

// ---- advanced options (pen 02 ProcSettings) ------------------------------
// Data-driven so adding/changing an option touches one table, not code paths.
// Each option declares: how it renders + how it maps to the backend. The
// backend keys/values are the real ones (verified against config/default.yaml
// + cli.py): chunking.strategy / parsing.vision.max_pages / safety.mode /
// outputs whitelist.

// Chunk strategy: backend accepts 7 values; we surface the common 3 in plain
// language (GUI_DESIGN: speak plainly, hide expert/experimental options).
const CHUNK_STRATEGIES = [
  { value: "auto", label: "自動（おまかせ）" },
  { value: "heading", label: "見出しで区切る" },
  { value: "recursive", label: "サイズで区切る" },
];

// safety.mode tri-state — labels match the 07 settings screen exactly.
const SAFETY_MODES = [
  { value: "strict", label: "必ず確認" },
  { value: "warn", label: "警告のみ" },
  { value: "off", label: "確認なし" },
];

// Live option state. (No max-pages knob: parsing.vision.max_pages defaults to
// null on purpose — a hard cap silently drops pages = information loss. Page/
// cost control is the job of the cost dialog + safety thresholds, not a risky,
// easily-misused UI field. Advanced users set it in config if they must.)
const options = {
  strategy: "auto",
  safetyMode: "strict",
  markdownOnly: false,
};

// Build the config the bridge passes to ingest(). Only non-default choices are
// emitted, so the merged config stays minimal and the api defaults stand.
function buildIngestOptions() {
  const overrides = {};
  if (options.strategy && options.strategy !== "auto") {
    overrides["chunking.strategy"] = options.strategy;
  }
  if (options.safetyMode && options.safetyMode !== "strict") {
    overrides["safety.mode"] = options.safetyMode;
  }
  const result = {};
  if (Object.keys(overrides).length) result.config_overrides = overrides;
  // markdown-only → produce only markdown (drops chunks.jsonl).
  if (options.markdownOnly) result.outputs = ["markdown"];
  return result;
}

// Render the option rows into the settings popover body.
function renderProcSettings() {
  const body = document.getElementById("ps-body");
  if (!body) return;
  body.innerHTML = "";

  body.appendChild(
    optRowSelect("チャンク戦略", CHUNK_STRATEGIES, options.strategy, (v) => {
      options.strategy = v;
    })
  );
  body.appendChild(
    optRowSelect("セーフティ", SAFETY_MODES, options.safetyMode, (v) => {
      options.safetyMode = v;
    })
  );
  body.appendChild(
    optRowSwitch("Markdown のみ出力", options.markdownOnly, (v) => {
      options.markdownOnly = v;
    })
  );
}

function rowShell(labelText) {
  const row = el("div", "ps-row row between items-center");
  row.appendChild(el("span", "ps-label", labelText));
  return row;
}

function optRowSelect(labelText, choices, current, onChange) {
  const row = rowShell(labelText);
  const select = el("select", "ps-select");
  for (const c of choices) {
    const o = document.createElement("option");
    o.value = c.value;
    o.textContent = c.label;
    if (c.value === current) o.selected = true;
    select.appendChild(o);
  }
  select.addEventListener("change", () => onChange(select.value));
  row.appendChild(select);
  return row;
}


function optRowSwitch(labelText, current, onChange) {
  const row = rowShell(labelText);
  const sw = el("button", "ps-switch");
  sw.setAttribute("role", "switch");
  sw.setAttribute("aria-checked", String(current));
  sw.classList.toggle("is-on", current);
  sw.appendChild(el("span", "ps-knob"));
  sw.addEventListener("click", () => {
    const next = !sw.classList.contains("is-on");
    sw.classList.toggle("is-on", next);
    sw.setAttribute("aria-checked", String(next));
    onChange(next);
  });
  row.appendChild(sw);
  return row;
}

// Collapse/expand the advanced panel. We toggle a single .is-open class on
// the container; CSS shows/hides the body and rotates the chevron. We do NOT
// swap the lucide icon's data-lucide and re-run createIcons — once lucide has
// replaced <i> with <svg>, re-setting data-lucide isn't reliably re-rendered.
// One fixed chevron + a CSS rotation is robust and avoids that whole class of
// "icon won't update" bugs.
const psToggle = document.getElementById("ps-toggle");
psToggle?.addEventListener("click", (e) => {
  e.stopPropagation(); // don't let the document handler immediately close it
  document.getElementById("proc-settings")?.classList.toggle("is-open");
});
// Click outside the popover (or its trigger) closes it.
document.addEventListener("click", (e) => {
  const pop = document.getElementById("proc-settings");
  if (!pop || !pop.classList.contains("is-open")) return;
  if (e.target.closest(".ps-anchor")) return; // clicks inside anchor/trigger
  pop.classList.remove("is-open");
});

// ---- selected-file list (pen 02 ListWrap) --------------------------------
// Per-format icon (lucide names verified against pen file rows). inspect()
// gives name/format/pages; rows render from state.inspectRows when present,
// else from raw paths before inspection.

const FORMAT_ICONS = {
  pdf: "file-text",
  pptx: "presentation", ppt: "presentation",
  xlsx: "sheet", xls: "sheet", csv: "sheet",
  docx: "file-text", doc: "file-text",
  mp3: "music", wav: "music", m4a: "music", flac: "music",
  mp4: "video", mov: "video", mkv: "video", webm: "video",
  zip: "folder-archive",
};

function formatIcon(fmt) {
  return FORMAT_ICONS[(fmt || "").toLowerCase()] || "file";
}

function metaLabel(row) {
  if (row.duration_sec != null) {
    const m = Math.floor(row.duration_sec / 60);
    const s = String(row.duration_sec % 60).padStart(2, "0");
    return `${m}:${s}`;
  }
  if (row.pages != null) return `${row.pages} ページ`;
  return "";
}

function renderFileList() {
  const list = document.getElementById("file-list");
  if (!list) return;
  list.innerHTML = "";

  // Prefer inspect rows (have format/pages); fall back to bare paths.
  const rows = state.inspectRows.length
    ? state.inspectRows
    : state.paths.map((p) => ({ name: basename(p), format: extOf(p) }));

  rows.forEach((row) => {
    const fileRow = el("div", "file-row row between items-center gap-3");

    const left = el("div", "fr-left row items-center gap-3");
    left.appendChild(icon(formatIcon(row.format), "fr-icon"));
    left.appendChild(el("span", "fr-name", row.name || ""));
    fileRow.appendChild(left);

    // Key the row by display name (present on both inspectRows and the
    // path-derived fallback). Deleting by index is unsafe because paths and
    // inspectRows can diverge in length/order; name is the stable identity
    // shown to the user, and we remove every matching entry from both arrays.
    fileRow.dataset.name = row.name || "";

    const right = el("div", "fr-right row items-center gap-4");
    const meta = metaLabel(row);
    if (meta) right.appendChild(el("span", "fr-meta mono", meta));
    // The × is a lucide icon that renderIcons() will replace with an <svg>,
    // so we DON'T bind a listener on it here (it would be lost on replace).
    // Removal is handled by a single delegated listener on #file-list below.
    right.appendChild(icon("x", "fr-x clickable"));
    fileRow.appendChild(right);

    list.appendChild(fileRow);
  });
  renderIcons();
}

// Delegated × removal: survives lucide's <i>→<svg> swap (a listener bound to
// the icon node itself would be discarded when the node is replaced).
document.getElementById("file-list")?.addEventListener("click", (e) => {
  const x = e.target.closest(".fr-x");
  if (!x) return;
  const rowEl = x.closest(".file-row");
  const name = rowEl ? rowEl.dataset.name : "";
  if (!name) return;
  // Remove by name from both arrays (they may differ in length/order). basename
  // of each path is compared so a path and its inspect row match by file name.
  state.paths = state.paths.filter((p) => basename(p) !== name);
  state.inspectRows = state.inspectRows.filter((r) => r.name !== name);
  refreshHomeFromSelection();
});

function refreshHomeFromSelection() {
  const has = state.paths.length > 0;
  setHomeState(has ? "selected" : "empty");
  if (btnPrimary) btnPrimary.disabled = !has;

  const count = document.getElementById("list-count");
  if (count) count.textContent = `選択中 · ${state.paths.length} 件`;

  if (has) {
    renderFileList();
    renderProcSettings();
  }
}

document.getElementById("list-clear")?.addEventListener("click", () => {
  state.paths = [];
  state.inspectRows = [];
  refreshHomeFromSelection();
});

// Dropzone → native multi-select file picker (pywebview). Adds to selection.
document.getElementById("dropzone")?.addEventListener("click", async () => {
  try {
    const picked = await api.pickFiles();
    if (picked && picked.length) {
      for (const p of picked) if (!state.paths.includes(p)) state.paths.push(p);
      state.inspectRows = []; // selection changed → previous inspect is stale
      state.inspectMeta = null;
      refreshHomeFromSelection();
    }
  } catch (err) {
    console.error("file pick failed", err);
  }
});

// Native drag-and-drop: gui_app's Python drop handler resolves real file
// paths (the browser can't) and pushes them here. They join the selection
// like picked files. Visual hover feedback is handled by the dragover/leave
// listeners below.
window.__onFilesDropped = function (paths) {
  if (Array.isArray(paths) && paths.length) addInputs(paths);
  document.getElementById("dropzone")?.classList.remove("is-dragover");
};

const _dz = document.getElementById("dropzone");
if (_dz) {
  // Prevent the browser default + show a hover state. The actual file paths
  // arrive via __onFilesDropped (Python side); these are just for feedback.
  ["dragenter", "dragover"].forEach((ev) =>
    _dz.addEventListener(ev, (e) => {
      e.preventDefault();
      _dz.classList.add("is-dragover");
    })
  );
  ["dragleave", "drop"].forEach((ev) =>
    _dz.addEventListener(ev, (e) => {
      e.preventDefault();
      _dz.classList.remove("is-dragover");
    })
  );
}

// Add inputs (files or a URL) to the selection, dropping duplicates and
// invalidating a stale inspect. Shared by file pick and URL add.
function addInputs(items) {
  let added = false;
  for (const it of items) {
    if (it && !state.paths.includes(it)) { state.paths.push(it); added = true; }
  }
  if (added) {
    state.inspectRows = [];
    state.inspectMeta = null;
    refreshHomeFromSelection();
  }
}

// URL input: ingest/inspect accept http(s) URLs (api._is_url), so the URL
// field is a real input. Enter or the + button adds it to the selection.
const urlField = document.getElementById("url-field");
function commitUrl() {
  if (!urlField) return;
  const v = urlField.value.trim();
  if (!/^https?:\/\//i.test(v)) return; // only accept real URLs
  addInputs([v]);
  urlField.value = "";
}
urlField?.addEventListener("keydown", (e) => {
  if (e.key === "Enter") commitUrl();
});
document.getElementById("url-add")?.addEventListener("click", commitUrl);

// inspect → open the cost dialog (09). Shared by the primary button and the
// "処理コストを確認" link (both want pre-flight + the cost dialog).
async function inspectAndShowCost() {
  if (!state.paths.length) return;
  try {
    const data = await api.inspect(state.paths);
    state.inspectRows = data.files || [];
    state.inspectMeta = { totals: data.totals, run_violations: data.run_violations };
    renderFileList();
    renderCostDialog();
    showOverlay("cost-confirm");
  } catch (err) {
    console.error("inspect failed", err);
  }
}

// Primary action — disabled in empty state, so a click always has a selection.
btnPrimary?.addEventListener("click", inspectAndShowCost);

// "処理コストを確認" link (pen EstLink) — same pre-flight + cost dialog.
document.getElementById("est-link")?.addEventListener("click", inspectAndShowCost);

// ---- cost confirm dialog (09) --------------------------------------------
// Renders the inspect rows + totals + over-budget notice, then on confirm
// kicks off ingest with acknowledge_large=True (user has seen the cost).

function fmtCost(usd) {
  return `$${(usd || 0).toFixed(4)}`;
}

// Human label for a violation dimension (pen: 上限超過 + a per-row tag).
const METRIC_LABELS = {
  pages: "ページ", total_pages: "合計ページ",
  est_cost_usd: "コスト", total_est_cost_usd: "合計コスト",
  rows: "行数", chars_est: "文字数",
  duration_sec: "長さ", size_mb: "サイズ", total_files: "ファイル数",
};

function renderCostDialog() {
  const overlay = document.querySelector('.overlay[data-overlay="cost-confirm"]');
  if (!overlay) return;
  const tbody = overlay.querySelector("#cost-rows");
  const totalEl = overlay.querySelector("#cost-total");
  const totalLabel = overlay.querySelector("#cost-total-label");
  const warnEl = overlay.querySelector("#cost-warn");
  if (!tbody) return;

  tbody.innerHTML = "";
  for (const row of state.inspectRows) {
    const over = (row.violations || []).length > 0;
    const r = el("div", "cost-row row between items-center");

    const left = el("div", "cr-left row items-center gap-3");
    left.appendChild(el("span", "cr-name", row.name || ""));
    if (over) left.appendChild(el("span", "cr-tag", "上限超過"));
    r.appendChild(left);

    const right = el("div", "cr-right row items-center gap-5");
    const meta = metaLabel(row);
    if (meta) right.appendChild(el("span", "cr-meta mono", meta));
    right.appendChild(el("span", "cr-cost mono", fmtCost(row.est_cost_usd)));
    r.appendChild(right);

    tbody.appendChild(r);
  }

  const totals = state.inspectMeta?.totals || { pages: 0, est_cost_usd: 0 };
  if (totalLabel) totalLabel.textContent = `合計（${totals.pages} ページ）`;
  if (totalEl) totalEl.textContent = fmtCost(totals.est_cost_usd);

  // Over-budget notice: prefer run-level (pen bottom line), else any file viol.
  const runV = state.inspectMeta?.run_violations || [];
  const fileV = state.inspectRows.some((r) => (r.violations || []).length);
  if (warnEl) {
    if (runV.length || fileV) {
      const overCount = state.inspectRows.filter((r) => (r.violations || []).length).length;
      // Use a representative per-file threshold for the message (pen shows the
      // page cap). Fall back to a generic line when only run-level trips.
      const firstFileViol = state.inspectRows
        .flatMap((r) => r.violations || [])
        .find((v) => v.metric === "pages");
      let msg;
      if (overCount && firstFileViol) {
        msg = `${overCount} 件が予算上限（${firstFileViol.threshold} ${METRIC_LABELS[firstFileViol.metric] || ""}）を超えています。続行するには確認が必要です。`;
      } else if (runV.length) {
        const v = runV[0];
        msg = `実行全体が上限（${v.threshold} ${METRIC_LABELS[v.metric] || ""}）を超えています。続行するには確認が必要です。`;
      } else {
        msg = "一部のファイルが予算上限を超えています。続行するには確認が必要です。";
      }
      warnEl.querySelector(".cw-text").textContent = msg;
      warnEl.classList.remove("is-hidden");
    } else {
      warnEl.classList.add("is-hidden");
    }
  }
  renderIcons();
}

document.getElementById("cost-cancel")?.addEventListener("click", () =>
  hideOverlay("cost-confirm")
);

document.getElementById("cost-confirm-btn")?.addEventListener("click", async () => {
  hideOverlay("cost-confirm");
  showScreen("processing");
  initProcessing();
  try {
    const name = state.libraryName || deriveLibraryName(state.paths);
    await api.startIngest(state.paths, name, buildIngestOptions(), true);
  } catch (err) {
    console.error("start ingest failed", err);
  }
});

// A friendly default library name from the first selection (user can rename
// later in the flow; pen has no name field on home, so we derive one).
function deriveLibraryName(paths) {
  if (!paths.length) return "library";
  return basename(paths[0]).replace(/\.[^.]+$/, "") || "library";
}

// ---- processing screen (03) ----------------------------------------------
// Backend emits one file_done event per file (completion-only, file-level —
// no mid-file stage). So we pre-list every file as 待機中, then on each event
// mark that file done (by real status) and the NEXT one 処理中. The "処理中"
// state is inferred (current+1), not claimed from the backend — honest.

// Map a backend file_done status → row visual state.
//   added/updated/forced → done ; cached → cached ; failed → failed ;
//   skipped → skipped. (See pipeline _emit_progress statuses.)
function statusToState(status) {
  if (status === "failed") return "failed";
  if (status === "cached") return "cached";
  if (status === "skipped") return "skipped";
  return "done"; // added / updated / forced
}

const PROC_STATE_ICON = {
  done: "check", cached: "zap", active: "loader",
  failed: "x", skipped: "circle", waiting: "circle",
};

// Pages come from the earlier inspect() (event has chunks but no pages).
function pagesForFile(name) {
  const row = state.inspectRows.find((r) => r.name === name);
  return row && row.pages != null ? row.pages : null;
}

function procRowInfo(stateName, ev, fileName) {
  switch (stateName) {
    case "done": {
      const pg = pagesForFile(fileName);
      const pages = pg != null ? `${pg} ページ · ` : "";
      return `${pages}${ev ? ev.chunks || 0 : 0} chunks`;
    }
    case "cached": return "キャッシュ再利用";
    case "failed": return "解析に失敗";
    case "skipped": return "スキップ";
    case "active": return "処理中…";
    default: return "待機中";
  }
}

// Build initial 待機中 rows from the selection (one per file, by basename).
function initProcessing() {
  const list = document.getElementById("proc-list");
  if (!list) return;
  state.tally = { done: 0, cached: 0, failed: 0, skipped: 0 }; // fresh run
  list.innerHTML = "";
  const names = state.paths.map((p) => basename(p));
  names.forEach((name) => {
    const row = el("div", "p-row is-waiting row between items-center gap-3");
    row.dataset.file = name;
    const left = el("div", "pr-left row items-center gap-3");
    left.appendChild(icon(PROC_STATE_ICON.waiting, "pr-icon"));
    left.appendChild(el("span", "pr-name", name));
    row.appendChild(left);
    row.appendChild(el("span", "pr-info", "待機中"));
    list.appendChild(row);
  });
  // First file is the one being processed once ingest starts.
  setProcRowState(0, "active");
  updateProcHeader(0, names.length);
  renderIcons();
}

function setProcRowState(index, stateName, ev) {
  const rows = document.querySelectorAll("#proc-list .p-row");
  const row = rows[index];
  if (!row) return;
  row.className = `p-row is-${stateName} row between items-center gap-3`;
  const ic = row.querySelector(".pr-icon");
  if (ic) {
    ic.setAttribute("data-lucide", PROC_STATE_ICON[stateName] || "circle");
  }
  const info = row.querySelector(".pr-info");
  if (info) info.textContent = procRowInfo(stateName, ev, row.dataset.file);
  renderIcons();
}

function updateProcHeader(done, total) {
  const sub = document.getElementById("proc-sub");
  if (sub) sub.textContent = `${done} / ${total} 件 完了`;
  const fill = document.getElementById("proc-fill");
  if (fill) fill.style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
}

// ---- ingest progress hooks (pushed from Python via evaluate_js) ----------

window.__onIngestProgress = function (event) {
  // event: {kind,status,file,current,total,chunks,elapsed_ms,error,error_type}
  // Mark the completed file (matched by basename) with its real status. Files
  // may complete out of list order (parallel processing), so we do NOT infer
  // "next = index current" — instead, after marking done, the first row still
  // 待機中 becomes 処理中 (only while files remain). Honest: we never claim a
  // specific file is mid-process beyond "the next pending one".
  const rows = Array.from(document.querySelectorAll("#proc-list .p-row"));
  const idx = rows.findIndex((r) => r.dataset.file === event.file && !r.dataset.settled);
  if (idx >= 0) {
    const st = statusToState(event.status);
    setProcRowState(idx, st, event);
    rows[idx].dataset.settled = "1"; // completed rows are never re-touched
    if (state.tally[st] != null) state.tally[st] += 1; // for the done screen
  }
  // Exactly one 処理中 marker: the first not-yet-settled row. We can't know
  // the backend's real parallelism, so we don't fake N concurrent rows —
  // one honest "next up" marker, the rest 待機中. Clear any stale active first.
  rows.forEach((r) => {
    if (!r.dataset.settled && r.classList.contains("is-active")) {
      setProcRowState(rows.indexOf(r), "waiting");
    }
  });
  if (event.current < event.total) {
    const next = rows.find((r) => !r.dataset.settled);
    if (next) setProcRowState(rows.indexOf(next), "active");
  }
  updateProcHeader(event.current, event.total);
};

window.__onIngestDone = function (summary) {
  state.currentLibraryDir = summary.output_dir;
  renderDone(summary);
  showScreen("done");
};

window.__onIngestError = function (message) {
  console.error("ingest error →", message);
};

// ---- done screen (04) ----------------------------------------------------
// Fills stats (front-end tally for the cached count the backend doesn't
// provide), notices (warnings + unreadable), artifact descriptions, and the
// preview file selector. summary = {output_dir, stats, summary:get_summary()}.

function renderDone(summary) {
  const stats = summary.stats || {};
  const lib = summary.summary || {};
  const libStats = lib.stats || {};

  // Stats: done/failed from backend stats; cached from front-end tally (no
  // backend field); cost from the pre-flight total we showed at confirm time.
  setText("m-done", String(stats.successful != null ? stats.successful : state.tally.done));
  setText("m-cached", String(state.tally.cached));
  setText("m-failed", String(stats.failed != null ? stats.failed : state.tally.failed));
  const cost = state.inspectMeta?.totals?.est_cost_usd;
  setText("m-cost", cost != null ? `$${cost.toFixed(4)}` : "—");
  // Failed metric turns alert-colored only when there ARE failures.
  const mf = document.getElementById("m-failed");
  if (mf) mf.style.color = (stats.failed || state.tally.failed) ? "var(--danger)" : "";

  // Artifact descriptions from the real library summary.
  const fileCount = libStats.total_files != null ? libStats.total_files : (lib.files || []).length;
  setText("art-sources-desc", `${fileCount} 件の Markdown`);
  setText("art-chunks-desc", `${libStats.total_chunks || 0} チャンク`);

  renderNotices(stats, lib);
  renderPreviewSelector(lib);
  renderIcons();
}

// NoticeBox: backend warnings (page-cap, OCR downgrade, ...) + unreadable
// spots from quality. Hidden entirely when there's nothing to report.
function renderNotices(stats, lib) {
  const box = document.getElementById("done-notices");
  if (!box) return;
  box.innerHTML = "";
  const rows = [];

  for (const w of stats.warnings || []) {
    const file = w.file ? `${w.file}：` : "";
    rows.push({ icon: "triangle-alert", cls: "is-warn", text: `${file}${w.message || ""}` });
  }
  const unreadable = lib.quality?.total_unreadable;
  if (unreadable) {
    rows.push({ icon: "eye-off", cls: "is-muted", text: `${unreadable} か所、読み取れない箇所があります` });
  }

  if (!rows.length) {
    box.classList.add("is-hidden");
    return;
  }
  box.classList.remove("is-hidden");
  for (const r of rows) {
    const row = el("div", "notice-row row items-center gap-3");
    row.appendChild(icon(r.icon, `nr-icon ${r.cls}`));
    row.appendChild(el("span", "nr-text", r.text));
    box.appendChild(row);
  }
}

// Preview selector: list sources/*.md from the summary; load on change.
function renderPreviewSelector(lib) {
  const sel = document.getElementById("pv-select");
  const body = document.getElementById("pv-body");
  if (!sel || !body) return;
  sel.innerHTML = "";
  body.textContent = "";

  // index.json file entries carry "path" = "sources/<name>.md" (verified in
  // output/index_builder.py). The preview reads that markdown by basename;
  // gui_logic.preview_markdown resolves it under the library's sources/.
  const names = (lib.files || [])
    .map((f) => f.path)
    .filter((p) => typeof p === "string" && p.toLowerCase().endsWith(".md"))
    .map((p) => basename(p));

  if (!names.length) {
    body.textContent = "（プレビューできるファイルがありません）";
    return;
  }
  for (const n of names) {
    const o = document.createElement("option");
    o.value = n;
    o.textContent = basename(n);
    sel.appendChild(o);
  }
  loadPreview(names[0]);
  sel.addEventListener("change", () => loadPreview(sel.value));
}

async function loadPreview(filename) {
  const body = document.getElementById("pv-body");
  if (!body || !state.currentLibraryDir) return;
  try {
    const md = await api.previewMarkdown(state.currentLibraryDir, basename(filename));
    if (!md) {
      body.textContent = "（内容が空です）";
      return;
    }
    body.innerHTML = renderMarkdown(md);
  } catch (err) {
    body.textContent = "（プレビューの読み込みに失敗しました）";
  }
}

// MD → sanitized HTML. Strips YAML frontmatter (the sources/*.md files start
// with a --- block) so the preview shows content, not metadata. Falls back to
// plain text if the libs didn't load (e.g. offline dev with no CDN).
function renderMarkdown(md) {
  const body = stripFrontmatter(md);
  if (window.marked && window.DOMPurify) {
    return window.DOMPurify.sanitize(window.marked.parse(body));
  }
  // Fallback: escape + preserve line breaks.
  const esc = body.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  return `<pre style="white-space:pre-wrap">${esc}</pre>`;
}

function stripFrontmatter(md) {
  if (md.startsWith("---\n")) {
    const end = md.indexOf("\n---\n", 4);
    if (end !== -1) return md.slice(end + 5);
  }
  return md;
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

// Done-screen action buttons.
document.getElementById("done-open")?.addEventListener("click", () => {
  if (state.currentLibraryDir) api.openFolder(state.currentLibraryDir);
});
document.getElementById("done-graph")?.addEventListener("click", () => showScreen("graph-empty"));

// ---- refine-style dialog (10) --------------------------------------------
// "読みやすく整形" opens the dialog; pick a skill; 整形する runs refine on the
// CURRENTLY PREVIEWED file (focused, cost-controlled). refine is slow (LLM per
// file) and has no per-file progress, so we show a "整形中…" state on the
// button and surface a terminal result.

document.getElementById("done-refine")?.addEventListener("click", () => {
  if (!currentPreviewFile()) return; // nothing selected to refine
  showOverlay("refine-style");
});

// Option-card selection (single-select; default 原文に忠実).
document.getElementById("refine-opts")?.addEventListener("click", (e) => {
  const opt = e.target.closest(".refine-opt");
  if (!opt) return;
  document.querySelectorAll("#refine-opts .refine-opt").forEach((o) =>
    o.classList.toggle("is-selected", o === opt)
  );
});

document.getElementById("refine-cancel")?.addEventListener("click", () =>
  hideOverlay("refine-style")
);

const refineGoBtn = document.getElementById("refine-go");
refineGoBtn?.addEventListener("click", async () => {
  const selected = document.querySelector("#refine-opts .refine-opt.is-selected");
  const skill = selected ? selected.dataset.skill : "refine_faithful";
  const file = currentPreviewFile();
  if (!file || !state.currentLibraryDir) return;

  refineGoBtn.disabled = true;
  refineGoBtn.textContent = "整形中…";
  try {
    await api.startRefine(state.currentLibraryDir, [file], skill);
  } catch (err) {
    console.error("start refine failed", err);
    resetRefineButton();
  }
});

function resetRefineButton() {
  if (!refineGoBtn) return;
  refineGoBtn.disabled = false;
  refineGoBtn.textContent = "整形する";
}

// The file currently shown in the 04 preview selector (refine target).
function currentPreviewFile() {
  const sel = document.getElementById("pv-select");
  return sel && sel.value ? sel.value : null;
}

window.__onRefineDone = function (result) {
  hideOverlay("refine-style");
  resetRefineButton();
  // Best-effort: tell the user it's done. readable/ output now exists on disk.
  const n = (result && result.files && result.files.length) || 0;
  window.alert(`整形が完了しました（${n} 件）。readable フォルダに保存されました。`);
};

window.__onRefineError = function (message) {
  resetRefineButton();
  console.error("refine error →", message);
  window.alert("整形に失敗しました。");
};

// ---- knowledge graph build (11 → 12) -------------------------------------
// Build runs on a background thread (LLM per chunk). Progress is per-chunk
// (backend gives no "stage" events — LightRAG is a black box), so screen 12
// shows an honest chunk count + bar, NOT the four-stage mockup in the pen.

document.getElementById("graph-build")?.addEventListener("click", async () => {
  if (!state.currentLibraryDir) return;
  showScreen("graph-building");
  setGraphProgress(0, 0);
  try {
    await api.startBuildGraph(state.currentLibraryDir);
  } catch (err) {
    console.error("start build graph failed", err);
  }
});

function setGraphProgress(current, total) {
  const sub = document.getElementById("graph-sub");
  const fill = document.getElementById("graph-fill");
  if (sub) {
    sub.textContent = total
      ? `チャンクを処理中… ${current} / ${total}`
      : "準備中…";
  }
  if (fill) fill.style.width = total ? `${Math.round((current / total) * 100)}%` : "0%";
}

window.__onGraphProgress = function (event) {
  // event: {current, total, chunk_id, status}
  setGraphProgress(event.current || 0, event.total || 0);
};

window.__onGraphDone = function (result) {
  const e = result.entities || 0;
  const r = result.relations || 0;
  window.alert(`知識グラフを構築しました（用語 ${e} / 関係 ${r}）。`);
  showScreen("done");
};

window.__onGraphError = function (message) {
  console.error("graph build error →", message);
  window.alert("知識グラフの構築に失敗しました。");
  showScreen("done");
};

// ---- library list (history) ----------------------------------------------
// Lists processed libraries (list_knowledge) and opens one in screen 04.

async function renderLibraryList() {
  const list = document.getElementById("lib-list");
  if (!list) return;
  list.innerHTML = '<span class="lib-empty">読み込み中…</span>';
  try {
    const libs = await api.listLibraries();
    list.innerHTML = "";
    if (!libs.length) {
      list.innerHTML = '<span class="lib-empty">まだナレッジベースがありません。</span>';
      return;
    }
    for (const lib of libs) {
      const card = el("div", "lib-card row between items-center gap-3");
      const left = el("div", "lc-left col gap-1");
      left.appendChild(el("span", "lib-name", lib.display_name || lib.name));
      const parts = [];
      if (lib.files != null) parts.push(`${lib.files} ファイル`);
      if (lib.chunks != null) parts.push(`${lib.chunks} チャンク`);
      if (lib.created_at) parts.push(String(lib.created_at).slice(0, 10));
      left.appendChild(el("span", "lib-meta", parts.join(" · ")));
      card.appendChild(left);
      card.appendChild(icon("chevron-right", "lib-chev"));
      card.addEventListener("click", () => openLibrary(lib.dir));
      list.appendChild(card);
    }
    renderIcons();
  } catch (err) {
    console.error("list libraries failed", err);
    list.innerHTML = '<span class="lib-empty">読み込みに失敗しました。</span>';
  }
}

// Open an existing library in screen 04. Unlike a just-finished ingest, there
// are no run stats here (cached/failed/cost can't be reconstructed) — we reset
// the run tally + cost so the done screen shows the library's real counts and
// honest "—" for cost, not stale values from a previous ingest.
async function openLibrary(dir) {
  try {
    const summary = await api.getSummary(dir);
    state.currentLibraryDir = dir;
    state.tally = { done: summary.stats?.total_files || 0, cached: 0, failed: 0, skipped: 0 };
    state.inspectMeta = null;
    renderDone({ output_dir: dir, stats: {}, summary });
    showScreen("done");
  } catch (err) {
    console.error("open library failed", err);
  }
}

// ---- boot ----------------------------------------------------------------

function boot() {
  renderProcSettings(); // settings popover is always present now, render once
  renderIcons();
  setHomeState("empty");
}

// ---- settings: environment check (05) ------------------------------------
// Pure doctor() read. Japanese descriptions are display copy (pen labels),
// keyed by the real env-var / tool names doctor returns; status comes from
// doctor's set/ok flags. No backend change.

const ENV_KEY_DESC = {
  GEMINI_API_KEY: "Vision AI（既定）",
  DASHSCOPE_API_KEY: "音声文字起こし（Qwen3-ASR）",
  OPENAI_API_KEY: "フォールバック",
};
const ENV_TOOL_DESC = {
  LibreOffice: "Excel / Word / PPT の Vision 変換",
  ffmpeg: "音声・動画の抽出と分割",
  ExifTool: "ファイルメタデータ抽出（任意）",
};

function checkRow(name, desc, ok) {
  const row = el("div", "check-row row between items-center gap-3");
  const left = el("div", "cr-left col");
  left.appendChild(el("span", "cr-name", name));
  if (desc) left.appendChild(el("span", "cr-desc", desc));
  row.appendChild(left);

  const st = el("div", `cr-status row items-center gap-2 ${ok ? "is-ok" : "is-missing"}`);
  if (ok) st.appendChild(icon("check", "cs-icon"));
  st.appendChild(el("span", "cs-text", ok ? "検出済み" : "未設定"));
  row.appendChild(st);
  return row;
}

// doctor() is slow (~12s: it imports every dependency to check presence). The
// environment doesn't change within a session, so we cache the first result
// and reuse it on re-entry. First load shows a "確認中…" state instead of
// blocking — doctor runs async and the rows fill in when it returns.
let _doctorCache = null;

function paintEnvCheck(d) {
  const keysEl = document.getElementById("env-keys");
  const toolsEl = document.getElementById("env-tools");
  if (!keysEl || !toolsEl) return;
  keysEl.innerHTML = "";
  toolsEl.innerHTML = "";
  for (const k of ["GEMINI_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"]) {
    const info = (d.api_keys || {})[k];
    if (!info) continue;
    keysEl.appendChild(checkRow(k, ENV_KEY_DESC[k] || info.purpose || "", !!info.set));
  }
  for (const t of ["LibreOffice", "ffmpeg", "ExifTool"]) {
    const info = (d.tools || {})[t];
    if (!info) continue;
    toolsEl.appendChild(checkRow(t, ENV_TOOL_DESC[t] || info.purpose || "", !!info.ok));
  }
  renderIcons();
}

function envLoading() {
  const keysEl = document.getElementById("env-keys");
  const toolsEl = document.getElementById("env-tools");
  if (keysEl) keysEl.innerHTML = '<span class="env-loading">確認中…</span>';
  if (toolsEl) toolsEl.innerHTML = "";
}

async function renderEnvCheck() {
  if (_doctorCache) {
    paintEnvCheck(_doctorCache); // instant on re-entry
    return;
  }
  envLoading();
  try {
    _doctorCache = await api.doctor();
    // Guard: only paint if the user is still on this screen.
    if (document.querySelector('.screen[data-screen="settings-env"].is-active')) {
      paintEnvCheck(_doctorCache);
    }
  } catch (err) {
    console.error("doctor failed", err);
    const keysEl = document.getElementById("env-keys");
    if (keysEl) keysEl.innerHTML = '<span class="env-loading">確認に失敗しました</span>';
  }
}

// ---- settings: AI model (06) ---------------------------------------------
// Model dropdowns use plain-language tiers mapped to REAL model IDs (verified
// against config/default.yaml). "既定" = the backend's actual default (Vision
// defaults to flash, so 高速 is the default — pen's "高精度（既定）" label was
// wrong; we follow the source of truth). Changing a dropdown or a key saves
// immediately via save_settings (no Save button in the pen).
//
// Tier value = the config model ID. Saved as a flat dotted key; build_config
// expands dotted keys into nested overrides (see api._normalize_overrides).
const VISION_TIERS = [
  { value: "gemini-3-flash-preview", label: "高速（既定）" },
  { value: "gemini-3-pro-preview", label: "高精度" },
];
const ASR_TIERS = [
  { value: "qwen3-asr-flash", label: "標準（既定）" },
];
const VISION_KEY = "models.vision.primary.model";
const ASR_KEY = "models.audio_transcription.primary.model";

const KEY_FIELDS = [
  { env: "GEMINI_API_KEY", cfg: "GEMINI_API_KEY" },
  { env: "DASHSCOPE_API_KEY", cfg: "DASHSCOPE_API_KEY" },
  { env: "OPENAI_API_KEY", cfg: "OPENAI_API_KEY" },
];

function fillTierSelect(selectEl, tiers, currentValue) {
  selectEl.innerHTML = "";
  for (const t of tiers) {
    const o = document.createElement("option");
    o.value = t.value;
    o.textContent = t.label;
    if (t.value === currentValue) o.selected = true;
    selectEl.appendChild(o);
  }
}

async function renderModelSettings() {
  const vSel = document.getElementById("model-vision");
  const aSel = document.getElementById("model-asr");
  const keysEl = document.getElementById("model-keys");
  if (!vSel || !aSel || !keysEl) return;

  let settings = {};
  let doctorData = {};
  try {
    settings = (await api.getSettings()) || {};
    // Reuse the env-check doctor cache (doctor() is slow); run once if cold.
    if (!_doctorCache) _doctorCache = await api.doctor();
    doctorData = _doctorCache;
  } catch (err) {
    console.error("load model settings failed", err);
  }

  // Dropdowns: show saved value if present, else the default (first tier).
  fillTierSelect(vSel, VISION_TIERS, settings[VISION_KEY] || VISION_TIERS[0].value);
  fillTierSelect(aSel, ASR_TIERS, settings[ASR_KEY] || ASR_TIERS[0].value);
  vSel.onchange = () => saveSetting(VISION_KEY, vSel.value);
  aSel.onchange = () => saveSetting(ASR_KEY, aSel.value);

  // Key rows: "set / not set" from doctor (covers .env); editable, saves to
  // config.yaml. We never display .env key values (security + get_settings
  // can't read them) — only whether they're detected.
  keysEl.innerHTML = "";
  const apiKeys = doctorData.api_keys || {};
  for (const f of KEY_FIELDS) {
    const detected = !!(apiKeys[f.env] && apiKeys[f.env].set);
    keysEl.appendChild(keyRow(f, detected));
  }
  renderIcons();
}

function keyRow(field, detected) {
  const wrap = el("div", "key-row col gap-2");
  wrap.appendChild(el("span", "kr-label", field.env));

  const box = el("div", "kr-box row items-center gap-2");
  const input = el("input", "kr-input");
  input.type = "password";
  // Detected (incl. via .env) → show a masked placeholder, don't reveal value.
  // Not detected → invite input.
  input.placeholder = detected ? "検出済み（変更する場合のみ入力）" : "未設定（任意）";
  box.appendChild(input);

  const state = el("i", `kr-state ${detected ? "is-ok" : "is-muted"}`);
  state.setAttribute("data-lucide", detected ? "check" : "eye");
  box.appendChild(state);
  wrap.appendChild(box);

  // Save on change/blur — only when the user actually typed something (empty
  // input must NOT wipe an existing .env key).
  const commit = () => {
    const v = input.value.trim();
    if (v) saveSetting(field.cfg, v);
  };
  input.addEventListener("change", commit);
  input.addEventListener("blur", commit);
  return wrap;
}

// Persist one setting (flat dotted key) into ~/.docingest/config.yaml, merging
// with what's already there so we never clobber other saved settings.
async function saveSetting(key, value) {
  try {
    const current = (await api.getSettings()) || {};
    current[key] = value;
    await api.saveSettings(current);
  } catch (err) {
    console.error("save setting failed", err);
  }
}

// ---- settings: cost limits (07) ------------------------------------------
// Safety mode (tri-state) + per-file / per-run thresholds. Initial values come
// from effective_safety() (resolved config + saved settings) — NOT hardcoded,
// so the screen reflects real defaults and follows config changes. Changing a
// tab or a field saves immediately (no Save button in the pen).

const COST_MODES = [
  { value: "strict", label: "必ず確認" },
  { value: "warn", label: "警告のみ" },
  { value: "off", label: "確認なし" },
];
// label / unit / config key for each numeric limit (pen rows).
const PER_FILE_LIMITS = [
  { key: "safety.per_file.max_pages", label: "最大ページ数", unit: "ページ", get: (s) => s.per_file?.max_pages },
  { key: "safety.per_file.max_est_cost_usd", label: "最大コスト", unit: "USD", get: (s) => s.per_file?.max_est_cost_usd },
];
const PER_RUN_LIMITS = [
  { key: "safety.per_run.max_total_pages", label: "合計ページ数", unit: "ページ", get: (s) => s.per_run?.max_total_pages },
  { key: "safety.per_run.max_total_est_cost_usd", label: "合計コスト", unit: "USD", get: (s) => s.per_run?.max_total_est_cost_usd },
];

async function renderCostSettings() {
  const modeEl = document.getElementById("cost-mode");
  const fileEl = document.getElementById("cost-per-file");
  const runEl = document.getElementById("cost-per-run");
  if (!modeEl || !fileEl || !runEl) return;

  let safety = {};
  try {
    safety = (await window.pywebview.api.effective_safety()) || {};
  } catch (err) {
    console.error("load cost settings failed", err);
  }

  // Mode segmented control.
  modeEl.innerHTML = "";
  const current = safety.mode || "strict";
  for (const m of COST_MODES) {
    const tab = el("div", `seg-tab ${m.value === current ? "is-active" : ""}`, m.label);
    tab.addEventListener("click", () => {
      saveSetting("safety.mode", m.value);
      modeEl.querySelectorAll(".seg-tab").forEach((t) => t.classList.remove("is-active"));
      tab.classList.add("is-active");
    });
    modeEl.appendChild(tab);
  }

  // Numeric limit rows.
  fileEl.innerHTML = "";
  runEl.innerHTML = "";
  for (const lim of PER_FILE_LIMITS) fileEl.appendChild(limitRow(lim, safety));
  for (const lim of PER_RUN_LIMITS) runEl.appendChild(limitRow(lim, safety));
}

function limitRow(lim, safety) {
  const row = el("div", "limit-row row between items-center");
  row.appendChild(el("span", "lr-label", lim.label));

  const box = el("div", "lr-box row items-center gap-2");
  const input = el("input", "lr-input");
  input.type = "number";
  input.min = "0";
  input.step = lim.unit === "USD" ? "0.01" : "1";
  const val = lim.get(safety);
  if (val != null) input.value = String(val);
  box.appendChild(input);
  box.appendChild(el("span", "lr-unit", lim.unit));
  row.appendChild(box);

  // Save on change. Empty input → don't save (keeps the existing threshold).
  const commit = () => {
    const v = parseFloat(input.value);
    if (Number.isFinite(v) && v >= 0) saveSetting(lim.key, v);
  };
  input.addEventListener("change", commit);
  input.addEventListener("blur", commit);
  return row;
}

// Render icons / init as soon as the DOM is ready (lucide loads before app.js).
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

window.addEventListener("pywebviewready", () => {
  console.log("pywebview bridge ready");
});
