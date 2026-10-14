"use strict";

const byId = (id) => document.getElementById(id);
const loginForm = byId("login-form");
const statusError = byId("status-error");
const configForm = byId("config-form");
const configMessage = byId("config-message");
const configState = byId("config-state");
const tabButtons = [...document.querySelectorAll("[data-tab]")];
let configSnapshot = null;
let configLoaded = false;
let modelListBaseUrl = "";

async function request(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers,
  });
  if (response.status === 401 && !loginForm) {
    window.location.replace("/login");
    throw new Error("login required");
  }
  return response;
}

function showMessage(text, kind = "info") {
  if (!configMessage) return;
  configMessage.textContent = text;
  configMessage.className = `form-message ${kind}`;
  configMessage.hidden = false;
}

function setConfigState(text, kind = "") {
  if (!configState) return;
  configState.textContent = text;
  configState.className = `state-note ${kind}`;
}

if (loginForm) {
  loginForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = loginForm.querySelector("button");
    const error = byId("login-error");
    button.disabled = true;
    error.hidden = true;
    try {
      const response = await request("/api/login", {
        method: "POST",
        body: JSON.stringify({ password: byId("password").value }),
      });
      if (response.ok) window.location.replace("/");
      else {
        error.textContent = response.status === 429 ? "尝试过多，请稍后再试。" : "口令无效，请重试。";
        error.hidden = false;
      }
    } catch {
      error.textContent = "暂时无法连接后台。";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });
}

function setState(id, text, variant) {
  const node = byId(id);
  if (!node) return;
  node.textContent = text;
  node.className = variant;
}

function setRestartMessage(text, kind = "") {
  const node = byId("restart-message");
  if (!node) return;
  node.textContent = text;
  node.className = `header-status ${kind}`.trim();
  node.hidden = !text;
}

function wait(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitForBotRestart() {
  const startedAt = Date.now();
  let observedStopped = false;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await wait(1000);
    let response;
    try {
      response = await request("/api/status");
    } catch (error) {
      if (error.message === "login required") throw error;
      continue;
    }
    if (!response.ok) continue;
    const result = await response.json().catch(() => ({}));
    const running = result.bot && result.bot.running === true;
    if (!running) observedStopped = true;
    // A fast systemd restart can complete between two polls; after a short
    // settling window, a running Bot is enough to report success.
    if (running && (observedStopped || Date.now() - startedAt >= 2500)) return;
  }
  throw new Error("Bot 服务尚未恢复，请到运行状态页检查。 ");
}

async function restartBot() {
  const button = byId("restart-bot");
  if (!button) return;
  button.disabled = true;
  button.textContent = "重启中…";
  setRestartMessage("正在重启 Bot 服务…", "state-wait");
  try {
    const response = await request("/api/bot/restart", { method: "POST" });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "Bot 重启请求失败");
    setRestartMessage("重启请求已发送，正在等待服务恢复…", "state-wait");
    await waitForBotRestart();
    setRestartMessage("Bot 已恢复运行", "state-good");
    await renderStatus();
  } catch (error) {
    if (error.message === "login required") return;
    setRestartMessage(error.message || "Bot 重启失败，请稍后重试。", "state-bad");
  } finally {
    button.disabled = false;
    button.textContent = "重启 Bot";
  }
}

function setMetric(name, percent, detail) {
  const meter = byId(`${name}-meter`);
  if (!meter) return;
  const value = Number.isFinite(percent) ? Math.max(0, Math.min(100, percent)) : 0;
  byId(`${name}-value`).textContent = detail;
  byId(`${name}-bar`).style.width = `${value}%`;
  meter.setAttribute("aria-valuenow", String(Math.round(value)));
  meter.className = `meter ${value >= 90 ? "danger" : value >= 75 ? "warning" : ""}`;
}

function bytes(n) {
  if (!Number.isFinite(n)) return "—";
  return `${(n / (1024 ** 3)).toFixed(1)} GB`;
}

function refreshQrImage() {
  const image = byId("qr-image");
  const message = byId("qr-message");
  image.hidden = true;
  image.onload = () => { image.hidden = false; message.hidden = true; };
  image.onerror = () => {
    image.hidden = true;
    message.textContent = "没有可用的新二维码，请刷新。";
    message.hidden = false;
  };
  image.src = `/api/qq/qr?t=${Date.now()}`;
}

