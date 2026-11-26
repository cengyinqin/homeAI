"use strict";

const PROMPT_FIELDS = ["prompt_en", "prompt_zh", "prompt_short_en", "negative_en"];
const PROMPT_META = [
  { key: "prompt_en", label: "英文完整提示词", mono: true, lang: "en" },
  { key: "prompt_zh", label: "中文完整提示词", mono: false, lang: "zh-CN" },
  { key: "prompt_short_en", label: "英文短提示词", mono: true, lang: "en" },
  { key: "negative_en", label: "负面提示词", mono: true, lang: "en" }
];

const state = {
  bootstrap: null,
  items: [],
  filteredTotal: 0,
  selectedId: null,
  detail: null,
  bootstrapLoaded: false,
  itemsController: null,
  detailController: null,
  detailSeq: 0,
  drafts: new Map(),
  jobs: new Map(),
  filter: {
    q: "",
    category: "",
    styles: new Set(),
    generated: false,
    sort: "style"
  },
  choices: { model: null, size: null },
  saving: false,
  restoring: false,
  submitting: new Set(),
  lightboxOpen: false,
  lastFocus: null,
  credentials: null,
  credentialsBusy: false
};

const CREDENTIAL_SOURCE_LABELS = {
  env: "环境变量",
  local: "本地 Key",
  none: "未配置"
};

const els = {};
let searchTimer = null;
let filterTimer = null;

document.addEventListener("DOMContentLoaded", init);

function init() {
  cacheElements();
  bindEvents();
  renderGallerySkeletons();
  renderDetailPlaceholder();
  window.setInterval(tickJobs, 500);
  void loadCredentials();
  void boot();
}

async function boot() {
  try {
    await loadBootstrap();
  } catch (error) {
    toast(`初始化统计数据失败：${error.message}`, "error", 6000);
  }
  await loadItems({ initial: true });
}

function cacheElements() {
  els.search = document.getElementById("gallery-search");
  els.totalCount = document.getElementById("total-count");
  els.categoryChips = document.getElementById("category-chips");
  els.stylePicker = document.getElementById("style-picker");
  els.styleButton = document.getElementById("style-button");
  els.styleMenu = document.getElementById("style-menu");
  els.styleOptions = document.getElementById("style-options");
  els.styleLabel = document.getElementById("style-label");
  els.styleSelectedCount = document.getElementById("style-selected-count");
  els.styleClear = document.getElementById("style-clear");
  els.generatedToggle = document.getElementById("generated-toggle");
  els.exportButton = document.getElementById("export-button");
  els.generatedCount = document.getElementById("generated-count");
  els.editedCount = document.getElementById("edited-count");
  els.resultSummary = document.getElementById("result-summary");
  els.sortSelect = document.getElementById("sort-select");
  els.gallery = document.getElementById("gallery");
  els.browserPanel = document.querySelector(".browser-panel");
  els.detailPanel = document.getElementById("detail-panel");
  els.detailClose = document.getElementById("detail-close");
  els.toasts = document.getElementById("toasts");
  els.lightbox = document.getElementById("lightbox");
  els.lightboxScrim = document.getElementById("lightbox-scrim");
  els.lightboxDialog = document.getElementById("lightbox-dialog");
  els.lightboxClose = document.getElementById("lightbox-close");
  els.lightboxContent = document.getElementById("lightbox-content");
  els.credentialsBanner = document.getElementById("credentials-banner");
  els.credentialsBannerOpen = document.getElementById("credentials-banner-open");
  els.settingsOpen = document.getElementById("settings-open");
  els.credentialsDialog = document.getElementById("credentials-dialog");
  els.settingsClose = document.getElementById("settings-close");
  els.settingsCloseFooter = document.getElementById("settings-close-footer");
  els.settingsSourceBadge = document.getElementById("settings-source-badge");
  els.settingsKeyMasked = document.getElementById("settings-key-masked");
  els.settingsBaseUrl = document.getElementById("settings-base-url");
  els.settingsApiKey = document.getElementById("settings-api-key");
  els.settingsError = document.getElementById("settings-error");
  els.settingsSave = document.getElementById("settings-save");
  els.settingsClear = document.getElementById("settings-clear");
}

function bindEvents() {
  els.search.addEventListener("input", () => {
    state.filter.q = els.search.value.trim();
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => requestItemsReload(0, true), 220);
  });

  els.styleButton.addEventListener("click", () => {
    setStyleMenuOpen(els.styleMenu.hidden);
  });

  els.styleOptions.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== "checkbox") return;
    if (input.checked) {
      state.filter.styles.add(input.value);
    } else {
      state.filter.styles.delete(input.value);
    }
    updateStyleButton();
    requestItemsReload(140, true);
  });

  els.styleClear.addEventListener("click", () => {
    if (!state.filter.styles.size) return;
    state.filter.styles.clear();
    syncStyleCheckboxes();
    updateStyleButton();
    requestItemsReload(0, true);
  });

  els.generatedToggle.addEventListener("click", () => {
    state.filter.generated = !state.filter.generated;
    updateGeneratedToggle();
    requestItemsReload(0, true);
  });

  els.sortSelect.addEventListener("change", () => {
    state.filter.sort = els.sortSelect.value === "name" ? "name" : "style";
    requestItemsReload(0, true);
  });

  els.exportButton.addEventListener("click", () => {
    const total = state.bootstrap?.total ?? state.items.length;
    toast(`正在导出全部 ${formatCount(total)} 条记录`, "info");
  });

  els.lightboxClose.addEventListener("click", closeLightbox);
  els.lightboxScrim.addEventListener("click", closeLightbox);
  els.detailClose.addEventListener("click", () => closeDetailPanel());

  els.settingsOpen?.addEventListener("click", () => void openCredentialsSettings());
  els.credentialsBannerOpen?.addEventListener("click", () => void openCredentialsSettings());
  els.settingsClose?.addEventListener("click", closeCredentialsSettings);
  els.settingsCloseFooter?.addEventListener("click", closeCredentialsSettings);
  els.settingsSave?.addEventListener("click", () => void saveCredentials());
  els.settingsClear?.addEventListener("click", () => void clearCredentials());
  els.credentialsDialog?.addEventListener("close", resetCredentialsForm);

  document.addEventListener("pointerdown", (event) => {
    if (els.styleMenu.hidden) return;
    if (!els.stylePicker.contains(event.target)) setStyleMenuOpen(false);
  });

  document.addEventListener("keydown", handleGlobalKeydown);
}

function handleGlobalKeydown(event) {
  const target = event.target;
  const isTyping = target instanceof HTMLElement && (
    target.matches("input, textarea, select") ||
    target.isContentEditable
  );

  if (event.key === "Escape") {
    if (state.lightboxOpen) {
      event.preventDefault();
      closeLightbox();
      return;
    }
    if (document.body.classList.contains("detail-panel-open")) {
      event.preventDefault();
      closeDetailPanel();
      return;
    }
    if (!els.styleMenu.hidden) {
      setStyleMenuOpen(false);
      els.styleButton.focus();
      return;
    }
    return;
  }

  if (document.body.classList.contains("view-remodel")) return;
  if (state.lightboxOpen) return;

  if (event.key === "/" && !isTyping) {
    event.preventDefault();
    els.search.focus();
    els.search.select();
    return;
  }

  if (isTyping) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;

  if (event.key === "j" || event.key === "k") {
    if (!state.items.length) return;
    event.preventDefault();
    moveSelection(event.key === "j" ? 1 : -1);
  }
}

