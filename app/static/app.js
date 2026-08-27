const API = "/api/v1";
const state = {
  config: null,
  services: [],
  summary: null,
  checks: [],
  incidents: [],
  scheduler: null,
};

const elements = {
  servicesBody: document.querySelector("#services-body"),
  historyList: document.querySelector("#history-list"),
  incidentList: document.querySelector("#incident-list"),
  serviceForm: document.querySelector("#service-form"),
  runAll: document.querySelector("#run-all"),
  refresh: document.querySelector("#refresh"),
  windowHours: document.querySelector("#window-hours"),
  toast: document.querySelector("#toast"),
  authButton: document.querySelector("#auth-button"),
  authDialog: document.querySelector("#auth-dialog"),
  authForm: document.querySelector("#auth-form"),
  apiKeyInput: document.querySelector("#api-key-input"),
  runScheduler: document.querySelector("#run-scheduler"),
  runRetention: document.querySelector("#run-retention"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatDate(value) {
  if (!value) return "Never";
  return new Intl.DateTimeFormat("en", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function formatRelativeDate(value) {
  if (!value) return "Never";
  const deltaSeconds = Math.round((new Date(value).getTime() - Date.now()) / 1000);
  const ranges = [
    [60, "second"],
    [60, "minute"],
    [24, "hour"],
    [7, "day"],
    [4.345, "week"],
    [12, "month"],
    [Number.POSITIVE_INFINITY, "year"],
  ];
  let valueInUnit = deltaSeconds;
  for (const [amount, unit] of ranges) {
    if (Math.abs(valueInUnit) < amount) {
      return new Intl.RelativeTimeFormat("en", { numeric: "auto" }).format(
        Math.round(valueInUnit),
        unit,
      );
    }
    valueInUnit /= amount;
  }
  return formatDate(value);
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "Ongoing";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

function humanizeState(value) {
  if (value === "never_checked") return "Not checked";
  return value;
}

function currentKey() {
  return sessionStorage.getItem("pulsewatch-admin-key") ?? "";
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.body) headers["Content-Type"] = "application/json";
  if (currentKey()) headers["X-API-Key"] = currentKey();
  const response = await fetch(path, { ...options, headers });

  if (!response.ok) {
    let message = `Request failed with HTTP ${response.status}`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
      if (Array.isArray(data.detail)) {
        message = data.detail.map((item) => item.msg).join(" · ");
      }
      if (data.detail?.message) message = data.detail.message;
    } catch (_) {
      // Preserve the generic HTTP error when the response is not JSON.
    }
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) return null;
  return response.json();
}

let toastTimer;
function showToast(message, type = "success") {
  window.clearTimeout(toastTimer);
  elements.toast.textContent = message;
  elements.toast.className = `toast visible ${type}`;
  toastTimer = window.setTimeout(() => {
    elements.toast.className = "toast";
  }, 3400);
}

function handleError(error) {
  showToast(error.message, "error");
  if (error.status === 401 && elements.authDialog.showModal) {
    elements.authDialog.showModal();
  }
}

function renderSystemStatus() {
  if (!state.config) return;
  const schedulerText = state.config.scheduler_enabled
    ? "● Scheduler enabled"
    : "○ Scheduler disabled";
  document.querySelector("#scheduler-badge").textContent = schedulerText;
  document.querySelector("#webhook-badge").textContent = state.config.webhook_configured
    ? "● Webhook configured"
    : "○ Webhook not configured";
  document.querySelector("#auth-badge").textContent = state.config.authentication_required
    ? "● Admin auth required"
    : "○ Local admin mode";
  document.querySelector("#retention-detail").textContent =
    `Keeps ${state.config.retention_days} days of check history`;
  elements.authButton.textContent = currentKey() ? "Admin key set" : "Admin key";

  if (state.scheduler) {
    const status = state.scheduler.running ? "Running" : "Stopped";
    const latest = state.scheduler.last_cycle_finished_at
      ? `Last cycle ${formatRelativeDate(state.scheduler.last_cycle_finished_at)}`
      : "No completed cycles";
    document.querySelector("#scheduler-detail").textContent =
      `${status} · ${latest} · ${state.scheduler.checks_executed} checks`;
  }
}

function renderMetrics() {
  const summary = state.summary;
  if (!summary) return;
  document.querySelector("#metric-uptime").textContent =
    summary.uptime_percentage === null ? "—" : `${summary.uptime_percentage}%`;
  document.querySelector("#metric-latency").textContent =
    summary.average_latency_ms === null ? "—" : `${summary.average_latency_ms} ms`;
  document.querySelector("#metric-incidents").textContent = summary.open_incidents;
  document.querySelector("#metric-services").textContent = summary.services_total;
  document.querySelector("#metric-uptime-note").textContent = summary.checks_total
    ? `${summary.checks_total} checks in selected window`
    : "No checks recorded";
  document.querySelector("#metric-incidents-note").textContent = summary.open_incidents
    ? `${summary.open_incidents} unresolved ${summary.open_incidents === 1 ? "incident" : "incidents"}`
    : "No active failures";

  const online = summary.healthy_services + summary.degraded_services;
  document.querySelector("#metric-services-note").textContent = summary.services_total
    ? `${online} responding · ${summary.down_services} down`
    : "Nothing monitored yet";
}

function renderServices() {
  const metrics = new Map((state.summary?.services ?? []).map((item) => [item.id, item]));
  if (!state.services.length) {
    elements.servicesBody.innerHTML = `
      <tr class="empty-row">
        <td colspan="6">Add your first endpoint to start scheduled monitoring.</td>
      </tr>`;
    return;
  }

  elements.servicesBody.innerHTML = state.services
    .map((service) => {
      const metric = metrics.get(service.id) ?? {
        state: service.active ? "never_checked" : "paused",
        last_latency_ms: null,
        uptime_percentage: null,
        last_checked_at: null,
        last_status_code: null,
        next_check_at: service.next_check_at,
      };
      const visualState = service.active ? metric.state : "paused";
      const response =
        metric.last_latency_ms === null
          ? "—"
          : `${metric.last_latency_ms} ms · HTTP ${metric.last_status_code ?? "—"}`;
      const uptime =
        metric.uptime_percentage === null ? "—" : `${metric.uptime_percentage}%`;
      const tagLine = service.tags.length
        ? service.tags.map((tag) => `#${tag}`).join(" ")
        : service.description ?? service.url;

      return `
        <tr>
          <td class="service-name">
            <strong>${escapeHtml(service.name)}</strong>
            <span title="${escapeHtml(service.url)}">${escapeHtml(tagLine)}</span>
          </td>
          <td>
            <span class="status-chip status-${visualState}">${escapeHtml(humanizeState(visualState))}</span>
          </td>
          <td>${escapeHtml(response)}</td>
          <td>${escapeHtml(uptime)}</td>
          <td title="${escapeHtml(formatDate(metric.next_check_at))}">
            ${service.active ? escapeHtml(formatRelativeDate(metric.next_check_at)) : "Paused"}
          </td>
          <td>
            <div class="row-actions">
              <button class="row-action" type="button" data-action="check" data-id="${service.id}" aria-label="Run check">▶</button>
              <button class="row-action" type="button" data-action="toggle" data-id="${service.id}" aria-label="${service.active ? "Pause" : "Resume"} service">
                ${service.active ? "Ⅱ" : "↗"}
              </button>
              <button class="row-action danger" type="button" data-action="delete" data-id="${service.id}" aria-label="Delete service">×</button>
            </div>
          </td>
        </tr>`;
    })
    .join("");
}

function renderHistory() {
  if (!state.checks.length) {
    elements.historyList.innerHTML = '<li class="history-empty">No check history yet.</li>';
    return;
  }
  const names = new Map(state.services.map((service) => [service.id, service.name]));
  elements.historyList.innerHTML = state.checks
    .slice(0, 20)
    .map((check) => {
      const serviceName = names.get(check.service_id) ?? `Service #${check.service_id}`;
      const resultText = check.is_healthy
        ? `HTTP ${check.status_code} · ${check.latency_ms ?? "—"} ms`
        : check.error_message ?? `HTTP ${check.status_code ?? "—"}`;
      return `
        <li class="history-item ${check.is_healthy ? "healthy" : "failed"}">
          <span class="history-dot" aria-hidden="true"></span>
          <div class="history-copy">
            <strong>${escapeHtml(serviceName)}</strong>
            <span>${escapeHtml(resultText)} · ${escapeHtml(check.trigger)}</span>
          </div>
          <span class="history-meta" title="${escapeHtml(formatDate(check.checked_at))}">
            ${escapeHtml(formatRelativeDate(check.checked_at))}<br />
            ${check.attempts} ${check.attempts === 1 ? "attempt" : "attempts"}
          </span>
        </li>`;
    })
    .join("");
}

function renderIncidents() {
  if (!state.incidents.length) {
    elements.incidentList.innerHTML =
      '<li class="history-empty">No incidents recorded. All clear.</li>';
    return;
  }
  elements.incidentList.innerHTML = state.incidents
    .slice(0, 20)
    .map((incident) => {
      const resolved = incident.status === "resolved";
      const detail = incident.last_error_message ?? "Endpoint check failed";
      return `
        <li class="history-item ${resolved ? "healthy" : "failed"}">
          <span class="history-dot" aria-hidden="true"></span>
          <div class="history-copy">
            <strong>${escapeHtml(incident.service_name)} · ${escapeHtml(incident.status)}</strong>
            <span>${escapeHtml(detail)} · ${incident.failure_count} failures</span>
          </div>
          <span class="history-meta" title="${escapeHtml(formatDate(incident.opened_at))}">
            ${escapeHtml(formatRelativeDate(incident.opened_at))}<br />
            ${escapeHtml(formatDuration(incident.duration_seconds))}
          </span>
        </li>`;
    })
    .join("");
}

async function refreshDashboard({ quiet = false } = {}) {
  elements.refresh.firstElementChild?.classList.add("spin");
  try {
    const hours = elements.windowHours.value;
    const [config, services, summary, checks, incidents, scheduler] = await Promise.all([
      api(`${API}/config`),
      api(`${API}/services?limit=500`),
      api(`${API}/summary?window_hours=${hours}`),
      api(`${API}/checks?limit=30`),
      api(`${API}/incidents?limit=30`),
      api(`${API}/operations/scheduler`),
    ]);
    Object.assign(state, { config, services, summary, checks, incidents, scheduler });
    renderSystemStatus();
    renderMetrics();
    renderServices();
    renderHistory();
    renderIncidents();
    if (!quiet) showToast("Dashboard refreshed");
  } catch (error) {
    handleError(error);
  } finally {
    elements.refresh.firstElementChild?.classList.remove("spin");
  }
}

async function runChecks(serviceIds = null) {
  elements.runAll.disabled = true;
  try {
    const result = await api(`${API}/checks/run`, {
      method: "POST",
      body: JSON.stringify(serviceIds ? { service_ids: serviceIds } : {}),
    });
    const message = result.failed
      ? `${result.healthy} healthy · ${result.failed} failed`
      : `${result.healthy} ${result.healthy === 1 ? "service" : "services"} healthy`;
    showToast(message, result.failed ? "error" : "success");
    await refreshDashboard({ quiet: true });
  } catch (error) {
    handleError(error);
  } finally {
    elements.runAll.disabled = false;
  }
}

elements.serviceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = elements.serviceForm.querySelector('button[type="submit"]');
  submit.disabled = true;
  const form = new FormData(elements.serviceForm);
  const payload = {
    name: form.get("name"),
    description: form.get("description") || null,
    url: form.get("url"),
    method: form.get("method"),
    expected_status: Number(form.get("expected_status")),
    timeout_seconds: Number(form.get("timeout_seconds")),
    retry_count: Number(form.get("retry_count")),
    check_interval_seconds: Number(form.get("check_interval_seconds")),
    tags: String(form.get("tags") ?? "")
      .split(",")
      .map((tag) => tag.trim())
      .filter(Boolean),
  };
  try {
    await api(`${API}/services`, {
      method: "POST",
      body: JSON.stringify(payload),
    });
    elements.serviceForm.reset();
    showToast("Service added and scheduled");
    await refreshDashboard({ quiet: true });
  } catch (error) {
    handleError(error);
  } finally {
    submit.disabled = false;
  }
});

elements.servicesBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const serviceId = Number(button.dataset.id);
  const service = state.services.find((item) => item.id === serviceId);

  if (button.dataset.action === "check") {
    button.disabled = true;
    await runChecks([serviceId]);
    button.disabled = false;
  }

  if (button.dataset.action === "toggle") {
    try {
      await api(`${API}/services/${serviceId}`, {
        method: "PATCH",
        body: JSON.stringify({ active: !service.active }),
      });
      showToast(service.active ? "Service paused" : "Service resumed");
      await refreshDashboard({ quiet: true });
    } catch (error) {
      handleError(error);
    }
  }

  if (button.dataset.action === "delete") {
    const confirmed = window.confirm(
      `Delete ${service?.name ?? "this service"} and all related history?`,
    );
    if (!confirmed) return;
    try {
      await api(`${API}/services/${serviceId}`, { method: "DELETE" });
      showToast("Service and related records deleted");
      await refreshDashboard({ quiet: true });
    } catch (error) {
      handleError(error);
    }
  }
});

