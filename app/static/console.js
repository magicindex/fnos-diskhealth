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

  const $ = (sel) => document.querySelector(sel);
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TOKEN_KEY = "disk-health-console-token";
  const state = { token: sessionStorage.getItem(TOKEN_KEY) || "", inventory: null, op: "create_vd" };

  function toast(message, error = false) {
    const el = document.createElement("div");
    el.className = `toast${error ? " error" : ""}`;
    el.textContent = message;
    document.body.appendChild(el);
    requestAnimationFrame(() => el.classList.add("show"));
    setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 250); }, 3200);
  }

  async function api(path, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (state.token) headers["X-Console-Token"] = state.token;
    if (options.body) headers["Content-Type"] = "application/json";
    const res = await fetch(path, { ...options, headers, cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  function bindRisk() {
    const ack = $("#risk-ack");
    const sync = () => {
      const ok = !!ack?.checked;
      ["#setup-submit", "#login-submit"].forEach((sel) => {
        const button = $(sel);
        if (button) button.disabled = !ok;
      });
    };
    ack?.addEventListener("change", sync);
    sync();
  }

  function words(prefix) {
    return [1, 2, 3].map((i) => $(`#${prefix}-w${i}`).value.trim()).filter(Boolean);
  }

  async function loadState() {
    const data = await api("api/console/state");
    if (!data.configured) {
      $("#console-setup").hidden = false;
      $("#console-login").hidden = true;
      $("#console-gate").hidden = false;
      $("#console-dashboard").hidden = true;
      return;
    }
    if (data.authenticated) {
      showDashboard();
      return;
    }
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    $("#console-setup").hidden = true;
    $("#console-login").hidden = false;
    $("#console-gate").hidden = false;
    $("#console-dashboard").hidden = true;
  }

  async function enterWith(wordsList) {
    const data = await api("api/console/login", { method: "POST", body: JSON.stringify({ words: wordsList }) });
    state.token = data.token || "";
    sessionStorage.setItem(TOKEN_KEY, state.token);
    await showDashboard();
  }

  async function setup() {
    const list = words("setup");
    if (list.length !== 3) { $("#setup-hint").textContent = "请输入 3 组单词"; return; }
    try {
      await api("api/console/setup", { method: "POST", body: JSON.stringify({ words: list }) });
      $("#setup-hint").textContent = "安全码已创建，正在进入…";
      await enterWith(list);
    } catch (error) {
      $("#setup-hint").textContent = error.message;
    }
  }

  async function login() {
    const list = words("login");
    if (list.length !== 3) { $("#login-hint").textContent = "请输入 3 组单词"; return; }
    try {
      await enterWith(list);
    } catch (error) {
      $("#login-hint").textContent = error.message;
    }
  }

  async function showDashboard() {
    $("#console-gate").hidden = true;
    $("#console-dashboard").hidden = false;
    await loadInventory();
  }

  async function loadInventory() {
    const data = await api("api/console/inventory");
    state.inventory = data;
    renderInventory();
    renderFields();
  }

  function renderInventory() {
    const wrap = $("#console-inventory");
    const controllers = state.inventory?.controllers || [];
    if (!controllers.length) { wrap.textContent = "没有可控制的硬件 RAID 控制器。"; return; }
    wrap.innerHTML = controllers.map((ctrl) => `
      <div class="console-ctrl">
        <div class="console-ctrl-head"><b>${esc(ctrl.model)}</b><span class="mono">${esc(ctrl.id)}</span></div>
        <div class="console-vds">${(ctrl.vds || []).map((vd) => `<span class="console-vd">${esc(vd.id)} · ${esc(vd.level)} · ${esc(vd.size)} · ${esc(vd.state)}</span>`).join("") || `<span class="console-empty">无虚拟盘</span>`}</div>
        <div class="console-pds">${(ctrl.pds || []).map((pd) => `<span class="console-pd">${esc(pd.id)} · ${esc(pd.state)}${pd.vd_id ? ` · ${esc(pd.vd_id)}` : ""}</span>`).join("")}</div>
      </div>`).join("");
  }

  function controllerOptions() {
    return (state.inventory?.controllers || []).map((c) => `<option value="${esc(c.id)}">${esc(c.id)} · ${esc(c.model)}</option>`).join("");
  }

  function vdOptions() {
    const out = [];
    (state.inventory?.controllers || []).forEach((c) => (c.vds || []).forEach((vd) => out.push(`<option value="${esc(vd.id)}">${esc(vd.id)} · ${esc(vd.level)} · ${esc(vd.size)}</option>`)));
    return out.join("");
  }

  function pdOptions(unassigned = false) {
    const out = [];
    (state.inventory?.controllers || []).forEach((c) => (c.pds || []).forEach((pd) => {
      if (unassigned && pd.vd_id) return;
      out.push(`<option value="${esc(pd.id)}">${esc(pd.id)} · ${esc(pd.bay || "")} · ${esc(pd.state || "")}</option>`);
    }));
    return out.join("");
  }

  function renderFields() {
    const op = $("#op-kind").value;
    state.op = op;
    const fields = $("#op-fields");
    let html = "";
    if (op === "create_vd") {
      html = `<label class="console-field">控制器<select id="f-controller">${controllerOptions()}</select></label>
        <label class="console-field">RAID 级别<select id="f-level"><option value="1">RAID1</option><option value="5">RAID5</option><option value="6">RAID6</option><option value="10">RAID10</option><option value="0">RAID0</option></select></label>
        <label class="console-field">物理盘（可多选）<select id="f-drives" multiple size="6">${pdOptions(true)}</select></label>
        <div class="console-inline"><label class="console-field">条带<select id="f-strip"><option>256</option><option>64</option><option>128</option><option>512</option><option>1024</option></select></label><label class="console-field">名称<input id="f-name" type="text" maxlength="15" placeholder="可选"></label></div>`;
    } else if (op === "delete_vd" || op === "expand_vd" || op === "init_vd") {
      html = `<label class="console-field">虚拟盘<select id="f-vd">${vdOptions()}</select></label>`;
      if (op === "expand_vd") html += `<div class="console-free-head">可扩容的新盘</div><div id="f-free-disks" class="console-free-list"></div>`;
      if (op === "init_vd") html += `<label class="console-check"><input id="f-full" type="checkbox"> 全盘初始化（更慢）</label>`;
    } else if (op === "rebuild") {
      html = `<label class="console-field">物理盘<select id="f-disk">${pdOptions(false)}</select></label>`;
    } else if (op === "import_foreign") {
      html = `<label class="console-field">控制器<select id="f-controller">${controllerOptions()}</select></label>`;
    }
    fields.innerHTML = html;
    ["#f-controller", "#f-vd", "#f-disk", "#f-level", "#f-full"].forEach((sel) => {
      const el = $(sel);
      if (el) el.addEventListener("change", () => { renderFreeDisks(); updatePhrase(); });
    });
    if (op === "expand_vd") renderFreeDisks();
    updatePhrase();
  }

  function renderFreeDisks() {
    const wrap = $("#f-free-disks");
    if (!wrap) return;
    const vdId = $("#f-vd")?.value || "";
    const ctrl = (state.inventory?.controllers || []).find((c) => (c.vds || []).some((v) => v.id === vdId));
    const vd = ctrl?.vds?.find((v) => v.id === vdId);
    if (!ctrl || !vd) { wrap.innerHTML = `<p class="console-empty-warn">未选择可扩容的虚拟盘。</p>`; return; }
    const unassigned = (ctrl.pds || []).filter((pd) => !pd.vd_id);
    if (!unassigned.length) {
      wrap.innerHTML = `<p class="console-empty-warn">未发现任何空闲磁盘。请先在控制器中插入或解绑一块 Unconfigured Good 磁盘，再执行扩容。</p>`;
      return;
    }
    const min = Number(vd.min_size_sectors || 0);
    const rows = unassigned.map((pd) => {
      const sizeOk = !min || Number(pd.size_sectors || 0) >= min;
      const eligible = pd.free && sizeOk;
      const reason = pd.free ? (sizeOk ? "" : "容量小于现有成员盘") : pd.free_reason;
      return `<label class="console-disk-choice ${eligible ? "" : "disabled"}">
        <input type="checkbox" name="expand-drive" value="${esc(pd.id)}" ${eligible ? "" : "disabled"}>
        <span><b>${esc(pd.id)}</b> · ${esc(pd.bay || "")}<small>${esc(pd.size || "")} · ${esc(pd.state || "")}${reason ? ` · ${esc(reason)}` : ""}</small></span>
      </label>`;
    }).join("");
    const hasEligible = unassigned.some((pd) => pd.free && (!min || Number(pd.size_sectors || 0) >= min));
    wrap.innerHTML = (hasEligible ? `<p class="console-free-hint">新盘容量必须不小于现有成员盘；勾选后执行迁移扩容。</p>` : `<p class="console-empty-warn">没有符合条件的新盘：需要同一控制器下、未加入阵列、状态为 Unconfigured Good，且容量不小于现有成员盘。</p>`) + rows;
    wrap.querySelectorAll('input[name="expand-drive"]').forEach((el) => el.addEventListener("change", updatePhrase));
  }

  function updatePhrase() {
    const op = state.op;
    let phrase = "";
    if (op === "create_vd") phrase = "CREATE VD";
    else if (op === "delete_vd") phrase = `DELETE ${$("#f-vd")?.value || ""}`;
    else if (op === "expand_vd") phrase = `EXPAND ${$("#f-vd")?.value || ""}`;
    else if (op === "init_vd") phrase = `INIT ${$("#f-vd")?.value || ""}`;
    else if (op === "rebuild") {
      const pd = (state.inventory?.controllers || []).flatMap((c) => c.pds || []).find((p) => p.id === $("#f-disk")?.value);
      phrase = pd ? `REBUILD ${pd.eid}:${pd.slot}` : "REBUILD";
    } else if (op === "import_foreign") phrase = `IMPORT ${$("#f-controller")?.value || ""}`;
    $("#op-phrase").textContent = phrase || "—";
  }

  async function execute() {
    const confirm = $("#op-confirm").value.trim();
    if (!confirm) { toast("请输入确认短语", true); return; }
    let payload = {};
    if (state.op === "create_vd") {
      payload = {
        controller: $("#f-controller").value,
        level: $("#f-level").value,
        drives: Array.from($("#f-drives").selectedOptions).map((o) => o.value),
        strip: $("#f-strip").value,
        name: $("#f-name").value.trim(),
      };
    } else if (state.op === "delete_vd" || state.op === "expand_vd" || state.op === "init_vd") {
      payload = { vd_id: $("#f-vd").value };
      if (state.op === "expand_vd") {
        payload.drives = Array.from(document.querySelectorAll('input[name="expand-drive"]:checked')).map((el) => el.value);
        if (!payload.drives.length) { toast("没有选择可扩容的新盘", true); return; }
      }
      if (state.op === "init_vd") payload.full = $("#f-full").checked;
    } else if (state.op === "rebuild") {
      payload = { disk_id: $("#f-disk").value };
    } else if (state.op === "import_foreign") {
      payload = { controller: $("#f-controller").value };
    }
    payload.confirm = confirm;
    const button = $("#op-submit");
    button.disabled = true;
    button.textContent = "执行中…";
    try {
      const data = await api("api/console/op", { method: "POST", body: JSON.stringify({ op: state.op, payload }) });
      const out = $("#op-output");
      out.hidden = false;
      out.textContent = `${data.command || state.op}\n\n${data.output || "OK"}`;
      $("#op-confirm").value = "";
      toast("操作已执行");
      await loadInventory();
    } catch (error) {
      const out = $("#op-output");
      out.hidden = false;
      out.textContent = `失败：${error.message}`;
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = "执行写操作";
    }
  }

  $("#setup-submit").addEventListener("click", setup);
  $("#login-submit").addEventListener("click", login);
  $("#op-kind").addEventListener("change", renderFields);
  $("#op-submit").addEventListener("click", execute);
  $("#console-logout").addEventListener("click", async () => {
    try { await api("api/console/logout", { method: "POST" }); } catch (e) {}
    state.token = "";
    sessionStorage.removeItem(TOKEN_KEY);
    location.reload();
  });
  bindRisk();
  loadState().catch((error) => {
    $("#console-setup").hidden = false;
    $("#setup-hint").textContent = `加载失败：${error.message}`;
  });
})();
