const PAGE_SIZE = 20;

const state = {
  posts: [],
  comments: [],
  activeTab: "posts",
  postsPage: 0,
  commentsPage: 0,
  search: "",
  timer: null,
  startedAt: 0,
  busy: false,
  localBackend: false,
  localCredentialsConfigured: false,
  activeLocalJob: "",
  loadedSourceUrl: "",
  savedRevision: null,
  savedSyncWarning: "",
};

const SOURCE_STORAGE_KEY = "group-pulse-last-source-url";
const JOB_STORAGE_KEY = "group-pulse-active-job";

function savedLocalJob() {
  try { return JSON.parse(localStorage.getItem(JOB_STORAGE_KEY) || "null"); } catch { return null; }
}

function rememberLocalJob(job) {
  try {
    if (job) localStorage.setItem(JOB_STORAGE_KEY, JSON.stringify(job));
    else localStorage.removeItem(JOB_STORAGE_KEY);
  } catch { /* The server also exposes the active job for recovery. */ }
}

const byId = (id) => document.getElementById(id);
const form = byId("scrape-form");
const sourceUrlInput = byId("source-url");
const formError = byId("form-error");
const statusBox = byId("run-status");

const REACTIONS = [
  ["reactionLikeCount", "Like", "👍"],
  ["reactionLoveCount", "Love", "❤️"],
  ["reactionCareCount", "Care", "🥰"],
  ["reactionHahaCount", "Haha", "😆"],
  ["reactionWowCount", "Wow", "😮"],
  ["reactionSadCount", "Sad", "😢"],
  ["reactionAngryCount", "Angry", "😡"],
];

function number(value) {
  if (value === null || value === undefined || value === "") return 0;
  const parsed = Number(String(value).replaceAll(",", ""));
  return Number.isFinite(parsed) ? parsed : 0;
}

function formatNumber(value) {
  return new Intl.NumberFormat("vi-VN").format(number(value));
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("vi-VN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function setStatus(stateName, title, detail) {
  statusBox.dataset.state = stateName;
  byId("status-title").textContent = title;
  byId("status-detail").textContent = detail;
}

function setFormError(message = "") {
  formError.textContent = message;
  formError.hidden = !message;
}

function updateSummary() {
  const sourceUrl = sourceUrlInput.value.trim();
  try {
    const parsed = new URL(sourceUrl);
    byId("summary-source").textContent = `${parsed.hostname}${parsed.pathname.replace(/\/+$/, "")}` || "—";
  } catch {
    byId("summary-source").textContent = sourceUrl || "—";
  }
  byId("summary-post-count").textContent = "Tất cả";
  byId("summary-comment-count").textContent = "Tất cả";
  byId("summary-replies").textContent = byId("include-replies").checked ? "Có" : "Không";
}

function updateLocalStatus() {
  byId("local-mode-status").textContent = state.localBackend
    ? state.localCredentialsConfigured
      ? "Backend local sẵn sàng. Mật khẩu Facebook không đi qua trình duyệt web."
      : "Backend đã chạy; hãy điền FACEBOOK_EMAIL và FACEBOOK_PASSWORD vào web/.env rồi khởi động lại server."
    : "Hãy chạy web/local_server.py rồi mở trang tại http://127.0.0.1:4173.";
  byId("playwright-credentials-status").textContent = state.localCredentialsConfigured
    ? "Thông tin đăng nhập đọc từ web/.env; phiên Chromium được lưu cục bộ giữa các lần chạy."
    : "Cần thêm FACEBOOK_EMAIL và FACEBOOK_PASSWORD vào web/.env trước khi chạy.";
}

async function detectLocalBackend() {
  if (!["localhost", "127.0.0.1"].includes(window.location.hostname)) {
    updateLocalStatus();
    return;
  }
  try {
    const payload = await localApiJson("/api/health");
    if (!payload.localBackend) throw new Error("Local API unavailable");
    state.localBackend = true;
    state.localCredentialsConfigured = Boolean(payload.credentialsConfigured);
    updateLocalStatus();
  } catch {
    state.localBackend = false;
    state.localCredentialsConfigured = false;
    updateLocalStatus();
  }
}

function normalizeUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    url.search = "";
    url.hash = "";
    url.pathname = url.pathname.replace(/\/+$/, "");
    return url.toString().toLowerCase();
  } catch {
    return String(value).trim().toLowerCase();
  }
}

