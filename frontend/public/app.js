// Giao diện tải file lên, theo dõi việc và xem kết quả OCR / chép lời
"use strict";

const MODES = {
  ocr: {
    ext: ["pdf", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "docx", "doc", "odt", "rtf", "txt", "md"],
    endpoint: "/api/uploads/ocr",
    hint: "PDF, ảnh (jpg, png, tif...), Word (docx, doc), odt, rtf, txt, md",
    group: "Gộp thành một tài liệu (ảnh là các trang theo tên; có Word/txt thì lấy text, không OCR)",
  },
  speech: {
    ext: ["mp3", "m4a", "m4b", "aac", "wav", "flac", "ogg", "oga", "opus", "wma", "amr", "aiff", "aif", "mp4", "webm", "mkv", "mov"],
    endpoint: "/api/uploads/speech",
    hint: "mp3, m4a, wav, flac, ogg, opus, aac, wma... và video mp4, webm, mkv, mov (lấy tiếng)",
    group: "Gộp thành một bản chép lời (nối các file theo tên)",
  },
};
const POLL_MS = 3000;
const $ = (id) => document.getElementById(id);

let mode = "ocr";
let selected = []; // {file, path}
let skipped = [];
let jobs = load("jobs", []);
const localFiles = new Map(); // request_id -> File[] (chỉ trong phiên này, để phát lại âm thanh)
const results = new Map(); // request_id -> kết quả
let current = null; // việc đang xem
let view = "text";

// Lưu trữ trên trình duyệt (có thể bị chặn)
function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* bỏ qua */ }
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
  return node;
}

