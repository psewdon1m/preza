"use strict";
const $ = (id) => document.getElementById(id);
let csrf = "", presentations = [], selected = null, previewIndex = 0;
let busy = false, previewBusy = false, slugTouched = false;
let updateState = null, updateBusy = false, updateInProgress = false, updateTimer = null;
let pendingUpdate = null;
try { pendingUpdate = JSON.parse(localStorage.getItem("preza-update-request") || "null"); } catch { /* Storage is optional. */ }

function message(text = "", error = false, target = "message") {
  $(target).textContent = text;
  $(target).classList.toggle("error", error);
}

function showLogin() {
  clearTimeout(updateTimer);
  csrf = "";
  selected = null;
  presentations = [];
  $("admin-app").hidden = true;
  $("login-panel").hidden = false;
  $("preview-image").removeAttribute("src");
  $("presentation-list").replaceChildren();
  $("presentation-form").reset();
  $("editor-content").hidden = true;
  $("preview-panel").hidden = true;
  $("welcome").hidden = false;
  message();
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401 && !path.endsWith("/login")) showLogin();
    throw new Error(typeof data.detail === "string" ? data.detail : "Не удалось выполнить запрос. Проверьте заполненные поля.");
  }
  return data;
}

function setBusy(value) {
  busy = value;
  const disabled = value || updateInProgress;
  $("presentation-form").querySelectorAll("button, input, select").forEach((el) => { el.disabled = disabled; });
  $("new-btn").disabled = disabled;
  $("welcome-new-btn").disabled = disabled;
  $("logout-btn").disabled = disabled;
  renderPreview();
  renderUpdateButtons();
}

function rememberUpdate(request) {
  pendingUpdate = request;
  try {
    if (request) localStorage.setItem("preza-update-request", JSON.stringify(request));
    else localStorage.removeItem("preza-update-request");
  } catch { /* The current page can still follow the update without storage. */ }
}

function renderUpdateButtons() {
  const disabled = busy || previewBusy || updateBusy || updateInProgress || !updateState?.enabled;
  $("update-check").disabled = disabled;
  $("update-install").disabled = disabled;
  $("update-install").hidden = !updateState?.update_available;
  if (updateState?.latest_version) $("update-install").textContent = "Обновить до " + updateState.latest_version;
  $("update-refresh").hidden = updateState?.job?.state !== "completed";
}

function scheduleUpdatePoll() {
  clearTimeout(updateTimer);
  if (csrf) updateTimer = setTimeout(loadUpdates, 2000);
}

function applyUpdateState(data) {
  updateState = data;
  updateInProgress = data.updating;
  $("update-current-version").textContent = "Версия " + data.current_version;
  let text = "Нажмите «Проверить обновления», чтобы найти новую версию.";
  let error = false;
  if (!data.enabled) {
    rememberUpdate(null);
    text = "Служба обновления не подключена. Установите серверную версию Preza с поддержкой обновлений.";
  }
  else if (data.updating) text = data.job?.message || "Выполняется обновление. Ожидаем перезапуска сайта…";
  else if (data.job && ["completed", "failed", "recovery_required"].includes(data.job.state)) {
    text = data.job.message;
    error = data.job.state !== "completed";
  } else if (data.checked_at) text = data.update_available ? "Доступна версия " + data.latest_version + "." : "Новых версий пока нет.";
  message(text, error, "update-message");
  if (pendingUpdate && data.job?.id === pendingUpdate.request_id && !data.updating) {
    rememberUpdate(null);
    loadList().catch(() => {});
  }
  setBusy(busy);
  if (data.updating || pendingUpdate) scheduleUpdatePoll();
}

