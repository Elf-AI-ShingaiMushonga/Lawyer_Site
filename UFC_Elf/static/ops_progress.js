"use strict";

(function () {
  const overlay = document.getElementById("ops-progress");
  const titleEl = document.getElementById("ops-progress-title");
  const copyEl = document.getElementById("ops-progress-copy");
  const meterEl = document.getElementById("ops-progress-meter");
  const percentEl = document.getElementById("ops-progress-percent");
  const elapsedEl = document.getElementById("ops-progress-seconds");
  const forms = Array.from(document.querySelectorAll(".ops-buttons form[data-ops-action]"));
  const shell = document.querySelector("main.shell");
  const JOBS_CREATE_URL = "/ufc/api/jobs";
  const JOBS_ACTIVE_URL = "/ufc/api/jobs/active";

  if (!overlay || forms.length === 0) {
    return;
  }

  let elapsedTimerId = null;
  let pollTimerId = null;
  let pollInFlight = false;
  let activeJobId = null;
  let startedAtMs = 0;
  let fallbackTitle = "Running operation...";

  function formatElapsed(totalSeconds) {
    const seconds = Math.max(0, Math.floor(totalSeconds));
    const minutes = Math.floor(seconds / 60);
    const rem = seconds % 60;
    if (minutes === 0) {
      return seconds + "s";
    }
    return minutes + "m " + String(rem).padStart(2, "0") + "s";
  }

  function clampPercent(rawValue) {
    const value = Number(rawValue);
    if (!Number.isFinite(value)) {
      return 0;
    }
    return Math.max(0, Math.min(100, Math.round(value)));
  }

  function renderError(message) {
    if (!shell) {
      return;
    }
    const existing = shell.querySelector(".panel.error[data-ops-error='true']");
    if (existing) {
      existing.textContent = message;
      return;
    }
    const section = document.createElement("section");
    section.className = "panel error";
    section.dataset.opsError = "true";
    section.textContent = message;
    const anchor = shell.firstElementChild ? shell.firstElementChild.nextElementSibling : null;
    shell.insertBefore(section, anchor);
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

  function setProgress(percent) {
    const pct = clampPercent(percent);
    if (meterEl) {
      meterEl.style.width = pct + "%";
    }
    if (percentEl) {
      percentEl.textContent = pct + "%";
    }
  }

  function updateElapsed() {
    if (!elapsedEl || startedAtMs <= 0) {
      return;
    }
    const elapsedSeconds = (Date.now() - startedAtMs) / 1000;
    elapsedEl.textContent = formatElapsed(elapsedSeconds);
  }

  function startElapsedTimer() {
    if (elapsedTimerId !== null) {
      window.clearInterval(elapsedTimerId);
      elapsedTimerId = null;
    }
    elapsedTimerId = window.setInterval(updateElapsed, 1000);
  }

  function stopElapsedTimer() {
    if (elapsedTimerId !== null) {
      window.clearInterval(elapsedTimerId);
      elapsedTimerId = null;
    }
  }

  function showOverlay(label) {
    fallbackTitle = label || "Running operation...";
    if (titleEl) {
      titleEl.textContent = fallbackTitle;
    }
    if (copyEl) {
      copyEl.textContent = "Submitting operation...";
    }
    overlay.classList.add("is-visible");
    overlay.setAttribute("aria-hidden", "false");
    document.body.classList.add("ops-progress-active");
    setButtonsDisabled(true);
    startedAtMs = Date.now();
    setProgress(1);
    if (elapsedEl) {
      elapsedEl.textContent = "0s";
    }
    startElapsedTimer();
  }

  function resetOverlay() {
    stopElapsedTimer();
    overlay.classList.remove("is-visible");
    overlay.setAttribute("aria-hidden", "true");
    document.body.classList.remove("ops-progress-active");
    setButtonsDisabled(false);
    setProgress(0);
    if (copyEl) {
      copyEl.textContent = "This can take several minutes. Keep this tab open.";
    }
  }

  function stopPolling() {
    if (pollTimerId !== null) {
      window.clearInterval(pollTimerId);
      pollTimerId = null;
    }
    pollInFlight = false;
    activeJobId = null;
  }

  function updateOverlayFromJob(job) {
    if (!job || typeof job !== "object") {
      return;
    }
    if (titleEl) {
      titleEl.textContent = String(job.stage || fallbackTitle || "Running operation...");
    }
    if (copyEl) {
      const message = String(job.message || "").trim();
      copyEl.textContent = message || "Operation is running...";
    }
    setProgress(job.progress_pct);
    const startedAt = Date.parse(String(job.started_at_utc || ""));
    if (Number.isFinite(startedAt) && startedAt > 0) {
      startedAtMs = startedAt;
      updateElapsed();
    }
  }

  async function fetchJobStatus(jobId) {
    const response = await fetch("/ufc/api/jobs/" + encodeURIComponent(jobId), {
      method: "GET",
      headers: {
        Accept: "application/json",
      },
      cache: "no-store",
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok || !payload || payload.ok !== true || !payload.job) {
      throw new Error(String(payload.error || "Failed to fetch job status."));
    }
    return payload.job;
  }

  async function pollJobOnce() {
    if (!activeJobId || pollInFlight) {
      return;
    }
    pollInFlight = true;
    try {
      const job = await fetchJobStatus(activeJobId);
      updateOverlayFromJob(job);
      const state = String(job.state || "").toLowerCase();
      if (state === "succeeded") {
        setProgress(100);
        if (titleEl) {
          titleEl.textContent = "Completed";
        }
        if (copyEl) {
          copyEl.textContent = "Operation complete. Refreshing page...";
        }
        stopPolling();
        window.setTimeout(() => window.location.reload(), 700);
      } else if (state === "failed") {
        stopPolling();
        resetOverlay();
        renderError(String(job.error || job.message || "Operation failed."));
      }
    } catch (error) {
      stopPolling();
      resetOverlay();
      renderError(String(error && error.message ? error.message : error));
    } finally {
      pollInFlight = false;
    }
  }

  function beginPolling(jobId) {
    activeJobId = String(jobId || "");
    if (!activeJobId) {
      return;
    }
    if (pollTimerId !== null) {
      window.clearInterval(pollTimerId);
      pollTimerId = null;
    }
    pollJobOnce();
    pollTimerId = window.setInterval(pollJobOnce, 1000);
  }

  async function startAsyncJob(form) {
    const actionInput = form.querySelector("input[name='action']");
    const action = String((actionInput && actionInput.value) || form.dataset.opsAction || "").trim();
    if (!action) {
      form.submit();
      return;
    }

    const label = form.getAttribute("data-ops-progress-label") || "Running operation...";
    showOverlay(label);
    try {
      const response = await fetch(JOBS_CREATE_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ action: action }),
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409 && payload && payload.active_job && payload.active_job.job_id) {
        updateOverlayFromJob(payload.active_job);
        beginPolling(payload.active_job.job_id);
        return;
      }
      if (!response.ok || !payload || payload.ok !== true || !payload.job || !payload.job.job_id) {
        throw new Error(String(payload.error || "Failed to start operation."));
      }
      updateOverlayFromJob(payload.job);
      beginPolling(payload.job.job_id);
    } catch (error) {
      stopPolling();
      resetOverlay();
      renderError(String(error && error.message ? error.message : error));
    }
  }

  async function resumeActiveJob() {
    try {
      const response = await fetch(JOBS_ACTIVE_URL, {
        method: "GET",
        headers: {
          Accept: "application/json",
        },
        cache: "no-store",
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || !payload || payload.ok !== true || !payload.job || !payload.job.job_id) {
        return;
      }
      const state = String(payload.job.state || "").toLowerCase();
      if (state !== "queued" && state !== "running") {
        return;
      }
      showOverlay("Resuming operation...");
      updateOverlayFromJob(payload.job);
      beginPolling(payload.job.job_id);
    } catch (_error) {
      return;
    }
  }

  forms.forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      if (overlay.classList.contains("is-visible") || activeJobId) {
        return;
      }
      startAsyncJob(form);
    });
  });

  window.addEventListener("pageshow", () => {
    stopPolling();
    resetOverlay();
    resumeActiveJob();
  });
  resumeActiveJob();
})();
