/* RAID 阵列健康监控页面逻辑 */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const HEALTH = { ok: "正常", warning: "警告", critical: "严重", unknown: "未知" };

  const state = {
    data: null,
    controller: "",
    bay: "",
    health: "",
    q: "",
    expanded: new Set(),
    fetching: false,
    lastFetch: 0,
    collecting: false,
    auto: localStorage.getItem("disk-health-auto") !== "0",
    timer: null,
    refreshing: false,
    otherInfoDismissed: false,
    user: null,
    app: null,
    ai: { status: null, report: null, busy: false, admin: null },
    adminDb: null,
    trend: { diskId: "", metric: "temperature_c", days: 30, data: null, geom: null, loading: false, view: "single" },
    compare: { days: 30, controller: "", data: null, loading: false },
    topo: { controller: "" },
  };

  /* ---------------- 基础工具 ---------------- */
  const esc = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function request(url, options = {}) {
    const res = await fetch(url, { cache: "no-store", ...options });
    if (res.status === 401) {
      toast("登录已过期，正在跳转登录…", true);
      setTimeout(() => { window.location.reload(); }, 600);
      throw new Error("unauthorized");
    }
    return res;
  }

  const num = (value) => (value === null || value === undefined || value === "" ? "—" : Number(value).toLocaleString("zh-CN"));

  function fmtTime(iso, withDate = true) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso);
    const opts = withDate
      ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }
      : { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
    return date.toLocaleString("zh-CN", opts).replace(/\//g, "-");
  }

  function powerOn(hours) {
    if (hours === null || hours === undefined) return "—";
    if (hours >= 8760) return `${(hours / 8760).toFixed(1)} 年`;
    return `${num(hours)} h`;
  }

  function tempClass(celsius, media, iface) {
    if (celsius === null || celsius === undefined) return "";
    const text = `${media || ""} ${iface || ""}`.toUpperCase();
    let warn = 50;
    let crit = 58;
    if (text.includes("NVME")) { warn = 70; crit = 80; }
    else if (text.includes("SSD")) { warn = 65; crit = 75; }
    if (celsius >= crit) return "crit";
    if (celsius >= warn) return "hot";
    return "";
  }

  function badge(health, extra = "") {
    const level = HEALTH[health] ? health : "unknown";
    return `<span class="badge ${level} ${extra}">${HEALTH[level]}</span>`;
  }

  function toast(message, isError = false) {
    const el = $("#toast");
    el.textContent = message;
    el.className = `toast show${isError ? " error" : ""}`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { el.className = "toast"; }, 3200);
  }

  function showProgress(on) {
    $("#progress").classList.toggle("on", !!on);
    $("#refresh").disabled = !!on;
  }

  /* ---------------- 数据加载 ---------------- */
  async function load(initial = false) {
    if (state.fetching) return;
    state.fetching = true;
    try {
      const res = await request("api/disks");
      if (res.status === 202) {
        state.collecting = true;
        return;
      }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      state.collecting = !!data.collecting;
      const changed = !state.data || data.generated_at !== state.data.generated_at || data.collect_error !== state.data.collect_error;
      state.data = data;
      if (changed) render();
      else updateMeta();
      $("#skeleton").classList.add("hidden");
    } catch (error) {
      if (initial) {
        $("#skeleton").classList.add("hidden");
        toast(`加载失败：${error.message}`, true);
      }
    } finally {
      state.fetching = false;
      state.lastFetch = Date.now();
    }
  }

  /* ---------------- 渲染 ---------------- */
  function render() {
    const data = state.data;
    if (!data) return;
    updateMeta();
    renderKpis(data);
    renderBanner(data);
    renderOtherInfo(data);
    renderControllers(data);
    renderFilters(data);
    renderTable();
    renderWarnings(data);
    const interval = data.refresh_interval || 120;
    $("#foot-interval").textContent = `自动采集 ${interval} 秒`;
  }

  function updateMeta() {
    const data = state.data;
    if (!data) return;
    $("#host").textContent = data.host || "-";
    $("#updated").textContent = fmtTime(data.generated_at, false);
    $("#updated").title = data.generated_at || "";
    if (data.collect_error) toast(data.collect_error, true);
  }

  function renderKpis(data) {
    const summary = data.summary || {};
    const set = (key, value) => {
      const el = document.querySelector(`[data-kpi="${key}"] .kpi-value`);
      if (el) el.textContent = value;
    };
    set("controllers", summary.controllers ?? "—");
    set("disks", summary.disks ?? "—");
    set("ok", summary.ok ?? 0);
    set("warning", summary.warning ?? 0);
    set("critical", summary.critical ?? 0);
    set("vds", `${summary.vds_optimal ?? 0}/${summary.vds ?? 0}`);

    const bar = $("#kpi-bar");
    const total = summary.disks || 0;
    const parts = [
      ["b-ok", summary.ok || 0],
      ["b-warn", summary.warning || 0],
      ["b-crit", summary.critical || 0],
      ["b-unknown", summary.unknown || 0],
    ];
    bar.innerHTML = total
      ? parts.filter(([, value]) => value > 0).map(([cls, value]) => `<span class="${cls}" style="width:${(value / total) * 100}%"></span>`).join("")
      : "";
  }

  function renderBanner(data) {
    const banner = $("#banner");
    const summary = data.summary || {};
    if (summary.critical > 0) {
      banner.hidden = false;
      banner.className = "banner";
      banner.innerHTML = `<b>${summary.critical}</b> 块硬盘处于严重状态，请尽快检查下方明细。`;
      banner.dataset.health = "critical";
      banner.style.cursor = "pointer";
    } else if (summary.warning > 0) {
      banner.hidden = false;
      banner.className = "banner warn";
      banner.innerHTML = `<b>${summary.warning}</b> 块硬盘有告警（温度或 SMART 指标），建议关注。`;
      banner.dataset.health = "warning";
      banner.style.cursor = "pointer";
    } else {
      banner.hidden = true;
      banner.dataset.health = "";
    }
  }

  function renderOtherInfo(data) {
    const section = $("#other-info");
    if (state.otherInfoDismissed) {
      section.hidden = true;
      return;
    }
    const disks = (data.disks || []).filter((disk) => (disk.other_errors || 0) > 0);
    if (!disks.length) {
      section.hidden = true;
      return;
    }
    const max = Math.max(...disks.map((disk) => disk.other_errors || 0));
    const media = disks.reduce((sum, disk) => sum + (disk.media_errors || 0), 0);
    $("#other-info-text").textContent = media
      ? `检测到 ${disks.length} 块盘存在非介质错误累计，最高 ${max}；同时介质错误合计 ${media}，请优先查看介质错误。`
      : `检测到 ${disks.length} 块盘存在非介质错误累计，最高 ${max}；介质错误合计 0。该计数不等于盘片故障，不计入健康告警。`;
    section.hidden = false;
  }

  function controllerKindClass(kind) {
    return kind === "raid" ? "" : kind === "hba" ? "ctrl-hba" : "ctrl-direct";
  }

  function renderControllers(data) {
    const wrap = $("#controllers");
    const list = data.controllers || [];
    wrap.innerHTML = list.map((ctrl) => {
      const active = state.controller === ctrl.id ? " active" : "";
      const vds = (ctrl.vds || []).map((vd) => `
        <div class="vd">
          <span class="lv">${esc(vd.level || "RAID")}</span>
          <span class="sz">${esc(vd.size || "")}</span>
          ${badge(vd.health || "unknown", "sm")}
          <span class="os">${esc(vd.os_device || "")}</span>
        </div>`).join("");
      return `
        <article class="ctrl ${controllerKindClass(ctrl.kind)}${active}" data-controller="${esc(ctrl.id)}" tabindex="0">
          <div class="ctrl-head">
            <div class="ctrl-title">
              <h3 title="${esc(ctrl.model)}">${esc(ctrl.model || ctrl.id)}</h3>
              <span class="ctrl-kind">${esc(ctrl.kind_label || ctrl.kind)}</span>
            </div>
            ${badge(ctrl.health || "unknown")}
          </div>
          <div class="ctrl-meta">
            ${ctrl.firmware ? `<span>FW <b>${esc(ctrl.firmware)}</b></span>` : ""}
            ${ctrl.serial ? `<span>SN <b>${esc(ctrl.serial)}</b></span>` : ""}
            ${ctrl.pci ? `<span>PCI <b>${esc(ctrl.pci)}</b></span>` : ""}
            ${ctrl.driver ? `<span>驱动 <b>${esc(ctrl.driver)}</b></span>` : ""}
            ${ctrl.temperature_c !== null && ctrl.temperature_c !== undefined ? `<span>温度 <b>${esc(ctrl.temperature_c)}°C</b></span>` : ""}
            ${ctrl.bbu?.state ? `<span>BBU <b>${esc(ctrl.bbu.state)}</b></span>` : ""}
            ${ctrl.enclosure_count ? `<span>背板 <b>${esc(ctrl.enclosure_count)}</b></span>` : ""}
            ${ctrl.expander_count ? `<span>扩展器 <b>${esc(ctrl.expander_count)}</b></span>` : ""}
          </div>
          ${vds ? `<div class="vds">${vds}</div>` : ""}
          <div class="ctrl-foot">
            <span>物理磁盘 <b>${ctrl.disk_count ?? 0}</b> 块</span>
            <span class="ctrl-foot-right">
              <button class="topo-link" type="button" data-controller-id="${esc(ctrl.id)}">查看拓扑</button>
              <span>${esc(ctrl.id)} · 只读</span>
            </span>
          </div>
        </article>`;
    }).join("");

    wrap.querySelectorAll(".ctrl").forEach((card) => {
      const activate = () => {
        const id = card.dataset.controller;
        state.controller = state.controller === id ? "" : id;
        state.bay = "";
        $("#f-controller").value = state.controller;
        rebuildBays();
        renderTable();
        renderControllers(state.data);
        card.blur();
      };
      card.addEventListener("click", activate);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
      });
      const topoLink = card.querySelector(".topo-link");
      if (topoLink) {
        topoLink.addEventListener("click", (event) => {
          event.stopPropagation();
          topologyOpen(card.dataset.controller);
        });
      }
    });
  }

  function renderFilters(data) {
    const controllerSelect = $("#f-controller");
    const current = state.controller;
    const options = [`<option value="">全部控制器</option>`].concat(
      (data.controllers || []).map((ctrl) => `<option value="${esc(ctrl.id)}">${esc(ctrl.model)}（${esc(ctrl.kind_label || ctrl.kind)}）</option>`)
    );
    controllerSelect.innerHTML = options.join("");
    controllerSelect.value = current;
    rebuildBays();
    $("#f-health").value = state.health;
    $("#f-q").value = state.q;
  }

  function rebuildBays() {
    const select = $("#f-bay");
    if (!state.data) { select.innerHTML = `<option value="">全部盘位</option>`; return; }
    const disks = state.data.disks || [];
    const groups = new Map();
    disks.forEach((disk) => {
      if (state.controller && disk.controller_id !== state.controller) return;
      if (!groups.has(disk.controller_id)) groups.set(disk.controller_id, []);
      groups.get(disk.controller_id).push(disk);
    });
    const parts = [`<option value="">全部盘位</option>`];
    groups.forEach((list, cid) => {
      const name = list[0]?.controller_name || cid;
      parts.push(`<optgroup label="${esc(name)}">`);
      list
        .slice()
        .sort((a, b) => String(a.bay).localeCompare(String(b.bay), "zh-CN", { numeric: true }))
        .forEach((disk) => {
          const value = `${disk.controller_id}::${disk.bay}`;
          const used = state.bay === value ? " selected" : "";
          parts.push(`<option value="${esc(value)}"${used}>${esc(disk.bay_label || disk.bay)}${disk.model ? ` · ${esc(disk.model)}` : ""}</option>`);
        });
      parts.push(`</optgroup>`);
    });
    select.innerHTML = parts.join("");
    if (![...select.options].some((opt) => opt.value === state.bay)) state.bay = "";
    select.value = state.bay;
  }

  function filteredDisks() {
    if (!state.data) return [];
    const query = state.q.trim().toLowerCase();
    return (state.data.disks || []).filter((disk) => {
      if (state.controller && disk.controller_id !== state.controller) return false;
      if (state.bay) {
        const [cid, bay] = state.bay.split("::");
        if (disk.controller_id !== cid || disk.bay !== bay) return false;
      }
      if (state.health && disk.health !== state.health) return false;
      if (query) {
        const haystack = [disk.model, disk.serial, disk.bay, disk.bay_label, disk.device, disk.controller_name, disk.role]
          .filter(Boolean).join(" ").toLowerCase();
        if (!haystack.includes(query)) return false;
      }
      return true;
    });
  }

  function renderTable() {
    const body = $("#disk-body");
    const disks = filteredDisks();
    const total = state.data?.disks?.length || 0;
    $("#table-count").textContent = `· ${disks.length} / ${total}`;
    $("#result-count").innerHTML = `显示 <b>${disks.length}</b> / ${total} 块`;

    if (!disks.length) {
      body.innerHTML = "";
      $("#empty").hidden = false;
      $("#empty-text").textContent = total ? "没有符合筛选条件的磁盘" : "尚未发现物理磁盘";
      return;
    }
    $("#empty").hidden = true;

    body.innerHTML = disks.map((disk) => {
      const open = state.expanded.has(disk.id);
      const temp = disk.temperature_c;
      const tempCell = temp === null || temp === undefined
        ? `<span class="dash">—</span>`
        : `<span class="temp ${tempClass(temp, disk.media, disk.interface)}">${temp}°C</span>`;
      const mediaErrors = disk.media_errors;
      const errCell = mediaErrors === null || mediaErrors === undefined
        ? `<span class="dash">—</span>`
        : mediaErrors > 0
          ? `<span class="err-bad">${num(mediaErrors)}</span>`
          : `<span class="mono">0</span>`;
      const otherErrors = disk.other_errors;
      const otherCell = otherErrors === null || otherErrors === undefined
        ? `<span class="dash">—</span>`
        : `<span class="other-count ${otherErrors > 0 ? "has-info" : ""}" title="非介质错误：常由 SMART 直通轮询 / SAS 链路累计，不是盘片介质故障，不计入健康告警。">${num(otherErrors)}</span>`;
      const raidText = disk.vd_id
        ? `<span class="chip">${esc(disk.vd_level || "RAID")}</span>${disk.dg !== null && disk.dg !== undefined ? `<small class="mono"> DG${esc(disk.dg)}</small>` : ""}`
        : `<span class="dash">—</span>`;
      const stateBad = !["Onln", "Online", "Optl", "JBOD", "UGood", "Good"].includes(disk.state || "");
      return `
        <tr class="main${open ? " open" : ""}" data-id="${esc(disk.id)}">
          <td>${badge(disk.health)}</td>
          <td class="col-ctrl"><div class="model" title="${esc(disk.controller_name)}">${esc(disk.controller_name)}
            <small>${esc(disk.controller_id)} · ${esc(disk.kind === "raid" ? "RAID" : "直连")}</small></div></td>
          <td><span class="bay">${esc(disk.bay_label || disk.bay)}</span></td>
          <td><div class="model">${esc(disk.model || "—")}<small>${esc(disk.role || "")}</small></div></td>
          <td class="col-serial"><span class="mono">${esc(disk.serial || "—")}</span></td>
          <td class="col-size"><span class="mono">${esc(disk.size || "—")}</span></td>
          <td class="col-if"><span class="chip if-${esc(String(disk.interface || "").toLowerCase())}">${esc(disk.interface || "—")}</span></td>
          <td class="col-state"><span class="${stateBad ? "err-warn" : "mono"}">${esc(disk.state_label || disk.state || "—")}</span></td>
          <td class="col-temp">${tempCell}</td>
          <td class="col-hours"><span class="mono" title="${disk.power_on_hours ?? ""} 小时">${powerOn(disk.power_on_hours)}</span></td>
          <td class="col-err">${errCell}</td>
          <td class="col-other-err">${otherCell}</td>
          <td class="col-raid">${raidText}</td>
          <td class="col-expand"><button class="expand-btn" type="button" aria-label="展开详情">
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M6 9l6 6 6-6"/></svg>
          </button></td>
        </tr>
        ${open ? `<tr class="detail"><td colspan="14">${detailHtml(disk)}</td></tr>` : ""}`;
    }).join("");

    body.querySelectorAll("tr.main").forEach((row) => {
      row.addEventListener("click", () => {
        const id = row.dataset.id;
        if (state.expanded.has(id)) state.expanded.delete(id);
        else state.expanded.add(id);
        renderTable();
      });
    });

    body.querySelectorAll(".raw-btn").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        loadRaw(button);
      });
    });

    body.querySelectorAll(".trend-btn").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        trendOpen(button.dataset.id);
      });
    });

    body.querySelectorAll(".health-trend").forEach(initInlineTrend);
  }

  function metric(label, value, level = "", hint = "") {
    const help = hint ? ` <span class="metric-help" title="${esc(hint)}">?</span>` : "";
    return `<div class="metric"><div class="m-label">${esc(label)}${help}</div><div class="m-value ${level}">${esc(value)}</div></div>`;
  }

  function detailHtml(disk) {
    const smart = disk.smart || {};
    const metrics = disk.smart_metrics || {};
    const nvme = smart.nvme || {};
    const scsi = smart.scsi || {};
    const sas = smart.sas || {};
    const reasons = (disk.reasons || []).map((reason) => {
      const cls = reason.includes("非介质错误")
        ? ""
        : /故障|严重|未通过|过高|丢失|离线|降级|介质错误|预测性/.test(reason)
          ? "bad"
          : /警告|偏高|错误|告警|低于阈值|重映射|待映射|增长缺陷/.test(reason)
            ? "warn"
            : "";
      return `<li class="${cls}">${esc(reason)}</li>`;
    }).join("");

    const trip = disk.temperature_trip_c ?? smart.temperature_trip_c;
    const protocol = disk.protocol_detail || smart.protocol_name || disk.interface || "—";
    const rpm = smart.rotation_rate ? `${num(smart.rotation_rate)} rpm` : "—";
    const info = [
      ["控制器", `${disk.controller_name || "—"}（${disk.controller_id || "—"}）`],
      ["盘位", `${disk.bay_label || disk.bay || "—"}`],
      ["厂商", disk.vendor || smart.vendor || "—"],
      ["型号", disk.model || smart.product || "—"],
      ["协议", `${protocol}${disk.scsi_version ? ` · ${disk.scsi_version}` : ""}`],
      ["设备", disk.device || "—"],
      ["序列号", disk.serial || "—"],
      ["固件", disk.firmware || smart.revision || "—"],
      ["WWN", disk.wwn || "—"],
      ["容量", disk.size || "—"],
      ["接口 / 介质", `${disk.interface || "—"} / ${disk.media || "—"}`],
      ["转速", rpm],
      ["温度", disk.temperature_c === null || disk.temperature_c === undefined ? "—" : `${disk.temperature_c}°C${trip ? ` / 阈值 ${trip}°C` : ""}`],
      ["RAID 组", disk.vd_id ? `${disk.vd_level || "RAID"} · DG${disk.dg ?? "-"}` : "—"],
      ["链路速率", disk.link_speed || disk.link_rate || "—"],
      ["背板 / 扩展器", disk.enclosure_product || disk.expander_product || disk.enclosure_id || disk.expander_id || "—"],
      ["SES 槽位", disk.ses_bay === null || disk.ses_bay === undefined ? "—" : String(disk.ses_bay)],
      ["SAS 地址", disk.sas_address || "—"],
      ["PHY / 端口", `${disk.phy_id ?? "—"} / ${disk.connected_port || "—"}`],
      ["物理位置", disk.position || "—"],
    ].map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value)}</dd>`).join("");

    const metricBits = [
      metric("温度", disk.temperature_c === null || disk.temperature_c === undefined ? "—" : `${disk.temperature_c}°C`, tempClass(disk.temperature_c, disk.media, disk.interface) === "crit" ? "bad" : tempClass(disk.temperature_c, disk.media, disk.interface)),
      metric("通电时间", powerOn(disk.power_on_hours)),
      metric("介质错误", num(disk.media_errors), disk.media_errors > 0 ? "bad" : ""),
      metric(
        "其他错误（非介质）",
        num(disk.other_errors),
        (disk.other_errors || 0) >= 20 ? "info" : "",
        "非介质错误：常由 SMART 直通轮询 / SAS 链路累计，不是盘片介质故障，不计入健康告警。",
      ),
      metric("预测故障", num(disk.predictive_failures), disk.predictive_failures > 0 ? "bad" : ""),
    ];
    if (metrics.reallocated !== null && metrics.reallocated !== undefined) metricBits.push(metric("重映射扇区", num(metrics.reallocated), metrics.reallocated > 0 ? "warn" : ""));
    if (metrics.pending !== null && metrics.pending !== undefined) metricBits.push(metric("待映射扇区", num(metrics.pending), metrics.pending > 0 ? "warn" : ""));
    if (metrics.offline_uncorrectable !== null && metrics.offline_uncorrectable !== undefined) metricBits.push(metric("不可修正扇区", num(metrics.offline_uncorrectable), metrics.offline_uncorrectable > 0 ? "warn" : ""));
    if (metrics.grown_defects !== null && metrics.grown_defects !== undefined) metricBits.push(metric("增长缺陷", num(metrics.grown_defects), metrics.grown_defects > 0 ? "warn" : ""));
    if (metrics.read_uncorrected) metricBits.push(metric("读不可修正", num(metrics.read_uncorrected), "warn"));
    if (metrics.write_uncorrected) metricBits.push(metric("写不可修正", num(metrics.write_uncorrected), "warn"));
    if (nvme.percentage_used !== null && nvme.percentage_used !== undefined) metricBits.push(metric("NVMe 寿命已用", `${nvme.percentage_used}%`, nvme.percentage_used >= 90 ? "warn" : ""));
    if (nvme.media_errors) metricBits.push(metric("NVMe 介质错误", num(nvme.media_errors), "bad"));

    const attributes = (smart.attributes || []).map((attr) => `
      <tr>
        <td class="num">${esc(attr.id)}</td>
        <td class="name">${esc(attr.name)}</td>
        <td class="num">${esc(attr.value)}</td>
        <td class="num">${esc(attr.worst)}</td>
        <td class="num">${esc(attr.thresh)}</td>
        <td class="num" title="${esc(attr.raw_string || "")}">${esc(attr.raw_string || attr.raw)}</td>
      </tr>`).join("");

    const scsiMetrics = [];
    if (scsi.grown_defects !== undefined) scsiMetrics.push(metric("增长缺陷", num(scsi.grown_defects), scsi.grown_defects > 0 ? "warn" : ""));
    if ((scsi.read || {}).uncorrected !== undefined) scsiMetrics.push(metric("读不可修正", num(scsi.read.uncorrected), scsi.read.uncorrected > 0 ? "warn" : ""));
    if ((scsi.write || {}).uncorrected !== undefined) scsiMetrics.push(metric("写不可修正", num(scsi.write.uncorrected), scsi.write.uncorrected > 0 ? "warn" : ""));
    if (scsi.non_medium_errors !== undefined) scsiMetrics.push(metric("非介质错误", num(scsi.non_medium_errors), scsi.non_medium_errors > 0 ? "warn" : ""));
    if (scsi.start_stop_cycles !== undefined) scsiMetrics.push(metric("启停次数", num(scsi.start_stop_cycles)));

    const sasRows = [["read", "读取"], ["write", "写入"], ["verify", "校验"]]
      .filter(([key]) => sas[key])
      .map(([key, label]) => {
        const row = sas[key] || {};
        return `<tr><td class="name">${label}</td><td class="num">${num(row.corrected)}</td><td class="num ${row.uncorrected > 0 ? "err-bad" : ""}">${num(row.uncorrected)}</td><td class="num">${esc(row.gigabytes || "—")}</td></tr>`;
      }).join("");
    const sasCounters = [];
    if (sas.grown_defects !== undefined) sasCounters.push(metric("增长缺陷", num(sas.grown_defects), sas.grown_defects > 0 ? "warn" : ""));
    if (sas.non_medium_errors !== undefined) sasCounters.push(metric("非介质错误", num(sas.non_medium_errors), sas.non_medium_errors > 0 ? "warn" : ""));
    if (sas.start_stop_cycles !== undefined) sasCounters.push(metric("启停次数", num(sas.start_stop_cycles)));
    if (sas.load_unload_cycles !== undefined) sasCounters.push(metric("装载/卸载", num(sas.load_unload_cycles)));
    if (sas.manufactured) sasCounters.push(metric("制造周", sas.manufactured));
    if (sas.drive_trip_c !== undefined && sas.drive_trip_c !== null) sasCounters.push(metric("温度阈值", `${sas.drive_trip_c}°C`));
    const sasPanel = sasRows
      ? `<div class="attr-wrap"><table class="attr-table sas-table">
           <colgroup><col class="col-op"><col class="col-num"><col class="col-num"><col class="col-gb"></colgroup>
           <thead><tr><th>操作</th><th>已纠正</th><th>不可修正</th><th>处理量 (GB)</th></tr></thead>
           <tbody>${sasRows}</tbody>
         </table></div>
         ${sasCounters.length ? `<div class="metrics" style="margin-top:10px">${sasCounters.join("")}</div>` : ""}
         <p class="mono" style="margin:9px 0 0;color:var(--muted-2)">SAS 接口没有 ATA 属性表，以上为 SCSI 错误计数器 / 缺陷与运行计数。</p>`
      : "";

    const attrPanel = attributes
      ? `<div class="attr-wrap"><table class="attr-table">
           <colgroup><col class="col-id"><col class="col-name"><col class="col-num"><col class="col-num"><col class="col-num"><col class="col-raw"></colgroup>
           <thead><tr><th>ID</th><th>属性</th><th>当前</th><th>最差</th><th>阈值</th><th>原始值</th></tr></thead>
           <tbody>${attributes}</tbody>
         </table></div>`
      : sasPanel
        ? sasPanel
        : scsiMetrics.length
          ? `<div class="metrics">${scsiMetrics.join("")}</div><p class="mono" style="margin:9px 0 0;color:var(--muted-2)">未提供 ATA SMART 属性表，以上为 SCSI 错误计数器。</p>`
          : `<div class="empty" style="padding:18px"><p>该设备未提供可用的 SMART 属性。</p></div>`;

    const messages = (smart.messages || []).map((msg) => `<li>${esc(msg.text)}</li>`).join("");
    const smartError = smart.error ? `<li style="color:#ffe6a6">${esc(smart.error)}</li>` : "";
    let selfTestText = "";
    if (smart.self_test) {
      const raw = String(smart.self_test).toLowerCase();
      selfTestText = raw.includes("without error")
        ? "已完成，无错误"
        : raw.includes("failed")
          ? "自检失败"
          : raw.includes("completed")
            ? "已完成"
            : smart.self_test;
    }
    const selfTest = selfTestText ? `<span class="mono">自检：${esc(selfTestText)}</span>` : "";

    return `
      <div class="detail-inner">
        <div class="detail-grid">
          <div class="panel">
            <h4>设备信息</h4>
            <dl class="kv">${info}</dl>
          </div>
          <div class="panel health-panel">
            <h4>健康结论 · ${esc(HEALTH[disk.health] || "未知")}</h4>
            <ul class="reasons">${reasons || "<li>暂无</li>"}</ul>
            <div class="metrics" style="margin-top:10px">${metricBits.join("")}</div>
            ${(disk.other_errors || 0) > 0 ? `<p class="metric-explain">其他错误为非介质错误，常由 SMART 直通轮询 / SAS 链路累计，不代表盘片故障，也不计入健康告警。</p>` : ""}
            <div class="health-trend" data-disk-id="${esc(disk.id)}" data-metric="temperature_c" data-days="30">
              <div class="health-trend-head">
                <div><b class="health-trend-title">温度趋势</b><span class="health-trend-range">最近 30 天</span></div>
                <div class="health-trend-controls">
                  <select class="inline-trend-metric" aria-label="趋势指标">
                    <option value="temperature_c">温度</option>
                    <option value="media_errors">介质错误</option>
                    <option value="other_errors">其他错误</option>
                    <option value="predictive_failures">预测故障</option>
                    <option value="reallocated">重映射扇区</option>
                    <option value="pending">待映射扇区</option>
                    <option value="offline_uncorrectable">不可修正扇区</option>
                    <option value="grown_defects">SAS 增长缺陷</option>
                    <option value="read_uncorrected">读不可修正</option>
                    <option value="write_uncorrected">写不可修正</option>
                    <option value="power_on_hours">通电时间</option>
                  </select>
                  <select class="inline-trend-days" aria-label="趋势范围">
                    <option value="7">7 天</option>
                    <option value="30" selected>30 天</option>
                    <option value="90">90 天</option>
                  </select>
                  <button class="inline-trend-full" type="button">全屏</button>
                </div>
              </div>
              <div class="inline-trend-wrap">
                <canvas class="inline-trend-canvas"></canvas>
                <div class="inline-trend-empty" hidden>暂无历史采样</div>
              </div>
              <div class="inline-trend-foot"></div>
            </div>
          </div>
        </div>
        <div class="smart-grid">
          <div class="panel smart-card smart-attrs-card">
            <div class="smart-card-head">
              <h4>SMART 属性 ${selfTest}</h4>
            </div>
            <div class="smart-attrs">
              ${attrPanel}
              ${messages || smartError ? `<ul class="msgs" style="margin-top:10px">${smartError}${messages}</ul>` : ""}
            </div>
          </div>
          <div class="panel smart-card smart-raw-card">
            <div class="smart-card-head">
              <h4>原始 SMART JSON</h4>
              <div class="smart-raw-tools">
                <span class="raw-command mono">smartctl -a -j</span>
                <button class="btn btn-ghost btn-sm raw-btn" type="button" data-id="${esc(disk.id)}">查看原始 SMART JSON</button>
              </div>
            </div>
            <div class="smart-raw">
              <div class="smart-raw-empty">点击「查看原始 SMART JSON」<br>在此处加载原始数据</div>
              <pre class="raw-pre" hidden></pre>
            </div>
          </div>
        </div>
      </div>`;
  }

  async function loadRaw(button) {
    const row = button.closest(".detail-inner");
    const pre = row.querySelector(".raw-pre");
    const placeholder = row.querySelector(".smart-raw-empty");
    if (!pre.hidden) {
      pre.hidden = true;
      if (placeholder) placeholder.hidden = false;
      button.textContent = "查看原始 SMART JSON";
      return;
    }
    button.disabled = true;
    button.textContent = "读取中…";
    try {
      const res = await request(`api/disk?id=${encodeURIComponent(button.dataset.id)}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      pre.textContent = data.raw?.json
        ? `${data.raw.command}\n退出码 ${data.raw.exit_code}\n\n${JSON.stringify(data.raw.json, null, 2)}`
        : `${data.raw?.command || ""}\n${data.raw?.stderr || "无输出"}`;
      pre.hidden = false;
      if (placeholder) placeholder.hidden = true;
      button.textContent = "收起原始数据";
    } catch (error) {
      toast(`原始数据读取失败：${error.message}`, true);
      button.textContent = "查看原始 SMART JSON";
    } finally {
      button.disabled = false;
    }
  }

  function renderWarnings(data) {
    const warnings = data.warnings || [];
    const section = $("#warnings");
    if (!warnings.length) { section.hidden = true; return; }
    section.hidden = false;
    $("#warn-count").textContent = `(${warnings.length})`;
    $("#warn-list").innerHTML = warnings.map((item) => `<li>${esc(item)}</li>`).join("");
  }

  /* ---------------- 交互 ---------------- */
  async function loadSession() {
    try {
      const res = await request("api/session");
      if (!res.ok) return;
      const data = await res.json();
      const user = data.user || {};
      const name = user.username || (user.uid !== null && user.uid !== undefined ? `用户 ${user.uid}` : "fnOS");
      const role = user.admin ? "管理员" : "成员";
      $("#user-name").textContent = name;
      $("#user-role").textContent = `fnOS · ${role}`;
      $("#user-avatar").textContent = (name || "?").trim().slice(0, 1).toUpperCase();
      $("#user-pill").title = `fnOS 登录：${name}（${role}）`;
      $("#user-pill").hidden = false;
      $("#logout").hidden = data.can_logout === false;
      state.user = user;
      state.app = data.app || null;
      if (state.app?.version) {
        $("#app-version").textContent = state.app.version;
        const brandVersion = $("#brand-version");
        if (brandVersion) brandVersion.textContent = state.app.version;
      }
      $("#admin-open").hidden = !user.admin;
    } catch (error) {
      /* 401 已由 request() 处理 */
    }
  }

  function bind() {
    $("#auto").checked = state.auto;
    $("#auto").addEventListener("change", (event) => {
      state.auto = event.target.checked;
      localStorage.setItem("disk-health-auto", state.auto ? "1" : "0");
      toast(state.auto ? "已开启自动刷新（30 秒）" : "已暂停自动刷新");
    });

    $("#refresh").addEventListener("click", () => manualRefresh(true));
    $("#other-info-close").addEventListener("click", () => {
      state.otherInfoDismissed = true;
      $("#other-info").hidden = true;
    });
    $("#donate-open").addEventListener("click", () => { window.location.href = "donate"; });

    $("#logout").addEventListener("click", async (event) => {
      event.stopPropagation();
      try {
        await fetch("api/logout", { method: "POST", cache: "no-store" });
      } catch (error) {
        /* 忽略网络错误，仍然跳转 */
      }
      window.location.reload();
    });

    $("#f-controller").addEventListener("change", (event) => {
      state.controller = event.target.value;
      state.bay = "";
      rebuildBays();
      renderTable();
      renderControllers(state.data);
    });

    $("#f-bay").addEventListener("change", (event) => {
      state.bay = event.target.value;
      renderTable();
    });

    $("#f-health").addEventListener("change", (event) => {
      state.health = event.target.value;
      renderTable();
    });

    let searchTimer = null;
    $("#f-q").addEventListener("input", (event) => {
      clearTimeout(searchTimer);
      const value = event.target.value;
      searchTimer = setTimeout(() => { state.q = value; renderTable(); }, 180);
    });

    $("#clear").addEventListener("click", () => {
      state.controller = "";
      state.bay = "";
      state.health = "";
      state.q = "";
      $("#f-controller").value = "";
      $("#f-health").value = "";
      $("#f-q").value = "";
      rebuildBays();
      renderTable();
      renderControllers(state.data);
    });

    $("#banner").addEventListener("click", (event) => {
      const health = event.currentTarget.dataset.health;
      if (!health) return;
      state.health = health;
      $("#f-health").value = health;
      renderTable();
    });

    const menu = $("#export-menu");
    $("#export-btn").addEventListener("click", (event) => {
      event.stopPropagation();
      const open = menu.classList.toggle("open");
      $("#export-btn").setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("click", () => {
      menu.classList.remove("open");
      $("#export-btn").setAttribute("aria-expanded", "false");
    });
    menu.querySelectorAll("[data-format]").forEach((button) => {
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        menu.classList.remove("open");
        downloadExport(button.dataset.format);
      });
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        menu.classList.remove("open");
        if (!$("#ai-modal").hidden) aiClose();
        if (!$("#admin-modal").hidden) adminClose();
        if (!$("#trend-modal").hidden) trendClose();
        if (!$("#topology-modal").hidden) topologyClose();
        if (!$("#about-modal").hidden) aboutClose();
      }
    });
  }

  function downloadExport(format) {
    const params = new URLSearchParams({ format });
    if (state.controller) params.set("controller", state.controller);
    if (state.bay) {
      const [cid, bay] = state.bay.split("::");
      params.set("controller", cid);
      params.set("bay", bay);
    }
    if (state.health) params.set("health", state.health);
    if (state.q.trim()) params.set("q", state.q.trim());
    toast(`正在生成 ${format.toUpperCase()} 日志…`);
    window.location.href = `api/export?${params.toString()}`;
  }

  async function manualRefresh(force = false) {
    if (state.refreshing) return;
    state.refreshing = true;
    showProgress(true);
    try {
      await request("api/refresh", { method: "POST" });
      const before = state.data?.generated_ts;
      const started = Date.now();
      while (Date.now() - started < 100000) {
        await sleep(1800);
        const res = await request("api/disks");
        if (res.status === 202) continue;
        if (!res.ok) continue;
        const data = await res.json();
        if (data.generated_ts !== before) {
          state.data = data;
          state.collecting = false;
          render();
          $("#skeleton").classList.add("hidden");
          toast("采集完成，已更新");
          return;
        }
      }
      if (force) toast("采集耗时较长，请稍后刷新查看", true);
    } catch (error) {
      toast(`刷新失败：${error.message}`, true);
    } finally {
      showProgress(false);
      state.refreshing = false;
    }
  }

  function tick() {
    if (document.hidden) return;
    if (state.collecting || state.refreshing) {
      load();
      return;
    }
    if (state.auto && Date.now() - state.lastFetch > 30000) load();
  }

  /* ---------------- AI 故障分析 ---------------- */
  async function aiLoadStatus() {
    const res = await request("api/ai/status");
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.ai.status = data;
    aiUpdateUi();
    return data;
  }

  function aiUpdateUi() {
    const status = state.ai.status;
    const configured = !!status?.configured;
    const report = state.ai.report;
    $("#ai-run").disabled = !configured || state.ai.busy;
    $("#ai-empty").hidden = !!report;
    $("#ai-report").hidden = !report;
    if (!report) {
      if (!configured) {
        $("#ai-empty").textContent = state.user?.admin
          ? "AI 接口尚未配置。点击右上角齿轮进入「后台设置」，配置供应商与 API Key。"
          : "AI 接口尚未配置，请联系管理员在后台设置中启用。";
      } else {
        $("#ai-empty").textContent = "选择分析范围并点击「开始分析」。";
      }
    }
    $("#ai-status").className = "ai-status";
    if (configured) {
      $("#ai-status").textContent = `${status.provider_label || status.provider} · ${status.model}`;
    } else {
      $("#ai-status").textContent = "AI 未配置";
    }
  }

  async function aiOpen() {
    $("#ai-modal").hidden = false;
    $("#admin-modal").hidden = true;
    document.body.style.overflow = "hidden";
    if (!state.ai.status) {
      try { await aiLoadStatus(); } catch (error) { toast(`读取 AI 状态失败：${error.message}`, true); }
    }
    aiUpdateUi();
  }

  function aiClose() {
    $("#ai-modal").hidden = true;
    document.body.style.overflow = "";
  }

  function aiRenderMarkdown(markdown) {
    const lines = esc(markdown || "").split("\n");
    const out = [];
    let list = null;
    let code = false;
    const inline = (text) => text
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    const closeList = () => {
      if (list) { out.push(`</${list}>`); list = null; }
    };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (line.trim().startsWith("```")) {
        closeList();
        if (!code) { out.push("<pre><code>"); code = true; }
        else { out.push("</code></pre>"); code = false; }
        continue;
      }
      if (code) { out.push(line + "\n"); continue; }
      if (!line.trim()) { closeList(); continue; }
      let match;
      if ((match = line.match(/^###\s+(.*)$/))) { closeList(); out.push(`<h4>${inline(match[1])}</h4>`); }
      else if ((match = line.match(/^##\s+(.*)$/))) { closeList(); out.push(`<h3>${inline(match[1])}</h3>`); }
      else if ((match = line.match(/^#\s+(.*)$/))) { closeList(); out.push(`<h2>${inline(match[1])}</h2>`); }
      else if ((match = line.match(/^\s*[-*]\s+(.*)$/))) {
        if (list !== "ul") { closeList(); list = "ul"; out.push("<ul>"); }
        out.push(`<li>${inline(match[1])}</li>`);
      } else if ((match = line.match(/^\s*\d+[.)]\s+(.*)$/))) {
        if (list !== "ol") { closeList(); list = "ol"; out.push("<ol>"); }
        out.push(`<li>${inline(match[1])}</li>`);
      } else {
        closeList();
        out.push(`<p>${inline(line)}</p>`);
      }
    }
    closeList();
    if (code) out.push("</code></pre>");
    return out.join("");
  }

  async function aiAnalyze() {
    if (state.ai.busy) return;
    const status = state.ai.status;
    if (!status?.configured) {
      toast("AI 尚未配置，请联系管理员", true);
      if (state.user?.admin) adminOpen();
      return;
    }
    if ($("#ai-scope").value === "issues" && !(state.data?.summary?.warning || state.data?.summary?.critical || state.data?.summary?.unknown)) {
      toast("当前没有异常磁盘，可选择「全部磁盘」做整体体检");
      return;
    }
    const payload = { scope: $("#ai-scope").value };
    if (payload.scope === "filtered") {
      const ids = filteredDisks().map((disk) => disk.id);
      if (!ids.length) {
        toast("当前筛选没有磁盘", true);
        return;
      }
      payload.scope = "ids";
      payload.ids = ids;
    }
    state.ai.busy = true;
    $("#ai-run").disabled = true;
    $("#ai-status").className = "ai-status busy";
    $("#ai-status").textContent = `正在请求 ${status.provider_label || status.provider} 分析…`;
    try {
      const res = await request("api/ai/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      state.ai.report = data.report;
      $("#ai-empty").hidden = true;
      $("#ai-report").hidden = false;
      $("#ai-report").innerHTML = aiRenderMarkdown(data.report.content);
      $("#ai-meta").textContent = `${data.report.provider_label} / ${data.report.model} · ${data.report.scope_label} · ${data.report.disk_count} 块 · ${data.report.created_at}`;
      document.querySelectorAll("[data-ai-export]").forEach((button) => { button.disabled = false; });
      $("#ai-status").className = "ai-status";
      $("#ai-status").textContent = "分析完成";
      toast("AI 分析完成，可一键导出报告");
    } catch (error) {
      $("#ai-status").className = "ai-status error";
      $("#ai-status").textContent = `分析失败：${error.message}`;
      toast(`AI 分析失败：${error.message}`, true);
    } finally {
      state.ai.busy = false;
      $("#ai-run").disabled = !state.ai.status?.configured;
    }
  }

  function aiExport(format) {
    if (!state.ai.report) return;
    window.location.href = `api/ai/export?id=${encodeURIComponent(state.ai.report.id)}&format=${encodeURIComponent(format)}`;
  }

  /* ---------------- 后台设置（仅管理员） ---------------- */
  async function adminLoad() {
    const res = await request("api/admin/ai");
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.ai.admin = data;
    const select = $("#admin-provider");
    select.innerHTML = (data.providers || []).map((item) => `<option value="${esc(item.id)}">${esc(item.label)}</option>`).join("");
    select.value = data.provider;
    $("#admin-base").value = data.base_url || "";
    $("#admin-model").value = data.model || "";
    $("#admin-key").value = "";
    $("#admin-key").type = "password";
    $("#admin-key-toggle").textContent = "显示";
    $("#admin-key").placeholder = data.has_key ? "已保存，留空保持不变" : "sk-...";
    $("#admin-key-state").textContent = data.has_key ? "已保存" : "未配置";
    $("#admin-key-state").className = `key-state ${data.has_key ? "saved" : "empty"}`;
    $("#admin-temp").value = data.temperature ?? 0.2;
    $("#admin-maxtokens").value = data.max_tokens ?? 1800;
    $("#admin-redact").checked = data.redact_serials !== false;
    $("#admin-clear-key").checked = false;
    $("#admin-hint").textContent = data.has_key ? "已保存 API Key" : "尚未配置 API Key";
    $("#admin-status").textContent = "";
    return data;
  }

  function adminApplyPreset() {
    const preset = (state.ai.admin?.providers || []).find((item) => item.id === $("#admin-provider").value);
    if (!preset) return;
    if (preset.base_url) $("#admin-base").value = preset.base_url;
    if (preset.model) $("#admin-model").value = preset.model;
  }

  async function adminSave(silent = false) {
    const payload = {
      provider: $("#admin-provider").value,
      base_url: $("#admin-base").value.trim(),
      model: $("#admin-model").value.trim(),
      api_key: $("#admin-key").value.trim(),
      redact_serials: $("#admin-redact").checked,
      clear_key: $("#admin-clear-key").checked,
      temperature: Number($("#admin-temp").value || 0.2),
      max_tokens: Number($("#admin-maxtokens").value || 1800),
    };
    const res = await request("api/admin/ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    state.ai.admin = data;
    $("#admin-key").value = "";
    $("#admin-key").type = "password";
    $("#admin-key-toggle").textContent = "显示";
    $("#admin-clear-key").checked = false;
    $("#admin-key").placeholder = data.has_key ? "已保存，留空保持不变" : "sk-...";
    $("#admin-key-state").textContent = data.has_key ? "已保存" : "未配置";
    $("#admin-key-state").className = `key-state ${data.has_key ? "saved" : "empty"}`;
    $("#admin-hint").textContent = data.has_key ? "已保存 API Key" : "尚未配置 API Key";
    await aiLoadStatus();
    if (!silent) toast("AI 接口设置已保存");
    return data;
  }

  async function adminTest() {
    if (state.ai.busy) return;
    try {
      state.ai.busy = true;
      $("#admin-test").disabled = true;
      $("#admin-status").className = "ai-hint";
      $("#admin-status").textContent = "正在测试连接…";
      await adminSave(true);
      const res = await request("api/admin/ai/test", { method: "POST" });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      $("#admin-status").textContent = "连接成功";
      toast(`AI 连接成功：${data.reply || "OK"}`);
    } catch (error) {
      $("#admin-status").textContent = `连接失败：${error.message}`;
      toast(`AI 连接失败：${error.message}`, true);
    } finally {
      state.ai.busy = false;
      $("#admin-test").disabled = false;
    }
  }

  function dbBytes(value) {
    const size = Number(value) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 * 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
    return `${(size / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

  function dbDate(ts) {
    if (!ts) return "—";
    const date = new Date(Number(ts) * 1000);
    if (Number.isNaN(date.getTime())) return "—";
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  function dbRender(data) {
    state.adminDb = data;
    $("#admin-db-dir").value = data.directory || "";
    $("#db-file-path").textContent = data.path || "—";
    const stateLabel = data.error ? "读取失败" : !data.exists ? "未创建" : "正常";
    const stateClass = data.error ? "error" : !data.exists ? "empty" : "ok";
    $("#db-state").textContent = stateLabel;
    $("#db-state").className = `db-state ${stateClass}`;
    $("#db-default-hint").textContent = `默认：${data.default_directory || "—"}`;
    $("#db-default-hint").title = "修改后切换到该目录下的 history.db；原数据库文件不会自动移动。";
    const range = data.oldest_ts && data.newest_ts ? `${dbDate(data.oldest_ts)} → ${dbDate(data.newest_ts)}` : "—";
    const wal = Number(data.wal_bytes) || 0;
    $("#db-stats").innerHTML = `
      <div class="db-stat">
        <span>数据量</span>
        <b>${Number(data.samples || 0).toLocaleString("zh-CN")} 条</b>
        <small>${Number(data.disks || 0)} 块盘 · ${Number(data.controllers || 0)} 个控制器</small>
      </div>
      <div class="db-stat">
        <span>数据库体积</span>
        <b>${dbBytes(data.total_bytes)}</b>
        <small>主库 ${dbBytes(data.db_bytes)}${wal ? ` · WAL ${dbBytes(wal)}` : ""}</small>
      </div>
      <div class="db-stat">
        <span>覆盖范围</span>
        <b class="db-range">${esc(range)}</b>
        <small>保留 ${Number(data.retention_days || 180)} 天</small>
      </div>
    `;
    $("#db-status").textContent = data.error ? `读取失败：${data.error}` : "";
  }

  async function dbLoad() {
    $("#db-status").textContent = "读取数据库信息…";
    try {
      const res = await request("api/admin/db");
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      dbRender(data);
      $("#db-status").textContent = "";
    } catch (error) {
      $("#db-status").textContent = `读取失败：${error.message}`;
      $("#db-state").textContent = "读取失败";
      $("#db-state").className = "db-state error";
    }
  }

  async function dbSave() {
    const directory = $("#admin-db-dir").value.trim();
    $("#db-status").textContent = "保存中…";
    try {
      const res = await request("api/admin/db", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ directory }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      dbRender(data);
      toast("历史数据库路径已保存");
    } catch (error) {
      $("#db-status").textContent = `保存失败：${error.message}`;
      toast(`数据库路径保存失败：${error.message}`, true);
    }
  }

  async function adminOpen() {
    if (!state.user?.admin) {
      toast("需要 fnOS 管理员权限", true);
      return;
    }
    $("#ai-modal").hidden = true;
    $("#admin-modal").hidden = false;
    document.body.style.overflow = "hidden";
    try { await adminLoad(); } catch (error) { toast(`读取后台设置失败：${error.message}`, true); }
    await dbLoad();
  }

  function adminClose() {
    $("#admin-modal").hidden = true;
    document.body.style.overflow = "";
  }

  function bindAi() {
    $("#ai-open").addEventListener("click", aiOpen);
    $("#ai-close").addEventListener("click", aiClose);
    $("#ai-modal").addEventListener("click", (event) => { if (event.target === $("#ai-modal")) aiClose(); });
    $("#ai-run").addEventListener("click", aiAnalyze);
    document.querySelectorAll("[data-ai-export]").forEach((button) => {
      button.addEventListener("click", () => aiExport(button.dataset.aiExport));
    });

    $("#admin-open").addEventListener("click", adminOpen);
    $("#admin-close").addEventListener("click", adminClose);
    $("#db-save").addEventListener("click", dbSave);
    $("#db-refresh").addEventListener("click", dbLoad);
    $("#admin-modal").addEventListener("click", (event) => { if (event.target === $("#admin-modal")) adminClose(); });
    $("#admin-provider").addEventListener("change", adminApplyPreset);
    $("#admin-save").addEventListener("click", async () => {
      try { await adminSave(); } catch (error) { toast(`保存失败：${error.message}`, true); }
    });
    $("#admin-test").addEventListener("click", adminTest);
    $("#admin-key-toggle").addEventListener("click", () => {
      const input = $("#admin-key");
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      $("#admin-key-toggle").textContent = show ? "隐藏" : "显示";
    });
  }

  /* ---------------- 历史趋势 ---------------- */
  const TREND_PAD = { left: 58, right: 18, top: 18, bottom: 34 };

  function trendFormat(value, metric) {
    if (value === null || value === undefined) return "—";
    if (metric === "temperature_c") return `${Number(value).toFixed(1)}`;
    if (metric === "power_on_hours") return `${Math.round(value).toLocaleString("zh-CN")}`;
    return `${Math.round(value).toLocaleString("zh-CN")}`;
  }

  function trendDate(ts, withTime = true) {
    const date = new Date(ts * 1000);
    const mm = String(date.getMonth() + 1).padStart(2, "0");
    const dd = String(date.getDate()).padStart(2, "0");
    if (!withTime) return `${mm}-${dd}`;
    const hh = String(date.getHours()).padStart(2, "0");
    const mi = String(date.getMinutes()).padStart(2, "0");
    return `${mm}-${dd} ${hh}:${mi}`;
  }

  async function trendLoadDisks(days, preferred) {
    const res = await request(`api/history/disks?days=${encodeURIComponent(days)}`);
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    const metricSelect = $("#trend-metric");
    if (!metricSelect.options.length) {
      metricSelect.innerHTML = (data.metrics || [])
        .map((m) => `<option value="${esc(m.key)}">${esc(m.label)}${m.unit ? `（${esc(m.unit)}）` : ""}</option>`)
        .join("");
      metricSelect.value = state.trend.metric;
    }
    const diskSelect = $("#trend-disk");
    const list = data.disks || [];
    const ids = list.map((d) => d.disk_id);
    const options = list.map((d) => `<option value="${esc(d.disk_id)}">${esc(d.bay || d.disk_id)} · ${esc(d.model || "")} · ${d.samples} 点</option>`);
    if (preferred && !ids.includes(preferred)) {
      options.unshift(`<option value="${esc(preferred)}">${esc(preferred)} · 暂无采样</option>`);
    }
    diskSelect.innerHTML = options.length ? options.join("") : `<option value="">暂无历史磁盘</option>`;
    const wanted = preferred || state.trend.diskId;
    if (wanted && [...diskSelect.options].some((option) => option.value === wanted)) diskSelect.value = wanted;
    state.trend.diskId = diskSelect.value || "";
    return options.length > 0;
  }

  function trendUpdateStats(data) {
    const metric = data.metric;
    const stats = data.stats || {};
    const unit = data.unit || "";
    const latestTime = stats.latest_ts ? trendDate(stats.latest_ts) : "—";
    const items = [
      ["最新", stats.latest === null || stats.latest === undefined ? "—" : `${trendFormat(stats.latest, metric)} ${unit}`, latestTime],
      ["最低", stats.min === null || stats.min === undefined ? "—" : `${trendFormat(stats.min, metric)} ${unit}`, `${stats.samples || 0} 个采样`],
      ["最高", stats.max === null || stats.max === undefined ? "—" : `${trendFormat(stats.max, metric)} ${unit}`, `范围 ${data.days} 天`],
      ["平均", stats.avg === null || stats.avg === undefined ? "—" : `${trendFormat(stats.avg, metric)} ${unit}`, "SQLite 历史"],
    ];
    $("#trend-stats").innerHTML = items
      .map(([label, value, sub]) => `<div class="trend-stat"><span>${esc(label)}</span><b>${esc(value)}</b><span>${esc(sub)}</span></div>`)
      .join("");
  }

  function chartPalette() {
    const style = getComputedStyle(document.documentElement);
    return {
      grid: style.getPropertyValue("--chart-grid").trim() || "rgba(120,160,220,.10)",
      text: style.getPropertyValue("--chart-text").trim() || "#5f6f88",
      line: style.getPropertyValue("--chart-line").trim() || "#38bdf8",
      point: style.getPropertyValue("--chart-point-bg").trim() || "#0b1220",
    };
  }

  function trendDraw() {
    const canvas = $("#trend-canvas");
    const wrap = canvas.parentElement;
    const points = state.trend.data?.points || [];
    const data = state.trend.data;
    if (!data) return;
    const width = Math.max(300, wrap.clientWidth - 20);
    const height = window.innerWidth <= 760 ? 270 : 320;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const palette = chartPalette();
    state.trend.geom = null;
    if (!points.length) return;

    const plotW = width - TREND_PAD.left - TREND_PAD.right;
    const plotH = height - TREND_PAD.top - TREND_PAD.bottom;
    const values = points.map((p) => Number(p.value));
    let minY = Math.min(...values);
    let maxY = Math.max(...values);
    if (minY === maxY) {
      const delta = Math.max(1, Math.abs(minY) * 0.05);
      minY -= delta;
      maxY += delta;
    } else {
      const pad = (maxY - minY) * 0.08;
      minY -= pad;
      maxY += pad;
    }
    let minX = points[0].ts;
    let maxX = points[points.length - 1].ts;
    if (maxX === minX) { minX -= 3600; maxX += 3600; }

    ctx.lineWidth = 1;
    ctx.font = '10px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 4; i += 1) {
      const y = TREND_PAD.top + (plotH * i) / 4;
      ctx.strokeStyle = palette.grid;
      ctx.beginPath(); ctx.moveTo(TREND_PAD.left, y); ctx.lineTo(width - TREND_PAD.right, y); ctx.stroke();
      const value = maxY - ((maxY - minY) * i) / 4;
      ctx.fillStyle = palette.text;
      ctx.textAlign = "right";
      ctx.fillText(trendFormat(value, data.metric), TREND_PAD.left - 8, y);
    }
    for (let i = 0; i <= 4; i += 1) {
      const x = TREND_PAD.left + (plotW * i) / 4;
      ctx.strokeStyle = palette.grid;
      ctx.beginPath(); ctx.moveTo(x, TREND_PAD.top); ctx.lineTo(x, height - TREND_PAD.bottom); ctx.stroke();
      const ts = minX + ((maxX - minX) * i) / 4;
      ctx.fillStyle = palette.text;
      ctx.textAlign = "center";
      ctx.fillText(trendDate(Math.round(ts), data.days <= 7), x, height - TREND_PAD.bottom + 16);
    }

    const px = (ts) => TREND_PAD.left + ((ts - minX) / (maxX - minX)) * plotW;
    const py = (value) => TREND_PAD.top + (1 - (value - minY) / (maxY - minY)) * plotH;
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = px(point.ts), y = py(Number(point.value));
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = palette.line;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.stroke();

    const gradient = ctx.createLinearGradient(0, TREND_PAD.top, 0, height - TREND_PAD.bottom);
    gradient.addColorStop(0, "rgba(56,189,248,.24)");
    gradient.addColorStop(1, "rgba(56,189,248,.01)");
    ctx.lineTo(px(points[points.length - 1].ts), height - TREND_PAD.bottom);
    ctx.lineTo(px(points[0].ts), height - TREND_PAD.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();

    if (points.length <= 150) {
      points.forEach((point) => {
        const x = px(point.ts), y = py(Number(point.value));
        ctx.beginPath(); ctx.arc(x, y, 2.2, 0, Math.PI * 2);
        ctx.fillStyle = palette.point; ctx.fill();
        ctx.strokeStyle = palette.line; ctx.lineWidth = 1.4; ctx.stroke();
      });
    }

    ctx.fillStyle = palette.text;
    ctx.textAlign = "left";
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, monospace";
    ctx.fillText(`${data.label} (${data.unit})`, TREND_PAD.left, 8);

    state.trend.geom = {
      points: points.map((point) => ({ x: px(point.ts), y: py(Number(point.value)), ts: point.ts, value: Number(point.value) })),
      width, height, unit: data.unit, metric: data.metric,
    };
  }

  function trendHideTooltip() {
    const tip = $("#trend-tooltip");
    if (tip) tip.hidden = true;
  }

  function trendMove(event) {
    const geom = state.trend.geom;
    if (!geom) return;
    const canvas = $("#trend-canvas");
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    let best = null;
    geom.points.forEach((point) => {
      if (!best || Math.abs(point.x - x) < Math.abs(best.x - x)) best = point;
    });
    if (!best || Math.abs(best.x - x) > 26) { trendHideTooltip(); return; }
    const tip = $("#trend-tooltip");
    const wrap = canvas.parentElement;
    tip.innerHTML = `<b>${esc(trendFormat(best.value, geom.metric))} ${esc(geom.unit)}</b><br>${esc(trendDate(best.ts))}`;
    tip.hidden = false;
    const left = Math.min(Math.max(8, best.x + 14), wrap.clientWidth - 130);
    tip.style.left = `${left}px`;
    tip.style.top = `${Math.max(8, best.y - 16)}px`;
  }

  async function trendLoadSeries() {
    const diskId = state.trend.diskId;
    const days = state.trend.days;
    const metric = state.trend.metric;
    state.trend.loading = true;
    $("#trend-status").textContent = "加载中…";
    if (!diskId) {
      state.trend.data = null;
      $("#trend-stats").innerHTML = "";
      $("#trend-empty").hidden = false;
      $("#trend-empty").textContent = "暂无历史采样。应用运行后会每 120 秒记录一次，稍后再来查看。";
      $("#trend-foot").textContent = "历史数据保存在应用数据目录的 history.db（SQLite）。";
      trendDraw();
      state.trend.loading = false;
      return;
    }
    try {
      const res = await request(`api/history/series?disk_id=${encodeURIComponent(diskId)}&metric=${encodeURIComponent(metric)}&days=${encodeURIComponent(days)}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      state.trend.data = data;
      trendUpdateStats(data);
      $("#trend-status").textContent = `${data.disk.model || diskId} · ${data.disk.bay || ""} · ${data.points.length} 个点`;
      $("#trend-foot").textContent = `最近 ${data.days} 天 · ${data.stats.samples} 个采样 · 每 120 秒记录到 SQLite`;
      const hasPoints = data.points.length > 0;
      $("#trend-empty").hidden = hasPoints;
      if (!hasPoints) $("#trend-empty").textContent = "该指标在所选范围内暂无采样。";
      requestAnimationFrame(() => trendDraw());
    } catch (error) {
      state.trend.data = null;
      $("#trend-empty").hidden = false;
      $("#trend-empty").textContent = `历史数据读取失败：${error.message}`;
      $("#trend-status").textContent = "读取失败";
      $("#trend-stats").innerHTML = "";
      trendDraw();
    } finally {
      state.trend.loading = false;
    }
  }

  async function trendOpen(diskId = "") {
    $("#trend-modal").hidden = false;
    $("#ai-modal").hidden = true;
    $("#admin-modal").hidden = true;
    document.body.style.overflow = "hidden";
    trendHideTooltip();
    trendSetView("single", false);
    const days = Number($("#trend-range").value || 30);
    state.trend.days = days;
    try {
      await trendLoadDisks(days, diskId);
      const metricSelect = $("#trend-metric");
      if (metricSelect.options.length) metricSelect.value = state.trend.metric;
      await trendLoadSeries();
    } catch (error) {
      toast(`趋势数据读取失败：${error.message}`, true);
    }
  }

  function trendClose() {
    $("#trend-modal").hidden = true;
    document.body.style.overflow = "";
    trendHideTooltip();
    state.trend.geom = null;
  }

  function trendExport() {
    if (!state.trend.diskId) return;
    window.location.href = `api/history/export?disk_id=${encodeURIComponent(state.trend.diskId)}&metric=${encodeURIComponent(state.trend.metric)}&days=${encodeURIComponent(state.trend.days)}`;
  }

  /* ---------------- 同型号对比 ---------------- */
  const COMPARE_METRICS = [
    { key: "temperature_c", label: "温度", unit: "°C", digits: 1, kind: "temp" },
    { key: "power_on_hours", label: "通电", unit: "h", digits: 0 },
    { key: "media_errors", label: "介质", unit: "次", digits: 0, kind: "media" },
    { key: "other_errors", label: "其他", unit: "次", digits: 0, kind: "other" },
    { key: "reallocated", label: "重映射", unit: "个", digits: 0, kind: "warn" },
    { key: "pending", label: "待映射", unit: "个", digits: 0, kind: "warn" },
    { key: "offline_uncorrectable", label: "不可修正", unit: "个", digits: 0, kind: "warn" },
    { key: "grown_defects", label: "增长缺陷", unit: "个", digits: 0, kind: "warn" },
    { key: "read_uncorrected", label: "读错", unit: "次", digits: 0, kind: "warn" },
    { key: "write_uncorrected", label: "写错", unit: "次", digits: 0, kind: "warn" },
  ];

  function compareControllerName(id) {
    const controller = (state.data?.controllers || []).find((item) => item.id === id);
    return controller?.model || id || "未知控制器";
  }

  function compareControllerLabel(controller) {
    return `${controller?.model || controller?.id || "未知控制器"}`;
  }

  function compareValue(disk, key, digits = 0) {
    const value = disk?.latest?.[key];
    if (value === null || value === undefined) return "—";
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    return digits ? number.toFixed(digits) : Math.round(number).toLocaleString("zh-CN");
  }

  function compareRange(disks, key) {
    const values = disks
      .map((disk) => disk?.latest?.[key])
      .filter((value) => value !== null && value !== undefined && Number.isFinite(Number(value)))
      .map(Number);
    if (!values.length) return null;
    return { min: Math.min(...values), max: Math.max(...values) };
  }

  function compareCellClass(disk, metric) {
    const value = Number(disk?.latest?.[metric.key]);
    if (!Number.isFinite(value) || value <= 0) return "";
    if (metric.kind === "media") return "err-bad";
    if (metric.kind === "other") return "compare-info";
    if (metric.kind === "warn") return "err-warn";
    return "";
  }

  function compareModelGroups(disks) {
    const groups = new Map();
    disks.forEach((disk) => {
      const model = disk.model || "未知型号";
      if (!groups.has(model)) groups.set(model, []);
      groups.get(model).push(disk);
    });
    return [...groups.entries()]
      .map(([model, items]) => ({
        model,
        disks: items.sort((a, b) => String(a.bay || "").localeCompare(String(b.bay || ""), "zh-CN")),
      }))
      .filter((group) => group.disks.length >= 2);
  }

  function compareRender() {
    const data = state.compare.data;
    const groupsRoot = $("#compare-groups");
    if (!data) return;
    comparePopulateControllers();
    const currentIds = new Set((state.data?.disks || []).map((disk) => disk.id));
    let disks = (data.disks || []).filter((disk) => !currentIds.size || currentIds.has(disk.disk_id));
    if (state.compare.controller) disks = disks.filter((disk) => disk.controller_id === state.compare.controller);

    const byController = new Map();
    disks.forEach((disk) => {
      const id = disk.controller_id || "";
      if (!byController.has(id)) byController.set(id, []);
      byController.get(id).push(disk);
    });

    const controllerSections = [];
    let groupCount = 0;
    let comparedCount = 0;
    for (const controller of state.data?.controllers || []) {
      const controllerDisks = byController.get(controller.id) || [];
      if (!controllerDisks.length) continue;
      const groups = compareModelGroups(controllerDisks);
      if (!groups.length) continue;
      groupCount += groups.length;
      comparedCount += groups.reduce((sum, group) => sum + group.disks.length, 0);
      controllerSections.push(`
        <section class="compare-controller">
          <div class="compare-controller-head">
            <div><b>${esc(compareControllerLabel(controller))}</b><span class="compare-controller-id">${esc(controller.id)}</span></div>
            <span>${groups.length} 个同型号组 · ${groups.reduce((sum, group) => sum + group.disks.length, 0)} 块</span>
          </div>
          ${groups.map((group) => compareModelHtml(group)).join("")}
        </section>
      `);
    }

    if (!controllerSections.length) {
      groupsRoot.innerHTML = `<div class="compare-empty">当前控制器下没有同型号且 ≥2 块的磁盘，暂无可对比项。</div>`;
      $("#compare-status").textContent = "暂无可对比项";
      $("#compare-summary").innerHTML = "";
      return;
    }
    groupsRoot.innerHTML = controllerSections.join("");
    $("#compare-status").textContent = `${groupCount} 个型号组 · ${comparedCount} 块盘 · 近 ${data.days} 天`;
    $("#compare-summary").innerHTML = `
      <span><b>${groupCount}</b> 个同型号组</span>
      <span><b>${comparedCount}</b> 块盘参与对比</span>
      <span>按控制器 / 型号分组</span>
      <span>数据范围：最近 ${data.days} 天</span>
    `;
  }

  function compareModelHtml(group) {
    const { model, disks } = group;
    const temp = compareRange(disks, "temperature_c");
    const other = compareRange(disks, "other_errors");
    const power = compareRange(disks, "power_on_hours");
    const media = disks.reduce((sum, disk) => sum + (Number(disk?.latest?.media_errors) || 0), 0);
    const reallocated = disks.reduce((sum, disk) => sum + (Number(disk?.latest?.reallocated) || 0), 0);
    const tempDiff = temp ? temp.max - temp.min : null;
    const otherDiff = other ? other.max - other.min : null;
    const headers = COMPARE_METRICS.map((metric) => `
      <th title="${esc(metric.label)}：最新值，分组上方显示同型号范围">${esc(metric.label)}</th>
    `).join("");
    const rows = disks.map((disk) => {
      const cells = COMPARE_METRICS.map((metric) => {
        const value = compareValue(disk, metric.key, metric.digits);
        const stats = disk.stats?.[metric.key] || {};
        const rangeText = stats.min !== null && stats.max !== null && stats.min !== undefined && stats.max !== undefined
          ? `${stats.min}–${stats.max}`
          : "—";
        return `<td class="${compareCellClass(disk, metric)}" title="最新值；近 ${state.compare.days} 天范围 ${esc(rangeText)}">${esc(value)}${metric.unit && value !== "—" ? `<small class="compare-unit">${esc(metric.unit)}</small>` : ""}</td>`;
      }).join("");
      return `
        <tr>
          <td class="compare-bay">${esc(disk.bay || disk.disk_id)}</td>
          <td class="compare-serial" title="${esc(disk.serial || "")}">${esc(disk.serial || "—")}</td>
          <td>${badge(disk.health || "unknown")}</td>
          ${cells}
        </tr>
      `;
    }).join("");
    return `
      <div class="compare-model">
        <div class="compare-model-head">
          <div>
            <b>${esc(model)}</b>
            <span class="compare-badge">同型号 ${disks.length} 块</span>
          </div>
          <div class="compare-diff">
            <span>温差 <b>${tempDiff === null ? "—" : `${tempDiff.toFixed(1)}°C`}</b></span>
            <span>其他错误范围 <b>${other ? `${Math.round(other.min)}–${Math.round(other.max)}` : "—"}</b></span>
            <span>通电范围 <b>${power ? `${Math.round(power.min).toLocaleString("zh-CN")}–${Math.round(power.max).toLocaleString("zh-CN")} h` : "—"}</b></span>
            <span>介质合计 <b>${media}</b></span>
            <span>重映射合计 <b>${reallocated}</b></span>
          </div>
        </div>
        <div class="compare-table-wrap">
          <table class="compare-table">
            <thead><tr><th>盘位</th><th>序列号</th><th>健康</th>${headers}</tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </div>
    `;
  }

  function comparePopulateControllers() {
    const select = $("#compare-controller");
    const controllers = state.data?.controllers || [];
    const options = ['<option value="">全部控制器</option>'].concat(
      controllers.map((controller) => `<option value="${esc(controller.id)}">${esc(compareControllerLabel(controller))} · ${esc(controller.id)}</option>`)
    );
    select.innerHTML = options.join("");
    if (state.compare.controller && controllers.some((controller) => controller.id === state.compare.controller)) {
      select.value = state.compare.controller;
    } else {
      state.compare.controller = "";
      select.value = "";
    }
  }

  async function compareLoad() {
    if (state.compare.loading) return;
    state.compare.loading = true;
    $("#compare-status").textContent = "加载中…";
    try {
      const res = await request(`api/history/compare?days=${encodeURIComponent(state.compare.days)}`);
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      state.compare.data = data;
      compareRender();
    } catch (error) {
      state.compare.data = null;
      $("#compare-groups").innerHTML = `<div class="compare-empty">对比数据读取失败：${esc(error.message)}</div>`;
      $("#compare-summary").innerHTML = "";
      $("#compare-status").textContent = "读取失败";
    } finally {
      state.compare.loading = false;
    }
  }

  function trendSetView(view, load = true) {
    state.trend.view = view;
    document.querySelectorAll(".trend-tab").forEach((tab) => {
      const active = tab.dataset.trendView === view;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    $("#trend-single-view").hidden = view !== "single";
    $("#trend-compare-view").hidden = view !== "compare";
    if (view === "compare" && load) compareLoad();
  }

  function bindTrend() {
    $("#trend-open").addEventListener("click", () => trendOpen());
    document.querySelectorAll(".trend-tab").forEach((tab) => {
      tab.addEventListener("click", () => trendSetView(tab.dataset.trendView));
    });
    $("#compare-controller").addEventListener("change", (event) => {
      state.compare.controller = event.target.value;
      compareRender();
    });
    $("#compare-range").addEventListener("change", (event) => {
      state.compare.days = Number(event.target.value || 30);
      compareLoad();
    });
    $("#compare-refresh").addEventListener("click", () => compareLoad());
    $("#trend-close").addEventListener("click", trendClose);
    $("#trend-modal").addEventListener("click", (event) => { if (event.target === $("#trend-modal")) trendClose(); });
    $("#trend-export").addEventListener("click", trendExport);
    $("#trend-disk").addEventListener("change", (event) => {
      state.trend.diskId = event.target.value;
      trendLoadSeries();
    });
    $("#trend-metric").addEventListener("change", (event) => {
      state.trend.metric = event.target.value;
      trendLoadSeries();
    });
    $("#trend-range").addEventListener("change", async (event) => {
      state.trend.days = Number(event.target.value || 30);
      try {
        await trendLoadDisks(state.trend.days, state.trend.diskId);
        await trendLoadSeries();
      } catch (error) {
        toast(`趋势数据读取失败：${error.message}`, true);
      }
    });
    $("#trend-canvas").addEventListener("mousemove", trendMove);
    $("#trend-canvas").addEventListener("mouseleave", trendHideTooltip);
    window.addEventListener("resize", () => {
      if (!$("#trend-modal").hidden) requestAnimationFrame(trendDraw);
      document.querySelectorAll(".health-trend").forEach((box) => {
        if (box._trendData) requestAnimationFrame(() => inlineTrendDraw(box, box._trendData));
      });
    });
  }

  /* ---------------- 健康结论内嵌趋势 ---------------- */
  function inlineTrendLabel(box) {
    const select = box.querySelector(".inline-trend-metric");
    return select?.selectedOptions?.[0]?.textContent || "温度";
  }

  function inlineTrendDraw(box, data) {
    const canvas = box.querySelector(".inline-trend-canvas");
    const wrap = box.querySelector(".inline-trend-wrap");
    const empty = box.querySelector(".inline-trend-empty");
    const foot = box.querySelector(".inline-trend-foot");
    if (!canvas || !wrap) return;
    const width = Math.max(240, wrap.clientWidth);
    const height = window.innerWidth <= 760 ? 150 : 170;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, width, height);
    const palette = chartPalette();
    const points = data?.points || [];
    if (!points.length) {
      if (empty) empty.hidden = false;
      if (foot) foot.textContent = "暂无历史采样，应用运行后会持续记录。";
      return;
    }
    if (empty) empty.hidden = true;
    const pad = { left: 40, right: 12, top: 14, bottom: 22 };
    const plotW = width - pad.left - pad.right;
    const plotH = height - pad.top - pad.bottom;
    const values = points.map((point) => Number(point.value));
    let minY = Math.min(...values);
    let maxY = Math.max(...values);
    if (minY === maxY) {
      const delta = Math.max(1, Math.abs(minY) * 0.05);
      minY -= delta;
      maxY += delta;
    } else {
      const delta = (maxY - minY) * 0.08;
      minY -= delta;
      maxY += delta;
    }
    let minX = points[0].ts;
    let maxX = points[points.length - 1].ts;
    if (minX === maxX) { minX -= 3600; maxX += 3600; }
    const px = (ts) => pad.left + ((ts - minX) / (maxX - minX)) * plotW;
    const py = (value) => pad.top + (1 - (value - minY) / (maxY - minY)) * plotH;

    ctx.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.textBaseline = "middle";
    for (let i = 0; i <= 2; i += 1) {
      const y = pad.top + (plotH * i) / 2;
      ctx.strokeStyle = palette.grid;
      ctx.beginPath(); ctx.moveTo(pad.left, y); ctx.lineTo(width - pad.right, y); ctx.stroke();
      const value = maxY - ((maxY - minY) * i) / 2;
      ctx.fillStyle = palette.text; ctx.textAlign = "right";
      ctx.fillText(trendFormat(value, data.metric), pad.left - 6, y);
    }
    for (let i = 0; i <= 3; i += 1) {
      const x = pad.left + (plotW * i) / 3;
      ctx.fillStyle = palette.text; ctx.textAlign = "center";
      ctx.fillText(trendDate(Math.round(minX + ((maxX - minX) * i) / 3), data.days <= 7), x, height - 9);
    }
    ctx.beginPath();
    points.forEach((point, index) => {
      const x = px(point.ts), y = py(Number(point.value));
      if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = palette.line;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = "round";
    ctx.stroke();
    const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom);
    gradient.addColorStop(0, "rgba(56,189,248,.22)");
    gradient.addColorStop(1, "rgba(56,189,248,.01)");
    ctx.lineTo(px(points[points.length - 1].ts), height - pad.bottom);
    ctx.lineTo(px(points[0].ts), height - pad.bottom);
    ctx.closePath();
    ctx.fillStyle = gradient;
    ctx.fill();
    if (points.length <= 60) {
      points.forEach((point) => {
        const x = px(point.ts), y = py(Number(point.value));
        ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI * 2);
        ctx.fillStyle = palette.point; ctx.fill();
        ctx.strokeStyle = palette.line; ctx.lineWidth = 1.2; ctx.stroke();
      });
    }
    if (foot) {
      const latest = data.stats?.latest;
      foot.textContent = `${points.length} 个点 · 最新 ${trendFormat(latest, data.metric)} ${data.unit} · 最近 ${data.days} 天`;
    }
  }

  async function inlineTrendLoad(box) {
    const diskId = box.dataset.diskId;
    const metric = box.querySelector(".inline-trend-metric")?.value || "temperature_c";
    const days = Number(box.querySelector(".inline-trend-days")?.value || 30);
    box.dataset.metric = metric;
    box.dataset.days = String(days);
    const title = box.querySelector(".health-trend-title");
    const range = box.querySelector(".health-trend-range");
    if (title) title.textContent = `${inlineTrendLabel(box)}趋势`;
    if (range) range.textContent = `最近 ${days} 天`;
    const foot = box.querySelector(".inline-trend-foot");
    if (foot) foot.textContent = "加载中…";
    const seq = (Number(box.dataset.seq) || 0) + 1;
    box.dataset.seq = String(seq);
    try {
      const res = await request(`api/history/series?disk_id=${encodeURIComponent(diskId)}&metric=${encodeURIComponent(metric)}&days=${encodeURIComponent(days)}`);
      const data = await res.json().catch(() => ({}));
      if (box.dataset.seq !== String(seq)) return;
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
      box._trendData = data;
      inlineTrendDraw(box, data);
    } catch (error) {
      if (box.dataset.seq !== String(seq)) return;
      box._trendData = null;
      const empty = box.querySelector(".inline-trend-empty");
      if (empty) { empty.hidden = false; empty.textContent = "历史数据读取失败"; }
      if (foot) foot.textContent = error.message;
    }
  }

  function inlineTrendFull(box) {
    const metric = box.querySelector(".inline-trend-metric")?.value || "temperature_c";
    const days = Number(box.querySelector(".inline-trend-days")?.value || 30);
    state.trend.metric = metric;
    state.trend.days = days;
    const range = $("#trend-range");
    if (range) range.value = String(days);
    trendOpen(box.dataset.diskId);
  }

  function initInlineTrend(box) {
    if (!box || box.dataset.ready === "1") return;
    box.dataset.ready = "1";
    box.querySelector(".inline-trend-metric")?.addEventListener("change", () => inlineTrendLoad(box));
    box.querySelector(".inline-trend-days")?.addEventListener("change", () => inlineTrendLoad(box));
    box.querySelector(".inline-trend-full")?.addEventListener("click", (event) => {
      event.stopPropagation();
      inlineTrendFull(box);
    });
    box.querySelector(".inline-trend-canvas")?.addEventListener("click", (event) => {
      event.stopPropagation();
      inlineTrendFull(box);
    });
    inlineTrendLoad(box);
  }

  /* ---------------- 存储拓扑 ---------------- */
  function topoDisk(disk) {
    const temp = disk.temperature_c === null || disk.temperature_c === undefined ? "" : `${disk.temperature_c}°C`;
    const title = [
      disk.model || "",
      disk.serial ? `SN ${disk.serial}` : "",
      disk.sas_address ? `SAS ${disk.sas_address}` : "",
      disk.link_rate || disk.link_speed ? `链路 ${disk.link_rate || disk.link_speed}` : "",
      disk.state_label || disk.state || "",
    ].filter(Boolean).join(" · ");
    return `<div class="topo-disk" title="${esc(title)}">
      ${badge(disk.health || "unknown", "sm")}
      <span class="bay">${esc(disk.bay_label || disk.bay || "—")}</span>
      <span class="name">${esc(disk.model || "—")}</span>
      ${temp ? `<span class="temp">${esc(temp)}</span>` : ""}
    </div>`;
  }

  function topoEnclosure(enclosure) {
    const chips = [];
    if (enclosure.ses) chips.push(`<span class="chip">SES</span>`);
    if (enclosure.expander) chips.push(`<span class="chip">扩展器</span>`);
    const meta = [
      enclosure.vendor ? `厂商 <b>${esc(enclosure.vendor)}</b>` : "",
      enclosure.revision ? `版本 <b>${esc(enclosure.revision)}</b>` : "",
      enclosure.slots !== undefined && enclosure.slots !== null ? `槽位 <b>${esc(enclosure.slots)}</b>` : "",
      enclosure.power_supplies !== undefined && enclosure.power_supplies !== null ? `电源 <b>${esc(enclosure.power_supplies)}</b>` : "",
      enclosure.fans !== undefined && enclosure.fans !== null ? `风扇 <b>${esc(enclosure.fans)}</b>` : "",
      enclosure.temperature_sensors !== undefined && enclosure.temperature_sensors !== null ? `温感 <b>${esc(enclosure.temperature_sensors)}</b>` : "",
      enclosure.alarms !== undefined && enclosure.alarms !== null ? `告警 <b>${esc(enclosure.alarms)}</b>` : "",
      enclosure.sim !== undefined && enclosure.sim !== null ? `SIM <b>${esc(enclosure.sim)}</b>` : "",
      enclosure.connector ? `接口 <b>${esc(enclosure.connector)}</b>` : "",
      enclosure.fru && enclosure.fru !== "N/A" ? `FRU <b>${esc(enclosure.fru)}</b>` : "",
      enclosure.serial && enclosure.serial !== "N/A" ? `SN <b>${esc(enclosure.serial)}</b>` : "",
      enclosure.position ? `位置 <b>${esc(enclosure.position)}</b>` : "",
      enclosure.enclosure_identifier ? `SES <b>${esc(enclosure.enclosure_identifier)}</b>` : "",
      enclosure.expander_id ? `扩展器 <b>${esc(enclosure.expander_id)}</b>` : "",
    ].filter(Boolean).join("");
    const drives = (enclosure.drives || []).map(topoDisk).join("");
    return `<div class="topo-node enclosure">
      <div class="topo-node-head">
        <b>${esc(enclosure.id || "enclosure")} · ${esc(enclosure.product || enclosure.kind_label || "背板")}</b>
        ${chips.join("")}
        ${badge(enclosure.health || "unknown", "sm")}
        <span class="mono">${esc(enclosure.drive_count ?? (enclosure.drives || []).length)} 盘</span>
      </div>
      <div class="topo-node-meta">${meta || "SES 背板信息"}</div>
      <div class="topo-disks">${drives || `<span class="mono" style="font-size:10.5px;color:var(--muted-2)">暂无磁盘</span>`}</div>
    </div>`;
  }

  function topoExpander(expander) {
    const links = (expander.link_rates || []).join(" / ") || "—";
    const meta = [
      expander.vendor ? `厂商 <b>${esc(expander.vendor)}</b>` : "",
      expander.revision ? `版本 <b>${esc(expander.revision)}</b>` : "",
      expander.sas_address ? `SAS <b>${esc(expander.sas_address)}</b>` : "",
      expander.level !== undefined ? `层级 <b>${esc(expander.level)}</b>` : "",
      expander.phy_count !== undefined ? `PHY <b>${esc(expander.phy_count)}</b>` : "",
      `链路 <b>${esc(links)}</b>`,
      expander.invalid_dword_count ? `无效 DWORD <b>${esc(expander.invalid_dword_count)}</b>` : "",
      expander.loss_of_dword_sync_count ? `同步丢失 <b>${esc(expander.loss_of_dword_sync_count)}</b>` : "",
      expander.running_disparity_error_count ? `校验错误 <b>${esc(expander.running_disparity_error_count)}</b>` : "",
    ].filter(Boolean).join("");
    return `<div class="topo-node expander">
      <div class="topo-node-head">
        <b>${esc(expander.id || "expander")} · ${esc(expander.product || "SAS 扩展器")}</b>
        <span class="chip">扩展器</span>
        <span class="mono">${esc(expander.component_vendor || "")} ${esc(expander.component_id || "")}</span>
      </div>
      <div class="topo-node-meta">${meta}</div>
    </div>`;
  }

  function topoController(controller) {
    const meta = [
      controller.pci ? `PCI <b>${esc(controller.pci)}</b>` : "",
      controller.sas_address ? `SAS <b>${esc(controller.sas_address)}</b>` : "",
      controller.firmware ? `FW <b>${esc(controller.firmware)}</b>` : "",
      controller.bios ? `BIOS <b>${esc(controller.bios)}</b>` : "",
      controller.temperature_c !== null && controller.temperature_c !== undefined ? `温度 <b>${esc(controller.temperature_c)}°C</b>` : "",
      controller.bbu?.state ? `BBU <b>${esc(controller.bbu.state)}</b>` : "",
      controller.memory_correctable ? `ECC 可纠正 <b>${esc(controller.memory_correctable)}</b>` : "",
      controller.memory_uncorrectable ? `ECC 不可纠正 <b>${esc(controller.memory_uncorrectable)}</b>` : "",
      controller.details?.onboard_memory ? `缓存 <b>${esc(controller.details.onboard_memory)}</b>` : "",
      controller.details?.front_end_ports ? `前端端口 <b>${esc(controller.details.front_end_ports)}</b>` : "",
      controller.details?.backend_ports != null ? `后端端口 <b>${esc(controller.details.backend_ports)}</b>` : "",
      controller.details?.onboard_expander ? `板载扩展器 <b>${esc(controller.details.onboard_expander)}</b>` : "",
    ].filter(Boolean).join("");
    const vds = (controller.vds || []).map((vd) => `<span class="topo-vd"><span class="lv">${esc(vd.level || "RAID")}</span><span>${esc(vd.size || "")}</span>${badge(vd.health || "unknown", "sm")}<span class="mono">${esc(vd.os_device || "")}</span></span>`).join("");
    const branches = [];
    (controller.enclosures || []).forEach((enclosure) => branches.push(topoEnclosure(enclosure)));
    (controller.expanders || []).forEach((expander) => branches.push(topoExpander(expander)));
    if ((controller.direct_disks || []).length) {
      branches.push(`<div class="topo-node topo-direct">
        <div class="topo-node-head"><b>直连磁盘 / 无背板</b><span class="chip">${esc(controller.kind_label || "直连")}</span><span class="mono">${controller.direct_disks.length} 盘</span></div>
        <div class="topo-disks">${controller.direct_disks.map(topoDisk).join("")}</div>
      </div>`);
    }
    return `<section class="topo-controller ${esc(controller.kind || "")}">
      <div class="topo-head">
        <div>
          <div class="topo-title"><h4>${esc(controller.model || controller.id)}</h4><span class="chip">${esc(controller.kind_label || controller.kind || "")}</span>${badge(controller.health || "unknown", "sm")}</div>
          <div class="topo-meta"><span>控制器 <b>${esc(controller.id || "—")}</b></span><span>磁盘 <b>${esc(controller.disk_count ?? 0)}</b> 块</span>${meta}</div>
        </div>
      </div>
      ${vds ? `<div class="topo-vds">${vds}</div>` : ""}
      <div class="topo-branches">${branches.join("") || `<div class="topo-empty">该控制器暂无背板 / 扩展器 / 物理磁盘信息。</div>`}</div>
    </section>`;
  }

  function renderTopology() {
    const topology = state.data?.topology;
    if (!topology || !(topology.controllers || []).length) {
      $("#topo-summary").innerHTML = "";
      $("#topo-tree").innerHTML = `<div class="topo-empty">暂无拓扑数据。等待下一次采集，或确认 perccli / sysfs 可读。</div>`;
      return;
    }
    const list = state.topo.controller
      ? topology.controllers.filter((c) => c.id === state.topo.controller)
      : topology.controllers;
    const stats = [
      ["控制器", topology.controllers.length],
      ["RAID 卡", topology.controllers.filter((c) => c.kind === "raid").length],
      ["HBA / JBOD", topology.controllers.filter((c) => c.kind === "hba").length],
      ["背板 / SES", topology.enclosures ?? 0],
      ["SAS 扩展器", topology.expanders ?? 0],
      ["物理磁盘", topology.disks ?? 0],
      ["虚拟磁盘", topology.controllers.reduce((sum, c) => sum + (c.vds || []).length, 0)],
    ];
    $("#topo-summary").innerHTML = stats.map(([label, value]) => `<div class="metric"><div class="m-label">${esc(label)}</div><div class="m-value">${esc(value)}</div></div>`).join("")
      + (state.topo.controller ? `<button class="topo-link" id="topo-show-all" type="button">显示全部控制器</button>` : "");
    $("#topo-tree").innerHTML = list.map(topoController).join("");
    const all = $("#topo-show-all");
    if (all) all.addEventListener("click", () => { state.topo.controller = ""; renderTopology(); });
  }

  function topologyOpen(controllerId = "") {
    state.topo.controller = controllerId || "";
    $("#topology-modal").hidden = false;
    $("#ai-modal").hidden = true;
    $("#admin-modal").hidden = true;
    $("#trend-modal").hidden = true;
    document.body.style.overflow = "hidden";
    renderTopology();
  }

  function topologyClose() {
    $("#topology-modal").hidden = true;
    document.body.style.overflow = "";
  }

  function bindTopology() {
    $("#topology-open").addEventListener("click", () => topologyOpen());
    $("#topology-close").addEventListener("click", topologyClose);
    $("#topology-modal").addEventListener("click", (event) => { if (event.target === $("#topology-modal")) topologyClose(); });
  }

  /* ---------------- 关于 / 版权 ---------------- */
  function aboutOpen() {
    const app = state.app || {};
    $("#about-version").textContent = app.version ? `v${app.version}` : "—";
    $("#about-build").textContent = app.build || "—";
    $("#about-modal").hidden = false;
    $("#ai-modal").hidden = true;
    $("#admin-modal").hidden = true;
    $("#trend-modal").hidden = true;
    $("#topology-modal").hidden = true;
    document.body.style.overflow = "hidden";
  }

  function aboutClose() {
    $("#about-modal").hidden = true;
    document.body.style.overflow = "";
  }

  function bindAbout() {
    $("#about-open").addEventListener("click", aboutOpen);
    $("#about-close").addEventListener("click", aboutClose);
    $("#about-done").addEventListener("click", aboutClose);
    $("#about-modal").addEventListener("click", (event) => { if (event.target === $("#about-modal")) aboutClose(); });
  }

  /* ---------------- 亮色 / 暗色主题 ---------------- */
  const THEME_KEY = "disk-health-theme";

  function currentTheme() {
    return document.documentElement.dataset.theme === "light" ? "light" : "dark";
  }

  function applyTheme(theme, persist = true) {
    const next = theme === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    document.documentElement.style.colorScheme = next;
    if (persist) {
      try { localStorage.setItem(THEME_KEY, next); } catch (error) { /* ignore */ }
    }
    const button = $("#theme-toggle");
    if (button) {
      const label = next === "light" ? "切换到暗色主题" : "切换到亮色主题";
      button.title = label;
      button.setAttribute("aria-label", label);
    }
    if (!$("#trend-modal").hidden) requestAnimationFrame(trendDraw);
    document.querySelectorAll(".health-trend").forEach((box) => {
      if (box._trendData) requestAnimationFrame(() => inlineTrendDraw(box, box._trendData));
    });
  }

  function bindTheme() {
    const button = $("#theme-toggle");
    if (!button) return;
    button.addEventListener("click", () => applyTheme(currentTheme() === "light" ? "dark" : "light"));
    applyTheme(currentTheme(), false);
  }

  /* ---------------- 启动 ---------------- */  /* ---------------- 启动 ---------------- */  /* ---------------- 启动 ---------------- */  /* ---------------- 启动 ---------------- */  /* ---------------- 启动 ---------------- */
  bind();
  bindAi();
  bindTrend();
  bindTopology();
  bindAbout();
  bindTheme();
  loadSession();
  load(true);
  $("#skeleton").classList.remove("hidden");
  setInterval(tick, 3000);
  setInterval(() => {
    if (state.collecting) load();
  }, 4000);
})();
