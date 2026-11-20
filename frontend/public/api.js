// Trang thử API kiểu Swagger: danh sách API, nhập tham số, gửi request thật, xem JSON trả về
"use strict";

const $ = (id) => document.getElementById(id);

// Mô tả các API (khớp backend/app/api)

const BY = ["", "protonx", "dict", "replace"];
const QUEUES = [
  "fullpage_prepare_queue", "fullpage_ocr_queue", "fullpage_spell_queue", "fullpage_db_queue",
  "speech_prepare_queue", "speech_asr_queue", "speech_spell_queue", "speech_db_queue", "book_queue",
];
const S3_BODY = (path, extra = {}) => ({
  s3_path: path,
  access_key: "",
  secret_key: "",
  endpoint: "",
  callback_url: "",
  document_id: 12345,
  ...extra,
});
const S3_NOTE = "Bỏ trống access_key, secret_key, endpoint thì backend dùng S3 cấu hình sẵn trong .env. "
  + "s3_path kết thúc bằng / là cả thư mục (gộp thành một việc).";
const RID = { name: "request_id", in: "path", required: true, desc: "Mã việc nhận được khi gửi" };
const PAGING = (limit, max) => [
  { name: "limit", in: "query", type: "number", def: limit, desc: `Tối đa ${max}` },
  { name: "offset", in: "query", type: "number", def: 0 },
];

