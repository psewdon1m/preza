const viewer = document.getElementById("viewer");
const statusEl = document.getElementById("status");
let lastSignature = "";

function setStatus(text) {
  if (!statusEl) return;
  statusEl.textContent = text;
}

function buildSignature(slides) {
  return slides.join("|");
}

function renderSlides(slides) {
  const signature = buildSignature(slides);
  if (signature === lastSignature) return;
  lastSignature = signature;

  viewer.innerHTML = "";

  if (!slides.length) {
    const empty = document.createElement("div");
    empty.className = "status";
    empty.textContent = "Презентация не загружена";
    viewer.appendChild(empty);
    return;
  }

  slides.forEach((src, index) => {
    const img = document.createElement("img");
    img.className = "slide";
    img.src = `${src}?v=${Date.now()}`;
    img.alt = `Слайд ${index + 1}`;
    img.loading = index < 2 ? "eager" : "lazy";
    viewer.appendChild(img);
  });
}

async function refreshViewer() {
  try {
    setStatus("Загрузка...");
    const res = await fetch("/api/slides", { cache: "no-store" });
    const data = await res.json();
    renderSlides(data.slides || []);
  } catch (err) {
    viewer.innerHTML = "";
    const error = document.createElement("div");
    error.className = "status";
    error.textContent = "Ошибка загрузки презентации";
    viewer.appendChild(error);
  }
}

refreshViewer();
setInterval(refreshViewer, 15000);
window.addEventListener("focus", refreshViewer);

