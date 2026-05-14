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
});
