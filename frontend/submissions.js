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