const extOf = (name) => (name.split(".").pop() || "").toLowerCase();
const fmtSize = (n) => n < 1024 ** 2 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`;
function clock(sec) {
  const s = Math.floor(sec);
  return [Math.floor(s / 3600), Math.floor(s % 3600 / 60), s % 60].map((x) => String(x).padStart(2, "0")).join(":");
}
function headers() {
  const key = $("apiKey").value.trim();
  return key ? { "X-API-Key": key } : {};
}
async function api(path) {
  const r = await fetch(path, { headers: headers() });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail ? JSON.stringify(body.detail) : `HTTP ${r.status}`);
  return body;
}

// Kết nối

$("apiKey").value = load("apiKey", "");
$("apiKey").addEventListener("change", () => save("apiKey", $("apiKey").value.trim()));

async function checkHealth() {
  try {
    const r = await fetch("/health");
    $("health").className = "dot " + (r.ok ? "ok" : "bad");
    $("health").title = r.ok ? "Backend đang chạy" : `Backend lỗi (HTTP ${r.status})`;
  } catch {
    $("health").className = "dot bad";
    $("health").title = "Không kết nối được backend";
  }
}

// Chọn file

function setMode(m) {
  mode = m;
  document.querySelectorAll(".tabs:not(.small) .tab").forEach((t) => t.classList.toggle("active", t.dataset.mode === m));
  $("hint").textContent = MODES[m].hint;
  $("groupLabel").textContent = MODES[m].group;
  const accept = MODES[m].ext.map((e) => "." + e).join(",");
  $("pickFiles").accept = accept;
  addFiles([], false, true);
}

function addFiles(items, fromFolder, reset = false) {
  if (reset) { selected = []; skipped = []; }
  for (const { file, path } of items) {
    if (!MODES[mode].ext.includes(extOf(file.name)) || file.name.startsWith(".") || file.name.startsWith("~$")) {
      skipped.push(path);
    } else if (!selected.some((s) => s.path === path && s.file.size === file.size)) {
      selected.push({ file, path });
    }
  }
  // Chọn thư mục thì mặc định gộp thành một tài liệu
  if (fromFolder) $("optGroup").checked = true;
  else if (reset || selected.length === items.length) $("optGroup").checked = false;
  renderSelection();
}

function renderSelection() {
  $("selection").hidden = !selected.length && !skipped.length;
  const total = selected.reduce((n, s) => n + s.file.size, 0);
  $("selCount").textContent = `${selected.length} file · ${fmtSize(total)}`;
  $("selList").replaceChildren(...selected.map((s, i) =>
    el("li", {}, el("span", { class: "name" }, s.path), el("span", { class: "size" }, fmtSize(s.file.size), " ",
      el("button", { class: "link", title: "Bỏ file này", onclick: () => { selected.splice(i, 1); renderSelection(); } }, "✕")))));
  $("selSkipped").hidden = !skipped.length;
  $("selSkipped").textContent = skipped.length
    ? `Bỏ qua ${skipped.length} file không hỗ trợ: ${skipped.slice(0, 8).join(", ")}${skipped.length > 8 ? "…" : ""}` : "";
  $("submit").disabled = !selected.length;
}

$("pickFiles").addEventListener("change", (e) => {
  addFiles([...e.target.files].map((f) => ({ file: f, path: f.name })), false);
  e.target.value = "";
});
$("pickFolder").addEventListener("change", (e) => {
  addFiles([...e.target.files].map((f) => ({ file: f, path: f.webkitRelativePath || f.name })), true);
  e.target.value = "";
});
$("clearSel").addEventListener("click", () => addFiles([], false, true));

// Kéo thả, kể cả thư mục
const drop = $("drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, () => drop.classList.remove("over")));
drop.addEventListener("drop", async (e) => {
  e.preventDefault();
  const entries = [...e.dataTransfer.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) {
    addFiles([...e.dataTransfer.files].map((f) => ({ file: f, path: f.name })), false);
    return;
  }
  const out = [];
  await Promise.all(entries.map((entry) => walk(entry, out)));
  addFiles(out, entries.some((en) => en.isDirectory));
});

async function walk(entry, out) {
  if (entry.isFile) {
    const file = await new Promise((ok, fail) => entry.file(ok, fail));
    out.push({ file, path: entry.fullPath.replace(/^\//, "") });
  } else if (entry.isDirectory) {
    const reader = entry.createReader();
    // readEntries trả từng phần, đọc tới khi hết
    for (;;) {
      const batch = await new Promise((ok, fail) => reader.readEntries(ok, fail));
      if (!batch.length) break;
      await Promise.all(batch.map((child) => walk(child, out)));
    }
  }
}

// Gửi

$("submit").addEventListener("click", () => {
  const form = new FormData();
  selected.forEach((s) => form.append("files", s.file, s.path));
  form.append("group", $("optGroup").checked ? "true" : "false");
  form.append("spellcheck", $("optSpell").checked ? "true" : "false");
  const sent = selected.slice();
  const kind = mode;

  $("submit").disabled = true;
  $("submitError").hidden = true;
  $("progress").hidden = false;
  const bar = $("progress").firstElementChild;
  const label = $("progress").lastElementChild;

  const xhr = new XMLHttpRequest();
  xhr.open("POST", MODES[kind].endpoint);
  for (const [k, v] of Object.entries(headers())) xhr.setRequestHeader(k, v);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const p = Math.round(e.loaded / e.total * 100);
    bar.style.width = p + "%";
    label.textContent = p < 100 ? `Đang tải lên ${p}%` : "Đang lưu…";
  };
  xhr.onload = () => {
    $("progress").hidden = true;
    let body = {};
    try { body = JSON.parse(xhr.responseText); } catch { /* bỏ qua */ }
    if (xhr.status !== 202) {
      showError(xhr.status === 401 ? "Sai hoặc thiếu API key." : `Lỗi ${xhr.status}: ${JSON.stringify(body.detail ?? xhr.responseText)}`);
      return;
    }
    for (const job of body.jobs) {
      jobs.unshift({ id: job.request_id, kind, files: job.files, created: Date.now(), status: "PENDING" });
      localFiles.set(job.request_id, sent.filter((s) => job.files.includes(s.file.name)).map((s) => s.file));
    }
    save("jobs", jobs);
    addFiles([], false, true);
    if (body.skipped.length) showError(`Backend bỏ qua: ${body.skipped.map((s) => s.file).join(", ")}`);
    renderJobs();
    poll();
  };
  xhr.onerror = () => { $("progress").hidden = true; showError("Không gửi được (mất kết nối?)."); };
  xhr.send(form);
});

function showError(msg) {
  $("submitError").hidden = false;
  $("submitError").textContent = msg;
  $("submit").disabled = !selected.length;
}

// Danh sách việc

const STATUS = {
  PENDING: ["Đang chờ / xử lý", "wait"], STARTED: ["Đang lưu", "wait"], RETRY: ["Thử lại", "wait"],
  SUCCESS: ["Xong", "ok"], FAILURE: ["Lỗi", "bad"],
};

function renderJobs() {
  $("noJobs").hidden = jobs.length > 0;
  $("jobs").replaceChildren(...jobs.map((j) => {
    const [text, cls] = STATUS[j.status] || [j.status, "wait"];
    const mins = Math.round((Date.now() - j.created) / 60000);
    const meta = `${j.kind === "ocr" ? "OCR" : "Chép lời"} · ${new Date(j.created).toLocaleString("vi-VN")}`
      + (j.status === "FAILURE" && j.error ? ` · ${j.error}` : "")
      + (cls === "wait" ? ` · ${mins} phút` : "");
    return el("li", { class: "job", onclick: () => openJob(j) },
      el("span", { class: "files-line" }, j.files.length > 3 ? `${j.files.slice(0, 3).join(", ")} và ${j.files.length - 3} file khác` : j.files.join(", ")),
      el("span", { class: "badge " + cls }, text),
      el("span", { class: "meta" }, meta));
  }));
}

$("clearJobs").addEventListener("click", () => { jobs = []; save("jobs", jobs); renderJobs(); $("viewer").hidden = true; });

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  while (jobs.some((j) => !["SUCCESS", "FAILURE"].includes(j.status))) {
    for (const j of jobs.filter((x) => !["SUCCESS", "FAILURE"].includes(x.status))) {
      try {
        const r = await api(`/api/request-status/${j.id}`);
        j.status = r.status;
        if (r.status === "SUCCESS") results.set(j.id, r.result);
        if (r.status === "FAILURE") j.error = r.error;
      } catch (err) {
        j.error = err.message;
      }
    }
    save("jobs", jobs);
    renderJobs();
    if (current && results.has(current.id) && $("vBody").dataset.waiting === current.id) openJob(current);
    await new Promise((ok) => setTimeout(ok, POLL_MS));
  }
  polling = false;
}

// Xem kết quả

async function openJob(job) {
  current = job;
  $("viewer").hidden = false;
  $("vTitle").textContent = job.files.join(", ");
  $("vAudio").hidden = true;
  if (!results.has(job.id) && job.status === "SUCCESS") {
    try { results.set(job.id, (await api(`/api/request-status/${job.id}`)).result); } catch { /* hiện thông báo bên dưới */ }
  }
  const res = results.get(job.id);
  if (!res) {
    $("vMeta").textContent = "";
    $("vNotice").hidden = true;
    $("vBody").dataset.waiting = job.id;
    $("vBody").replaceChildren(el("p", { class: "empty" },
      job.status === "FAILURE" ? `Việc bị lỗi: ${job.error || ""}` : "Chưa xong; trang sẽ tự cập nhật khi có kết quả."));
    $("viewer").scrollIntoView({ behavior: "smooth" });
    return;
  }
  delete $("vBody").dataset.waiting;
  const speech = job.kind === "speech";
  $("vMeta").textContent = speech
    ? `${clock(res.duration)} · ${res.segment_count} đoạn · ${res.correction_total} chỗ sửa · ${(res.elapsed_ms / 1000).toFixed(1)} giây xử lý`
    : `${res.page_count} trang · ${res.correction_total} chỗ sửa · ${(res.elapsed_ms / 1000).toFixed(1)} giây xử lý`;
  $("vNotice").hidden = !res.notice;
  $("vNotice").textContent = res.notice || "";
  $("vChangeCount").textContent = res.changes.length;
  $("vUnknownCount").textContent = res.unknown_total;
  // Phát lại âm thanh nếu file còn trong phiên này
  const files = localFiles.get(job.id) || [];
  if (speech && files.length) {
    $("vAudio").hidden = false;
    $("vAudio").dataset.file = "";
  }
  render();
  $("viewer").scrollIntoView({ behavior: "smooth" });
}

function render() {
  document.querySelectorAll("#vTabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.view === view));
  const res = results.get(current.id);
  const speech = current.kind === "speech";
  const body = {
    text: () => speech ? speechText(res) : ocrText(res),
    changes: () => changesView(res, speech),
    unknown: () => unknownView(res, speech),
    input: () => inputView(res, speech),
  }[view]();
  $("vBody").replaceChildren(body);
}

document.querySelectorAll("#vTabs .tab").forEach((t) => t.addEventListener("click", () => { view = t.dataset.view; render(); }));
$("vClose").addEventListener("click", () => { $("viewer").hidden = true; current = null; $("vAudio").pause(); });
$("vDownload").addEventListener("click", () => {
  const res = results.get(current?.id);
  if (!res) return;
  const text = current.kind === "speech"
    ? res.segments.map((s) => `[${clock(s.start)}] ${s.text}`).join("\n")
    : res.text;
  const a = el("a", { href: URL.createObjectURL(new Blob([text], { type: "text/plain;charset=utf-8" })),
    download: current.files[0].replace(/\.[^.]+$/, "") + ".txt" });
  a.click();
  URL.revokeObjectURL(a.href);
});

function ocrText(res) {
  const wrap = el("div");
  for (const p of res.pages) {
    const src = { chandra: "OCR", pdf_text: "text PDF", text_file: "file văn bản", none: "lỗi" }[p.source] || p.source;
    wrap.append(el("div", { class: "page" },
      el("h3", {}, `Trang ${p.page} · ${src}${p.error ? " · " + p.error : ""}`),
      el("pre", {}, p.text || "(trống)")));
  }
  return wrap;
}

function play(fileName, start) {
  const files = localFiles.get(current.id) || [];
  const file = files.find((f) => f.name === fileName) || files[0];
  if (!file) return;
  const audio = $("vAudio");
  if (audio.dataset.file !== file.name) {
    if (audio.src) URL.revokeObjectURL(audio.src);
    audio.src = URL.createObjectURL(file);
    audio.dataset.file = file.name;
  }
  audio.currentTime = start;
  audio.play().catch(() => {});
}

function speechText(res) {
  if (!res.segments.length) return el("p", { class: "empty" }, "Không có đoạn nào có tiếng nói.");
  const changed = new Set(res.changes.map((c) => c.segment));
  const many = new Set(res.segments.map((s) => s.file)).size > 1;
  return el("div", {}, res.segments.map((s) =>
    el("div", { class: "seg" + (changed.has(s.index) ? " changed" : "") },
      el("button", { class: "time", title: "Phát từ đây", onclick: () => play(s.file, s.start) }, clock(s.start)),
      el("span", {}, many ? el("span", { class: "muted" }, `${s.file} · `) : null, s.error ? `[${s.error}]` : s.text))));
}

function marked(change) {
  const line = el("div");
  (change.segments || []).forEach((part, i) => {
    if (i) line.append(" ");
    if (part.type === "same") line.append(part.text);
    else line.append(el("del", {}, part.before), el("ins", {}, part.after), part.by ? el("span", { class: "by" }, part.by) : null);
  });
  return line;
}

function changesView(res, speech) {
  if (!res.changes.length) return el("p", { class: "empty" }, "Không có chỗ nào bị sửa.");
  return el("div", {}, res.changes.map((c) => el("div", { class: "change" },
    el("div", { class: "where" }, speech
      ? el("button", { class: "link", onclick: () => play(c.file, c.start) }, `Đoạn ${c.segment} · ${clock(c.start)}`)
      : `Trang ${c.page} · dòng ${c.line}`),
    marked(c))));
}

function unknownView(res, speech) {
  if (!res.unknown_words.length) return el("p", { class: "empty" }, "Không có từ lạ.");
  return el("table", {},
    el("thead", {}, el("tr", {}, el("th", {}, "Từ"), el("th", {}, "Loại"), el("th", {}, "Số lần"),
      el("th", {}, speech ? "Đoạn" : "Trang"), el("th", {}, "Ngữ cảnh"))),
    el("tbody", {}, res.unknown_words.map((w) => el("tr", {},
      el("td", {}, el("strong", {}, w.word)), el("td", {}, w.kind), el("td", {}, w.count),
      el("td", {}, (w.segments || w.pages || []).join(", ")), el("td", {}, (w.samples || [])[0] || "")))));
}

function inputView(res, speech) {
  const info = res.input || {};
  const rows = [["Cách lấy text", { text: "Lấy từ file văn bản (không OCR)", ocr: "OCR / text PDF", speech: "Trích xuất lời nói" }[info.mode] || info.mode]];
  if (speech) (info.files || []).forEach((f) => rows.push([f.file, clock(f.duration)]));
  else rows.push(["File dùng", (info.files || []).join(", ")]);
  (info.skipped || []).forEach((s) => rows.push([`Bỏ qua: ${s.file}`, s.reason]));
  (info.pdf_text || []).forEach((p) => rows.push([`Text PDF: ${p.file}`,
    `${p.text_pages} trang lấy text, ${p.ocr_pages} trang OCR${p.reason ? " · " + p.reason : ""}`]));
  if (info.warning) rows.push(["Cảnh báo", info.warning]);
  return el("table", {}, el("tbody", {}, rows.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, v)))));
}

// Khởi động

document.querySelectorAll(".tabs:not(.small) .tab").forEach((t) => t.addEventListener("click", () => setMode(t.dataset.mode)));
setMode("ocr");
renderJobs();
checkHealth();
setInterval(checkHealth, 30000);
if (jobs.some((j) => !["SUCCESS", "FAILURE"].includes(j.status))) poll();
