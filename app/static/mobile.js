(() => {
  const MOBILE = window.matchMedia("(max-width: 760px)");
  const topbar = document.querySelector(".topbar");
  const actions = document.querySelector(".top-actions");
  if (!topbar || !actions) return;

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "mobile-menu-toggle";
  toggle.id = "mobile-menu-toggle";
  toggle.setAttribute("aria-label", "展开菜单");
  toggle.setAttribute("aria-expanded", "false");
  toggle.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  actions.appendChild(toggle);

  const panel = document.createElement("div");
  panel.className = "mobile-menu-panel";
  panel.id = "mobile-menu-panel";
  panel.hidden = true;
  topbar.appendChild(panel);

  let userChip = null;
  async function ensureUser() {
    if (userChip) return;
    const existing = document.querySelector("#user-pill");
    if (existing) { existing.classList.add("mobile-user-visible"); return; }
    userChip = document.createElement("div");
    userChip.className = "mobile-user";
    userChip.innerHTML = '<span class="mobile-avatar">?</span><span class="mobile-user-name">用户</span>';
    actions.insertBefore(userChip, toggle);
    try {
      const res = await fetch("api/session", { cache: "no-store" });
      if (!res.ok) return;
      const data = await res.json();
      const user = data.user || {};
      const name = user.username || (user.uid !== undefined && user.uid !== null ? `用户 ${user.uid}` : "用户");
      userChip.querySelector(".mobile-avatar").textContent = (name || "?").trim().slice(0, 1).toUpperCase();
      userChip.querySelector(".mobile-user-name").textContent = name;
    } catch (e) { /* keep fallback */ }
  }
  ensureUser();

  function candidates() {
    const selectors = [
      "#refresh", "#topology-open", "#trend-open", "#ai-open", "#console-open", ".log-center-btn",
      "#theme-toggle", "#admin-open", "#donate-open", ".logout-btn", ".top-actions > a.btn-ghost",
    ];
    const seen = new Set();
    const out = [];
    selectors.forEach((sel) => {
      document.querySelectorAll(sel).forEach((el) => {
        if (seen.has(el) || el === toggle || el.closest(".mobile-menu-panel")) return;
        if (el.hidden || el.disabled) return;
        if (el.closest(".top-icon-group")?.closest(".top-actions") && sel === "#theme-toggle") {
          /* theme toggle is inside top-icon-group; keep it eligible */
        }
        seen.add(el);
        out.push(el);
      });
    });
    return out;
  }

  function labelFor(el) {
    const text = (el.innerText || "").replace(/\s+/g, " ").trim();
    return text || el.getAttribute("title") || el.getAttribute("aria-label") || "操作";
  }

  function buildMenu() {
    panel.innerHTML = "";
    candidates().forEach((original) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "mobile-menu-item";
      const svg = original.querySelector("svg");
      item.innerHTML = `<span class="mobile-menu-icon">${svg ? svg.outerHTML : ""}</span><span>${labelFor(original)}</span>`;
      item.addEventListener("click", () => {
        closeMenu();
        original.click();
      });
      panel.appendChild(item);
    });
    if (!panel.children.length) {
      const empty = document.createElement("div");
      empty.className = "mobile-menu-empty";
      empty.textContent = "当前没有可用操作";
      panel.appendChild(empty);
    }
  }

  function openMenu() {
    buildMenu();
    panel.hidden = false;
    toggle.setAttribute("aria-expanded", "true");
    toggle.classList.add("open");
  }

  function closeMenu() {
    panel.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
    toggle.classList.remove("open");
  }

  toggle.addEventListener("click", (event) => {
    event.stopPropagation();
    if (panel.hidden) openMenu(); else closeMenu();
  });
  panel.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("pointerdown", (event) => {
    if (panel.hidden) return;
    if (event.target === toggle || toggle.contains(event.target)) return;
    if (panel.contains(event.target)) return;
    closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });
  MOBILE.addEventListener("change", (event) => { if (!event.matches) closeMenu(); });
})();
