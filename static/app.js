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
    const checks = Array.from(form.querySelectorAll('input[type="checkbox"][name="athlete_ids"]'));
    if (!selectAll || !checks.length) return;

    selectAll.addEventListener('change', () => {
      checks.forEach((check) => { check.checked = selectAll.checked; });
    });

    checks.forEach((check) => {
      check.addEventListener('change', () => {
        selectAll.checked = checks.every((item) => item.checked);
      });
    });
  });
});
