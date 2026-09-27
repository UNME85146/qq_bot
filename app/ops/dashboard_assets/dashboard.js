"use strict";

const byId = (id) => document.getElementById(id);
const loginForm = byId("login-form");
const statusError = byId("status-error");

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", cache: "no-store", ...options });
  if (response.status === 401 && !loginForm) {
    window.location.replace("/login");
    throw new Error("login required");
  }
  return response;
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
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: byId("password").value }),
      });
      if (response.ok) window.location.replace("/");
      else { error.textContent = response.status === 429 ? "尝试过多，请稍后再试。" : "口令无效，请重试。"; error.hidden = false; }
    } catch { error.textContent = "暂时无法连接后台。"; error.hidden = false; }
    finally { button.disabled = false; }
  });
}

function setState(id, text, variant) {
  const node = byId(id);
  node.textContent = text;
  node.className = variant;
}

function setMetric(name, percent, detail) {
  const meter = byId(`${name}-meter`);
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

async function refreshQrImage() {
  const image = byId("qr-image");
  const message = byId("qr-message");
  image.hidden = true;
  image.onload = () => { image.hidden = false; message.hidden = true; };
  image.onerror = () => { image.hidden = true; message.textContent = "没有可用的新二维码，请刷新。"; message.hidden = false; };
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

if (byId("qq-state")) {
  renderStatus();
  setInterval(() => { if (!document.hidden) renderStatus(); }, 10000);
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
  byId("logout").addEventListener("click", async () => {
    try { await request("/api/logout", { method: "POST" }); }
    finally { window.location.replace("/login"); }
  });
}