function moveSelection(direction) {
  const items = state.items;
  if (!items.length) return;
  let index = items.findIndex((item) => item.id === state.selectedId);
  if (index < 0) {
    index = direction > 0 ? 0 : items.length - 1;
  } else {
    index = Math.max(0, Math.min(items.length - 1, index + direction));
  }
  const next = items[index];
  if (!next) return;
  void selectItem(next.id, { reveal: true });
  scrollCardIntoView(next.id);
}

// ---------- data loading ----------

async function loadBootstrap() {
  const data = await api("/api/bootstrap");
  state.bootstrap = data;
  state.bootstrapLoaded = true;
  if (!Array.isArray(data.models) || !data.models.length) data.models = ["qwen-image-3.0-pro"];
  if (!Array.isArray(data.sizes) || !data.sizes.length) data.sizes = ["1080*1440"];
  if (!state.choices.model || !data.models.includes(state.choices.model)) {
    state.choices.model = data.models.includes("qwen-image-3.0-pro")
      ? "qwen-image-3.0-pro"
      : data.models[0];
  }
  if (!state.choices.size || !data.sizes.includes(state.choices.size)) {
    state.choices.size = data.sizes.includes("1080*1440") ? "1080*1440" : data.sizes[0];
  }
  updateHeaderCounts();
  renderCategoryChips();
  renderStyleOptions();
  updateStyleButton();
  updateResultSummary();
  if (state.detail) renderDetail({ preserveScroll: true });
}

// ---------- model credentials ----------

async function loadCredentials({ announce = false } = {}) {
  try {
    const data = await api("/api/credentials");
    applyCredentialState(data);
    return data;
  } catch (error) {
    if (announce) toast(`读取凭据状态失败：${error.message}`, "error", 6000);
    return null;
  }
}

function applyCredentialState(data) {
  if (!data || typeof data !== "object") return;
  state.credentials = data;
  const source = CREDENTIAL_SOURCE_LABELS[data.source] ? data.source : "none";
  const label = CREDENTIAL_SOURCE_LABELS[source];
  if (els.settingsSourceBadge) {
    els.settingsSourceBadge.textContent = label;
    els.settingsSourceBadge.dataset.source = source;
  }
  if (els.settingsKeyMasked) {
    els.settingsKeyMasked.textContent = data.key_masked || "未配置";
  }
  if (els.settingsBaseUrl) {
    els.settingsBaseUrl.value = data.base_url || "";
  }
  if (els.settingsClear) {
    els.settingsClear.disabled = !data.has_local;
  }
  if (els.credentialsBanner) {
    els.credentialsBanner.hidden = source !== "none";
  }
}

function showCredentialError(message) {
  if (!els.settingsError) return;
  els.settingsError.textContent = message;
  els.settingsError.hidden = false;
}

function resetCredentialsForm() {
  if (els.settingsApiKey) els.settingsApiKey.value = "";
  if (els.settingsError) {
    els.settingsError.textContent = "";
    els.settingsError.hidden = true;
  }
}

async function openCredentialsSettings() {
  if (!els.credentialsDialog) return;
  resetCredentialsForm();
  if (!els.credentialsDialog.open) {
    if (typeof els.credentialsDialog.showModal === "function") {
      els.credentialsDialog.showModal();
    } else {
      els.credentialsDialog.setAttribute("open", "");
    }
  }
  await loadCredentials({ announce: true });
  els.settingsApiKey?.focus();
}

function closeCredentialsSettings() {
  if (!els.credentialsDialog) return;
  if (typeof els.credentialsDialog.close === "function") {
    if (els.credentialsDialog.open) els.credentialsDialog.close();
  } else {
    els.credentialsDialog.removeAttribute("open");
    resetCredentialsForm();
  }
}

function setCredentialsBusy(busy) {
  state.credentialsBusy = busy;
  if (els.settingsSave) els.settingsSave.disabled = busy;
  if (els.settingsClear) {
    els.settingsClear.disabled = busy || !(state.credentials?.has_local);
  }
}

async function saveCredentials() {
  if (state.credentialsBusy || !els.settingsApiKey) return;
  const apiKey = els.settingsApiKey.value.trim();
  if (!apiKey) {
    showCredentialError("请先填写 API Key，空值不会发送或保存");
    els.settingsApiKey.focus();
    return;
  }
  if (apiKey.length <= 20) {
    showCredentialError("API Key 长度必须大于 20 个字符");
    els.settingsApiKey.focus();
    return;
  }
  const baseUrl = els.settingsBaseUrl?.value.trim() || "";
  setCredentialsBusy(true);
  try {
    const data = await api("/api/credentials", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        ...(baseUrl ? { base_url: baseUrl } : {})
      })
    });
    applyCredentialState(data);
    els.settingsApiKey.value = "";
    if (els.settingsError) els.settingsError.hidden = true;
    toast("凭据已保存（本地文件）", "success", 5200);
  } catch (error) {
    showCredentialError(`保存失败：${error.message}`);
  } finally {
    setCredentialsBusy(false);
  }
}

async function clearCredentials() {
  if (state.credentialsBusy) return;
  setCredentialsBusy(true);
  try {
    const data = await api("/api/credentials", { method: "DELETE" });
    applyCredentialState(data);
    els.settingsApiKey && (els.settingsApiKey.value = "");
    if (els.settingsError) els.settingsError.hidden = true;
    if (data.source === "env") {
      toast("本地凭据已清除，已回落到环境变量", "success", 5200);
    } else {
      toast("本地凭据已清除", "success", 5200);
    }
  } catch (error) {
    showCredentialError(`清除失败：${error.message}`);
  } finally {
    setCredentialsBusy(false);
  }
}

async function loadItems({ initial = false, resetScroll = false, silent = false } = {}) {
  if (state.itemsController) state.itemsController.abort();
  const controller = new AbortController();
  state.itemsController = controller;

  if (!silent) {
    if (!state.items.length) {
      renderGallerySkeletons();
    } else {
      els.browserPanel.classList.add("is-refreshing");
    }
  }

  const params = new URLSearchParams();
  if (state.filter.styles.size) params.set("style", [...state.filter.styles].join(","));
  if (state.filter.category) params.set("category", state.filter.category);
  if (state.filter.q) params.set("q", state.filter.q);
  if (state.filter.generated) params.set("generated", "1");
  if (state.filter.sort) params.set("sort", state.filter.sort);

  try {
    const data = await api(`/api/items?${params.toString()}`, { signal: controller.signal });
    if (controller !== state.itemsController) return;
    state.items = Array.isArray(data.items) ? data.items : [];
    state.filteredTotal = Number(data.total) || 0;
    renderGallery();
    updateResultSummary();
    if (resetScroll) els.browserPanel.scrollTop = 0;

    if (!state.bootstrap && !hasActiveFilters()) {
      els.totalCount.textContent = formatCount(state.items.length);
    }

    const selectionExists = state.items.some((item) => item.id === state.selectedId);
    if (!selectionExists) {
      if (state.items.length) {
        void selectItem(state.items[0].id);
      } else {
        state.selectedId = null;
        state.detail = null;
        renderDetailPlaceholder();
      }
    } else {
      updateSelectionRing();
    }
  } catch (error) {
    if (error.name === "AbortError") return;
    toast(`图库加载失败：${error.message}`, "error", 6000);
    if (!state.items.length) renderGalleryError();
  } finally {
    if (controller === state.itemsController) {
      els.browserPanel.classList.remove("is-refreshing");
    }
  }
}