async function loadSavedDataset(sourceUrl, { showStatus = true } = {}) {
  const requestedKey = normalizeUrl(sourceUrl);
  if (!requestedKey || !state.localBackend) return;
  try {
    const data = await localApiJson(`/api/data?sourceUrl=${encodeURIComponent(sourceUrl)}`);
    if (normalizeUrl(sourceUrlInput.value) !== requestedKey) return;
    state.posts = Array.isArray(data.posts) ? data.posts : [];
    state.comments = Array.isArray(data.comments) ? data.comments : [];
    state.loadedSourceUrl = requestedKey;
    state.savedRevision = Number.isSafeInteger(data.revision) ? data.revision : null;
    state.savedSyncWarning = "";
    byId("results").hidden = state.posts.length === 0 && state.comments.length === 0;
    byId("results-caption").textContent = data.lastScrapedAt
      ? `Dữ liệu lưu trên máy · ${sourceUrl} · cập nhật ${formatDate(data.lastScrapedAt)}`
      : sourceUrl;
    renderAll();
    if (showStatus && (state.posts.length || state.comments.length)) {
      setStatus("success", "Đã nạp dữ liệu đã lưu", `Có ${formatNumber(state.posts.length)} bài đăng và ${formatNumber(state.comments.length)} bình luận trên máy.`);
    }
    return data;
  } catch (error) {
    if (showStatus) setFormError(error.message);
    return null;
  }
}

async function restoreSavedSource() {
  let savedSource = "";
  try { savedSource = localStorage.getItem(SOURCE_STORAGE_KEY) || ""; } catch { /* local storage may be disabled */ }
  if (!savedSource || !state.localBackend) return;
  sourceUrlInput.value = savedSource;
  updateSummary();
  await loadSavedDataset(savedSource);
  let job = savedLocalJob();
  if (job && normalizeUrl(job.sourceUrl) !== normalizeUrl(savedSource)) job = null;
  if (!job) {
    try { job = (await localApiJson('/api/jobs/active?sourceUrl='+encodeURIComponent(savedSource))).job; } catch { /* Older backend or connection unavailable. */ }
  }
  if (job?.id) await handleScrape({ preventDefault() {} }, job);
}

function savedRecordKey(url, fallback = "") {
  try {
    const parsed = new URL(url);
    parsed.hostname = parsed.hostname.replace(/^(www|m|web)\./, "");
    parsed.pathname = parsed.pathname.replace(/\/posts\/(\d+)/, "/permalink/$1");
    return normalizeUrl(parsed.toString());
  } catch { return String(fallback); }
}

async function syncSavedChanges(sourceUrl) {
  const sourceKey = normalizeUrl(sourceUrl);
  if (state.savedRevision === null || state.loadedSourceUrl !== sourceKey) return;
  try {
    const data = await localApiJson(`/api/data/changes?sourceUrl=${encodeURIComponent(sourceUrl)}&afterRevision=${state.savedRevision}`, { retry: false, timeoutMs: 5000 });
    if (state.loadedSourceUrl !== sourceKey || normalizeUrl(sourceUrlInput.value) !== sourceKey) return;
    if (!Number.isSafeInteger(data.revision) || data.revision < state.savedRevision || !Array.isArray(data.posts) || !Array.isArray(data.comments)) {
      throw new Error("Invalid saved-data update");
    }
    if (data.posts.length) {
      const changed = new Map(data.posts.map(post => [savedRecordKey(postUrl(post), post.id), post]));
      const existing = new Set(state.posts.map(post => savedRecordKey(postUrl(post), post.id)));
      state.posts = [
        ...data.posts.filter(post => !existing.has(savedRecordKey(postUrl(post), post.id))),
        ...state.posts.map(post => changed.get(savedRecordKey(postUrl(post), post.id)) || post),
      ];
      state.posts.sort((a, b) => {
        const aTime = Date.parse(a.time || a.timestamp || a.date || "");
        const bTime = Date.parse(b.time || b.timestamp || b.date || "");
        if (Number.isFinite(aTime) && Number.isFinite(bTime)) return bTime - aTime;
        return Number.isFinite(aTime) ? -1 : Number.isFinite(bTime) ? 1 : 0;
      });
      state.comments = state.comments.filter(row => !changed.has(savedRecordKey(commentParentUrl(row), row.postId))).concat(data.comments);
      byId("results").hidden = false;
      renderAll();
    }
    state.savedRevision = data.revision;
    state.savedSyncWarning = "";
  } catch {
    // Retain the cursor and retry next poll. A display outage must not stop
    // scraping or imply that the backend failed to commit its data.
    state.savedSyncWarning = " Chưa cập nhật được kho trên giao diện; đang thử lại.";
  }
}