const GROUPS = [
  {
    title: "Gửi việc từ S3 / MinIO", note: "bất đồng bộ, trả request_id",
    ops: [
      { method: "POST", path: "/api/ocr-fullpage", summary: "OCR toàn văn",
        desc: "OCR file PDF, ảnh, docx, doc, odt, rtf, txt, md trên S3. " + S3_NOTE,
        body: S3_BODY("s3://bucket/thu-muc/file.pdf", { spellcheck: true }), codes: [202, 400, 401, 422] },
      { method: "POST", path: "/api/book-metadata", summary: "Thông tin xuất bản sách",
        desc: "Đọc trang cuối sách, tách thông tin xuất bản. " + S3_NOTE,
        body: S3_BODY("s3://bucket/sach-abc/"), codes: [202, 400, 401, 422] },
      { method: "POST", path: "/api/speech-to-text", summary: "Chép lời audio/video",
        desc: "mp3, m4a, wav, flac, ogg, opus, aac, wma, mp4, webm, mkv, mov... " + S3_NOTE,
        body: S3_BODY("s3://bucket/ghi-am/file.m4a", { spellcheck: true }), codes: [202, 400, 401, 422] },
    ],
  },
  {
    title: "Theo dõi việc", note: "dùng cho mọi request_id",
    ops: [
      { method: "GET", path: "/api/request-status/{request_id}", summary: "Trạng thái và kết quả",
        desc: "PENDING: đang chờ hoặc đang chạy. SUCCESS: kết quả nằm trong result. FAILURE: lý do nằm trong error.",
        params: [RID], poll: true, codes: [200, 401, 404] },
    ],
  },
  {
    title: "Tải file từ máy lên", note: "bất đồng bộ, trả danh sách jobs",
    ops: [
      { method: "POST", path: "/api/uploads/ocr", summary: "Tải lên rồi OCR",
        desc: "group=false: mỗi file một việc. group=true: cả lô thành một tài liệu (xếp theo tên).",
        params: [
          { name: "files", in: "form", type: "files", required: true, desc: "Chọn một hoặc nhiều file" },
          { name: "group", in: "form", type: "bool", def: "false" },
          { name: "spellcheck", in: "form", type: "bool", def: "true" },
        ], codes: [202, 401, 413, 415, 422] },
      { method: "POST", path: "/api/uploads/metadata", summary: "Tải sách lên, lấy thông tin xuất bản",
        desc: "group=true: cả lô là một cuốn sách (ví dụ PDF + Word cùng tên).",
        params: [
          { name: "files", in: "form", type: "files", required: true },
          { name: "group", in: "form", type: "bool", def: "true" },
        ], codes: [202, 401, 413, 415, 422] },
      { method: "POST", path: "/api/uploads/speech", summary: "Tải audio lên rồi chép lời",
        desc: "group=true: các file audio nối nhau theo tên thành một việc.",
        params: [
          { name: "files", in: "form", type: "files", required: true },
          { name: "group", in: "form", type: "bool", def: "false" },
          { name: "spellcheck", in: "form", type: "bool", def: "true" },
        ], codes: [202, 401, 413, 415, 422] },
    ],
  },
  {
    title: "Xử lý ngay", note: "đồng bộ, chờ đến khi xong; kết quả không lưu",
    ops: [
      { method: "POST", path: "/api/ocr-fullpage/sync", summary: "OCR một file, trả kết quả ngay",
        params: [
          { name: "file", in: "form", type: "file", required: true },
          { name: "spellcheck", in: "form", type: "bool", def: "true" },
          { name: "include_raw", in: "form", type: "bool", def: "false", desc: "true: thêm raw_text (trước khi sửa) cho mỗi trang" },
        ], codes: [200, 401, 413, 415, 422, 502] },
      { method: "POST", path: "/api/speech-to-text/sync", summary: "Chép lời một file, trả kết quả ngay",
        params: [
          { name: "file", in: "form", type: "file", required: true },
          { name: "spellcheck", in: "form", type: "bool", def: "true" },
          { name: "include_raw", in: "form", type: "bool", def: "false" },
        ], codes: [200, 401, 413, 415, 422, 502] },
      { method: "POST", path: "/api/book-metadata/sync", summary: "Thông tin xuất bản, trả kết quả ngay",
        params: [{ name: "files", in: "form", type: "files", required: true, desc: "Các file của cùng một cuốn sách" }],
        codes: [200, 401, 413, 415, 422] },
      { method: "POST", path: "/api/spellcheck", summary: "Sửa chính tả một đoạn văn bản",
        desc: "text tối đa 200.000 ký tự, nhiều dòng cách nhau bằng \\n.",
        body: { text: "Hôm nay troi đẹp.\nChúng ta đi học bài mới." }, codes: [200, 401, 422] },
    ],
  },
  {
    title: "Kết quả OCR đã lưu", note: "/api/documents",
    ops: [
      { method: "GET", path: "/api/documents", summary: "Danh sách tài liệu (mới nhất trước)",
        params: [...PAGING(20, 100), { name: "document_id", in: "query", desc: "Lọc theo mã phía client" }], codes: [200, 401, 503] },
      { method: "GET", path: "/api/documents/{request_id}", summary: "Tổng quan một tài liệu", params: [RID], codes: [200, 401, 404, 503] },
      { method: "GET", path: "/api/documents/{request_id}/pages/{page}", summary: "Mọi dòng của một trang",
        params: [RID, { name: "page", in: "path", type: "number", required: true, def: 1 }], codes: [200, 401, 404, 503] },
      { method: "GET", path: "/api/documents/{request_id}/changes", summary: "Các dòng đã sửa",
        params: [RID, { name: "page", in: "query", type: "number" }, { name: "by", in: "query", type: "select", options: BY },
          ...PAGING(50, 500)], codes: [200, 401, 404, 503] },
    ],
  },
  {
    title: "Bản chép lời đã lưu", note: "/api/transcripts",
    ops: [
      { method: "GET", path: "/api/transcripts", summary: "Danh sách bản chép lời",
        params: [...PAGING(20, 100), { name: "document_id", in: "query" }], codes: [200, 401, 503] },
      { method: "GET", path: "/api/transcripts/{request_id}", summary: "Tổng quan một bản chép lời", params: [RID], codes: [200, 401, 404, 503] },
      { method: "GET", path: "/api/transcripts/{request_id}/segments", summary: "Các đoạn theo thời gian",
        params: [RID, { name: "file", in: "query", desc: "Tên file audio" },
          { name: "changed", in: "query", type: "select", options: ["", "true", "false"] }, ...PAGING(100, 1000)],
        codes: [200, 401, 404, 503] },
      { method: "GET", path: "/api/transcripts/{request_id}/changes", summary: "Các đoạn đã sửa",
        params: [RID, { name: "file", in: "query" }, { name: "by", in: "query", type: "select", options: BY }, ...PAGING(50, 500)],
        codes: [200, 401, 404, 503] },
    ],
  },
  {
    title: "Thông tin xuất bản đã lưu", note: "/api/book-metadata",
    ops: [
      { method: "GET", path: "/api/book-metadata", summary: "Danh sách",
        params: [...PAGING(20, 100), { name: "document_id", in: "query" }], codes: [200, 401, 503] },
      { method: "GET", path: "/api/book-metadata/{request_id}", summary: "Một bản ghi", params: [RID], codes: [200, 401, 404, 503] },
    ],
  },
  {
    title: "Quản lý worker", note: "quản trị",
    ops: [
      { method: "GET", path: "/api/workers/status", summary: "Worker đang chạy (qua Flower)",
        params: [{ name: "queue_name", in: "query", type: "select", options: ["", ...QUEUES] }], codes: [200, 401] },
      { method: "GET", path: "/api/workers/presets", summary: "Số worker mặc định từng hàng đợi", codes: [200, 401] },
      { method: "POST", path: "/api/workers/start", summary: "Bật thêm worker",
        desc: "num_workers 1–16, concurrency 1–8. queue_name: " + QUEUES.join(", "),
        body: { queue_name: "fullpage_ocr_queue", num_workers: 1, concurrency: 1 }, codes: [200, 401, 422] },
      { method: "POST", path: "/api/workers/stop", summary: "Dừng worker của một hàng đợi",
        body: { queue_name: "fullpage_ocr_queue" }, codes: [200, 401, 422] },
      { method: "POST", path: "/api/workers/restart", summary: "Khởi động lại worker",
        desc: "num_workers, concurrency có thể bỏ (giữ như cũ).",
        body: { queue_name: "fullpage_ocr_queue", num_workers: 2, concurrency: 1 }, codes: [200, 401, 422] },
    ],
  },
  {
    title: "Hệ thống", note: "",
    ops: [
      { method: "GET", path: "/health", summary: "Backend còn chạy", noAuth: true, codes: [200] },
    ],
  },
];