function requestItemsReload(delay = 0, resetScroll = true) {
  window.clearTimeout(filterTimer);
  if (delay > 0) {
    filterTimer = window.setTimeout(() => void loadItems({ resetScroll }), delay);
  } else {
    void loadItems({ resetScroll });
  }
}

async function fetchDetail(id, { preserveScroll = false } = {}) {
  if (state.detailController) state.detailController.abort();
  const controller = new AbortController();
  state.detailController = controller;
  const sequence = ++state.detailSeq;

  if (!state.detail) renderDetailSkeleton();
  els.detailPanel.classList.add("is-busy");
  els.detailPanel.setAttribute("aria-busy", "true");

  try {
    const record = await api(`/api/items/${encodeURIComponent(id)}`, { signal: controller.signal });
    if (sequence !== state.detailSeq) return;
    const previousId = state.detail?.id;
    state.detail = record;
    state.selectedId = record.id;
    updateSelectionRing();
    renderDetail({ preserveScroll: preserveScroll || previousId === record.id });
  } catch (error) {
    if (error.name === "AbortError") return;
    toast(`条目详情加载失败：${error.message}`, "error", 6000);
    if (state.detail) {
      state.selectedId = state.detail.id;
      updateSelectionRing();
    } else {
      state.selectedId = null;
      renderDetailPlaceholder();
    }
  } finally {
    if (sequence === state.detailSeq) {
      els.detailPanel.classList.remove("is-busy");
      els.detailPanel.removeAttribute("aria-busy");
    }
  }
}

async function selectItem(id, { reveal = false } = {}) {
  if (!id) return;
  const alreadyLoaded = state.detail?.id === id;
  state.selectedId = id;
  updateSelectionRing();
  if (reveal) openDetailPanel();
  if (alreadyLoaded) return;
  scrollCardIntoView(id);
  await fetchDetail(id);
}

function isMobileDetail() {
  return window.matchMedia("(max-width: 560px)").matches;
}

function openDetailPanel() {
  if (!isMobileDetail()) return;
  document.body.classList.add("detail-panel-open");
}

function closeDetailPanel() {
  if (!document.body.classList.contains("detail-panel-open")) return;
  document.body.classList.remove("detail-panel-open");
  const card = [...els.gallery.querySelectorAll(".card[data-id]")]
    .find((node) => node.dataset.id === state.selectedId);
  card?.focus({ preventScroll: true });
}

async function refreshStats() {
  try {
    const data = await api("/api/bootstrap");
    state.bootstrap = data;
    updateHeaderCounts();
    updateResultSummary();
  } catch (error) {
    toast(`统计刷新失败：${error.message}`, "error");
  }
}

async function refreshAfterGeneration(itemId) {
  await Promise.allSettled([
    loadItems({ silent: true, resetScroll: false }),
    refreshStats()
  ]);
  if (state.selectedId === itemId && state.detail?.id === itemId) {
    await fetchDetail(itemId, { preserveScroll: true });
  }
}

// ---------- header and filters ----------

function renderCategoryChips() {
  els.categoryChips.replaceChildren();
  if (!state.bootstrap) return;
  const total = state.bootstrap.total || 0;
  els.categoryChips.append(categoryChip("全部", "", total));
  for (const category of state.bootstrap.categories || []) {
    if (!category?.name_zh) continue;
    els.categoryChips.append(categoryChip(category.name_zh, category.name_zh, category.count));
  }
}

function categoryChip(label, value, count) {
  const active = state.filter.category === value;
  const chip = make("button", `chip${active ? " is-active" : ""}`);
  chip.type = "button";
  chip.setAttribute("aria-pressed", String(active));
  chip.append(document.createTextNode(label));
  const small = make("small", "", formatCount(count));
  chip.append(small);
  chip.addEventListener("click", () => {
    state.filter.category = state.filter.category === value ? "" : value;
    renderCategoryChips();
    requestItemsReload(0, true);
  });
  return chip;
}

function renderStyleOptions() {
  els.styleOptions.replaceChildren();
  if (!state.bootstrap) {
    els.styleOptions.append(make("p", "samples-empty", "筛选数据暂不可用"));
    return;
  }
  for (const style of state.bootstrap.styles || []) {
    const label = make("label", "style-option");
    const input = document.createElement("input");
    input.type = "checkbox";
    input.value = style.name_zh;
    input.checked = state.filter.styles.has(style.name_zh);
    input.setAttribute("aria-label", `${style.name_zh}，${style.count} 张`);
    const check = make("span", "checkmark");
    const name = make("span", "style-option-name", style.name_zh);
    const count = make("span", "style-option-count", formatCount(style.count));
    label.append(input, check, name, count);
    els.styleOptions.append(label);
  }
}

function syncStyleCheckboxes() {
  for (const input of els.styleOptions.querySelectorAll('input[type="checkbox"]')) {
    input.checked = state.filter.styles.has(input.value);
  }
}

function updateStyleButton() {
  const names = [...state.filter.styles];
  if (!names.length) {
    els.styleLabel.textContent = "全部风格";
    els.styleSelectedCount.hidden = true;
    els.styleButton.removeAttribute("title");
    return;
  }
  if (names.length === 1) {
    els.styleLabel.textContent = names[0];
  } else {
    els.styleLabel.textContent = `已选 ${names.length} 种风格`;
  }
  els.styleSelectedCount.hidden = false;
  els.styleSelectedCount.textContent = String(names.length);
  els.styleButton.title = names.join("、");
}

function setStyleMenuOpen(open) {
  els.styleMenu.hidden = !open;
  els.styleButton.setAttribute("aria-expanded", String(open));
}

function updateGeneratedToggle() {
  els.generatedToggle.setAttribute("aria-pressed", String(state.filter.generated));
}

function updateHeaderCounts() {
  if (!state.bootstrap) return;
  els.totalCount.textContent = formatCount(state.bootstrap.total);
  els.generatedCount.textContent = formatCount(state.bootstrap.generated_count);
  els.editedCount.textContent = formatCount(state.bootstrap.edited_count);
}

function updateResultSummary() {
  const total = state.bootstrap?.total ?? state.items.length;
  if (!state.bootstrapLoaded && !state.items.length) {
    els.resultSummary.textContent = "正在加载图库…";
    return;
  }
  if (hasActiveFilters()) {
    els.resultSummary.textContent = `筛选出 ${formatCount(state.filteredTotal)} 张，共 ${formatCount(total)} 张`;
  } else {
    els.resultSummary.textContent = `共 ${formatCount(total)} 张图像`;
  }
}

function hasActiveFilters() {
  return Boolean(
    state.filter.q ||
    state.filter.category ||
    state.filter.styles.size ||
    state.filter.generated
  );
}

function clearAllFilters() {
  state.filter.q = "";
  state.filter.category = "";
  state.filter.styles.clear();
  state.filter.generated = false;
  els.search.value = "";
  els.sortSelect.value = state.filter.sort;
  renderCategoryChips();
  syncStyleCheckboxes();
  updateStyleButton();
  updateGeneratedToggle();
  requestItemsReload(0, true);
}

// ---------- gallery ----------

function renderGallerySkeletons(count = 12) {
  els.gallery.setAttribute("aria-busy", "true");
  els.gallery.replaceChildren();
  const fragment = document.createDocumentFragment();
  for (let index = 0; index < count; index += 1) {
    const card = make("div", "card skeleton-card");
    card.setAttribute("aria-hidden", "true");
    const media = make("span", "card-media");
    const copy = make("span", "card-copy");
    copy.append(make("span", "skeleton-line"), make("span", "skeleton-line short"));
    card.append(media, copy);
    fragment.append(card);
  }
  els.gallery.append(fragment);
}