async function renderStatus() {
  try {
    const response = await request("/api/status");
    if (!response.ok) throw new Error("status unavailable");
    const result = await response.json();
    statusError.hidden = true;
    setState("qq-state", result.qq.state === "degraded" ? "在线，连接异常" : result.qq.online === true ? "在线" : result.qq.state === "login_required" ? "等待扫码" : "状态未知",
      result.qq.state === "degraded" ? "state-wait" : result.qq.online === true ? "state-good" : result.qq.state === "login_required" ? "state-wait" : "state-bad");
    setState("bot-state", result.bot.running ? "运行中" : "未运行", result.bot.running ? "state-good" : "state-bad");
    const date = new Date(result.checkedAt);
    const time = Number.isNaN(date.getTime()) ? "—" : date.toLocaleTimeString("zh-CN", { hour12: false });
    byId("update-time").textContent = time;
    byId("last-check").textContent = `最近检查：${time}`;
    const cpu = result.system.cpuPercent;
    setMetric("cpu", cpu, Number.isFinite(cpu) ? `${cpu.toFixed(1)}%` : "—");
    for (const name of ["memory", "disk"]) {
      const total = result.system[`${name}Total`];
      const used = result.system[`${name}Used`];
      const percent = total > 0 ? 100 * used / total : null;
      setMetric(name, percent, Number.isFinite(percent) ? `${bytes(used)} / ${bytes(total)} · ${percent.toFixed(1)}%` : "—");
    }
    const qrSection = byId("qr-section");
    if (result.qq.online !== true && qrSection.hidden) { qrSection.hidden = false; refreshQrImage(); }
    if (result.qq.online === true) { qrSection.hidden = true; byId("qr-image").src = ""; }
  } catch (error) {
    if (error.message === "login required") return;
    statusError.textContent = "状态暂时不可用，请稍后重试。";
    statusError.hidden = false;
  }
}

function labelForFeature(key) {
  const labels = {
    codexRunway: "Codex Runway 监控",
    usageRankingReport: "用量排行播报",
    markets: "行情与股票",
    search: "联网搜索",
    speech: "语音回复",
    "speech.enabled": "语音回复",
    "speech.privateEnabled": "私聊语音",
    "speech.groupEnabled": "群聊语音",
    "speech.randomReplyEnabled": "随机语音",
    imageGeneration: "图片生成",
  };
  return labels[key] || key;
}

function normalizeFeatures(raw) {
  if (Array.isArray(raw)) {
    return raw.map((item) => ({
      key: String(item.key || item.name || ""),
      label: String(item.label || labelForFeature(item.key || item.name || "")),
      description: String(item.description || "重启 Bot 后生效。"),
      enabled: Boolean(item.enabled),
    })).filter((item) => item.key);
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw).map(([key, value]) => {
      const item = value && typeof value === "object" ? value : {};
      return {
        key,
        label: String(item.label || labelForFeature(key)),
        description: String(item.description || "重启 Bot 后生效。"),
        enabled: Boolean(typeof value === "object" ? item.enabled : value),
      };
    });
  }
  return [];
}

function renderFeatures(rawFeatures) {
  const features = normalizeFeatures(rawFeatures);
  const list = byId("feature-list");
  const enabledCount = features.filter((item) => item.enabled).length;
  byId("feature-count").textContent = `${enabledCount} / ${features.length} 已开启`;
  list.replaceChildren();
  if (!features.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "暂无可管理的功能。";
    list.append(empty);
    return;
  }
  for (const feature of features) {
    const card = document.createElement("div");
    card.className = "feature-card";
    const label = document.createElement("label");
    label.className = "feature-row";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.dataset.featureKey = feature.key;
    input.checked = feature.enabled;
    input.addEventListener("change", () => {
      const enabled = [...list.querySelectorAll("input[type=checkbox]")].filter((item) => item.checked).length;
      byId("feature-count").textContent = `${enabled} / ${features.length} 已开启`;
    });
    const slider = document.createElement("span");
    slider.className = "switch-track";
    const knob = document.createElement("span");
    knob.className = "switch-knob";
    slider.append(knob);
    const copy = document.createElement("span");
    copy.className = "feature-copy";
    const title = document.createElement("strong");
    title.textContent = feature.label;
    const description = document.createElement("small");
    description.textContent = feature.description;
    copy.append(title, description);
    label.append(input, slider, copy);
    card.append(label);
    list.append(card);
  }
}

function setProviderKeyState(id, configured) {
  const node = byId(id);
  if (!node) return;
  node.textContent = configured ? "API Key 已配置" : "API Key 未配置";
  node.className = `config-chip ${configured ? "is-good" : "is-warn"}`;
}