const CODE_TEXT = {
  200: "OK", 202: "Đã nhận việc", 400: "Thiếu S3 / callback không được phép", 401: "Sai tên đăng nhập / mật khẩu",
  404: "Không tìm thấy", 413: "Quá lớn", 415: "Định dạng không hỗ trợ", 422: "Dữ liệu sai",
  502: "Model không phản hồi", 503: "Chưa cấu hình DB",
};

// Tiện ích

function load(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* bỏ qua */ }
}
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c != null) node.append(c instanceof Node ? c : String(c));
  return node;
}
const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
const shq = (s) => "'" + String(s).replace(/'/g, "'\\''") + "'";

function highlight(json) {
  if (json.length > 2_000_000) return esc(json);
  return esc(json).replace(
    /("(?:\\u[\da-fA-F]{4}|\\[^u]|[^\\"])*"(\s*:)?|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)/g,
    (m, str, colon) => {
      if (str) return colon ? `<span class="j-key">${str.slice(0, -colon.length)}</span>${colon}` : `<span class="j-str">${m}</span>`;
      return /^[tfn]/.test(m) ? `<span class="j-lit">${m}</span>` : `<span class="j-num">${m}</span>`;
    });
}

async function copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    const old = btn.textContent;
    btn.textContent = "Đã chép";
    setTimeout(() => { btn.textContent = old; }, 1200);
  } catch { /* trình duyệt chặn clipboard */ }
}