async function loadUpdates() {
  try {
    const data = await api("/api/admin/updates");
    if (pendingUpdate && data.enabled && !data.updating && data.job?.id !== pendingUpdate.request_id) {
      // Repeating the same request is safe if the original response was lost during restart.
      if (data.current_version === pendingUpdate.version) rememberUpdate(null);
      else {
        try {
          const resumed = await api("/api/admin/updates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pendingUpdate) });
          applyUpdateState(resumed);
          return;
        } catch (err) {
          rememberUpdate(null);
          applyUpdateState(data);
          message(err.message, true, "update-message");
          return;
        }
      }
    }
    applyUpdateState(data);
  } catch (err) {
    message(updateInProgress || pendingUpdate ? "Ожидаем возвращения сайта после перезапуска…" : err.message, !(updateInProgress || pendingUpdate), "update-message");
    if (updateInProgress || pendingUpdate) scheduleUpdatePoll();
  }
}

async function resumeUpdates() {
  if (pendingUpdate) {
    $("updates-panel").hidden = false;
    $("updates-toggle").setAttribute("aria-expanded", "true");
  }
  await loadUpdates();
}

$("updates-toggle").addEventListener("click", () => {
  $("updates-panel").hidden = !$("updates-panel").hidden;
  $("updates-toggle").setAttribute("aria-expanded", String(!$("updates-panel").hidden));
  if (!$("updates-panel").hidden) loadUpdates();
});
$("update-check").addEventListener("click", async () => {
  updateBusy = true;
  renderUpdateButtons();
  message("Проверяем опубликованные версии…", false, "update-message");
  try {
    const data = await api("/api/admin/updates/check", { method: "POST" });
    applyUpdateState(data);
    if (!data.updating) message(data.update_available ? "Доступна версия " + data.latest_version + "." : "Новых версий пока нет.", false, "update-message");
  } catch (err) { message(err.message, true, "update-message"); }
  finally { updateBusy = false; renderUpdateButtons(); }
});
$("update-install").addEventListener("click", async () => {
  if (!updateState?.update_available || updateBusy || busy || previewBusy || updateInProgress) return;
  rememberUpdate({ version: updateState.latest_version, request_id: crypto.randomUUID() });
  updateBusy = true;
  updateInProgress = true;
  setBusy(busy);
  message("Запрашиваем обновление…", false, "update-message");
  try {
    applyUpdateState(await api("/api/admin/updates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pendingUpdate) }));
  } catch { scheduleUpdatePoll(); }
  finally { updateBusy = false; renderUpdateButtons(); }
});
$("update-refresh").addEventListener("click", () => location.reload());

function renderList() {
  $("library-count").textContent = String(presentations.length);
  const query = $("search").value.toLocaleLowerCase("ru");
  const filtered = presentations.filter((item) => (item.title + " " + item.slug).toLocaleLowerCase("ru").includes(query));
  $("presentation-list").replaceChildren();
  if (!filtered.length) {
    const empty = document.createElement("p");
    empty.className = "library-empty";
    empty.textContent = presentations.length ? "Ничего не найдено" : "Здесь появятся ваши презентации";
    $("presentation-list").append(empty);
  }
  filtered.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "presentation-item" + (selected?.id === item.id ? " selected" : "");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const detail = document.createElement("span");
    const plural = new Intl.PluralRules("ru").select(item.slide_count);
    const word = {one:"слайд", few:"слайда", many:"слайдов", other:"слайда"}[plural];
    detail.textContent = item.slide_count + " " + word + " · /p/" + item.slug;
    button.append(title, detail);
    if (item.live_active || item.password_required) {
      const badge = document.createElement("small");
      badge.textContent = [item.live_active ? "● Идет показ" : "", item.password_required ? "По паролю" : ""].filter(Boolean).join(" · ");
      button.append(badge);
    }
    button.addEventListener("click", () => { if (!busy && !previewBusy) openEditor(item.id); });
    $("presentation-list").append(button);
  });
}

async function loadList() {
  presentations = (await api("/api/admin/presentations")).presentations;
  renderList();
}

function remember(record) {
  selected = record;
  const index = presentations.findIndex((item) => item.id === record.id);
  if (index >= 0) presentations[index] = record;
  else presentations.unshift(record);
  renderList();
}

function updatePasswordField() {
  const showing = $("password-action").value === "set";
  $("password-field").hidden = !showing;
  $("presentation-password").required = showing;
}

function populate(record) {
  selected = record;
  previewIndex = record?.current_slide || 0;
  slugTouched = Boolean(record);
  $("welcome").hidden = true;
  $("editor-content").hidden = false;
  $("presentation-form").reset();
  $("title").value = record?.title || "";
  $("slug").value = record?.slug || crypto.randomUUID().replaceAll("-", "").slice(0, 12);
  $("default-mode").value = record?.default_mode || "scroll";
  $("allow-switch").checked = record ? record.allow_switch : true;
  $("password-action").querySelector('[value="keep"]').hidden = !record?.password_required;
  $("password-action").value = record?.password_required ? "keep" : "remove";
  updatePasswordField();
  $("editor-heading").textContent = record ? "Настройки презентации" : "Новая презентация";
  $("save-btn").textContent = record ? "Сохранить изменения" : "Опубликовать";
  $("file-label").textContent = record ? "Заменить PDF" : "PDF-презентация";
  $("file-hint").textContent = record ? "Оставьте пустым, чтобы сохранить PDF. Замена файла завершит текущий показ." : "До 256 МБ и 500 страниц.";
  $("pdf-file").required = !record;
  $("start-live-field").hidden = Boolean(record);
  ["open-link", "copy-btn", "delete-btn", "preview-panel"].forEach((id) => { $(id).hidden = !record; });
  if (record) $("open-link").href = record.url;
  renderPreview();
  renderList();
}

async function openEditor(id) {
  try { message(); populate(await api("/api/admin/presentations/" + id)); }
  catch (err) { message(err.message, true); }
}

function newPresentation() {
  if (busy || previewBusy || updateInProgress) return;
  populate(null);
  message();
  $("title").focus();
}

function renderPreview() {
  if (!selected) { $("live-badge").hidden = true; return; }
  $("live-badge").hidden = !selected.live_active;
  $("live-btn").textContent = selected.live_active ? "Завершить показ" : "Начать показ";
  $("live-btn").classList.toggle("primary", !selected.live_active);
  $("live-btn").disabled = busy || previewBusy || updateInProgress;
  $("preview-hint").textContent = selected.live_active
    ? "Зрители видят этот слайд. Используйте кнопки или стрелки клавиатуры, чтобы продолжить показ."
    : "Слайды переключаются только в превью. Начните показ, чтобы управлять просмотром зрителей.";
  const src = selected.preview_base + previewIndex;
  if ($("preview-image").getAttribute("src") !== src) $("preview-image").src = src;
  $("preview-image").alt = "Слайд " + (previewIndex + 1);
  $("preview-count").textContent = (previewIndex + 1) + " / " + selected.slide_count;
  $("preview-prev").disabled = busy || previewBusy || previewIndex <= 0;
  $("preview-next").disabled = busy || previewBusy || previewIndex >= selected.slide_count - 1;
}

async function controlLive(active, index) {
  if (!selected || busy || previewBusy || updateInProgress) return;
  previewBusy = true;
  renderPreview();
  try {
    remember(await api("/api/admin/presentations/" + selected.id + "/live", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ active, slide: index }),
    }));
    previewIndex = index;
    message(active ? "Показ идет. Зрители видят слайд " + (index + 1) : "Показ завершен. Зрители могут смотреть презентацию самостоятельно.");
  } catch (err) { message(err.message, true); }
  finally { previewBusy = false; renderPreview(); }
}