function renderGallery() {
  els.gallery.setAttribute("aria-busy", "false");
  els.gallery.replaceChildren();
  if (!state.items.length) {
    renderEmptyState();
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const item of state.items) {
    fragment.append(buildCard(item));
  }
  els.gallery.append(fragment);
}

function buildCard(item) {
  const selected = item.id === state.selectedId;
  const card = make("button", "card");
  card.type = "button";
  card.dataset.id = item.id;
  card.setAttribute("aria-current", String(selected));
  card.setAttribute("aria-label", `${item.name}，${item.style_primary_zh || "未标风格"}${item.generated ? "，已出图" : ""}`);

  const media = make("span", "card-media");
  const image = document.createElement("img");
  image.src = item.thumb_url;
  image.alt = `${item.name} 空间缩略图`;
  image.loading = "lazy";
  image.decoding = "async";
  image.addEventListener("load", () => image.classList.add("is-loaded"), { once: true });
  image.addEventListener("error", () => image.classList.add("is-failed"), { once: true });
  media.append(image, make("span", "card-skeleton"));

  if (item.generated) {
    media.append(make("span", "generated-pill", "已出图"));
  }

  const openHint = make("span", "card-open-hint", "查看");
  openHint.setAttribute("aria-hidden", "true");
  media.append(openHint);

  const copy = make("span", "card-copy");
  copy.append(make("span", "card-name", item.name || item.id));
  const meta = make("span", "card-meta");
  meta.append(make("span", "style-badge", item.style_primary_zh || "未标风格"));
  const palette = make("span", "palette-dots");
  for (const color of (item.palette || []).slice(0, 4)) {
    if (!color || !isHexColor(color.hex)) continue;
    const dot = make("span", "palette-dot");
    dot.style.backgroundColor = color.hex;
    dot.title = [color.name_zh, color.hex].filter(Boolean).join(" ");
    palette.append(dot);
  }
  meta.append(palette);
  copy.append(meta);
  card.append(media, copy);
  card.addEventListener("click", () => void selectItem(item.id, { reveal: true }));
  return card;
}

function updateSelectionRing() {
  for (const card of els.gallery.querySelectorAll(".card[data-id]")) {
    card.setAttribute("aria-current", String(card.dataset.id === state.selectedId));
  }
}

function scrollCardIntoView(id) {
  const card = [...els.gallery.querySelectorAll(".card[data-id]")].find((node) => node.dataset.id === id);
  if (!card) return;
  card.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function renderEmptyState() {
  const wrap = make("div", "empty-state");
  const glyph = make("div", "empty-glyph");
  glyph.setAttribute("aria-hidden", "true");
  glyph.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  const title = make("h2", "", "没有符合条件的图片");
  const copy = make("p", "", "试试清空搜索词，或者换一个分类与风格。");
  const button = make("button", "filter-button", "清除筛选");
  button.type = "button";
  button.addEventListener("click", clearAllFilters);
  wrap.append(glyph, title, copy, button);
  els.gallery.append(wrap);
}

function renderGalleryError() {
  els.gallery.setAttribute("aria-busy", "false");
  els.gallery.replaceChildren();
  const wrap = make("div", "error-state");
  const title = make("h2", "", "图库没有加载成功");
  const copy = make("p", "", "服务可能暂时不可用。当前页面不会丢失，可以重新尝试。");
  const button = make("button", "filter-button", "重新加载");
  button.type = "button";
  button.addEventListener("click", () => {
    void (async () => {
      try {
        await loadBootstrap();
      } catch (error) {
        toast(`初始化统计数据失败：${error.message}`, "error", 6000);
      }
      await loadItems({ initial: true });
    })();
  });
  wrap.append(title, copy, button);
  els.gallery.append(wrap);
}

// ---------- detail ----------

function renderDetailPlaceholder() {
  els.detailPanel.replaceChildren();
  const wrap = make("div", "detail-placeholder");
  const mark = make("span", "placeholder-mark");
  mark.setAttribute("aria-hidden", "true");
  mark.append(document.createElement("i"), document.createElement("i"), document.createElement("i"));
  wrap.append(
    mark,
    make("h2", "", "从图库中选择一张图片"),
    make("p", "", "查看空间拆解、色板与提示词，并在右侧直接生成新图。")
  );
  const keys = make("p", "placeholder-keys");
  const k1 = make("kbd", "", "j");
  const k2 = make("kbd", "", "k");
  keys.append(k1, k2, document.createTextNode(" 可前后切换"));
  wrap.append(keys);
  els.detailPanel.append(wrap);
}

function renderDetailSkeleton() {
  els.detailPanel.replaceChildren();
  const wrap = make("div", "detail-skeleton");
  wrap.append(
    make("div", "hero-skeleton"),
    make("div", "line title"),
    make("div", "line short"),
    make("div", "line"),
    make("div", "line short"),
    make("div", "line block")
  );
  els.detailPanel.append(wrap);
}

function renderDetail({ preserveScroll = false } = {}) {
  const detail = state.detail;
  if (!detail) {
    renderDetailPlaceholder();
    return;
  }
  const scroll = preserveScroll ? els.detailPanel.scrollTop : 0;
  const fragment = document.createDocumentFragment();
  fragment.append(
    buildHero(detail),
    buildDetailHead(detail),
    buildSamplesSection(detail),
    buildMetaSection(detail),
    buildPaletteSection(detail),
    buildBreakdownSection(detail),
    buildPromptSection(detail),
    buildGenerationSection(detail)
  );
  els.detailPanel.replaceChildren(fragment);
  updatePromptCounts();
  updateDirtyUI();
  updateGenerationUI();
  els.detailPanel.scrollTop = scroll;
}

function buildHero(detail) {
  const button = make("button", "detail-hero");
  button.type = "button";
  button.setAttribute("aria-label", "查看大图");
  const image = document.createElement("img");
  image.src = mediaUrl(detail.local_path);
  image.alt = detail.alt || `${detail.name} 空间原图`;
  image.loading = "eager";
  image.decoding = "async";
  button.append(image, make("span", "hero-hint", "查看大图"));
  button.addEventListener("click", () => openSingleLightbox(detail));
  return button;
}

function buildDetailHead(detail) {
  const head = make("div", "detail-head");
  const row = make("div", "detail-title-row");
  const title = make("h2", "detail-title", detail.name || detail.id);
  const badges = make("div", "detail-title-badges");
  badges.append(make("span", "tag", detail.style_primary_zh || "未标风格"));
  if ((detail.samples || []).length) {
    badges.append(make("span", "tag is-generated", "已出图"));
  }
  if (detail.overridden) {
    badges.append(make("span", "tag", "提示词已改"));
  }
  row.append(title, badges);
  head.append(row, make("div", "detail-id", detail.id));
  return head;
}

function buildMetaSection(detail) {
  const section = make("section", "detail-section");
  section.append(sectionTitle("条目信息"));
  const grid = make("dl", "meta-grid");

  grid.append(metaItem("分类", detail.category_zh || detail.category || "—"));

  const styleItem = make("div", "meta-item");
  styleItem.append(make("dt", "", "主风格"));
  const styleValue = make("dd", "", detail.style_primary_zh || "—");
  if (detail.style_secondary_zh) styleValue.title = `次要风格：${detail.style_secondary_zh}`;
  styleItem.append(styleValue);
  grid.append(styleItem);

  const confidence = Number(detail.style_confidence);
  const confidenceItem = make("div", "meta-item");
  confidenceItem.append(make("dt", "", "风格置信度"));
  const confidenceValue = make("dd", "", "");
  const confidenceWrap = make("span", "confidence");
  const pct = Number.isFinite(confidence) ? Math.max(0, Math.min(100, Math.round(confidence * 100))) : 0;
  const track = make("span", "confidence-track");
  const fill = make("span");
  fill.style.width = `${pct}%`;
  track.append(fill);
  confidenceWrap.append(make("span", "", `${pct}%`), track);
  confidenceValue.append(confidenceWrap);
  confidenceItem.append(confidenceValue);
  grid.append(confidenceItem);

  const width = detail.actual_width || detail.width;
  const height = detail.actual_height || detail.height;
  grid.append(metaItem("图像尺寸", width && height ? `${width} × ${height}` : "—", true));

  grid.append(metaItem("创建时间", detail.created_at || "—", true, true));

  const isAiImage = /(^|\/)images\/ai\//.test(String(detail.local_path || ""));
  if (isAiImage) {
    grid.append(metaItem("图像来源", "AI 生成 · 按拆解提示词复现", true));
  } else {
    const sourceItem = make("div", "meta-item wide");
    sourceItem.append(make("dt", "", "来源页面"));
    const sourceValue = make("dd");
    const sourceUrl = safeHttpUrl(detail.source_page);
    if (sourceUrl) {
      const link = document.createElement("a");
      link.href = sourceUrl;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = sourceUrl;
      sourceValue.append(link);
    } else {
      sourceValue.textContent = detail.source_page || "—";
    }
    sourceItem.append(sourceValue);
    grid.append(sourceItem);
  }

  section.append(grid);
  return section;
}

function metaItem(label, value, wide = false, mono = false) {
  const item = make("div", `meta-item${wide ? " wide" : ""}`);
  item.append(make("dt", "", label));
  const dd = make("dd", mono ? "mono" : "", value === undefined || value === null ? "—" : String(value));
  item.append(dd);
  return item;
}

function buildPaletteSection(detail) {
  const section = make("section", "detail-section");
  const palette = Array.isArray(detail.palette) ? detail.palette : [];
  section.append(sectionTitle("空间色板", palette.length ? `${palette.length} 色` : ""));
  if (!palette.length) {
    section.append(make("p", "breakdown-paragraph", "暂无取色数据。"));
    return section;
  }
  const list = make("div", "palette-list");
  for (const color of palette) {
    if (!color || typeof color !== "object") continue;
    const share = Number(color.share);
    const pct = Number.isFinite(share) ? Math.max(0, Math.min(100, share * 100)) : 0;
    const row = make("div", "palette-row");
    const swatch = make("span", "palette-swatch");
    if (isHexColor(color.hex)) swatch.style.backgroundColor = color.hex;

    const copy = make("div", "palette-copy");
    const name = make("div", "palette-name");
    name.append(make("span", "", color.name_zh || color.name_en || "未命名"));
    if (color.role) name.append(make("span", "palette-role", color.role));
    const bar = make("div", "palette-bar");
    const barFill = make("span");
    barFill.style.width = `${pct}%`;
    if (isHexColor(color.hex)) barFill.style.backgroundColor = color.hex;
    bar.append(barFill);
    copy.append(name, bar);

    const values = make("div", "palette-values");
    values.append(
      make("span", "palette-hex", color.hex || "—"),
      make("span", "palette-share", `${pct.toFixed(1)}%`)
    );
    row.append(swatch, copy, values);
    list.append(row);
  }
  section.append(list);
  return section;
}

function buildBreakdownSection(detail) {
  const section = make("section", "detail-section");
  section.append(sectionTitle("空间拆解"));
  const breakdown = make("div", "breakdown");
  const style = detail.style_detail && typeof detail.style_detail === "object" ? detail.style_detail : {};

  breakdown.append(
    breakdownBlock("家具", renderFurniture(detail.furniture ?? style.furniture)),
    breakdownBlock("灯具", renderLighting(detail.lighting ?? style.lighting)),
    breakdownBlock("织物", renderTextiles(detail.textiles ?? style.textiles)),
    breakdownBlock("材质", renderMaterials(detail.materials ?? style.materials)),
    breakdownBlock("陈设", renderDecor(detail.decor ?? style.decor)),
    breakdownBlock("空间结构", renderSpace(detail.space ?? style.space)),
    breakdownBlock("构图光线", renderCamera(detail.camera ?? style.camera))
  );
  section.append(breakdown);
  return section;
}

function breakdownBlock(label, content) {
  const block = make("div", "breakdown-block");
  block.append(make("div", "breakdown-label", label));
  if (content) {
    block.append(content);
  } else {
    block.append(make("p", "breakdown-paragraph", "暂无拆解"));
  }
  return block;
}

function renderFurniture(items) {
  if (!Array.isArray(items) || !items.length) return null;
  const wrap = make("div", "spec-list");
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const name = item.item_zh || item.item_en || "家具";
    const details = [item.material_zh, item.color_zh].filter(Boolean).join("，");
    wrap.append(specRow(name, details, item.note_zh));
  }
  return wrap.childElementCount ? wrap : null;
}

