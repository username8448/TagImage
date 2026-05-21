/*!
 * PreviewModal standalone lightbox for canvas images.
 *
 * Required HTML contract:
 * - #preview-modal            (overlay root)
 * - #preview-close            (close button)
 * - #preview-modal-canvas     (canvas used inside lightbox)
 * - #zoom-level               (zoom label, e.g. "100%")
 */
(function initPreviewModalStandalone(global) {
  "use strict";

  var DEFAULTS = {
    modalId: "preview-modal",
    canvasId: "preview-modal-canvas",
    closeId: "preview-close",
    zoomId: "zoom-level",
    minScale: 0.1,
    maxScale: 10,
    initialViewportFill: 0.6,
    messageType: "web-tools:preview-modal",
    postToParent: true
  };

  function mergeOptions(options) {
    var merged = {};
    var key;
    for (key in DEFAULTS) {
      if (Object.prototype.hasOwnProperty.call(DEFAULTS, key)) {
        merged[key] = DEFAULTS[key];
      }
    }
    if (!options || typeof options !== "object") return merged;
    for (key in options) {
      if (Object.prototype.hasOwnProperty.call(options, key) && options[key] !== undefined) {
        merged[key] = options[key];
      }
    }
    return merged;
  }

  function clamp(value, min, max) {
    if (value < min) return min;
    if (value > max) return max;
    return value;
  }

  function requireElement(id, expected, message) {
    var node = document.getElementById(id);
    if (!(node instanceof expected)) {
      throw new Error(message + " (id=\"" + id + "\")");
    }
    return node;
  }

  function getCanvas2DContext(canvas) {
    var ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Could not obtain 2D context from preview canvas.");
    return ctx;
  }

  function PreviewModal(options) {
    this.options = mergeOptions(options);
    this.modal = requireElement(this.options.modalId, HTMLElement, "Preview modal element is missing");
    this.canvas = requireElement(this.options.canvasId, HTMLCanvasElement, "Preview modal canvas is missing");
    this.closeBtn = requireElement(this.options.closeId, HTMLElement, "Preview close button is missing");
    this.zoomLevel = requireElement(this.options.zoomId, HTMLElement, "Preview zoom label is missing");
    this.ctx = getCanvas2DContext(this.canvas);
    this.isOpen = false;
    this.scale = 1;
    this.minScale = Number(this.options.minScale);
    this.maxScale = Number(this.options.maxScale);
    this.offsetX = 0;
    this.offsetY = 0;
    this.isDragging = false;
    this.hasDragged = false;
    this.dragStartX = 0;
    this.dragStartY = 0;
    this.lastX = 0;
    this.lastY = 0;
    this.imageWidth = 0;
    this.imageHeight = 0;
    this.containerWidth = 0;
    this.containerHeight = 0;
    if (!Number.isFinite(this.minScale) || this.minScale <= 0) this.minScale = DEFAULTS.minScale;
    if (!Number.isFinite(this.maxScale) || this.maxScale <= this.minScale) this.maxScale = DEFAULTS.maxScale;
    this._boundWheel = this.handleWheel.bind(this);
    this._boundMouseDown = this.handleMouseDown.bind(this);
    this._boundMouseMove = this.handleMouseMove.bind(this);
    this._boundMouseUp = this.handleMouseUp.bind(this);
    this._boundKeyDown = this.handleKeyDown.bind(this);
    this._boundBackdropClick = this.handleBackdropClick.bind(this);
    this._boundCloseClick = this.close.bind(this);
    this.initEventListeners();
  }

  PreviewModal.prototype.notifyShell = function notifyShell(open) {
    if (!this.options.postToParent) return;
    if (!global.parent || global.parent === global) return;
    global.parent.postMessage({ type: this.options.messageType, open: Boolean(open) }, "*");
  };

  PreviewModal.prototype.initEventListeners = function initEventListeners() {
    this.closeBtn.addEventListener("click", this._boundCloseClick);
    this.modal.addEventListener("click", this._boundBackdropClick);
    document.addEventListener("keydown", this._boundKeyDown);
    this.canvas.addEventListener("wheel", this._boundWheel, { passive: false });
    this.canvas.addEventListener("mousedown", this._boundMouseDown);
    this.canvas.addEventListener("mousemove", this._boundMouseMove);
    this.canvas.addEventListener("mouseup", this._boundMouseUp);
    this.canvas.addEventListener("mouseleave", this._boundMouseUp);
  };

  PreviewModal.prototype.handleBackdropClick = function handleBackdropClick(event) {
    if (event.target === this.modal) this.close();
  };

  PreviewModal.prototype.handleKeyDown = function handleKeyDown(event) {
    if (event.key === "Escape" && this.isOpen) this.close();
  };

  PreviewModal.prototype.open = function open(sourceCanvas) {
    if (!(sourceCanvas instanceof HTMLCanvasElement)) {
      throw new TypeError("PreviewModal.open(sourceCanvas) expects an HTMLCanvasElement.");
    }
    this.imageWidth = sourceCanvas.width;
    this.imageHeight = sourceCanvas.height;
    if (!this.imageWidth || !this.imageHeight) throw new Error("Source canvas is empty.");
    this.containerWidth = global.innerWidth;
    this.containerHeight = global.innerHeight;
    var fill = Number(this.options.initialViewportFill);
    if (!Number.isFinite(fill) || fill <= 0 || fill > 1) fill = DEFAULTS.initialViewportFill;
    var targetWidth = this.containerWidth * fill;
    var targetHeight = this.containerHeight * fill;
    var scaleX = targetWidth / this.imageWidth;
    var scaleY = targetHeight / this.imageHeight;
    this.scale = clamp(Math.min(scaleX, scaleY), this.minScale, this.maxScale);
    this.offsetX = (this.containerWidth - this.imageWidth * this.scale) / 2;
    this.offsetY = (this.containerHeight - this.imageHeight * this.scale) / 2;
    this.canvas.width = this.imageWidth;
    this.canvas.height = this.imageHeight;
    this.ctx.clearRect(0, 0, this.imageWidth, this.imageHeight);
    this.ctx.drawImage(sourceCanvas, 0, 0);
    this.modal.style.display = "flex";
    this.isOpen = true;
    this.notifyShell(true);
    this.render();
  };

  PreviewModal.prototype.update = function update(sourceCanvas) {
    if (!this.isOpen) return;
    if (!(sourceCanvas instanceof HTMLCanvasElement)) return;
    if (sourceCanvas.width !== this.imageWidth || sourceCanvas.height !== this.imageHeight) {
      this.open(sourceCanvas);
      return;
    }
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.ctx.drawImage(sourceCanvas, 0, 0);
  };

  PreviewModal.prototype.close = function close() {
    this.modal.style.display = "none";
    this.isOpen = false;
    this.isDragging = false;
    this.hasDragged = false;
    this.notifyShell(false);
  };

  PreviewModal.prototype.zoomAt = function zoomAt(mouseX, mouseY, factor) {
    var newScale = this.scale * factor;
    if (newScale < this.minScale || newScale > this.maxScale) return;
    var canvasX = (mouseX - this.offsetX) / this.scale;
    var canvasY = (mouseY - this.offsetY) / this.scale;
    this.scale = newScale;
    this.offsetX = mouseX - canvasX * this.scale;
    this.offsetY = mouseY - canvasY * this.scale;
    this.constrainOffset();
    this.render();
  };

  PreviewModal.prototype.handleWheel = function handleWheel(event) {
    event.preventDefault();
    var delta = event.deltaY > 0 ? 0.9 : 1.1;
    this.zoomAt(event.clientX, event.clientY, delta);
  };

  PreviewModal.prototype.handleMouseDown = function handleMouseDown(event) {
    if (event.button !== 0) return;
    this.isDragging = true;
    this.hasDragged = false;
    this.dragStartX = event.clientX;
    this.dragStartY = event.clientY;
    this.lastX = event.clientX;
    this.lastY = event.clientY;
    this.canvas.style.cursor = "grabbing";
  };

  PreviewModal.prototype.handleMouseMove = function handleMouseMove(event) {
    if (!this.isDragging) return;
    var deltaX = event.clientX - this.lastX;
    var deltaY = event.clientY - this.lastY;
    var totalDx = event.clientX - this.dragStartX;
    var totalDy = event.clientY - this.dragStartY;
    if (!this.hasDragged && (Math.abs(totalDx) > 3 || Math.abs(totalDy) > 3)) this.hasDragged = true;
    if (this.hasDragged) {
      this.offsetX += deltaX;
      this.offsetY += deltaY;
      this.constrainOffset();
      this.render();
    }
    this.lastX = event.clientX;
    this.lastY = event.clientY;
  };

  PreviewModal.prototype.handleMouseUp = function handleMouseUp(event) {
    if (!this.isDragging) return;
    if (!this.hasDragged) this.zoomAt(event.clientX, event.clientY, 1.5);
    this.isDragging = false;
    this.canvas.style.cursor = "grab";
  };

  PreviewModal.prototype.constrainOffset = function constrainOffset() {
    var scaledWidth = this.imageWidth * this.scale;
    var scaledHeight = this.imageHeight * this.scale;
    var minOffsetX = this.containerWidth - scaledWidth;
    var maxOffsetX = 0;
    var minOffsetY = this.containerHeight - scaledHeight;
    var maxOffsetY = 0;
    if (scaledWidth < this.containerWidth) this.offsetX = (this.containerWidth - scaledWidth) / 2;
    else this.offsetX = Math.max(minOffsetX, Math.min(maxOffsetX, this.offsetX));
    if (scaledHeight < this.containerHeight) this.offsetY = (this.containerHeight - scaledHeight) / 2;
    else this.offsetY = Math.max(minOffsetY, Math.min(maxOffsetY, this.offsetY));
  };

  PreviewModal.prototype.render = function render() {
    if (!this.isOpen) return;
    var scaledWidth = this.imageWidth * this.scale;
    var scaledHeight = this.imageHeight * this.scale;
    this.canvas.style.width = scaledWidth + "px";
    this.canvas.style.height = scaledHeight + "px";
    this.canvas.style.left = this.offsetX + "px";
    this.canvas.style.top = this.offsetY + "px";
    this.zoomLevel.textContent = Math.round(this.scale * 100) + "%";
  };

  global.PreviewModal = PreviewModal;
})(window);

