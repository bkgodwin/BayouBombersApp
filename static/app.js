document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('form').forEach((form) => {
    form.addEventListener('submit', () => {
      const btn = form.querySelector('button[type="submit"]');
      if (btn) {
        if (btn.disabled) {
          return;
        }
        btn.disabled = true;
        btn.dataset.original = btn.textContent || 'Submit';
        btn.textContent = 'Saving...';
      }
    });
  });
});
