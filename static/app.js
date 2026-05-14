document.addEventListener('DOMContentLoaded', () => {
  // Form double-submit prevention
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', (event) => {
      const btn = form.querySelector('button[type="submit"]');
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

  // Mobile nav toggle
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.getElementById('site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', () => {
      const isOpen = nav.classList.toggle('open');
      toggle.setAttribute('aria-expanded', String(isOpen));
    });
  }

  // Coach plan assignment select-all controls
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

    form.addEventListener('submit', () => {
      athleteIdsInput.value = athleteCheckboxes.filter((item) => item.checked).map((item) => item.value).join(',');
    });
  });
});