let allImages = [];
let visibleImages = [];
let allTagPool = [];
let allTagMeta = new Map();
let tabs = [];
let activeTabId = null;
let lbIndex = -1;
let statusInterval = null;
let loadedIds = new Set();
let observer = null;
let sessionSaveTimer = null;
let renderedCount = 0;
let serverTotal = 0;
let nextCursor = null;
let hasMorePages = false;
let isLoadingPage = false;
let activeImagesRequest = 0;
let previewModal = null;
let lightboxImages = [];
let previewRequestToken = 0;
let suppressPreviewCloseClear = false;
let tagManagerSelection = {includeTags: [], excludeTags: []};
let latestStatus = null;
let filterSuggestionOpen = false;
let settingsActiveTab = 'general';
let tagAdminSort = 'name';
let lastScrollY = 0;
let graphState = null;
const PAGE = 48;
const canvasCache = new Map();
const MAX_CANVAS_CACHE = 9;

function makeDefaultTab(title) {
  const id = "tab-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
  return { id, title: title || "Все фото", includeTags: [], excludeTags: [], matchMode: "any", lastImageId: null, scrollTop: 0 };
}

function activeTab() {
  if (!tabs.length) {
    const tab = makeDefaultTab();
    tabs = [tab];
    activeTabId = tab.id;
  }
  let tab = tabs.find(item => item.id === activeTabId);
  if (!tab) {
    tab = tabs[0];
    activeTabId = tab.id;
  }
  tab.includeTags = Array.isArray(tab.includeTags) ? tab.includeTags : [];
  tab.excludeTags = Array.isArray(tab.excludeTags) ? tab.excludeTags : [];
  tab.matchMode = tab.matchMode === "all" ? "all" : "any";
  return tab;
}

async function openFolder() {
  const setupInput = document.getElementById('setup-folder');
  const folderInput = document.getElementById('folder-input');
  const setupVisible = document.getElementById('setup-screen').style.display !== 'none';
  const p = (setupVisible ? setupInput.value : (folderInput.value || setupInput.value)).trim();
  if (!p) return alert('Введите путь к папке');
  try {
    const r = await fetch('/api/folder', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path: p})
    });
    if (!r.ok) throw new Error(await readError(r));
    folderInput.value = p;
    setupInput.value = p;
    tabs = [makeDefaultTab()];
    activeTabId = tabs[0].id;
    showGallery();
    renderTabs();
    renderFilterControls();
    startStatusPolling();
    await refreshImages(true);
    saveSessionSoon();
  } catch (e) {
    alert('Ошибка: ' + e.message);
  }
}

function toggleSettings(force) {
  const panel = document.getElementById('settings-panel');
  const open = force === undefined ? !panel.classList.contains('open') : Boolean(force);
  panel.classList.toggle('open', open);
  panel.setAttribute('aria-hidden', open ? 'false' : 'true');
  if (open) document.body.classList.add('modal-open');
  else if (!document.getElementById('graph-overlay').classList.contains('open')) document.body.classList.remove('modal-open');
  if (open) {
    showChrome();
    setSettingsTab(settingsActiveTab || 'general');
  }
}

function setSettingsTab(tab) {
  settingsActiveTab = ['general', 'tags', 'graph'].includes(tab) ? tab : 'general';
  document.querySelectorAll('.settings-tab').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.settingsTab === settingsActiveTab);
  });
  document.querySelectorAll('.settings-panel-page').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.settingsPanel === settingsActiveTab);
  });
  if (settingsActiveTab === 'tags') renderTagAdmin();
}

function showGallery() {
  document.getElementById('setup-screen').style.display = 'none';
  document.getElementById('gallery-screen').style.display = 'block';
}

function showSetup() {
  document.getElementById('setup-screen').style.display = 'flex';
  document.getElementById('gallery-screen').style.display = 'none';
}

document.getElementById('setup-folder').addEventListener('keydown', e => {
  if (e.key === 'Enter') openFolder();
});
document.getElementById('folder-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') openFolder();
});

async function startStatusPolling() {
  if (statusInterval) clearInterval(statusInterval);
  await pollStatus();
  statusInterval = setInterval(pollStatus, 1000);
}

async function pollStatus() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    latestStatus = s;
    renderDbStatus(s);
    if (s.root) {
      document.getElementById('folder-input').value = s.root;
      document.getElementById('setup-folder').value = s.root;
      document.getElementById('current-root').textContent = s.root;
    }
    const prog = document.getElementById('scan-progress');
    const bar = document.getElementById('scan-bar');
    const txt = document.getElementById('status-text');
    if (s.running) {
      prog.style.display = 'block';
      const pct = s.total > 0 ? Math.round(s.done / s.total * 100) : 0;
      bar.style.width = pct + '%';
      txt.textContent = `Сканирование: ${s.done}/${s.total}`;
      if (s.done % 20 === 0 || s.done === s.total) refreshImages(false);
    } else if (s.queued) {
      prog.style.display = 'block';
      bar.style.width = '0';
      txt.textContent = 'Сканирование в очереди';
    } else {
      prog.style.display = 'none';
      bar.style.width = '0';
      txt.textContent = s.error ? s.error : (s.root || 'Готово');
      if (s.root) refreshImages(false);
      if (statusInterval && !s.error && !s.queued) {
        clearInterval(statusInterval);
        statusInterval = null;
      }
    }
  } catch (e) {
    setDbStatus(false, e.message);
  }
}

function renderDbStatus(status) {
  if (status.db_ready) setDbStatus(true, 'PostgreSQL подключен');
  else setDbStatus(false, status.db_error || 'PostgreSQL недоступен');
}

function setDbStatus(ok, text) {
  const db = document.getElementById('db-status');
  const setup = document.getElementById('setup-db-status');
  db.textContent = text;
  db.title = text;
  setup.textContent = text;
  setup.title = text;
  db.classList.toggle('ok', ok);
  db.classList.toggle('bad', !ok);
}

async function refreshImages(clear = true) {
  const requestId = ++activeImagesRequest;
  const tab = activeTab();
  const params = new URLSearchParams();
  if (tab.includeTags && tab.includeTags.length) params.set('include_tags', tab.includeTags.join(','));
  if (tab.excludeTags && tab.excludeTags.length) params.set('exclude_tags', tab.excludeTags.join(','));
  params.set('match_mode', tab.matchMode === 'all' ? 'all' : 'any');
  params.set('limit', String(PAGE));
  params.set('sort', 'path_asc');
  params.set('include_total', '0');
  try {
    const r = await fetch('/api/images?' + params.toString());
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    if (requestId !== activeImagesRequest) return;
    allImages = d.items || d.images || [];
    visibleImages = allImages.slice();
    serverTotal = Number((d.page && d.page.total) || visibleImages.length || 0);
    nextCursor = d.page ? d.page.next_cursor : null;
    hasMorePages = Boolean(d.page && d.page.has_more);
    isLoadingPage = false;
    await refreshTagPool();
    applyFilter(clear);
    restorePreviewIfNeeded();
  } catch (e) {
    console.error(e);
    document.getElementById('status-text').textContent = e.message;
  }
}

function applyFilter(clear = true) {
  const tab = activeTab();
  visibleImages = allImages.slice();
  const count = serverTotal || visibleImages.length;
  document.getElementById('count-text').textContent = `${count}${hasMorePages ? '+' : ''} фото`;
  updateTabTitle(tab);
  renderTabs();
  renderFilterControls();
  if (clear) {
    loadedIds.clear();
    renderedCount = 0;
    document.getElementById('gallery').innerHTML = '';
  }
  renderBatch();
  document.getElementById('empty-state').style.display = visibleImages.length ? 'none' : 'flex';
  requestGraphRebuild();
}

async function loadNextImagesPage() {
  if (!hasMorePages || !nextCursor || isLoadingPage) return;
  isLoadingPage = true;
  const tab = activeTab();
  const params = new URLSearchParams();
  if (tab.includeTags && tab.includeTags.length) params.set('include_tags', tab.includeTags.join(','));
  if (tab.excludeTags && tab.excludeTags.length) params.set('exclude_tags', tab.excludeTags.join(','));
  params.set('match_mode', tab.matchMode === 'all' ? 'all' : 'any');
  params.set('limit', String(PAGE));
  params.set('sort', 'path_asc');
  params.set('include_total', '0');
  params.set('cursor', nextCursor);
  try {
    const r = await fetch('/api/images?' + params.toString());
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    const items = d.items || d.images || [];
    const seen = new Set(allImages.map(img => img.id));
    items.forEach(img => {
      if (!seen.has(img.id)) allImages.push(img);
    });
    visibleImages = allImages.slice();
    serverTotal = Number((d.page && d.page.total) || serverTotal || visibleImages.length || 0);
    nextCursor = d.page ? d.page.next_cursor : null;
    hasMorePages = Boolean(d.page && d.page.has_more);
    renderBatch();
  } catch (e) {
    console.error(e);
  } finally {
    isLoadingPage = false;
  }
}

function renderBatch() {
  const gallery = document.getElementById('gallery');
  const start = renderedCount;
  const end = Math.min(start + PAGE, visibleImages.length);
  for (let i = start; i < end; i++) {
    const img = visibleImages[i];
    if (loadedIds.has(img.id)) continue;
    loadedIds.add(img.id);
    gallery.appendChild(makeCard(img, i));
  }
  renderedCount = end;
  setupLazyLoad();
}