function postUrl(post) {
  return post.url || post.postUrl || post.facebookUrl || post.inputUrl || "";
}

function commentParentUrl(comment) {
  return comment.parentPostUrl || comment.facebookUrl || comment.postUrl || comment.postURL || comment.inputUrl || "";
}

function postReactionTotal(post) {
  if (post.reactionBreakdownComplete === true) {
    return REACTIONS.reduce((sum, [key]) => sum + number(post[key]), 0);
  }
  if (post.likesCount !== undefined && post.likesCount !== null && post.likesCount !== "") return number(post.likesCount);
  return null;
}

function formatOptionalNumber(value) {
  return value === null || value === undefined || value === "" ? "—" : formatNumber(value);
}

function safeFacebookUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !(url.hostname === "facebook.com" || url.hostname.endsWith(".facebook.com"))) return "";
    return url.href;
  } catch {
    return "";
  }
}

function safeFacebookMediaUrl(value) {
  try {
    const url = new URL(value);
    const host = url.hostname.toLowerCase();
    const allowed = host === "fbcdn.net" || host.endsWith(".fbcdn.net") ||
      host === "fbsbx.com" || host.endsWith(".fbsbx.com") ||
      host === "facebook.com" || host.endsWith(".facebook.com");
    return url.protocol === "https:" && allowed ? url.href : "";
  } catch {
    return "";
  }
}

function savedMediaUrl(item) {
  const local = String(item.localUrl || "");
  if (/^\/api\/media\/[a-f0-9]{64}\.(?:jpg|png|webp|gif|img)$/.test(local)) return local;
  return safeFacebookMediaUrl(item.url);
}

function safeFacebookSourceUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !(url.hostname === "facebook.com" || url.hostname.endsWith(".facebook.com"))) return "";
    if (url.username || url.password || !url.pathname || url.pathname === "/") return "";
    return url.href;
  } catch {
    return "";
  }
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function addCell(row, value, className = "") {
  const cell = node("td", className, value === undefined || value === null || value === "" ? "—" : value);
  row.appendChild(cell);
  return cell;
}

function filteredPosts() {
  const query = state.search.trim().toLocaleLowerCase();
  if (!query) return state.posts;
  return state.posts.filter((post) => `${post.user?.name || post.author || post.profileName || ""} ${post.text || post.caption || ""}`.toLocaleLowerCase().includes(query));
}

function filteredComments() {
  const query = state.search.trim().toLocaleLowerCase();
  if (!query) return state.comments;
  return state.comments.filter((comment) => `${comment.profileName || comment.author || ""} ${comment.text || ""}`.toLocaleLowerCase().includes(query));
}

function renderPagination(container, items, page, onPage) {
  container.replaceChildren();
  const pageCount = Math.ceil(items.length / PAGE_SIZE);
  if (pageCount <= 1) return;
  const label = node("span", "page-count", `${formatNumber(page + 1)} / ${formatNumber(pageCount)}`);
  const previous = node("button", "", "‹");
  previous.type = "button";
  previous.disabled = page <= 0;
  previous.setAttribute("aria-label", "Trang trước");
  previous.addEventListener("click", () => onPage(page - 1));
  const next = node("button", "", "›");
  next.type = "button";
  next.disabled = page >= pageCount - 1;
  next.setAttribute("aria-label", "Trang tiếp theo");
  next.addEventListener("click", () => onPage(page + 1));
  container.append(label, previous, next);
}