function renderLighting(lighting) {
  if (!lighting || typeof lighting !== "object") return null;
  const wrap = make("div", "spec-list");
  for (const fixture of Array.isArray(lighting.fixtures) ? lighting.fixtures : []) {
    if (!fixture || typeof fixture !== "object") continue;
    const name = fixture.type_zh || fixture.type_en || "灯具";
    const details = [fixture.material_zh, fixture.position_zh].filter(Boolean).join("，");
    wrap.append(specRow(name, details, ""));
  }
  if (lighting.mood_zh) {
    wrap.append(make("p", "breakdown-paragraph", lighting.mood_zh));
  }
  const tokens = [lighting.time_of_day, lighting.direction_zh, lighting.color_temp].filter(Boolean);
  if (tokens.length) {
    const row = make("div", "camera-row");
    for (const token of tokens) row.append(make("span", "camera-token", token));
    wrap.append(row);
  }
  return wrap.childElementCount ? wrap : null;
}

function renderTextiles(items) {
  if (!Array.isArray(items) || !items.length) return null;
  const wrap = make("div", "spec-list");
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const name = item.item_zh || item.item_en || "织物";
    const details = [item.fabric_zh, item.color_zh, item.pattern_zh].filter(Boolean).join("，");
    wrap.append(specRow(name, details, ""));
  }
  return wrap.childElementCount ? wrap : null;
}

function renderMaterials(items) {
  if (!Array.isArray(items) || !items.length) return null;
  const wrap = make("div", "spec-list");
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    wrap.append(specRow(item.name_zh || item.name_en || "材质", item.where_zh || "", ""));
  }
  return wrap.childElementCount ? wrap : null;
}

function renderDecor(items) {
  if (!Array.isArray(items) || !items.length) return null;
  const wrap = make("div", "item-chips");
  for (const item of items) {
    if (!item || typeof item !== "object") continue;
    const label = item.item_zh || item.item_en;
    if (label) wrap.append(make("span", "item-chip", label));
  }
  return wrap.childElementCount ? wrap : null;
}

function renderSpace(space) {
  if (!space || typeof space !== "object") return null;
  const wrap = make("div", "spec-list");
  if (space.room_zh) wrap.append(make("div", "spec-head", space.room_zh));
  if (space.structure_zh) wrap.append(make("p", "breakdown-paragraph", space.structure_zh));
  return wrap.childElementCount ? wrap : null;
}