function makeCard(img, idx) {
  const card = document.createElement('article');
  card.className = 'card';
  card.dataset.id = img.id;
  card.dataset.idx = idx;
  const name = fileName(img.path);
  const ph = document.createElement('img');
  ph.className = 'lazy';
  ph.dataset.src = img.thumb_url || `/thumb-file/${img.id}.jpg`;
  ph.dataset.fallbackSrc = `/thumb/${img.id}`;
  ph.alt = name;
  ph.decoding = 'async';
  ph.loading = 'lazy';
  ph.style.aspectRatio = aspectCss(img);
  if (img.width > 0 && img.height > 0) {
    ph.width = img.width;
    ph.height = img.height;
  }
  const overlay = document.createElement('div');
  overlay.className = 'card-overlay';
  overlay.innerHTML = `
    <div class="card-name">${escHtml(name)}</div>
    <div class="card-tags">${renderTagChips(img)}</div>
  `;
  card.appendChild(ph);
  card.appendChild(overlay);
  card.addEventListener('click', () => openLightbox(idx));
  return card;
}

function renderTagChips(img) {
  const autoTags = img.auto_tags || img.folder_tags || [];
  const userTags = img.user_tags || [];
  return [
    ...autoTags.map(t => renderTagChip(t, {auto: true, className: 'tag-chip'})),
    ...userTags.map(t => renderTagChip(t, {className: 'tag-chip'}))
  ].join('');
}

function updateCardForImage(img) {
  const card = document.querySelector(`.card[data-id="${cssEscape(img.id)}"]`);
  if (!card) return;
  const tags = card.querySelector('.card-tags');
  if (tags) tags.innerHTML = renderTagChips(img);
}

function setupLazyLoad() {
  if (observer) observer.disconnect();
  observer = new IntersectionObserver(entries => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const img = entry.target;
      if (img.dataset.src) {
        img.onload = () => img.classList.add('loaded');
        img.onerror = () => {
          const fallback = img.dataset.fallbackSrc;
          if (fallback && img.src !== fallback) {
            loadThumbWithRetry(img, fallback);
            return;
          }
          img.classList.add('loaded');
          img.alt = 'Ошибка загрузки';
        };
        img.src = img.dataset.src;
        img.removeAttribute('data-src');
        observer.unobserve(img);
      }
    });
  }, {rootMargin: '320px'});
  document.querySelectorAll('img.lazy').forEach(img => observer.observe(img));
  const gallery = document.getElementById('gallery');
  const old = gallery.querySelector('.sentinel');
  if (old) old.remove();
  if (renderedCount < visibleImages.length) {
    const sentinel = document.createElement('div');
    sentinel.className = 'sentinel';
    sentinel.style.height = '1px';
    gallery.appendChild(sentinel);
    const sentinelObs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) {
        sentinelObs.disconnect();
        if (renderedCount < visibleImages.length) {
          renderBatch();
        } else if (hasMorePages) {
          loadNextImagesPage();
        }
      }
    }, {rootMargin: '620px'});
    sentinelObs.observe(sentinel);
  } else if (hasMorePages) {
    const sentinel = document.createElement('div');
    sentinel.className = 'sentinel';
    sentinel.style.height = '1px';
    gallery.appendChild(sentinel);
    const sentinelObs = new IntersectionObserver(entries => {
      if (entries[0].isIntersecting) {
        sentinelObs.disconnect();
        loadNextImagesPage();
      }
    }, {rootMargin: '620px'});
    sentinelObs.observe(sentinel);
  }
}

async function loadThumbWithRetry(img, url, attempt = 0) {
  const maxAttempts = 20;
  try {
    const r = await fetch(url, {cache: 'no-store'});
    if (r.status === 200) {
      img.src = url;
      img.onload = () => img.classList.add('loaded');
      img.onerror = () => {
        img.classList.add('loaded');
        img.alt = 'Ошибка загрузки';
      };
      return;
    }
    if (r.status === 202 && attempt < maxAttempts) {
      let retryAfter = 180;
      try {
        const p = await r.json();
        retryAfter = Math.max(80, Number(p.retry_after_ms || retryAfter));
      } catch {}
      setTimeout(() => loadThumbWithRetry(img, url, attempt + 1), retryAfter);
      return;
    }
  } catch {}
  img.classList.add('loaded');
  img.alt = 'Ошибка загрузки';
}

async function refreshTagPool() {
  try {
    const d = await fetch('/api/tags').then(r => r.json());
    setTagPool(d.tags || []);
    updateFishGhosts();
    renderFilterSuggestions();
    renderTagAdmin();
  } catch {}
}

function addTab() {
  saveActiveScroll();
  const tab = makeDefaultTab();
  tabs.push(tab);
  activeTabId = tab.id;
  closePreview(false);
  refreshImages(true);
  window.scrollTo({top: 0});
  saveSessionSoon();
}

function switchTab(id) {
  if (id === activeTabId) return;
  saveActiveScroll();
  activeTabId = id;
  closePreview(false);
  refreshImages(true);
  const tab = activeTab();
  setTimeout(() => window.scrollTo({top: tab.scrollTop || 0}), 0);
  saveSessionSoon();
}

function closeTab(id) {
  if (tabs.length <= 1) {
    tabs = [makeDefaultTab()];
    activeTabId = tabs[0].id;
  } else {
    const idx = tabs.findIndex(tab => tab.id === id);
    tabs = tabs.filter(tab => tab.id !== id);
    if (activeTabId === id) {
      const next = tabs[Math.max(0, idx - 1)] || tabs[0];
      activeTabId = next.id;
    }
  }
  closePreview(false);
  refreshImages(true);
  saveSessionSoon();
}

function renderTabs() {
  const el = document.getElementById('tabs');
  el.innerHTML = tabs.map(tab => `
    <button class="tab ${tab.id === activeTabId ? 'active' : ''}" data-tab="${escAttr(tab.id)}" title="${escAttr(tab.title)}">
      <span class="tab-title">${escHtml(tab.title)}</span>
      <span class="tab-close" data-close="${escAttr(tab.id)}">×</span>
    </button>
  `).join('');
  el.querySelectorAll('.tab[data-tab]').forEach(btn => {
    btn.addEventListener('click', e => {
      if (e.target.closest('.tab-close')) return;
      switchTab(btn.dataset.tab);
    });
  });
  el.querySelectorAll('.tab-close[data-close]').forEach(btn => {
    btn.addEventListener('click', e => {
      e.stopPropagation();
      closeTab(btn.dataset.close);
    });
  });
}

function updateTabTitle(tab) {
  const include = tab.includeTags || [];
  const exclude = tab.excludeTags || [];
  if (!include.length && !exclude.length) {
    tab.title = "Все фото";
    return;
  }
  const parts = [];
  if (include.length) parts.push((tab.matchMode === "all" ? "все " : "любой ") + include.join(tab.matchMode === "all" ? " + " : " / "));
  if (exclude.length) parts.push("без " + exclude.join(", "));
  tab.title = parts.join(" / ").slice(0, 80);
}

function saveActiveScroll() {
  if (!tabs.length) return;
  activeTab().scrollTop = window.scrollY || document.documentElement.scrollTop || 0;
}

function renderFilterControls() {
  const tab = activeTab();
  document.getElementById('match-any').classList.toggle('active', tab.matchMode !== 'all');
  document.getElementById('match-all').classList.toggle('active', tab.matchMode === 'all');
  renderSelectedFilterTags();
  renderFilterSuggestions();
}

function setMatchMode(mode) {
  activeTab().matchMode = mode === 'all' ? 'all' : 'any';
  refreshImages(true);
  saveSessionSoon();
}

function renderSelectedFilterTags() {
  const tab = activeTab();
  const el = document.getElementById('selected-filter-tags');
  const include = (tab.includeTags || []).map(tag => renderTagChip(tag, {
    removable: true,
    actionKind: 'include',
    className: 'filter-chip'
  })).join('');
  const exclude = (tab.excludeTags || []).map(tag => renderTagChip(tag, {
    removable: true,
    exclude: true,
    actionKind: 'exclude',
    className: 'filter-chip'
  })).join('');
  el.innerHTML = include + exclude || '<span class="muted-inline">Фильтры не выбраны</span>';
  el.querySelectorAll('button[data-tag]').forEach(btn => {
    btn.addEventListener('click', () => removeFilterTag(btn.dataset.kind, btn.dataset.tag));
  });
}

function parseFilterTagInput(raw) {
  const parsed = splitTagPrefix(raw);
  let value = parsed.value;
  let kind = 'include';
  if (parsed.prefix) {
    kind = 'exclude';
  }
  if (!value) return null;
  return {kind, tag: findDisplayTag(value)};
}

function addFilterFromInput(raw) {
  commitFilterTokens(raw);
}

function addFilterTag(kind, tag, clearInput = false) {
  const changed = applyFilterTagToTab(kind, tag, activeTab());
  if (clearInput) document.getElementById('filter-tag-input').value = '';
  if (changed) {
    refreshImages(true);
    saveSessionSoon();
  } else {
    renderFilterSuggestions();
  }
}

function applyFilterTagToTab(kind, tag, tab) {
  const value = findDisplayTag(tag);
  if (!value) return false;
  const norm = normalizeTag(value);
  const key = kind === 'exclude' ? 'excludeTags' : 'includeTags';
  const oppositeKey = kind === 'exclude' ? 'includeTags' : 'excludeTags';
  const before = JSON.stringify([tab.includeTags || [], tab.excludeTags || []]);
  tab[oppositeKey] = (tab[oppositeKey] || []).filter(item => normalizeTag(item) !== norm);
  tab[key] = dedupeDisplayTags([...(tab[key] || []), value]);
  return before !== JSON.stringify([tab.includeTags || [], tab.excludeTags || []]);
}