function codeBox(text, html) {
  const pre = el("pre");
  if (html) pre.innerHTML = html; else pre.textContent = text;
  const btn = el("button", { class: "btn copy", type: "button" }, "Chép");
  btn.addEventListener("click", () => copyText(text, btn));
  return el("div", { class: "codebox" }, pre, btn);
}

// Trạng thái backend, request_id gần nhất

async function checkHealth() {
  try {
    const r = await fetch("/health");
    $("health").className = "dot " + (r.ok ? "ok" : "bad");
    $("health").title = r.ok ? "Backend đang chạy" : `Backend lỗi (HTTP ${r.status})`;
  } catch {
    $("health").className = "dot bad";
    $("health").title = "Không kết nối được";
  }
}
checkHealth();
setInterval(checkHealth, 30000);

let lastId = load("apiLastRequestId", "");
const ridInputs = [];
function setLastId(id) {
  if (!id || id === lastId) return;
  const prev = lastId;
  lastId = id;
  save("apiLastRequestId", id);
  $("lastId").textContent = id;
  for (const input of ridInputs) if (!input.value || input.value === prev) input.value = id;
}
$("lastId").textContent = lastId || "—";

// Dựng giao diện

function paramRow(p) {
  let input;
  if (p.type === "file" || p.type === "files") {
    input = el("input", { type: "file", multiple: p.type === "files" });
  } else if (p.type === "bool" || p.type === "select") {
    const opts = p.type === "bool" ? ["true", "false"] : p.options;
    input = el("select", {}, opts.map((o) => el("option", { value: o, selected: o === String(p.def ?? "") }, o || "—")));
  } else {
    input = el("input", { type: p.type === "number" ? "number" : "text", placeholder: p.def != null ? String(p.def) : "" });
    if (p.def != null) input.value = p.def;
    if (p.name === "request_id") { input.value = lastId; ridInputs.push(input); }
  }
  input.dataset.name = p.name;
  input.dataset.in = p.in;
  input.dataset.type = p.type || "string";
  const type = { file: "file", files: "file (nhiều)", bool: "boolean", number: "number", select: "string" }[p.type] || "string";
  return {
    input,
    row: el("tr", {},
      el("td", {},
        el("span", { class: "pname mono" }, p.name), p.required ? el("span", { class: "req" }, "* bắt buộc") : null,
        el("span", { class: "ptype" }, `${type} · ${p.in}`)),
      el("td", {}, input, p.desc ? el("span", { class: "pdesc" }, p.desc) : null)),
  };
}

