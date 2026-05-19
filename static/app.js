document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const btn = form.querySelector('button[type="submit"]:not([formaction])');
      if (btn) {
        if (btn.disabled) {
          event.preventDefault();
          return;
        }
        btn.disabled = true;
        btn.dataset.original = btn.textContent || 'Submit';
        btn.textContent = 'Saving...';
      }
    });
  });

  const toggle = document.querySelector('.nav-toggle');
  const nav = document.getElementById('site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', () => {
      const isOpen = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(isOpen));
    });
  }

  document.querySelectorAll('form[action="/coach/assign"]').forEach((form) => {
    const selectAll = form.querySelector('.plan-select-all');
    const athleteCheckboxes = Array.from(form.querySelectorAll('.plan-athlete-choice'));
    const athleteIdsInput = form.querySelector('input[name="athlete_ids"]');
    if (!selectAll || !athleteCheckboxes.length || !athleteIdsInput) return;

    selectAll.addEventListener('change', () => {
      athleteCheckboxes.forEach((check) => { check.checked = selectAll.checked; });
    });

    athleteCheckboxes.forEach((check) => {
      check.addEventListener('change', () => {
        selectAll.checked = athleteCheckboxes.every((item) => item.checked);
      });
    });

    form.addEventListener('submit', (event) => {
      const selectedIds = athleteCheckboxes.filter((item) => item.checked).map((item) => item.value);
      athleteIdsInput.value = selectedIds.join(',');
      if (!selectedIds.length) {
        event.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) {
          submitBtn.disabled = false;
          if (submitBtn.dataset.original) submitBtn.textContent = submitBtn.dataset.original;
        }
        window.alert('Select at least one athlete before assigning this plan.');
      }
    });
  });

  document.querySelectorAll('.module-picker-form').forEach((form) => {
    const selectAll = form.querySelector('.plan-select-all-modules');
    const moduleCheckboxes = Array.from(form.querySelectorAll('.plan-module-choice'));
    const moduleIdsInput = form.querySelector('input[name="module_ids"]');
    if (!moduleCheckboxes.length || !moduleIdsInput) return;

    if (selectAll) {
      selectAll.addEventListener('change', () => {
        moduleCheckboxes.forEach((check) => { check.checked = selectAll.checked; });
      });
    }

    moduleCheckboxes.forEach((check) => {
      check.addEventListener('change', () => {
        if (selectAll) selectAll.checked = moduleCheckboxes.every((item) => item.checked);
      });
    });

    form.addEventListener('submit', (event) => {
      const selectedIds = moduleCheckboxes.filter((item) => item.checked).map((item) => item.value);
      moduleIdsInput.value = selectedIds.join(',');
      if (!selectedIds.length) {
        event.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) {
          submitBtn.disabled = false;
          if (submitBtn.dataset.original) submitBtn.textContent = submitBtn.dataset.original;
        }
        window.alert('Select at least one module before saving a plan.');
      }
    });
  });

  document.querySelectorAll('.calendar-widget').forEach((widget) => {
    const buttons = Array.from(widget.querySelectorAll('[data-calendar-date]'));
    const panels = Array.from(widget.querySelectorAll('[data-calendar-panel]'));
    if (!buttons.length || !panels.length) return;

    const setActive = (dateValue) => {
      buttons.forEach((button) => button.classList.toggle('active', button.dataset.calendarDate === dateValue));
      panels.forEach((panel) => {
        const panelKey = panel.dataset.calendarPanel || 'default';
        panel.classList.toggle('active', panelKey === dateValue);
      });
    };

    buttons.forEach((button) => {
      button.addEventListener('click', () => {
        if (!button.classList.contains('has-events')) {
          setActive('default');
          return;
        }
        setActive(button.dataset.calendarDate || 'default');
      });
    });

    const firstEventButton = buttons.find((button) => button.classList.contains('has-events'));
    setActive(firstEventButton ? (firstEventButton.dataset.calendarDate || 'default') : 'default');
  });

  document.querySelectorAll('form[action="/coach/modules"]').forEach((form) => {
    const planType = form.querySelector('select[name="plan_type"]');
    const categoryInput = form.querySelector('input[name="category"]');
    if (!planType || !categoryInput) return;

    planType.addEventListener('change', () => {
      if (planType.value === 'weight_room' && !categoryInput.value.trim()) categoryInput.value = 'Weight Room';
      if (planType.value === 'practice' && categoryInput.value.trim().toLowerCase() === 'weight room') categoryInput.value = 'Technique';
    });
  });
});