function commitFilterTokens(raw) {
  const input = document.getElementById('filter-tag-input');
  const tokens = String(raw || input.value || '').split(/\s+/).map(item => item.trim()).filter(Boolean);
  if (!tokens.length) return;
  let changed = false;
  const tab = activeTab();
  tokens.forEach(token => {
    const parsed = parseFilterTagInput(token);
    if (parsed) changed = applyFilterTagToTab(parsed.kind, parsed.tag, tab) || changed;
  });
  input.value = '';
  updateFilterGhost();
  updateFishGhosts();
  renderFilterSuggestions();
  if (changed) {
    refreshImages(true);
    saveSessionSoon();
  }
}

function removeFilterTag(kind, tag) {
  const tab = activeTab();
  const key = kind === 'exclude' ? 'excludeTags' : 'includeTags';
  tab[key] = (tab[key] || []).filter(item => normalizeTag(item) !== normalizeTag(tag));
  refreshImages(true);
  saveSessionSoon();
}

function clearActiveFilters() {
  const tab = activeTab();
  tab.includeTags = [];
  tab.excludeTags = [];
  refreshImages(true);
  saveSessionSoon();
}

function currentFilterPrefix() {
  const input = document.getElementById('filter-tag-input');
  const token = String(input.value || '').split(/\s+/).pop() || '';
  const parsed = splitTagPrefix(token);
  return {token, prefix: parsed.prefix, query: normalizeTag(parsed.value), kind: parsed.prefix ? 'exclude' : 'include'};
}

function updateFilterGhost() {
  const input = document.getElementById('filter-tag-input');
  const ghost = document.getElementById('filter-tag-ghost');
  const raw = input.value || '';
  const parts = raw.split(/\s+/);
  const token = parts.pop() || '';
  const parsed = splitTagPrefix(token);
  const q = normalizeTag(parsed.value);
  const suggestion = q
    ? allTagPool.find(tag => normalizeTag(tag).startsWith(q) && normalizeTag(tag) !== q)
    : '';
  if (!suggestion) {
    input.dataset.suggestion = '';
    ghost.innerHTML = '';
    return;
  }
  const prefix = parts.length ? parts.join(' ') + ' ' : '';
  const completion = prefix + parsed.prefix + suggestion;
  input.dataset.suggestion = completion;
  ghost.innerHTML = completion.startsWith(raw)
    ? `<span class="ghost-hidden">${escHtml(raw)}</span>${escHtml(completion.slice(raw.length))}`
    : '';
}

function renderFilterSuggestions() {
  const list = document.getElementById('filter-suggestion-list');
  if (!list) return;
  updateFilterGhost();
  const {query, prefix, kind} = currentFilterPrefix();
  const tab = activeTab();
  const used = new Set([...(tab.includeTags || []), ...(tab.excludeTags || [])].map(normalizeTag));
  const matches = allTagPool
    .filter(tag => !query || normalizeTag(tag).includes(query))
    .slice(0, 80);
  list.classList.toggle('open', filterSuggestionOpen);
  if (!filterSuggestionOpen) return;
  list.innerHTML = matches.length
    ? matches.map(tag => {
      const norm = normalizeTag(tag);
      const activeKind = (tab.excludeTags || []).some(item => normalizeTag(item) === norm)
        ? 'exclude'
        : (tab.includeTags || []).some(item => normalizeTag(item) === norm)
          ? 'include'
          : '';
      const label = `${prefix ? '!' : ''}${tag}`;
      return `<button class="filter-suggestion ${kind === 'exclude' ? 'is-exclude' : ''} ${activeKind ? 'active' : ''}" type="button" data-kind="${kind}" data-tag="${escAttr(tag)}">${escHtml(label)}${used.has(norm) ? ' ✓' : ''}</button>`;
    }).join('')
    : '<span class="muted-inline">Теги не найдены</span>';
  list.querySelectorAll('button[data-tag]').forEach(btn => {
    btn.addEventListener('click', () => {
      addFilterTag(btn.dataset.kind, btn.dataset.tag, true);
      document.getElementById('filter-tag-input').focus();
    });
  });
}

function initFilterInput() {
  const input = document.getElementById('filter-tag-input');
  input.addEventListener('focus', () => {
    filterSuggestionOpen = true;
    renderFilterSuggestions();
  });
  input.addEventListener('input', () => {
    filterSuggestionOpen = true;
    updateFilterGhost();
    renderFilterSuggestions();
  });
  input.addEventListener('keydown', e => {
    if ((e.key === 'Tab' || e.key === 'ArrowRight') && input.dataset.suggestion) {
      e.preventDefault();
      input.value = input.dataset.suggestion;
      updateFilterGhost();
      renderFilterSuggestions();
      return;
    }
    if (e.key === ' ') {
      e.preventDefault();
      commitFilterTokens(input.value);
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      commitFilterTokens(input.value);
      return;
    }
    if (e.key === 'Escape') {
      filterSuggestionOpen = false;
      input.value = '';
      updateFilterGhost();
      renderFilterSuggestions();
    }
  });
}

function openTagManager() {
  const tab = activeTab();
  tagManagerSelection = {
    includeTags: [...(tab.includeTags || [])],
    excludeTags: [...(tab.excludeTags || [])]
  };
  document.getElementById('tag-overlay').classList.add('open');
  toggleCreateTagPanel(false);
  renderTagManager();
}

function closeTagManager() {
  document.getElementById('tag-overlay').classList.remove('open');
}

function renderTagManager() {
  const selectedInclude = new Set((tagManagerSelection.includeTags || []).map(normalizeTag));
  const selectedExclude = new Set((tagManagerSelection.excludeTags || []).map(normalizeTag));
  const selectedEl = document.getElementById('tag-manager-selected');
  selectedEl.innerHTML = [
    ...(tagManagerSelection.includeTags || []).map(tag => renderTagChip(tag, {
      removable: true,
      actionKind: 'include',
      className: 'filter-chip'
    })),
    ...(tagManagerSelection.excludeTags || []).map(tag => renderTagChip(tag, {
      removable: true,
      exclude: true,
      actionKind: 'exclude',
      className: 'filter-chip'
    }))
  ].join('') || '<span class="muted-inline">Выберите теги ниже</span>';
  selectedEl.querySelectorAll('button[data-tag]').forEach(btn => {
    btn.addEventListener('click', () => {
      removeManagerTag(btn.dataset.kind, btn.dataset.tag);
      renderTagManager();
    });
  });
  const list = document.getElementById('tag-list');
  list.innerHTML = `<button class="tag-option add" onclick="toggleCreateTagPanel(true)" title="Создать тег">+</button>` +
    allTagPool.map(tag => `
      <span class="tag-choice ${selectedInclude.has(normalizeTag(tag)) ? 'include' : ''} ${selectedExclude.has(normalizeTag(tag)) ? 'exclude' : ''}">
        <button class="tag-choice-name" data-kind="include" data-tag="${escAttr(tag)}">${renderTagChip(tag, {count: true})}</button>
        <button class="tag-choice-anti" data-kind="exclude" data-tag="${escAttr(tag)}">!</button>
      </span>
    `).join('');
  list.querySelectorAll('.tag-choice button[data-tag]').forEach(btn => {
    btn.addEventListener('click', () => toggleManagerTag(btn.dataset.kind, btn.dataset.tag));
  });
}

function removeManagerTag(kind, tag) {
  const norm = normalizeTag(tag);
  const key = kind === 'exclude' ? 'excludeTags' : 'includeTags';
  tagManagerSelection[key] = (tagManagerSelection[key] || []).filter(item => normalizeTag(item) !== norm);
}

function toggleManagerTag(kind, tag) {
  const value = findDisplayTag(tag);
  const norm = normalizeTag(value);
  const key = kind === 'exclude' ? 'excludeTags' : 'includeTags';
  const oppositeKey = kind === 'exclude' ? 'includeTags' : 'excludeTags';
  if ((tagManagerSelection[key] || []).some(item => normalizeTag(item) === norm)) {
    removeManagerTag(kind, value);
  } else {
    tagManagerSelection[oppositeKey] = (tagManagerSelection[oppositeKey] || []).filter(item => normalizeTag(item) !== norm);
    tagManagerSelection[key] = dedupeDisplayTags([...(tagManagerSelection[key] || []), value]);
  }
  renderTagManager();
}

function applyTagManagerSelection() {
  activeTab().includeTags = dedupeDisplayTags(tagManagerSelection.includeTags || []);
  activeTab().excludeTags = dedupeDisplayTags(tagManagerSelection.excludeTags || []);
  closeTagManager();
  refreshImages(true);
  saveSessionSoon();
}

function toggleCreateTagPanel(open) {
  const panel = document.getElementById('tag-create-panel');
  panel.classList.toggle('open', Boolean(open));
  if (open) {
    document.getElementById('tag-create-input').value = '';
    updateFishGhosts();
    setTimeout(() => document.getElementById('tag-create-input').focus(), 0);
  }
}

async function createTagFromManager() {
  const input = document.getElementById('tag-create-input');
  const value = input.value.trim();
  if (!value) return;
  try {
    const r = await fetch('/api/tags', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: value})
    });
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    setTagPool(d.tags || [...allTagPool, d.tag || value]);
    const createdName = tagName(d.tag || value);
    tagManagerSelection.includeTags = dedupeDisplayTags([...(tagManagerSelection.includeTags || []), createdName]);
    tagManagerSelection.excludeTags = (tagManagerSelection.excludeTags || []).filter(tag => normalizeTag(tag) !== normalizeTag(createdName));
    input.value = '';
    toggleCreateTagPanel(false);
    renderTagManager();
    renderTagAdmin();
    updateFishGhosts();
  } catch (e) {
    alert('Ошибка: ' + e.message);
  }
}