function renderPosts() {
  const items = filteredPosts();
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  state.postsPage = Math.min(state.postsPage, pageCount - 1);
  const pageItems = items.slice(state.postsPage * PAGE_SIZE, (state.postsPage + 1) * PAGE_SIZE);
  const body = byId("posts-table-body");
  body.replaceChildren();

  for (const post of pageItems) {
    const row = document.createElement("tr");
    const postCell = node("td", "post-cell");
    const authorName = post.user?.name || post.author || post.profileName || "Không rõ tác giả";
    const author = node("div", "post-author");
    author.append(node("span", "author-avatar", authorName.slice(0, 1).toLocaleUpperCase()), node("span", "", authorName));
    postCell.appendChild(author);
    postCell.appendChild(node("div", "post-text", post.text || post.caption || "(Bài đăng không có văn bản)"));
    const media = Array.isArray(post.media) ? post.media : [];
    const mediaLinks = media
      .map((item, index) => ({ item, url: savedMediaUrl(item), index }))
      .filter(({ url }) => url);
    if (mediaLinks.length) {
      const mediaList = node("div", "post-media-list");
      mediaLinks.forEach(({ item, url, index }) => {
        const kind = item.type === "video" ? "Video" : "Ảnh";
        const link = node("a", "post-media-link", `${kind} ${index + 1} ↗`);
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer";
        if (item.alt) link.title = item.alt;
        mediaList.appendChild(link);
      });
      postCell.appendChild(mediaList);
    }
    if (post.ocrText) {
      postCell.appendChild(node("div", "post-ocr-text", `Chữ trong ảnh: ${post.ocrText}`));
    } else if (post.ocrStatus === "unavailable" || post.ocrStatus === "partial") {
      postCell.appendChild(node("div", "post-ocr-note", "Chưa nhận dạng được chữ từ một số ảnh."));
    }
    const linkValue = safeFacebookUrl(postUrl(post));
    if (linkValue) {
      const linkPath = new URL(linkValue).pathname;
      const isGroupPermalink = /\/groups\/[^/]+\/permalink\/\d+\/?$/i.test(linkPath);
      const linkLabel = isGroupPermalink ? `Permalink: ${linkPath}` : "Mở bài đăng ↗";
      const link = node("a", "post-url", linkLabel);
      link.href = linkValue;
      link.title = linkValue;
      link.target = "_blank";
      link.rel = "noreferrer";
      postCell.appendChild(link);
    }
    row.appendChild(postCell);
    addCell(row, formatDate(post.time || post.timestamp || post.date), "date-cell");
    const reactionCell = node("td", "numeric-cell");
    reactionCell.appendChild(node("div", "", formatOptionalNumber(postReactionTotal(post))));
    const knownBreakdown = REACTIONS
      .filter(([key]) => post[key] !== undefined && post[key] !== null && post[key] !== "")
      .map(([key, label, emoji]) => `${emoji} ${formatNumber(post[key])}`);
    if (knownBreakdown.length) reactionCell.appendChild(node("div", "post-reaction-breakdown", knownBreakdown.join(" · ")));
    row.appendChild(reactionCell);
    addCell(row, formatNumber(post.commentsCount), "comments-cell");
    addCell(row, formatNumber(post.sharesCount), "numeric-cell");
    body.appendChild(row);
  }

  byId("posts-empty").hidden = pageItems.length > 0;
  renderPagination(byId("posts-pagination"), items, state.postsPage, (page) => { state.postsPage = page; renderPosts(); });
  byId("posts-tab-count").textContent = formatNumber(state.posts.length);
}

function renderComments() {
  const items = filteredComments();
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));
  state.commentsPage = Math.min(state.commentsPage, pageCount - 1);
  const pageItems = items.slice(state.commentsPage * PAGE_SIZE, (state.commentsPage + 1) * PAGE_SIZE);
  const body = byId("comments-table-body");
  body.replaceChildren();

  for (const comment of pageItems) {
    const row = document.createElement("tr");
    const contentCell = node("td", "comment-cell");
    contentCell.appendChild(node("div", "comment-text", comment.text || "(Bình luận không có văn bản)"));
    const parentCell = node("td", "comment-parent-cell");
    const parent = safeFacebookUrl(commentParentUrl(comment));
    if (parent) {
      const label = comment.postAuthor
        ? "\u0042\u00e0i vi\u1ebft: " + comment.postAuthor + " \u2197"
        : "\u004d\u1edf b\u00e0i vi\u1ebft \u2197";
      const link = node("a", "comment-post-link", label);
      link.href = parent;
      link.target = "_blank";
      link.rel = "noreferrer";
      parentCell.appendChild(link);
    }
    row.appendChild(contentCell);
    row.appendChild(parentCell);
    addCell(row, comment.profileName || comment.author || "Không rõ", "");
    addCell(row, formatDate(comment.date || comment.time || comment.timestamp), "date-cell");
    addCell(row, formatNumber(comment.likesCount), "numeric-cell");
    addCell(row, comment.threadingDepth === undefined ? "—" : formatNumber(comment.threadingDepth), "numeric-cell");
    body.appendChild(row);
  }

  byId("comments-empty").hidden = pageItems.length > 0;
  renderPagination(byId("comments-pagination"), items, state.commentsPage, (page) => { state.commentsPage = page; renderComments(); });
  byId("comments-tab-count").textContent = formatNumber(state.comments.length);
}

