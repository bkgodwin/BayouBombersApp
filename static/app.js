document.addEventListener('DOMContentLoaded', () => {
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
});