function renderAllTagSurfaces() {
  renderFilterControls();
  if (document.getElementById('tag-overlay').classList.contains('open')) renderTagManager();
  renderTagAdmin();
  visibleImages.forEach(updateCardForImage);
  const previewImage = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
  if (previewImage) renderPreviewTags(previewImage);
}

function renderTagAdmin() {
  const list = document.getElementById('tag-admin-list');
  if (!list) return;
  const query = normalizeTag(document.getElementById('tag-admin-search').value || '');
  const tags = allTagPool
    .filter(tag => !query || normalizeTag(tag).includes(query))
    .sort((a, b) => {
      const metaA = getTagMeta(a);
      const metaB = getTagMeta(b);
      if (tagAdminSort === 'image_count' || tagAdminSort === 'user_count' || tagAdminSort === 'auto_count') {
        return Number(metaB[tagAdminSort] || 0) - Number(metaA[tagAdminSort] || 0) || a.localeCompare(b);
      }
      return a.localeCompare(b);
    });
  list.innerHTML = tags.length ? tags.map(tag => {
    const meta = getTagMeta(tag);
    const isAuto = Boolean(meta.is_auto || Number(meta.auto_count || 0) > 0);
    const color = meta.color || (isAuto ? '#D4D4D4' : '#E5E5E5');
    return `
      <div class="tag-admin-row" data-tag="${escAttr(tag)}">
        <input type="color" value="${escAttr(color)}" data-action="color" data-tag="${escAttr(tag)}" title="Цвет тега">
        <div class="tag-admin-identity">
          ${renderTagChip(tag, {count: true})}
          <input class="tag-admin-name-input" type="text" value="${escAttr(tag)}" ${isAuto ? 'disabled' : ''} data-name-input="${escAttr(tag)}">
        </div>
        <span class="tag-admin-counts">${Number(meta.image_count || 0)} фото · ${Number(meta.user_count || 0)} ручн · ${Number(meta.auto_count || 0)} папок</span>
        <button class="btn btn-ghost btn-sm" type="button" data-action="rename" data-tag="${escAttr(tag)}" ${isAuto ? 'disabled' : ''}>Сохранить</button>
        <button class="btn btn-danger btn-sm" type="button" data-action="delete" data-tag="${escAttr(tag)}" ${isAuto ? 'disabled' : ''}>Удалить</button>
        ${isAuto ? '<span class="tag-admin-protected">protected</span>' : '<span></span>'}
      </div>
    `;
  }).join('') : '<span class="muted-inline">Теги не найдены</span>';

  list.querySelectorAll('input[data-action="color"]').forEach(input => {
    input.addEventListener('change', () => updateTagDefinition(input.dataset.tag, {color: input.value}, false));
  });
  list.querySelectorAll('button[data-action="rename"]').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = btn.closest('.tag-admin-row').querySelector('.tag-admin-name-input');
      updateTagDefinition(btn.dataset.tag, {name: input ? input.value : btn.dataset.tag}, true);
    });
  });
  list.querySelectorAll('button[data-action="delete"]').forEach(btn => {
    btn.addEventListener('click', () => deleteTagDefinition(btn.dataset.tag));
  });
}

function setTagAdminSort(sort) {
  tagAdminSort = ['name', 'image_count', 'user_count', 'auto_count'].includes(sort) ? sort : 'name';
  renderTagAdmin();
}

async function createTagFromSettings() {
  const input = document.getElementById('tag-admin-create');
  const value = input.value.trim();
  if (!value) return;
  try {
    const r = await fetch('/api/tags', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({name: value})
    });
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    setTagPool(d.tags || [...allTagPool, d.tag || value]);
    input.value = '';
    renderAllTagSurfaces();
    updateFishGhosts();
  } catch (e) {
    alert('Ошибка тега: ' + e.message);
  }
}

async function updateTagDefinition(tag, body, reloadImages) {
  try {
    const r = await fetch(`/api/tags/${encodeURIComponent(tag)}`, {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    setTagPool(d.tags || []);
    if (reloadImages) await refreshImages(true);
    else renderAllTagSurfaces();
  } catch (e) {
    alert('Ошибка тега: ' + e.message);
    renderTagAdmin();
  }
}

async function deleteTagDefinition(tag) {
  if (!confirm(`Удалить тег "${tag}"?`)) return;
  try {
    const r = await fetch(`/api/tags/${encodeURIComponent(tag)}`, {method: 'DELETE'});
    if (!r.ok) throw new Error(await readError(r));
    const d = await r.json();
    setTagPool(d.tags || []);
    await refreshImages(true);
  } catch (e) {
    alert('Ошибка тега: ' + e.message);
  }
}

function scrollToTop() {
  window.scrollTo({top: 0, behavior: 'smooth'});
}

function updateScrollTopButton() {
  const btn = document.getElementById('scroll-top-btn');
  if (!btn) return;
  btn.classList.toggle('visible', window.scrollY > 320);
}

function hasActiveModal() {
  return Boolean(
    document.getElementById('settings-panel').classList.contains('open') ||
    document.getElementById('tag-overlay').classList.contains('open') ||
    document.getElementById('graph-overlay').classList.contains('open') ||
    (previewModal && previewModal.isOpen)
  );
}

function showChrome() {
  document.body.classList.remove('chrome-hidden');
  document.documentElement.classList.remove('chrome-hidden');
}

function hideChrome() {
  document.body.classList.add('chrome-hidden');
  document.documentElement.classList.add('chrome-hidden');
}

function handleChromeScroll() {
  const y = window.scrollY || document.documentElement.scrollTop || document.body.scrollTop || 0;
  updateScrollTopButton();
  if (y < 64 || y < lastScrollY || hasActiveModal()) {
    showChrome();
  } else if (y > lastScrollY + 4) {
    hideChrome();
  }
  lastScrollY = y;
}

function ensureGraphState() {
  if (graphState) return graphState;
  const canvas = document.getElementById('graph-canvas');
  graphState = {
    open: false,
    scope: 'current',
    nodes: [],
    edges: [],
    scale: 1,
    panX: 0,
    panY: 0,
    dragNode: null,
    isPanning: false,
    pointerMoved: false,
    lastX: 0,
    lastY: 0,
    tick: 0,
    raf: null,
    needsRebuild: false,
    query: '',
    nodeLimit: 1000,
    canvas,
    ctx: canvas.getContext('2d')
  };
  canvas.addEventListener('wheel', handleGraphWheel, {passive: false});
  canvas.addEventListener('mousedown', handleGraphMouseDown);
  window.addEventListener('mousemove', handleGraphMouseMove);
  window.addEventListener('mouseup', handleGraphMouseUp);
  canvas.addEventListener('dblclick', resetGraphLayout);
  document.getElementById('graph-search').addEventListener('input', e => {
    graphState.query = normalizeTag(e.target.value);
    renderGraph();
  });
  return graphState;
}

function openGraph(scope) {
  const state = ensureGraphState();
  if (document.getElementById('settings-panel').classList.contains('open')) toggleSettings(false);
  state.open = true;
  if (scope) state.scope = scope;
  document.getElementById('graph-overlay').classList.add('open');
  document.getElementById('graph-overlay').setAttribute('aria-hidden', 'false');
  document.body.classList.add('modal-open');
  showChrome();
  updateGraphScopeButtons();
  rebuildGraph();
}

function closeGraph() {
  if (!graphState) return;
  graphState.open = false;
  if (graphState.raf) cancelAnimationFrame(graphState.raf);
  graphState.raf = null;
  document.getElementById('graph-overlay').classList.remove('open');
  document.getElementById('graph-overlay').setAttribute('aria-hidden', 'true');
  if (!document.getElementById('settings-panel').classList.contains('open')) {
    document.body.classList.remove('modal-open');
  }
}

function setGraphScope(scope) {
  const state = ensureGraphState();
  state.scope = scope === 'all' ? 'all' : 'current';
  document.querySelectorAll('input[name="settings-graph-scope"]').forEach(input => {
    input.checked = input.value === state.scope;
  });
  updateGraphScopeButtons();
  if (state.open) rebuildGraph();
}

function updateGraphScopeButtons() {
  const state = ensureGraphState();
  document.getElementById('graph-scope-current').classList.toggle('active', state.scope !== 'all');
  document.getElementById('graph-scope-all').classList.toggle('active', state.scope === 'all');
}

function requestGraphRebuild() {
  if (!graphState || !graphState.open) return;
  graphState.needsRebuild = true;
  requestAnimationFrame(() => {
    if (graphState && graphState.open && graphState.needsRebuild) rebuildGraph();
  });
}

function graphImages() {
  const state = ensureGraphState();
  return state.scope === 'all' ? allImages : visibleImages;
}

function rebuildGraph() {
  const state = ensureGraphState();
  state.needsRebuild = false;
  resizeGraphCanvas();
  const images = graphImages();
  const tagMap = new Map();
  const imageNodes = [];
  const edges = [];

  images.forEach(img => {
    const tags = dedupeDisplayTags(img.tags || []);
    if (!tags.length) return;
    const imageNode = {
      id: `img:${img.id}`,
      type: 'image',
      imageId: img.id,
      image: img,
      tags,
      x: 0,
      y: 0,
      vx: 0,
      vy: 0,
      r: 3.6
    };
    imageNodes.push(imageNode);
    tags.forEach(tag => {
      const norm = normalizeTag(tag);
      if (!tagMap.has(norm)) {
        tagMap.set(norm, {
          id: `tag:${norm}`,
          type: 'tag',
          tag,
          label: tag,
          x: 0,
          y: 0,
          vx: 0,
          vy: 0,
          r: 10
        });
      }
      edges.push({source: imageNode.id, target: `tag:${norm}`});
    });
  });

  const tagNodes = Array.from(tagMap.values());
  const totalNodes = tagNodes.length + imageNodes.length;
  const notice = document.getElementById('graph-notice');
  if (totalNodes > state.nodeLimit) {
    state.nodes = [];
    state.edges = [];
    notice.textContent = `Граф скрыт: ${totalNodes} узлов. Сузьте фильтр или переключитесь на текущую выборку.`;
    notice.classList.add('open');
    renderGraph();
    return;
  }
  notice.classList.remove('open');
  notice.textContent = '';

  const radius = Math.min(state.canvas.width, state.canvas.height) * 0.34;
  tagNodes.forEach((node, i) => {
    const angle = (Math.PI * 2 * i) / Math.max(1, tagNodes.length);
    node.x = Math.cos(angle) * radius;
    node.y = Math.sin(angle) * radius;
  });
  const tagById = new Map(tagNodes.map(node => [node.id, node]));
  imageNodes.forEach((node, i) => {
    const linked = node.tags.map(tag => tagById.get(`tag:${normalizeTag(tag)}`)).filter(Boolean);
    const baseX = linked.reduce((sum, tag) => sum + tag.x, 0) / Math.max(1, linked.length);
    const baseY = linked.reduce((sum, tag) => sum + tag.y, 0) / Math.max(1, linked.length);
    const jitter = 18 + (i % 23);
    node.x = baseX + Math.cos(i * 2.399) * jitter;
    node.y = baseY + Math.sin(i * 2.399) * jitter;
  });
  state.nodes = [...tagNodes, ...imageNodes];
  state.edges = edges;
  state.tick = 0;
  if (!state.panX && !state.panY) {
    state.panX = state.canvas.width / 2;
    state.panY = state.canvas.height / 2;
  }
  startGraphAnimation();
}

function resizeGraphCanvas() {
  const state = ensureGraphState();
  const rect = state.canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * dpr));
  const height = Math.max(1, Math.floor(rect.height * dpr));
  if (state.canvas.width !== width || state.canvas.height !== height) {
    state.canvas.width = width;
    state.canvas.height = height;
    state.ctx.setTransform(1, 0, 0, 1, 0, 0);
    state.panX = width / 2;
    state.panY = height / 2;
  }
}