function renderReactions() {
  const knownTotals = state.posts.map(postReactionTotal).filter((value) => value !== null);
  const total = knownTotals.reduce((sum, value) => sum + value, 0);
  byId("reaction-total-label").textContent = knownTotals.length
    ? `${formatNumber(total)} tổng lượt`
    : "Chưa có tổng lượt";
  const host = byId("reaction-bars");
  host.replaceChildren();
  const reactionTotals = REACTIONS.map(([key, label, emoji]) => {
    const known = state.posts.filter((post) => post[key] !== undefined && post[key] !== null && post[key] !== "");
    return { key, label, emoji, knownCount: known.length, total: known.reduce((sum, post) => sum + number(post[key]), 0) };
  });
  const largest = Math.max(0, ...reactionTotals.map((reaction) => reaction.total));
  for (const reaction of reactionTotals) {
    const row = node("div", "reaction-bar-row");
    row.appendChild(node("span", "reaction-name", `${reaction.emoji} ${reaction.label}`));
    const track = node("span", "reaction-track");
    const fill = node("span", "reaction-fill");
    fill.style.width = largest ? `${(reaction.total / largest) * 100}%` : "0%";
    track.appendChild(fill);
    row.append(track, node("span", "reaction-count", reaction.knownCount ? formatNumber(reaction.total) : "—"));
    host.appendChild(row);
  }
  const postsWithBreakdown = state.posts.filter((post) => post.reactionBreakdownAvailable).length;
  const completePosts = state.posts.filter((post) => post.reactionBreakdownComplete).length;
  if (!state.posts.length) {
    byId("reaction-note").textContent = "Chưa có bài đăng để tổng hợp cảm xúc.";
  } else if (!postsWithBreakdown) {
    byId("reaction-note").textContent = "Facebook chưa hiển thị số liệu theo từng loại cảm xúc trong các bài đã đọc. “—” nghĩa là chưa có dữ liệu, không phải 0.";
  } else if (completePosts === state.posts.length) {
    byId("reaction-note").textContent = `Đã đọc đủ breakdown cho ${formatNumber(completePosts)} bài đăng.`;
  } else {
    byId("reaction-note").textContent = `Đã đọc breakdown cho ${formatNumber(postsWithBreakdown)} / ${formatNumber(state.posts.length)} bài; “—” nghĩa là Facebook chưa cung cấp số liệu cho loại đó.`;
  }
}

function renderMetrics() {
  const commentsCount = state.comments.length;
  const knownReactions = state.posts.map(postReactionTotal).filter((value) => value !== null);
  const reactions = knownReactions.reduce((sum, value) => sum + value, 0);
  const shares = state.posts.reduce((sum, post) => sum + number(post.sharesCount), 0);
  byId("metric-posts").textContent = formatNumber(state.posts.length);
  byId("metric-comments").textContent = formatNumber(commentsCount);
  byId("metric-reactions").textContent = knownReactions.length ? formatNumber(reactions) : "—";
  byId("metric-shares").textContent = formatNumber(shares);
  byId("export-json").disabled = state.posts.length === 0;
  byId("export-posts-csv").disabled = state.posts.length === 0;
  byId("export-comments-csv").disabled = state.comments.length === 0;
}

function renderAll() {
  renderPosts();
  renderComments();
  renderReactions();
  renderMetrics();
  if (state.activeTab === "raw") renderRawJson();
  byId("comments-note").textContent = state.comments.length
    ? `${formatNumber(state.comments.length)} bình luận và câu trả lời được đọc từ phiên Facebook đang đăng nhập.`
    : "";
}

function renderRawJson() {
  byId("raw-json").textContent = JSON.stringify({ posts: state.posts, comments: state.comments }, null, 2);
}

function setTab(tab) {
  state.activeTab = tab;
  for (const button of document.querySelectorAll(".tab")) {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  }
  for (const name of ["posts", "comments", "reactions", "raw"]) byId(`view-${name}`).hidden = name !== tab;
  if (tab === "raw") renderRawJson();
}

