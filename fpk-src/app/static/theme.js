(() => {
  const KEY = "disk-health-theme";
  let theme = "dark";
  try {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (error) {
    /* localStorage 不可用时保持默认暗色 */
  }
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
})();
