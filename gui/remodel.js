"use strict";

(function () {
  const UPLOAD_LIMIT = 12 * 1024 * 1024;
  const ALLOWED_TYPES = new Set(["image/jpeg", "image/png", "image/webp"]);
  const MODES = new Set(["img2img", "text"]);
  const ROUTE_LIST = "#remodel";
  const ROUTE_PREFIX = "#remodel/";

  const state = {
    loaded: false,
    packs: [],
    packsLoading: null,
    listLoaded: false,
    listLoading: false,
    listRequest: 0,
    loadingMore: false,
    pageSize: 24,
    items: [],
    total: 0,
    current: null,
    detailItemId: "",
    detailRequest: 0,
    detailLoading: false,
    paramsInitialized: false,
    historyFilter: "all",
    mode: "img2img",
    style: "",
    generating: false,
    analyzing: false,
    uploading: false,
    detailOptimizeOpen: false,
    job: null,
    pollTimer: null,
    pollBusy: false,
    pendingDeleteGid: "",
    pendingNoteGid: "",
    pendingCompareGid: "",
    pendingDeleteItemId: "",
    createFile: null,
    createPreviewUrl: ""
  };

  const els = {};

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    cacheElements();
    bindNavigation();
    bindListControls();
    bindDetailControls();
    bindDialogs();
    bindVisibility();
    initLightbox();
    renderModeState();
    route();
  }

  function cacheElements() {
    els.view = document.getElementById("remodel-view");
    els.listView = document.getElementById("rm-list-view");
    els.detailView = document.getElementById("rm-detail-view");
    els.itemGrid = document.getElementById("rm-item-grid");
    els.itemCount = document.getElementById("rm-item-count");
    els.listSkeleton = document.getElementById("rm-list-skeleton");
    els.listEmpty = document.getElementById("rm-list-empty");
    els.listError = document.getElementById("rm-list-error");
    els.listMore = document.getElementById("rm-list-more");
    els.loadMore = document.getElementById("rm-load-more");
    els.newItem = document.getElementById("rm-new-item");
    els.emptyNew = document.getElementById("rm-empty-new");
    els.back = document.getElementById("rm-back");
    els.detailName = document.getElementById("rm-detail-name");
    els.detailMeta = document.getElementById("rm-detail-meta");
    els.refreshItem = document.getElementById("rm-refresh-item");
    els.renameItem = document.getElementById("rm-rename-item");
    els.deleteItem = document.getElementById("rm-delete-item");
    els.sourceFrame = document.getElementById("rm-source-frame");
    els.sourceImg = document.getElementById("rm-source-img");
    els.sourceFile = document.getElementById("rm-source-file");
    els.replaceSource = document.getElementById("rm-replace-source");
    els.sourceStatus = document.getElementById("rm-source-status");
    els.reanalyze = document.getElementById("rm-reanalyze-item");
    els.immovable = document.getElementById("rm-immovable-list");
    els.movable = document.getElementById("rm-movable-list");
    els.analysisError = document.getElementById("rm-analysis-error");
    els.analysisState = document.getElementById("rm-analysis-state");
    els.structureEn = document.getElementById("rm-structure-en");
    els.detailMode = document.getElementById("rm-detail-mode");
    els.styleSelect = document.getElementById("rm-detail-style");
    els.promptOverride = document.getElementById("rm-prompt-override");
    els.detailEnabled = document.getElementById("rm-detail-enabled");
    els.detailInput = document.getElementById("rm-detail-input");
    els.detailCount = document.getElementById("rm-detail-count");
    els.genNote = document.getElementById("rm-gen-note");
    els.generate = document.getElementById("rm-generate-item");
    els.lastGen = document.getElementById("rm-last-gen");
    els.itemProgress = document.getElementById("rm-item-progress");
    els.itemProgressText = document.getElementById("rm-item-progress-text");
    els.itemProgressHint = document.getElementById("rm-item-progress-hint");
    els.currentCard = document.getElementById("rm-current-card");
    els.currentBadges = document.getElementById("rm-current-badges");
    els.currentFrame = document.getElementById("rm-current-frame");
    els.currentImg = document.getElementById("rm-current-img");
    els.currentMeta = document.getElementById("rm-current-meta");
    els.currentPrompt = document.getElementById("rm-current-prompt");
    els.promptBrief = document.getElementById("rm-prompt-brief");
    els.promptExtra = document.getElementById("rm-prompt-extra");
    els.promptExtraValue = document.getElementById("rm-prompt-extra-value");
    els.promptToggleText = document.getElementById("rm-prompt-toggle-text");
    els.promptMeta = document.getElementById("rm-prompt-meta");
    els.promptText = document.getElementById("rm-prompt-text");
    els.promptNegativeWrap = document.getElementById("rm-prompt-negative-wrap");
    els.promptNegative = document.getElementById("rm-prompt-negative");
    els.promptCopy = document.getElementById("rm-prompt-copy");
    els.currentDownload = document.getElementById("rm-current-download");
    els.currentSource = document.getElementById("rm-current-source");
    els.currentCompare = document.getElementById("rm-current-compare");
    els.currentEmpty = document.getElementById("rm-current-empty");
    els.currentDetail = document.getElementById("rm-current-detail");
    els.currentProgress = document.getElementById("rm-current-progress");
    els.currentProgressText = document.getElementById("rm-current-progress-text");
    els.detailOptimizePanel = document.getElementById("rm-detail-optimize-panel");
    els.detailOptimizeInput = document.getElementById("rm-detail-optimize-input");
    els.detailOptimizeSubmit = document.getElementById("rm-detail-optimize-submit");
    els.detailOptimizeCancel = document.getElementById("rm-detail-optimize-cancel");
    els.genGrid = document.getElementById("rm-gen-grid");
    els.genFilters = document.getElementById("rm-gen-filters");
    els.genEmpty = document.getElementById("rm-gen-empty");
    els.toasts = document.getElementById("toasts");
    els.dialogCreate = document.getElementById("rm-dialog-create");
    els.dialogRename = document.getElementById("rm-dialog-rename");
    els.dialogDelete = document.getElementById("rm-dialog-delete");
    els.dialogPurge = document.getElementById("rm-dialog-purge");
    els.dialogGenDelete = document.getElementById("rm-dialog-gen-delete");
    els.dialogNote = document.getElementById("rm-dialog-note");
    els.dialogCompare = document.getElementById("rm-dialog-compare");
    els.createDrop = document.getElementById("rm-create-drop");
    els.createPlaceholder = document.getElementById("rm-create-placeholder");
    els.createPreview = document.getElementById("rm-create-preview");
    els.createFileActions = document.getElementById("rm-create-file-actions");
    els.createReselect = document.getElementById("rm-create-reselect");
    els.createRemove = document.getElementById("rm-create-remove");
    els.createFile = document.getElementById("rm-create-file");
    els.createFileName = document.getElementById("rm-create-file-name");
    els.createName = document.getElementById("rm-create-name");
    els.createError = document.getElementById("rm-create-error");
    els.createSubmit = document.getElementById("rm-create-submit");
    els.renameInput = document.getElementById("rm-rename-input");
    els.renameError = document.getElementById("rm-rename-error");
    els.renameSubmit = document.getElementById("rm-rename-submit");
    els.deleteSoft = document.getElementById("rm-delete-soft");
    els.deletePurgeOpen = document.getElementById("rm-delete-purge-open");
    els.purgeHint = document.getElementById("rm-purge-hint");
    els.purgeInput = document.getElementById("rm-purge-input");
    els.purgeError = document.getElementById("rm-purge-error");
    els.purgeSubmit = document.getElementById("rm-purge-submit");
    els.genDeleteSubmit = document.getElementById("rm-gen-delete-submit");
    els.noteInput = document.getElementById("rm-note-input");
    els.noteError = document.getElementById("rm-note-error");
    els.noteSubmit = document.getElementById("rm-note-submit");
    els.compareBefore = document.getElementById("rm-compare-before");
    els.compareAfter = document.getElementById("rm-compare-after");
    els.compareBeforeFrame = els.compareBefore && els.compareBefore.parentElement;
    els.compareAfterFrame = els.compareAfter && els.compareAfter.parentElement;
    els.compareDownload = document.getElementById("rm-compare-download");
  }

  // ---------- navigation ----------

  function bindNavigation() {
    for (const tab of document.querySelectorAll(".mode-tab")) {
      tab.addEventListener("click", () => {
        const view = tab.dataset.view === "remodel" ? "remodel" : "gallery";
        if (view === "remodel") {
          if (window.location.hash !== ROUTE_LIST && !window.location.hash.startsWith(ROUTE_PREFIX)) {
            window.location.hash = ROUTE_LIST;
          } else {
            route();
          }
        } else {
          if (window.location.hash) window.location.hash = "";
          else route();
        }
      });
    }
    window.addEventListener("hashchange", route);
  }

  function bindVisibility() {
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) {
        stopPolling();
        return;
      }
      const hash = window.location.hash || "";
      if (hash.startsWith(ROUTE_PREFIX)) {
        if (state.detailItemId) void refreshDetail(state.detailItemId);
        else route();
      } else if (hash === ROUTE_LIST) {
        void loadItems();
      }
    });
  }

  function setShellView(view) {
    const isRemodel = view === "remodel";
    document.body.classList.toggle("view-remodel", isRemodel);
    const workspace = document.querySelector(".workspace");
    if (workspace) workspace.hidden = isRemodel;
    if (els.view) els.view.hidden = !isRemodel;
    for (const tab of document.querySelectorAll(".mode-tab")) {
      const active = tab.dataset.view === view;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-pressed", String(active));
    }
    if (isRemodel) document.body.classList.remove("detail-panel-open");
  }

  function route() {
    const hash = window.location.hash || "";
    if (hash === ROUTE_LIST || hash.startsWith(ROUTE_PREFIX)) {
      setShellView("remodel");
      const rawId = hash.startsWith(ROUTE_PREFIX)
        ? hash.slice(ROUTE_PREFIX.length).split("?")[0]
        : "";
      const itemId = safeDecode(rawId);
      if (itemId) {
        void openDetail(itemId);
      } else {
        showList();
      }
      return;
    }
    stopPolling();
    closeLightbox(false);
    setShellView("gallery");
  }

  function showList() {
    state.detailItemId = "";
    state.detailRequest += 1;
    state.current = null;
    state.paramsInitialized = false;
    state.historyFilter = "all";
    state.job = null;
    stopPolling();
    hideProgress();
    resetDetailOptimize();
    closeLightbox(false);
    if (els.detailView) els.detailView.hidden = true;
    if (els.listView) els.listView.hidden = false;
    void loadItems();
  }

  function navigateToList() {
    if (window.location.hash === ROUTE_LIST) showList();
    else window.location.hash = ROUTE_LIST;
  }

  // ---------- data ----------

  async function apiJson(url, options = {}) {
    const response = await fetch(url, { cache: "no-store", ...options });
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
      const failure = new Error(errorMessage(payload, response.status));
      failure.status = response.status;
      failure.payload = payload;
      throw failure;
    }
    return payload;
  }

  function freshUrl(url) {
    const parsed = new URL(url, window.location.origin);
    parsed.searchParams.set("t", String(Date.now()));
    return parsed.pathname + parsed.search;
  }

  function errorMessage(payload, status) {
    if (payload && typeof payload.error === "object" && payload.error) {
      return String(payload.error.message || payload.error.code || `请求失败（${status}）`);
    }
    if (payload && payload.error) return String(payload.error);
    return `请求失败（${status}）`;
  }

  function uploadFormWithProgress(url, form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", url);
      xhr.responseType = "text";
      if (xhr.upload && typeof onProgress === "function") {
        xhr.upload.onprogress = (event) => {
          if (event.lengthComputable) onProgress(event.loaded, event.total);
        };
      }
      xhr.onload = () => {
        let payload = null;
        try {
          payload = xhr.responseText ? JSON.parse(xhr.responseText) : null;
        } catch (error) {
          payload = null;
        }
        if (xhr.status >= 200 && xhr.status < 300) {
          resolve(payload);
          return;
        }
        const failure = new Error(errorMessage(payload, xhr.status));
        failure.status = xhr.status;
        failure.payload = payload;
        reject(failure);
      };
      xhr.onerror = () => reject(new Error("网络错误，上传失败"));
      xhr.onabort = () => reject(new Error("上传已取消"));
      xhr.send(form);
    });
  }

  function ensurePacks() {
    if (state.packs.length) return Promise.resolve(state.packs);
    if (state.packsLoading) return state.packsLoading;
    state.packsLoading = apiJson("/api/remodel/packs")
      .then((packs) => {
        state.packs = Array.isArray(packs) ? packs : [];
        return state.packs;
      })
      .catch((error) => {
        notify(`风格包加载失败：${error.message}`, "error", 6000);
        state.packs = [];
        return state.packs;
      })
      .finally(() => {
        state.packsLoading = null;
        state.loaded = true;
      });
    return state.packsLoading;
  }

  async function loadItems() {
    if (!els.itemGrid) return;
    state.listLoading = true;
    const request = ++state.listRequest;
    els.listError.hidden = true;
    if (!state.items.length) {
      els.listSkeleton.hidden = false;
      els.listEmpty.hidden = true;
      els.itemGrid.replaceChildren();
    }
    try {
      const data = await apiJson(
        freshUrl(`/api/remodel/items?limit=${state.pageSize}&offset=0`)
      );
      if (request !== state.listRequest) return;
      state.items = Array.isArray(data && data.items) ? data.items : [];
      state.total = Number(data && data.total) || state.items.length;
      state.listLoaded = true;
      renderList();
    } catch (error) {
      if (request !== state.listRequest) return;
      els.listError.textContent = `项目列表加载失败：${error.message}`;
      els.listError.hidden = false;
      if (!state.items.length) {
        els.listEmpty.hidden = true;
        els.itemGrid.replaceChildren();
      }
    } finally {
      if (request === state.listRequest) {
        state.listLoading = false;
        els.listSkeleton.hidden = true;
      }
    }
  }

  async function loadMoreItems() {
    if (state.listLoading || state.loadingMore) return;
    if (state.items.length >= state.total) return;
    state.loadingMore = true;
    updateLoadMore();
    try {
      const data = await apiJson(
        freshUrl(`/api/remodel/items?limit=${state.pageSize}&offset=${state.items.length}`)
      );
      const rows = Array.isArray(data && data.items) ? data.items : [];
      const seen = new Set(state.items.map((entry) => entry.item_id));
      const fresh = rows.filter((entry) => entry && !seen.has(entry.item_id));
      state.items.push(...fresh);
      state.total = Number(data && data.total) || state.items.length;
      if (fresh.length) appendItemCards(fresh);
      else if (!rows.length) state.total = state.items.length;
    } catch (error) {
      notify(`加载更多失败：${error.message}`, "error", 6000);
    } finally {
      state.loadingMore = false;
      updateLoadMore();
    }
  }

  function appendItemCards(rows) {
    if (!els.itemGrid) return;
    els.listEmpty.hidden = true;
    els.itemGrid.hidden = false;
    for (const item of rows) els.itemGrid.append(renderItemCard(item));
  }

  function updateLoadMore() {
    const more = state.items.length > 0 && state.items.length < state.total;
    if (els.listMore) els.listMore.hidden = !more;
    if (els.loadMore) {
      els.loadMore.disabled = state.loadingMore;
      els.loadMore.textContent = state.loadingMore ? "加载中…" : "加载更多";
    }
  }

  // ---------- list ----------

  function renderList() {
    if (!els.itemGrid) return;
    els.itemCount.textContent = String(state.total);
    els.itemGrid.replaceChildren();
    if (!state.items.length) {
      els.listEmpty.hidden = false;
      els.itemGrid.hidden = true;
      updateLoadMore();
      return;
    }
    els.listEmpty.hidden = true;
    els.itemGrid.hidden = false;
    for (const item of state.items) {
      els.itemGrid.append(renderItemCard(item));
    }
    updateLoadMore();
  }

  function renderItemCard(item) {
    const card = el("article", "rm-item-card");
    card.dataset.itemId = item.item_id;

    const coverLink = el("a", "rm-item-cover");
    coverLink.href = ROUTE_PREFIX + encodeURIComponent(item.item_id);
    coverLink.setAttribute("aria-label", `打开项目 ${item.name || ""}`);
    if (item.thumb_url || item.cover_url) {
      const image = document.createElement("img");
      image.src = item.thumb_url || item.cover_url;
      image.alt = `${item.name || "改造项目"}的封面`;
      image.width = Number(item.thumb_width) || 560;
      image.height = Number(item.thumb_height) || 420;
      image.loading = "lazy";
      image.decoding = "async";
      let fellBack = false;
      image.addEventListener(
        "error",
        () => {
          if (!fellBack && item.cover_url) {
            fellBack = true;
            image.src = item.cover_url;
          }
        },
        { once: true }
      );
      coverLink.append(image);
    } else {
      coverLink.append(el("span", "rm-item-cover-empty", "等待原图"));
    }
    if (item.running) {
      const badge = el("span", "rm-running-badge");
      badge.append(el("i", "rm-running-dot"), el("span", "", "生成中"));
      coverLink.append(badge);
    }
    card.append(coverLink);

    const body = el("div", "rm-item-body");
    const titleRow = el("div", "rm-item-title-row");
    const heading = el("h3", "rm-item-name");
    const titleLink = el("a", "", item.name || "未命名项目");
    titleLink.href = coverLink.href;
    heading.append(titleLink);
    titleRow.append(heading, renderItemMenu(item));
    body.append(titleRow);

    const tags = el("div", "rm-item-tags");
    const styles = Array.isArray(item.styles) ? item.styles : [];
    if (!styles.length) {
      tags.append(el("span", "rm-tag muted", "尚未生成"));
    } else {
      tags.append(el("span", "rm-tag", styles[0]));
      if (styles.length > 1) {
        tags.append(el("span", "rm-tag muted", `+${styles.length - 1}`));
      }
    }
    body.append(tags);

    const meta = el("p", "rm-item-meta");
    const count = Number(item.gen_count) || 0;
    meta.append(
      el("span", "", `共 ${count} 个版本`),
      el("span", "rm-dot-sep", "·"),
      el("span", "", `更新于 ${formatTime(item.updated_at)}`)
    );
    body.append(meta);

    if (item.last_error && !count && !item.running) {
      const fullError = String(item.last_error).trim();
      const errorLine = el("p", "rm-item-error", compactFailureReason(fullError));
      errorLine.title = fullError;
      errorLine.setAttribute("aria-label", fullError);
      body.append(errorLine);
    }
    card.append(body);
    return card;
  }

  function renderItemMenu(item) {
    const wrap = el("div", "rm-item-menu-wrap");
    const button = el("button", "rm-icon-button", "⋯");
    button.type = "button";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", `${item.name || "项目"}的更多操作`);
    const menu = el("div", "rm-item-menu");
    menu.setAttribute("role", "menu");
    menu.hidden = true;
    for (const [action, label] of [
      ["open", "打开"],
      ["rename", "重命名"],
      ["delete", "删除"]
    ]) {
      const entry = el("button", "rm-menu-item", label);
      entry.type = "button";
      entry.setAttribute("role", "menuitem");
      entry.dataset.action = action;
      menu.append(entry);
    }
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeMenus(menu);
      menu.hidden = !menu.hidden;
      button.setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) {
        const first = menu.querySelector("button");
        if (first) first.focus();
      }
    });
    menu.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-action]") : null;
      if (!target) return;
      event.stopPropagation();
      const action = target.dataset.action;
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
      if (action === "open") window.location.hash = ROUTE_PREFIX + encodeURIComponent(item.item_id);
      else if (action === "rename") openRenameDialog(item);
      else if (action === "delete") openDeleteDialog(item);
    });
    wrap.append(button, menu);
    return wrap;
  }

  function closeMenus(except) {
    for (const menu of document.querySelectorAll(".rm-item-menu")) {
      if (menu === except) continue;
      menu.hidden = true;
      const button = menu.parentElement && menu.parentElement.querySelector(".rm-icon-button");
      if (button) button.setAttribute("aria-expanded", "false");
    }
  }

  document.addEventListener("click", (event) => {
    if (event.target instanceof Element && event.target.closest(".rm-item-menu-wrap")) return;
    closeMenus();
  });

  // ---------- detail ----------

  async function openDetail(itemId) {
    if (!/^[0-9a-f]{8,32}$/.test(itemId)) {
      navigateToList();
      return;
    }
    if (els.listView) els.listView.hidden = true;
    if (els.detailView) els.detailView.hidden = false;
    const isNewItem = state.detailItemId !== itemId;
    if (isNewItem) {
      closeLightbox(false);
      state.detailItemId = itemId;
      state.current = null;
      state.generating = false;
      state.analyzing = false;
      state.job = null;
      state.paramsInitialized = false;
      state.historyFilter = "all";
      stopPolling();
      hideProgress();
      resetDetailOptimize();
      if (els.promptOverride) els.promptOverride.value = "";
      resetDetailControls();
      if (els.genNote) els.genNote.value = "";
      if (els.detailName) els.detailName.textContent = "正在加载…";
    }
    await ensurePacks();
    await loadDetail(itemId, { entry: isNewItem });
  }

  async function loadDetail(itemId, options = {}) {
    const request = ++state.detailRequest;
    setDetailLoading(true);
    try {
      const item = await apiJson(
        freshUrl(`/api/remodel/items/${encodeURIComponent(itemId)}`)
      );
      if (request !== state.detailRequest || state.detailItemId !== itemId) return;
      applyItem(item, options);
      if (item.running && item.running_job) {
        state.generating = true;
        startPolling(item.running_job);
      }
    } catch (error) {
      if (request !== state.detailRequest || state.detailItemId !== itemId) return;
      if (error.status === 404) {
        notify("改造项目不存在或已被删除", "error", 5000);
        navigateToList();
        return;
      }
      notify(`项目加载失败：${error.message}`, "error", 6000);
    } finally {
      if (request === state.detailRequest) setDetailLoading(false);
    }
  }

  function setDetailLoading(loading) {
    state.detailLoading = loading;
    if (!els.refreshItem) return;
    els.refreshItem.disabled = loading;
    els.refreshItem.textContent = loading ? "刷新中…" : "刷新";
  }

  async function refreshDetail(itemId) {
    await loadDetail(itemId, { entry: false });
    if (state.job && state.job.id && !state.pollTimer) {
      startPolling(state.job.id);
    }
  }

  function applyItem(item, options = {}) {
    if (!item || item.item_id !== state.detailItemId) return;
    state.current = item;
    if (options.entry || !state.paramsInitialized) {
      initializeParams(item);
      state.paramsInitialized = true;
    }
    renderDetail(options.resetInputs === true);
  }

  function initializeParams(item) {
    const latest = Array.isArray(item.gens) ? item.gens[0] : null;
    const latestMode = latest && MODES.has(latest.mode) ? latest.mode : "";
    const defaultMode = MODES.has(item.default_mode) ? item.default_mode : "";
    const globalStyle = state.packs[0] && state.packs[0].style_zh;
    state.mode = latestMode || defaultMode || "img2img";
    state.style =
      String((latest && latest.style) || "").trim() ||
      String(item.default_style || "").trim() ||
      String(globalStyle || "");
  }

  function renderDetail(resetInputs) {
    const item = state.current;
    if (!item) return;
    els.detailName.textContent = item.name || "未命名项目";
    const versions = Number(item.gen_count) || 0;
    const failures = Array.isArray(item.failed_jobs)
      ? item.failed_jobs.filter((job) => job && job.status === "error").length
      : 0;
    const versionSummary = failures
      ? `${versions} 个版本 · ${failures} 次失败`
      : `${versions} 个版本`;
    els.detailMeta.textContent = `${versionSummary} · 更新于 ${formatTime(item.updated_at)}`;
    renderSource(item);
    renderAnalysis(item);
    renderParams(item, resetInputs);
    renderCurrent(item);
    renderGens(item);
  }

  function renderSource(item) {
    const source =
      item.source && typeof item.source === "object" && item.source.current
        ? item.source
        : Array.isArray(item.sources)
          ? item.sources.find((entry) => entry && entry.current)
          : null;
    if (item.source_url) {
      els.sourceImg.src = source && source.url ? source.url : item.source_url;
      els.sourceImg.alt = `${item.name || "改造项目"}的当前原图`;
      setImageSize(els.sourceImg, source && source.width, source && source.height);
      applyAssetFrame(els.sourceFrame, source && source.width, source && source.height);
      els.sourceImg.hidden = false;
      setZoomable(
        els.sourceImg,
        `放大预览 ${item.name || "改造项目"}的当前原图`
      );
    } else {
      els.sourceImg.removeAttribute("src");
      els.sourceImg.removeAttribute("width");
      els.sourceImg.removeAttribute("height");
      applyAssetFrame(els.sourceFrame, null, null);
      els.sourceImg.hidden = true;
      setZoomable(els.sourceImg, "", false);
    }
    els.sourceStatus.textContent = state.uploading ? "上传中…" : "";
    els.replaceSource.disabled = state.uploading || state.generating;
    if (item.source_url) els.currentSource.href = item.source_url;
    else els.currentSource.removeAttribute("href");
  }

  function renderAnalysis(item) {
    const structure = item.structure && typeof item.structure === "object" ? item.structure : {};
    const hasStructure = Boolean(String(structure.structure_en || "").trim());
    renderChips(els.immovable, structure.immovable_zh, "分析完成后显示不可改结构");
    renderChips(els.movable, structure.movable_zh, "分析完成后显示可替换软装");
    els.structureEn.textContent = String(structure.structure_en || "").trim() || "尚未分析。";
    if (els.analysisState) {
      if (hasStructure) {
        els.analysisState.hidden = true;
        els.analysisState.textContent = "";
      } else {
        els.analysisState.hidden = false;
        els.analysisState.textContent =
          state.mode === "text"
            ? "纯文本模式需要结构分析，生成时会先分析"
            : "图生图不需要结构分析";
      }
    }
    const message = String(item.analysis_error || "");
    els.analysisError.hidden = !message;
    els.analysisError.textContent = message;
    els.reanalyze.disabled = state.analyzing;
    els.reanalyze.textContent = state.analyzing
      ? "分析中…"
      : hasStructure
        ? "重新分析"
        : "分析结构";
    // Neither mode requires an analysis before submitting: img2img never
    // needs one, and text mode analyses inside its job.
    els.generate.disabled = state.generating || state.analyzing;
  }

  function renderChips(container, values, emptyText) {
    container.replaceChildren();
    const list = Array.isArray(values) ? values : [];
    if (!list.length) {
      container.append(el("span", "rm-empty-chip", emptyText));
      return;
    }
    for (const value of list) container.append(el("span", "rm-chip", String(value)));
  }

  function renderParams(item, resetInputs) {
    for (const button of els.detailMode.querySelectorAll("[data-item-mode]")) {
      button.setAttribute("aria-checked", String(button.dataset.itemMode === state.mode));
    }
    els.styleSelect.replaceChildren();
    if (!state.style) {
      state.style =
        String(item.default_style || "").trim() ||
        String((state.packs[0] && state.packs[0].style_zh) || "");
    }
    if (!state.packs.length) {
      const option = document.createElement("option");
      option.value = state.style;
      option.textContent = state.style || "风格包暂不可用";
      els.styleSelect.append(option);
    } else {
      if (
        state.style &&
        !state.packs.some((pack) => pack.style_zh === state.style)
      ) {
        const option = document.createElement("option");
        option.value = state.style;
        option.textContent = `${state.style} · 历史风格`;
        els.styleSelect.append(option);
      }
      for (const pack of state.packs) {
        const option = document.createElement("option");
        option.value = pack.style_zh;
        option.textContent = pack.style_en
          ? `${pack.style_zh} · ${pack.style_en}`
          : pack.style_zh;
        els.styleSelect.append(option);
      }
      els.styleSelect.value = state.style;
    }
    if (resetInputs) {
      els.promptOverride.value = "";
      resetDetailControls();
      els.genNote.value = "";
    }
    renderLastGeneration(item);
    els.generate.disabled = state.generating || state.analyzing;
  }

  function resetDetailControls() {
    if (els.detailEnabled) els.detailEnabled.checked = false;
    if (els.detailInput) els.detailInput.value = "";
    syncDetailControls(false);
  }

  function syncDetailControls(
    enabled = Boolean(els.detailEnabled && els.detailEnabled.checked)
  ) {
    if (els.detailInput) {
      els.detailInput.disabled = !enabled;
      if (enabled) els.detailInput.removeAttribute("aria-disabled");
      else els.detailInput.setAttribute("aria-disabled", "true");
    }
    updateDetailCount();
  }

  function updateDetailCount() {
    if (!els.detailCount) return;
    const used = els.detailInput ? els.detailInput.value.length : 0;
    els.detailCount.textContent = `${used}/300`;
  }

  function setDetailFromJob(snapshot) {
    const enabled = Boolean(snapshot && snapshot.detail_enabled);
    if (els.detailEnabled) els.detailEnabled.checked = enabled;
    if (els.detailInput) {
      els.detailInput.value = String((snapshot && snapshot.detail_zh) || "");
    }
    syncDetailControls(enabled);
  }

  function renderLastGeneration(item) {
    if (!els.lastGen) return;
    const latest = Array.isArray(item.gens) ? item.gens[0] : null;
    if (!latest) {
      els.lastGen.hidden = true;
      els.lastGen.textContent = "";
      return;
    }
    const parts = [
      `上次生成：${modeLabel(latest.mode)}`,
      latest.style || "未指定风格",
      formatScoreShort(latest.score)
    ];
    els.lastGen.textContent = parts.join(" · ");
    els.lastGen.hidden = false;
  }

  function renderCurrentPrompt(latest) {
    if (!els.currentPrompt) return;
    const prompt = latest ? String(latest.prompt || "") : "";
    if (!latest || !prompt) {
      els.currentPrompt.hidden = true;
      els.currentPrompt.open = false;
      if (els.promptBrief) els.promptBrief.textContent = "";
      if (els.promptExtraValue) els.promptExtraValue.textContent = "";
      if (els.promptExtra) els.promptExtra.hidden = true;
      if (els.promptToggleText) els.promptToggleText.textContent = "查看提示词";
      if (els.promptMeta) els.promptMeta.textContent = "";
      if (els.promptText) els.promptText.textContent = "";
      if (els.promptNegative) els.promptNegative.textContent = "";
      if (els.promptNegativeWrap) els.promptNegativeWrap.hidden = true;
      return;
    }
    els.currentPrompt.hidden = false;
    const brief = [latest.model, latest.size_requested, latest.style]
      .map((value) => String(value || "").trim())
      .filter(Boolean);
    const extraZh = String(latest.extra_zh || "").trim();
    const extraEn = String(latest.extra_en || "").trim();
    let extraValue = "";
    if (extraZh) {
      if (latest.extra_verbatim) {
        extraValue = `${extraZh}（未能翻译，按原文生效）`;
      } else if (extraEn) {
        extraValue = `${extraZh} → ${extraEn}`;
      } else {
        extraValue = extraZh;
      }
    }
    if (els.promptBrief) els.promptBrief.textContent = brief.join(" · ");
    if (els.promptExtraValue) els.promptExtraValue.textContent = extraValue;
    if (els.promptExtra) els.promptExtra.hidden = !extraValue;
    if (els.promptToggleText) els.promptToggleText.textContent = "查看提示词";
    let meta = `提示词 ${prompt.length} 字`;
    const detailZh = String(latest.detail_zh || "").trim();
    if (detailZh) {
      const short = detailZh.length > 40 ? `${detailZh.slice(0, 40)}…` : detailZh;
      meta += ` · 中文补充：${short}`;
    }
    if (latest.refine_of) meta += " · 基于上一版结果优化";
    if (els.promptMeta) els.promptMeta.textContent = meta;
    if (els.promptText) els.promptText.textContent = prompt;
    const negative = String(latest.negative || "").trim();
    if (els.promptNegative && els.promptNegativeWrap) {
      els.promptNegative.textContent = negative;
      els.promptNegativeWrap.hidden = !negative;
    }
    els.currentPrompt.open = false;
  }

  function currentPromptForCopy() {
    const prompt = els.promptText ? els.promptText.textContent || "" : "";
    if (!prompt) return "";
    const negative =
      els.promptNegative && els.promptNegativeWrap && !els.promptNegativeWrap.hidden
        ? els.promptNegative.textContent || ""
        : "";
    return negative ? `${prompt}\n\nNegative prompt:\n${negative}` : prompt;
  }

  function fallbackCopyText(text) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    let copied = false;
    try {
      copied = document.execCommand("copy");
    } catch (_error) {
      copied = false;
    }
    area.remove();
    return copied;
  }

  async function copyCurrentPrompt() {
    const text = currentPromptForCopy();
    if (!text || !els.promptCopy) return;
    let copied = false;
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch (_error) {
      copied = false;
    }
    if (!copied) copied = fallbackCopyText(text);
    if (!copied) {
      notify("复制失败，请手动选择提示词", "error", 4000);
      return;
    }
    const button = els.promptCopy;
    button.textContent = "已复制";
    window.setTimeout(() => {
      button.textContent = "复制";
    }, 1500);
  }

  function renderCurrent(item) {
    const gens = Array.isArray(item.gens) ? item.gens : [];
    const latest = gens[0];
    if (!latest) {
      els.currentCard.hidden = false;
      els.currentImg.removeAttribute("src");
      els.currentImg.hidden = true;
      if (els.currentEmpty) els.currentEmpty.hidden = false;
      els.currentBadges.replaceChildren();
      els.currentMeta.replaceChildren();
      appendMeta(els.currentMeta, "还没有结果图");
      els.currentDownload.removeAttribute("href");
      els.currentDownload.removeAttribute("download");
      els.currentDownload.setAttribute("aria-disabled", "true");
      els.currentDownload.tabIndex = -1;
      els.currentCompare.disabled = true;
      applyAssetFrame(els.currentFrame, null, null);
      setZoomable(els.currentImg, "", false);
      resetDetailOptimize();
      setDetailOptimizeBusy(false, false);
      hideOptimizeProgress();
      renderCurrentPrompt(null);
      return;
    }
    els.currentCard.hidden = false;
    els.currentImg.hidden = false;
    if (els.currentEmpty) els.currentEmpty.hidden = true;
    els.currentImg.src = latest.url;
    els.currentImg.alt = `最新改造结果：${latest.style || ""}`;
    setImageSize(els.currentImg, latest.width, latest.height);
    applyAssetFrame(els.currentFrame, latest.width, latest.height);
    setZoomable(
      els.currentImg,
      `放大预览最新版本：${latest.style || "改造结果"}`
    );
    els.currentBadges.replaceChildren();
    els.currentBadges.append(modeBadge(latest.mode));
    if (latest.style) els.currentBadges.append(el("span", "rm-tag", latest.style));
    if (latest.detail_enabled) {
      els.currentBadges.append(el("span", "rm-tag detail", "含细节"));
    }
    if (latest.refine_of) {
      els.currentBadges.append(el("span", "rm-tag refine", "由结果优化"));
    }
    if (item.cover_gen === latest.gid) els.currentBadges.append(el("span", "rm-tag cover", "封面"));
    els.currentMeta.replaceChildren();
    appendMeta(els.currentMeta, `生成于 ${formatTime(latest.created_at)}`);
    const assetSize = formatAssetSize(latest.width, latest.height, latest.size_final);
    if (assetSize) appendMeta(els.currentMeta, assetSize);
    appendMeta(els.currentMeta, formatScore(latest.score));
    const checkWarning = renderCheckWarning(latest);
    if (checkWarning) els.currentMeta.append(checkWarning);
    const attempts = Number(latest.attempts) || 0;
    if (attempts > 1) appendMeta(els.currentMeta, `第 ${attempts} 次尝试`);
    const design = designSummary(latest);
    if (design.lighting) appendMeta(els.currentMeta, design.lighting);
    if (design.decor) appendMeta(els.currentMeta, design.decor);
    renderCurrentPrompt(latest);
    els.currentDownload.removeAttribute("aria-disabled");
    els.currentDownload.removeAttribute("tabindex");
    els.currentDownload.href = latest.url;
    els.currentDownload.download = `${item.name || "remodel"}-${latest.gid.slice(0, 8)}.png`;
    els.currentCompare.disabled = false;
    els.currentCompare.dataset.gid = latest.gid;
    setDetailOptimizeBusy(Boolean(state.job && state.job.optimize), true);
  }

  function renderGens(item) {
    const gens = Array.isArray(item.gens) ? item.gens : [];
    const failed = Array.isArray(item.failed_jobs) ? item.failed_jobs : [];
    els.genGrid.replaceChildren();
    const entries = [
      ...gens.map((gen) => ({ kind: "gen", at: gen.created_at, data: gen })),
      ...failed.map((job) => ({ kind: "failed", at: job.created_at, data: job }))
    ];
    entries.sort((a, b) => String(b.at || "").localeCompare(String(a.at || "")));
    const filter = state.historyFilter;
    const filtered = entries.filter(
      (entry) => filter === "all" || entry.data.mode === filter
    );
    els.genEmpty.hidden = filtered.length > 0;
    els.genEmpty.textContent = entries.length
      ? "没有符合当前筛选的版本。"
      : "还没有生成版本。设置好参数后点击「生成改造方案」。";
    for (const button of els.genFilters.querySelectorAll("[data-gen-filter]")) {
      button.setAttribute(
        "aria-pressed",
        String(button.dataset.genFilter === state.historyFilter)
      );
    }
    for (const entry of filtered) {
      els.genGrid.append(
        entry.kind === "failed"
          ? renderFailedJobCard(item, entry.data)
          : renderGenCard(item, entry.data)
      );
    }
  }

  function renderGenCard(item, gen) {
    const card = el("article", "rm-gen-card");
    card.dataset.gid = gen.gid;
    card.dataset.mode = gen.mode === "text" ? "text" : "img2img";
    const thumb = el("button", "rm-gen-thumb");
    thumb.type = "button";
    thumb.setAttribute("aria-label", `放大预览 ${gen.style || "改造"} 版本`);
    const image = document.createElement("img");
    image.src = gen.thumb_url || gen.url;
    image.alt = `${gen.style || "改造"}版本缩略图`;
    setImageSize(
      image,
      Number(gen.thumb_width) || Number(gen.width),
      Number(gen.thumb_height) || Number(gen.height)
    );
    image.loading = "lazy";
    image.decoding = "async";
    let fellBack = false;
    image.addEventListener(
      "error",
      () => {
        if (!fellBack && gen.url) {
          fellBack = true;
          image.src = gen.url;
        }
      },
      { once: true }
    );
    thumb.append(image);
    thumb.addEventListener("click", () => openLightboxAtGen(gen.gid, thumb));
    card.append(thumb);

    const info = el("div", "rm-gen-info");
    const top = el("div", "rm-gen-top");
    top.append(modeBadge(gen.mode));
    if (gen.style) top.append(el("span", "rm-tag", gen.style));
    top.append(el("span", "rm-gen-score", formatScoreShort(gen.score)));
    if (gen.detail_enabled) top.append(el("span", "rm-tag detail", "含细节"));
    if (gen.refine_of) top.append(el("span", "rm-tag refine", "由结果优化"));
    if (item.cover_gen === gen.gid) top.append(el("span", "rm-tag cover", "封面"));
    top.append(renderGenMenu(item, gen));
    info.append(top);
    info.append(el("p", "rm-gen-time", formatTime(gen.created_at)));
    const outputSize = outputSizeLabel(gen);
    if (outputSize) info.append(el("p", "rm-gen-size", outputSize));
    if (gen.note) info.append(el("p", "rm-gen-note", gen.note));
    card.append(info);
    return card;
  }

  function renderGenMenu(item, gen) {
    const wrap = el("div", "rm-item-menu-wrap rm-gen-menu-wrap");
    const button = el("button", "rm-icon-button", "⋯");
    button.type = "button";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-label", `${gen.style || "改造"}版本的更多操作`);
    const menu = el("div", "rm-item-menu rm-gen-menu");
    menu.setAttribute("role", "menu");
    menu.hidden = true;

    const download = document.createElement("a");
    download.className = "rm-menu-item";
    download.href = gen.url;
    download.download = `${item.name || "remodel"}-${gen.gid.slice(0, 8)}.png`;
    download.textContent = "下载";
    download.setAttribute("role", "menuitem");
    download.addEventListener("click", () => {
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
    });
    menu.append(download);
    for (const [action, label, danger] of [
      ["compare", "对比原图", false],
      ["cover", "设为封面", false],
      ["note", "备注", false],
      ["delete", "删除本版", true]
    ]) {
      const entry = el(
        "button",
        `rm-menu-item${danger ? " danger" : ""}`,
        label
      );
      entry.type = "button";
      entry.setAttribute("role", "menuitem");
      entry.dataset.action = action;
      menu.append(entry);
    }

    button.addEventListener("click", (event) => {
      event.stopPropagation();
      closeMenus(menu);
      menu.hidden = !menu.hidden;
      button.setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) {
        const first = menu.querySelector("button, a");
        if (first) first.focus();
      }
    });
    menu.addEventListener("click", (event) => {
      const target =
        event.target instanceof Element
          ? event.target.closest("[data-action]")
          : null;
      menu.hidden = true;
      button.setAttribute("aria-expanded", "false");
      if (!target) return;
      event.stopPropagation();
      const action = target.dataset.action;
      if (action === "compare") openCompare(gen);
      else if (action === "cover") void setCover(gen.gid);
      else if (action === "note") openNoteDialog(gen);
      else if (action === "delete") openGenDeleteDialog(gen);
    });
    wrap.append(button, menu);
    return wrap;
  }

  function renderFailedJobCard(item, job) {
    const card = el("article", "rm-gen-card is-error");
    card.dataset.jobId = job.job_id || "";
    card.dataset.mode = job.mode === "text" ? "text" : "img2img";
    const fullError = String(job.error || "未知错误").trim() || "未知错误";
    card.title = fullError;
    card.setAttribute("aria-label", `生成失败：${fullError}`);

    const thumb = el("div", "rm-gen-thumb rm-gen-thumb-error");
    thumb.title = fullError;
    thumb.append(el("span", "rm-error-label", "生成失败"));
    card.append(thumb);

    const info = el("div", "rm-gen-info");
    const top = el("div", "rm-gen-top");
    top.append(modeBadge(job.mode));
    if (job.style) top.append(el("span", "rm-tag", job.style));
    info.append(top);
    info.append(el("p", "rm-gen-time", formatTime(job.created_at)));
    const reason = el("p", "rm-gen-error", compactFailureReason(fullError));
    reason.title = fullError;
    reason.setAttribute("aria-label", fullError);
    info.append(reason);
    const actions = el("div", "rm-gen-actions");
    if (isTimeoutError(fullError)) {
      const retry = el("button", "rm-gen-action", "改用纯文本重试");
      retry.type = "button";
      retry.addEventListener("click", () => void retryFailedInText(job));
      actions.append(retry);
    }
    if (actions.childElementCount) info.append(actions);
    card.append(info);
    return card;
  }

  function compactFailureReason(message) {
    const text = String(message || "").trim() || "未知错误";
    if (/排队超时|超时|timeout/i.test(text)) {
      const seconds = text.match(/(?:已等待\s*)?(\d+(?:\.\d+)?)\s*(?:秒|s\b)/i);
      const label = /排队超时/.test(text) ? "排队超时" : "超时";
      return seconds
        ? `${label} · ${Math.round(Number(seconds[1]))} 秒`
        : label;
    }
    return text.length > 36 ? `${text.slice(0, 34)}…` : text;
  }

  function isTimeoutError(message) {
    return /排队超时|超时|timeout/i.test(String(message || ""));
  }

  function modeBadge(mode) {
    const value = mode === "text" ? "text" : "img2img";
    const badge = el("span", "rm-mode-badge", value === "text" ? "纯文本" : "图生图");
    badge.dataset.mode = value;
    return badge;
  }

  function appendMeta(container, text) {
    if (!text) return;
    container.append(el("span", "", text));
  }

  function designSummary(gen) {
    const lighting = gen && gen.lighting_summary && typeof gen.lighting_summary === "object"
      ? String(gen.lighting_summary.time_zh || gen.lighting_summary.time_en || "").trim()
      : "";
    const decor = gen && gen.decor_summary && typeof gen.decor_summary === "object"
      ? Number(gen.decor_summary.item_count) || 0
      : 0;
    return {
      lighting: lighting ? `灯光设计 ${lighting}` : "",
      decor: decor > 0 ? `软装层次 ${decor} 件` : ""
    };
  }

  // ---------- detail controls ----------

  function bindListControls() {
    for (const button of [els.newItem, els.emptyNew]) {
      if (button) button.addEventListener("click", openCreateDialog);
    }
    if (els.loadMore) {
      els.loadMore.addEventListener("click", () => void loadMoreItems());
    }
    els.itemGrid.addEventListener("click", (event) => {
      if (event.target instanceof Element && event.target.closest(".rm-item-menu-wrap")) return;
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMenus();
        const active = document.activeElement;
        if (active && active.closest && active.closest(".rm-item-menu-wrap")) {
          const button = active.closest(".rm-item-menu-wrap").querySelector(".rm-icon-button");
          if (button) button.focus();
        }
      }
    });
  }

  function bindDetailControls() {
    els.back.addEventListener("click", (event) => {
      event.preventDefault();
      navigateToList();
    });
    if (els.refreshItem) {
      els.refreshItem.addEventListener("click", () => {
        if (state.detailItemId) void refreshDetail(state.detailItemId);
      });
    }
    els.detailMode.addEventListener("click", (event) => {
      const target = event.target instanceof Element ? event.target.closest("[data-item-mode]") : null;
      if (!target) return;
      state.mode = target.dataset.itemMode === "text" ? "text" : "img2img";
      renderParams(state.current, false);
      if (state.current) renderAnalysis(state.current);
    });
    els.styleSelect.addEventListener("change", () => {
      state.style = els.styleSelect.value;
    });
    if (els.detailEnabled) {
      els.detailEnabled.addEventListener("change", () => syncDetailControls());
    }
    if (els.detailInput) {
      els.detailInput.addEventListener("input", updateDetailCount);
    }
    if (els.genFilters) {
      els.genFilters.addEventListener("click", (event) => {
        const target =
          event.target instanceof Element
            ? event.target.closest("[data-gen-filter]")
            : null;
        if (!target) return;
        state.historyFilter = target.dataset.genFilter || "all";
        if (state.current) renderGens(state.current);
      });
    }
    els.generate.addEventListener("click", () => void generateCurrent());
    els.reanalyze.addEventListener("click", () => void reanalyzeCurrent());
    els.replaceSource.addEventListener("click", () => els.sourceFile.click());
    els.sourceFile.addEventListener("change", () => {
      const file = els.sourceFile.files && els.sourceFile.files[0];
      els.sourceFile.value = "";
      if (file) void replaceSource(file);
    });
    els.renameItem.addEventListener("click", () => openRenameDialog(state.current));
    els.deleteItem.addEventListener("click", () => openDeleteDialog(state.current));
    els.currentCompare.addEventListener("click", () => {
      const gens = state.current && Array.isArray(state.current.gens) ? state.current.gens : [];
      const gen = gens.find((entry) => entry.gid === els.currentCompare.dataset.gid) || gens[0];
      if (gen) openCompare(gen);
    });
    if (els.currentDetail) {
      els.currentDetail.addEventListener("click", toggleDetailOptimize);
    }
    if (els.currentPrompt) {
      els.currentPrompt.addEventListener("toggle", () => {
        if (!els.promptToggleText) return;
        els.promptToggleText.textContent = els.currentPrompt.open ? "收起提示词" : "查看提示词";
      });
    }
    if (els.promptCopy) {
      els.promptCopy.addEventListener("click", () => void copyCurrentPrompt());
    }
    if (els.detailOptimizeSubmit) {
      els.detailOptimizeSubmit.addEventListener("click", () => {
        void optimizeCurrentDetail();
      });
    }
    if (els.detailOptimizeCancel) {
      els.detailOptimizeCancel.addEventListener("click", () => {
        resetDetailOptimize();
      });
    }
    if (els.detailOptimizeInput) {
      els.detailOptimizeInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
          event.preventDefault();
          void optimizeCurrentDetail();
        }
      });
    }
    els.genGrid.addEventListener("click", (event) => {
      // Actions are bound directly; this only prevents card-level surprises.
      if (event.target instanceof Element && event.target.closest("button, a")) return;
    });
  }

  async function generateCurrent(overrides = {}) {
    const item = state.current;
    if (!item || state.generating) return;
    const mode = MODES.has(overrides.mode) ? overrides.mode : state.mode;
    const style = String(overrides.style || state.style || "");
    if (!style) {
      notify("请先选择风格", "error", 5000);
      return;
    }
    const detailEnabled = Boolean(
      els.detailEnabled && els.detailEnabled.checked
    );
    const detailZh = detailEnabled
      ? String(els.detailInput.value || "").trim()
      : "";
    state.generating = true;
    els.generate.disabled = true;
    els.replaceSource.disabled = true;
    showProgress("正在提交生成任务…");
    try {
      const result = await apiJson(`/api/remodel/items/${encodeURIComponent(item.item_id)}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          mode,
          style,
          prompt_override: String(els.promptOverride.value || "").trim(),
          detail_enabled: detailEnabled,
          detail_zh: detailZh,
          note: String(els.genNote.value || "").trim()
        })
      });
      state.job = {
        id: result.job_id,
        startedAt: Date.now(),
        pollErrorShown: false
      };
      showProgress("排队中 · 00:00");
      startPolling(result.job_id);
    } catch (error) {
      state.generating = false;
      els.generate.disabled = false;
      els.replaceSource.disabled = false;
      hideProgress();
      notify(`生成失败：${error.message}`, "error", 7000);
    }
  }

  function toggleDetailOptimize() {
    if (!els.detailOptimizePanel || !els.currentDetail) return;
    if (state.generating) return;
    const gens =
      state.current && Array.isArray(state.current.gens)
        ? state.current.gens
        : [];
    if (!gens.length) return;
    state.detailOptimizeOpen = !state.detailOptimizeOpen;
    els.detailOptimizePanel.hidden = !state.detailOptimizeOpen;
    if (state.detailOptimizeOpen) {
      els.detailOptimizeInput.focus();
    }
  }

  function resetDetailOptimize() {
    state.detailOptimizeOpen = false;
    if (els.detailOptimizePanel) els.detailOptimizePanel.hidden = true;
    if (els.detailOptimizeInput) els.detailOptimizeInput.value = "";
    if (els.detailOptimizeSubmit) els.detailOptimizeSubmit.disabled = false;
    hideOptimizeProgress();
  }

  function setDetailOptimizeBusy(busy, hasResult) {
    if (els.currentDetail) {
      els.currentDetail.disabled = Boolean(busy) || !hasResult;
      els.currentDetail.textContent = busy ? "优化中…" : "细节优化";
      els.currentDetail.title = hasResult ? "" : "先生成一次改造结果";
      els.currentDetail.setAttribute("aria-busy", String(Boolean(busy)));
    }
    if (els.detailOptimizeSubmit) els.detailOptimizeSubmit.disabled = Boolean(busy);
    if (els.detailOptimizeInput) els.detailOptimizeInput.disabled = Boolean(busy);
    if (els.detailOptimizeCancel) els.detailOptimizeCancel.disabled = Boolean(busy);
  }

  function showOptimizeProgress(text) {
    if (!els.currentProgress) return;
    els.currentProgress.hidden = false;
    if (els.currentProgressText) els.currentProgressText.textContent = text || "";
  }

  function hideOptimizeProgress() {
    if (!els.currentProgress) return;
    els.currentProgress.hidden = true;
    if (els.currentProgressText) els.currentProgressText.textContent = "";
  }

  async function optimizeCurrentDetail() {
    const item = state.current;
    if (!item || state.generating) return;
    const gens = Array.isArray(item.gens) ? item.gens : [];
    if (!gens.length) {
      notify("先生成一次改造结果", "error", 5000);
      return;
    }
    const detailZh = String(
      (els.detailOptimizeInput && els.detailOptimizeInput.value) || ""
    ).trim();
    state.generating = true;
    setDetailOptimizeBusy(true, true);
    els.generate.disabled = true;
    els.replaceSource.disabled = true;
    showOptimizeProgress("正在提交细节优化…");
    try {
      const result = await apiJson(
        `/api/remodel/items/${encodeURIComponent(item.item_id)}/detail-optimize`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ detail_zh: detailZh })
        }
      );
      state.job = {
        id: result.job_id,
        startedAt: Date.now(),
        pollErrorShown: false,
        optimize: true
      };
      showOptimizeProgress("排队中 · 00:00");
      startPolling(result.job_id);
    } catch (error) {
      state.generating = false;
      setDetailOptimizeBusy(false, true);
      els.generate.disabled = false;
      els.replaceSource.disabled = false;
      hideOptimizeProgress();
      notify(`细节优化失败：${error.message}`, "error", 7000);
    }
  }

  async function retryFailedInText(job) {
    if (!state.current || state.generating) return;
    const style = String((job && job.style) || state.style || "").trim();
    if (!style) {
      notify("这个失败任务没有可复用的风格", "error", 5000);
      return;
    }
    state.mode = "text";
    state.style = style;
    renderParams(state.current, false);
    await generateCurrent({ mode: "text", style });
  }

  function startPolling(jobId) {
    stopPolling();
    if (!jobId || document.hidden) return;
    state.job = state.job && state.job.id === jobId
      ? state.job
      : {
          id: jobId,
          startedAt: Date.now(),
          pollErrorShown: false,
          restored: true
        };
    state.pollTimer = window.setInterval(() => void pollJob(), 1000);
    void pollJob();
  }

  function stopPolling() {
    if (state.pollTimer) {
      window.clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
    state.pollBusy = false;
  }

  async function pollJob() {
    if (!state.job || !state.job.id || state.pollBusy || document.hidden) return;
    const jobId = state.job.id;
    state.pollBusy = true;
    try {
      const snapshot = await apiJson(
        freshUrl(`/api/remodel/jobs/${encodeURIComponent(jobId)}`)
      );
      if (!state.job || state.job.id !== jobId) return;
      if (snapshot.status === "queued" || snapshot.status === "running") {
        const elapsed = Math.max(0, snapshot.seconds || (Date.now() - state.job.startedAt) / 1000);
        const labels = {
          queued: "排队中",
          encoding: "准备中",
          analyzing: "分析原图",
          requesting: "请求模型",
          downloading: "下载结果",
          saving: "保存图片",
          checking: "结构自查",
          repairing: "修复重试"
        };
        if (
          state.job.restored &&
          !state.job.detailsApplied &&
          snapshot.base !== "current" &&
          "detail_enabled" in snapshot
        ) {
          state.job.detailsApplied = true;
          setDetailFromJob(snapshot);
        }
        if (snapshot.base === "current") {
          state.job.optimize = true;
          setDetailOptimizeBusy(true, true);
        }
        const label = labels[snapshot.stage] || (snapshot.status === "queued" ? "排队中" : "生成中");
        const hint =
          snapshot.mode === "text"
            ? elapsed > 45
              ? "纯文本模式通常约 80 秒"
              : ""
            : elapsed > 60
              ? "模型排队中，图生图通常 3-6 分钟，最长约 15 分钟"
              : "";
        if (state.job.optimize) {
          showOptimizeProgress(`${label} · ${formatClockDuration(elapsed)}`);
        } else {
          showProgress(`${label} · ${formatClockDuration(elapsed)}`, hint);
        }
        return;
      }
      const finishedJob = state.job || {};
      const wasOptimize = Boolean(finishedJob.optimize);
      stopPolling();
      state.generating = false;
      state.job = null;
      if (wasOptimize) hideOptimizeProgress();
      else hideProgress();
      setDetailOptimizeBusy(false, true);
      els.generate.disabled = false;
      els.replaceSource.disabled = false;
      if (snapshot.status === "done") {
        if (wasOptimize) {
          notify("细节优化完成", "success", 5200);
          resetDetailOptimize();
        } else {
          notify(`生成完成 · 用时 ${formatSeconds(snapshot.seconds)}`, "success", 5200);
          if (els.genNote) els.genNote.value = "";
          resetDetailControls();
        }
        await reloadCurrent();
        highlightLatestResult();
      } else {
        const message = snapshot.error || "未知错误";
        if (wasOptimize) {
          notify(`细节优化失败：${compactFailureReason(message)}`, "error", 7000);
        } else {
          notify(`生成失败：${compactFailureReason(message)}`, "error", 7000);
        }
        await reloadCurrent();
      }
    } catch (error) {
      if (state.job && !state.job.pollErrorShown) {
        state.job.pollErrorShown = true;
        notify(`读取生成状态失败，正在重试：${error.message}`, "error", 5000);
      }
    } finally {
      state.pollBusy = false;
    }
  }

  function highlightLatestResult() {
    const card =
      els.currentCard && !els.currentCard.hidden
        ? els.currentCard
        : els.genGrid.querySelector(".rm-gen-card:not(.is-error)");
    if (!card) return;
    card.classList.add("is-new");
    window.setTimeout(() => card.classList.remove("is-new"), 2000);
    // Only pull the page when the user is still near the top; far-away
    // readers get the toast + highlight without losing their scroll spot.
    const view = els.view;
    if (!view || view.scrollTop <= 160) {
      card.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }

  async function reloadCurrent() {
    const itemId = state.detailItemId;
    if (!itemId) return;
    try {
      const item = await apiJson(
        freshUrl(`/api/remodel/items/${encodeURIComponent(itemId)}`)
      );
      if (state.detailItemId !== itemId) return;
      applyItem(item);
    } catch (error) {
      if (error.status === 404) navigateToList();
      else notify(`刷新项目失败：${error.message}`, "error", 5000);
    }
  }

  async function reanalyzeCurrent() {
    const item = state.current;
    if (!item || state.analyzing) return;
    state.analyzing = true;
    renderAnalysis(item);
    const hasStructure = Boolean(
      String((item.structure || {}).structure_en || "").trim()
    );
    const progressText = hasStructure ? "正在重新分析结构…" : "正在分析结构…";
    els.sourceStatus.textContent = progressText;
    try {
      const updated = await apiJson(`/api/remodel/items/${encodeURIComponent(item.item_id)}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: true })
      });
      if (state.detailItemId === item.item_id) applyItem(updated);
      notify("结构分析已更新", "success", 4000);
    } catch (error) {
      notify(`结构分析失败：${error.message}`, "error", 7000);
      await reloadCurrent();
    } finally {
      state.analyzing = false;
      if (state.current) renderAnalysis(state.current);
      if (els.sourceStatus.textContent === progressText) els.sourceStatus.textContent = "";
    }
  }

  async function replaceSource(file) {
    if (!validateFile(file)) return;
    const item = state.current;
    if (!item || state.uploading) return;
    state.uploading = true;
    els.replaceSource.disabled = true;
    els.sourceStatus.textContent = "上传中…";
    const form = new FormData();
    form.append("photo", file, file.name || "photo");
    try {
      const updated = await apiJson(`/api/remodel/items/${encodeURIComponent(item.item_id)}/source`, {
        method: "POST",
        body: form
      });
      if (state.detailItemId === item.item_id) applyItem(updated);
      els.sourceStatus.textContent = "原图已替换，旧的结构分析已清除";
      notify(
        "已替换原图，旧分析已清除；图生图可直接生成，纯文本生成时会先分析",
        "success",
        6000
      );
    } catch (error) {
      els.sourceStatus.textContent = "";
      notify(`替换原图失败：${error.message}`, "error", 7000);
    } finally {
      state.uploading = false;
      els.replaceSource.disabled = false;
      if (els.sourceStatus.textContent === "上传中…") els.sourceStatus.textContent = "";
    }
  }

  // ---------- generation actions ----------

  async function setCover(gid) {
    const item = state.current;
    if (!item) return;
    try {
      const updated = await apiJson(`/api/remodel/items/${encodeURIComponent(item.item_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cover_gen: gid })
      });
      applyItem(updated);
      notify("已设为封面", "success", 3500);
    } catch (error) {
      notify(`设置封面失败：${error.message}`, "error", 6000);
    }
  }

  async function deleteGeneration(gid) {
    const item = state.current;
    if (!item) return;
    try {
      await apiJson(
        `/api/remodel/items/${encodeURIComponent(item.item_id)}/gens/${encodeURIComponent(gid)}`,
        { method: "DELETE" }
      );
      closeDialog(els.dialogGenDelete);
      notify("已删除该版本，其他版本不受影响", "success", 4000);
      await reloadCurrent();
    } catch (error) {
      notify(`删除版本失败：${error.message}`, "error", 6000);
    }
  }

  async function saveNote(gid) {
    const item = state.current;
    if (!item) return;
    const note = String(els.noteInput.value || "");
    try {
      await apiJson(
        `/api/remodel/items/${encodeURIComponent(item.item_id)}/gens/${encodeURIComponent(gid)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note })
        }
      );
      closeDialog(els.dialogNote);
      notify("备注已保存", "success", 3500);
      await reloadCurrent();
    } catch (error) {
      els.noteError.textContent = error.message;
      els.noteError.hidden = false;
    }
  }

  // ---------- create / rename / delete ----------

  function openCreateDialog() {
    if (!els.dialogCreate) return;
    clearCreateFile();
    els.createName.value = "";
    els.createError.hidden = true;
    els.createError.textContent = "";
    els.createSubmit.disabled = true;
    showDialog(els.dialogCreate);
    els.createName.focus();
  }

  async function createItem() {
    const file =
      state.createFile || (els.createFile.files && els.createFile.files[0]);
    const invalid = createFileError(file);
    if (invalid) {
      els.createError.textContent = invalid;
      els.createError.hidden = false;
      els.createSubmit.disabled = true;
      return;
    }
    els.createSubmit.disabled = true;
    els.createSubmit.textContent = "上传中 0%";
    const form = new FormData();
    form.append("photo", file, file.name || "photo");
    const name = String(els.createName.value || "").trim();
    if (name) form.append("name", name);
    try {
      const item = await uploadFormWithProgress(
        "/api/remodel/items",
        form,
        (loaded, total) => {
          const pct = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
          els.createSubmit.textContent =
            pct >= 100 ? "创建中…" : `上传中 ${pct}%`;
        }
      );
      clearCreateFile();
      closeDialog(els.dialogCreate);
      notify("项目已创建，可直接开始生成", "success", 5000);
      navigateToList();
      window.location.hash = ROUTE_PREFIX + encodeURIComponent(item.item_id);
    } catch (error) {
      els.createError.textContent = error.message;
      els.createError.hidden = false;
    } finally {
      els.createSubmit.disabled = !state.createFile;
      els.createSubmit.textContent = "创建项目";
    }
  }

  function openRenameDialog(item) {
    if (!item || !els.dialogRename) return;
    state.pendingRenameItem = item;
    els.renameInput.value = item.name || "";
    els.renameError.hidden = true;
    showDialog(els.dialogRename);
    els.renameInput.focus();
    els.renameInput.select();
  }

  async function renameItem() {
    const item = state.pendingRenameItem || state.current;
    if (!item) return;
    const name = String(els.renameInput.value || "").trim();
    if (!name) {
      els.renameError.textContent = "项目名不能为空";
      els.renameError.hidden = false;
      return;
    }
    els.renameSubmit.disabled = true;
    try {
      const updated = await apiJson(`/api/remodel/items/${encodeURIComponent(item.item_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name })
      });
      closeDialog(els.dialogRename);
      if (state.detailItemId === updated.item_id) applyItem(updated);
      else if (state.items.some((entry) => entry.item_id === updated.item_id)) {
        state.items = state.items.map((entry) =>
          entry.item_id === updated.item_id ? { ...entry, name: updated.name } : entry
        );
        renderList();
      }
      notify("项目已重命名", "success", 3500);
    } catch (error) {
      els.renameError.textContent = error.message;
      els.renameError.hidden = false;
    } finally {
      els.renameSubmit.disabled = false;
    }
  }

  function openDeleteDialog(item) {
    if (!item || !els.dialogDelete) return;
    state.pendingDeleteItemId = item.item_id;
    showDialog(els.dialogDelete);
    els.deleteSoft.focus();
  }

  async function deleteItem(purge) {
    const itemId = state.pendingDeleteItemId || (state.current && state.current.item_id);
    if (!itemId) return;
    const suffix = purge ? "?purge=1" : "";
    try {
      await apiJson(`/api/remodel/items/${encodeURIComponent(itemId)}${suffix}`, {
        method: "DELETE"
      });
      closeDialog(els.dialogDelete);
      closeDialog(els.dialogPurge);
      state.pendingDeleteItemId = "";
      notify(purge ? "项目已永久删除" : "项目已移入回收站", "success", 4500);
      if (state.detailItemId === itemId) navigateToList();
      else await loadItems();
    } catch (error) {
      notify(`删除项目失败：${error.message}`, "error", 7000);
    }
  }

  function openPurgeDialog() {
    const item =
      state.items.find((entry) => entry.item_id === state.pendingDeleteItemId) ||
      (state.current && state.current.item_id === state.pendingDeleteItemId
        ? state.current
        : null);
    if (!item) return;
    closeDialog(els.dialogDelete);
    els.purgeHint.textContent = `项目：${item.name || item.item_id}`;
    els.purgeInput.value = "";
    els.purgeError.hidden = true;
    els.purgeSubmit.disabled = true;
    els.purgeSubmit.dataset.expected = item.name || item.item_id;
    showDialog(els.dialogPurge);
    els.purgeInput.focus();
  }

  function openGenDeleteDialog(gen) {
    if (!gen) return;
    state.pendingDeleteGid = gen.gid;
    showDialog(els.dialogGenDelete);
    els.genDeleteSubmit.focus();
  }

  function openNoteDialog(gen) {
    if (!gen) return;
    state.pendingNoteGid = gen.gid;
    els.noteInput.value = gen.note || "";
    els.noteError.hidden = true;
    showDialog(els.dialogNote);
    els.noteInput.focus();
  }

  function openCompare(gen) {
    const item = state.current;
    if (!item || !gen) return;
    state.pendingCompareGid = gen.gid;
    const source =
      item.source && typeof item.source === "object" && item.source.current
        ? item.source
        : null;
    els.compareAfter.src = gen.url;
    els.compareAfter.alt = `${gen.style || "改造"}版本结果`;
    setImageSize(els.compareAfter, gen.width, gen.height);
    applyAssetFrame(els.compareAfterFrame, gen.width, gen.height, null);
    if (item.source_url) {
      els.compareBefore.src = source && source.url ? source.url : item.source_url;
      setImageSize(
        els.compareBefore,
        source && source.width,
        source && source.height
      );
      applyAssetFrame(
        els.compareBeforeFrame,
        source && source.width,
        source && source.height,
        null
      );
    } else {
      els.compareBefore.removeAttribute("src");
      els.compareBefore.removeAttribute("width");
      els.compareBefore.removeAttribute("height");
      applyAssetFrame(els.compareBeforeFrame, null, null, null);
    }
    els.compareBefore.alt = `${item.name || "改造项目"}的原图`;
    els.compareDownload.href = gen.url;
    els.compareDownload.download = `${item.name || "remodel"}-${gen.gid.slice(0, 8)}.png`;
    showDialog(els.dialogCompare);
  }

  // ---------- dialogs ----------

  function bindDialogs() {
    for (const button of document.querySelectorAll("[data-close-dialog]")) {
      button.addEventListener("click", () => {
        const dialog = button.closest("dialog");
        if (dialog) closeDialog(dialog);
      });
    }
    els.createFile.addEventListener("change", () => {
      const file = els.createFile.files && els.createFile.files[0];
      if (!file) {
        clearCreateFile();
        return;
      }
      applyCreateFile(file);
    });
    els.createSubmit.addEventListener("click", () => void createItem());
    els.createDrop.addEventListener("click", (event) => {
      if (event.target === els.createFile) return;
      if (event.target instanceof Element && event.target.closest("button")) return;
      els.createFile.click();
    });
    els.createReselect.addEventListener("click", (event) => {
      event.stopPropagation();
      els.createFile.click();
    });
    els.createRemove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeCreateFile();
    });
    els.createDrop.addEventListener("keydown", (event) => {
      if (event.target !== els.createDrop) return;
      if (event.key === "Enter" || event.key === " " || event.key === "Spacebar") {
        event.preventDefault();
        els.createFile.click();
      }
    });
    for (const type of ["dragenter", "dragover"]) {
      els.createDrop.addEventListener(type, (event) => {
        event.preventDefault();
        if (event.dataTransfer) event.dataTransfer.dropEffect = "copy";
        els.createDrop.classList.add("is-dragover");
      });
    }
    els.createDrop.addEventListener("dragleave", (event) => {
      if (!event.relatedTarget || !els.createDrop.contains(event.relatedTarget)) {
        els.createDrop.classList.remove("is-dragover");
      }
    });
    els.createDrop.addEventListener("drop", (event) => {
      event.preventDefault();
      els.createDrop.classList.remove("is-dragover");
      const file =
        event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) applyCreateFile(file);
    });
    els.dialogCreate.addEventListener("close", () => {
      clearCreateFile();
      els.createError.hidden = true;
      els.createError.textContent = "";
    });
    els.renameSubmit.addEventListener("click", () => void renameItem());
    els.renameInput.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        void renameItem();
      }
    });
    els.deleteSoft.addEventListener("click", () => void deleteItem(false));
    els.deletePurgeOpen.addEventListener("click", openPurgeDialog);
    els.purgeInput.addEventListener("input", () => {
      els.purgeSubmit.disabled = els.purgeInput.value !== els.purgeSubmit.dataset.expected;
    });
    els.purgeSubmit.addEventListener("click", () => {
      if (!els.purgeSubmit.disabled) void deleteItem(true);
    });
    els.genDeleteSubmit.addEventListener("click", () => {
      if (state.pendingDeleteGid) void deleteGeneration(state.pendingDeleteGid);
    });
    els.noteSubmit.addEventListener("click", () => {
      if (state.pendingNoteGid) void saveNote(state.pendingNoteGid);
    });
  }

  function showDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") {
      if (!dialog.open) dialog.showModal();
    } else {
      dialog.setAttribute("open", "");
    }
  }

  function closeDialog(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
  }

  // ---------- detail image lightbox ----------

  const lightbox = {
    root: null,
    dialog: null,
    stage: null,
    image: null,
    caption: null,
    download: null,
    closeBtn: null,
    prevBtn: null,
    nextBtn: null,
    items: [],
    index: 0,
    mode: "fit",
    lastFocus: null,
    drag: null,
    suppressClickUntil: 0,
    keydownBound: false
  };

  function initLightbox() {
    if (lightbox.root) return;
    const root = el("div", "rm-lightbox");
    root.hidden = true;
    root.dataset.mode = "fit";
    root.dataset.index = "0";

    const scrim = el("div", "rm-lightbox-scrim");
    scrim.setAttribute("aria-hidden", "true");

    const dialog = el("section", "rm-lightbox-dialog");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("aria-modal", "true");
    dialog.setAttribute("aria-label", "图片预览");

    const closeBtn = el("button", "rm-lightbox-close", "×");
    closeBtn.type = "button";
    closeBtn.setAttribute("aria-label", "关闭预览");

    const prevBtn = el("button", "rm-lightbox-nav prev", "‹");
    prevBtn.type = "button";
    prevBtn.setAttribute("aria-label", "上一张");

    const nextBtn = el("button", "rm-lightbox-nav next", "›");
    nextBtn.type = "button";
    nextBtn.setAttribute("aria-label", "下一张");

    const stage = el("div", "rm-lightbox-stage");
    const image = document.createElement("img");
    image.className = "rm-lightbox-image";
    image.alt = "";
    image.tabIndex = 0;
    image.setAttribute("role", "button");
    image.setAttribute("aria-label", "切换为原始尺寸");
    stage.append(image);

    const bar = el("div", "rm-lightbox-bar");
    const caption = el("p", "rm-lightbox-caption");
    caption.setAttribute("aria-live", "polite");
    const download = document.createElement("a");
    download.className = "rm-lightbox-download rm-button secondary";
    download.textContent = "下载图片";
    download.download = "";
    bar.append(caption, download);

    dialog.append(closeBtn, prevBtn, stage, nextBtn, bar);
    root.append(scrim, dialog);
    document.body.append(root);

    lightbox.root = root;
    lightbox.dialog = dialog;
    lightbox.stage = stage;
    lightbox.image = image;
    lightbox.caption = caption;
    lightbox.download = download;
    lightbox.closeBtn = closeBtn;
    lightbox.prevBtn = prevBtn;
    lightbox.nextBtn = nextBtn;

    scrim.addEventListener("click", () => closeLightbox());
    closeBtn.addEventListener("click", () => closeLightbox());
    prevBtn.addEventListener("click", () => navigateLightbox(-1));
    nextBtn.addEventListener("click", () => navigateLightbox(1));
    image.addEventListener("click", () => {
      if (Date.now() < lightbox.suppressClickUntil) return;
      toggleLightboxZoom();
    });
    image.addEventListener("keydown", (event) => {
      if (isActivationKey(event)) {
        event.preventDefault();
        toggleLightboxZoom();
      }
    });
    image.addEventListener("pointerdown", startLightboxPan);

    els.sourceImg.addEventListener("click", openLightboxForSource);
    els.sourceImg.addEventListener("keydown", (event) =>
      activateOnKey(event, openLightboxForSource)
    );
    els.currentImg.addEventListener("click", openLightboxForLatest);
    els.currentImg.addEventListener("keydown", (event) =>
      activateOnKey(event, openLightboxForLatest)
    );
  }

  function isActivationKey(event) {
    return event.key === "Enter" || event.key === " " || event.key === "Spacebar";
  }

  function activateOnKey(event, action) {
    if (!isActivationKey(event)) return;
    event.preventDefault();
    action();
  }

  function setZoomable(image, label, enabled = true) {
    if (!image) return;
    if (enabled) {
      image.setAttribute("role", "button");
      image.tabIndex = 0;
      image.setAttribute("aria-label", label || "点击放大预览");
      image.classList.add("rm-zoomable");
    } else {
      image.removeAttribute("role");
      image.removeAttribute("tabindex");
      image.removeAttribute("aria-label");
      image.classList.remove("rm-zoomable");
    }
  }

  function lightboxItemsFor(item) {
    if (!item) return [];
    const gens = Array.isArray(item.gens) ? item.gens : [];
    const items = gens
      .map((gen, index) => ({
        url: gen && gen.url ? gen.url : "",
        width: gen && gen.width,
        height: gen && gen.height,
        label: index === 0 ? "最新版本" : "历史版本",
        style: (gen && gen.style) || "",
        mode: (gen && gen.mode) || "",
        date: (gen && gen.created_at) || "",
        alt: `${(gen && gen.style) || "改造"}版本`,
        download: `${item.name || "remodel"}-${String((gen && gen.gid) || "").slice(0, 8)}.png`
      }))
      .filter((entry) => entry.url);
    if (item.source_url) {
      const source =
        item.source && typeof item.source === "object" && item.source.current
          ? item.source
          : Array.isArray(item.sources)
            ? item.sources.find((entry) => entry && entry.current)
            : null;
      items.push({
        url: source && source.url ? source.url : item.source_url,
        width: source && source.width,
        height: source && source.height,
        label: "原图",
        style: "",
        mode: "",
        date: item.created_at || "",
        alt: `${item.name || "改造项目"}的当前原图`,
        download: `${item.name || "remodel"}-source.png`
      });
    }
    return items;
  }

  function openLightboxForSource() {
    const item = state.current;
    if (!item || els.sourceImg.hidden || !els.sourceImg.getAttribute("src")) return;
    const items = lightboxItemsFor(item);
    const index = items.findIndex((entry) => entry.label === "原图");
    if (index >= 0) openLightbox(items, index, els.sourceImg);
  }

  function openLightboxForLatest() {
    const item = state.current;
    if (!item || els.currentCard.hidden || !els.currentImg.getAttribute("src")) return;
    const items = lightboxItemsFor(item);
    if (items.length) openLightbox(items, 0, els.currentImg);
  }

  function openLightboxAtGen(gid, trigger) {
    const item = state.current;
    if (!item) return;
    const gens = Array.isArray(item.gens) ? item.gens : [];
    const index = gens.findIndex((gen) => gen && gen.gid === gid);
    const items = lightboxItemsFor(item);
    if (index < 0 || !items.length) return;
    openLightbox(items, index, trigger || null);
  }

  function openLightbox(items, index, trigger) {
    if (!items || !items.length) return;
    lightbox.items = items;
    lightbox.lastFocus =
      trigger instanceof HTMLElement
        ? trigger
        : document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
    lightbox.root.hidden = false;
    document.body.classList.add("rm-lightbox-open");
    if (!lightbox.keydownBound) {
      document.addEventListener("keydown", onLightboxKeydown, true);
      lightbox.keydownBound = true;
    }
    showLightboxIndex(index);
    lightbox.closeBtn.focus();
  }

  function showLightboxIndex(index) {
    const count = lightbox.items.length;
    if (!count) return;
    const next = ((Number(index) || 0) % count + count) % count;
    lightbox.index = next;
    const item = lightbox.items[next];
    lightbox.root.dataset.index = String(next);
    lightbox.image.src = item.url;
    lightbox.image.alt = item.alt || "";
    lightbox.download.href = item.url;
    lightbox.download.download = item.download || "";
    lightbox.caption.textContent = lightboxCaption(item);
    setLightboxMode("fit");
  }

  function lightboxCaption(item) {
    return [
      item.label || "",
      item.style || "",
      item.mode ? modeLabel(item.mode) : "",
      item.date ? formatTime(item.date) : "",
      formatAssetSize(item.width, item.height)
    ]
      .filter(Boolean)
      .join(" · ");
  }

  function setLightboxMode(mode) {
    const next = mode === "actual" ? "actual" : "fit";
    lightbox.mode = next;
    lightbox.root.dataset.mode = next;
    lightbox.stage.scrollLeft = 0;
    lightbox.stage.scrollTop = 0;
    if (next === "actual") {
      const item = lightbox.items[lightbox.index] || {};
      const width = lightbox.image.naturalWidth || Number(item.width) || 0;
      const height = lightbox.image.naturalHeight || Number(item.height) || 0;
      if (width && height) {
        lightbox.image.style.width = `${width}px`;
        lightbox.image.style.height = `${height}px`;
      }
      lightbox.image.style.maxWidth = "none";
      lightbox.image.style.maxHeight = "none";
      lightbox.image.setAttribute("aria-label", "切换为适应屏幕");
    } else {
      lightbox.image.style.width = "";
      lightbox.image.style.height = "";
      lightbox.image.style.maxWidth = "";
      lightbox.image.style.maxHeight = "";
      lightbox.image.setAttribute("aria-label", "切换为原始尺寸");
    }
  }

  function toggleLightboxZoom() {
    if (!lightbox.root || lightbox.root.hidden) return;
    const item = lightbox.items[lightbox.index] || {};
    const width = lightbox.image.naturalWidth || Number(item.width) || 0;
    const height = lightbox.image.naturalHeight || Number(item.height) || 0;
    if (!width || !height) return;
    setLightboxMode(lightbox.mode === "fit" ? "actual" : "fit");
  }

  function navigateLightbox(delta) {
    if (!lightbox.items.length) return;
    showLightboxIndex(lightbox.index + delta);
  }

  function closeLightbox(restoreFocus = true) {
    if (!lightbox.root || lightbox.root.hidden) return;
    lightbox.root.hidden = true;
    lightbox.root.classList.remove("is-panning");
    document.body.classList.remove("rm-lightbox-open");
    lightbox.drag = null;
    detachLightboxPan();
    setLightboxMode("fit");
    if (lightbox.keydownBound) {
      document.removeEventListener("keydown", onLightboxKeydown, true);
      lightbox.keydownBound = false;
    }
    const focus = lightbox.lastFocus;
    lightbox.lastFocus = null;
    if (restoreFocus && focus && typeof focus.focus === "function") {
      focus.focus();
    }
  }

  function onLightboxKeydown(event) {
    if (!lightbox.root || lightbox.root.hidden) return;
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closeLightbox();
      return;
    }
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      event.stopPropagation();
      navigateLightbox(-1);
      return;
    }
    if (event.key === "ArrowRight") {
      event.preventDefault();
      event.stopPropagation();
      navigateLightbox(1);
      return;
    }
    if (event.key === "Tab") {
      trapLightboxFocus(event);
    }
  }

  function trapLightboxFocus(event) {
    const focusable = Array.from(
      lightbox.dialog.querySelectorAll(
        'button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])'
      )
    ).filter((node) => node.getClientRects().length > 0);
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (!lightbox.dialog.contains(active)) {
      event.preventDefault();
      first.focus();
    } else if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function startLightboxPan(event) {
    if (!lightbox.root || lightbox.root.hidden) return;
    if (lightbox.mode !== "actual" || event.button !== 0) return;
    event.preventDefault();
    lightbox.drag = {
      x: event.clientX,
      y: event.clientY,
      left: lightbox.stage.scrollLeft,
      top: lightbox.stage.scrollTop,
      moved: false
    };
    lightbox.root.classList.add("is-panning");
    document.addEventListener("pointermove", onLightboxPanMove, true);
    document.addEventListener("pointerup", endLightboxPan, true);
    document.addEventListener("pointercancel", endLightboxPan, true);
  }

  function onLightboxPanMove(event) {
    const drag = lightbox.drag;
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) drag.moved = true;
    lightbox.stage.scrollLeft = drag.left - dx;
    lightbox.stage.scrollTop = drag.top - dy;
    event.preventDefault();
  }

  function endLightboxPan() {
    const drag = lightbox.drag;
    if (!drag) return;
    lightbox.drag = null;
    lightbox.root.classList.remove("is-panning");
    detachLightboxPan();
    if (drag.moved) lightbox.suppressClickUntil = Date.now() + 300;
  }

  function detachLightboxPan() {
    document.removeEventListener("pointermove", onLightboxPanMove, true);
    document.removeEventListener("pointerup", endLightboxPan, true);
    document.removeEventListener("pointercancel", endLightboxPan, true);
  }

  // ---------- helpers ----------

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function notify(message, type, duration) {
    if (typeof window.toast === "function") {
      window.toast(message, type || "info", duration);
      return;
    }
    if (!els.toasts) return;
    const node = el(
      "div",
      `toast${type === "error" ? " is-error" : type === "success" ? " is-success" : ""}`,
      message
    );
    els.toasts.append(node);
    window.setTimeout(() => node.remove(), duration || 4200);
  }

  function createFileError(file) {
    if (!file) return "请选择一张照片";
    if (!ALLOWED_TYPES.has(file.type)) return "只支持 JPEG、PNG、WebP 格式的图片";
    if (file.size > UPLOAD_LIMIT) return "图片不能超过 12 MB";
    return "";
  }

  function revokeCreatePreview() {
    if (!state.createPreviewUrl) return;
    URL.revokeObjectURL(state.createPreviewUrl);
    state.createPreviewUrl = "";
  }

  function clearCreateFile() {
    revokeCreatePreview();
    state.createFile = null;
    if (els.createPreview) {
      els.createPreview.removeAttribute("src");
      els.createPreview.alt = "";
      els.createPreview.hidden = true;
    }
    if (els.createPlaceholder) els.createPlaceholder.hidden = false;
    if (els.createFileActions) els.createFileActions.hidden = true;
    if (els.createDrop) els.createDrop.classList.remove("has-file", "is-dragover");
    if (els.createFileName) els.createFileName.textContent = "";
    if (els.createFile) els.createFile.value = "";
    if (els.createSubmit) els.createSubmit.disabled = true;
  }

  function removeCreateFile() {
    clearCreateFile();
    els.createError.hidden = true;
    els.createError.textContent = "";
  }

  function applyCreateFile(file) {
    const invalid = createFileError(file);
    if (invalid) {
      clearCreateFile();
      els.createError.textContent = invalid;
      els.createError.hidden = false;
      return;
    }
    revokeCreatePreview();
    state.createFile = file;
    state.createPreviewUrl = URL.createObjectURL(file);
    els.createPreview.src = state.createPreviewUrl;
    els.createPreview.alt = file.name || "已选择的老房照片";
    els.createPreview.hidden = false;
    els.createPlaceholder.hidden = true;
    els.createFileActions.hidden = false;
    els.createDrop.classList.add("has-file");
    els.createFileName.textContent = `${file.name} · ${formatBytes(file.size)}`;
    els.createError.hidden = true;
    els.createError.textContent = "";
    els.createSubmit.disabled = false;
  }

  function validateFile(file) {
    if (!ALLOWED_TYPES.has(file.type)) {
      notify("只支持 JPEG、PNG、WebP 图片", "error", 5000);
      return false;
    }
    if (file.size > UPLOAD_LIMIT) {
      notify("图片不能超过 12 MB", "error", 5000);
      return false;
    }
    return true;
  }

  function showProgress(text, hint) {
    els.itemProgress.hidden = false;
    els.itemProgressText.textContent = text || "";
    els.itemProgressHint.hidden = !hint;
    els.itemProgressHint.textContent = hint || "";
  }

  function hideProgress() {
    els.itemProgress.hidden = true;
    els.itemProgressHint.hidden = true;
    els.itemProgressHint.textContent = "";
  }

  function modeLabel(mode) {
    return mode === "text" ? "纯文本" : "图生图";
  }

  function setImageSize(image, width, height) {
    if (!image) return;
    const w = Number(width);
    const h = Number(height);
    if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
      image.width = w;
      image.height = h;
    } else {
      image.removeAttribute("width");
      image.removeAttribute("height");
    }
  }

  function applyAssetFrame(frame, width, height, maxHeightVh = 62) {
    if (!frame) return;
    const w = Number(width);
    const h = Number(height);
    if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) {
      frame.style.aspectRatio = "4 / 3";
      frame.style.maxWidth = "";
      return;
    }
    frame.style.aspectRatio = `${w} / ${h}`;
    frame.style.maxWidth = maxHeightVh
      ? `calc(${maxHeightVh}vh * ${(w / h).toFixed(6)})`
      : "";
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value < 1024) return `${value} B`;
    if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
    return `${(value / (1024 * 1024)).toFixed(1)} MB`;
  }

  function formatSeconds(value) {
    const seconds = Math.max(0, Number(value) || 0);
    if (seconds < 60) return `${seconds.toFixed(1)} 秒`;
    const minutes = Math.floor(seconds / 60);
    const rest = Math.round(seconds % 60);
    return `${minutes} 分 ${String(rest).padStart(2, "0")} 秒`;
  }

  function formatClockDuration(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    const minutes = Math.floor(seconds / 60);
    return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function formatTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function formatAssetSize(width, height, fallback) {
    const w = Number(width);
    const h = Number(height);
    if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
      return `${Math.round(w)} × ${Math.round(h)}`;
    }
    const text = String(fallback || "").trim();
    const match = text.match(/^(\d+)\s*[*×x]\s*(\d+)$/);
    if (!match) return "";
    return `${match[1]} × ${match[2]}`;
  }

  function outputSizeLabel(gen) {
    const final = String((gen && gen.size_final) || "").trim();
    const match = final.match(/^(\d+)\s*[*×x]\s*(\d+)$/);
    if (match) return `输出 ${match[1]}×${match[2]}`;
    const w = Number(gen && gen.width);
    const h = Number(gen && gen.height);
    if (Number.isFinite(w) && w > 0 && Number.isFinite(h) && h > 0) {
      return `输出 ${Math.round(w)}×${Math.round(h)}`;
    }
    return "";
  }

  function formatScore(score) {
    if (score === null || score === undefined || score === "") return "未评分";
    const value = Number(score);
    if (!Number.isFinite(value)) return "未评分";
    return `结构符合度 ${Math.round(value * 100)}%`;
  }

  function formatScoreShort(score) {
    if (score === null || score === undefined || score === "") return "未评分";
    const value = Number(score);
    if (!Number.isFinite(value)) return "未评分";
    return `${Math.round(value * 100)}%`;
  }

  const CHECK_FAIL_LABELS = {
    function: "房型",
    openings: "门窗洞口",
    count: "洞口数量",
    framing: "构图",
    perspective: "透视",
    ceiling: "吊顶",
    floor: "地面",
    fixed_features: "固定设施",
    storage: "柜体收纳",
    lighting: "灯光",
    richness: "陈设密度",
    realism: "质感"
  };

  function checkFailures(check) {
    const failures = [];
    const seen = new Set();
    function add(item) {
      if (!item || item.ok !== false) return;
      const key = String(item.key || "").trim();
      const note = String(item.note || "").trim();
      const token = `${key}\u0000${note}`;
      if (seen.has(token)) return;
      seen.add(token);
      failures.push({ key: key || "未标注", note });
    }
    if (!check || typeof check !== "object") return failures;
    if (Array.isArray(check.items)) {
      for (const item of check.items) add(item);
    } else {
      add(check);
    }
    return failures;
  }

  function checkFailureLabel(failure) {
    const key = String(failure && failure.key ? failure.key : "").toLowerCase();
    if (key === "openings" || key === "count") {
      const match = String(failure.note || "").match(
        /introduc\w*\s+(\d+)\s+(?:window|opening|door)/i
      );
      if (match) return `新增 ${Number(match[1])} 扇窗`;
      return "门窗洞口";
    }
    return CHECK_FAIL_LABELS[key] || (failure && failure.key) || "未标注项";
  }

  function checkFailureLabels(failures) {
    const labels = [];
    let openingLabel = "";
    for (const failure of failures) {
      const key = String(failure.key || "").toLowerCase();
      const label = checkFailureLabel(failure);
      if (key === "openings" || key === "count") {
        if (!openingLabel) {
          openingLabel = label;
        } else if (openingLabel === "门窗洞口" && label !== "门窗洞口") {
          openingLabel = label;
        }
        continue;
      }
      if (!labels.includes(label)) labels.push(label);
    }
    if (openingLabel) labels.unshift(openingLabel);
    return labels;
  }

  function renderCheckWarning(gen) {
    const checks = gen && gen.checks;
    const check = Array.isArray(checks) ? checks[checks.length - 1] : checks;
    const failures = checkFailures(check);
    if (!failures.length) return null;
    const labels = checkFailureLabels(failures);
    const summary = labels.length > 1
      ? `${labels[0]} 等 ${labels.length} 项`
      : labels[0];
    const fragment = document.createDocumentFragment();
    const chip = el("button", "rm-check-chip", `⚠ 结构存疑 · ${summary}`);
    chip.type = "button";
    chip.setAttribute("aria-expanded", "false");
    chip.setAttribute("aria-controls", "rm-current-check-detail");
    const detail = el("div", "rm-check-detail");
    detail.id = "rm-current-check-detail";
    detail.hidden = true;
    detail.append(el("p", "rm-check-detail-title", "结构自查未通过项"));
    for (const failure of failures) {
      const row = el("p", "rm-check-detail-row");
      row.append(el("code", "rm-check-detail-key", failure.key));
      row.append(
        el("span", "rm-check-detail-note", failure.note || "（无说明）")
      );
      detail.append(row);
    }
    chip.addEventListener("click", () => {
      const open = chip.getAttribute("aria-expanded") !== "true";
      chip.setAttribute("aria-expanded", String(open));
      detail.hidden = !open;
    });
    fragment.append(chip, detail);
    return fragment;
  }

  function safeDecode(value) {
    try {
      return decodeURIComponent(String(value || "")).trim();
    } catch (error) {
      return String(value || "").trim();
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => window.setTimeout(resolve, ms));
  }

  function renderModeState() {
    if (!els.detailMode) return;
    for (const button of els.detailMode.querySelectorAll("[data-item-mode]")) {
      button.setAttribute("aria-checked", String(button.dataset.itemMode === state.mode));
    }
  }
})();