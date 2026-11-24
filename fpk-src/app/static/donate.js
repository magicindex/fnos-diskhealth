(() => {
  const KEY = "disk-health-theme";
  const root = document.documentElement;
  const button = document.getElementById("theme-toggle");
  const current = () => root.dataset.theme === "light" ? "light" : "dark";
  const apply = (theme, persist = true) => {
    const next = theme === "light" ? "light" : "dark";
    root.dataset.theme = next;
    root.style.colorScheme = next;
    if (persist) {
      try { localStorage.setItem(KEY, next); } catch (error) { /* ignore */ }
    }
    if (button) {
      const label = next === "light" ? "切换到暗色主题" : "切换到亮色主题";
      button.title = label;
      button.setAttribute("aria-label", label);
    }
  };
  apply(current(), false);
  button?.addEventListener("click", () => apply(current() === "light" ? "dark" : "light"));
})();