function renderCamera(camera) {
  if (!camera || typeof camera !== "object") return null;
  const wrap = make("div", "spec-list");
  const tokens = [camera.view_zh, camera.shot_zh].filter(Boolean);
  if (tokens.length) {
    const row = make("div", "camera-row");
    for (const token of tokens) row.append(make("span", "camera-token", token));
    wrap.append(row);
  }
  if (camera.light_desc_zh) wrap.append(make("p", "breakdown-paragraph", camera.light_desc_zh));
  if (camera.quality_zh) wrap.append(make("p", "breakdown-paragraph", camera.quality_zh));
  return wrap.childElementCount ? wrap : null;
}

function specRow(name, detail, note) {
  const row = make("div", "spec-row");
  const head = make("div", "spec-head");
  head.append(make("span", "spec-name", name));
  if (detail) head.append(make("span", "spec-detail", detail));
  row.append(head);
  if (note) row.append(make("p", "spec-note", note));
  return row;
}

// ---------- prompts ----------

function buildPromptSection(detail) {
  const section = make("section", "detail-section");
  section.append(sectionTitle("提示词"));

  const toolbar = make("div", "prompt-toolbar");
  toolbar.append(make("span", "dirty-hint", "来自原始提示词库"));
  toolbar.append(make("span", "", "保存在本地覆盖层"));
  section.append(toolbar);

  const draft = state.drafts.get(detail.id);
  for (const meta of PROMPT_META) {
    const field = make("div", "prompt-field");
    const labelRow = make("div", "prompt-label-row");
    const label = make("label", "prompt-label");
    label.htmlFor = `prompt-${detail.id}-${meta.key}`;
    label.append(make("span", "", meta.label), make("code", "", meta.key));
    const count = make("span", "prompt-count", "");
    count.dataset.countFor = meta.key;
    labelRow.append(label, count);

    const textarea = document.createElement("textarea");
    textarea.className = `prompt-textarea${meta.mono ? " mono" : ""}`;
    textarea.id = `prompt-${detail.id}-${meta.key}`;
    textarea.dataset.field = meta.key;
    textarea.lang = meta.lang;
    textarea.spellcheck = meta.key === "prompt_zh";
    textarea.value = draft?.values?.[meta.key] ?? detail[meta.key] ?? "";
    textarea.setAttribute("aria-label", meta.label);
    textarea.addEventListener("input", () => {
      updatePromptCounts();
      syncDraftFromEditors();
    });

    field.append(labelRow, textarea);
    section.append(field);
  }

  const actions = make("div", "prompt-actions");
  const save = make("button", "button primary", "保存修改");
  save.type = "button";
  save.dataset.saveButton = "true";
  save.addEventListener("click", () => void savePrompt());
  const restore = make("button", "button secondary", "恢复原文");
  restore.type = "button";
  restore.dataset.restoreButton = "true";
  restore.addEventListener("click", () => void restorePrompt());
  actions.append(save, restore);
  section.append(actions);
  return section;
}

function readPromptValues() {
  const values = {};
  for (const textarea of els.detailPanel.querySelectorAll("textarea[data-field]")) {
    values[textarea.dataset.field] = textarea.value;
  }
  return values;
}

function promptDirty() {
  const detail = state.detail;
  if (!detail) return false;
  const values = readPromptValues();
  return PROMPT_FIELDS.some((field) => (values[field] ?? "") !== (detail[field] ?? ""));
}

function syncDraftFromEditors() {
  const detail = state.detail;
  if (!detail) return;
  const values = readPromptValues();
  const dirty = PROMPT_FIELDS.some((field) => (values[field] ?? "") !== (detail[field] ?? ""));
  if (dirty) {
    state.drafts.set(detail.id, { values });
  } else {
    state.drafts.delete(detail.id);
  }
  updateDirtyUI();
}

function updateDirtyUI() {
  const detail = state.detail;
  const hint = els.detailPanel.querySelector(".dirty-hint");
  const save = els.detailPanel.querySelector("[data-save-button]");
  const restore = els.detailPanel.querySelector("[data-restore-button]");
  if (!detail || !hint || !save || !restore) return;

  const dirty = promptDirty();
  hint.classList.remove("is-dirty", "is-saved");
  if (dirty) {
    hint.textContent = "有未保存的修改";
    hint.classList.add("is-dirty");
  } else if (detail.overridden) {
    hint.textContent = "已应用本地修改";
    hint.classList.add("is-saved");
  } else {
    hint.textContent = "来自原始提示词库";
  }

  save.disabled = !dirty || state.saving;
  save.textContent = state.saving ? "保存中…" : "保存修改";
  restore.disabled = (!detail.overridden && !dirty) || state.restoring || state.saving;
  restore.textContent = state.restoring ? "恢复中…" : "恢复原文";
}

function updatePromptCounts() {
  if (!state.detail) return;
  for (const textarea of els.detailPanel.querySelectorAll("textarea[data-field]")) {
    const field = textarea.dataset.field;
    const value = textarea.value || "";
    const count = els.detailPanel.querySelector(`[data-count-for="${field}"]`);
    if (!count) continue;
    if (field === "prompt_en") {
      const words = countWords(value);
      count.textContent = `${formatCount(words)} 词，${formatCount(value.length)} 字符`;
    } else {
      count.textContent = `${formatCount(value.length)} 字符`;
    }
  }
}

async function savePrompt() {
  const detail = state.detail;
  if (!detail || state.saving || state.restoring) return;
  const values = readPromptValues();
  const missing = PROMPT_FIELDS.find((field) => !(field in values));
  if (missing) {
    toast("提示词编辑器尚未准备好", "error");
    return;
  }
  state.saving = true;
  updateDirtyUI();
  try {
    const record = await api(`/api/items/${encodeURIComponent(detail.id)}/prompt`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values)
    });
    state.drafts.delete(detail.id);
    state.saving = false;
    if (state.detail?.id === record.id) {
      state.detail = record;
      renderDetail({ preserveScroll: true });
    }
    setListItemEdited(record.id, true);
    toast("提示词修改已保存", "success");
    void refreshStats();
  } catch (error) {
    state.saving = false;
    updateDirtyUI();
    toast(`保存失败：${error.message}`, "error", 6000);
  }
}

async function restorePrompt() {
  const detail = state.detail;
  if (!detail || state.saving || state.restoring) return;
  state.restoring = true;
  updateDirtyUI();
  try {
    const record = await api(`/api/items/${encodeURIComponent(detail.id)}/restore`, {
      method: "POST"
    });
    state.drafts.delete(detail.id);
    state.restoring = false;
    if (state.detail?.id === record.id) {
      state.detail = record;
      renderDetail({ preserveScroll: true });
    }
    setListItemEdited(record.id, false);
    toast("已恢复原始提示词", "success");
    void refreshStats();
  } catch (error) {
    state.restoring = false;
    updateDirtyUI();
    toast(`恢复失败：${error.message}`, "error", 6000);
  }
}

function setListItemEdited(id, edited) {
  const item = state.items.find((entry) => entry.id === id);
  if (item) item.edited = edited;
}

// ---------- generation ----------