function startGraphAnimation() {
  const state = ensureGraphState();
  if (state.raf) cancelAnimationFrame(state.raf);
  const step = () => {
    if (!state.open) return;
    if (state.tick < 180 && !state.dragNode) {
      simulateGraphTick();
      state.tick += 1;
      renderGraph();
      state.raf = requestAnimationFrame(step);
    } else {
      state.raf = null;
      renderGraph();
    }
  };
  state.raf = requestAnimationFrame(step);
}

function simulateGraphTick() {
  const state = ensureGraphState();
  const nodeById = new Map(state.nodes.map(node => [node.id, node]));
  const tagNodes = state.nodes.filter(node => node.type === 'tag');
  const imageNodes = state.nodes.filter(node => node.type === 'image');
  const alpha = Math.max(0.02, 0.16 * (1 - state.tick / 190));

  state.edges.forEach(edge => {
    const source = nodeById.get(edge.source);
    const target = nodeById.get(edge.target);
    if (!source || !target) return;
    const dx = target.x - source.x;
    const dy = target.y - source.y;
    source.vx += dx * 0.0024 * alpha;
    source.vy += dy * 0.0024 * alpha;
    target.vx -= dx * 0.0007 * alpha;
    target.vy -= dy * 0.0007 * alpha;
  });

  for (let i = 0; i < tagNodes.length; i++) {
    for (let j = i + 1; j < tagNodes.length; j++) {
      const a = tagNodes[i];
      const b = tagNodes[j];
      const dx = b.x - a.x || 0.1;
      const dy = b.y - a.y || 0.1;
      const distSq = dx * dx + dy * dy;
      const force = Math.min(1.8, 1700 / Math.max(80, distSq)) * alpha;
      a.vx -= dx * force * 0.002;
      a.vy -= dy * force * 0.002;
      b.vx += dx * force * 0.002;
      b.vy += dy * force * 0.002;
    }
  }

  imageNodes.forEach(node => {
    node.vx += -node.x * 0.00002;
    node.vy += -node.y * 0.00002;
  });
  state.nodes.forEach(node => {
    node.vx *= 0.82;
    node.vy *= 0.82;
    node.x += node.vx;
    node.y += node.vy;
  });
}

function renderGraph() {
  const state = ensureGraphState();
  const ctx = state.ctx;
  resizeGraphCanvas();
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, state.canvas.width, state.canvas.height);
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, state.canvas.width, state.canvas.height);
  ctx.save();
  ctx.translate(state.panX, state.panY);
  ctx.scale(state.scale, state.scale);

  const nodeById = new Map(state.nodes.map(node => [node.id, node]));
  ctx.lineWidth = 1 / state.scale;
  ctx.strokeStyle = 'rgba(180,180,180,.16)';
  state.edges.forEach(edge => {
    const a = nodeById.get(edge.source);
    const b = nodeById.get(edge.target);
    if (!a || !b) return;
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
  });

  const activeIncludes = new Set((activeTab().includeTags || []).map(normalizeTag));
  state.nodes.forEach(node => {
    if (node.type === 'image') {
      ctx.beginPath();
      ctx.fillStyle = 'rgba(210,210,210,.55)';
      ctx.arc(node.x, node.y, node.r, 0, Math.PI * 2);
      ctx.fill();
      return;
    }
    const norm = normalizeTag(node.tag);
    const highlighted = state.query && norm.includes(state.query);
    const active = activeIncludes.has(norm);
    const size = highlighted || active ? 13 : 10;
    ctx.save();
    ctx.translate(node.x, node.y);
    ctx.rotate(Math.PI / 4);
    ctx.fillStyle = active ? '#FFFFFF' : highlighted ? '#CFCFCF' : '#111111';
    ctx.strokeStyle = active ? '#FFFFFF' : '#D4D4D4';
    ctx.lineWidth = (active || highlighted ? 1.8 : 1.1) / state.scale;
    ctx.beginPath();
    ctx.rect(-size, -size, size * 2, size * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
    ctx.font = `${11 / state.scale}px ui-monospace, SFMono-Regular, Consolas, monospace`;
    ctx.fillStyle = active ? '#FFFFFF' : '#D4D4D4';
    ctx.textAlign = 'center';
    ctx.fillText(node.label, node.x, node.y - 17);
  });
  ctx.restore();
}

function graphPoint(event) {
  const state = ensureGraphState();
  const rect = state.canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const x = (event.clientX - rect.left) * dpr;
  const y = (event.clientY - rect.top) * dpr;
  return {
    screenX: x,
    screenY: y,
    x: (x - state.panX) / state.scale,
    y: (y - state.panY) / state.scale
  };
}

function findGraphNode(point) {
  const state = ensureGraphState();
  for (let i = state.nodes.length - 1; i >= 0; i--) {
    const node = state.nodes[i];
    const size = node.type === 'tag' ? 14 : 7;
    if (Math.abs(point.x - node.x) <= size && Math.abs(point.y - node.y) <= size) return node;
  }
  return null;
}

function handleGraphWheel(event) {
  const state = ensureGraphState();
  if (!state.open) return;
  event.preventDefault();
  const point = graphPoint(event);
  const factor = event.deltaY > 0 ? 0.9 : 1.1;
  const nextScale = Math.max(0.22, Math.min(4, state.scale * factor));
  state.panX = point.screenX - point.x * nextScale;
  state.panY = point.screenY - point.y * nextScale;
  state.scale = nextScale;
  renderGraph();
}

function handleGraphMouseDown(event) {
  const state = ensureGraphState();
  if (!state.open || event.button !== 0) return;
  const point = graphPoint(event);
  state.pointerMoved = false;
  state.lastX = point.screenX;
  state.lastY = point.screenY;
  state.dragNode = findGraphNode(point);
  state.isPanning = !state.dragNode;
  state.canvas.classList.add('dragging');
}

function handleGraphMouseMove(event) {
  const state = ensureGraphState();
  if (!state.open || (!state.dragNode && !state.isPanning)) return;
  const point = graphPoint(event);
  const dx = point.screenX - state.lastX;
  const dy = point.screenY - state.lastY;
  if (Math.abs(dx) + Math.abs(dy) > 2) state.pointerMoved = true;
  if (state.dragNode) {
    state.dragNode.x = point.x;
    state.dragNode.y = point.y;
    state.dragNode.vx = 0;
    state.dragNode.vy = 0;
  } else {
    state.panX += dx;
    state.panY += dy;
  }
  state.lastX = point.screenX;
  state.lastY = point.screenY;
  renderGraph();
}

function handleGraphMouseUp(event) {
  const state = ensureGraphState();
  if (!state.open) return;
  const clickedNode = state.dragNode;
  state.canvas.classList.remove('dragging');
  state.dragNode = null;
  state.isPanning = false;
  if (!clickedNode || state.pointerMoved) return;
  if (clickedNode.type === 'tag') {
    toggleGraphTagFilter(clickedNode.tag);
  } else {
    openGraphImage(clickedNode.imageId);
  }
}

function toggleGraphTagFilter(tag) {
  const tab = activeTab();
  const norm = normalizeTag(tag);
  if ((tab.includeTags || []).some(item => normalizeTag(item) === norm)) {
    removeFilterTag('include', tag);
  } else {
    addFilterTag('include', tag, false);
  }
  renderGraph();
}

function openGraphImage(imageId) {
  const source = graphImages();
  const idx = source.findIndex(img => img.id === imageId);
  if (idx < 0) return;
  closeGraph();
  openLightbox(idx, true, source);
}

function resetGraphLayout() {
  if (!graphState) return;
  graphState.scale = 1;
  graphState.panX = 0;
  graphState.panY = 0;
  if (graphState.open) rebuildGraph();
}

