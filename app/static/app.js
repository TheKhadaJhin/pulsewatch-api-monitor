const state = {
  services: [],
  summary: null,
  checks: [],
};

const elements = {
  servicesBody: document.querySelector("#services-body"),
  historyList: document.querySelector("#history-list"),
  serviceForm: document.querySelector("#service-form"),
  runAll: document.querySelector("#run-all"),
  refresh: document.querySelector("#refresh"),
  windowHours: document.querySelector("#window-hours"),
  toast: document.querySelector("#toast"),
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

function humanizeState(value) {
  return value === "never_checked" ? "Not checked" : value;
}

async function api(path, options = {}) {
  const headers = { ...options.headers };
  if (options.body) headers["Content-Type"] = "application/json";
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
    throw new Error(message);
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
  }, 3200);
}

function renderMetrics() {
  const summary = state.summary;
  if (!summary) return;

  document.querySelector("#metric-uptime").textContent =
    summary.uptime_percentage === null ? "—" : `${summary.uptime_percentage}%`;
  document.querySelector("#metric-latency").textContent =
    summary.average_latency_ms === null ? "—" : `${summary.average_latency_ms} ms`;
  document.querySelector("#metric-incidents").textContent = summary.incidents_total;
  document.querySelector("#metric-services").textContent = summary.services_total;

  const checksNote = summary.checks_total
    ? `${summary.checks_total} checks in the selected window`
    : "No checks recorded";
  document.querySelector("#metric-uptime-note").textContent = checksNote;
  document.querySelector("#metric-incidents-note").textContent =
    summary.incidents_total === 1 ? "1 failed check detected" : "Failed checks in selected window";

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
        <td colspan="6">Add your first endpoint to start monitoring.</td>
      </tr>`;
    return;
  }

  elements.servicesBody.innerHTML = state.services
    .map((service) => {
      const metric = metrics.get(service.id) ?? {
        state: "never_checked",
        last_latency_ms: null,
        uptime_percentage: null,
        last_checked_at: null,
        last_status_code: null,
      };
      const response = metric.last_latency_ms === null
        ? "—"
        : `${metric.last_latency_ms} ms · HTTP ${metric.last_status_code ?? "—"}`;
      const uptime = metric.uptime_percentage === null ? "—" : `${metric.uptime_percentage}%`;

      return `
        <tr>
          <td class="service-name">
            <strong>${escapeHtml(service.name)}</strong>
            <span title="${escapeHtml(service.url)}">${escapeHtml(service.url)}</span>
          </td>
          <td>
            <span class="status-chip status-${metric.state}">${escapeHtml(humanizeState(metric.state))}</span>
          </td>
          <td>${escapeHtml(response)}</td>
          <td>${escapeHtml(uptime)}</td>
          <td title="${escapeHtml(formatDate(metric.last_checked_at))}">${escapeHtml(formatRelativeDate(metric.last_checked_at))}</td>
          <td>
            <div class="row-actions">
              <button class="row-action" type="button" data-action="check" data-id="${service.id}" title="Run check">▶</button>
              <button class="row-action danger" type="button" data-action="delete" data-id="${service.id}" title="Delete service">×</button>
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
            <span>${escapeHtml(resultText)}</span>
          </div>
          <span class="history-meta" title="${escapeHtml(formatDate(check.checked_at))}">
            ${escapeHtml(formatRelativeDate(check.checked_at))}<br />
            ${check.attempts} ${check.attempts === 1 ? "attempt" : "attempts"}
          </span>
        </li>`;
    })
    .join("");
}

async function refreshDashboard({ quiet = false } = {}) {
  elements.refresh.firstElementChild?.classList.add("spin");
  try {
    const hours = elements.windowHours.value;
    const [services, summary, checks] = await Promise.all([
      api("/api/services"),
      api(`/api/summary?window_hours=${hours}`),
      api("/api/checks?limit=30"),
    ]);
    state.services = services;
    state.summary = summary;
    state.checks = checks;
    renderMetrics();
    renderServices();
    renderHistory();
    if (!quiet) showToast("Dashboard refreshed");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    elements.refresh.firstElementChild?.classList.remove("spin");
  }
}

async function runChecks(serviceIds = null) {
  elements.runAll.disabled = true;
  const payload = serviceIds ? { service_ids: serviceIds } : {};
  try {
    const result = await api("/api/checks/run", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    const message = result.failed
      ? `${result.healthy} healthy · ${result.failed} failed`
      : `${result.healthy} ${result.healthy === 1 ? "service" : "services"} healthy`;
    showToast(message, result.failed ? "error" : "success");
    await refreshDashboard({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
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
    url: form.get("url"),
    method: form.get("method"),
    expected_status: Number(form.get("expected_status")),
    timeout_seconds: Number(form.get("timeout_seconds")),
    retry_count: Number(form.get("retry_count")),
  };

  try {
    await api("/api/services", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    elements.serviceForm.reset();
    showToast("Service added successfully");
    await refreshDashboard({ quiet: true });
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

elements.servicesBody.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const serviceId = Number(button.dataset.id);

  if (button.dataset.action === "check") {
    button.disabled = true;
    await runChecks([serviceId]);
    button.disabled = false;
  }

  if (button.dataset.action === "delete") {
    const service = state.services.find((item) => item.id === serviceId);
    const confirmed = window.confirm(`Delete ${service?.name ?? "this service"} and its check history?`);
    if (!confirmed) return;
    try {
      await api(`/api/services/${serviceId}`, { method: "DELETE" });
      showToast("Service deleted");
      await refreshDashboard({ quiet: true });
    } catch (error) {
      showToast(error.message, "error");
    }
  }
});

elements.runAll.addEventListener("click", () => runChecks());
elements.refresh.addEventListener("click", () => refreshDashboard());
elements.windowHours.addEventListener("change", () => refreshDashboard({ quiet: true }));

refreshDashboard({ quiet: true });