function buildOp(op) {
  const details = el("details", { class: `op ${op.method.toLowerCase()}` });
  details.dataset.search = `${op.method} ${op.path} ${op.summary} ${op.desc || ""}`.toLowerCase();
  details.append(el("summary", {},
    el("span", { class: "method" }, op.method),
    el("span", { class: "path mono" }, op.path),
    op.noAuth ? el("span", { class: "lock" }, "không cần khóa") : null,
    el("span", { class: "summary" }, op.summary)));

  const body = el("div", { class: "op-body" });
  details.append(body);
  if (op.desc) body.append(el("div", { class: "desc" }, el("p", {}, op.desc)));

  const inputs = [];
  if (op.params?.length) {
    const rows = op.params.map(paramRow);
    inputs.push(...rows.map((r) => r.input));
    body.append(el("div", {}, el("h3", {}, "Tham số"), el("table", { class: "params" }, el("tbody", {}, rows.map((r) => r.row)))));
  }

  let textarea = null;
  if (op.body) {
    const example = JSON.stringify(op.body, null, 2);
    textarea = el("textarea", { class: "body", spellcheck: "false" });
    textarea.value = example;
    textarea.rows = Math.min(18, example.split("\n").length + 1);
    textarea.addEventListener("input", () => {
      try { JSON.parse(textarea.value); textarea.classList.remove("invalid"); } catch { textarea.classList.add("invalid"); }
    });
    const tools = el("div", { class: "body-tools" },
      el("button", { class: "link", type: "button", onclick: () => { textarea.value = example; textarea.classList.remove("invalid"); } }, "Khôi phục mẫu"),
      el("button", { class: "link", type: "button", onclick: () => {
        try { textarea.value = JSON.stringify(JSON.parse(textarea.value), null, 2); } catch { /* giữ nguyên */ }
      } }, "Định dạng lại"),
      el("span", { class: "muted mono" }, "application/json"));
    body.append(el("div", {}, el("h3", {}, "Body"), textarea, tools));
  }

  if (op.codes) {
    body.append(el("div", {}, el("h3", {}, "Mã trả về"),
      el("div", { class: "codes" }, op.codes.map((c) => el("span", {}, el("b", {}, c), CODE_TEXT[c] || "")))));
  }

  const runBtn = el("button", { class: "btn primary", type: "button" }, "Gửi");
  const stopBtn = el("button", { class: "btn", type: "button", hidden: true }, "Dừng");
  const pollBox = op.poll ? el("input", { type: "checkbox", checked: true }) : null;
  const runRow = el("div", { class: "run-row" }, runBtn, stopBtn,
    pollBox ? el("label", {}, pollBox, "Tự hỏi lại mỗi 4 giây đến khi SUCCESS / FAILURE") : null);
  const out = el("div", { class: "out" });
  body.append(runRow, out);

  let controller = null;
  stopBtn.addEventListener("click", () => controller?.abort());
  runBtn.addEventListener("click", async () => {
    let req;
    try { req = buildRequest(op, inputs, textarea); } catch (err) {
      out.replaceChildren(el("p", { class: "error" }, err.message));
      return;
    }
    controller = new AbortController();
    runBtn.disabled = true;
    stopBtn.hidden = false;
    try {
      for (let round = 1; ; round++) {
        const result = await send(req, controller.signal);
        render(out, req, result, round);
        const status = result.json?.status;
        if (!pollBox?.checked || result.status !== 200 || !["PENDING", "STARTED", "RETRY"].includes(status)) break;
        await new Promise((resolve, reject) => {
          const t = setTimeout(resolve, 4000);
          controller.signal.addEventListener("abort", () => { clearTimeout(t); reject(new DOMException("", "AbortError")); }, { once: true });
        });
      }
    } catch (err) {
      if (err.name === "AbortError") out.prepend(el("p", { class: "warn" }, "Đã dừng."));
      else out.replaceChildren(el("p", { class: "error" }, String(err.message || err)));
    } finally {
      runBtn.disabled = false;
      stopBtn.hidden = true;
      controller = null;
    }
  });
  return details;
}

function buildRequest(op, inputs, textarea) {
  let path = op.path;
  const query = new URLSearchParams();
  let form = null;
  const formShow = [];
  for (const input of inputs) {
    const { name, type } = input.dataset;
    const where = input.dataset.in;
    const param = op.params.find((p) => p.name === name);
    if (type === "file" || type === "files") {
      if (!input.files.length) {
        if (param.required) throw new Error(`Chưa chọn file cho "${name}".`);
        continue;
      }
      form ??= new FormData();
      for (const f of input.files) { form.append(name, f); formShow.push(`-F ${shq(`${name}=@${f.name}`)}`); }
      continue;
    }
    const value = input.value.trim();
    if (!value) {
      if (param.required) throw new Error(`Thiếu giá trị cho "${name}".`);
      continue;
    }
    if (where === "path") path = path.replace(`{${name}}`, encodeURIComponent(value));
    else if (where === "query") query.set(name, value);
    else { form ??= new FormData(); form.append(name, value); formShow.push(`-F ${shq(`${name}=${value}`)}`); }
  }
  const qs = query.toString();
  const url = path + (qs ? `?${qs}` : "");
  const headers = {};
  if (!op.noAuth) Object.assign(headers, window.authHeaders());

  let body = null;
  const curl = [`curl -X ${op.method} ${shq(location.origin + url)}`];
  if (!op.noAuth) curl.push('-u "$API_USERNAME:$API_PASSWORD"');
  if (textarea) {
    let parsed;
    try { parsed = JSON.parse(textarea.value); } catch (err) { throw new Error(`Body không phải JSON hợp lệ: ${err.message}`); }
    body = JSON.stringify(parsed);
    headers["Content-Type"] = "application/json";
    curl.push(`-H ${shq("Content-Type: application/json")}`, `-d ${shq(body)}`);
  } else if (form) {
    body = form;
    curl.push(...formShow);
  }
  return { method: op.method, url, headers, body, curl: curl.join(" \\\n  ") };
}