function fillConfig(result) {
  configSnapshot = result;
  const model = result.model || {};
  byId("model-provider").value = model.provider || "";
  byId("model-api-key-env").value = model.apiKeyEnv || "";
  byId("model-base-url").value = model.baseUrl || "";
  byId("model-name").value = model.name || "";
  const modelSelect = byId("model-options");
  modelSelect.replaceChildren(new Option("获取模型后从列表选择", ""));
  modelListBaseUrl = model.baseUrl || "";
  byId("model-reasoning").value = model.reasoningEffort || "";
  byId("model-timeout").value = model.timeoutSeconds ?? 90;
  byId("model-temperature").value = model.temperature ?? 0.8;
  byId("model-max-text-chars").value = model.maxTextChars ?? 4800;
  byId("model-api-key").value = "";
  const keyConfigured = Boolean(model.apiKeyConfigured);
  byId("key-state").textContent = keyConfigured ? "API Key 已配置" : "API Key 未配置";
  byId("key-state").className = `config-chip ${keyConfigured ? "is-good" : "is-warn"}`;
  const speech = result.speech || {};
  byId("speech-api-mode").value = speech.apiMode || "audio_speech";
  byId("speech-format").value = speech.format || "mp3";
  byId("speech-model-options").replaceChildren(new Option("获取模型后从列表选择", ""));
  byId("speech-model").value = speech.model || "";
  byId("speech-voice").value = speech.voice || "";
  setProviderKeyState("speech-key-state", keyConfigured);
  const image = result.imageGeneration || {};
  byId("image-model-options").replaceChildren(new Option("获取模型后从列表选择", ""));
  byId("image-model").value = image.model || "";
  byId("image-generation-endpoint").value = image.generationEndpoint || "/images/generations";
  byId("image-edit-endpoint").value = image.editEndpoint || "/images/edits";
  setProviderKeyState("image-key-state", keyConfigured);
  renderFeatures(result.features);
  setConfigState(result.restartRequired ? "保存后需重启 Bot" : "已同步", result.restartRequired ? "state-wait" : "state-good");
}

async function loadConfig({ quiet = false } = {}) {
  if (!quiet) setConfigState("正在读取配置…");
  try {
    const response = await request("/api/config");
    if (!response.ok) throw new Error("config unavailable");
    fillConfig(await response.json());
    configLoaded = true;
  } catch (error) {
    if (error.message === "login required") return;
    setConfigState("配置读取失败", "state-bad");
    showMessage("暂时无法读取配置，请稍后重试。", "error");
  }
}

function collectConfig() {
  const features = {};
  for (const input of document.querySelectorAll("[data-feature-key]")) features[input.dataset.featureKey] = input.checked;
  const model = {
    provider: byId("model-provider").value.trim(),
    apiKeyEnv: byId("model-api-key-env").value.trim(),
    baseUrl: byId("model-base-url").value.trim(),
    name: byId("model-name").value.trim(),
    reasoningEffort: byId("model-reasoning").value || null,
    timeoutSeconds: Number(byId("model-timeout").value),
    temperature: Number(byId("model-temperature").value),
    maxTextChars: Number(byId("model-max-text-chars").value),
  };
  const apiKey = byId("model-api-key").value;
  if (apiKey) model.apiKey = apiKey;
  const speech = {
    apiMode: byId("speech-api-mode").value,
    format: byId("speech-format").value,
    model: byId("speech-model").value.trim(),
    voice: byId("speech-voice").value.trim(),
  };
  const imageGeneration = {
    model: byId("image-model").value.trim(),
    generationEndpoint: byId("image-generation-endpoint").value.trim(),
    editEndpoint: byId("image-edit-endpoint").value.trim(),
  };
  return { revision: configSnapshot?.revision || "", model, features, speech, imageGeneration };
}

