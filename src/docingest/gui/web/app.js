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
  listRefined: (dir, file) => window.pywebview.api.list_refined(dir, file),
  previewRefined: (dir, skill, file) => window.pywebview.api.preview_refined(dir, skill, file),
  startRefine: (dir, files, skill, ack) =>
    window.pywebview.api.start_refine(dir, files, skill, !!ack),
  doctor: () => window.pywebview.api.doctor(),
  getSettings: () => window.pywebview.api.get_settings(),
  saveSettings: (s) => window.pywebview.api.save_settings(s),
  openFolder: (path) => window.pywebview.api.open_folder(path),
  openArtifact: (dir, key) => window.pywebview.api.open_artifact(dir, key),
  graphStatus: (dir) => window.pywebview.api.graph_status(dir),
  startBuildGraph: (dir, options) =>
    window.pywebview.api.start_build_graph(dir, options || null),
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
  costMode: "cost",      // done-screen cost metric: "cost" (default) | "tokens"
  costView: null,        // {cost, tokens, cacheHits} stashed by renderDone
  currentPreviewMd: "",  // raw text of the previewed file (for copy)
  previewView: "source", // "source" (sources/) | "refined" (readable/)
  refinedFor: null,      // {skill, filename} of the refined copy of current file, or null
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

// Output purpose preset — maps to the api `purpose` arg. Plain-language
// labels (GUI_DESIGN: speak plainly, hide file names / internal terms). The
// user picks WHAT THEY WANT IT FOR; the backend decides which files to keep.
//   full     → everything (default, "一式そろえる")
//   markdown → clean Markdown only (index/assets cleaned up after the run)
//   rag      → Markdown + chunks + index (chunking auto-on, for vector RAG)
//   agentic  → Markdown + index + search guide (for agent grep/read)
const OUTPUT_PURPOSES = [
  { value: "full", label: "一式そろえる（標準）" },
  { value: "markdown", label: "Markdown のみ" },
  { value: "rag", label: "RAG 用（チャンク付き）" },
  { value: "agentic", label: "エージェント検索用" },
];

