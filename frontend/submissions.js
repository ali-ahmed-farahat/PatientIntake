document.addEventListener("click", function (event) {
  const button = event.target.closest("[data-panel-target]");
  if (!button) return;
  const card = button.closest(".submission");
  const panel = document.getElementById(button.dataset.panelTarget);
  if (!card || !panel) return;

  const shouldOpen = panel.hidden;
  card.querySelectorAll(".submission-panel").forEach(function (item) {
    item.hidden = true;
  });
  card.querySelectorAll("[data-panel-target]").forEach(function (item) {
    item.setAttribute("aria-expanded", "false");
    item.classList.remove("submission-tab-active");
  });

  panel.hidden = !shouldOpen;
  button.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
  button.classList.toggle("submission-tab-active", shouldOpen);
});

(function () {
  const STORAGE_KEY = "submissions-language";
  const toggleButton = document.getElementById("language-toggle");
  if (!toggleButton) return;

  function getLanguage() {
    return localStorage.getItem(STORAGE_KEY) === "ar" ? "ar" : "en";
  }

  function translateStaticText(language) {
    document.querySelectorAll("[data-i18n-en][data-i18n-ar]").forEach(function (node) {
      const value = language === "ar" ? node.dataset.i18nAr : node.dataset.i18nEn;
      if (value) node.textContent = value;
    });
  }

  function applyDirection(language) {
    document.documentElement.lang = language;
    document.documentElement.dir = language === "ar" ? "rtl" : "ltr";
    document.body.classList.toggle("is-arabic", language === "ar");
  }

  function updateToggle(language) {
    toggleButton.textContent = language === "ar" ? (toggleButton.dataset.i18nAr || "English") : (toggleButton.dataset.i18nEn || "العربية");
    toggleButton.setAttribute("aria-pressed", language === "ar" ? "true" : "false");
  }

  function applyLanguage(language) {
    applyDirection(language);
    translateStaticText(language);
    updateToggle(language);
    localStorage.setItem(STORAGE_KEY, language);
    document.dispatchEvent(new CustomEvent("submissions-language-change", { detail: { language: language } }));
  }

  toggleButton.addEventListener("click", function () {
    applyLanguage(getLanguage() === "ar" ? "en" : "ar");
  });

  applyLanguage(getLanguage());
})();
