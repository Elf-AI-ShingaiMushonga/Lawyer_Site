"use strict";

(function () {
  const overlay = document.getElementById("ops-progress");
  const titleEl = document.getElementById("ops-progress-title");
  const elapsedEl = document.getElementById("ops-progress-seconds");
  const forms = Array.from(document.querySelectorAll(".ops-buttons form[data-ops-progress-label]"));

  if (!overlay || forms.length === 0) {
    return;
  }

  let timerId = null;
  let startedAtMs = 0;

  function formatElapsed(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const minutes = Math.floor(seconds / 60);
    const rem = seconds % 60;
    if (minutes === 0) {
      return seconds + "s";
    }
    return minutes + "m " + String(rem).padStart(2, "0") + "s";
  }

  function setButtonsDisabled(disabled) {
    forms.forEach((form) => {
      const button = form.querySelector("button[type='submit']");
      if (!button) {
        return;
      }
      if (disabled) {
        button.dataset.originalText = button.textContent || "";
        button.disabled = true;
        button.textContent = "Running...";
      } else {
        button.disabled = false;
        if (button.dataset.originalText) {
          button.textContent = button.dataset.originalText;
          delete button.dataset.originalText;
        }
      }
    });
  }

  function showOverlay(label) {
    if (titleEl) {
      titleEl.textContent = label || "Running operation...";
    }
    overlay.classList.add("is-visible");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("ops-progress-active");
    setButtonsDisabled(true);
    startedAtMs = Date.now();
    if (elapsedEl) {
      elapsedEl.textContent = "0s";
    }
    timerId = window.setInterval(() => {
      if (!elapsedEl) {
        return;
      }
      const elapsedSeconds = (Date.now() - startedAtMs) / 1000;
      elapsedEl.textContent = formatElapsed(elapsedSeconds);
    }, 1000);
  }

  function resetOverlay() {
    if (timerId !== null) {
      window.clearInterval(timerId);
      timerId = null;
    }
    overlay.classList.remove("is-visible");
    overlay.setAttribute("aria-hidden", "true");
    document.body.classList.remove("ops-progress-active");
    setButtonsDisabled(false);
  }

  forms.forEach((form) => {
    form.addEventListener("submit", () => {
      if (overlay.classList.contains("is-visible")) {
        return;
      }
      const label = form.getAttribute("data-ops-progress-label") || "Running operation...";
      showOverlay(label);
    });
  });

  window.addEventListener("pageshow", resetOverlay);
})();
