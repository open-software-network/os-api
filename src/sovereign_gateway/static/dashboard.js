const $ = (selector) => document.querySelector(selector);
const dialog = $("#create-dialog");

function money(microusd) {
  if (microusd === null || microusd === undefined) return "No limit";
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(microusd / 1_000_000);
}

function date(value) {
  return value ? new Intl.DateTimeFormat("en-US", { dateStyle: "medium" }).format(new Date(value)) : "Never";
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = String(value ?? "");
  return element.innerHTML;
}

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: "same-origin", ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  if (response.status === 401) { window.location.href = "/auth/start"; throw new Error("Sign in required"); }
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "Request failed");
  return body;
}

async function loadKeys() {
  const { data } = await request("/api/keys");
  $("#keys").innerHTML = data.length ? `<table><thead><tr><th>Name</th><th>Key</th><th>Expires</th><th>Spend</th><th>Status</th><th></th></tr></thead><tbody>${data.map((key) => `<tr><td><strong>${escapeHtml(key.name)}</strong></td><td><code>${escapeHtml(key.prefix)}</code></td><td>${date(key.expires_at)}</td><td>${money(key.spend_used_microusd)} / ${money(key.spend_limit_microusd)}</td><td><span class="pill ${key.status}">${escapeHtml(key.status.replaceAll("_", " "))}</span></td><td>${key.status === "active" ? `<button class="text-button revoke" data-id="${escapeHtml(key.id)}">Revoke</button>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="empty"><strong>No API keys yet</strong><span>Create a key to connect your first app.</span></div>`;
  document.querySelectorAll(".revoke").forEach((button) => button.addEventListener("click", async () => {
    if (!confirm("Revoke this API key? Requests using it will stop immediately.")) return;
    await request(`/api/keys/${encodeURIComponent(button.dataset.id)}`, { method: "DELETE" });
    await loadKeys();
  }));
}

async function loadUsage() {
  const { data } = await request("/api/usage");
  $("#usage").innerHTML = data.length ? data.slice(0, 12).map((row) => `<div class="usage-row"><div><strong>${escapeHtml(row.model || "Inference")}</strong><span>${date(row.created_at)} · ${escapeHtml(row.status)}</span></div><div><strong>${money(row.actual_microusd ?? row.estimate_microusd)}</strong><span>${Number(row.total_tokens || 0).toLocaleString()} tokens</span></div></div>`).join("") : `<div class="empty"><strong>No usage yet</strong><span>Completed API requests appear here.</span></div>`;
}

$("#open-create").addEventListener("click", () => dialog.showModal());
$("#close-create").addEventListener("click", () => dialog.close());
$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  const error = $("#form-error");
  error.classList.add("hidden");
  try {
    const expires = data.get("expires_in_days");
    const limit = data.get("spend_limit_usd");
    const body = { name: data.get("name"), expires_in_days: expires ? Number(expires) : null, spend_limit_usd: limit || null };
    const created = await request("/api/keys", { method: "POST", body: JSON.stringify(body) });
    dialog.close();
    $("#new-key").textContent = created.data.key;
    $("#key-reveal").classList.remove("hidden");
    await Promise.all([loadKeys(), loadUsage()]);
  } catch (err) {
    error.textContent = err.message;
    error.classList.remove("hidden");
  }
});
$("#copy-key").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("#new-key").textContent);
  $("#copy-key").textContent = "Copied";
});
$("#logout-button").addEventListener("click", async () => {
  await request("/auth/logout", { method: "POST", body: "{}" });
  window.location.href = "/";
});

Promise.all([loadKeys(), loadUsage()]).catch((error) => {
  $("#keys").innerHTML = `<div class="notice error">${escapeHtml(error.message)}</div>`;
});
