/* notifications.js - real-time SSE notifications for the doctor's submissions page */
(function () {
  "use strict";

  const I18N = {
    en: {
      notifications: "Notifications",
      recentNotifications: "Recent notifications",
      clearAll: "Clear all",
      noNew: "No new submissions yet.",
      connecting: "Connecting...",
      live: "receiving updates",
      reconnecting: "Reconnecting...",
      error: "Connection lost - retrying",
      newPatientSubmission: "New patient submission",
      aiReportReady: "AI clinical report is ready",
      view: "View",
      dismiss: "Dismiss",
      age: "Age",
      urology: "Urology / مسالك",
      consultation: "Consultation / استشارة",
      examination: "Examination / كشف",
    },
    ar: {
      notifications: "الإشعارات",
      recentNotifications: "أحدث الإشعارات",
      clearAll: "مسح الكل",
      noNew: "لا توجد استمارات جديدة حتى الآن.",
      connecting: "جارٍ الاتصال...",
      live: "يتم استقبال التحديثات",
      reconnecting: "جارٍ إعادة الاتصال...",
      error: "انقطع الاتصال - تتم إعادة المحاولة",
      newPatientSubmission: "استمارة مريض جديدة",
      aiReportReady: "أصبح التقرير السريري بالذكاء الاصطناعي جاهزاً",
      view: "عرض",
      dismiss: "إغلاق",
      age: "العمر",
      urology: "مسالك",
      consultation: "استشارة",
      examination: "كشف",
    }
  };

  let unreadCount = 0;
  const originalTitle = document.title;
  let retryDelay = 2000;
  let es = null;
  let connectionState = "connecting";

  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function currentLanguage() {
    return document.documentElement.lang === "ar" ? "ar" : "en";
  }

  function t(key) {
    const lang = currentLanguage();
    return (I18N[lang] && I18N[lang][key]) || I18N.en[key] || key;
  }

  function visitLabel(type) {
    return {
      urology: t("urology"),
      consultation: t("consultation"),
      examination: t("examination"),
    }[type] || (type || "-");
  }

  function buildUI() {
    const toolbar = document.querySelector(".toolbar");
    if (!toolbar) {
      console.warn("[notifications] .toolbar not found - bell cannot be injected");
      return false;
    }

    const actionsHost = toolbar.querySelector(".toolbar-actions") || toolbar;

    const bell = document.createElement("button");
    bell.id = "notif-bell";
    bell.className = "notif-bell";
    bell.setAttribute("aria-label", t("notifications"));
    bell.setAttribute("aria-haspopup", "true");
    bell.setAttribute("aria-expanded", "false");
    bell.innerHTML = `
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
           stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/>
        <path d="M13.73 21a2 2 0 0 1-3.46 0"/>
      </svg>
      <span id="notif-badge" class="notif-badge" hidden>0</span>
    `;
    actionsHost.appendChild(bell);

    const panel = document.createElement("div");
    panel.id = "notif-panel";
    panel.className = "notif-panel";
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", t("recentNotifications"));
    panel.innerHTML = `
      <div class="notif-panel-header">
        <span id="notif-panel-title">${t("notifications")}</span>
        <button id="notif-clear" class="notif-clear-btn" type="button">${t("clearAll")}</button>
      </div>
      <ul id="notif-list" class="notif-list">
        <li class="notif-empty">${t("noNew")}</li>
      </ul>
      <div id="notif-status" class="notif-conn-status notif-conn-connecting">${t("connecting")}</div>
    `;
    document.body.appendChild(panel);

    const toasts = document.createElement("div");
    toasts.id = "notif-toasts";
    toasts.className = "notif-toasts";
    toasts.setAttribute("aria-live", "assertive");
    document.body.appendChild(toasts);

    bell.addEventListener("click", function (e) {
      e.stopPropagation();
      const open = !panel.hidden;
      panel.hidden = open;
      bell.setAttribute("aria-expanded", String(!open));
      if (!open) clearUnread();
    });

    document.addEventListener("click", function (e) {
      if (!panel.hidden && !panel.contains(e.target) && e.target !== bell) {
        panel.hidden = true;
        bell.setAttribute("aria-expanded", "false");
      }
    });

    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && !panel.hidden) {
        panel.hidden = true;
        bell.setAttribute("aria-expanded", "false");
        bell.focus();
      }
    });

    document.getElementById("notif-clear").addEventListener("click", function () {
      clearNotifList();
      clearUnread();
    });

    return true;
  }

  function incrementUnread() {
    unreadCount++;
    const badge = document.getElementById("notif-badge");
    if (badge) {
      badge.textContent = unreadCount > 99 ? "99+" : String(unreadCount);
      badge.hidden = false;
    }
    document.title = "(" + unreadCount + ") " + originalTitle;
  }

  function clearUnread() {
    unreadCount = 0;
    const badge = document.getElementById("notif-badge");
    if (badge) badge.hidden = true;
    document.title = originalTitle;
  }

  function clearNotifList() {
    const list = document.getElementById("notif-list");
    if (list) list.innerHTML = '<li class="notif-empty">' + escHtml(t("noNew")) + "</li>";
  }

  function prependToList(sub) {
    const list = document.getElementById("notif-list");
    if (!list) return;
    const empty = list.querySelector(".notif-empty");
    if (empty) empty.remove();

    const li = document.createElement("li");
    li.className = "notif-item";
    li.innerHTML = `
      <a href="#submission-${escHtml(String(sub.submission_id))}" class="notif-link">
        <span class="notif-name">${escHtml(sub.full_name)}</span>
        <span class="notif-meta">${escHtml(visitLabel(sub.visit_type))}${sub.age ? " · " + escHtml(t("age")) + " " + escHtml(String(sub.age)) : ""} · #${escHtml(String(sub.submission_id))}</span>
      </a>
      <time class="notif-time">${escHtml(sub.timestamp)}</time>
    `;
    li.querySelector(".notif-link").addEventListener("click", function () {
      const panel = document.getElementById("notif-panel");
      if (panel) panel.hidden = true;
      const bell = document.getElementById("notif-bell");
      if (bell) bell.setAttribute("aria-expanded", "false");
    });
    list.prepend(li);

    const items = list.querySelectorAll(".notif-item");
    if (items.length > 50) items[items.length - 1].remove();
  }

  function showToast(sub) {
    const container = document.getElementById("notif-toasts");
    if (!container) return;

    const isPipelineCompleted = sub.type === "pipeline_completed";
    const title = isPipelineCompleted ? t("aiReportReady") : t("newPatientSubmission");
    const subtitle = isPipelineCompleted
      ? "#" + escHtml(String(sub.submission_id)) + " · " + escHtml(String(sub.status || "completed"))
      : escHtml(sub.full_name) + (sub.age ? " · " + escHtml(t("age")) + " " + escHtml(String(sub.age)) : "") + " · " + escHtml(visitLabel(sub.visit_type));

    const toast = document.createElement("div");
    toast.className = "notif-toast";
    toast.setAttribute("role", "status");
    toast.innerHTML = `
      <div class="notif-toast-icon" aria-hidden="true">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/>
          <path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>
        </svg>
      </div>
      <div class="notif-toast-body">
        <strong class="notif-toast-title">${title}</strong>
        <span class="notif-toast-sub">${subtitle}</span>
      </div>
      <a href="#submission-${escHtml(String(sub.submission_id))}" class="notif-toast-action">${escHtml(t("view"))}</a>
      <button class="notif-toast-close" aria-label="${escHtml(t("dismiss"))}" type="button">×</button>
    `;

    toast.querySelector(".notif-toast-close").addEventListener("click", function () {
      dismissToast(toast);
    });
    toast.querySelector(".notif-toast-action").addEventListener("click", function () {
      dismissToast(toast);
    });
    container.appendChild(toast);

    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        toast.classList.add("notif-toast-visible");
      });
    });

    const timer = setTimeout(function () {
      dismissToast(toast);
    }, 7000);
    toast._dismissTimer = timer;
  }

  function dismissToast(toast) {
    clearTimeout(toast._dismissTimer);
    toast.classList.remove("notif-toast-visible");
    setTimeout(function () {
      if (toast.parentNode) toast.parentNode.removeChild(toast);
    }, 400);
  }

  function highlightNewSubmission(id) {
    const card = document.getElementById("submission-" + id);
    if (card) {
      card.classList.add("submission-new");
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      setTimeout(function () {
        card.classList.remove("submission-new");
      }, 6000);
    } else {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  function setConnStatus(state) {
    connectionState = state;
    const el = document.getElementById("notif-status");
    if (!el) return;
    el.className = "notif-conn-status notif-conn-" + state;
    el.textContent = {
      connecting: t("connecting"),
      live: t("live"),
      reconnecting: t("reconnecting"),
      error: t("error"),
    }[state] || state;
  }

  function refreshLanguage() {
    const bell = document.getElementById("notif-bell");
    const panel = document.getElementById("notif-panel");
    const panelTitle = document.getElementById("notif-panel-title");
    const clearButton = document.getElementById("notif-clear");
    const empty = document.querySelector(".notif-empty");

    if (bell) bell.setAttribute("aria-label", t("notifications"));
    if (panel) panel.setAttribute("aria-label", t("recentNotifications"));
    if (panelTitle) panelTitle.textContent = t("notifications");
    if (clearButton) clearButton.textContent = t("clearAll");
    if (empty) empty.textContent = t("noNew");
    setConnStatus(connectionState);
  }

  function connect() {
    if (es) {
      es.close();
      es = null;
    }

    if (!window.EventSource) {
      console.warn("[notifications] EventSource not supported in this browser.");
      setConnStatus("error");
      return;
    }

    console.log("[notifications] Connecting to /events ...");
    setConnStatus("connecting");
    es = new EventSource("/events");

    es.addEventListener("connected", function () {
      console.log("[notifications] SSE connected");
      retryDelay = 2000;
      setConnStatus("live");
    });

    es.addEventListener("new_submission", function (event) {
      console.log("[notifications] new_submission received:", event.data);
      let sub;
      try {
        sub = JSON.parse(event.data);
      } catch (err) {
        console.error("[notifications] JSON parse error:", err);
        return;
      }
      incrementUnread();
      if (sub.type !== "pipeline_completed") {
        prependToList(sub);
      }
      showToast(sub);
      highlightNewSubmission(sub.submission_id);
      if (sub.type === "pipeline_completed" && window.location.pathname === "/submissions") {
        setTimeout(function () {
          window.location.reload();
        }, 1200);
      }
    });

    es.addEventListener("error", function (err) {
      console.warn("[notifications] SSE error, will retry in", retryDelay, "ms", err);
      es.close();
      es = null;
      setConnStatus("reconnecting");
      setTimeout(function () {
        connect();
      }, retryDelay);
      retryDelay = Math.min(retryDelay * 1.5, 30000);
    });
  }

  function init() {
    const uiReady = buildUI();
    if (uiReady) {
      refreshLanguage();
      connect();
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  document.addEventListener("submissions-language-change", refreshLanguage);
})();
