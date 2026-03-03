(() => {
  const isTextInput = (element) => {
    if (!element) {
      return false;
    }
    const tag = element.tagName ? element.tagName.toLowerCase() : "";
    if (tag === "input" || tag === "textarea" || tag === "select") {
      return true;
    }
    return Boolean(element.isContentEditable);
  };

  const initFlashDismiss = () => {
    const dismissers = document.querySelectorAll("[data-dismiss-alert]");
    dismissers.forEach((button) => {
      button.addEventListener("click", () => {
        const card = button.closest(".flash-card");
        if (!card) {
          return;
        }
        card.style.opacity = "0";
        card.style.transform = "translateY(-4px)";
        window.setTimeout(() => {
          card.remove();
        }, 160);
      });
    });
  };

  const initPasswordToggles = () => {
    const toggles = document.querySelectorAll("[data-password-toggle]");
    toggles.forEach((button) => {
      button.addEventListener("click", () => {
        const targetId = button.getAttribute("data-password-toggle");
        const input = targetId ? document.getElementById(targetId) : null;
        if (!input || !(input instanceof HTMLInputElement)) {
          return;
        }
        const reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        button.textContent = reveal ? "Hide" : "Show";
        button.setAttribute("aria-pressed", reveal ? "true" : "false");
      });
    });
  };

  const initStatusToneSystem = () => {
    const toneClasses = ["tone-accent", "tone-positive", "tone-warning", "tone-danger", "tone-neutral"];

    const accentTokens = new Set([
      "open",
      "todo",
      "new",
      "draft",
      "queued",
      "running",
      "in-progress",
      "active",
      "submitted",
    ]);
    const dangerTokens = new Set([
      "critical",
      "high",
      "high-risk",
      "overdue",
      "failed",
      "failure",
      "rejected",
      "declined",
      "blocked",
      "exception",
      "at-risk",
      "breach",
      "error",
      "alert",
      "risk-high",
      "risk-critical",
    ]);
    const positiveTokens = new Set([
      "on-track",
      "low",
      "low-risk",
      "approved",
      "accepted",
      "qualified",
      "retained",
      "paid",
      "settled",
      "resolved",
      "complete",
      "completed",
      "signed",
      "ack",
      "acknowledged",
      "success",
      "healthy",
      "ready",
    ]);
    const warningTokens = new Set([
      "on-hold",
      "hold",
      "pending",
      "due",
      "paused",
      "processing",
      "review",
      "in-review",
      "under-review",
      "medium",
      "medium-risk",
    ]);
    const neutralTokens = new Set([
      "closed",
      "done",
      "cancelled",
      "canceled",
      "archived",
      "inactive",
      "unknown",
      "none",
      "expired",
      "n-a",
      "na",
    ]);

    const normalizeToken = (value) =>
      String(value || "")
        .toLowerCase()
        .trim()
        .replace(/[_/]+/g, "-")
        .replace(/[^a-z0-9 -]+/g, "")
        .replace(/\s+/g, "-")
        .replace(/-+/g, "-")
        .replace(/^-|-$/g, "");

    const toneFromToken = (rawToken) => {
      const token = normalizeToken(rawToken).replace(/^(status|risk|stage)-/, "");
      if (!token) {
        return null;
      }
      if (dangerTokens.has(token)) {
        return "tone-danger";
      }
      if (positiveTokens.has(token)) {
        return "tone-positive";
      }
      if (warningTokens.has(token)) {
        return "tone-warning";
      }
      if (neutralTokens.has(token)) {
        return "tone-neutral";
      }
      if (accentTokens.has(token)) {
        return "tone-accent";
      }
      return null;
    };

    const semanticTokensFromText = (value) => {
      const clean = String(value || "")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, " ")
        .trim();
      if (!clean) {
        return [];
      }
      const words = clean.split(/\s+/).filter((item) => item.length > 0);
      const pairs = [];
      for (let index = 0; index < words.length - 1; index += 1) {
        pairs.push(`${words[index]}-${words[index + 1]}`);
      }
      return [...pairs, ...words];
    };

    const firstPrefixedToken = (element, prefix, ignored) => {
      const classes = Array.from(element.classList);
      for (const className of classes) {
        if (!className.startsWith(prefix) || ignored.has(className)) {
          continue;
        }
        return className.slice(prefix.length);
      }
      return "";
    };

    const applyTone = (element, fallbackTone = null) => {
      if (!(element instanceof HTMLElement)) {
        return;
      }
      toneClasses.forEach((tone) => element.classList.remove(tone));

      const explicitStatus = firstPrefixedToken(element, "status-", new Set(["status-badge"]));
      const explicitRisk = firstPrefixedToken(element, "risk-", new Set(["risk-chip"]));
      const tokens = [];
      if (explicitStatus) {
        tokens.push(explicitStatus);
      }
      if (explicitRisk) {
        tokens.push(explicitRisk);
      }
      tokens.push(...semanticTokensFromText(element.textContent || ""));

      let tone = null;
      for (const token of tokens) {
        tone = toneFromToken(token);
        if (tone) {
          break;
        }
      }
      if (!tone && fallbackTone) {
        tone = fallbackTone;
      }
      if (tone) {
        element.classList.add(tone);
      }
    };

    document.querySelectorAll(".status-badge").forEach((element) => applyTone(element, "tone-accent"));
    document.querySelectorAll(".risk-chip").forEach((element) => applyTone(element, "tone-warning"));
    document.querySelectorAll(".tag-chip").forEach((element) => applyTone(element, "tone-accent"));
    document
      .querySelectorAll(".filter-chip-btn, .nav-pill[data-matter-filter], .matter-status-filter-badge")
      .forEach((element) => applyTone(element));
  };

  const initGlobalOmnibox = () => {
    const form = document.getElementById("global-omnibox-form");
    const input = document.getElementById("global-nav-search");
    const datalist = document.getElementById("global-omnibox-options");
    if (!(form instanceof HTMLFormElement) || !(input instanceof HTMLInputElement)) {
      return;
    }

    const normalize = (value) =>
      String(value || "")
        .toLowerCase()
        .replace(/\s+/g, " ")
        .trim();

    const tokenize = (value) =>
      normalize(value)
        .split(/[^a-z0-9/_-]+/g)
        .filter((token) => token.length > 0);

    const unique = (values) => {
      const seen = new Set();
      return values.filter((value) => {
        if (seen.has(value)) {
          return false;
        }
        seen.add(value);
        return true;
      });
    };

    const parseRoutes = () =>
      Array.from(document.querySelectorAll("[data-command-item]"))
        .map((item) => {
          if (!(item instanceof HTMLAnchorElement)) {
            return null;
          }
          const href = item.getAttribute("href") || "";
          if (!href) {
            return null;
          }
          const titleNode = item.querySelector(".command-item-title");
          const title = (titleNode ? titleNode.textContent : item.textContent || "")
            .replace(/\s+/g, " ")
            .trim();
          const keywords = (item.getAttribute("data-keywords") || "")
            .replace(/\s+/g, " ")
            .trim();
          if (!title) {
            return null;
          }

          return {
            href,
            title,
            normalizedTitle: normalize(title),
            aliases: unique(tokenize(`${title} ${keywords}`)),
            searchable: normalize(`${title} ${keywords}`),
          };
        })
        .filter((item) => item && item.title);

    let routes = parseRoutes();

    const getRoutes = () => {
      if (routes.length === 0) {
        routes = parseRoutes();
      }
      return routes;
    };

    const populateDatalist = () => {
      if (!(datalist instanceof HTMLDataListElement)) {
        return;
      }
      const existing = new Set(
        Array.from(datalist.querySelectorAll("option"))
          .map((option) => option.value.trim().toLowerCase())
          .filter((value) => value.length > 0)
      );
      getRoutes()
        .slice(0, 24)
        .forEach((route) => {
          const key = route.title.toLowerCase();
          if (existing.has(key)) {
            return;
          }
          const option = document.createElement("option");
          option.value = route.title;
          datalist.appendChild(option);
          existing.add(key);
        });
    };

    const findBestRoute = (rawQuery) => {
      const query = normalize(rawQuery);
      if (!query) {
        return null;
      }
      const list = getRoutes();
      if (list.length === 0) {
        return null;
      }

      const tokens = tokenize(query);
      const exact =
        list.find((route) => route.normalizedTitle === query || route.aliases.includes(query)) || null;
      if (exact) {
        return exact;
      }

      const titlePrefix = list.find((route) => route.normalizedTitle.startsWith(query)) || null;
      if (titlePrefix) {
        return titlePrefix;
      }

      const tokenPrefix =
        list.find(
          (route) =>
            tokens.length > 0 &&
            tokens.every((token) => route.aliases.some((alias) => alias.startsWith(token)))
        ) || null;
      if (tokenPrefix) {
        return tokenPrefix;
      }

      return list.find((route) => route.searchable.includes(query)) || null;
    };

    form.addEventListener("submit", (event) => {
      const query = input.value.trim();
      if (!query) {
        return;
      }

      const forcedSearch =
        query.startsWith("?")
          ? query.slice(1).trim()
          : query.toLowerCase().startsWith("search ")
            ? query.slice(7).trim()
            : null;
      if (forcedSearch !== null) {
        input.value = forcedSearch;
        return;
      }

      if (/^\/[a-z0-9/_-]*$/i.test(query)) {
        event.preventDefault();
        window.location.href = query;
        return;
      }

      const route = findBestRoute(query);
      if (route) {
        event.preventDefault();
        window.location.href = route.href;
      }
    });

    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && input.value.trim().length > 0) {
        input.value = "";
      }
    });

    populateDatalist();
  };

  const initCommandPalette = () => {
    const palette = document.getElementById("command-palette");
    const openers = document.querySelectorAll("[data-command-open]");
    const closers = document.querySelectorAll("[data-command-close]");
    const input = document.getElementById("command-search");
    const items = Array.from(document.querySelectorAll("[data-command-item]"));
    const empty = document.getElementById("command-empty");
    const navSearch = document.getElementById("global-nav-search");
    const dynamicSearch = document.getElementById("command-search-route");
    const defaultSearchHref = dynamicSearch ? dynamicSearch.getAttribute("href") : null;

    if (!palette || !input || items.length === 0) {
      return;
    }

    const isOpen = () => !palette.hasAttribute("hidden");
    let activeIndex = -1;

    const visibleItems = () => items.filter((item) => !item.hidden);

    const clearActiveItem = () => {
      items.forEach((item) => item.classList.remove("is-active"));
      activeIndex = -1;
    };

    const setActiveItem = (index) => {
      const visible = visibleItems();
      if (visible.length === 0) {
        clearActiveItem();
        return null;
      }
      const wrappedIndex = ((index % visible.length) + visible.length) % visible.length;
      clearActiveItem();
      const item = visible[wrappedIndex];
      item.classList.add("is-active");
      activeIndex = wrappedIndex;
      item.scrollIntoView({ block: "nearest" });
      return item;
    };

    const navigateToActiveItem = () => {
      const visible = visibleItems();
      if (visible.length === 0) {
        return;
      }
      const target = activeIndex >= 0 ? visible[activeIndex] : visible[0];
      const href = target.getAttribute("href") || target.href;
      if (href) {
        window.location.href = href;
      }
    };

    const filterItems = () => {
      const query = input.value.trim().toLowerCase();
      let visibleCount = 0;
      items.forEach((item) => {
        const keywords = (item.dataset.keywords || "").toLowerCase();
        const content = item.textContent ? item.textContent.toLowerCase() : "";
        const matches = !query || keywords.includes(query) || content.includes(query);
        item.hidden = !matches;
        if (matches) {
          visibleCount += 1;
        }
      });
      if (empty) {
        empty.hidden = visibleCount > 0;
      }

      if (dynamicSearch && defaultSearchHref) {
        if (query) {
          dynamicSearch.setAttribute("href", `/search?q=${encodeURIComponent(query)}`);
        } else {
          dynamicSearch.setAttribute("href", defaultSearchHref);
        }
      }

      if (visibleCount === 0) {
        clearActiveItem();
      } else {
        setActiveItem(0);
      }
    };

    const openPalette = () => {
      palette.removeAttribute("hidden");
      document.body.style.overflow = "hidden";
      filterItems();
      window.setTimeout(() => {
        input.focus();
        input.select();
      }, 0);
    };

    const closePalette = () => {
      palette.setAttribute("hidden", "");
      document.body.style.overflow = "";
      clearActiveItem();
    };

    openers.forEach((button) => {
      button.addEventListener("click", () => {
        if (isOpen()) {
          closePalette();
        } else {
          openPalette();
        }
      });
    });

    closers.forEach((button) => {
      button.addEventListener("click", closePalette);
    });

    palette.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement)) {
        return;
      }
      if (target.classList.contains("command-backdrop")) {
        closePalette();
      }
    });

    input.addEventListener("input", filterItems);
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        setActiveItem(activeIndex + 1);
        return;
      }
      if (event.key === "ArrowUp") {
        event.preventDefault();
        setActiveItem(activeIndex - 1);
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        navigateToActiveItem();
      }
    });

    items.forEach((item) => {
      item.addEventListener("mouseenter", () => {
        const visible = visibleItems();
        const index = visible.indexOf(item);
        if (index >= 0) {
          setActiveItem(index);
        }
      });
      item.addEventListener("focus", () => {
        const visible = visibleItems();
        const index = visible.indexOf(item);
        if (index >= 0) {
          setActiveItem(index);
        }
      });
    });

    document.addEventListener("keydown", (event) => {
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        if (isOpen()) {
          closePalette();
        } else {
          openPalette();
        }
        return;
      }
      if (event.key === "Escape" && isOpen()) {
        event.preventDefault();
        closePalette();
        return;
      }
      if (event.key === "/" && !isOpen() && !isTextInput(event.target)) {
        if (navSearch) {
          event.preventDefault();
          navSearch.focus();
          navSearch.select();
        }
      }
    });
  };

  const initMatterQuickFilters = () => {
    const list = document.getElementById("matter-list");
    const buttons = Array.from(document.querySelectorAll("[data-matter-filter]"));
    if (!list || buttons.length === 0) {
      return;
    }
    const items = Array.from(list.querySelectorAll("[data-matter-item]"));
    const statusBadges = Array.from(list.querySelectorAll("[data-matter-status-filter]"));
    const empty = document.getElementById("matter-filter-empty");
    const summary = document.getElementById("matter-filter-summary");
    const state = { status: "all", risk: "all" };

    const updateButtons = () => {
      buttons.forEach((button) => {
        const raw = button.dataset.matterFilter || "";
        const [group, value] = raw.split(":");
        const isActive = state[group] === value;
        button.classList.toggle("is-active", isActive);
        button.setAttribute("aria-pressed", String(isActive));
      });
      statusBadges.forEach((badge) => {
        const value = badge.getAttribute("data-matter-status-filter") || "";
        const isActive = value && state.status === value;
        badge.classList.toggle("is-active-filter", isActive);
        badge.setAttribute("aria-pressed", String(isActive));
      });
    };

    const applyFilters = () => {
      let visible = 0;
      items.forEach((item) => {
        const itemStatus = item.dataset.status || "unknown";
        const itemRisk = item.dataset.risk || "unknown";
        const statusMatch = state.status === "all" || state.status === itemStatus;
        const riskMatch = state.risk === "all" || state.risk === itemRisk;
        const matches = statusMatch && riskMatch;
        item.hidden = !matches;
        if (matches) {
          visible += 1;
        }
      });
      if (empty) {
        empty.hidden = visible > 0;
      }
      if (summary) {
        const statusText = state.status === "all" ? "All statuses" : `Status: ${state.status.replace("-", " ")}`;
        const riskText = state.risk === "all" ? "All risks" : `Risk: ${state.risk}`;
        summary.textContent = `${visible} visible · ${statusText} · ${riskText}`;
      }
    };

    buttons.forEach((button) => {
      button.addEventListener("click", () => {
        const raw = button.dataset.matterFilter || "";
        const [group, value] = raw.split(":");
        if (!group || !value || !(group in state)) {
          return;
        }
        state[group] = value;
        updateButtons();
        applyFilters();
      });
    });

    statusBadges.forEach((badge) => {
      badge.addEventListener("click", (event) => {
        event.preventDefault();
        event.stopPropagation();
        const status = badge.getAttribute("data-matter-status-filter") || "";
        if (!status) {
          return;
        }
        state.status = state.status === status ? "all" : status;
        updateButtons();
        applyFilters();
      });
    });

    const goToMatter = (event, item) => {
      const target = event.target;
      if (target instanceof HTMLElement && target.closest("a, button, input, select, textarea, label, summary")) {
        return;
      }
      const href = item.dataset.matterLink || "";
      if (!href) {
        return;
      }
      window.location.href = href;
    };

    items.forEach((item) => {
      item.addEventListener("click", (event) => goToMatter(event, item));
      if (item.tabIndex >= 0) {
        item.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") {
            return;
          }
          event.preventDefault();
          goToMatter(event, item);
        });
      }
    });

    updateButtons();
    applyFilters();
  };

  const initNavMenus = () => {
    const menus = Array.from(document.querySelectorAll("[data-nav-menu]")).filter(
      (menu) => menu instanceof HTMLDetailsElement
    );
    if (menus.length === 0) {
      return;
    }

    const closeAll = () => {
      menus.forEach((menu) => {
        menu.open = false;
      });
    };

    document.addEventListener("click", (event) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      const clickedInMenu = menus.some((menu) => menu.contains(target));
      if (!clickedInMenu) {
        closeAll();
      }
    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeAll();
      }
    });
  };

  const initTimePrompts = () => {
    const forms = Array.from(document.querySelectorAll("[data-time-prompt-form]"));
    if (forms.length === 0) {
      return;
    }

    const renderPrompts = (panel, list, prompts) => {
      if (!panel || !list) {
        return;
      }
      list.innerHTML = "";
      if (!Array.isArray(prompts) || prompts.length === 0) {
        panel.hidden = true;
        return;
      }
      prompts.forEach((prompt) => {
        const item = document.createElement("li");
        const level = String(prompt.level || "info").toLowerCase();
        item.className = `time-prompt-item time-prompt-${level}`;
        item.textContent = String(prompt.message || "").trim();
        if (item.textContent.length === 0) {
          return;
        }
        list.appendChild(item);
      });
      panel.hidden = list.children.length === 0;
    };

    forms.forEach((form) => {
      if (!(form instanceof HTMLFormElement)) {
        return;
      }
      const panel = form.querySelector("[data-time-prompts]");
      const list = form.querySelector("[data-time-prompts-list]");
      if (!(panel instanceof HTMLElement) || !(list instanceof HTMLElement)) {
        return;
      }

      const endpoint = form.getAttribute("data-time-prompt-endpoint") || "/time/prompts";
      let debounceHandle = 0;
      let sequence = 0;

      const readField = (name) => {
        const control = form.querySelector(`[name='${name}']`);
        if (!control) {
          return { exists: false, value: "" };
        }
        if (control instanceof HTMLInputElement && control.type === "checkbox") {
          return { exists: true, value: control.checked ? "1" : "0" };
        }
        if (
          control instanceof HTMLInputElement ||
          control instanceof HTMLTextAreaElement ||
          control instanceof HTMLSelectElement
        ) {
          return { exists: true, value: control.value.trim() };
        }
        return { exists: true, value: "" };
      };

      const loadPrompts = async () => {
        const matter = readField("matter_id");
        if (!matter.exists || !matter.value) {
          renderPrompts(panel, list, []);
          return;
        }

        const params = new URLSearchParams();
        params.set("matter_id", matter.value);

        ["start_at", "end_at", "narrative", "activity_code", "task_code", "is_billable"].forEach((name) => {
          const { exists, value } = readField(name);
          if (!exists) {
            return;
          }
          params.set(name, value);
        });

        sequence += 1;
        const localSequence = sequence;
        try {
          const response = await fetch(`${endpoint}?${params.toString()}`, {
            method: "GET",
            headers: { Accept: "application/json" },
          });
          if (localSequence !== sequence) {
            return;
          }
          const payload = await response.json().catch(() => ({}));
          if (!response.ok) {
            const error = String(payload.error || "Unable to load prompts.");
            renderPrompts(panel, list, [{ level: "warning", message: error }]);
            return;
          }
          renderPrompts(panel, list, payload.prompts || []);
        } catch (_error) {
          renderPrompts(panel, list, [{ level: "warning", message: "Prompt service unavailable." }]);
        }
      };

      const schedule = () => {
        if (debounceHandle) {
          window.clearTimeout(debounceHandle);
        }
        debounceHandle = window.setTimeout(loadPrompts, 240);
      };

      const controls = Array.from(form.querySelectorAll("input, textarea, select"));
      controls.forEach((control) => {
        const name = control.getAttribute("name") || "";
        if (!["matter_id", "start_at", "end_at", "narrative", "activity_code", "task_code", "is_billable"].includes(name)) {
          return;
        }
        const eventName = control instanceof HTMLInputElement && control.type === "checkbox" ? "change" : "input";
        control.addEventListener(eventName, schedule);
        if (eventName !== "change") {
          control.addEventListener("change", schedule);
        }
      });

      loadPrompts();
    });
  };

  const describeAIFallbackReason = (reasonCode) => {
    const code = String(reasonCode || "").trim().toLowerCase();
    if (code === "ai_disabled") {
      return "AI is disabled in server configuration";
    }
    if (code === "missing_api_key") {
      return "OpenAI API key is not configured";
    }
    if (code === "unsupported_provider") {
      return "Configured AI provider is unsupported for this draft flow";
    }
    if (code === "openai_error") {
      return "OpenAI request failed";
    }
    if (code) {
      return code.replace(/_/g, " ");
    }
    return "fallback reason unavailable";
  };

  const fillDraftControl = (control, value) => {
    if (
      !(
        control instanceof HTMLInputElement ||
        control instanceof HTMLTextAreaElement ||
        control instanceof HTMLSelectElement
      )
    ) {
      return;
    }
    control.value = String(value || "");
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const fillDraftCheckbox = (control, value) => {
    if (!(control instanceof HTMLInputElement) || control.type !== "checkbox") {
      return;
    }
    control.checked = Boolean(value);
    control.dispatchEvent(new Event("change", { bubbles: true }));
  };

  const initArchetypeAIDraftGenerator = () => {
    const root = document.querySelector("[data-archetype-ai-widget]");
    if (!(root instanceof HTMLElement)) {
      return;
    }

    const promptInput = root.querySelector("[data-ai-archetype-prompt]");
    const generateButton = root.querySelector("[data-ai-archetype-generate]");
    const statusEl = root.querySelector("[data-ai-archetype-status]");
    const csrfInput = document.querySelector("form input[name='csrf_token']");
    const endpoint = String(root.dataset.endpoint || "").trim();
    const timeoutMs = Math.max(15000, Number.parseInt(String(root.dataset.timeoutMs || "90000"), 10) || 90000);
    if (
      !(promptInput instanceof HTMLTextAreaElement) ||
      !(generateButton instanceof HTMLButtonElement) ||
      !(statusEl instanceof HTMLElement) ||
      !(csrfInput instanceof HTMLInputElement) ||
      !endpoint
    ) {
      return;
    }

    const controls = {
      name: document.getElementById("matter-template-name"),
      legalCategory: document.getElementById("matter-template-category"),
      practiceArea: document.getElementById("matter-template-area"),
      defaultStage: document.getElementById("matter-template-stage"),
      defaultRiskLevel: document.getElementById("matter-template-risk"),
      checklist: document.getElementById("matter-template-checklist"),
      requiredFields: document.getElementById("matter-template-required-fields"),
      boilerplateTemplate: document.getElementById("matter-template-boilerplate"),
    };

    const fieldLine = (field) => {
      const key = String((field && field.key) || "").trim();
      const label = String((field && field.label) || "").trim();
      const helpText = String((field && field.help) || "").trim();
      if (!key) {
        return "";
      }
      if (helpText) {
        return `${key}|${label || key}|${helpText}`;
      }
      if (label) {
        return `${key}|${label}`;
      }
      return key;
    };

    const setStatus = (text, tone = "muted") => {
      statusEl.className = `small ${tone}`;
      statusEl.textContent = text;
    };

    const defaultButtonText = String(generateButton.textContent || "Generate Draft with AI").trim();
    let requestInFlight = false;

    generateButton.addEventListener("click", async () => {
      if (requestInFlight) {
        return;
      }
      const prompt = String(promptInput.value || "").trim();
      if (prompt.length < 20) {
        setStatus("Provide at least 20 characters in the prompt.", "text-warning");
        promptInput.focus();
        return;
      }

      requestInFlight = true;
      generateButton.disabled = true;
      generateButton.setAttribute("aria-busy", "true");

      const startedAtMs = Date.now();
      let elapsedSeconds = 0;
      const renderProgress = () => {
        const phasePrefix = elapsedSeconds < 8 ? "Generating archetype draft..." : "Still generating draft...";
        generateButton.textContent = `${phasePrefix} ${elapsedSeconds}s`;
        setStatus(`${phasePrefix} ${elapsedSeconds}s elapsed.`, "text-muted");
      };
      renderProgress();
      const progressHandle = window.setInterval(() => {
        elapsedSeconds = Math.floor((Date.now() - startedAtMs) / 1000);
        renderProgress();
      }, 1000);

      const controller = new AbortController();
      const timeoutHandle = window.setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrfInput.value,
          },
          body: JSON.stringify({
            prompt,
            legal_category_hint:
              controls.legalCategory instanceof HTMLInputElement || controls.legalCategory instanceof HTMLSelectElement
                ? controls.legalCategory.value
                : "",
            name_hint: controls.name instanceof HTMLInputElement ? controls.name.value : "",
          }),
          signal: controller.signal,
        });
        const contentType = String(response.headers.get("content-type") || "").toLowerCase();
        const payload = contentType.includes("application/json") ? await response.json().catch(() => ({})) : {};
        if (!response.ok || payload.ok !== true) {
          let errorMessage = String(payload.error || "").trim();
          if (!errorMessage) {
            if (response.status === 400) {
              errorMessage = "Request rejected. Check prompt length and refresh the page to renew CSRF/session.";
            } else if (response.status === 401 || response.status === 403 || response.redirected) {
              errorMessage = "Session or permissions invalid. Sign in as admin and try again.";
            } else if (response.status >= 500) {
              errorMessage = "Server error while generating draft. Check AI configuration and retry.";
            } else {
              errorMessage = "AI draft failed.";
            }
          }
          setStatus(errorMessage, "text-warning");
          return;
        }

        const suggestion = payload.suggestion || {};
        fillDraftControl(controls.name, suggestion.name);
        fillDraftControl(controls.legalCategory, suggestion.legal_category);
        fillDraftControl(controls.practiceArea, suggestion.practice_area);
        fillDraftControl(controls.defaultStage, suggestion.default_stage);
        fillDraftControl(controls.defaultRiskLevel, suggestion.default_risk_level);
        fillDraftControl(controls.checklist, Array.isArray(suggestion.checklist) ? suggestion.checklist.join("\n") : "");
        fillDraftControl(
          controls.requiredFields,
          Array.isArray(suggestion.required_fields) ? suggestion.required_fields.map(fieldLine).filter(Boolean).join("\n") : ""
        );
        fillDraftControl(controls.boilerplateTemplate, suggestion.boilerplate_template || "");

        const source = String(suggestion.source || "fallback").trim() || "fallback";
        const elapsedMs = Math.max(0, Number.parseInt(String(payload.elapsed_ms || ""), 10) || Date.now() - startedAtMs);
        const elapsedText = `${Math.max(1, Math.round(elapsedMs / 1000))}s`;
        if (source === "fallback") {
          const reasonCode = String(suggestion.fallback_reason || payload.fallback_reason || "").trim();
          const reasonText = describeAIFallbackReason(reasonCode);
          const rawDetail = String(suggestion.fallback_detail || payload.fallback_detail || "").trim();
          const detail = rawDetail && rawDetail.length <= 180 ? rawDetail : "";
          const reasonMessage = detail ? `${reasonText}: ${detail}` : reasonText;
          setStatus(`Archetype draft generated (fallback) in ${elapsedText}. Reason: ${reasonMessage}. Review and save.`, "text-warning");
          return;
        }
        setStatus(`Archetype draft generated (${source}) in ${elapsedText}. Review and save.`, "text-success");
      } catch (error) {
        if (error && typeof error === "object" && String(error.name || "") === "AbortError") {
          setStatus("AI draft timed out. Check AI connectivity/configuration and try again.", "text-warning");
        } else {
          setStatus("AI draft service is unavailable right now. Please retry.", "text-warning");
        }
      } finally {
        window.clearTimeout(timeoutHandle);
        window.clearInterval(progressHandle);
        generateButton.removeAttribute("aria-busy");
        generateButton.disabled = false;
        generateButton.textContent = defaultButtonText;
        requestInFlight = false;
      }
    });
  };

  const initContractAIDraftGenerator = () => {
    const root = document.querySelector("[data-contract-ai-widget]");
    if (!(root instanceof HTMLElement)) {
      return;
    }

    const promptInput = root.querySelector("[data-ai-contract-prompt]");
    const generateButton = root.querySelector("[data-ai-contract-generate]");
    const statusEl = root.querySelector("[data-ai-contract-status]");
    const csrfInput = document.querySelector("form input[name='csrf_token']");
    const endpoint = String(root.dataset.endpoint || "").trim();
    const timeoutMs = Math.max(15000, Number.parseInt(String(root.dataset.timeoutMs || "90000"), 10) || 90000);
    if (
      !(promptInput instanceof HTMLTextAreaElement) ||
      !(generateButton instanceof HTMLButtonElement) ||
      !(statusEl instanceof HTMLElement) ||
      !(csrfInput instanceof HTMLInputElement) ||
      !endpoint
    ) {
      return;
    }

    const controls = {
      name: document.getElementById("contract-template-name"),
      legalCategory: document.getElementById("contract-template-category"),
      contractType: document.getElementById("contract-template-type"),
      requiredFields: document.getElementById("contract-template-required-fields"),
      body: document.getElementById("contract-template-body"),
      requiresSignature: document.getElementById("contract-template-signature"),
      autoCreateOnMatterOpen: document.getElementById("contract-template-auto"),
      isActive: document.getElementById("contract-template-active"),
    };

    const fieldLine = (field) => {
      const key = String((field && field.key) || "").trim();
      const label = String((field && field.label) || "").trim();
      const helpText = String((field && field.help) || "").trim();
      if (!key) {
        return "";
      }
      if (helpText) {
        return `${key}|${label || key}|${helpText}`;
      }
      if (label) {
        return `${key}|${label}`;
      }
      return key;
    };

    const setStatus = (text, tone = "muted") => {
      statusEl.className = `small ${tone}`;
      statusEl.textContent = text;
    };

    const defaultButtonText = String(generateButton.textContent || "Generate Draft with AI").trim();
    let requestInFlight = false;

    generateButton.addEventListener("click", async () => {
      if (requestInFlight) {
        return;
      }
      const prompt = String(promptInput.value || "").trim();
      if (prompt.length < 20) {
        setStatus("Provide at least 20 characters in the prompt.", "text-warning");
        promptInput.focus();
        return;
      }

      requestInFlight = true;
      generateButton.disabled = true;
      generateButton.setAttribute("aria-busy", "true");

      const startedAtMs = Date.now();
      let elapsedSeconds = 0;
      const renderProgress = () => {
        const phasePrefix = elapsedSeconds < 8 ? "Generating contract draft..." : "Still generating draft...";
        generateButton.textContent = `${phasePrefix} ${elapsedSeconds}s`;
        setStatus(`${phasePrefix} ${elapsedSeconds}s elapsed.`, "text-muted");
      };
      renderProgress();
      const progressHandle = window.setInterval(() => {
        elapsedSeconds = Math.floor((Date.now() - startedAtMs) / 1000);
        renderProgress();
      }, 1000);

      const controller = new AbortController();
      const timeoutHandle = window.setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrfInput.value,
          },
          body: JSON.stringify({
            prompt,
            legal_category_hint:
              controls.legalCategory instanceof HTMLInputElement || controls.legalCategory instanceof HTMLSelectElement
                ? controls.legalCategory.value
                : "",
            name_hint: controls.name instanceof HTMLInputElement ? controls.name.value : "",
            contract_type_hint: controls.contractType instanceof HTMLInputElement ? controls.contractType.value : "",
          }),
          signal: controller.signal,
        });
        const contentType = String(response.headers.get("content-type") || "").toLowerCase();
        const payload = contentType.includes("application/json") ? await response.json().catch(() => ({})) : {};
        if (!response.ok || payload.ok !== true) {
          let errorMessage = String(payload.error || "").trim();
          if (!errorMessage) {
            if (response.status === 400) {
              errorMessage = "Request rejected. Check prompt length and refresh the page to renew CSRF/session.";
            } else if (response.status === 401 || response.status === 403 || response.redirected) {
              errorMessage = "Session or permissions invalid. Sign in as admin and try again.";
            } else if (response.status >= 500) {
              errorMessage = "Server error while generating draft. Check AI configuration and retry.";
            } else {
              errorMessage = "AI draft failed.";
            }
          }
          setStatus(errorMessage, "text-warning");
          return;
        }

        const suggestion = payload.suggestion || {};
        fillDraftControl(controls.name, suggestion.name);
        fillDraftControl(controls.legalCategory, suggestion.legal_category);
        fillDraftControl(controls.contractType, suggestion.contract_type);
        fillDraftControl(
          controls.requiredFields,
          Array.isArray(suggestion.required_fields) ? suggestion.required_fields.map(fieldLine).filter(Boolean).join("\n") : ""
        );
        fillDraftControl(controls.body, suggestion.body || "");
        fillDraftCheckbox(controls.requiresSignature, suggestion.requires_signature);
        fillDraftCheckbox(controls.autoCreateOnMatterOpen, suggestion.auto_create_on_matter_open);
        fillDraftCheckbox(controls.isActive, suggestion.is_active);

        const source = String(suggestion.source || "fallback").trim() || "fallback";
        const elapsedMs = Math.max(0, Number.parseInt(String(payload.elapsed_ms || ""), 10) || Date.now() - startedAtMs);
        const elapsedText = `${Math.max(1, Math.round(elapsedMs / 1000))}s`;
        if (source === "fallback") {
          const reasonCode = String(suggestion.fallback_reason || payload.fallback_reason || "").trim();
          const reasonText = describeAIFallbackReason(reasonCode);
          const rawDetail = String(suggestion.fallback_detail || payload.fallback_detail || "").trim();
          const detail = rawDetail && rawDetail.length <= 180 ? rawDetail : "";
          const reasonMessage = detail ? `${reasonText}: ${detail}` : reasonText;
          setStatus(`Contract draft generated (fallback) in ${elapsedText}. Reason: ${reasonMessage}. Review and save.`, "text-warning");
          return;
        }
        setStatus(`Contract draft generated (${source}) in ${elapsedText}. Review and save.`, "text-success");
      } catch (error) {
        if (error && typeof error === "object" && String(error.name || "") === "AbortError") {
          setStatus("AI draft timed out. Check AI connectivity/configuration and try again.", "text-warning");
        } else {
          setStatus("AI draft service is unavailable right now. Please retry.", "text-warning");
        }
      } finally {
        window.clearTimeout(timeoutHandle);
        window.clearInterval(progressHandle);
        generateButton.removeAttribute("aria-busy");
        generateButton.disabled = false;
        generateButton.textContent = defaultButtonText;
        requestInFlight = false;
      }
    });
  };

  const initDocumentAIDraftGenerator = () => {
    const root = document.querySelector("[data-document-ai-widget]");
    if (!(root instanceof HTMLElement)) {
      return;
    }

    const promptInput = root.querySelector("[data-ai-document-prompt]");
    const generateButton = root.querySelector("[data-ai-document-generate]");
    const statusEl = root.querySelector("[data-ai-document-status]");
    const csrfInput = document.querySelector("form input[name='csrf_token']");
    const endpoint = String(root.dataset.endpoint || "").trim();
    const timeoutMs = Math.max(15000, Number.parseInt(String(root.dataset.timeoutMs || "90000"), 10) || 90000);
    if (
      !(promptInput instanceof HTMLTextAreaElement) ||
      !(generateButton instanceof HTMLButtonElement) ||
      !(statusEl instanceof HTMLElement) ||
      !(csrfInput instanceof HTMLInputElement) ||
      !endpoint
    ) {
      return;
    }

    const controls = {
      name: document.getElementById("doc-template-name"),
      templateType: document.getElementById("doc-template-type"),
      body: document.getElementById("doc-template-body"),
      requiresSignature: document.getElementById("requires_signature"),
    };

    const setStatus = (text, tone = "muted") => {
      statusEl.className = `small ${tone}`;
      statusEl.textContent = text;
    };

    const defaultButtonText = String(generateButton.textContent || "Generate Draft with AI").trim();
    let requestInFlight = false;

    generateButton.addEventListener("click", async () => {
      if (requestInFlight) {
        return;
      }
      const prompt = String(promptInput.value || "").trim();
      if (prompt.length < 20) {
        setStatus("Provide at least 20 characters in the prompt.", "text-warning");
        promptInput.focus();
        return;
      }

      requestInFlight = true;
      generateButton.disabled = true;
      generateButton.setAttribute("aria-busy", "true");

      const startedAtMs = Date.now();
      let elapsedSeconds = 0;
      const renderProgress = () => {
        const phasePrefix = elapsedSeconds < 8 ? "Generating document draft..." : "Still generating draft...";
        generateButton.textContent = `${phasePrefix} ${elapsedSeconds}s`;
        setStatus(`${phasePrefix} ${elapsedSeconds}s elapsed.`, "text-muted");
      };
      renderProgress();
      const progressHandle = window.setInterval(() => {
        elapsedSeconds = Math.floor((Date.now() - startedAtMs) / 1000);
        renderProgress();
      }, 1000);

      const controller = new AbortController();
      const timeoutHandle = window.setTimeout(() => controller.abort(), timeoutMs);

      try {
        const response = await fetch(endpoint, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrfInput.value,
          },
          body: JSON.stringify({
            prompt,
            name_hint: controls.name instanceof HTMLInputElement ? controls.name.value : "",
            template_type_hint:
              controls.templateType instanceof HTMLInputElement || controls.templateType instanceof HTMLSelectElement
                ? controls.templateType.value
                : "",
          }),
          signal: controller.signal,
        });
        const contentType = String(response.headers.get("content-type") || "").toLowerCase();
        const payload = contentType.includes("application/json") ? await response.json().catch(() => ({})) : {};
        if (!response.ok || payload.ok !== true) {
          let errorMessage = String(payload.error || "").trim();
          if (!errorMessage) {
            if (response.status === 400) {
              errorMessage = "Request rejected. Check prompt length and refresh the page to renew CSRF/session.";
            } else if (response.status === 401 || response.status === 403 || response.redirected) {
              errorMessage = "Session or permissions invalid. Sign in as admin and try again.";
            } else if (response.status >= 500) {
              errorMessage = "Server error while generating draft. Check AI configuration and retry.";
            } else {
              errorMessage = "AI draft failed.";
            }
          }
          setStatus(errorMessage, "text-warning");
          return;
        }

        const suggestion = payload.suggestion || {};
        fillDraftControl(controls.name, suggestion.name);
        fillDraftControl(controls.templateType, suggestion.template_type);
        fillDraftControl(controls.body, suggestion.body || "");
        fillDraftCheckbox(controls.requiresSignature, suggestion.requires_signature);

        const source = String(suggestion.source || "fallback").trim() || "fallback";
        const elapsedMs = Math.max(0, Number.parseInt(String(payload.elapsed_ms || ""), 10) || Date.now() - startedAtMs);
        const elapsedText = `${Math.max(1, Math.round(elapsedMs / 1000))}s`;
        if (source === "fallback") {
          const reasonCode = String(suggestion.fallback_reason || payload.fallback_reason || "").trim();
          const reasonText = describeAIFallbackReason(reasonCode);
          const rawDetail = String(suggestion.fallback_detail || payload.fallback_detail || "").trim();
          const detail = rawDetail && rawDetail.length <= 180 ? rawDetail : "";
          const reasonMessage = detail ? `${reasonText}: ${detail}` : reasonText;
          setStatus(`Document draft generated (fallback) in ${elapsedText}. Reason: ${reasonMessage}. Review and save.`, "text-warning");
          return;
        }
        setStatus(`Document draft generated (${source}) in ${elapsedText}. Review and save.`, "text-success");
      } catch (error) {
        if (error && typeof error === "object" && String(error.name || "") === "AbortError") {
          setStatus("AI draft timed out. Check AI connectivity/configuration and try again.", "text-warning");
        } else {
          setStatus("AI draft service is unavailable right now. Please retry.", "text-warning");
        }
      } finally {
        window.clearTimeout(timeoutHandle);
        window.clearInterval(progressHandle);
        generateButton.removeAttribute("aria-busy");
        generateButton.disabled = false;
        generateButton.textContent = defaultButtonText;
        requestInFlight = false;
      }
    });
  };

  const initTimeCodeAssist = () => {
    const forms = Array.from(document.querySelectorAll("form[data-time-code-assist]"));
    if (forms.length === 0) {
      return;
    }

    const normalize = (value) => String(value || "").trim();
    const uniqueStrings = (values, limit = 12) => {
      const seen = new Set();
      const output = [];
      values.forEach((value) => {
        const candidate = normalize(value);
        if (!candidate || seen.has(candidate)) {
          return;
        }
        seen.add(candidate);
        output.push(candidate);
      });
      return output.slice(0, Math.max(1, limit));
    };
    const toCodes = (bucket, key) => {
      if (!bucket || typeof bucket !== "object" || !Array.isArray(bucket[key])) {
        return [];
      }
      return bucket[key];
    };
    const toPairs = (bucket) => {
      if (!bucket || typeof bucket !== "object" || !Array.isArray(bucket.pairs)) {
        return [];
      }
      return bucket.pairs
        .map((pair) => ({
          task_code: normalize(pair && pair.task_code),
          activity_code: normalize(pair && pair.activity_code),
          label: normalize(pair && pair.label),
        }))
        .filter((pair) => pair.task_code || pair.activity_code);
    };
    const pairKey = (taskCode, activityCode) => `${normalize(taskCode)}\u241f${normalize(activityCode)}`;
    const dispatchFieldUpdate = (field) => {
      field.dispatchEvent(new Event("input", { bubbles: true }));
      field.dispatchEvent(new Event("change", { bubbles: true }));
    };

    forms.forEach((form) => {
      if (!(form instanceof HTMLFormElement)) {
        return;
      }

      const rawPayload = form.getAttribute("data-time-code-assist") || "";
      if (!rawPayload) {
        return;
      }

      let payload = {};
      try {
        payload = JSON.parse(rawPayload);
      } catch (_error) {
        return;
      }

      const globalBucket =
        payload && typeof payload === "object" && payload.global && typeof payload.global === "object"
          ? payload.global
          : {};
      const byMatter =
        payload && typeof payload === "object" && payload.by_matter && typeof payload.by_matter === "object"
          ? payload.by_matter
          : {};

      const matterSelect = form.querySelector("[name='matter_id']");
      const taskInput = form.querySelector("[name='task_code']");
      const activityInput = form.querySelector("[name='activity_code']");
      const pairSelect = form.querySelector("[data-time-code-pair]");
      if (
        !(matterSelect instanceof HTMLSelectElement) ||
        !(taskInput instanceof HTMLInputElement) ||
        !(activityInput instanceof HTMLInputElement)
      ) {
        return;
      }

      const taskListId = taskInput.getAttribute("list") || "";
      const activityListId = activityInput.getAttribute("list") || "";
      const taskDatalist = taskListId ? document.getElementById(taskListId) : null;
      const activityDatalist = activityListId ? document.getElementById(activityListId) : null;

      const renderDatalist = (target, values) => {
        if (!(target instanceof HTMLDataListElement)) {
          return;
        }
        target.innerHTML = "";
        values.forEach((value) => {
          const option = document.createElement("option");
          option.value = value;
          target.appendChild(option);
        });
      };

      const resolveMatterBucket = () => {
        const matterKey = normalize(matterSelect.value);
        if (!matterKey) {
          return null;
        }
        const bucket = byMatter[matterKey];
        if (!bucket || typeof bucket !== "object") {
          return null;
        }
        return bucket;
      };

      const pairLookup = new Map();

      const applyPair = (pair) => {
        const taskCode = normalize(pair && pair.task_code);
        const activityCode = normalize(pair && pair.activity_code);

        if (taskInput.value !== taskCode) {
          taskInput.value = taskCode;
          dispatchFieldUpdate(taskInput);
        }
        if (activityInput.value !== activityCode) {
          activityInput.value = activityCode;
          dispatchFieldUpdate(activityInput);
        }
      };

      const refresh = () => {
        const matterBucket = resolveMatterBucket();
        const taskCodes = uniqueStrings([...toCodes(matterBucket, "task_codes"), ...toCodes(globalBucket, "task_codes")], 12);
        const activityCodes = uniqueStrings(
          [...toCodes(matterBucket, "activity_codes"), ...toCodes(globalBucket, "activity_codes")],
          12
        );
        renderDatalist(taskDatalist, taskCodes);
        renderDatalist(activityDatalist, activityCodes);

        if (!(pairSelect instanceof HTMLSelectElement)) {
          return;
        }

        pairLookup.clear();
        pairSelect.innerHTML = "";
        const placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "Select recent pair...";
        pairSelect.appendChild(placeholder);

        const candidatePairs = [];
        const latestPair =
          matterBucket && matterBucket.latest_pair && typeof matterBucket.latest_pair === "object"
            ? {
                task_code: normalize(matterBucket.latest_pair.task_code),
                activity_code: normalize(matterBucket.latest_pair.activity_code),
                label: normalize(matterBucket.latest_pair.label),
              }
            : null;
        if (latestPair && (latestPair.task_code || latestPair.activity_code)) {
          candidatePairs.push(latestPair);
        }
        candidatePairs.push(...toPairs(matterBucket), ...toPairs(globalBucket));

        candidatePairs.forEach((pair) => {
          const taskCode = normalize(pair.task_code);
          const activityCode = normalize(pair.activity_code);
          if (!taskCode && !activityCode) {
            return;
          }
          const key = pairKey(taskCode, activityCode);
          if (pairLookup.has(key)) {
            return;
          }
          const option = document.createElement("option");
          option.value = key;
          option.textContent = normalize(pair.label) || [taskCode, activityCode].filter(Boolean).join(" / ");
          pairSelect.appendChild(option);
          pairLookup.set(key, {
            task_code: taskCode,
            activity_code: activityCode,
          });
        });

        pairSelect.value = "";
        pairSelect.disabled = pairLookup.size === 0;
      };

      matterSelect.addEventListener("change", refresh);
      if (pairSelect instanceof HTMLSelectElement) {
        pairSelect.addEventListener("change", () => {
          const key = normalize(pairSelect.value);
          if (!key) {
            return;
          }
          const pair = pairLookup.get(key);
          if (!pair) {
            return;
          }
          applyPair(pair);
        });
      }

      refresh();
    });
  };

  const initTimerPresenceGuard = () => {
    const root = document.querySelector("[data-timer-presence-root]");
    if (!(root instanceof HTMLElement)) {
      return;
    }

    const warning = root.querySelector("[data-timer-idle-warning]");
    const message = root.querySelector("[data-timer-idle-message]");
    const keepRunning = root.querySelector("[data-timer-still-here]");
    const pauseNow = root.querySelector("[data-timer-pause-now]");
    const autoPauseForm = root.querySelector("[data-timer-auto-pause-form]");
    if (!(autoPauseForm instanceof HTMLFormElement)) {
      return;
    }

    const parsePositiveInt = (value, fallback) => {
      const parsed = Number.parseInt(String(value || ""), 10);
      if (!Number.isFinite(parsed) || parsed <= 0) {
        return fallback;
      }
      return parsed;
    };

    const idlePromptSeconds = parsePositiveInt(root.dataset.idlePromptSeconds, 45 * 60);
    const idleGraceSeconds = parsePositiveInt(root.dataset.idleGraceSeconds, 60);
    let lastActivityAt = Date.now();
    let warningShown = false;
    let countdown = idleGraceSeconds;
    let submitted = false;

    const renderCountdown = () => {
      if (!(message instanceof HTMLElement)) {
        return;
      }
      message.textContent = `No activity detected. Auto-pausing in ${countdown}s unless you confirm you're still working.`;
    };

    const hideWarning = () => {
      warningShown = false;
      countdown = idleGraceSeconds;
      if (warning instanceof HTMLElement) {
        warning.hidden = true;
      }
    };

    const showWarning = () => {
      warningShown = true;
      countdown = idleGraceSeconds;
      if (warning instanceof HTMLElement) {
        warning.hidden = false;
      }
      renderCountdown();
    };

    const markActivity = () => {
      if (submitted) {
        return;
      }
      lastActivityAt = Date.now();
      if (warningShown) {
        hideWarning();
      }
    };

    const submitPause = (reason) => {
      if (submitted) {
        return;
      }
      submitted = true;

      let reasonInput = autoPauseForm.querySelector("input[name='pause_reason']");
      if (!(reasonInput instanceof HTMLInputElement)) {
        reasonInput = document.createElement("input");
        reasonInput.type = "hidden";
        reasonInput.name = "pause_reason";
        autoPauseForm.appendChild(reasonInput);
      }
      reasonInput.value = reason;
      autoPauseForm.submit();
    };

    if (keepRunning instanceof HTMLButtonElement) {
      keepRunning.addEventListener("click", () => {
        markActivity();
      });
    }
    if (pauseNow instanceof HTMLButtonElement) {
      pauseNow.addEventListener("click", () => {
        submitPause("idle_timeout");
      });
    }

    ["mousedown", "mousemove", "touchstart", "scroll"].forEach((eventName) => {
      window.addEventListener(eventName, markActivity, { passive: true });
    });
    window.addEventListener("keydown", markActivity);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) {
        markActivity();
      }
    });

    window.setInterval(() => {
      if (submitted) {
        return;
      }
      const idleSeconds = Math.floor((Date.now() - lastActivityAt) / 1000);
      if (!warningShown && idleSeconds >= idlePromptSeconds) {
        showWarning();
        return;
      }
      if (!warningShown) {
        return;
      }

      countdown = Math.max(0, countdown - 1);
      renderCountdown();
      if (countdown === 0) {
        submitPause("idle_timeout");
      }
    }, 1000);
  };

  const initLiveBillingCue = () => {
    const root = document.querySelector("[data-live-timer-root]");
    if (!(root instanceof HTMLElement)) {
      return;
    }

    const elapsedNode = root.querySelector("[data-live-timer-elapsed]");
    if (!(elapsedNode instanceof HTMLElement)) {
      return;
    }

    const parseNonNegativeInt = (value, fallback = 0) => {
      const parsed = Number.parseInt(String(value || ""), 10);
      if (!Number.isFinite(parsed) || parsed < 0) {
        return fallback;
      }
      return parsed;
    };

    const elapsedSeedSeconds = parseNonNegativeInt(root.dataset.elapsedSeedSeconds, 0);
    const startedAtRaw = String(root.dataset.startedAt || "").trim();
    const startedAtMs = startedAtRaw ? Date.parse(startedAtRaw) : Number.NaN;

    const formatElapsed = (totalSeconds) => {
      const safeTotal = Math.max(0, parseNonNegativeInt(totalSeconds, 0));
      const hours = Math.floor(safeTotal / 3600);
      const minutes = Math.floor((safeTotal % 3600) / 60);
      const seconds = safeTotal % 60;
      const pad = (value) => String(value).padStart(2, "0");
      return `${pad(hours)}:${pad(minutes)}:${pad(seconds)}`;
    };

    const render = () => {
      let elapsed = elapsedSeedSeconds;
      if (Number.isFinite(startedAtMs)) {
        elapsed += Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
      }
      elapsedNode.textContent = formatElapsed(elapsed);
    };

    render();
    window.setInterval(render, 1000);
  };

  const initQuoteAssist = () => {
    const forms = Array.from(document.querySelectorAll("[data-quote-form]"));
    if (forms.length === 0) {
      return;
    }

    const toNumber = (input) => {
      if (!(input instanceof HTMLInputElement)) {
        return null;
      }
      const raw = input.value.trim();
      if (!raw) {
        return null;
      }
      const parsed = Number(raw);
      return Number.isFinite(parsed) ? parsed : null;
    };

    const formatAmount = (value) => (Number.isFinite(value) ? value.toFixed(2) : "0.00");
    const feeHints = {
      fixed: "Set a single total fee for the scoped work.",
      hourly: "Estimated fee can be auto-calculated from hours × rate.",
      capped: "Set a ceiling amount and capture assumptions clearly.",
    };

    forms.forEach((form) => {
      if (!(form instanceof HTMLFormElement)) {
        return;
      }

      const feeModelField = form.querySelector("[data-quote-field='fee-model']");
      const estimatedAmountField = form.querySelector("[data-quote-field='estimated-amount']");
      const hoursField = form.querySelector("[data-quote-field='hours']");
      const rateField = form.querySelector("[data-quote-field='rate']");
      const disbursementField = form.querySelector("[data-quote-field='disbursements']");
      const taxRateField = form.querySelector("[data-quote-field='tax-rate']");
      const feeHint = form.querySelector("[data-quote-fee-hint]");
      const summaryBase = form.querySelector("[data-quote-summary-base]");
      const summaryDisbursements = form.querySelector("[data-quote-summary-disbursements]");
      const summaryTax = form.querySelector("[data-quote-summary-tax]");
      const summaryGrandTotal = form.querySelector("[data-quote-summary-grand-total]");

      const updateSummary = () => {
        const manualBase = toNumber(estimatedAmountField);
        const hours = toNumber(hoursField);
        const rate = toNumber(rateField);
        let base = manualBase !== null ? manualBase : 0.0;
        if (manualBase === null && hours !== null && rate !== null) {
          base = hours * rate;
        }
        const disbursements = Math.max(0, toNumber(disbursementField) || 0);
        const taxRate = Math.max(0, toNumber(taxRateField) || 0);
        const subtotal = Math.max(0, base) + disbursements;
        const taxAmount = subtotal * (taxRate / 100);
        const grandTotal = subtotal + taxAmount;

        if (summaryBase instanceof HTMLElement) {
          summaryBase.textContent = formatAmount(Math.max(0, base));
        }
        if (summaryDisbursements instanceof HTMLElement) {
          summaryDisbursements.textContent = formatAmount(disbursements);
        }
        if (summaryTax instanceof HTMLElement) {
          summaryTax.textContent = formatAmount(taxAmount);
        }
        if (summaryGrandTotal instanceof HTMLElement) {
          summaryGrandTotal.textContent = formatAmount(grandTotal);
        }
      };

      const applyFeeHint = () => {
        if (!(feeHint instanceof HTMLElement) || !(feeModelField instanceof HTMLSelectElement)) {
          return;
        }
        const model = feeModelField.value.trim().toLowerCase();
        feeHint.textContent = feeHints[model] || "Set a proposal structure that matches your pricing strategy.";
      };

      const maybeAutofillEstimate = () => {
        if (!(estimatedAmountField instanceof HTMLInputElement)) {
          return;
        }
        if (estimatedAmountField.value.trim().length > 0) {
          return;
        }
        if (!(feeModelField instanceof HTMLSelectElement)) {
          return;
        }
        if (feeModelField.value.trim().toLowerCase() !== "hourly") {
          return;
        }
        const hours = toNumber(hoursField);
        const rate = toNumber(rateField);
        if (hours === null || rate === null) {
          return;
        }
        estimatedAmountField.value = formatAmount(Math.max(0, hours * rate));
      };

      const refresh = () => {
        applyFeeHint();
        maybeAutofillEstimate();
        updateSummary();
      };

      [
        feeModelField,
        estimatedAmountField,
        hoursField,
        rateField,
        disbursementField,
        taxRateField,
      ].forEach((field) => {
        if (
          field instanceof HTMLInputElement ||
          field instanceof HTMLTextAreaElement ||
          field instanceof HTMLSelectElement
        ) {
          field.addEventListener("input", refresh);
          field.addEventListener("change", refresh);
        }
      });

      refresh();
    });
  };

  const initPortalMessageComposer = () => {
    const matterSelect = document.getElementById("portal-message-matter");
    const threadSelect = document.getElementById("portal-message-thread");
    const subjectInput = document.getElementById("portal-message-subject");
    const hint = document.querySelector("[data-thread-matter-hint]");
    if (!(matterSelect instanceof HTMLSelectElement) || !(threadSelect instanceof HTMLSelectElement)) {
      return;
    }

    const threadOptions = Array.from(threadSelect.options);

    const setHint = (text) => {
      if (hint instanceof HTMLElement) {
        hint.textContent = text;
      }
    };

    const setSubjectState = () => {
      const hasSelectedThread = threadSelect.value.trim().length > 0;
      if (subjectInput instanceof HTMLInputElement) {
        subjectInput.disabled = hasSelectedThread;
        subjectInput.placeholder = hasSelectedThread ? "Subject locked to selected thread" : "Subject";
      }
      if (hasSelectedThread) {
        setHint("Replying in selected thread. Subject is locked.");
      }
    };

    const filterThreadsByMatter = () => {
      const selectedMatterId = matterSelect.value.trim();
      let visibleCount = 0;

      threadOptions.forEach((option, index) => {
        if (index === 0) {
          option.hidden = false;
          return;
        }
        const optionMatterId = option.getAttribute("data-matter-id") || "";
        const matchesMatter = !selectedMatterId || !optionMatterId || optionMatterId === selectedMatterId;
        option.hidden = !matchesMatter;
        if (matchesMatter) {
          visibleCount += 1;
        }
      });

      const selectedOption = threadSelect.selectedOptions.item(0);
      if (selectedOption && selectedOption.hidden) {
        threadSelect.value = "";
      }

      if (threadSelect.value.trim().length > 0) {
        setHint("Replying in selected thread. Subject is locked.");
        return;
      }
      if (!selectedMatterId) {
        setHint("Select a thread to lock matter and subject automatically.");
        return;
      }
      if (visibleCount === 0) {
        setHint("No existing threads for this matter. Start a new one.");
        return;
      }
      const suffix = visibleCount === 1 ? "" : "s";
      setHint(`${visibleCount} existing thread${suffix} for this matter.`);
    };

    const syncMatterFromThread = () => {
      const hasSelectedThread = threadSelect.value.trim().length > 0;
      if (hasSelectedThread) {
        const selectedOption = threadSelect.selectedOptions.item(0);
        const matterId = selectedOption ? selectedOption.getAttribute("data-matter-id") || "" : "";
        if (matterId) {
          const match = Array.from(matterSelect.options).find((option) => option.value === matterId);
          if (match) {
            matterSelect.value = matterId;
          }
        }
      }

      filterThreadsByMatter();
      setSubjectState();
    };

    matterSelect.addEventListener("change", () => {
      filterThreadsByMatter();
      setSubjectState();
    });
    threadSelect.addEventListener("change", syncMatterFromThread);
    filterThreadsByMatter();
    setSubjectState();
  };

  const initLeadMatterSync = () => {
    const intakeMatterSelect = document.querySelector("[data-lead-matter-select='intake']");
    const engagementMatterSelect = document.querySelector("[data-lead-matter-select='engagement']");
    const syncToggle = document.querySelector("[data-lead-matter-sync-toggle]");
    if (
      !(intakeMatterSelect instanceof HTMLSelectElement) ||
      !(engagementMatterSelect instanceof HTMLSelectElement)
    ) {
      return;
    }

    const isSyncEnabled = () =>
      !(syncToggle instanceof HTMLInputElement) || syncToggle.type !== "checkbox" || syncToggle.checked;

    const syncMatter = (source, target) => {
      if (!isSyncEnabled()) {
        return;
      }
      const nextValue = source.value;
      if (target.value !== nextValue) {
        target.value = nextValue;
      }
    };

    const alignOnEnable = () => {
      if (!isSyncEnabled()) {
        return;
      }
      if (intakeMatterSelect.value) {
        engagementMatterSelect.value = intakeMatterSelect.value;
        return;
      }
      if (engagementMatterSelect.value) {
        intakeMatterSelect.value = engagementMatterSelect.value;
      }
    };

    intakeMatterSelect.addEventListener("change", () => syncMatter(intakeMatterSelect, engagementMatterSelect));
    engagementMatterSelect.addEventListener("change", () => syncMatter(engagementMatterSelect, intakeMatterSelect));
    if (syncToggle instanceof HTMLInputElement && syncToggle.type === "checkbox") {
      syncToggle.addEventListener("change", alignOnEnable);
    }

    alignOnEnable();
  };

  const initQuickFillButtons = () => {
    const buttons = Array.from(document.querySelectorAll("[data-fill-target][data-fill-value]"));
    buttons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      button.addEventListener("click", () => {
        const targetId = button.getAttribute("data-fill-target") || "";
        const value = button.getAttribute("data-fill-value") || "";
        const target = targetId ? document.getElementById(targetId) : null;
        if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) {
          return;
        }

        target.value = value;
        target.dispatchEvent(new Event("input", { bubbles: true }));
        target.dispatchEvent(new Event("change", { bubbles: true }));
        target.focus();
        target.select();
      });
    });
  };

  const initCopyActions = () => {
    const buttons = Array.from(document.querySelectorAll("[data-copy-target]"));
    if (buttons.length === 0) {
      return;
    }

    const setFeedback = (targetId, text) => {
      const feedbackNode = Array.from(document.querySelectorAll("[data-copy-feedback-for]")).find(
        (node) => node.getAttribute("data-copy-feedback-for") === targetId
      );
      if (feedbackNode instanceof HTMLElement) {
        feedbackNode.textContent = text;
      }
    };

    const legacyCopy = (text) => {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.appendChild(helper);
      helper.focus();
      helper.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch (error) {
        copied = false;
      }
      document.body.removeChild(helper);
      return copied;
    };

    buttons.forEach((button) => {
      if (!(button instanceof HTMLButtonElement)) {
        return;
      }
      button.addEventListener("click", async () => {
        const targetId = button.getAttribute("data-copy-target") || "";
        const successText = button.getAttribute("data-copy-success-text") || "Copied";
        const source = targetId ? document.getElementById(targetId) : null;
        if (!(source instanceof HTMLInputElement || source instanceof HTMLTextAreaElement)) {
          return;
        }
        const text = source.value.trim();
        if (!text) {
          setFeedback(targetId, "Nothing to copy yet.");
          return;
        }

        let copied = false;
        if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
          try {
            await navigator.clipboard.writeText(text);
            copied = true;
          } catch (error) {
            copied = false;
          }
        }
        if (!copied) {
          copied = legacyCopy(text);
        }

        if (copied) {
          const original = button.textContent || "Copy";
          button.textContent = successText;
          window.setTimeout(() => {
            button.textContent = original;
          }, 1300);
          setFeedback(targetId, "Copied to clipboard.");
        } else {
          setFeedback(targetId, "Unable to copy automatically. Copy manually.");
        }
      });
    });
  };

  const initPortalLinkMatterFilter = () => {
    const matterSelect = document.getElementById("portal-link-matter");
    const documentSelect = document.getElementById("portal-link-document");
    if (!(matterSelect instanceof HTMLSelectElement) || !(documentSelect instanceof HTMLSelectElement)) {
      return;
    }

    const options = Array.from(documentSelect.options);
    const applyMatterFilter = () => {
      const selectedMatterId = matterSelect.value.trim();
      options.forEach((option, index) => {
        if (index === 0) {
          option.hidden = false;
          return;
        }
        const optionMatterId = option.getAttribute("data-matter-id") || "";
        const matchesMatter = !selectedMatterId || !optionMatterId || optionMatterId === selectedMatterId;
        option.hidden = !matchesMatter;
        if (!matchesMatter && option.selected) {
          documentSelect.value = "";
        }
      });
    };

    matterSelect.addEventListener("change", applyMatterFilter);
    applyMatterFilter();
  };

  const initListFilters = () => {
    const groups = Array.from(document.querySelectorAll("[data-list-filter]"));
    groups.forEach((group) => {
      const targetId = group.getAttribute("data-list-filter-target") || "";
      if (!targetId) {
        return;
      }
      const list = document.getElementById(targetId);
      const search = group.querySelector("[data-list-filter-search]");
      const count = group.querySelector("[data-list-filter-count]");
      if (!(list instanceof HTMLElement) || !(search instanceof HTMLInputElement)) {
        return;
      }
      const scope = group.parentElement || document;
      const empty = scope.querySelector("[data-list-filter-empty]");
      const items = Array.from(list.querySelectorAll("[data-list-item]"));

      const apply = () => {
        const query = search.value.trim().toLowerCase();
        let visible = 0;
        items.forEach((item) => {
          const haystack = (item.getAttribute("data-search") || item.textContent || "").toLowerCase();
          const matches = !query || haystack.includes(query);
          item.hidden = !matches;
          if (matches) {
            visible += 1;
          }
        });

        if (count instanceof HTMLElement) {
          count.textContent = `${visible} of ${items.length} shown`;
        }
        if (empty instanceof HTMLElement) {
          empty.hidden = visible > 0;
        }
      };

      search.addEventListener("input", apply);
      search.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !search.value) {
          return;
        }
        event.preventDefault();
        search.value = "";
        apply();
      });
      apply();
    });
  };

  const initVoiceRecorder = () => {
    const widgets = Array.from(document.querySelectorAll("[data-voice-recorder]"));
    widgets.forEach((widget) => {
      if (!(widget instanceof HTMLElement)) {
        return;
      }
      const inputId = widget.getAttribute("data-voice-file-input") || "";
      const fileInput = inputId ? document.getElementById(inputId) : null;
      const startButton = widget.querySelector("[data-voice-start]");
      const stopButton = widget.querySelector("[data-voice-stop]");
      const clearButton = widget.querySelector("[data-voice-clear]");
      const statusNode = widget.querySelector("[data-voice-status]");
      if (!(fileInput instanceof HTMLInputElement) || fileInput.type !== "file") {
        return;
      }
      if (
        !(startButton instanceof HTMLButtonElement) ||
        !(stopButton instanceof HTMLButtonElement) ||
        !(clearButton instanceof HTMLButtonElement)
      ) {
        return;
      }

      let mediaRecorder = null;
      let recordingStream = null;
      let chunks = [];

      const updateStatus = (message) => {
        if (statusNode instanceof HTMLElement) {
          statusNode.textContent = message;
        }
      };

      const stopStream = () => {
        if (!recordingStream) {
          return;
        }
        recordingStream.getTracks().forEach((track) => {
          track.stop();
        });
        recordingStream = null;
      };

      const buildFilename = (mimeType) => {
        const normalized = String(mimeType || "").toLowerCase();
        if (normalized.includes("ogg")) {
          return "voice-note.ogg";
        }
        if (normalized.includes("mp4") || normalized.includes("m4a")) {
          return "voice-note.m4a";
        }
        return "voice-note.webm";
      };

      const attachBlobToFileInput = (blob) => {
        const file = new File([blob], buildFilename(blob.type), { type: blob.type || "audio/webm" });
        const transfer = new DataTransfer();
        transfer.items.add(file);
        fileInput.files = transfer.files;
      };

      if (!navigator.mediaDevices || typeof window.MediaRecorder === "undefined") {
        startButton.disabled = true;
        stopButton.disabled = true;
        updateStatus("In-browser recording is unavailable in this browser. Upload an audio file instead.");
        return;
      }

      startButton.addEventListener("click", async () => {
        if (mediaRecorder && mediaRecorder.state === "recording") {
          return;
        }
        try {
          recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
          mediaRecorder = new MediaRecorder(recordingStream);
        } catch (_error) {
          updateStatus("Unable to access microphone. Check browser permissions.");
          stopStream();
          return;
        }

        chunks = [];
        mediaRecorder.addEventListener("dataavailable", (event) => {
          if (event.data && event.data.size > 0) {
            chunks.push(event.data);
          }
        });
        mediaRecorder.addEventListener("stop", () => {
          const recordedBlob = new Blob(chunks, { type: mediaRecorder?.mimeType || "audio/webm" });
          if (recordedBlob.size > 0) {
            try {
              attachBlobToFileInput(recordedBlob);
              updateStatus(`Recorded ${buildFilename(recordedBlob.type)} and attached to upload.`);
            } catch (_error) {
              updateStatus("Recording captured, but this browser cannot attach it automatically.");
            }
          } else {
            updateStatus("No audio captured.");
          }
          startButton.disabled = false;
          stopButton.disabled = true;
          stopStream();
          mediaRecorder = null;
        });

        mediaRecorder.start();
        startButton.disabled = true;
        stopButton.disabled = false;
        updateStatus("Recording... click Stop when finished.");
      });

      stopButton.addEventListener("click", () => {
        if (!mediaRecorder || mediaRecorder.state !== "recording") {
          return;
        }
        mediaRecorder.stop();
        stopButton.disabled = true;
        updateStatus("Processing recording...");
      });

      clearButton.addEventListener("click", () => {
        if (mediaRecorder && mediaRecorder.state === "recording") {
          mediaRecorder.stop();
        }
        fileInput.value = "";
        startButton.disabled = false;
        stopButton.disabled = true;
        updateStatus("Recorder cleared.");
      });

      fileInput.addEventListener("change", () => {
        const selected = fileInput.files && fileInput.files.length > 0 ? fileInput.files[0].name : "";
        if (selected) {
          updateStatus(`Selected ${selected}.`);
        }
      });
    });
  };

  const parseSortValue = (value, type) => {
    const raw = value.trim();
    if (type === "number") {
      const cleaned = raw.replace(/[^0-9.\-]/g, "");
      const parsed = Number(cleaned);
      return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
    }
    if (type === "date") {
      const parsed = Date.parse(raw);
      return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
    }
    return raw.toLowerCase();
  };

  const normalizeCellText = (value) =>
    String(value || "")
      .replace(/\s+/g, " ")
      .trim();

  const csvEscape = (value) => {
    const normalized = normalizeCellText(value);
    if (normalized.includes('"') || normalized.includes(",") || normalized.includes("\n")) {
      return `"${normalized.replace(/"/g, '""')}"`;
    }
    return normalized;
  };

  const downloadCsv = (filename, csvText) => {
    const blob = new Blob([`\ufeff${csvText}`], { type: "text/csv;charset=utf-8" });
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(href), 1000);
  };

  const initTableTools = () => {
    const buildAutoTableToolbars = () => {
      const explicitTargets = new Set(
        Array.from(document.querySelectorAll("[data-table-tools]"))
          .map((toolbar) => toolbar.getAttribute("data-table-target") || "")
          .filter((value) => value.length > 0)
      );
      let autoIndex = 0;
      const tables = Array.from(document.querySelectorAll("table.table"));
      tables.forEach((table) => {
        if (!(table instanceof HTMLTableElement)) {
          return;
        }
        if (table.dataset.noTableTools === "true") {
          return;
        }
        const body = table.tBodies.item(0);
        if (!body) {
          return;
        }
        if (!table.id) {
          autoIndex += 1;
          table.id = `auto-table-${autoIndex}`;
        }
        if (explicitTargets.has(table.id)) {
          return;
        }
        const existing = document.querySelector(
          `[data-table-tools][data-table-target="${table.id}"]`
        );
        if (existing) {
          return;
        }

        const toolbar = document.createElement("div");
        toolbar.className = "table-tools";
        toolbar.setAttribute("data-table-tools", "");
        toolbar.setAttribute("data-table-target", table.id);

        const search = document.createElement("input");
        search.className = "form-control";
        search.type = "search";
        search.placeholder = "Filter records...";
        search.setAttribute("data-table-search", "");

        const count = document.createElement("div");
        count.className = "table-count";
        count.setAttribute("data-table-count", "");
        count.setAttribute("aria-live", "polite");

        toolbar.appendChild(search);
        toolbar.appendChild(count);

        const responsive = table.closest(".table-responsive");
        if (responsive && responsive.parentElement) {
          responsive.parentElement.insertBefore(toolbar, responsive);
          return;
        }
        if (table.parentElement) {
          table.parentElement.insertBefore(toolbar, table);
        }
      });
    };

    buildAutoTableToolbars();
    const toolbars = Array.from(document.querySelectorAll("[data-table-tools]"));
    toolbars.forEach((toolbar) => {
      const targetId = toolbar.getAttribute("data-table-target");
      const table = targetId ? document.getElementById(targetId) : null;
      if (!(table instanceof HTMLTableElement)) {
        return;
      }
      const body = table.tBodies.item(0);
      if (!body) {
        return;
      }
      const headerLabels = Array.from(table.tHead?.rows.item(0)?.cells || []).map((cell) =>
        normalizeCellText(cell.textContent)
      );

      let rows = Array.from(body.querySelectorAll("tr[data-table-row]"));
      if (rows.length === 0) {
        rows = Array.from(body.querySelectorAll("tr")).filter(
          (row) => !row.hasAttribute("data-table-empty-row")
        );
        rows.forEach((row) => {
          row.setAttribute("data-table-row", "");
        });
      }
      rows.forEach((row) => {
        if (!row.dataset.search) {
          row.dataset.search = row.textContent || "";
        }
        Array.from(row.cells).forEach((cell, index) => {
          if (!cell.getAttribute("data-label")) {
            cell.setAttribute("data-label", headerLabels[index] || `Column ${index + 1}`);
          }
        });
      });

      let emptyRow = body.querySelector("[data-table-empty-row]");
      if (!(emptyRow instanceof HTMLTableRowElement)) {
        const generatedEmpty = document.createElement("tr");
        generatedEmpty.setAttribute("data-table-empty-row", "");
        generatedEmpty.className = "table-empty-row";
        generatedEmpty.hidden = true;
        const emptyCell = document.createElement("td");
        const headColumns = table.tHead?.rows.item(0)?.cells.length || 1;
        emptyCell.colSpan = Math.max(1, headColumns);
        emptyCell.className = "muted";
        emptyCell.textContent = "No matching records.";
        generatedEmpty.appendChild(emptyCell);
        body.appendChild(generatedEmpty);
        emptyRow = generatedEmpty;
      }

      const search = toolbar.querySelector("[data-table-search]");
      let count = toolbar.querySelector("[data-table-count]");
      if (!(count instanceof HTMLElement)) {
        count = document.createElement("div");
        count.className = "table-count";
        count.setAttribute("data-table-count", "");
        count.setAttribute("aria-live", "polite");
      }

      let meta = toolbar.querySelector("[data-table-tools-meta]");
      if (!(meta instanceof HTMLElement)) {
        meta = document.createElement("div");
        meta.className = "table-tools-meta";
        meta.setAttribute("data-table-tools-meta", "");
        toolbar.appendChild(meta);
      }
      if (!meta.contains(count)) {
        meta.appendChild(count);
      }

      let clearButton = toolbar.querySelector("[data-table-clear]");
      if (!(clearButton instanceof HTMLButtonElement)) {
        clearButton = document.createElement("button");
        clearButton.type = "button";
        clearButton.className = "btn btn-sm btn-outline-light table-clear-btn";
        clearButton.textContent = "Clear";
        clearButton.setAttribute("data-table-clear", "");
        meta.appendChild(clearButton);
      }
      let exportButton = toolbar.querySelector("[data-table-export]");
      if (!(exportButton instanceof HTMLButtonElement)) {
        exportButton = document.createElement("button");
        exportButton.type = "button";
        exportButton.className = "btn btn-sm btn-outline-light table-export-btn";
        exportButton.textContent = "Export CSV";
        exportButton.setAttribute("data-table-export", "");
        meta.appendChild(exportButton);
      }
      const headerCells = Array.from(table.querySelectorAll("thead th"));
      headerCells.forEach((header, index) => {
        if (!(header instanceof HTMLTableCellElement)) {
          return;
        }
        if (header.hasAttribute("data-no-sort")) {
          return;
        }
        if (!header.dataset.sortIndex) {
          header.dataset.sortIndex = String(index);
        }
      });
      const sortableHeaders = headerCells.filter((header) =>
        Boolean(header.getAttribute("data-sort-index"))
      );

      const updateCount = (visible) => {
        if (!count) {
          return;
        }
        if (rows.length === 0) {
          count.textContent = "0 records";
          return;
        }
        count.textContent = `${visible} of ${rows.length} shown`;
      };

      const visibleRows = () => rows.filter((row) => !row.hidden);

      const applySearch = () => {
        const query =
          search && search instanceof HTMLInputElement
            ? search.value.trim().toLowerCase()
            : "";
        if (clearButton) {
          clearButton.hidden = query.length === 0;
        }
        let visible = 0;
        rows.forEach((row) => {
          const haystack = (row.dataset.search || row.textContent || "").toLowerCase();
          const matches = !query || haystack.includes(query);
          row.hidden = !matches;
          if (matches) {
            visible += 1;
          }
        });

        if (emptyRow instanceof HTMLElement) {
          if (rows.length === 0) {
            emptyRow.hidden = false;
          } else {
            emptyRow.hidden = visible > 0;
          }
        }
        if (exportButton) {
          exportButton.disabled = visible === 0;
        }
        updateCount(visible);
      };

      const sortByColumn = (header) => {
        const index = Number(header.dataset.sortIndex || "");
        if (Number.isNaN(index)) {
          return;
        }
        const type = (header.dataset.sortType || "text").toLowerCase();
        const current = header.getAttribute("aria-sort");
        const direction = current === "ascending" ? "descending" : "ascending";
        sortableHeaders.forEach((item) => {
          item.setAttribute("aria-sort", "none");
        });
        header.setAttribute("aria-sort", direction);

        rows.sort((left, right) => {
          const leftText = left.cells.item(index)?.textContent || "";
          const rightText = right.cells.item(index)?.textContent || "";
          const leftValue = parseSortValue(leftText, type);
          const rightValue = parseSortValue(rightText, type);

          if (leftValue < rightValue) {
            return direction === "ascending" ? -1 : 1;
          }
          if (leftValue > rightValue) {
            return direction === "ascending" ? 1 : -1;
          }
          return 0;
        });

        rows.forEach((row) => {
          body.appendChild(row);
        });
        applySearch();
      };

      if (search && search instanceof HTMLInputElement) {
        search.addEventListener("input", applySearch);
        search.addEventListener("keydown", (event) => {
          if (event.key !== "Escape") {
            return;
          }
          if (!search.value) {
            return;
          }
          event.preventDefault();
          search.value = "";
          applySearch();
        });
      }

      if (clearButton) {
        clearButton.addEventListener("click", () => {
          if (search && search instanceof HTMLInputElement) {
            search.value = "";
            search.focus();
          }
          applySearch();
        });
      }

      if (exportButton) {
        exportButton.addEventListener("click", () => {
          const rowsToExport = visibleRows();
          if (rowsToExport.length === 0) {
            return;
          }
          const headers = headerLabels.length > 0
            ? headerLabels
            : Array.from(rowsToExport[0].cells).map((_, index) => `Column ${index + 1}`);
          const csvRows = [
            headers.map((value) => csvEscape(value)).join(","),
            ...rowsToExport.map((row) =>
              Array.from(row.cells)
                .map((cell) => csvEscape(cell.textContent))
                .join(",")
            ),
          ];
          const baseName = (table.getAttribute("data-export-name") || table.id || "table-export")
            .toLowerCase()
            .replace(/[^a-z0-9_-]+/g, "-")
            .replace(/^-+|-+$/g, "") || "table-export";
          const dateTag = new Date().toISOString().slice(0, 10);
          downloadCsv(`${baseName}-${dateTag}.csv`, csvRows.join("\n"));
        });
      }

      sortableHeaders.forEach((header) => {
        header.tabIndex = 0;
        header.setAttribute("aria-sort", "none");
        header.classList.add("table-sort");
        header.addEventListener("click", () => sortByColumn(header));
        header.addEventListener("keydown", (event) => {
          if (event.key !== "Enter" && event.key !== " ") {
            return;
          }
          event.preventDefault();
          sortByColumn(header);
        });
      });

      applySearch();
    });
  };

  const initBackToTop = () => {
    const button = document.querySelector("[data-back-to-top]");
    if (!(button instanceof HTMLButtonElement)) {
      return;
    }

    const toggle = () => {
      button.classList.toggle("is-visible", window.scrollY > 460);
    };

    window.addEventListener("scroll", toggle, { passive: true });
    toggle();

    button.addEventListener("click", () => {
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  };

  const initCollapsibleNav = () => {
    const nav = document.querySelector("[data-collapsible-nav]");
    if (!(nav instanceof HTMLElement)) {
      return;
    }

    const minDelta = 8;
    let lastY = window.scrollY;
    let isCollapsed = false;
    let ticking = false;
    let expandedHeight = nav.offsetHeight;

    const measureExpandedHeight = () => {
      const wasCollapsed = nav.classList.contains("is-collapsed");
      if (wasCollapsed) {
        nav.classList.remove("is-collapsed");
      }
      expandedHeight = Math.max(1, nav.offsetHeight);
      if (wasCollapsed) {
        nav.classList.add("is-collapsed");
      }
    };

    const collapseStart = () => Math.max(72, expandedHeight + 18);

    const setCollapsed = (nextState) => {
      if (isCollapsed === nextState) {
        return;
      }
      isCollapsed = nextState;
      nav.classList.toggle("is-collapsed", nextState);
    };

    const update = () => {
      const y = Math.max(0, window.scrollY);
      const delta = y - lastY;

      if (y <= 10 || nav.contains(document.activeElement)) {
        setCollapsed(false);
      } else {
        if (delta > minDelta && y > collapseStart()) {
          setCollapsed(true);
        } else if (delta < -minDelta) {
          setCollapsed(false);
        }
      }

      lastY = y;
      ticking = false;
    };

    const onScroll = () => {
      if (ticking) {
        return;
      }
      ticking = true;
      window.requestAnimationFrame(update);
    };

    window.addEventListener("scroll", onScroll, { passive: true });
    window.addEventListener("resize", () => {
      measureExpandedHeight();
      if (window.scrollY <= collapseStart()) {
        setCollapsed(false);
      }
      update();
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Tab") {
        setCollapsed(false);
      }
    });

    measureExpandedHeight();
    update();
  };

  const initFormValidationUX = () => {
    const forms = Array.from(document.querySelectorAll("form"));
    if (forms.length === 0) {
      return;
    }

    const cssEscape = (value) => {
      if (typeof value !== "string") {
        return "";
      }
      if (window.CSS && typeof window.CSS.escape === "function") {
        return window.CSS.escape(value);
      }
      return value.replace(/[^a-zA-Z0-9_-]/g, "\\$&");
    };

    const isValidatableControl = (control) => {
      if (
        !(control instanceof HTMLInputElement) &&
        !(control instanceof HTMLTextAreaElement) &&
        !(control instanceof HTMLSelectElement)
      ) {
        return false;
      }
      if (control.disabled || control.type === "hidden") {
        return false;
      }
      if (
        control instanceof HTMLInputElement &&
        ["submit", "button", "reset", "image", "file"].includes(control.type)
      ) {
        return false;
      }
      return true;
    };

    const ensureControlId = (form, control) => {
      if (control.id) {
        return control.id;
      }
      const base = control.getAttribute("name") || "field";
      const suffix = Math.random().toString(36).slice(2, 8);
      const id = `auto-${base.replace(/[^a-zA-Z0-9_-]/g, "-")}-${suffix}`;
      control.id = id;
      return id;
    };

    const findControlLabel = (form, control) => {
      const id = ensureControlId(form, control);
      let label = form.querySelector(`label[for="${cssEscape(id)}"]`);
      if (label instanceof HTMLLabelElement) {
        return label;
      }
      label = control.closest("label");
      if (label instanceof HTMLLabelElement) {
        return label;
      }
      return null;
    };

    const getControlLabelText = (form, control) => {
      const label = findControlLabel(form, control);
      if (!label) {
        return control.getAttribute("name") || "Field";
      }
      return (label.textContent || "Field").replace(/\s*\*\s*$/, "").trim() || "Field";
    };

    const getControlErrorMessage = (control) => {
      if (control.validity.valueMissing) {
        return "This field is required.";
      }
      if (control.validity.typeMismatch) {
        return "Please enter a valid value.";
      }
      if (control.validity.tooShort) {
        return `Please use at least ${control.minLength} characters.`;
      }
      if (control.validity.tooLong) {
        return `Please use no more than ${control.maxLength} characters.`;
      }
      if (control.validity.rangeUnderflow || control.validity.rangeOverflow) {
        return "Please choose a value within the allowed range.";
      }
      return (control.validationMessage || "Please correct this field.").trim();
    };

    const ensureFieldErrorNode = (form, control) => {
      const key = ensureControlId(form, control);
      let node = form.querySelector(`[data-field-error-for="${cssEscape(key)}"]`);
      if (node instanceof HTMLElement) {
        return node;
      }
      node = document.createElement("div");
      node.className = "field-error";
      node.setAttribute("data-field-error-for", key);
      node.setAttribute("aria-live", "polite");
      node.hidden = true;

      const anchor = control.closest(".input-group") || control;
      anchor.insertAdjacentElement("afterend", node);
      return node;
    };

    const setControlValid = (form, control) => {
      const key = control.id || ensureControlId(form, control);
      const node = form.querySelector(`[data-field-error-for="${cssEscape(key)}"]`);
      control.classList.remove("is-invalid");
      control.removeAttribute("aria-invalid");
      if (node instanceof HTMLElement) {
        node.hidden = true;
        node.textContent = "";
      }
    };

    const setControlInvalid = (form, control) => {
      const node = ensureFieldErrorNode(form, control);
      const message = getControlErrorMessage(control);
      control.classList.add("is-invalid");
      control.setAttribute("aria-invalid", "true");
      if (node instanceof HTMLElement) {
        node.hidden = false;
        node.textContent = message;
      }
      return message;
    };

    const renderSummary = (form, invalidControls) => {
      let summary = form.querySelector("[data-form-error-summary]");
      if (invalidControls.length === 0) {
        if (summary instanceof HTMLElement) {
          summary.remove();
        }
        return;
      }

      if (!(summary instanceof HTMLElement)) {
        summary = document.createElement("div");
        summary.className = "alert form-error-summary";
        summary.setAttribute("data-form-error-summary", "");
        summary.tabIndex = -1;
        form.prepend(summary);
      }

      const title = invalidControls.length === 1
        ? "Please correct 1 field before submitting."
        : `Please correct ${invalidControls.length} fields before submitting.`;

      summary.replaceChildren();
      const titleNode = document.createElement("div");
      titleNode.className = "form-error-summary-title";
      titleNode.textContent = title;
      summary.appendChild(titleNode);

      const listNode = document.createElement("ul");
      listNode.className = "form-error-summary-list";
      invalidControls.forEach((control) => {
        const id = ensureControlId(form, control);
        const label = getControlLabelText(form, control);
        const item = document.createElement("li");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "form-error-link";
        button.setAttribute("data-form-error-target", id);
        button.textContent = label;
        button.addEventListener("click", () => {
          const target = button.getAttribute("data-form-error-target") || "";
          const focusedControl = target ? form.querySelector(`#${cssEscape(target)}`) : null;
          if (
            focusedControl instanceof HTMLInputElement ||
            focusedControl instanceof HTMLTextAreaElement ||
            focusedControl instanceof HTMLSelectElement
          ) {
            focusedControl.focus();
            focusedControl.scrollIntoView({ block: "center", behavior: "smooth" });
          }
        });
        item.appendChild(button);
        listNode.appendChild(item);
      });
      summary.appendChild(listNode);

      summary.focus();
    };

    forms.forEach((form) => {
      if (!(form instanceof HTMLFormElement)) {
        return;
      }
      if (form.dataset.formValidationUx === "off") {
        return;
      }

      const controls = Array.from(form.querySelectorAll("input, textarea, select")).filter(
        isValidatableControl
      );
      if (controls.length === 0) {
        return;
      }

      controls.forEach((control) => {
        if (control.required) {
          const label = findControlLabel(form, control);
          if (label) {
            label.classList.add("is-required");
          }
        }

        const eventName =
          control instanceof HTMLInputElement &&
          (control.type === "checkbox" || control.type === "radio")
            ? "change"
            : "input";
        control.addEventListener(eventName, () => {
          if (control.checkValidity()) {
            setControlValid(form, control);
          }
        });
        control.addEventListener("blur", () => {
          if (!control.checkValidity()) {
            setControlInvalid(form, control);
          }
        });
      });

      form.addEventListener(
        "submit",
        (event) => {
          const invalidControls = controls.filter((control) => !control.checkValidity());
          controls.forEach((control) => {
            if (invalidControls.includes(control)) {
              setControlInvalid(form, control);
            } else {
              setControlValid(form, control);
            }
          });

          renderSummary(form, invalidControls);
          if (invalidControls.length > 0) {
            event.preventDefault();
            event.stopPropagation();
            invalidControls[0].focus();
            invalidControls[0].scrollIntoView({ block: "center", behavior: "smooth" });
          }
        },
        { capture: true }
      );
    });
  };

  const initSubmitState = () => {
    const forms = Array.from(document.querySelectorAll("form"));
    forms.forEach((form) => {
      const method = (form.getAttribute("method") || "get").toLowerCase();
      if (method !== "post" || form.dataset.noSubmitState === "true") {
        return;
      }

      form.addEventListener(
        "submit",
        (event) => {
          if (form.dataset.submitting === "1") {
            event.preventDefault();
            return;
          }
          if (typeof form.checkValidity === "function" && !form.checkValidity()) {
            return;
          }

          form.dataset.submitting = "1";
          form.classList.add("is-submitting");
          const controls = Array.from(
            form.querySelectorAll("button[type='submit'], input[type='submit']")
          );
          controls.forEach((control) => {
            if (control instanceof HTMLButtonElement) {
              const nextText = control.getAttribute("data-submitting-text") || "Working...";
              control.dataset.originalText = control.textContent || "";
              control.textContent = nextText;
              control.setAttribute("disabled", "disabled");
              control.setAttribute("aria-busy", "true");
              return;
            }
            if (control instanceof HTMLInputElement) {
              const nextValue = control.getAttribute("data-submitting-text") || "Working...";
              control.dataset.originalText = control.value;
              control.value = nextValue;
              control.setAttribute("disabled", "disabled");
              control.setAttribute("aria-busy", "true");
            }
          });
        },
        { capture: true }
      );
    });
  };

  const initFormDrafts = () => {
    const forms = Array.from(document.querySelectorAll("form[data-form-draft-key]")).filter(
      (form) => form instanceof HTMLFormElement
    );
    if (forms.length === 0) {
      return;
    }

    const canUseStorage = (() => {
      try {
        const probeKey = "__form_draft_probe__";
        window.localStorage.setItem(probeKey, "1");
        window.localStorage.removeItem(probeKey);
        return true;
      } catch (_error) {
        return false;
      }
    })();
    if (!canUseStorage) {
      return;
    }

    const skipInputTypes = new Set(["hidden", "password", "file", "submit", "button", "reset"]);

    const collectNamedControls = (form) => {
      const grouped = new Map();
      Array.from(form.elements).forEach((element) => {
        if (
          !(
            element instanceof HTMLInputElement ||
            element instanceof HTMLTextAreaElement ||
            element instanceof HTMLSelectElement
          )
        ) {
          return;
        }
        if (!element.name || element.disabled) {
          return;
        }
        if (element instanceof HTMLInputElement && skipInputTypes.has(element.type)) {
          return;
        }
        if (!grouped.has(element.name)) {
          grouped.set(element.name, []);
        }
        grouped.get(element.name).push(element);
      });
      return grouped;
    };

    const controlValue = (controls) => {
      if (!Array.isArray(controls) || controls.length === 0) {
        return "";
      }
      const first = controls[0];
      if (first instanceof HTMLInputElement && first.type === "radio") {
        const selected = controls.find(
          (control) => control instanceof HTMLInputElement && control.checked
        );
        return selected instanceof HTMLInputElement ? selected.value : "";
      }
      if (first instanceof HTMLInputElement && first.type === "checkbox") {
        if (controls.length === 1) {
          return first.checked;
        }
        return controls
          .filter((control) => control instanceof HTMLInputElement && control.checked)
          .map((control) => (control instanceof HTMLInputElement ? control.value : ""))
          .filter((value) => value.length > 0);
      }
      if (first instanceof HTMLSelectElement && first.multiple) {
        return Array.from(first.selectedOptions).map((option) => option.value);
      }
      if (
        first instanceof HTMLInputElement ||
        first instanceof HTMLTextAreaElement ||
        first instanceof HTMLSelectElement
      ) {
        return first.value;
      }
      return "";
    };

    const serializeForm = (form) => {
      const grouped = collectNamedControls(form);
      const payload = {};
      grouped.forEach((controls, name) => {
        payload[name] = controlValue(controls);
      });
      return payload;
    };

    const isMeaningfulValue = (value) => {
      if (Array.isArray(value)) {
        return value.length > 0;
      }
      if (typeof value === "boolean") {
        return value;
      }
      return String(value || "").trim().length > 0;
    };

    const hasMeaningfulData = (payload) =>
      Object.values(payload || {}).some((value) => isMeaningfulValue(value));

    const applyPayload = (form, payload) => {
      if (!payload || typeof payload !== "object") {
        return;
      }
      const grouped = collectNamedControls(form);
      Object.entries(payload).forEach(([name, value]) => {
        const controls = grouped.get(name) || [];
        if (controls.length === 0) {
          return;
        }
        const first = controls[0];
        if (first instanceof HTMLInputElement && first.type === "radio") {
          const next = String(value || "");
          controls.forEach((control) => {
            if (control instanceof HTMLInputElement) {
              control.checked = control.value === next;
            }
          });
          return;
        }
        if (first instanceof HTMLInputElement && first.type === "checkbox") {
          if (controls.length === 1) {
            first.checked = Boolean(value);
            return;
          }
          const picked = Array.isArray(value) ? new Set(value.map((item) => String(item))) : new Set();
          controls.forEach((control) => {
            if (control instanceof HTMLInputElement) {
              control.checked = picked.has(control.value);
            }
          });
          return;
        }
        if (first instanceof HTMLSelectElement && first.multiple) {
          const picked = Array.isArray(value) ? new Set(value.map((item) => String(item))) : new Set();
          Array.from(first.options).forEach((option) => {
            option.selected = picked.has(option.value);
          });
          return;
        }
        if (
          first instanceof HTMLInputElement ||
          first instanceof HTMLTextAreaElement ||
          first instanceof HTMLSelectElement
        ) {
          first.value = String(value || "");
          first.dispatchEvent(new Event("input", { bubbles: true }));
          first.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    };

    const formatSavedAt = (value) => {
      const date = new Date(value || "");
      if (Number.isNaN(date.getTime())) {
        return "";
      }
      return date.toLocaleString();
    };

    forms.forEach((form) => {
      const draftKey = String(form.getAttribute("data-form-draft-key") || "").trim();
      if (!draftKey) {
        return;
      }
      const storageKey = `form_draft::${window.location.pathname}::${draftKey}`;
      const statusId = form.getAttribute("data-form-draft-status") || "";
      const statusNode = statusId ? document.getElementById(statusId) : null;

      const setStatus = (message, tone = "muted") => {
        if (!(statusNode instanceof HTMLElement)) {
          return;
        }
        statusNode.className = `form-help mt-1 ${tone}`;
        statusNode.textContent = message;
      };

      const readDraft = () => {
        const raw = window.localStorage.getItem(storageKey);
        if (!raw) {
          return null;
        }
        try {
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== "object") {
            return null;
          }
          return parsed;
        } catch (_error) {
          return null;
        }
      };

      const removeDraft = () => {
        window.localStorage.removeItem(storageKey);
      };

      const saveDraft = () => {
        const values = serializeForm(form);
        if (!hasMeaningfulData(values)) {
          removeDraft();
          setStatus("");
          return;
        }
        const savedAt = new Date().toISOString();
        window.localStorage.setItem(
          storageKey,
          JSON.stringify({
            saved_at: savedAt,
            values,
          })
        );
        const pretty = formatSavedAt(savedAt);
        setStatus(pretty ? `Draft saved locally at ${pretty}.` : "Draft saved locally.");
      };

      const draft = readDraft();
      if (draft && draft.values && typeof draft.values === "object") {
        const currentValues = serializeForm(form);
        const currentHasData = hasMeaningfulData(currentValues);
        if (!currentHasData) {
          applyPayload(form, draft.values);
          const pretty = formatSavedAt(draft.saved_at);
          setStatus(pretty ? `Draft restored from ${pretty}.` : "Draft restored.");
        } else if (draft.saved_at) {
          const pretty = formatSavedAt(draft.saved_at);
          setStatus(
            pretty
              ? `Saved draft available from ${pretty}. Clear current values to auto-restore.`
              : "Saved draft available."
          );
        }
      }

      let debounceHandle = 0;
      const queueSave = () => {
        window.clearTimeout(debounceHandle);
        debounceHandle = window.setTimeout(saveDraft, 300);
      };

      form.addEventListener("input", queueSave);
      form.addEventListener("change", queueSave);
      form.addEventListener(
        "submit",
        () => {
          removeDraft();
          setStatus("");
        },
        { capture: true }
      );
    });
  };

  const run = () => {
    initFlashDismiss();
    initPasswordToggles();
    initStatusToneSystem();
    initGlobalOmnibox();
    initCommandPalette();
    initNavMenus();
    initMatterQuickFilters();
    initTimePrompts();
    initArchetypeAIDraftGenerator();
    initContractAIDraftGenerator();
    initDocumentAIDraftGenerator();
    initTimeCodeAssist();
    initTimerPresenceGuard();
    initLiveBillingCue();
    initQuoteAssist();
    initPortalMessageComposer();
    initLeadMatterSync();
    initPortalLinkMatterFilter();
    initQuickFillButtons();
    initCopyActions();
    initListFilters();
    initVoiceRecorder();
    initTableTools();
    initCollapsibleNav();
    initBackToTop();
    initFormValidationUX();
    initSubmitState();
    initFormDrafts();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
