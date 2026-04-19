(() => {
  for (const form of document.querySelectorAll("form")) {
    const typeSelect = form.querySelector("[data-schedule-type-select], select[name='schedule_type']");
    const typeFields = Array.from(form.querySelectorAll("[data-schedule-type-field]"));

    if (!typeSelect || typeFields.length === 0) {
      continue;
    }

    const syncScheduleType = () => {
      const activeType = typeSelect.value;

      for (const field of typeFields) {
        const allowedTypes = (field.dataset.types || "")
          .split(",")
          .map((type) => type.trim())
          .filter(Boolean);
        const isActive = allowedTypes.includes(activeType);

        field.hidden = !isActive;
        field.setAttribute("aria-hidden", String(!isActive));
        if (field.tagName === "FIELDSET") {
          field.disabled = !isActive;
        }

        for (const control of field.querySelectorAll("input, select, textarea, button")) {
          control.disabled = !isActive;
        }
      }
    };

    typeSelect.addEventListener("change", syncScheduleType);
    if (window.jQuery) {
      window.jQuery(typeSelect).on("select2:select select2:clear", syncScheduleType);
    }
    syncScheduleType();
  }

  for (const root of document.querySelectorAll("[data-target-selector]")) {
    const filter = root.querySelector("[data-target-filter]");
    const select = root.querySelector("[data-target-select]");
    const empty = root.querySelector("[data-target-empty]");

    if (!filter || !select) {
      continue;
    }

    const options = Array.from(select.options);

    const applyFilter = () => {
      const query = filter.value.trim().toLowerCase();
      let visibleCount = 0;

      for (const option of options) {
        const searchable = (option.dataset.search || option.textContent || "").toLowerCase();
        const matches = searchable.includes(query);
        option.hidden = !matches;
        if (matches) {
          visibleCount += 1;
        }
      }

      const selectedOption = select.selectedOptions[0];
      if (selectedOption && selectedOption.hidden) {
        select.value = "";
      }

      const hasVisibleOptions = visibleCount > 0;
      select.disabled = !hasVisibleOptions;
      if (empty) {
        empty.hidden = hasVisibleOptions;
      }
    };

    filter.addEventListener("input", applyFilter);
    filter.addEventListener("search", applyFilter);
    applyFilter();
  }

  for (const root of document.querySelectorAll("[data-interval-group]")) {
    const valueInput = root.querySelector("[data-interval-value]");
    const unitSelect = root.querySelector("[data-interval-unit]");

    if (!valueInput || !unitSelect) {
      continue;
    }

    const syncMin = () => {
      const min = unitSelect.value === "minutes" ? 5 : 1;
      valueInput.min = String(min);
      if (Number(valueInput.value || min) < min) {
        valueInput.value = String(min);
      }
    };

    unitSelect.addEventListener("change", syncMin);
    syncMin();
  }
})();