async function fetchModelsFor({ buttonId, messageId, selectId, inputId }) {
  const button = byId(buttonId);
  const message = byId(messageId);
  button.disabled = true;
  message.textContent = "正在读取模型列表…";
  try {
    const body = { baseUrl: byId("model-base-url").value.trim() };
    const apiKey = byId("model-api-key").value;
    if (apiKey) body.apiKey = apiKey;
    const response = await request("/api/models", { method: "POST", body: JSON.stringify(body) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "模型列表请求失败");
    const models = Array.isArray(result.models) ? result.models : [];
    const select = byId(selectId);
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = models.length ? `已获取 ${models.length} 个模型` : "获取模型后从列表选择";
    select.append(placeholder);
    for (const name of models) {
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      select.append(option);
    }
    modelListBaseUrl = byId("model-base-url").value.trim();
    const currentName = byId(inputId).value;
    if (currentName && models.includes(currentName)) select.value = currentName;
    else if (models.length && !currentName) {
      select.value = models[0];
      byId(inputId).value = models[0];
    }
    message.textContent = models.length ? `已获取全部 ${models.length} 个模型，可从下拉列表选择。` : "接口返回了空模型列表，请手动填写模型名。";
    message.className = "field-help " + (models.length ? "is-good" : "is-warn");
  } catch (error) {
    message.textContent = error.message || "模型列表请求失败，请手动填写模型名。";
    message.className = "field-help is-error";
  } finally {
    button.disabled = false;
  }
}

function fetchModels() {
  return fetchModelsFor({
    buttonId: "fetch-models",
    messageId: "model-list-message",
    selectId: "model-options",
    inputId: "model-name",
  });
}

async function saveConfig(event) {
  event.preventDefault();
  if (!configForm.checkValidity()) {
    configForm.reportValidity();
    showMessage("请先补全模型配置中的必填字段。", "error");
    return;
  }
  const button = byId("save-config");
  button.disabled = true;
  showMessage("正在校验并保存配置…", "info");
  try {
    const response = await request("/api/config", { method: "PUT", body: JSON.stringify(collectConfig()) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail || "配置保存失败");
    fillConfig(result);
    showMessage(result.restartRequired ? "配置已保存。请重启 Bot 服务后使新设置生效。" : "配置已保存并生效。", "success");
  } catch (error) {
    if (error.message !== "login required") showMessage(error.message || "配置保存失败，请稍后重试。", "error");
  } finally {
    button.disabled = false;
  }
}

function switchTab(name) {
  for (const button of tabButtons) {
    const active = button.dataset.tab === name;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-selected", String(active));
  }
  for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== `tab-${name}`;
  if (name === "config" && !configLoaded) loadConfig();
}

if (byId("qq-state")) {
  renderStatus();
  setInterval(() => { if (!document.hidden) renderStatus(); }, 10000);
  byId("restart-bot").addEventListener("click", restartBot);
  byId("qr-refresh").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    byId("qr-message").textContent = "正在获取新二维码…";
    byId("qr-message").hidden = false;
    try {
      const response = await request("/api/qq/refresh", { method: "POST" });
      if (response.ok) refreshQrImage();
      else byId("qr-message").textContent = response.status === 409 ? "QQ 已在线。" : "刷新失败，请稍后重试。";
    } catch { byId("qr-message").textContent = "刷新失败，请稍后重试。"; }
    finally { button.disabled = false; }
  });
}

if (configForm) {
  for (const button of tabButtons) button.addEventListener("click", () => switchTab(button.dataset.tab));
  byId("fetch-models").addEventListener("click", fetchModels);
  byId("fetch-speech-models").addEventListener("click", () => fetchModelsFor({
    buttonId: "fetch-speech-models",
    messageId: "speech-model-list-message",
    selectId: "speech-model-options",
    inputId: "speech-model",
  }));
  byId("fetch-image-models").addEventListener("click", () => fetchModelsFor({
    buttonId: "fetch-image-models",
    messageId: "image-model-list-message",
    selectId: "image-model-options",
    inputId: "image-model",
  }));
  byId("model-base-url").addEventListener("input", (event) => {
    if (event.currentTarget.value.trim() === modelListBaseUrl) return;
    byId("model-options").replaceChildren(new Option("获取模型后从列表选择", ""));
    byId("speech-model-options").replaceChildren(new Option("获取模型后从列表选择", ""));
    byId("image-model-options").replaceChildren(new Option("获取模型后从列表选择", ""));
  });
  byId("model-options").addEventListener("change", (event) => {
    if (event.currentTarget.value) byId("model-name").value = event.currentTarget.value;
  });
  byId("speech-model-options").addEventListener("change", (event) => {
    if (event.currentTarget.value) byId("speech-model").value = event.currentTarget.value;
  });
  byId("image-model-options").addEventListener("change", (event) => {
    if (event.currentTarget.value) byId("image-model").value = event.currentTarget.value;
  });
  configForm.addEventListener("submit", saveConfig);
  byId("reset-config").addEventListener("click", () => {
    if (configSnapshot) fillConfig(configSnapshot);
    if (configMessage) configMessage.hidden = true;
  });
}

if (byId("logout")) {
  byId("logout").addEventListener("click", async () => {
    try { await request("/api/logout", { method: "POST" }); }
    finally { window.location.replace("/login"); }
  });
}