async function send(req, signal) {
  const started = performance.now();
  let resp;
  try {
    resp = await fetch(req.url, { method: req.method, headers: req.headers, body: req.body, signal });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    return { status: 0, ms: Math.round(performance.now() - started), text: String(err), json: null, type: "" };
  }
  const text = await resp.text();
  const ms = Math.round(performance.now() - started);
  let json = null;
  try { json = JSON.parse(text); } catch { /* không phải JSON */ }
  if (json && resp.ok) {
    if (typeof json.request_id === "string") setLastId(json.request_id);
    else if (Array.isArray(json.jobs) && json.jobs[0]?.request_id) setLastId(json.jobs[0].request_id);
  }
  return { status: resp.status, statusText: resp.statusText, ms, text, json, type: resp.headers.get("content-type") || "" };
}

function render(out, req, res, round) {
  const pretty = res.json != null ? JSON.stringify(res.json, null, 2) : res.text;
  const download = el("button", { class: "link", type: "button" }, "Tải JSON");
  download.addEventListener("click", () => {
    const a = el("a", { href: URL.createObjectURL(new Blob([pretty], { type: "application/json" })), download: "response.json" });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  const size = new Blob([res.text]).size;
  out.replaceChildren(
    el("div", {}, el("h3", {}, "curl"), codeBox(req.curl)),
    el("div", {}, el("h3", {}, "URL"), codeBox(location.origin + req.url)),
    el("div", {},
      el("h3", {}, "Response"),
      el("div", { class: "resp-head" },
        el("span", { class: `status s${String(res.status)[0]}` }, res.status || "Lỗi mạng"),
        el("span", { class: "muted" }, res.statusText || ""),
        el("span", { class: "muted" }, `${res.ms} ms`),
        el("span", { class: "muted" }, size < 1024 ? `${size} B` : `${(size / 1024).toFixed(1)} KB`),
        round > 1 ? el("span", { class: "muted" }, `lần hỏi thứ ${round}`) : null,
        el("span", { class: "resp-actions" }, download)),
      codeBox(pretty, res.json != null ? highlight(pretty) : null)),
  );
}

// Danh sách API và bộ lọc

for (const g of GROUPS) {
  $("groups").append(el("section", { class: "group" },
    el("h2", {}, g.title, g.note ? el("small", {}, g.note) : null),
    g.ops.map(buildOp)));
}

$("filter").addEventListener("input", () => {
  const q = $("filter").value.trim().toLowerCase();
  for (const g of document.querySelectorAll(".group")) {
    let shown = 0;
    for (const op of g.querySelectorAll(".op")) {
      const hit = !q || op.dataset.search.includes(q);
      op.hidden = !hit;
      shown += hit;
    }
    g.hidden = !shown;
  }
});
$("expandAll").addEventListener("click", () => document.querySelectorAll(".op:not([hidden])").forEach((d) => { d.open = true; }));
$("collapseAll").addEventListener("click", () => document.querySelectorAll(".op").forEach((d) => { d.open = false; }));
