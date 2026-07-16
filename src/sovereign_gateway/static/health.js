const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

function label(value) {
  return String(value || "unknown").replaceAll("_", " ").replace(/\b\w/g, (character) => character.toUpperCase());
}

function percent(value) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(value >= 0.999 ? 2 : 1)}%`;
}

function duration(seconds) {
  const value = Number(seconds || 0);
  if (value < 60) return `${value}s`;
  if (value < 3600) return `${Math.floor(value / 60)}m`;
  if (value < 86400) return `${Math.floor(value / 3600)}h ${Math.floor((value % 3600) / 60)}m`;
  return `${Math.floor(value / 86400)}d ${Math.floor((value % 86400) / 3600)}h`;
}

function renderChart(points) {
  const chart = $("#traffic-chart");
  const maximum = Math.max(1, ...points.map((point) => point.requests));
  const width = 600;
  const baseline = 140;
  const slot = width / points.length;
  const bars = points.map((point, index) => {
    const requestHeight = Math.max(point.requests ? 5 : 1, (point.requests / maximum) * 120);
    const errorHeight = Math.max(point.errors ? 5 : 0, (point.errors / maximum) * 120);
    const title = `${new Date(point.at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}: ${point.requests} requests, ${point.errors} errors`;
    const x = index * slot + 2;
    return `<g><title>${escapeHtml(title)}</title><rect class="request-bar" x="${x}" y="${baseline - requestHeight}" width="${Math.max(3, slot - 4)}" height="${requestHeight}" rx="2"></rect>${errorHeight ? `<rect class="error-bar" x="${x + Math.max(3, slot - 4) - 3}" y="${baseline - errorHeight}" width="3" height="${errorHeight}" rx="1"></rect>` : ""}</g>`;
  }).join("");
  chart.innerHTML = `<svg viewBox="0 0 ${width} 145" preserveAspectRatio="none" aria-hidden="true">${bars}</svg>`;
}

function renderComponents(components) {
  $("#component-list").innerHTML = components.map((component) => `<div class="component-row"><span class="mini-orb ${escapeHtml(component.status)}"></span><div><strong>${escapeHtml(component.name)}</strong><small>${escapeHtml(component.detail)}</small></div><em class="state-label ${escapeHtml(component.status)}">${escapeHtml(label(component.status))}</em></div>`).join("");
}

function renderProviders(providers) {
  $("#provider-grid").innerHTML = providers.length ? providers.map((provider) => {
    const endpoints = provider.endpoints.map((endpoint) => `<div class="endpoint-row"><span class="mini-orb ${escapeHtml(endpoint.status)}"></span><div><strong>${escapeHtml(endpoint.model)}</strong><small>${escapeHtml(endpoint.privacy)} · ${Number(endpoint.attempts).toLocaleString()} attempts</small></div><div class="endpoint-stats"><strong>${percent(endpoint.success_rate)}</strong><small>${endpoint.average_latency_ms === null ? "No latency samples" : `${Math.round(endpoint.average_latency_ms)} ms avg`}</small></div></div>`).join("");
    return `<article class="provider"><header><div><span class="mini-orb ${escapeHtml(provider.status)}"></span><strong>${escapeHtml(provider.name)}</strong></div><em class="state-label ${escapeHtml(provider.status)}">${escapeHtml(label(provider.status))}</em></header><p>${provider.endpoints.length} route${provider.endpoints.length === 1 ? "" : "s"} · ${escapeHtml(provider.regions.join(", ") || "global")}</p><div class="endpoint-list">${endpoints || '<div class="health-loading compact">No enabled routes</div>'}</div></article>`;
  }).join("") : '<div class="health-loading">No providers configured</div>';
}

function render(snapshot) {
  const fiveMinutes = snapshot.requests.last_5_minutes;
  const hourly = snapshot.requests.last_hour;
  const banner = $("#health-banner");
  banner.className = `health-banner ${snapshot.status}`;
  $("#health-orb").className = `health-orb ${snapshot.status}`;
  $("#health-title").textContent = snapshot.status === "operational" ? "All systems operational" : snapshot.status === "degraded" ? "Service is degraded" : "Service outage detected";
  $("#health-subtitle").textContent = `${snapshot.service.name} · ${snapshot.service.region}`;
  $("#health-updated").textContent = new Date(snapshot.checked_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  $("#region-label").textContent = snapshot.service.region;
  $("#metric-availability").textContent = percent(hourly.availability);
  $("#metric-availability-detail").textContent = hourly.requests ? `${hourly.errors} server error${hourly.errors === 1 ? "" : "s"} / ${hourly.requests.toLocaleString()} requests` : "No traffic yet";
  $("#metric-requests").textContent = fiveMinutes.requests.toLocaleString();
  $("#metric-request-detail").textContent = `${fiveMinutes.errors} server errors`;
  $("#metric-latency").textContent = hourly.p95_latency_ms === null ? "—" : `${Math.round(hourly.p95_latency_ms)} ms`;
  $("#metric-uptime").textContent = duration(snapshot.service.uptime_seconds);
  $("#metric-version").textContent = `Version ${snapshot.service.version}`;
  renderChart(snapshot.requests.series);
  renderComponents(snapshot.readiness.components);
  renderProviders(snapshot.providers);
}

async function refresh() {
  try {
    const response = await fetch("/api/health", { credentials: "same-origin", cache: "no-store" });
    if (response.status === 401) {
      window.location.href = "/auth/start?next=/health";
      return;
    }
    if (response.status === 403) throw new Error("This account is not authorized to view health data.");
    if (!response.ok) throw new Error("Health monitor unavailable");
    render(await response.json());
  } catch (error) {
    $("#health-banner").className = "health-banner outage";
    $("#health-orb").className = "health-orb outage";
    $("#health-title").textContent = "Monitor unavailable";
    $("#health-subtitle").textContent = error.message;
  }
}

$("#health-logout").addEventListener("click", async () => {
  await fetch("/auth/logout", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: "{}" });
  window.location.href = "/";
});

refresh();
setInterval(refresh, 15_000);