elements.authButton.addEventListener("click", () => {
  elements.apiKeyInput.value = currentKey();
  elements.authDialog.showModal();
});

elements.authForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") {
    elements.authDialog.close();
    return;
  }
  const key = elements.apiKeyInput.value.trim();
  if (key) sessionStorage.setItem("pulsewatch-admin-key", key);
  else sessionStorage.removeItem("pulsewatch-admin-key");
  elements.authDialog.close();
  renderSystemStatus();
  showToast(key ? "Admin key stored for this tab" : "Admin key cleared");
});

elements.runScheduler.addEventListener("click", async () => {
  elements.runScheduler.disabled = true;
  try {
    const result = await api(`${API}/operations/scheduler/run`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast(`Scheduler executed ${result.checks_executed} due checks`);
    await refreshDashboard({ quiet: true });
  } catch (error) {
    handleError(error);
  } finally {
    elements.runScheduler.disabled = false;
  }
});

elements.runRetention.addEventListener("click", async () => {
  elements.runRetention.disabled = true;
  try {
    const result = await api(`${API}/operations/retention/run`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    showToast(`Removed ${result.deleted_check_results} expired check results`);
    await refreshDashboard({ quiet: true });
  } catch (error) {
    handleError(error);
  } finally {
    elements.runRetention.disabled = false;
  }
});

elements.runAll.addEventListener("click", () => runChecks());
elements.refresh.addEventListener("click", () => refreshDashboard());
elements.windowHours.addEventListener("change", () => refreshDashboard({ quiet: true }));

refreshDashboard({ quiet: true });