function initFishInput(inputId, ghostId, getExcluded, onCommit) {
  const input = document.getElementById(inputId);
  const ghost = document.getElementById(ghostId);
  const controller = { suggestion: '' };
  function update() {
    const value = input.value;
    const parsed = splitTagPrefix(value);
    const q = normalizeTag(parsed.value);
    const excluded = new Set((getExcluded ? getExcluded(value) : []).map(normalizeTag));
    const suggestion = q
      ? allTagPool.find(tag => normalizeTag(tag).startsWith(q) && normalizeTag(tag) !== q && !excluded.has(normalizeTag(tag)))
      : '';
    controller.suggestion = suggestion ? parsed.prefix + suggestion : '';
    ghost.innerHTML = controller.suggestion
      ? `<span class="ghost-hidden">${escHtml(value)}</span>${escHtml(controller.suggestion.slice(value.length))}`
      : '';
  }
  input.addEventListener('input', update);
  input.addEventListener('keydown', e => {
    if ((e.key === 'Tab' || e.key === 'ArrowRight') && controller.suggestion) {
      e.preventDefault();
      input.value = controller.suggestion;
      update();
      return;
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      const value = input.value.trim();
      if (value) {
        onCommit(controller.suggestion && normalizeTag(value) !== normalizeTag(controller.suggestion) ? value : (controller.suggestion || value));
        input.value = '';
        update();
      }
    }
    if (e.key === 'Escape') {
      input.value = '';
      update();
    }
  });
  controller.update = update;
  return controller;
}

let fishControllers = [];
function initFishInputs() {
  fishControllers = [
    initFishInput('preview-tag-input', 'preview-tag-ghost', () => {
      const img = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
      return img ? img.tags || [] : [];
    }, tag => addTagToPreview(tag)),
    initFishInput('tag-create-input', 'tag-create-ghost', () => [], tag => {
      document.getElementById('tag-create-input').value = tag;
      createTagFromManager();
    })
  ];
}

function updateFishGhosts() {
  fishControllers.forEach(controller => controller.update && controller.update());
}

async function openLightbox(idx, persist = true, sourceList = visibleImages) {
  lightboxImages = Array.isArray(sourceList) && sourceList.length ? sourceList : visibleImages;
  if (!lightboxImages[idx]) return;
  togglePreviewTagDropdown(false);
  lbIndex = idx;
  const img = lightboxImages[lbIndex];
  activeTab().lastImageId = img.id;
  renderPreviewMeta(img, true);
  document.body.style.overflow = 'hidden';
  const token = ++previewRequestToken;
  try {
    const canvas = await getOriginalCanvas(img);
    if (token !== previewRequestToken || !lightboxImages[lbIndex] || lightboxImages[lbIndex].id !== img.id) return;
    previewModal.open(canvas);
    renderPreviewMeta(img, false);
    if (persist) saveSessionSoon();
    preloadPreviewNeighbors(idx);
  } catch (e) {
    console.error(e);
    alert('Не удалось открыть изображение: ' + e.message);
  }
}

function closePreview(clearLast = true) {
  suppressPreviewCloseClear = !clearLast;
  if (previewModal && previewModal.isOpen) previewModal.close();
  suppressPreviewCloseClear = false;
  if (clearLast) {
    activeTab().lastImageId = null;
    saveSessionSoon();
  }
}

function handlePreviewClosed() {
  document.body.style.overflow = '';
  togglePreviewTagDropdown(false);
  lightboxImages = [];
  if (!suppressPreviewCloseClear && tabs.length && activeTab().lastImageId) {
    activeTab().lastImageId = null;
    saveSessionSoon();
  }
}

function previewNav(dir) {
  const source = lightboxImages.length ? lightboxImages : visibleImages;
  if (!source.length) return;
  lbIndex = (lbIndex + dir + source.length) % source.length;
  openLightbox(lbIndex, true, source);
}

function renderPreviewMeta(img, loading) {
  const name = fileName(img.path);
  document.getElementById('preview-name').textContent = loading ? `Загрузка: ${name}` : name;
  document.getElementById('preview-size').textContent = `${fmtSize(img.size)} · ${img.width || '?'}×${img.height || '?'}`;
  document.getElementById('preview-open').href = `/file/${img.id}`;
  renderPreviewTags(img);
}

function renderPreviewTags(img) {
  const auto = document.getElementById('preview-auto-tags');
  const user = document.getElementById('preview-user-tags');
  const autoTags = img.auto_tags || img.folder_tags || [];
  const userTags = img.user_tags || [];
  auto.innerHTML = autoTags.map(t => renderTagChip(t, {auto: true, className: 'preview-chip'})).join('');
  user.innerHTML = userTags.map(t => `
    ${renderTagChip(t, {removable: true, className: 'preview-chip'})}
  `).join('');
  user.querySelectorAll('button[data-tag]').forEach(btn => {
    btn.addEventListener('click', () => removeTagFromPreview(btn.dataset.tag));
  });
  renderPreviewTagDropdown();
}

function togglePreviewTagDropdown(force) {
  const dropdown = document.getElementById('preview-tag-dropdown');
  const open = force === undefined ? !dropdown.classList.contains('open') : Boolean(force);
  dropdown.classList.toggle('open', open);
  if (open) renderPreviewTagDropdown();
}

function renderPreviewTagDropdown() {
  const dropdown = document.getElementById('preview-tag-dropdown');
  if (!dropdown) return;
  const img = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
  const used = new Set((img ? img.tags || [] : []).map(normalizeTag));
  const tags = allTagPool.filter(tag => !used.has(normalizeTag(tag)));
  dropdown.innerHTML = tags.length
    ? tags.map(tag => `<button class="preview-pick-tag" type="button" data-tag="${escAttr(tag)}">${renderTagChip(tag, {count: true})}</button>`).join('')
    : `<span class="muted-inline">Нет доступных тегов для добавления</span>`;
  dropdown.querySelectorAll('button[data-tag]').forEach(btn => {
    btn.addEventListener('click', async () => {
      await addTagToPreview(btn.dataset.tag);
      togglePreviewTagDropdown(false);
    });
  });
}

async function addTagFromPreviewInput() {
  const input = document.getElementById('preview-tag-input');
  const value = input.value.trim();
  if (!value) return;
  await addTagToPreview(value);
  input.value = '';
  updateFishGhosts();
}

async function addTagToPreview(tag) {
  const img = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
  if (!img) return;
  const newTags = dedupeDisplayTags([...(img.user_tags || []), findDisplayTag(tag)]);
  await saveTags(img.id, newTags);
}

async function removeTagFromPreview(tag) {
  const img = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
  if (!img) return;
  const newTags = (img.user_tags || []).filter(t => normalizeTag(t) !== normalizeTag(tag));
  await saveTags(img.id, newTags);
}