function csvCell(value) {
  let text = value === null || value === undefined ? "" : String(value);
  if (/^[\s]*[=+\-@]/.test(text)) text = `'${text}`;
  return `"${text.replaceAll('"', '""')}"`;
}

function exportSlug() {
  let sourceName = "";
  try {
    sourceName = new URL(sourceUrlInput.value).pathname.split("/").filter(Boolean).at(-1) || "";
  } catch {
    sourceName = "";
  }
  const slug = sourceName.toLocaleLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48);
  return slug || "facebook-data";
}

function downloadFile(filename, content, type) {
  const blob = new Blob(["\uFEFF", content], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function downloadPostsCsv() {
  const headers = ["groupTitle", "postId", "author", "time", "text", "url", "mediaUrls", "ocrText", "totalReactions", ...REACTIONS.map(([, label]) => `reaction${label}Count`), "commentsCount", "sharesCount"];
  const rows = state.posts.map((post) => [
    post.groupTitle || "",
    post.legacyId || post.facebookId || post.id || "",
    post.user?.name || post.author || post.profileName || "",
    post.time || post.timestamp || post.date || "",
    post.text || post.caption || "",
    postUrl(post),
    (Array.isArray(post.media) ? post.media : []).map((item) => item.url).filter(Boolean).join("\n"),
    post.ocrText || "",
    postReactionTotal(post),
    ...REACTIONS.map(([key]) => post[key] ?? ""),
    post.commentsCount ?? "",
    post.sharesCount ?? "",
  ]);
  downloadFile(`facebook-posts-${exportSlug()}.csv`, [headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
}

function downloadCommentsCsv() {
  const headers = ["postId", "postAuthor", "postUrl", "commentId", "author", "date", "text", "likesCount", "threadingDepth", "commentUrl"];
  const rows = state.comments.map((comment) => [
    comment.postId || "",
    comment.postAuthor || "",
    commentParentUrl(comment),
    comment.commentId || comment.id || "",
    comment.profileName || comment.author || "",
    comment.date || comment.time || "",
    comment.text || "",
    comment.likesCount ?? "",
    comment.threadingDepth ?? "",
    comment.commentUrl || "",
  ]);
  downloadFile(`facebook-comments-${exportSlug()}.csv`, [headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n"), "text/csv;charset=utf-8");
}

function setBusy(busy) {
  state.busy = busy;
  byId("run-button").disabled = busy;
  byId("cancel-button").hidden = !busy;
  byId('resume-history').disabled = busy;
  byId("run-button").innerHTML = busy
    ? '<span class="play-icon">◌</span> Đang chạy…'
    : '<span class="play-icon">▶</span> Bắt đầu scrape <span class="button-arrow">→</span>';
}

async function localApiJson(path, options = {}) {
  const method = options.method || 'GET';
  // A lost POST response may already have started work. Only GETs are retried.
  const attempts = method === 'GET' && options.retry !== false ? 3 : 1;
  for (let attempt = 0; attempt < attempts; attempt++) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), options.timeoutMs || 30000);
    try {
      const response = await fetch(path, {
        method,
        headers: options.body ? { 'Content-Type': 'application/json' } : {},
        ...(options.body ? { body: JSON.stringify(options.body) } : {}),
        cache: 'no-store', signal: controller.signal,
      });
      let payload;
      try { payload = await response.json(); } catch {
        const error = new Error('API trả về dữ liệu chưa đầy đủ. Đang thử nối lại.');
        error.retryable = true;
        throw error;
      }
      if (!response.ok) {
        const error = new Error(payload.error || `Local scraper lỗi HTTP ${response.status}.`);
        error.status = response.status;
        error.retryable = response.status >= 500 || response.status === 408 || response.status === 429;
        throw error;
      }
      return payload;
    } catch (failure) {
      const error = failure instanceof TypeError || failure.name === 'AbortError'
        ? Object.assign(new Error('Mất kết nối API local. Kiểm tra server tại http://127.0.0.1:4173; dữ liệu đã lưu vẫn còn trong kho.'), { retryable: true })
        : failure;
      if (!error.retryable || attempt + 1 >= attempts) throw error;
    } finally {
      window.clearTimeout(timer);
    }
    await new Promise(resolve => window.setTimeout(resolve, 500 * 2 ** attempt));
  }
}

async function runLocalScrape(input, resumeJob = null) {
  let started = resumeJob;
  if (!started) {
    try { started = (await localApiJson('/api/jobs/active?sourceUrl='+encodeURIComponent(input.sourceUrl))).job; } catch { /* The POST reports a persistent outage. */ }
  }
  if (!started) {
    try { started = await localApiJson('/api/jobs', { method: 'POST', body: input }); }
    catch (error) {
      if (!error.retryable) throw error;
      try { started = (await localApiJson('/api/jobs/active?sourceUrl='+encodeURIComponent(input.sourceUrl))).job; } catch { /* Do not repeat an ambiguous start request. */ }
      if (!started) throw error;
    }
  }
  state.activeLocalJob = started.id;
  const historyMode = typeof started.resumeHistory === 'boolean' ? started.resumeHistory : input.resumeHistory;
  if (typeof historyMode === 'boolean') byId('resume-history').checked = historyMode;
  rememberLocalJob({id: started.id, sourceUrl: input.sourceUrl, resumeHistory: historyMode, createdAt: started.createdAt || Date.now()/1000});
  let failures = 0;
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, Math.min(10000, 1400 * 2 ** Math.min(failures,3))));
    let job;
    try {
      if (state.cancelRequested && !state.cancelDelivered) {
        await localApiJson('/api/jobs/'+encodeURIComponent(started.id)+'/cancel',{method:'POST'});
        state.cancelDelivered = true;
      }
      job = await localApiJson(`/api/jobs/${encodeURIComponent(started.id)}?summary=1`, { retry:false });
      failures = 0;
    } catch (error) {
      if (!error.retryable) {
        rememberLocalJob(null);
        if (error.status === 404) throw new Error('Server không còn lượt chạy này, có thể đã khởi động lại. Bài đã lưu vẫn trong kho; bấm Bắt đầu để tiếp tục.');
        throw error;
      }
      failures++;
      byId('status-detail').textContent = 'Mất kết nối tạm thời — đang tự nối lại cùng lượt scrape. Dữ liệu trong kho được giữ nguyên.';
      continue;
    }
    if (['queued', 'running'].includes(job.status)) await syncSavedChanges(input.sourceUrl);
    byId("status-detail").textContent = `${job.progress || 0}% · ${job.message || "Đang thu thập dữ liệu…"}${state.savedSyncWarning}`;
    if (['completed','cancelled','failed'].includes(job.status)) {
      rememberLocalJob(null);
      if (job.status === 'failed') throw new Error(job.error || 'Facebook Scraper không hoàn tất.');
      // One final full response, instead of transferring the warehouse on polls.
      return localApiJson(`/api/jobs/${encodeURIComponent(started.id)}`);
    }
  }
}

