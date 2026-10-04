"use strict";
const $ = (id) => document.getElementById(id);
let slug = decodeURIComponent(location.pathname.split("/").filter(Boolean)[1] || "");
let presentation = null, events = null, mode = "scroll", index = 0;
let rendered = "", connected = true, loading = false, deleted = false;
let imageRetry = null;

function closeEvents() {
  if (events) events.close();
  events = null;
}

function showStatus(text) {
  closeEvents();
  document.body.classList.remove("individual-view");
  presentation = null;
  rendered = "";
  $("viewer").replaceChildren();
  ["viewer", "viewer-toolbar", "viewer-controls", "access-panel"].forEach((id) => { $(id).hidden = true; });
  $("view-status").hidden = false;
  $("view-status").textContent = text;
}

function showAccess(data) {
  showStatus("");
  $("view-status").hidden = true;
  $("access-panel").hidden = false;
  $("access-title").textContent = data.title;
  document.title = data.title + " — Preza";
}

function storedMode(id) {
  try { return sessionStorage.getItem("preza-mode-" + id); }
  catch { return null; }
}

function saveMode() {
  try { sessionStorage.setItem("preza-mode-" + presentation.id, mode); }
  catch { /* Storage may be disabled in private browser policies. */ }
}

function applyState(data) {
  if (presentation?.id === data.id && data.revision < presentation.revision) return;
  const previous = presentation;
  if (!previous || previous.id !== data.id) {
    const saved = storedMode(data.id);
    mode = data.allow_switch && ["scroll", "slides"].includes(saved) ? saved : data.default_mode;
    index = 0;
  } else if (previous.content_version !== data.content_version) {
    index = 0;
    rendered = "";
  }
  if (!data.allow_switch || (previous && previous.default_mode !== data.default_mode)) mode = data.default_mode;
  presentation = data;
  if (data.live_active) index = data.current_slide;
  index = Math.max(0, Math.min(index, data.slide_count - 1));
  if (slug !== data.slug) {
    slug = data.slug;
    history.replaceState(null, "", data.url);
  }
  document.title = data.title + " — Preza";
  $("view-title").textContent = data.title;
  $("access-panel").hidden = true;
  $("view-status").hidden = true;
  $("viewer").hidden = false;
  $("viewer-toolbar").hidden = false;
  render();
}

function render() {
  if (!presentation) return;
  const live = presentation.live_active;
  const displayMode = live ? "slides" : mode;
  $("mode-toggle").hidden = live || !presentation.allow_switch;
  $("mode-toggle").disabled = !connected;
  $("mode-toggle").textContent = mode === "scroll" ? "Отдельные слайды" : "Непрерывная лента";
  $("show-status").hidden = !live;
  $("viewer-controls").hidden = displayMode !== "slides";
  $("prev-slide").disabled = live || !connected || index <= 0;
  $("next-slide").disabled = live || !connected || index >= presentation.slide_count - 1;
  $("slide-count").textContent = (index + 1) + " / " + presentation.slide_count;
  document.body.classList.toggle("individual-view", displayMode === "slides");
  $("viewer").classList.toggle("individual", displayMode === "slides");
  const signature = [presentation.id, presentation.content_version, displayMode, live,
    displayMode === "slides" ? index : ""].join("|");
  if (signature === rendered) return;
  rendered = signature;
  $("viewer").replaceChildren();
  const indices = displayMode === "slides" ? [index] : Array.from({ length: presentation.slide_count }, (_, n) => n);
  indices.forEach((number) => {
    const image = document.createElement("img");
    image.className = "slide";
    image.src = presentation.slide_base + number;
    image.alt = "Слайд " + (number + 1);
    image.loading = displayMode === "slides" || number < 2 ? "eager" : "lazy";
    image.addEventListener("error", () => {
      if (image.isConnected) {
        rendered = "";
        $("connection-status").textContent = "Обновляем слайды…";
        if (!imageRetry) imageRetry = setTimeout(() => {
          imageRetry = null;
          refresh();
        }, 3000);
      }
    }, { once: true });
    $("viewer").append(image);
  });
  if (displayMode === "slides") window.scrollTo({ top: 0, behavior: "instant" });
}

function connectEvents(id) {
  if (events) return;
  const source = new EventSource("/api/p/" + id + "/events");
  events = source;
  source.addEventListener("state", (event) => {
    if (events !== source) return;
    connected = true;
    $("connection-status").textContent = "";
    applyState(JSON.parse(event.data));
  });
  source.addEventListener("locked", () => {
    if (events === source) { closeEvents(); refresh(); }
  });
  source.addEventListener("deleted", () => {
    if (events !== source) return;
    deleted = true;
    showStatus("Презентация удалена");
  });
  source.onopen = () => {
    if (events !== source) return;
    connected = true;
    $("connection-status").textContent = "";
    render();
  };
  source.onerror = () => {
    if (events !== source) return;
    connected = false;
    $("connection-status").textContent = "Восстанавливаем связь…";
    render();
  };
}

async function refresh() {
  if (loading || deleted) return;
  loading = true;
  try {
    const response = await fetch("/api/public/presentations/" + encodeURIComponent(slug), { cache: "no-store" });
    if (!response.ok) {
      if (response.status === 404) { deleted = true; showStatus("Презентация не найдена"); return; }
      throw new Error("Не удалось загрузить презентацию");
    }
    const data = await response.json();
    if (data.locked) { showAccess(data); return; }
    applyState(data);
    if (events?.readyState === EventSource.CLOSED) closeEvents();
    connectEvents(data.id);
  } catch (err) {
    if (presentation) {
      connected = false;
      $("connection-status").textContent = "Восстанавливаем связь…";
      render();
    } else {
      $("view-status").hidden = false;
      $("view-status").textContent = "Ошибка загрузки. Повторяем попытку…";
    }
  } finally { loading = false; }
}

function move(delta) {
  if (!presentation || presentation.live_active || !connected || mode !== "slides") return;
  index = Math.max(0, Math.min(presentation.slide_count - 1, index + delta));
  render();
}

$("mode-toggle").addEventListener("click", () => {
  if (!presentation?.allow_switch || presentation.live_active || !connected) return;
  if (mode === "scroll") {
    const images = [...$("viewer").querySelectorAll("img")];
    const visible = images.findIndex((image) => image.getBoundingClientRect().bottom > 70);
    if (visible >= 0) index = visible;
    mode = "slides";
  } else mode = "scroll";
  saveMode();
  render();
  if (mode === "scroll") $("viewer").children[index]?.scrollIntoView({ block: "start" });
});
$("prev-slide").addEventListener("click", () => move(-1));
$("next-slide").addEventListener("click", () => move(1));
window.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (presentation && (presentation.live_active || mode === "slides") && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    move(event.key === "ArrowRight" ? 1 : -1);
  }
});
$("access-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("access-form").querySelector("button");
  button.disabled = true;
  $("access-message").textContent = "Проверка…";
  $("access-message").classList.remove("error");
  try {
    const response = await fetch("/api/public/presentations/" + encodeURIComponent(slug) + "/unlock", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("password").value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Не удалось открыть презентацию");
    $("password").value = "";
    $("access-message").textContent = "";
    await refresh();
  } catch (err) {
    $("access-message").textContent = err.message;
    $("access-message").classList.add("error");
  } finally { button.disabled = false; }
});
window.addEventListener("focus", refresh);
setInterval(() => { if (!events || !connected) refresh(); }, 10000);
refresh();