async function saveTags(id, userTags) {
  try {
    const r = await fetch(`/api/tag/${id}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tags: userTags})
    });
    if (!r.ok) throw new Error(await readError(r));
    const updated = await r.json();
    updateImageTags(id, updated);
    await refreshTagPool();
  } catch (e) {
    console.error(e);
  }
}

function updateImageTags(id, updated) {
  let updatedImage = null;
  for (const list of [allImages, visibleImages]) {
    const img = list.find(item => item.id === id);
    if (!img) continue;
    img.tags = updated.tags || [];
    img.auto_tags = updated.auto_tags || updated.folder_tags || [];
    img.folder_tags = img.auto_tags;
    img.user_tags = updated.user_tags || [];
    updatedImage = img;
  }
  if (updatedImage) {
    setTagPool([...allTagPool, ...(updatedImage.tags || [])]);
    const previewImage = (lightboxImages.length ? lightboxImages : visibleImages)[lbIndex];
    if (previewImage && previewImage.id === id) renderPreviewTags(previewImage);
    updateCardForImage(updatedImage);
    updateFishGhosts();
  }
}

function getOriginalCanvas(img) {
  if (canvasCache.has(img.id)) return canvasCache.get(img.id);
  const promise = new Promise((resolve, reject) => {
    const image = new Image();
    image.decoding = 'async';
    image.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = image.naturalWidth || img.width || 1;
      canvas.height = image.naturalHeight || img.height || 1;
      const ctx = canvas.getContext('2d');
      ctx.drawImage(image, 0, 0);
      resolve(canvas);
      trimCanvasCache();
    };
    image.onerror = () => reject(new Error(img.path));
    image.src = `/file/${img.id}`;
  });
  canvasCache.set(img.id, promise);
  trimCanvasCache();
  return promise;
}

function trimCanvasCache() {
  while (canvasCache.size > MAX_CANVAS_CACHE) {
    const first = canvasCache.keys().next().value;
    canvasCache.delete(first);
  }
}

function preloadPreviewNeighbors(idx) {
  [-2, -1, 1, 2].forEach(offset => {
    const img = visibleImages[idx + offset];
    if (img) getOriginalCanvas(img).catch(() => {});
  });
}

function restorePreviewIfNeeded() {
  const tab = activeTab();
  if (!tab.lastImageId) return;
  const idx = visibleImages.findIndex(img => img.id === tab.lastImageId);
  if (idx >= 0 && !previewModal.isOpen) openLightbox(idx, false);
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (graphState && graphState.open) closeGraph();
    if (document.getElementById('settings-panel').classList.contains('open')) toggleSettings(false);
  }
  if (!previewModal || !previewModal.isOpen) return;
  if (e.target && ['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;
  if (e.key === 'ArrowLeft') previewNav(-1);
  else if (e.key === 'ArrowRight') previewNav(1);
});

async function rescan() {
  try {
    const r = await fetch('/api/rescan', {method: 'POST'});
    if (!r.ok) throw new Error(await readError(r));
    startStatusPolling();
  } catch (e) {
    alert('Ошибка: ' + e.message);
  }
}

function saveSessionSoon() {
  if (sessionSaveTimer) clearTimeout(sessionSaveTimer);
  sessionSaveTimer = setTimeout(saveSession, 250);
}

async function saveSession() {
  saveActiveScroll();
  const tab = activeTab();
  const body = {
    tabs: tabs.map(item => ({
      id: item.id,
      title: item.title,
      includeTags: item.includeTags || [],
      excludeTags: item.excludeTags || [],
      matchMode: item.matchMode === 'all' ? 'all' : 'any',
      lastImageId: item.lastImageId || null,
      scrollTop: item.scrollTop || 0
    })),
    active_tab_id: activeTabId,
    search_tags: tab.includeTags || [],
    last_image_id: tab.lastImageId || null
  };
  try {
    await fetch('/api/session', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
  } catch {}
}

async function loadSession() {
  try {
    const r = await fetch('/api/session');
    if (!r.ok) throw new Error(await readError(r));
    const s = await r.json();
    tabs = normalizeTabs(s.tabs, s);
    activeTabId = tabs.some(tab => tab.id === s.active_tab_id) ? s.active_tab_id : tabs[0].id;
    renderTabs();
    renderFilterControls();
    if (s.root_path) {
      document.getElementById('setup-folder').value = s.root_path;
      document.getElementById('folder-input').value = s.root_path;
      document.getElementById('current-root').textContent = s.root_path;
      showGallery();
      startStatusPolling();
      await refreshImages(true);
      const tab = activeTab();
      setTimeout(() => window.scrollTo({top: tab.scrollTop || 0}), 0);
    } else {
      showSetup();
      pollStatus();
    }
  } catch (e) {
    tabs = [makeDefaultTab()];
    activeTabId = tabs[0].id;
    renderTabs();
    renderFilterControls();
    showSetup();
    setDbStatus(false, e.message);
  }
}

function normalizeTabs(rawTabs, session) {
  if (Array.isArray(rawTabs) && rawTabs.length) {
    return rawTabs.map((tab, i) => ({
      id: String(tab.id || `tab-${i}`),
      title: String(tab.title || 'Все фото'),
      includeTags: dedupeDisplayTags(tab.includeTags || tab.include_tags || []),
      excludeTags: dedupeDisplayTags(tab.excludeTags || tab.exclude_tags || []),
      matchMode: tab.matchMode === 'all' || tab.match_mode === 'all' ? 'all' : 'any',
      lastImageId: tab.lastImageId || tab.last_image_id || null,
      scrollTop: Number(tab.scrollTop || tab.scroll_top || 0)
    }));
  }
  const tab = makeDefaultTab();
  tab.includeTags = dedupeDisplayTags(session.search_tags || []);
  tab.matchMode = session.search_mode === 'all' ? 'all' : 'any';
  tab.lastImageId = session.last_image_id || null;
  updateTabTitle(tab);
  return [tab];
}

function initPreview() {
  const baseClose = PreviewModal.prototype.close;
  PreviewModal.prototype.close = function closeWithAppState() {
    const wasOpen = this.isOpen;
    baseClose.call(this);
    if (wasOpen) handlePreviewClosed();
  };
  previewModal = new PreviewModal();
}

function aspectCss(img) {
  const w = Number(img.width || 0);
  const h = Number(img.height || 0);
  if (w > 0 && h > 0) return `${w} / ${h}`;
  const ratio = Number(img.aspect_ratio || 1);
  return `${Math.max(1, Math.round(ratio * 1000))} / 1000`;
}

function normalizeTag(tag) {
  return String(tag || '').trim().replace(/\s+/g, ' ').toLowerCase();
}

function tagName(tag) {
  if (tag && typeof tag === 'object') return String(tag.name || '').trim().replace(/\s+/g, ' ');
  return String(tag || '').trim().replace(/\s+/g, ' ');
}

function setTagPool(rawTags) {
  const previous = allTagMeta;
  const nextMeta = new Map();
  const names = [];
  for (const item of rawTags || []) {
    const name = tagName(item);
    const norm = normalizeTag(name);
    if (!name || nextMeta.has(norm)) continue;
    const old = previous.get(norm) || {};
    const meta = typeof item === 'object' && item !== null
      ? {
        name,
        normalized: item.normalized || norm,
        color: item.color || null,
        image_count: Number(item.image_count || 0),
        auto_count: Number(item.auto_count || 0),
        user_count: Number(item.user_count || 0),
        is_auto: Boolean(item.is_auto || Number(item.auto_count || 0) > 0)
      }
      : {
        name,
        normalized: norm,
        color: old.color || null,
        image_count: Number(old.image_count || 0),
        auto_count: Number(old.auto_count || 0),
        user_count: Number(old.user_count || 0),
        is_auto: Boolean(old.is_auto)
      };
    nextMeta.set(norm, meta);
    names.push(name);
  }
  allTagMeta = nextMeta;
  allTagPool = names;
}

function getTagMeta(tag) {
  const name = tagName(tag);
  const norm = normalizeTag(name);
  return allTagMeta.get(norm) || {
    name,
    normalized: norm,
    color: null,
    image_count: 0,
    auto_count: 0,
    user_count: 0,
    is_auto: false
  };
}

function normalizeHexColor(value, fallback = '#D4D4D4') {
  const color = String(value || '').trim();
  return /^#[0-9a-fA-F]{6}$/.test(color) ? color.toUpperCase() : fallback;
}

function hexToRgbTriplet(hex) {
  const color = normalizeHexColor(hex);
  const value = color.slice(1);
  return [
    parseInt(value.slice(0, 2), 16),
    parseInt(value.slice(2, 4), 16),
    parseInt(value.slice(4, 6), 16)
  ].join(', ');
}

function renderTagChip(tag, options = {}) {
  const name = tagName(tag);
  const meta = getTagMeta(name);
  const color = normalizeHexColor(meta.color, '#D4D4D4');
  const rgb = hexToRgbTriplet(color);
  const classes = [
    'tag-chip-base',
    options.className || '',
    meta.color ? 'has-color' : '',
    options.exclude ? 'is-exclude' : ''
  ].filter(Boolean).join(' ');
  const label = `${options.exclude ? '!' : ''}${name}`;
  const count = options.count ? `<span class="tag-count">${Number(meta.image_count || 0)}</span>` : '';
  const remove = options.removable
    ? `<button type="button" data-kind="${escAttr(options.actionKind || '')}" data-tag="${escAttr(name)}">×</button>`
    : '';
  return `<span class="${classes}" style="--tag-color:${escAttr(color)};--tag-rgb:${escAttr(rgb)}" title="${escAttr(label)}"><span class="tag-label">${escHtml(label)}</span>${count}${remove}</span>`;
}

function splitTagPrefix(value) {
  const raw = String(value || '').trim().replace(/\s+/g, ' ');
  if (raw.startsWith('!') || raw.startsWith('-')) {
    return {prefix: raw[0], value: raw.slice(1).trim()};
  }
  return {prefix: '', value: raw};
}

function dedupeDisplayTags(tags) {
  const seen = new Set();
  const result = [];
  for (const tag of tags || []) {
    const value = tagName(tag);
    const norm = normalizeTag(value);
    if (!value || seen.has(norm)) continue;
    seen.add(norm);
    result.push(value);
  }
  return result;
}

function findDisplayTag(tag) {
  const value = tagName(tag);
  const norm = normalizeTag(value);
  return allTagPool.find(item => normalizeTag(item) === norm) || value;
}

function cssEscape(value) {
  if (window.CSS && typeof window.CSS.escape === 'function') return window.CSS.escape(value);
  return String(value).replace(/"/g, '\\"');
}

function escHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escAttr(s) {
  return escHtml(s);
}

function fileName(path) {
  return String(path || '').split(/[/\\]/).pop() || '';
}

function fmtSize(bytes) {
  if (!Number.isFinite(Number(bytes))) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1048576).toFixed(1) + ' MB';
}

async function readError(response) {
  try {
    const data = await response.json();
    return data.detail || data.error || response.statusText;
  } catch {
    return response.statusText;
  }
}

document.getElementById('tag-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('tag-overlay')) closeTagManager();
});

document.getElementById('settings-panel').addEventListener('click', e => {
  if (e.target === document.getElementById('settings-panel')) toggleSettings(false);
});

document.getElementById('graph-overlay').addEventListener('click', e => {
  if (e.target === document.getElementById('graph-overlay')) closeGraph();
});

document.addEventListener('click', e => {
  const settings = document.getElementById('settings-panel');
  const settingsToggle = document.getElementById('settings-toggle');
  if (settings.classList.contains('open') && !settings.contains(e.target) && e.target !== settingsToggle && !settingsToggle.contains(e.target)) {
    toggleSettings(false);
  }
  const filterWrap = document.querySelector('.filter-input-wrap');
  if (filterWrap && !filterWrap.contains(e.target)) {
    filterSuggestionOpen = false;
    renderFilterSuggestions();
  }
  const previewTools = document.querySelector('.preview-user-tools');
  if (previewTools && !previewTools.contains(e.target)) togglePreviewTagDropdown(false);
});

document.getElementById('tag-admin-search').addEventListener('input', renderTagAdmin);
document.getElementById('tag-admin-create').addEventListener('keydown', e => {
  if (e.key === 'Enter') createTagFromSettings();
});
window.addEventListener('scroll', handleChromeScroll, {passive: true});
document.addEventListener('scroll', handleChromeScroll, {passive: true, capture: true});
window.addEventListener('wheel', () => requestAnimationFrame(handleChromeScroll), {passive: true});
window.addEventListener('touchmove', () => requestAnimationFrame(handleChromeScroll), {passive: true});
window.addEventListener('resize', () => {
  if (graphState && graphState.open) rebuildGraph();
});

initPreview();
initFilterInput();
initFishInputs();
updateScrollTopButton();
handleChromeScroll();
loadSession();