function movePreview(delta) {
  if (!selected || busy || previewBusy) return;
  const index = Math.max(0, Math.min(selected.slide_count - 1, previewIndex + delta));
  if (index === previewIndex) return;
  if (selected.live_active) controlLive(true, index);
  else { previewIndex = index; renderPreview(); }
}

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("login-form").querySelector("button");
  button.disabled = true;
  message("Вход…", false, "login-message");
  try {
    const data = await api("/api/admin/login", { method: "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ key: $("access-key").value }) });
    csrf = data.csrf;
    $("access-key").value = "";
    $("login-panel").hidden = true;
    $("admin-app").hidden = false;
    await loadList();
    await resumeUpdates();
    message("", false, "login-message");
  } catch (err) { message(err.message, true, "login-message"); }
  finally { button.disabled = false; }
});

$("presentation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (busy || previewBusy || updateInProgress) return;
  const data = new FormData();
  const creating = !selected;
  data.set("title", $("title").value);
  data.set("slug", $("slug").value);
  data.set("default_mode", $("default-mode").value);
  data.set("allow_switch", String($("allow-switch").checked));
  const action = $("password-action").value;
  data.set("password", action === "set" ? $("presentation-password").value : "");
  if (!creating) {
    data.set("password_action", action);
    data.set("expected_revision", String(selected.revision));
  }
  if ($("pdf-file").files[0]) data.set("file", $("pdf-file").files[0]);
  const startLive = creating && $("start-live").checked;
  setBusy(true);
  message($("pdf-file").files[0] ? "Загрузка и подготовка слайдов… Это может занять некоторое время." : "Сохранение…");
  try {
    const record = await api("/api/admin/presentations" + (creating ? "" : "/" + selected.id), {
      method: creating ? "POST" : "PUT", body: data,
    });
    remember(record);
    populate(record);
    message(creating ? "Презентация опубликована. Ссылка готова к отправке." : "Изменения сохранены.");
    setBusy(false);
    if (startLive) await controlLive(true, 0);
  } catch (err) { message(err.message, true); }
  finally { setBusy(false); }
});