function buildGenerationSection(detail) {
  const section = make("section", "detail-section");
  section.append(sectionTitle("生成新图"));
  const box = make("div", "generation-box");

  const controls = make("div", "generation-controls");
  const models = state.bootstrap?.models?.length ? state.bootstrap.models : ["qwen-image-3.0-pro"];
  const sizes = state.bootstrap?.sizes?.length ? state.bootstrap.sizes : ["1080*1440"];
  if (!models.includes(state.choices.model)) state.choices.model = models[0];
  if (!sizes.includes(state.choices.size)) state.choices.size = sizes[0];

  const modelField = make("label", "field-select");
  modelField.append(make("span", "", "模型"));
  const modelSelect = document.createElement("select");
  modelSelect.dataset.modelSelect = "true";
  for (const model of models) {
    const option = document.createElement("option");
    option.value = model;
    option.textContent = model;
    option.selected = model === state.choices.model;
    modelSelect.append(option);
  }
  modelSelect.addEventListener("change", () => {
    state.choices.model = modelSelect.value;
  });
  modelField.append(modelSelect);

  const sizeField = make("label", "field-select");
  sizeField.append(make("span", "", "尺寸"));
  const sizeSelect = document.createElement("select");
  sizeSelect.dataset.sizeSelect = "true";
  for (const size of sizes) {
    const option = document.createElement("option");
    option.value = size;
    option.textContent = size;
    option.selected = size === state.choices.size;
    sizeSelect.append(option);
  }
  sizeSelect.addEventListener("change", () => {
    state.choices.size = sizeSelect.value;
  });
  sizeField.append(sizeSelect);
  controls.append(modelField, sizeField);

  const actions = make("div", "generation-actions");
  const copy = make("button", "button secondary copy-button", "复制英文提示词");
  copy.type = "button";
  copy.addEventListener("click", () => void copyEnglishPrompt());
  const generate = make("button", "button primary generate-button", "生成图片");
  generate.type = "button";
  generate.dataset.generateButton = "true";
  generate.addEventListener("click", () => void generateImage());
  actions.append(copy, generate);

  const status = make("p", "generation-status", "生成会使用编辑器中的英文提示词，约需 1 分钟。");
  status.dataset.jobStatus = "true";
  status.setAttribute("aria-live", "polite");

  box.append(controls, actions, status);
  section.append(box);
  return section;
}

function buildSamplesSection(detail) {
  const section = make("section", "detail-section");
  // Sort a copy: the API keeps samples oldest-first and callers must not be
  // mutated. Invalid/missing created_at values fall back to API list order.
  const samples = (Array.isArray(detail.samples) ? detail.samples : [])
    .map((sample, index) => ({
      sample,
      index,
      time: Date.parse(sample?.created_at || "")
    }))
    .filter((entry) => entry.sample?.url)
    .sort((a, b) => {
      const aTime = Number.isFinite(a.time) ? a.time : null;
      const bTime = Number.isFinite(b.time) ? b.time : null;
      if (aTime !== null && bTime !== null && aTime !== bTime) return bTime - aTime;
      if (aTime !== null && bTime === null) return -1;
      if (aTime === null && bTime !== null) return 1;
      return b.index - a.index;
    })
    .map((entry) => entry.sample);

  section.append(sectionTitle(`生成结果 · ${samples.length} 张`));
  if (!samples.length) {
    section.append(make("div", "samples-empty", "还没有生成图。调整提示词后点击「生成图片」，结果会保存在这里。"));
    return section;
  }
  const grid = make("div", "sample-grid");
  if (samples.length > 4) grid.classList.add("is-capped");
  samples.forEach((sample, index) => {
    const button = make("button", "sample-card");
    button.type = "button";
    button.setAttribute("aria-label", `查看生成图与 ${detail.name} 原图的对比`);
    const image = document.createElement("img");
    image.src = sample.url;
    image.alt = `${detail.name} 的生成图`;
    image.loading = "lazy";
    image.decoding = "async";
    const meta = make("div", "sample-meta");
    meta.append(
      make("strong", "", sample.model || "生成图"),
      document.createTextNode(`${sample.size || "—"}，${formatElapsed(Number(sample.seconds) || 0)}`)
    );
    button.append(image, meta);
    if (index === 0) button.append(make("span", "sample-new-badge", "最新"));
    button.addEventListener("click", () => openCompareLightbox(detail, sample));
    grid.append(button);
  });
  section.append(grid);
  section.append(make("p", "samples-hint", "点击样图，与原图并排对比。"));
  return section;
}

async function copyEnglishPrompt() {
  const textarea = els.detailPanel.querySelector('textarea[data-field="prompt_en"]');
  const text = textarea?.value?.trim() || "";
  if (!text) {
    toast("英文提示词为空", "error");
    return;
  }
  try {
    await copyText(text);
    toast("英文提示词已复制", "success");
  } catch (error) {
    toast(`复制失败：${error.message}`, "error");
  }
}

async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch (error) {
      // Fall through to the selection-based copy path.
    }
  }
  const fallback = document.createElement("textarea");
  fallback.value = text;
  fallback.setAttribute("readonly", "");
  fallback.style.position = "fixed";
  fallback.style.top = "-1000px";
  fallback.style.opacity = "0";
  document.body.append(fallback);
  fallback.select();
  const ok = document.execCommand("copy");
  fallback.remove();
  if (!ok) throw new Error("浏览器拒绝了复制操作");
}