// Live option state. (No max-pages knob: parsing.vision.max_pages defaults to
// null on purpose — a hard cap silently drops pages = information loss. Page/
// cost control is the job of the cost dialog + safety thresholds, not a risky,
// easily-misused UI field. Advanced users set it in config if they must.)
const options = {
  strategy: "auto",
  safetyMode: "strict",
  purpose: "full",
  force: false,        // true → 増分キャッシュを無視して全件再処理
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
  // 再処理: 増分キャッシュを無視する。デフォルト false なので true の時だけ送る。
  // api._normalize_overrides がドット記法を {"incremental": {"force": true}} に展開する。
  if (options.force) overrides["incremental.force"] = true;
  const result = {};
  if (Object.keys(overrides).length) result.config_overrides = overrides;
  // Output purpose preset → the api `purpose` arg. "full" is the default
  // (produce everything), so only emit when the user narrowed it.
  if (options.purpose && options.purpose !== "full") result.purpose = options.purpose;
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
    optRowSelect("出力する内容", OUTPUT_PURPOSES, options.purpose, (v) => {
      options.purpose = v;
    })
  );
  body.appendChild(
    optRowSwitch("キャッシュを無視して再処理", options.force, (v) => {
      options.force = v;
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
// Backend emits a file_done event per file (completion-only, file-level). So we
// pre-list every file as 待機中, then on each event mark that file done (by real
// status) and the NEXT one 処理中. The "処理中" state is inferred (current+1),
// not claimed from the backend — honest.
// It ALSO emits file_progress events for within-file stages (Vision page
// sub_current/sub_total, or a "parse" busy signal), so the 処理中 row shows a
// live "Vision 5/11 ページ" instead of a frozen "処理中…". See updateFileProgress.

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

// Video files take a single long Gemini call (upload + whole-video analysis,
// tens of seconds, no mid-call progress), so their "active" row would sit on
// "処理中…" silently and read as frozen. We surface an honest "this takes a
// while" note instead — the standard UX for an indeterminate 10s+ wait.
// Prefer the inspect format; fall back to the filename extension when the user
// skipped the pre-flight check (inspectRows empty).
const VIDEO_EXTS = ["mp4", "mov", "mkv", "webm", "avi", "wmv", "flv", "ts", "m4v"];
function isVideoFile(name) {
  const row = state.inspectRows.find((r) => r.name === name);
  const fmt = row && row.format
    ? String(row.format).toLowerCase()
    : (name.split(".").pop() || "").toLowerCase();
  return VIDEO_EXTS.includes(fmt);
}

function procRowInfo(stateName, ev, fileName) {
  switch (stateName) {
    case "done": {
      const pg = pagesForFile(fileName);
      const pages = pg != null ? `${pg} ページ · ` : "";
      return `${pages}${ev ? ev.chunks || 0 : 0} chunks`;
    }
    case "cached": return "キャッシュ再利用";
    case "failed":
      // Distinguish a password-protected file from a generic parse failure so
      // the user knows to unlock it rather than wondering why it "broke".
      // error_type comes through on the file_done event (see __onIngestProgress).
      return ev && ev.error_type === "encrypted"
        ? "🔒 パスワード保護（解除が必要）"
        : "解析に失敗";
    case "skipped": return "スキップ";
    case "active":
      // Video: one long indeterminate Gemini call — tell the user it's slow
      // so the unmoving row doesn't read as a freeze.
      return isVideoFile(fileName)
        ? "AI が動画を解析中…（数十秒かかる場合があります）"
        : "処理中…";
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

// Within-file sub-progress (file_progress event). Updates the 処理中 row's info
// text with a live stage: "Vision N/M ページ" while pages are being enriched,
// "解析中…" during the (黒箱) parse phase. Matches the row by basename first
// (accurate under parallelism); falls back to the single is-active row. A no-op
// if the file's row is already settled or not found — purely cosmetic, never
// touches the file_done bookkeeping (settled flags / tally / header).
function updateFileProgress(event) {
  const rows = Array.from(document.querySelectorAll("#proc-list .p-row"));
  let row = rows.find((r) => r.dataset.file === event.file && !r.dataset.settled);
  if (!row) row = rows.find((r) => r.classList.contains("is-active"));
  if (!row) return;
  const info = row.querySelector(".pr-info");
  if (!info) return;
  if (event.phase === "vision" && event.sub_total > 0) {
    info.textContent = `Vision ${event.sub_current}/${event.sub_total} ページ`;
  } else if (event.phase === "parse") {
    info.textContent = "解析中…";
  }
}

window.__onIngestProgress = function (event) {
  // Two event kinds. file_progress = within-file stage (Vision pages / parse);
  // update the live row text and stop — it carries no file-completion info.
  if (event.kind === "file_progress") {
    updateFileProgress(event);
    return;
  }
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
  // Artifacts come from gui_logic._scan_artifacts — the REAL files on disk.
  // run_ingest puts them at summary.artifacts; openLibrary's library_summary
  // returns them nested under summary.summary.artifacts. Accept both.
  const artifacts = summary.artifacts || lib.artifacts || [];

  // Stats: done/failed from backend stats; cached from front-end tally (no
  // backend field). Cost metric is a cost↔token toggle (renderCostMetric).
  setText("m-done", String(stats.successful != null ? stats.successful : state.tally.done));
  setText("m-cached", String(state.tally.cached));
  setText("m-failed", String(stats.failed != null ? stats.failed : state.tally.failed));
  // Failed metric turns alert-colored only when there ARE failures.
  const mf = document.getElementById("m-failed");
  if (mf) mf.style.color = (stats.failed || state.tally.failed) ? "var(--danger)" : "";

  // Cost ↔ token: stash both values, render per the current toggle mode.
  state.costView = {
    cost: state.inspectMeta?.totals?.est_cost_usd,
    tokens: stats.token_usage?.total_tokens,
    cacheHits: stats.token_usage?.total_cache_hits,
  };
  renderCostMetric();

  // Plain-language run summary (B) + real artefact list (A).
  renderRunSummary(lib);
  renderArtifacts(artifacts);

  renderNotices(stats, lib);
  renderPreviewSelector(lib);
  renderIcons();
}

// Run summary in plain language: "N ファイル・M ページ・K チャンク". Pages are
// summed from the per-file index entries (only files that have a page count);
// omit a dimension when its number isn't available rather than show "0 ページ"
// for, say, an audio-only run. (frontend-design §五: speak plainly.)
function renderRunSummary(lib) {
  const box = document.getElementById("done-summary");
  if (!box) return;
  const files = lib.files || [];
  const libStats = lib.stats || {};
  const fileCount = libStats.total_files != null ? libStats.total_files : files.length;
  const pages = files.reduce((sum, f) => sum + (f.pages || 0), 0);
  const chunks = libStats.total_chunks;

  const parts = [`${fileCount} ファイル`];
  if (pages > 0) parts.push(`${pages} ページ`);
  if (chunks != null) parts.push(`${chunks} チャンク`);
  box.textContent = parts.join(" · ");
}

// Real artefacts on disk → display rows. Driven by gui_logic's scan, so a
// "Markdown のみ" run shows only sources/, never a phantom chunks.jsonl.
// label/icon/desc are display copy keyed by the backend artefact key.
const ARTIFACT_META = {
  sources: { icon: "folder", name: "sources/", desc: (c) => `${c} 件の Markdown` },
  chunks: { icon: "braces", name: "chunks.jsonl", desc: (c) => c != null ? `${c} チャンク` : "チャンク" },
  index: { icon: "list-tree", name: "index.json", desc: () => "ファイル索引" },
  knowledge_map: { icon: "compass", name: "knowledge_map", desc: () => "検索ガイド" },
  graph: { icon: "share-2", name: "graph/", desc: () => "知識グラフ" },
};

function renderArtifacts(artifacts) {
  const list = document.getElementById("done-artifacts");
  if (!list) return;
  list.innerHTML = "";
  for (const art of artifacts) {
    const meta = ARTIFACT_META[art.key];
    if (!meta) continue;
    // Rows are clickable: sources/ focuses the preview; the machine-readable
    // artefacts open their real file in the OS default app (a code editor /
    // browser, where they belong). data-key drives the delegated handler.
    const row = el("div", "art-row art-clickable row items-center gap-3");
    row.dataset.key = art.key;
    row.appendChild(icon(meta.icon, "art-icon"));
    const text = el("div", "art-text col");
    text.appendChild(el("span", "art-name mono", meta.name));
    text.appendChild(el("span", "art-desc", meta.desc(art.count)));
    row.appendChild(text);
    list.appendChild(row);
  }
  renderIcons();
}

// Delegated click on artefact rows. sources/ is a directory of md the preview
// pane already renders — clicking it scrolls the preview into view rather than
// opening a folder. Everything else opens its real file via the OS default app.
document.getElementById("done-artifacts")?.addEventListener("click", (e) => {
  const row = e.target.closest(".art-row");
  if (!row || !state.currentLibraryDir) return;
  const key = row.dataset.key;
  if (key === "sources") {
    document.querySelector(".preview-col")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    return;
  }
  api.openArtifact(state.currentLibraryDir, key);
});

// Cost ↔ token toggle. Default view = est. cost (friendly, safe to show a
// client). Click the metric to reveal the real token count (internal use).
// Both label and value swap; state.costMode persists within the session so the
// choice sticks across re-renders. "—" when the value genuinely isn't known.
function renderCostMetric() {
  const cv = state.costView || {};
  const label = document.getElementById("m-cost-label");
  if (state.costMode === "tokens") {
    const t = cv.tokens;
    setText("m-cost", t != null ? t.toLocaleString() : "—");
    if (label) label.textContent = "使用トークン";
  } else {
    const c = cv.cost;
    setText("m-cost", c != null ? `$${c.toFixed(4)}` : "—");
    if (label) label.textContent = "合計コスト";
  }
}

// NoticeBox: backend warnings (page-cap, OCR downgrade, ...) + unreadable
// spots from quality. Hidden entirely when there's nothing to report.
function renderNotices(stats, lib) {
  const box = document.getElementById("done-notices");
  if (!box) return;
  box.innerHTML = "";
  const rows = [];

  // Failed files first — the most important "did it work?" signal. stats.errors
  // carries {file, error, error_type}; show which file + a plain reason so the
  // user knows what to fix, instead of just a "失敗 N" number with no detail.
  for (const e of stats.errors || []) {
    const file = e.file ? `${basename(e.file)}：` : "";
    const reason = e.error_type === "encrypted"
      ? "パスワード保護（解除が必要）"
      : e.error_type === "timeout"
        ? "時間切れ（大きすぎる可能性）"
        : "解析に失敗";
    rows.push({ icon: "x", cls: "is-error", text: `${file}${reason}` });
  }

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

// 原文/整形版 toggle: switch the preview view and re-render the current file.
document.getElementById("pv-view-toggle")?.addEventListener("click", (e) => {
  const tab = e.target.closest(".pvt-tab");
  if (!tab) return;
  state.previewView = tab.dataset.view;
  updateViewToggle();
  const sel = document.getElementById("pv-select");
  renderPreviewBody(sel && sel.value ? basename(sel.value) : "");
});

async function loadPreview(filename) {
  const body = document.getElementById("pv-body");
  if (!body || !state.currentLibraryDir) return;
  const name = basename(filename);
  // Probe whether this source has a refined copy; enable the 原文/整形版 toggle
  // only when one exists. Pick the first refined copy (one skill at a time in
  // the GUI flow). Reset to the source view when switching files.
  try {
    const refs = await api.listRefined(state.currentLibraryDir, name);
    state.refinedFor = refs && refs.length ? refs[0] : null;
  } catch {
    state.refinedFor = null;
  }
  if (!state.refinedFor) state.previewView = "source";
  updateViewToggle();
  await renderPreviewBody(name);
}

// Render the preview body for the current view (source or refined). Stashes the
// raw text for the copy button. Refined HTML output is shown as-is; md is
// rendered. Falls back gracefully on read failure.
async function renderPreviewBody(name) {
  const body = document.getElementById("pv-body");
  if (!body || !state.currentLibraryDir) return;
  try {
    let text = "";
    let isHtml = false;
    if (state.previewView === "refined" && state.refinedFor) {
      const r = state.refinedFor;
      text = await api.previewRefined(state.currentLibraryDir, r.skill, r.filename);
      isHtml = /\.html$/i.test(r.filename);
    } else {
      text = await api.previewMarkdown(state.currentLibraryDir, name);
    }
    state.currentPreviewMd = text ? stripFrontmatter(text) : "";
    updateCopyButton();
    if (!text) {
      body.textContent = "（内容が空です）";
      return;
    }
    // Refined HTML is a fidelity-preserving fragment; sanitize and show. Md
    // (both source and md-refined) goes through the markdown renderer.
    if (isHtml) {
      body.innerHTML = window.DOMPurify ? window.DOMPurify.sanitize(text) : text;
    } else {
      body.innerHTML = renderMarkdown(text);
    }
  } catch (err) {
    state.currentPreviewMd = "";
    updateCopyButton();
    body.textContent = "（プレビューの読み込みに失敗しました）";
  }
}

// Show the 原文/整形版 toggle only when a refined copy exists; reflect the
// active view. Disabled-by-absence keeps the UI honest (no dead toggle).
function updateViewToggle() {
  const toggle = document.getElementById("pv-view-toggle");
  if (!toggle) return;
  if (!state.refinedFor) {
    toggle.classList.add("is-hidden");
    return;
  }
  toggle.classList.remove("is-hidden");
  toggle.querySelectorAll(".pvt-tab").forEach((t) => {
    t.classList.toggle("is-active", t.dataset.view === state.previewView);
  });
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
// 知識グラフ画面へ。graphStatus は初回呼び出しで lightrag を import するため
// 数秒〜十数秒かかる（doctor と同じ性質）。await すると押下から画面遷移までが
// 体感の空白になるので、まず未構築前提で即切替し、status は後追いで反映する。
// 既に構築済みのライブラリでは「未構築 → 再構築」の文言反転が起きるが、
// それは再訪時のみ・かつ目立たない位置で発生するため許容。
document.getElementById("done-graph")?.addEventListener("click", () => {
  setGraphBuildMode(false);          // まず HTML デフォルト（未構築）で見せる
  showScreen("graph-empty");
  if (!state.currentLibraryDir) return;
  api.graphStatus(state.currentLibraryDir)
    .then((st) => {
      if (st && st.built) setGraphBuildMode(true);
    })
    .catch((err) => console.error("graph status failed", err));
});

// 「再構築」モードかどうかで graph-empty の表示を切り替える。
//   未構築 → 通常文言、force スイッチは隠す（重建概念なし）
//   既構築 → 説明文を再構築寄りに、force 行を見せる（デフォルト ON 推奨だが
//            ユーザに最終決定権を渡す）
function setGraphBuildMode(rebuild) {
  const desc = document.querySelector(".build-card .bc-desc");
  const btnLabel = document.querySelector("#graph-build span");
  const forceRow = document.getElementById("bc-force-row");
  const forceSwitch = document.getElementById("bc-force-switch");
  if (rebuild) {
    if (desc) desc.textContent = "このナレッジベースには既に知識グラフがあります。再構築すると、現在の設定で作り直されます。";
    if (btnLabel) btnLabel.textContent = "再構築";
    if (forceRow) forceRow.hidden = false;
    // 再構築時のデフォルトは「キャッシュも無視」— 普通そうしたいから来ている
    if (forceSwitch && !forceSwitch.classList.contains("is-on")) {
      forceSwitch.classList.add("is-on");
      forceSwitch.setAttribute("aria-checked", "true");
    }
  } else {
    if (desc) desc.textContent = "このナレッジベースには、まだ知識グラフがありません。構築すると、複数の文書をまたいだ「関係」や「テーマ」を質問できるようになります。";
    if (btnLabel) btnLabel.textContent = "知識グラフを構築";
    if (forceRow) forceRow.hidden = true;
    if (forceSwitch) {
      forceSwitch.classList.remove("is-on");
      forceSwitch.setAttribute("aria-checked", "false");
    }
  }
}
// run.log は pipeline が必ず出すが、旧バージョン製の library には無い場合がある。
// open_artifact は存在しない場合 False を返すので、その時だけユーザに伝える。
document.getElementById("done-log")?.addEventListener("click", async () => {
  if (!state.currentLibraryDir) return;
  const ok = await api.openArtifact(state.currentLibraryDir, "run_log");
  if (!ok) showToast("ログがありません");
});

// Cost ↔ token toggle: click the cost metric to flip between the friendly
// est. cost (default, client-safe) and the real token count (internal). The
// mode persists on state so it survives re-renders within the session.
document.getElementById("m-cost-metric")?.addEventListener("click", () => {
  state.costMode = state.costMode === "tokens" ? "cost" : "tokens";
  renderCostMetric();
});

// 「コピー」 — copy the currently previewed file's raw markdown to the
// clipboard. WebView2 supports navigator.clipboard in a window context. Brief
// "コピーしました" feedback on the button, then revert.
const pvCopyBtn = document.getElementById("pv-copy");
pvCopyBtn?.addEventListener("click", async () => {
  const text = state.currentPreviewMd || "";
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    flashCopied();
  } catch (err) {
    // Clipboard API can reject (focus / permissions). Fall back to a hidden
    // textarea + execCommand, which works in older WebView2 builds.
    if (copyViaTextarea(text)) flashCopied();
    else console.error("copy failed", err);
  }
});

// Disable + dim the copy button when there's nothing to copy (empty preview).
function updateCopyButton() {
  if (!pvCopyBtn) return;
  pvCopyBtn.disabled = !state.currentPreviewMd;
}

function flashCopied() {
  const label = pvCopyBtn?.querySelector(".pvc-text");
  if (!label) return;
  const prev = label.textContent;
  label.textContent = "コピーしました";
  pvCopyBtn.classList.add("is-copied");
  setTimeout(() => {
    label.textContent = prev;
    pvCopyBtn.classList.remove("is-copied");
  }, 1400);
}

// Legacy clipboard fallback for WebView2 builds where navigator.clipboard is
// blocked. Returns true on success.
function copyViaTextarea(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch {
    return false;
  }
}

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

// Remember the last refine request so __onRefineBlocked can re-issue it with
// acknowledge=true after the user confirms the cost.
let _lastRefine = null;

async function _startRefine(dir, file, skill, ack) {
  _lastRefine = { dir, file, skill };
  refineGoBtn.disabled = true;
  refineGoBtn.classList.add("is-loading");
  const lbl = refineGoBtn.querySelector(".btn-label");
  if (lbl) lbl.textContent = "整形中…";
  const cancelBtn = document.getElementById("refine-cancel");
  if (cancelBtn) cancelBtn.textContent = "閉じる（バックグラウンドで継続）";
  try {
    await api.startRefine(dir, [file], skill, ack);
  } catch (err) {
    console.error("start refine failed", err);
    resetRefineButton();
  }
}

refineGoBtn?.addEventListener("click", async () => {
  const selected = document.querySelector("#refine-opts .refine-opt.is-selected");
  const skill = selected ? selected.dataset.skill : "refine_faithful";
  const file = currentPreviewFile();
  if (!file || !state.currentLibraryDir) return;
  await _startRefine(state.currentLibraryDir, file, skill, false);
});

function resetRefineButton() {
  if (!refineGoBtn) return;
  refineGoBtn.disabled = false;
  refineGoBtn.classList.remove("is-loading");
  const lbl = refineGoBtn.querySelector(".btn-label");
  if (lbl) lbl.textContent = "整形する";
  const cancelBtn = document.getElementById("refine-cancel");
  if (cancelBtn) cancelBtn.textContent = "キャンセル";
}

// The file currently shown in the 04 preview selector (refine target).
function currentPreviewFile() {
  const sel = document.getElementById("pv-select");
  return sel && sel.value ? sel.value : null;
}

window.__onRefineDone = function (result) {
  hideOverlay("refine-style");
  resetRefineButton();
  const n = (result && result.files && result.files.length) || 0;
  showToast(`整形が完了しました（${n} 件）`);
  // Re-probe the current file's refined copy and switch the preview to it, so
  // the user sees the result immediately without hunting for a folder.
  const sel = document.getElementById("pv-select");
  const name = sel && sel.value ? basename(sel.value) : "";
  if (name) {
    state.previewView = "refined";
    loadPreview(name);
  }
};

// Cost gate (refine.cost_check.mode=strict) blocked the run. Show the estimate
// and let the user confirm; on yes, re-issue the SAME refine with ack=true.
window.__onRefineBlocked = function (info) {
  resetRefineButton();
  const est = (info && info.estimate) || {};
  const reasons = (info && info.reasons) || [];
  const cost = est.est_cost_usd != null ? `$${Number(est.est_cost_usd).toFixed(4)}` : "—";
  const msg =
    "コスト確認\n\n" +
    reasons.map((r) => "• " + r).join("\n") +
    `\n\n予想: ${cost} / ${est.total_pieces || "?"} 分割（${est.model || ""}）\n\n続行しますか？`;
  if (window.confirm(msg) && _lastRefine) {
    _startRefine(_lastRefine.dir, _lastRefine.file, _lastRefine.skill, true);
  }
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

// Mode pick (詳しく / 節約) — radio-style, single selection. Delegated on the
// row so the listener doesn't get lost if the DOM is rebuilt.
document.querySelector(".bc-mode")?.addEventListener("click", (e) => {
  const opt = e.target.closest(".bc-mode-opt");
  if (!opt) return;
  document.querySelectorAll(".bc-mode-opt").forEach((o) =>
    o.classList.toggle("is-selected", o === opt)
  );
});

// Enrich / force switches — handwritten in HTML (single switches, don't go
// through optRowSwitch). Same toggle logic — share it via the helper below.
function bindSimpleSwitch(id) {
  document.getElementById(id)?.addEventListener("click", (e) => {
    const sw = e.currentTarget;
    const next = !sw.classList.contains("is-on");
    sw.classList.toggle("is-on", next);
    sw.setAttribute("aria-checked", String(next));
  });
}
bindSimpleSwitch("bc-enrich-switch");
bindSimpleSwitch("bc-force-switch");

// Read current build choices from the DOM. No JS state mirror — DOM is the
// single source of truth, defaults match the HTML (mode=full, enrich=off,
// force=off; force-row hidden when there's no existing graph).
function readGraphBuildOptions() {
  const selected = document.querySelector(".bc-mode-opt.is-selected");
  const mode = selected ? selected.dataset.mode : "full";
  const enrich = document
    .getElementById("bc-enrich-switch")
    ?.classList.contains("is-on") || false;
  const force = document
    .getElementById("bc-force-switch")
    ?.classList.contains("is-on") || false;
  const opts = {};
  // Only send non-default fields so the backend's config defaults stand.
  if (mode && mode !== "full") opts.mode = mode;
  if (enrich) opts.enrich_chunks = true;
  if (force) opts.force = true;
  return opts;
}

document.getElementById("graph-build")?.addEventListener("click", async () => {
  if (!state.currentLibraryDir) return;
  const options = readGraphBuildOptions();
  showScreen("graph-building");
  setGraphProgress(0, 0);
  try {
    await api.startBuildGraph(state.currentLibraryDir, options);
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
  showToast(`知識グラフを構築しました（用語 ${e} / 関係 ${r}）`);
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

// ---- toast (non-blocking completion notice) ------------------------------
// A small bottom-right notice that fades in/out on its own. Replaces window.
// alert for SUCCESS notifications (alert blocks the UI and reads as an error
// dialog). Errors / confirmations still use alert/confirm where a decision is
// needed.
let _toastTimer = null;
function showToast(message) {
  let toast = document.getElementById("app-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "app-toast";
    toast.className = "app-toast";
    document.body.appendChild(toast);
  }
  toast.textContent = message;
  // Force reflow so the fade-in transition re-runs on repeat toasts.
  void toast.offsetWidth;
  toast.classList.add("is-shown");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => toast.classList.remove("is-shown"), 2600);
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
// Vision のモデル選択のみ。ASR は default.yaml の qwen3-asr-flash を既定で
// 走らせる（ユーザの選択肢として出さない）。
//
// 切り替え時は単一 model id だけでなく provider と api_key_env も同期する必要
// があるため（同じ vision タスクで Google → OpenAI に乗り換えると認証先が
// 変わる）、各ティアは 3 つの設定値を持ち、保存時にまとめて書き込む。
// 保存形式は flat dotted key（api._normalize_overrides がネストに展開）。
const VISION_TIERS = [
  {
    value: "gemini-3-flash-preview",
    label: "Gemini 3 Flash（既定）",
    provider: "google",
    api_key_env: "GEMINI_API_KEY",
  },
  {
    value: "gemini-3.1-pro-preview",
    label: "Gemini 3.1 Pro（高精度）",
    provider: "google",
    api_key_env: "GEMINI_API_KEY",
  },
  {
    value: "gpt-5.5",
    label: "GPT-5.5（高精度）",
    provider: "openai",
    api_key_env: "OPENAI_API_KEY",
  },
  {
    value: "gpt-5.4-mini",
    label: "GPT-5.4 mini（節約）",
    provider: "openai",
    api_key_env: "OPENAI_API_KEY",
  },
];
const VISION_MODEL_KEY = "models.vision.primary.model";
const VISION_PROVIDER_KEY = "models.vision.primary.provider";
const VISION_KEYENV_KEY = "models.vision.primary.api_key_env";

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
  const keysEl = document.getElementById("model-keys");
  if (!vSel || !keysEl) return;

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

  // Dropdown: show saved value if present, else the default (first tier).
  fillTierSelect(vSel, VISION_TIERS, settings[VISION_MODEL_KEY] || VISION_TIERS[0].value);
  vSel.onchange = () => saveVisionTier(vSel.value);

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

// Vision tier 切替えは model / provider / api_key_env の 3 つを同時に書く
// （Google → OpenAI に乗り換えると認証先が変わるため、model だけ書くと
// provider が前回のままになり認証失敗する）。3 回 saveSetting すると間に
// レース条件が生じる（各 await で旧 settings を読み直すので最後の書き込み
// 以外が消える）ので、1 回の getSettings → set 3 つ → saveSettings に
// まとめる。
async function saveVisionTier(modelId) {
  const tier = VISION_TIERS.find((t) => t.value === modelId);
  if (!tier) return;
  try {
    const current = (await api.getSettings()) || {};
    current[VISION_MODEL_KEY] = tier.value;
    current[VISION_PROVIDER_KEY] = tier.provider;
    current[VISION_KEYENV_KEY] = tier.api_key_env;
    await api.saveSettings(current);
  } catch (err) {
    console.error("save vision tier failed", err);
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