$("delete-btn").addEventListener("click", async () => {
  if (!selected || busy || previewBusy || !confirm("Удалить «" + selected.title + "» и все ее слайды?")) return;
  setBusy(true);
  try {
    await api("/api/admin/presentations/" + selected.id, { method: "DELETE" });
    presentations = presentations.filter((item) => item.id !== selected.id);
    selected = null;
    $("preview-image").removeAttribute("src");
    $("editor-content").hidden = true;
    $("welcome").hidden = false;
    renderList();
    message("Презентация удалена.");
  } catch (err) { message(err.message, true); }
  finally { setBusy(false); }
});

$("copy-btn").addEventListener("click", async () => {
  if (!selected) return;
  const url = new URL(selected.url, location.origin).href;
  try { await navigator.clipboard.writeText(url); message("Ссылка скопирована."); }
  catch { prompt("Ссылка на презентацию", url); }
});
$("logout-btn").addEventListener("click", async () => {
  try { await api("/api/admin/logout", { method: "POST" }); showLogin(); }
  catch (err) { message(err.message, true); }
});
$("new-btn").addEventListener("click", newPresentation);
$("welcome-new-btn").addEventListener("click", newPresentation);
$("search").addEventListener("input", renderList);
$("password-action").addEventListener("change", updatePasswordField);
$("slug").addEventListener("input", () => { slugTouched = true; });
$("title").addEventListener("input", () => {
  if (slugTouched) return;
  const slug = $("title").value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80).replace(/-$/g, "");
  if (slug) $("slug").value = slug;
});
$("preview-prev").addEventListener("click", () => movePreview(-1));
$("preview-next").addEventListener("click", () => movePreview(1));
$("live-btn").addEventListener("click", () => controlLive(!selected.live_active, previewIndex));
window.addEventListener("keydown", (event) => {
  if (event.target.closest("input, textarea, select, [contenteditable]")) return;
  if (selected && ["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    movePreview(event.key === "ArrowRight" ? 1 : -1);
  }
});

(async () => {
  try {
    const data = await api("/api/admin/session");
    if (data.authenticated) {
      csrf = data.csrf;
      $("login-panel").hidden = true;
      $("admin-app").hidden = false;
      await loadList();
      await resumeUpdates();
    }
  } catch (err) { message(err.message, true, "login-message"); }
})();