async function handleScrape(event, resumeJob = null) {
  event.preventDefault();
  if (state.busy || state.starting) return;
  state.starting = true;
  try { await performScrape(resumeJob); }
  finally { state.starting = false; }
}

async function performScrape(resumeJob = null) {
  setFormError("");
  if (typeof resumeJob?.resumeHistory === 'boolean') byId('resume-history').checked = resumeJob.resumeHistory;

  const sourceUrl = safeFacebookSourceUrl(sourceUrlInput.value.trim());
  if (!state.localBackend) await detectLocalBackend();
  if (!state.localBackend) {
    setFormError("Không kết nối được backend local. Chạy web/local_server.py rồi mở lại trang này.");
    return;
  }
  if (!state.localCredentialsConfigured) {
    setFormError("Thêm FACEBOOK_EMAIL và FACEBOOK_PASSWORD vào web/.env, sau đó khởi động lại server.");
    return;
  }
  if (!sourceUrl) {
    setFormError("Nhập URL HTTPS của một group, page hoặc profile Facebook.");
    sourceUrlInput.focus();
    return;
  }

  if (state.loadedSourceUrl !== normalizeUrl(sourceUrl)) {
    await loadSavedDataset(sourceUrl, { showStatus: false });
  }
  try { localStorage.setItem(SOURCE_STORAGE_KEY, sourceUrl); } catch { /* saved data remains in the local database */ }

  state.cancelRequested = false;
  state.cancelDelivered = false;
  state.activeLocalJob = "";
  state.postsPage = 0;
  state.commentsPage = 0;
  state.search = "";
  byId("result-search").value = "";
  byId("results").hidden = false;
  byId("results-caption").textContent = sourceUrl + " · mới nhất trước · bắt đầu " + formatDate(new Date().toISOString());
  setTab("posts");
  setBusy(true);
  updateSummary();
  renderAll();
  state.startedAt = resumeJob?.createdAt ? resumeJob.createdAt * 1000 : Date.now();
  byId("status-elapsed").textContent = "00:00";
  state.timer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - state.startedAt) / 1000);
    byId("status-elapsed").textContent = String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
  }, 1000);

  try {
    setStatus("running", "Đang chạy Facebook Scraper", "Đang mở backend local và Chromium…");
    const result = await runLocalScrape({
      sourceUrl,
      includeReplies: byId("include-replies").checked,
      resumeHistory: byId('resume-history').checked,
    }, resumeJob);
    state.posts = result.posts || [];
    state.comments = result.comments || [];
    state.loadedSourceUrl = normalizeUrl(sourceUrl);
    if (result.status === "cancelled") state.cancelRequested = true;
    if (state.cancelRequested) throw new Error("Lần chạy đã dừng.");

    renderAll();
    setStatus("success", "Scrape hoàn tất", "Đã tải " + formatNumber(state.posts.length) + " bài đăng và " + formatNumber(state.comments.length) + " bình luận.");
    byId("results-caption").textContent = "Hoàn tất " + formatDate(new Date().toISOString()) + " · " + sourceUrl + " · mới nhất trước";
    byId("results").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    const saved = await loadSavedDataset(sourceUrl, { showStatus: false });
    renderAll();
    const stopped = state.cancelRequested;
    const savedDetail = saved
      ? ` Kho trên máy có ${formatNumber(state.posts.length)} bài và ${formatNumber(state.comments.length)} bình luận.`
      : "";
    setStatus("error", stopped ? "Lần chạy đã dừng" : "Không thể hoàn tất scrape", (error.message || "Lỗi không xác định khi chạy Playwright.") + savedDetail);
  } finally {
    window.clearInterval(state.timer);
    state.timer = null;
    state.cancelRequested = false;
    state.activeLocalJob = "";
    setBusy(false);
  }
}

