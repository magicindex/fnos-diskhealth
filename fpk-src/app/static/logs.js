(() => {
  const KEY = "disk-health-theme";
  const root = document.documentElement;
  const theme = document.getElementById("theme-toggle");
  const current = () => root.dataset.theme === "light" ? "light" : "dark";
  const apply = (value, persist = true) => {
    const next = value === "light" ? "light" : "dark";
    root.dataset.theme = next;
    root.style.colorScheme = next;
    if (persist) { try { localStorage.setItem(KEY, next); } catch (e) {} }
    if (theme) {
      const label = next === "light" ? "切换到暗色主题" : "切换到亮色主题";
      theme.title = label; theme.setAttribute("aria-label", label);
    }
  };
  apply(current(), false);
  theme?.addEventListener("click", () => apply(current() === "light" ? "dark" : "light"));

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
  const $ = (selector) => document.querySelector(selector);
  const state = {
    user: null,
    tab: "events",
    audit: { offset: 0, limit: 50, total: 0, filters: {}, loaded: false },
    eventsLoaded: false,
  };

  async function session() {
    try {
      const res = await fetch("api/session", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      state.user = data.user || {};
    } catch (e) { state.user = null; }
    if (state.user?.admin) {
      $('.log-tab[data-tab="audit"]').hidden = false;
    }
    const requested = new URLSearchParams(location.search).get("tab");
    if (requested === "audit" && state.user?.admin) switchTab("audit");
    else if (requested === "export") switchTab("export");
    else switchTab("events");
  }

  /* Audit */
  const filters = () => ({
    user: $("#audit-user").value.trim(),
    action: $("#audit-action").value.trim(),
    result: $("#audit-result").value,
    from: $("#audit-from").value,
    to: $("#audit-to").value,
    q: $("#audit-q").value.trim(),
  });

  const queryString = () => {
    const params = new URLSearchParams({ limit: String(state.audit.limit), offset: String(state.audit.offset) });
    Object.entries(state.audit.filters).forEach(([key, value]) => { if (value) params.set(key, value); });
    return params.toString();
  };

  async function loadAudit() {
    if (!state.user?.admin) return;
    $("#audit-summary").textContent = "加载中…";
    $("#audit-body").innerHTML = "";
    try {
      const res = await fetch(`api/admin/audit?${queryString()}`, { cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      state.audit.total = Number(data.total || 0);
      state.audit.loaded = true;
      const items = data.items || [];
      $("#audit-body").innerHTML = items.map((item) => `
        <tr>
          <td class="mono">${esc(item.time || "—")}</td>
          <td>${esc(item.user || (item.uid ? `用户 ${item.uid}` : "未知用户"))}<small>${esc(item.uid || "")}</small></td>
          <td class="mono">${esc(item.action || "—")}</td>
          <td>${esc(item.target || "—")}</td>
          <td><span class="audit-result ${item.result === "ok" ? "ok" : "denied"}">${esc(item.result || "—")}</span></td>
          <td>${item.read_only ? "yes" : "no"}</td>
          <td class="audit-detail" title="${esc(item.detail || "")}">${esc(item.detail || "—")}</td>
        </tr>`).join("");
      $("#audit-empty").hidden = items.length > 0;
      const pages = Math.max(1, Math.ceil(state.audit.total / state.audit.limit));
      const page = Math.floor(state.audit.offset / state.audit.limit) + 1;
      $("#audit-page").textContent = `第 ${page} / ${pages} 页 · 共 ${state.audit.total} 条`;
      $("#audit-summary").textContent = `共 ${state.audit.total} 条记录 · 每页 ${state.audit.limit} 条 · 管理日志`;
      $("#audit-prev").disabled = state.audit.offset <= 0;
      $("#audit-next").disabled = state.audit.offset + state.audit.limit >= state.audit.total;
      const list = $("#audit-users");
      if (list) list.innerHTML = (data.users || []).map((name) => `<option value="${esc(name)}"></option>`).join("");
    } catch (error) {
      $("#audit-summary").textContent = `读取失败：${error.message}`;
      $("#audit-empty").hidden = false;
      $("#audit-empty").textContent = "审计日志读取失败。";
      $("#audit-page").textContent = "—";
    }
  }

  /* Events */
  function eventTime(event) {
    if (event.time) return event.time;
    if (event.ts) {
      const date = new Date(Number(event.ts) * 1000);
      return Number.isNaN(date.getTime()) ? String(event.ts) : date.toLocaleString();
    }
    return "—";
  }

  async function loadEvents() {
    $("#events-summary").textContent = "加载中…";
    $("#events-body").innerHTML = "";
    try {
      const res = await fetch("api/events?limit=200", { cache: "no-store" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      const events = (data.events || []).slice().reverse();
      state.eventsLoaded = true;
      $("#events-body").innerHTML = events.map((event) => {
        const disk = event.disk_id || event.id || event.disk || "—";
        const from = event.from || event.old || "—";
        const to = event.to || event.new || "—";
        const message = event.message || event.reason || event.detail || "";
        return `<tr><td class="mono">${esc(eventTime(event))}</td><td class="mono">${esc(disk)}</td><td><span class="mono">${esc(from)}</span> → <span class="mono">${esc(to)}</span></td><td>${esc(message || "—")}</td></tr>`;
      }).join("");
      $("#events-empty").hidden = events.length > 0;
      $("#events-summary").textContent = `最近 ${events.length} 条健康变化事件 · 本地 events.jsonl`;
    } catch (error) {
      $("#events-summary").textContent = `读取失败：${error.message}`;
      $("#events-empty").hidden = false;
      $("#events-empty").textContent = "健康事件读取失败。";
    }
  }

  function switchTab(tab) {
    if (tab === "audit" && !state.user?.admin) tab = "events";
    state.tab = tab;
    document.querySelectorAll(".log-tab").forEach((button) => button.classList.toggle("active", button.dataset.tab === tab));
    $("#tab-audit").hidden = tab !== "audit";
    $("#tab-events").hidden = tab !== "events";
    $("#tab-export").hidden = tab !== "export";
    if (tab === "audit" && !state.audit.loaded) loadAudit();
    if (tab === "events" && !state.eventsLoaded) loadEvents();
  }

  document.querySelectorAll(".log-tab").forEach((button) => {
    button.addEventListener("click", () => switchTab(button.dataset.tab));
  });
  $("#audit-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    state.audit.filters = filters();
    state.audit.offset = 0;
    loadAudit();
  });
  $("#audit-reset").addEventListener("click", () => {
    $("#audit-filters").reset();
    state.audit.filters = {};
    state.audit.offset = 0;
    loadAudit();
  });
  $("#audit-prev").addEventListener("click", () => { state.audit.offset = Math.max(0, state.audit.offset - state.audit.limit); loadAudit(); });
  $("#audit-next").addEventListener("click", () => { state.audit.offset += state.audit.limit; loadAudit(); });
  $("#events-refresh").addEventListener("click", loadEvents);
  document.querySelectorAll("[data-export]").forEach((button) => {
    button.addEventListener("click", () => {
      const format = button.dataset.export;
      window.location.href = `api/export?format=${encodeURIComponent(format)}`;
    });
  });

  session();
})();
