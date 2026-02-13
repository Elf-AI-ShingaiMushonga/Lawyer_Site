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
      if (event.key !== "Enter") {
        return;
      }
      const firstVisible = items.find((item) => !item.hidden);
      if (firstVisible) {
        window.location.href = firstVisible.getAttribute("href") || firstVisible.href;
      }
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
      const items = invalidControls
        .map((control) => {
          const id = ensureControlId(form, control);
          const label = getControlLabelText(form, control);
          return `<li><button type="button" class="form-error-link" data-form-error-target="${id}">${label}</button></li>`;
        })
        .join("");

      summary.innerHTML = `
        <div class="form-error-summary-title">${title}</div>
        <ul class="form-error-summary-list">${items}</ul>
      `;

      Array.from(summary.querySelectorAll("[data-form-error-target]")).forEach((button) => {
        button.addEventListener("click", () => {
          const target = button.getAttribute("data-form-error-target") || "";
          const control = target ? form.querySelector(`#${cssEscape(target)}`) : null;
          if (
            control instanceof HTMLInputElement ||
            control instanceof HTMLTextAreaElement ||
            control instanceof HTMLSelectElement
          ) {
            control.focus();
            control.scrollIntoView({ block: "center", behavior: "smooth" });
          }
        });
      });

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

  const run = () => {
    initFlashDismiss();
    initPasswordToggles();
    initCommandPalette();
    initMatterQuickFilters();
    initTimePrompts();
    initTableTools();
    initBackToTop();
    initFormValidationUX();
    initSubmitState();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