function wireEvents() {
  for (const id of ["include-replies"]) {
    byId(id).addEventListener("change", updateSummary);
  }
  sourceUrlInput.addEventListener("input", updateSummary);
  sourceUrlInput.addEventListener("change", async () => {
    const sourceUrl = safeFacebookSourceUrl(sourceUrlInput.value.trim());
    if (!sourceUrl) return;
    try { localStorage.setItem(SOURCE_STORAGE_KEY, sourceUrl); } catch { /* local storage may be disabled */ }
    await loadSavedDataset(sourceUrl);
  });
  document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => setTab(button.dataset.tab)));
  byId("result-search").addEventListener("input", (event) => {
    state.search = event.target.value;
    state.postsPage = 0;
    state.commentsPage = 0;
    renderPosts();
    renderComments();
  });
  byId("export-json").addEventListener("click", () => {
    const metadata = {
      source: "Ibrahimghali/Facebook-Scraper (Playwright)",
      sourceUrl: safeFacebookSourceUrl(sourceUrlInput.value.trim()),
      order: "newest-first",
    };
    const content = JSON.stringify({ ...metadata, exportedAt: new Date().toISOString(), posts: state.posts, comments: state.comments }, null, 2);
    downloadFile("facebook-data-" + exportSlug() + ".json", content, "application/json;charset=utf-8");
  });
  byId("export-posts-csv").addEventListener("click", downloadPostsCsv);
  byId("export-comments-csv").addEventListener("click", downloadCommentsCsv);
  byId("cancel-button").addEventListener("click", async () => {
    if (!state.busy || state.cancelRequested) return;
    state.cancelRequested = true;
    if (state.activeLocalJob) {
      byId("status-detail").textContent = "Đang yêu cầu dừng Playwright sau bước hiện tại…";
      try {
        await localApiJson("/api/jobs/" + encodeURIComponent(state.activeLocalJob) + "/cancel", { method: "POST" });
        state.cancelDelivered = true;
      } catch (error) {
        byId("status-detail").textContent = error.message;
      }
    }
  });
  byId("run-button").addEventListener("click", handleScrape);
  form.addEventListener("submit", handleScrape);
}

wireEvents();
updateSummary();
detectLocalBackend().then(restoreSavedSource);
setTab("posts");