async function generateImage() {
  const detail = state.detail;
  if (!detail) return;
  const existing = state.jobs.get(detail.id);
  if (existing && ["starting", "queued", "running"].includes(existing.status)) return;
  if (state.submitting.has(detail.id)) return;

  const textarea = els.detailPanel.querySelector('textarea[data-field="prompt_en"]');
  const prompt = textarea?.value?.trim() || "";
  if (!prompt) {
    toast("请先填写英文提示词", "error");
    return;
  }

  const job = {
    id: "",
    itemId: detail.id,
    itemName: detail.name,
    status: "starting",
    startedAt: Date.now(),
    seconds: 0,
    error: null,
    image_url: null,
    lastPoll: 0,
    fetching: false,
    failures: 0
  };
  state.jobs.set(detail.id, job);
  state.submitting.add(detail.id);
  for (const node of els.detailPanel.querySelectorAll(".sample-card.is-new")) {
    node.classList.remove("is-new");
  }
  updateGenerationUI();

  try {
    const result = await api(`/api/items/${encodeURIComponent(detail.id)}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt,
        model: state.choices.model,
        size: state.choices.size,
        count: 1
      })
    });
    if (state.jobs.get(detail.id) !== job) return;
    job.id = result.job_id;
    job.status = "queued";
    job.lastPoll = 0;
    toast("生成任务已加入队列", "info");
  } catch (error) {
    if (state.jobs.get(detail.id) === job) state.jobs.delete(detail.id);
    toast(`提交生成任务失败：${error.message}`, "error", 6000);
  } finally {
    state.submitting.delete(detail.id);
    updateGenerationUI();
  }
}

function tickJobs() {
  const now = Date.now();
  for (const job of state.jobs.values()) {
    const active = job.status === "queued" || job.status === "running";
    if (active && job.id && !job.fetching && now - job.lastPoll >= 1100) {
      job.lastPoll = now;
      void pollJob(job);
    }
  }
  updateGenerationUI();
}

async function pollJob(job) {
  if (job.fetching || !job.id) return;
  job.fetching = true;
  try {
    const snapshot = await api(`/api/jobs/${encodeURIComponent(job.id)}`);
    if (state.jobs.get(job.itemId) !== job) return;
    job.status = snapshot.status;
    job.seconds = Number(snapshot.seconds) || 0;
    job.error = snapshot.error || null;
    job.image_url = snapshot.image_url || null;
    job.failures = 0;

    if (snapshot.status === "done") {
      await refreshAfterGeneration(job.itemId);
      if (state.jobs.get(job.itemId) !== job) return;
      const count = Array.isArray(state.detail?.samples) ? state.detail.samples.length : 0;
      job.completionMessage = `生成完成，共 ${count} 张`;
      updateGenerationUI();
      toast(`生成完成 · 用时 ${formatElapsed(job.seconds)}`, "success", 5200);
      revealNewestSample();
    } else if (snapshot.status === "error") {
      toast(`生成失败：${snapshot.error || "未知错误"}`, "error", 7000);
      const status = els.detailPanel.querySelector("[data-job-status]");
      status?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  } catch (error) {
    if (state.jobs.get(job.itemId) !== job) return;
    job.failures += 1;
    if (job.failures === 1 || job.failures === 5) {
      toast(`生成状态读取失败，正在重试：${error.message}`, "error", 5000);
    }
  } finally {
    job.fetching = false;
    updateGenerationUI();
  }
}

function revealNewestSample() {
  const card = els.detailPanel.querySelector(".sample-card");
  if (!card) return;
  card.classList.add("is-new");
  window.setTimeout(() => card.classList.remove("is-new"), 2000);
  card.scrollIntoView({ block: "center", behavior: "smooth" });
}

function updateGenerationUI() {
  const detail = state.detail;
  if (!detail) return;
  const job = state.jobs.get(detail.id);
  const generate = els.detailPanel.querySelector("[data-generate-button]");
  const modelSelect = els.detailPanel.querySelector("[data-model-select]");
  const sizeSelect = els.detailPanel.querySelector("[data-size-select]");
  const status = els.detailPanel.querySelector("[data-job-status]");
  if (!generate || !status) return;

  const active = Boolean(job && ["starting", "queued", "running"].includes(job.status));
  generate.disabled = active;
  generate.textContent = active ? "正在生成" : "生成图片";
  if (modelSelect) modelSelect.disabled = active;
  if (sizeSelect) sizeSelect.disabled = active;

  if (!job) {
    setGenerationStatus(status, "idle", "生成会使用编辑器中的英文提示词，约需 1 分钟。", false);
    return;
  }

  if (active) {
    const elapsed = formatElapsed(Math.max(0, (Date.now() - job.startedAt) / 1000));
    let text = "正在提交生成请求…";
    if (job.status === "queued") text = `排队中，已等待 ${elapsed}`;
    if (job.status === "running") text = `生成中，已用时 ${elapsed}`;
    setGenerationStatus(status, "running", text, true);
    return;
  }

  if (job.status === "done") {
    setGenerationStatus(
      status,
      "done",
      job.completionMessage || `生成完成，用时 ${formatElapsed(job.seconds)}。`,
      false
    );
    return;
  }

  if (job.status === "error") {
    setGenerationStatus(status, "error", `生成失败：${job.error || "未知错误"}`, false);
    return;
  }

  setGenerationStatus(status, "idle", "生成会使用编辑器中的英文提示词，约需 1 分钟。", false);
}

function setGenerationStatus(node, mode, text, withSpinner) {
  if (node.dataset.mode === mode && node.dataset.text === text) return;
  const keepSpinner = mode === "running" && node.dataset.mode === "running" && node.firstElementChild?.classList.contains("spinner");
  node.className = "generation-status";
  node.dataset.mode = mode;
  node.dataset.text = text;
  if (mode === "running") node.classList.add("is-running");
  if (mode === "done") node.classList.add("is-done");
  if (mode === "error") node.classList.add("is-error");
  if (keepSpinner) {
    node.lastChild.textContent = text;
    return;
  }
  node.replaceChildren();
  if (withSpinner) node.append(make("span", "spinner"));
  node.append(document.createTextNode(text));
}

// ---------- lightbox ----------

function openSingleLightbox(detail) {
  const wrap = make("div", "lightbox-single");
  const image = document.createElement("img");
  image.src = mediaUrl(detail.local_path);
  image.alt = detail.alt || `${detail.name} 原图`;
  wrap.append(image);
  showLightbox(wrap, "原图预览");
}

function openCompareLightbox(detail, sample) {
  const compare = make("div", "compare");
  compare.append(
    comparePane("主图", detail.name, mediaUrl(detail.local_path), `${detail.alt || detail.name}`),
    comparePane("生成图", sample.model || "生成图", sample.url, `${sample.size || ""}，${formatElapsed(Number(sample.seconds) || 0)}`)
  );
  showLightbox(compare, "主图与生成图对比");
}

function comparePane(label, title, url, meta) {
  const pane = make("div", "compare-pane");
  const frame = make("div", "compare-frame");
  const image = document.createElement("img");
  image.src = url;
  image.alt = `${title} ${label}`;
  frame.append(image);
  const caption = make("div", "compare-caption");
  const labelEl = make("span", "compare-label");
  labelEl.append(make("strong", "", label));
  caption.append(
    labelEl,
    make("span", "compare-title", title),
    make("span", "compare-meta", meta)
  );
  pane.append(frame, caption);
  return pane;
}

function showLightbox(content, label) {
  state.lastFocus = document.activeElement;
  els.lightboxContent.replaceChildren(content);
  els.lightboxDialog.setAttribute("aria-label", label);
  els.lightbox.hidden = false;
  els.lightbox.setAttribute("aria-hidden", "false");
  document.body.classList.add("modal-open");
  state.lightboxOpen = true;
  els.lightboxClose.focus();
}

function closeLightbox() {
  if (!state.lightboxOpen) return;
  state.lightboxOpen = false;
  els.lightbox.hidden = true;
  els.lightbox.setAttribute("aria-hidden", "true");
  els.lightboxContent.replaceChildren();
  document.body.classList.remove("modal-open");
  if (state.lastFocus instanceof HTMLElement) {
    state.lastFocus.focus({ preventScroll: true });
  }
  state.lastFocus = null;
}

// ---------- helpers ----------

async function api(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    ...options
  });
  const text = await response.text();
  let payload = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch (error) {
      payload = null;
    }
  }
  if (!response.ok) {
    const message = payload?.error || `请求失败（${response.status}）`;
    const failure = new Error(message);
    failure.status = response.status;
    throw failure;
  }
  return payload;
}

function make(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function sectionTitle(label, count = "") {
  const title = make("h3", "section-title");
  title.append(make("span", "", label));
  if (count) title.append(make("span", "section-count", count));
  return title;
}

function mediaUrl(localPath) {
  if (!localPath) return "";
  return "/media/" + String(localPath).split("/").map(encodeURIComponent).join("/");
}

function safeHttpUrl(value) {
  if (!value) return null;
  try {
    const url = new URL(value);
    if (url.protocol === "http:" || url.protocol === "https:") return url.href;
  } catch (error) {
    return null;
  }
  return null;
}

function isHexColor(value) {
  return typeof value === "string" && /^#[0-9a-f]{6}$/i.test(value);
}

function countWords(text) {
  const trimmed = String(text || "").trim();
  if (!trimmed) return 0;
  return trimmed.split(/\s+/).length;
}

function formatElapsed(seconds) {
  const value = Math.max(0, Number(seconds) || 0);
  if (value < 60) return `${value.toFixed(1)} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value % 60);
  return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
}

function formatCount(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "0";
  return new Intl.NumberFormat("zh-CN").format(numeric);
}

function toast(message, type = "info", duration = 4200) {
  const node = make("div", `toast${type === "success" ? " is-success" : type === "error" ? " is-error" : ""}`, message);
  while (els.toasts.childElementCount >= 4) {
    els.toasts.firstElementChild?.remove();
  }
  els.toasts.append(node);
  window.setTimeout(() => {
    node.classList.add("is-leaving");
    window.setTimeout(() => node.remove(), 200);
  }, duration);
}