let appMode = "model"; // model | texture | pack
let refImages = [];
let activeJobId = "";
let pollTimer = null;
let genStartTime = 0;
let genClockTimer = null;
let loadedPartialForTask = "";
let selectedWizardPath = "";
let wizInstallTimer = null;
let currentSettings = {};

async function api(url, opts = {}) {
  const r = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  return await r.json();
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

// ---------------- STARTUP WIZARD (2 STEPS) ----------------
let _wizAiReady = false;

function showWizStep(step) {
  document.getElementById("wizStep1").classList.toggle("hidden", step !== 1);
  document.getElementById("wizStep2").classList.toggle("hidden", step !== 2);
  document.getElementById("wizStep1Tab").classList.toggle("active", step === 1);
  document.getElementById("wizStep2Tab").classList.toggle("active", step === 2);
}

function setWizAuthMode(mode) {
  const isCookie = mode === "gemini_cookies";
  document.getElementById("wizAuthCookieTab").classList.toggle("active", isCookie);
  document.getElementById("wizAuthApiTab").classList.toggle("active", !isCookie);
  document.getElementById("wizCookiePane").classList.toggle("hidden", !isCookie);
  document.getElementById("wizApiPane").classList.toggle("hidden", isCookie);
}

function fillProxyFields(typeId, addrId, proxyUrl) {
  const s = (proxyUrl || "").trim();
  const tEl = document.getElementById(typeId);
  const aEl = document.getElementById(addrId);
  if (!s) {
    tEl.value = "none";
    aEl.value = "";
    return;
  }
  if (s.startsWith("socks")) {
    tEl.value = "socks5";
    aEl.value = s.replace(/^socks5h?:\/\//i, "");
  } else {
    tEl.value = "http";
    aEl.value = s.replace(/^https?:\/\//i, "");
  }
}

function readProxyPayload(typeId, addrId) {
  const proxy_type = document.getElementById(typeId).value;
  const proxy_raw = document.getElementById(addrId).value.trim();
  return { proxy_type, proxy_raw };
}

async function testProxy(typeId, addrId, msgId, btn) {
  const { proxy_type, proxy_raw } = readProxyPayload(typeId, addrId);
  const msgEl = document.getElementById(msgId);
  if (proxy_type === "none" || !proxy_raw) {
    msgEl.style.color = "var(--amber)";
    msgEl.textContent = "Выберите тип и укажите адрес";
    return;
  }
  btn.disabled = true;
  msgEl.style.color = "var(--muted)";
  msgEl.textContent = "Проверка...";
  try {
    const res = await api("/api/proxy/test", { method: "POST", body: JSON.stringify({ proxy_type, proxy_raw }) });
    if (res.ok) {
      msgEl.style.color = "var(--green)";
      msgEl.textContent = `OK · IP: ${res.ip} · ${res.latency_ms} мс`;
    } else {
      msgEl.style.color = "var(--red)";
      msgEl.textContent = res.error || "Ошибка";
    }
  } finally {
    btn.disabled = false;
  }
}

async function saveProxyFromSettings() {
  const { proxy_type, proxy_raw } = readProxyPayload("setProxyType", "setProxyAddr");
  const res = await api("/api/settings", { method: "POST", body: JSON.stringify({ proxy_type, proxy_raw }) });
  const msgEl = document.getElementById("setProxyMsg");
  msgEl.style.color = "var(--green)";
  msgEl.textContent = res.settings?.proxy_url ? `Сохранено: ${res.settings.proxy_url}` : "Прокси отключён";
  await checkRuntime();
}

async function openWizard(allowClose = false, startStep = 1) {
  document.getElementById("wizCloseBtn").classList.toggle("hidden", !allowClose);
  const s = (await api("/api/settings")).settings || {};
  document.getElementById("wizApiUrl").value = s.text_base_url || "https://api.openai.com/v1";
  document.getElementById("wizApiKey").value = s.text_api_key || "";
  fillProxyFields("wizProxyType", "wizProxyAddr", s.proxy_url || "");
  document.getElementById("wizProxyMsg").textContent = "";
  document.getElementById("wizCookieErr").textContent = "";
  showWizStep(startStep);
  document.getElementById("wizardModal").classList.add("open");
  await rescanBlender();
}

function closeWizard() {
  document.getElementById("wizardModal").classList.remove("open");
}

async function rescanBlender() {
  const stEl = document.getElementById("wizScanState");
  const listEl = document.getElementById("wizFoundList");
  const nfEl = document.getElementById("wizNotFoundBox");
  const useBtn = document.getElementById("wizUseBlenderBtn");
  const instBtn = document.getElementById("wizInstallBpyBtn");

  stEl.textContent = "Поиск установленного Blender (Steam, реестр, стандартные папки)...";
  listEl.classList.add("hidden");
  nfEl.classList.add("hidden");
  useBtn.classList.add("hidden");
  instBtn.classList.add("hidden");

  const data = await api("/api/setup/scan");
  const items = data.found_blenders || [];

  if (items.length > 0 || data.has_bpy) {
    stEl.textContent = items.length ? `Найдено установок Blender: ${items.length}` : "Найден установленный модуль bpy";
    const cards = items.map((it, i) => {
      if (i === 0) selectedWizardPath = it.path;
      return `
        <div class="hist-item ${i === 0 ? "active" : ""}" onclick="pickWizardPath('${esc(it.path).replace(/\\/g, "\\\\")}', this)">
          <div class="hist-title mono">${esc(it.path)}</div>
          <div class="hist-sub">${esc(it.source)}</div>
        </div>
      `;
    });
    if (data.has_bpy) {
      if (!selectedWizardPath) selectedWizardPath = "__BPY__";
      cards.push(`
        <div class="hist-item ${items.length === 0 ? "active" : ""}" onclick="pickWizardPath('__BPY__', this)">
          <div class="hist-title">Python модуль bpy</div>
          <div class="hist-sub">Установлен в текущем Python</div>
        </div>
      `);
    }
    listEl.innerHTML = cards.join("");
    listEl.classList.remove("hidden");
    useBtn.classList.remove("hidden");
  } else {
    selectedWizardPath = "";
    stEl.textContent = "Локальный Blender не обнаружен.";
    nfEl.classList.remove("hidden");
    instBtn.classList.remove("hidden");
  }
}

function pickWizardPath(val, el) {
  selectedWizardPath = val;
  document.querySelectorAll("#wizFoundList .hist-item").forEach(x => x.classList.remove("active"));
  el.classList.add("active");
}

async function browseBlender() {
  const res = await api("/api/setup/browse-blender", { method: "POST", body: "{}" });
  if (res.ok && res.path) {
    selectedWizardPath = res.path;
    const listEl = document.getElementById("wizFoundList");
    listEl.innerHTML = `
      <div class="hist-item active">
        <div class="hist-title mono">${esc(res.path)}</div>
        <div class="hist-sub">Выбран вручную</div>
      </div>
    `;
    listEl.classList.remove("hidden");
    document.getElementById("wizNotFoundBox").classList.add("hidden");
    document.getElementById("wizUseBlenderBtn").classList.remove("hidden");
    document.getElementById("wizInstallBpyBtn").classList.add("hidden");
  }
}

async function confirmSelectedBlender() {
  if (!selectedWizardPath) return;
  const payload = selectedWizardPath === "__BPY__"
    ? { blender_path: "", use_bpy: true }
    : { blender_path: selectedWizardPath, use_bpy: false };
  const res = await api("/api/setup/confirm", { method: "POST", body: JSON.stringify(payload) });
  if (res.ok) {
    if (res.runtime?.ai_ready) {
      closeWizard();
    } else {
      showWizStep(2);
    }
    checkRuntime();
  }
}

let _cookiePreviewTimer = null;
function previewCookies(inputId, boxId) {
  clearTimeout(_cookiePreviewTimer);
  _cookiePreviewTimer = setTimeout(async () => {
    const txt = document.getElementById(inputId).value.trim();
    const box = document.getElementById(boxId);
    if (!txt) {
      box.classList.add("hidden");
      return;
    }
    const res = await api("/api/cookies/parse-preview", { method: "POST", body: JSON.stringify({ raw_text: txt }) });
    box.classList.remove("hidden", "ok", "bad");
    if (res.ok) {
      box.classList.add("ok");
      const flags = ["1PSID", res.has_1psidts ? "1PSIDTS" : null, res.has_osid ? "OSID" : null].filter(Boolean).join(", ");
      box.textContent = `Формат: ${res.format} · Найдено: ${res.count} кук (${flags}) · ${res.psid_preview}`;
    } else {
      box.classList.add("bad");
      box.textContent = res.error || "Не удалось распознать куки";
    }
  }, 180);
}

function loadCookieFileTo(inputId, boxId, fileInput) {
  const f = fileInput.files?.[0];
  if (!f) return;
  const reader = new FileReader();
  reader.onload = () => {
    document.getElementById(inputId).value = String(reader.result || "");
    previewCookies(inputId, boxId);
  };
  reader.readAsText(f);
  fileInput.value = "";
}

async function saveWizardCookies() {
  const btn = document.getElementById("wizSaveCookieBtn");
  const errEl = document.getElementById("wizCookieErr");
  const raw = document.getElementById("wizCookiesInput").value.trim();
  const label = document.getElementById("wizCookieLabel").value.trim();
  errEl.textContent = "";
  if (!raw) { errEl.textContent = "Вставьте куки"; return; }
  btn.disabled = true;
  btn.textContent = "Проверка...";
  try {
    const px = readProxyPayload("wizProxyType", "wizProxyAddr");
    await api("/api/settings", { method: "POST", body: JSON.stringify(px) });
    const res = await api("/api/cookies/accounts", { method: "POST", body: JSON.stringify({ raw_text: raw, label, verify: true }) });
    if (!res.ok) {
      errEl.textContent = res.error || "Ошибка проверки кук";
    } else {
      document.getElementById("wizCookiesInput").value = "";
      document.getElementById("wizCookieLabel").value = "";
      closeWizard();
      await checkRuntime();
      await refreshQuickModels();
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Подключить аккаунт";
  }
}

async function saveWizardApi() {
  const url = document.getElementById("wizApiUrl").value.trim();
  const key = document.getElementById("wizApiKey").value.trim();
  const px = readProxyPayload("wizProxyType", "wizProxyAddr");
  await api("/api/settings", {
    method: "POST",
    body: JSON.stringify({
      ...px,
      provider_mode: "openai_api",
      text_base_url: url,
      text_api_key: key,
      image_base_url: url,
      image_api_key: key,
    }),
  });
  closeWizard();
  await checkRuntime();
  await refreshQuickModels();
}

async function installBpyNow() {
  const btn = document.getElementById("wizInstallBpyBtn");
  const logEl = document.getElementById("wizInstallLog");
  btn.disabled = true;
  btn.textContent = "Установка...";
  logEl.classList.remove("hidden");
  logEl.textContent = "Запуск pip install bpy...";
  const start = await api("/api/setup/install-bpy", { method: "POST", body: "{}" });
  clearInterval(wizInstallTimer);
  wizInstallTimer = setInterval(async () => {
    const t = await api("/api/task/" + start.task_id);
    logEl.textContent = (t.logs || []).slice(-18).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
    if (t.status === "done") {
      clearInterval(wizInstallTimer);
      btn.disabled = false;
      btn.textContent = "Установить bpy";
      closeWizard();
      checkRuntime();
    } else if (t.status === "error") {
      clearInterval(wizInstallTimer);
      btn.disabled = false;
      btn.textContent = "Повторить установку";
    }
  }, 900);
}

async function checkRuntime() {
  const st = await api("/api/status");
  const el = document.getElementById("runtimeBadge");
  if (st.has_blender_cli) {
    el.textContent = "Blender CLI готов";
    el.className = "badge ok";
  } else if (st.has_bpy) {
    el.textContent = "bpy готов";
    el.className = "badge ok";
  } else {
    el.textContent = "Нужен Blender / bpy";
    el.className = "badge warn";
  }

  const aiEl = document.getElementById("aiModeBadge");
  if (st.provider_mode === "gemini_cookies") {
    aiEl.textContent = st.ai_ready ? `Куки Gemini (${st.gemini_accounts_count})` : "Нужны куки Gemini";
    aiEl.className = st.ai_ready ? "badge ok" : "badge warn";
  } else {
    aiEl.textContent = st.ai_ready ? "OpenAI API" : "Нужен API ключ";
    aiEl.className = st.ai_ready ? "badge ok" : "badge warn";
  }

  if (!st.setup_done) {
    openWizard(false, 1);
  } else if (!st.ai_ready) {
    openWizard(true, 2);
  }
}

// ---------------- MODES & INLINE MODEL PICKERS ----------------
function setAppMode(mode, btn) {
  appMode = mode;
  document.querySelectorAll("#modeTabs .tab").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");

  document.getElementById("texOptionsBox").classList.toggle("hidden", mode === "model");
  document.getElementById("packCountWrap").classList.toggle("hidden", mode !== "pack");
  document.getElementById("pbrSlidersWrap").classList.toggle("hidden", mode !== "texture");
  document.getElementById("regenTexBtn").classList.toggle("hidden", mode !== "model");
  document.getElementById("refsBlock").classList.toggle("hidden", mode === "pack");

  const promptLbl = document.getElementById("promptLabel");
  const genBtn = document.getElementById("genBtnText");
  if (mode === "model") {
    promptLbl.textContent = "Описание 3D-модели";
    genBtn.textContent = "Создать 3D-модель";
    showViewerTabsFor("model");
  } else if (mode === "texture") {
    promptLbl.textContent = "Описание текстуры";
    genBtn.textContent = "Создать текстуру";
    showViewerTabsFor("texture");
  } else {
    promptLbl.textContent = "Описание пака текстур (тема или список)";
    genBtn.textContent = "Создать пак текстур";
    showViewerTabsFor("pack");
  }
}

function showViewerTabsFor(kind) {
  const isModel = kind === "model";
  const isTex = kind === "texture";
  const isPack = kind === "pack";
  document.getElementById("tab4SideBtn").classList.toggle("hidden", !isModel);
  document.getElementById("tabUvBtn").classList.toggle("hidden", !isModel);
  document.getElementById("tabCodeBtn").classList.toggle("hidden", !isModel);
  document.getElementById("tabMapsBtn").classList.toggle("hidden", !isTex);
  document.getElementById("tabPackBtn").classList.toggle("hidden", !isPack);
}

function switchView(name, btn) {
  document.querySelectorAll("[id^='view-']").forEach(v => v.classList.add("hidden"));
  document.getElementById("view-" + name).classList.remove("hidden");
  document.querySelectorAll(".toolbar .tab").forEach(b => b.classList.remove("active"));
  if (btn) btn.classList.add("active");
}

async function refreshQuickModels() {
  const s = (await api("/api/settings")).settings || {};
  currentSettings = s;
  const txtSel = document.getElementById("quickTextModel");
  const imgSel = document.getElementById("quickImageModel");

  const fillSelect = (sel, items, cur, strict = false) => {
    const clean = (items || []).filter(Boolean);
    const uniq = strict && clean.length
      ? Array.from(new Set(clean))
      : Array.from(new Set([cur, ...clean].filter(Boolean)));
    sel.innerHTML = uniq.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join("");
    if (cur && uniq.includes(cur)) {
      sel.value = cur;
      return cur;
    }
    if (uniq.length) {
      sel.value = uniq[0];
      return uniq[0];
    }
    return "";
  };

  fillSelect(txtSel, [s.text_model], s.text_model);
  fillSelect(imgSel, [s.image_model], s.image_model);

  try {
    if (s.provider_mode === "gemini_cookies") {
      const [rTxt, rImg] = await Promise.all([
        api("/api/models", { method: "POST", body: JSON.stringify({ provider_mode: "gemini_cookies", kind: "text" }) }),
        api("/api/models", { method: "POST", body: JSON.stringify({ provider_mode: "gemini_cookies", kind: "image" }) }),
      ]);
      const patch = {};
      if (rTxt.ok && rTxt.models?.length) {
        const chosenTxt = fillSelect(txtSel, rTxt.models, s.text_model, true);
        if (chosenTxt && chosenTxt !== s.text_model) patch.text_model = chosenTxt;
      }
      if (rImg.ok && rImg.models?.length) {
        const chosenImg = fillSelect(imgSel, rImg.models, s.image_model, true);
        if (chosenImg && chosenImg !== s.image_model) patch.image_model = chosenImg;
      }
      if (Object.keys(patch).length) {
        await api("/api/settings", { method: "POST", body: JSON.stringify(patch) });
      }
      return;
    }
    if (s.text_base_url) {
      const r1 = await api("/api/models", { method: "POST", body: JSON.stringify({ base_url: s.text_base_url, api_key: s.text_api_key, kind: "text" }) });
      if (r1.ok && r1.models?.length) fillSelect(txtSel, r1.models, s.text_model);
    }
    if (s.image_base_url) {
      const r2 = await api("/api/models", { method: "POST", body: JSON.stringify({ base_url: s.image_base_url, api_key: s.image_api_key, kind: "image" }) });
      if (r2.ok) {
        if (r2.models?.length) {
          const chosenImg = fillSelect(imgSel, r2.models, s.image_model, true);
          if (chosenImg && chosenImg !== s.image_model) {
            await api("/api/settings", { method: "POST", body: JSON.stringify({ image_model: chosenImg }) });
          }
        } else if (s.image_base_url !== s.text_base_url && r2.raw_models?.length) {
          fillSelect(imgSel, r2.raw_models, s.image_model);
        }
      }
    }
  } catch {}
}

async function onQuickModelChange() {
  const text_model = document.getElementById("quickTextModel").value;
  const image_model = document.getElementById("quickImageModel").value;
  await api("/api/settings", { method: "POST", body: JSON.stringify({ text_model, image_model }) });
}

async function loadStyles() {
  const data = await api("/api/styles");
  const sel = document.getElementById("styleSelect");
  sel.innerHTML = (data.styles || []).map(s => `<option value="${esc(s.id)}">${esc(s.label)}</option>`).join("");
  if (currentSettings.style) sel.value = currentSettings.style;
}

// ---------------- REFS ----------------
function addRefFiles(files) {
  for (const f of files) {
    const reader = new FileReader();
    reader.onload = () => {
      refImages.push({ name: f.name, data_url: reader.result });
      renderRefs();
    };
    reader.readAsDataURL(f);
  }
  document.getElementById("refFileInput").value = "";
}

function renderRefs() {
  const el = document.getElementById("refStrip");
  el.innerHTML = refImages.map((r, i) => `
    <div class="ref-thumb">
      <img src="${r.data_url}" alt="${esc(r.name)}" />
      <button class="ref-del" type="button" aria-label="Удалить референс" onclick="removeRef(${i})">×</button>
    </div>
  `).join("");
}

function removeRef(i) {
  refImages.splice(i, 1);
  renderRefs();
}

// ---------------- GENERATION & PROGRESS OVERLAY ----------------
function showGenOverlay(mode, msg, stepLabels = ["1. Геометрия и UV", "2. Текстура по UV", "3. GLB"]) {
  const ov = document.getElementById("genOverlay");
  const s1 = document.getElementById("gStep1");
  const s2 = document.getElementById("gStep2");
  const s3 = document.getElementById("gStep3");
  s1.textContent = stepLabels[0];
  s2.textContent = stepLabels[1];
  s3.textContent = stepLabels[2];

  ov.classList.remove("hidden", "bottom");
  s1.className = "gen-step";
  s2.className = "gen-step";
  s3.className = "gen-step";

  if (mode === "step1") {
    s1.classList.add("active");
  } else if (mode === "step2") {
    ov.classList.add("bottom");
    s1.classList.add("done");
    s2.classList.add("active");
  } else if (mode === "step3") {
    ov.classList.add("bottom");
    s1.classList.add("done");
    s2.classList.add("done");
    s3.classList.add("active");
  }
  document.getElementById("genMsg").textContent = msg;
}

function hideGenOverlay() {
  document.getElementById("genOverlay").classList.add("hidden");
  clearInterval(genClockTimer);
  if (window.setPlaceholder3D) window.setPlaceholder3D(false);
}

function startGenClock() {
  genStartTime = Date.now();
  clearInterval(genClockTimer);
  genClockTimer = setInterval(() => {
    const sec = Math.floor((Date.now() - genStartTime) / 1000);
    const mm = String(Math.floor(sec / 60)).padStart(2, "0");
    const ss = String(sec % 60).padStart(2, "0");
    document.getElementById("genTimer").textContent = `${mm}:${ss}`;
  }, 300);
}

function getSelectedMaps() {
  return Array.from(document.querySelectorAll(".map-chk:checked")).map(c => c.value);
}

async function startGenerate() {
  const prompt = document.getElementById("promptInput").value.trim();
  if (!prompt) return;
  setBusy(true);
  loadedPartialForTask = "";
  document.getElementById("logBox").textContent = "Запуск задачи...";
  if (window.setPlaceholder3D) window.setPlaceholder3D(true);
  startGenClock();

  const common = {
    prompt,
    style: document.getElementById("styleSelect").value,
    tex_size: Number(document.getElementById("texSizeSelect").value),
    text_model: document.getElementById("quickTextModel").value,
    image_model: document.getElementById("quickImageModel").value,
    ref_images: refImages,
  };

  let res;
  if (appMode === "model") {
    showGenOverlay("step1", "Генерация 3D-геометрии и UV...");
    res = await api("/api/generate", { method: "POST", body: JSON.stringify(common) });
  } else if (appMode === "texture") {
    showGenOverlay("step1", "Подготовка текстуры...", ["1. Промпт / Albedo", "2. Бесшовность", "3. PBR-карты"]);
    res = await api("/api/generate-texture", {
      method: "POST",
      body: JSON.stringify({
        ...common,
        seamless: document.getElementById("chkSeamless").checked,
        enhance: document.getElementById("chkEnhance").checked,
        maps: getSelectedMaps(),
        pbr_params: {
          normal_strength: Number(document.getElementById("pbrNormal").value || 2.4),
          roughness_base: Number(document.getElementById("pbrRough").value || 0.65),
          ao_strength: Number(document.getElementById("pbrAo").value || 0.75),
        },
        override_pbr: !document.getElementById("chkEnhance").checked,
      }),
    });
  } else {
    showGenOverlay("step1", "Планирование пака текстур...", ["1. План пака", "2. Генерация текстур", "3. PBR-карты"]);
    res = await api("/api/generate-pack", {
      method: "POST",
      body: JSON.stringify({
        ...common,
        seamless: document.getElementById("chkSeamless").checked,
        count: Number(document.getElementById("packCount").value || 4),
        maps: getSelectedMaps(),
      }),
    });
  }

  if (!res.ok) {
    document.getElementById("logBox").textContent = res.error || "Ошибка запуска";
    hideGenOverlay();
    setBusy(false);
    return;
  }
  pollTask(res.task_id, appMode);
}

async function regenTexture() {
  if (!activeJobId) return;
  setBusy(true);
  loadedPartialForTask = activeJobId;
  document.getElementById("logBox").textContent = "Перегенерация текстуры...";
  startGenClock();
  showGenOverlay("step2", "Генерация новой UV-текстуры...");

  const res = await api("/api/regen-texture", {
    method: "POST",
    body: JSON.stringify({
      job_id: activeJobId,
      prompt: document.getElementById("promptInput").value.trim(),
      style: document.getElementById("styleSelect").value,
      tex_size: Number(document.getElementById("texSizeSelect").value),
      text_model: document.getElementById("quickTextModel").value,
      image_model: document.getElementById("quickImageModel").value,
    }),
  });
  pollTask(res.task_id, "model");
}

function setBusy(busy) {
  document.getElementById("genBtn").disabled = busy;
  document.getElementById("regenTexBtn").disabled = busy || !activeJobId || appMode !== "model";
}

function pollTask(tid, mode) {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const t = await api("/api/task/" + tid);
    const logs = t.logs || [];
    const logEl = document.getElementById("logBox");
    logEl.textContent = logs.join("\n");
    logEl.scrollTop = logEl.scrollHeight;

    if (mode === "model") {
      if (t.partial && t.partial.stage === "geometry_ready" && loadedPartialForTask !== tid) {
        loadedPartialForTask = tid;
        applyPartialStage1(t.partial);
        showGenOverlay("step2", `Геометрия готова (${t.partial.meta?.poly_count || 0} полиг.) · Генерация текстуры...`);
      } else if (t.partial && (logs[logs.length - 1] || "").includes("Наложение")) {
        showGenOverlay("step3", "Наложение текстуры и экспорт GLB...");
      } else if (!t.partial) {
        showGenOverlay("step1", logs[logs.length - 1] || "Построение геометрии...");
      }
    } else if (mode === "pack") {
      if (t.partial && t.partial.stage === "pack_progress") {
        renderPackItems(t.partial.items || [], t.partial.pack_title || "");
        showGenOverlay("step2", `Пак: готово ${t.partial.done} из ${t.partial.total}...`, ["1. План пака", "2. Генерация текстур", "3. PBR-карты"]);
      } else {
        showGenOverlay("step1", logs[logs.length - 1] || "Анализ запроса...", ["1. План пака", "2. Генерация текстур", "3. PBR-карты"]);
      }
    } else {
      const last = logs[logs.length - 1] || "";
      if (last.includes("PBR")) showGenOverlay("step3", last, ["1. Промпт / Albedo", "2. Бесшовность", "3. PBR-карты"]);
      else if (last.includes("Albedo")) showGenOverlay("step2", last, ["1. Промпт / Albedo", "2. Бесшовность", "3. PBR-карты"]);
      else showGenOverlay("step1", last || "Обработка...", ["1. Промпт / Albedo", "2. Бесшовность", "3. PBR-карты"]);
    }

    if (t.status === "done") {
      clearInterval(pollTimer);
      hideGenOverlay();
      setBusy(false);
      applyJobResult(t.result);
      loadHistory();
    } else if (t.status === "error") {
      clearInterval(pollTimer);
      hideGenOverlay();
      setBusy(false);
    }
  }, 750);
}

function applyPartialStage1(p) {
  if (!p) return;
  activeJobId = p.job_id;
  showViewerTabsFor("model");
  const bust = "?t=" + Date.now();
  if (p.uv_guide_url) document.getElementById("imgUvGuide").src = p.uv_guide_url + bust;
  if (p.uv_3d_preview_url) document.getElementById("imgUv3d").src = p.uv_3d_preview_url + bust;
  if (p.uv_clean_url) document.getElementById("imgUvClean").src = p.uv_clean_url + bust;
  document.getElementById("imgTexture").removeAttribute("src");
  document.getElementById("codeOut").textContent = p.code || "";
  const m = p.meta || {};
  document.getElementById("polyStats").textContent = m.poly_count ? `${m.poly_count} полиг. · ${m.vert_count} верш.` : "";
  if (p.preview_glb_url && window.loadGlbInViewer) {
    window.loadGlbInViewer(p.preview_glb_url, true, false);
  }
}

function applyJobResult(res) {
  if (!res) return;
  activeJobId = res.job_id;
  const kind = res.kind || res.meta?.kind || "model";
  const m = res.meta || {};
  const bust = "?t=" + Date.now();
  const crisp = ["ps1", "voxel", "horror"].includes(m.style);

  showViewerTabsFor(kind);
  document.getElementById("openFolderBtn").classList.remove("hidden");

  if (kind === "model") {
    document.getElementById("regenTexBtn").disabled = (appMode !== "model");
    if (res.preview_4side_url) document.getElementById("img4Side").src = res.preview_4side_url + bust;
    if (res.uv_guide_url) document.getElementById("imgUvGuide").src = res.uv_guide_url + bust;
    if (res.uv_3d_preview_url) document.getElementById("imgUv3d").src = res.uv_3d_preview_url + bust;
    if (res.uv_clean_url) document.getElementById("imgUvClean").src = res.uv_clean_url + bust;
    if (res.texture_url) document.getElementById("imgTexture").src = res.texture_url + bust;
    document.getElementById("codeOut").textContent = res.code || "";
    document.getElementById("polyStats").textContent = m.poly_count ? `${m.poly_count} полиг. · ${m.vert_count} верш.` : "";

    const dl = document.getElementById("dlGlbBtn");
    if (res.glb_url) {
      dl.href = res.glb_url;
      dl.classList.remove("hidden");
      if (window.loadGlbInViewer) window.loadGlbInViewer(res.glb_url, false, crisp);
    }
    switchView("3d", document.getElementById("tab3dBtn"));
  } else if (kind === "texture") {
    document.getElementById("dlGlbBtn").classList.add("hidden");
    document.getElementById("polyStats").textContent = `${m.tex_size}px · ${m.seamless ? "бесшовная" : "одиночная"}`;
    const urls = res.urls || m.urls || {};
    renderTextureMapsGrid(urls, m);
    if (window.loadPbrMaterialInViewer) {
      window.loadPbrMaterialInViewer(urls, !!m.seamless, crisp);
    }
    switchView("3d", document.getElementById("tab3dBtn"));
  } else if (kind === "pack") {
    document.getElementById("dlGlbBtn").classList.add("hidden");
    const items = res.items || m.items || [];
    document.getElementById("polyStats").textContent = `Пак: ${items.length} текстур · ${m.tex_size}px`;
    renderPackItems(items, m.pack_title || m.prompt || "");
    if (items[0] && window.loadPbrMaterialInViewer) {
      window.loadPbrMaterialInViewer(items[0].urls, !!items[0].seamless, crisp);
    }
    switchView("pack", document.getElementById("tabPackBtn"));
  }
}

function renderTextureMapsGrid(urls, meta) {
  const bust = "?t=" + Date.now();
  const labels = [
    ["albedo", "Albedo (Основная)"],
    ["tiled", "Тайлинг 2×2"],
    ["normal", "Normal Map"],
    ["roughness", "Roughness Map"],
    ["height", "Height / Displacement"],
    ["ao", "Ambient Occlusion"],
    ["metallic", "Metallic"],
  ];
  const cards = labels.filter(([k]) => urls[k]).map(([k, title]) => `
    <div class="img-card">
      <div class="row between">
        <h2>${esc(title)}</h2>
        <a class="btn xs" href="${urls[k]}" download="${k}.png">PNG</a>
      </div>
      <img src="${urls[k] + bust}" alt="${esc(title)}" />
    </div>
  `);
  const info = meta.enhanced_prompt
    ? `<div class="img-card" style="grid-column:1/-1;"><h2>Промпт после улучшайзера</h2><div style="color:var(--muted);margin-top:8px;font-size:12px;">${esc(meta.enhanced_prompt)}</div></div>`
    : "";
  document.getElementById("mapsGrid").innerHTML = info + cards.join("");
}

function renderPackItems(items, packTitle) {
  const bust = "?t=" + Date.now();
  document.getElementById("packTitleHeader").textContent = packTitle ? `Пак: ${packTitle}` : "Пак текстур";
  document.getElementById("packGrid").innerHTML = (items || []).map((it, idx) => {
    const u = it.urls || {};
    const thumb = u.tiled || u.albedo || "";
    const mapBadges = Object.keys(u).filter(k => k !== "albedo" && k !== "tiled").join(", ");
    return `
      <div class="img-card">
        <div class="row between">
          <h2 style="font-size:13px;">${esc(it.title || it.slug)}</h2>
          <button class="btn xs" onclick='previewPackItem(${idx})'>3D</button>
        </div>
        ${thumb ? `<img src="${thumb + bust}" alt="${esc(it.title)}" />` : ""}
        <div class="hist-sub">${it.seamless ? "Бесшовная" : "Обычная"}${mapBadges ? " · " + esc(mapBadges) : ""}</div>
      </div>
    `;
  }).join("");
  window._currentPackItems = items || [];
}

window.previewPackItem = function(idx) {
  const it = (window._currentPackItems || [])[idx];
  if (!it || !it.urls) return;
  if (window.loadPbrMaterialInViewer) window.loadPbrMaterialInViewer(it.urls, !!it.seamless, false);
  switchView("3d", document.getElementById("tab3dBtn"));
};

// ---------------- HISTORY ----------------
async function loadHistory() {
  const data = await api("/api/jobs");
  const el = document.getElementById("historyList");
  el.innerHTML = (data.jobs || []).map(j => {
    const k = j.kind || "model";
    const sub = k === "model"
      ? `3D · ${j.style} · ${j.tex_size}px ${j.poly_count ? "· " + j.poly_count + " полиг." : ""}`
      : (k === "pack" ? `Пак (${(j.items || []).length}) · ${j.style}` : `Текстура · ${j.style} · ${j.tex_size}px`);
    return `
      <div class="hist-item ${j.job_id === activeJobId ? "active" : ""}" onclick="selectJob('${j.job_id}')">
        <div class="hist-title">${esc(j.pack_title || j.prompt)}</div>
        <div class="hist-sub">${esc(sub)}</div>
      </div>
    `;
  }).join("") || `<div class="hist-sub">Пока нет генераций.</div>`;
}

async function selectJob(jid) {
  const res = await api("/api/jobs/" + jid);
  if (res.meta?.prompt) document.getElementById("promptInput").value = res.meta.prompt;
  applyJobResult(res);
  loadHistory();
}

function openJobFolder() {
  api("/api/open-folder", { method: "POST", body: JSON.stringify({ job_id: activeJobId }) });
}

// ---------------- SETTINGS MODAL (COOKIES + API) ----------------
function switchSettingsTab(tab) {
  const isC = tab === "cookies";
  document.getElementById("setTabCookies").classList.toggle("active", isC);
  document.getElementById("setTabApi").classList.toggle("active", !isC);
  document.getElementById("setPaneCookies").classList.toggle("hidden", !isC);
  document.getElementById("setPaneApi").classList.toggle("hidden", isC);
}

async function openSettings() {
  const data = await api("/api/settings");
  const s = data.settings || {};
  document.getElementById("sProviderMode").value = s.provider_mode || "openai_api";
  fillProxyFields("setProxyType", "setProxyAddr", s.proxy_url || "");
  document.getElementById("setProxyMsg").textContent = "";
  document.getElementById("sTextUrl").value = s.text_base_url || "";
  document.getElementById("sTextKey").value = s.text_api_key || "";
  document.getElementById("sTextModel").value = s.text_model || "";
  document.getElementById("sImgUrl").value = s.image_base_url || "";
  document.getElementById("sImgKey").value = s.image_api_key || "";
  document.getElementById("sImgModel").value = s.image_model || "";
  document.getElementById("sBlenderPath").value = s.blender_path || "";
  document.getElementById("sRetries").value = s.max_retries || 3;
  document.getElementById("setMsg").textContent = "";
  document.getElementById("setCookieErr").textContent = "";
  switchSettingsTab("cookies");
  document.getElementById("settingsModal").classList.add("open");
  await loadCookieAccounts();
}

function closeSettings() {
  document.getElementById("settingsModal").classList.remove("open");
}

async function onProviderModeChange() {
  const mode = document.getElementById("sProviderMode").value;
  await api("/api/settings", { method: "POST", body: JSON.stringify({ provider_mode: mode }) });
  await checkRuntime();
  await refreshQuickModels();
}

async function loadCookieAccounts() {
  const data = await api("/api/cookies/accounts");
  const list = document.getElementById("setAccountsList");
  const accs = data.accounts || [];
  if (!accs.length) {
    list.innerHTML = `<div style="color:var(--muted);font-size:12px;">Нет добавленных аккаунтов.</div>`;
    return;
  }
  list.innerHTML = accs.map(a => {
    const ui = (a.quotas || {}).usage_info || {};
    const q5 = ui.current_5h ? `5ч: ост. ${ui.current_5h.remaining_credits} (${ui.current_5h.usage_percentage}%)` : "";
    const qw = ui.weekly ? `нед: ост. ${ui.weekly.remaining_credits} (${ui.weekly.usage_percentage}%)` : "";
    const qline = [q5, qw].filter(Boolean).join(" · ");
    return `
      <div class="acc-card" style="opacity:${a.enabled ? 1 : 0.55}">
        <div class="row between">
          <div>
            <div style="font-weight:600;">${esc(a.email || a.label)} ${a.tier ? `<span style="color:var(--muted);font-weight:500;">· ${esc(a.tier)}</span>` : ""}</div>
            <div class="mono" style="font-size:11px;color:var(--muted);">${esc(a.cookies_summary?.psid_preview || "")} · ${a.cookies_summary?.count || 0} кук</div>
          </div>
          <span class="pill ${esc(a.status)}">${esc(a.status)}</span>
        </div>
        ${qline ? `<div style="font-size:12px;color:var(--muted);">${esc(qline)}</div>` : ""}
        ${a.last_error ? `<div style="font-size:12px;color:var(--red);">${esc(a.last_error)}</div>` : ""}
        <div class="row">
          <button class="btn xs" type="button" onclick="checkCookieAcc('${a.id}', this)">Проверить</button>
          <button class="btn xs" type="button" onclick="toggleCookieAcc('${a.id}', ${!a.enabled})">${a.enabled ? "Отключить" : "Включить"}</button>
          <button class="btn xs" type="button" style="color:var(--red);" onclick="deleteCookieAcc('${a.id}')">Удалить</button>
        </div>
      </div>
    `;
  }).join("");
}

async function addSettingsCookieAccount() {
  const btn = document.getElementById("setAddCookieBtn");
  const errEl = document.getElementById("setCookieErr");
  const raw = document.getElementById("setCookiesInput").value.trim();
  const label = document.getElementById("setCookieLabel").value.trim();
  errEl.textContent = "";
  if (!raw) { errEl.textContent = "Вставьте куки"; return; }
  btn.disabled = true;
  btn.textContent = "Проверка...";
  try {
    const px = readProxyPayload("setProxyType", "setProxyAddr");
    await api("/api/settings", { method: "POST", body: JSON.stringify(px) });
    const res = await api("/api/cookies/accounts", { method: "POST", body: JSON.stringify({ raw_text: raw, label, verify: true }) });
    if (!res.ok) {
      errEl.textContent = res.error || "Ошибка проверки";
    } else {
      document.getElementById("setCookiesInput").value = "";
      document.getElementById("setCookieLabel").value = "";
      document.getElementById("setParsePreview").classList.add("hidden");
      document.getElementById("sProviderMode").value = "gemini_cookies";
      await checkRuntime();
      await refreshQuickModels();
    }
    await loadCookieAccounts();
  } finally {
    btn.disabled = false;
    btn.textContent = "Добавить и проверить";
  }
}

async function checkCookieAcc(id, btn) {
  btn.disabled = true;
  btn.textContent = "...";
  try {
    await api(`/api/cookies/accounts/${id}/check`, { method: "POST", body: "{}" });
    await loadCookieAccounts();
    await checkRuntime();
  } finally {
    btn.disabled = false;
    btn.textContent = "Проверить";
  }
}

async function toggleCookieAcc(id, enabled) {
  await api(`/api/cookies/accounts/${id}`, { method: "PATCH", body: JSON.stringify({ enabled }) });
  await loadCookieAccounts();
  await checkRuntime();
}

async function deleteCookieAcc(id) {
  await api(`/api/cookies/accounts/${id}`, { method: "DELETE" });
  await loadCookieAccounts();
  await checkRuntime();
}

async function fetchModelsFor(kind) {
  const base_url = document.getElementById(kind === "text" ? "sTextUrl" : "sImgUrl").value.trim();
  const api_key = document.getElementById(kind === "text" ? "sTextKey" : "sImgKey").value.trim();
  const msg = document.getElementById("setMsg");
  msg.textContent = "Загрузка списка моделей...";
  const res = await api("/api/models", { method: "POST", body: JSON.stringify({ base_url, api_key, kind }) });
  if (!res.ok) {
    msg.textContent = "Ошибка: " + res.error;
    return;
  }
  const dl = document.getElementById(kind === "text" ? "textModelsList" : "imgModelsList");
  const list = (res.models && res.models.length) ? res.models : (res.raw_models || []);
  dl.innerHTML = list.map(m => `<option value="${esc(m)}">`).join("");
  if (kind === "image" && !(res.models || []).length && (res.raw_models || []).length) {
    msg.textContent = `Image-модели не распознаны, показаны все (${res.raw_models.length})`;
  } else {
    msg.textContent = `Загружено моделей: ${(res.models || []).length}`;
  }
}

async function saveSettings() {
  const payload = {
    text_base_url: document.getElementById("sTextUrl").value.trim(),
    text_api_key: document.getElementById("sTextKey").value.trim(),
    text_model: document.getElementById("sTextModel").value.trim(),
    image_base_url: document.getElementById("sImgUrl").value.trim(),
    image_api_key: document.getElementById("sImgKey").value.trim(),
    image_model: document.getElementById("sImgModel").value.trim(),
    blender_path: document.getElementById("sBlenderPath").value.trim(),
    max_retries: Number(document.getElementById("sRetries").value || 3),
  };
  await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
  closeSettings();
  checkRuntime();
  refreshQuickModels();
}

(async () => {
  await refreshQuickModels();
  await loadStyles();
  await checkRuntime();
  await loadHistory();
})();
