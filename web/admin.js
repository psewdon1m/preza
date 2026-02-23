const uploadForm = document.getElementById("upload-form");
const fileInput = document.getElementById("pdf-file");
const messageEl = document.getElementById("message");
const summaryEl = document.getElementById("summary");
const slideListEl = document.getElementById("slide-list");
const refreshBtn = document.getElementById("refresh-btn");
const deleteBtn = document.getElementById("delete-btn");

function setMessage(text, isError = false) {
  messageEl.textContent = text;
  messageEl.classList.toggle("error", isError);
}

function setBusy(isBusy) {
  uploadForm.querySelector("button[type='submit']").disabled = isBusy;
  refreshBtn.disabled = isBusy;
  deleteBtn.disabled = isBusy;
}

function fileNameFromPath(path) {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

async function loadState() {
  setMessage("");
  const res = await fetch("/api/slides", { cache: "no-store" });
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}`);
  }

  const data = await res.json();
  const count = data.count || 0;
  const has = Boolean(data.hasPresentation);

  summaryEl.textContent = has
    ? `Загружена презентация, слайдов: ${count}`
    : "Презентация не загружена";

  slideListEl.innerHTML = "";
  (data.slides || []).forEach((slidePath) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = slidePath;
    a.target = "_blank";
    a.rel = "noopener";
    a.textContent = fileNameFromPath(slidePath);
    li.appendChild(a);
    slideListEl.appendChild(li);
  });
}

async function uploadPdf(event) {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    setMessage("Выберите PDF файл", true);
    return;
  }

  const formData = new FormData();
  formData.append("file", file);

  setBusy(true);
  setMessage("Загрузка и конвертация PDF...");
  try {
    const res = await fetch("/api/upload", {
      method: "POST",
      body: formData,
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    fileInput.value = "";
    setMessage("Презентация загружена");
    await loadState();
  } catch (err) {
    setMessage(err.message || "Ошибка загрузки", true);
  } finally {
    setBusy(false);
  }
}

async function deletePresentation() {
  if (!confirm("Удалить текущую презентацию и все слайды?")) {
    return;
  }

  setBusy(true);
  setMessage("Удаление...");
  try {
    const res = await fetch("/api/presentation", { method: "DELETE" });
    if (!res.ok) {
      throw new Error(`HTTP ${res.status}`);
    }
    setMessage("Презентация удалена");
    await loadState();
  } catch (err) {
    setMessage(err.message || "Ошибка удаления", true);
  } finally {
    setBusy(false);
  }
}

uploadForm.addEventListener("submit", uploadPdf);
refreshBtn.addEventListener("click", async () => {
  try {
    setBusy(true);
    await loadState();
    setMessage("Состояние обновлено");
  } catch (err) {
    setMessage(err.message || "Ошибка обновления", true);
  } finally {
    setBusy(false);
  }
});
deleteBtn.addEventListener("click", deletePresentation);

(async () => {
  try {
    await loadState();
  } catch (err) {
    setMessage(err.message || "Ошибка загрузки состояния", true);
  }
})();

