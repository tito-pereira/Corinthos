(function () {
  const deadline = performance.now() + 20000;
  const timer = setInterval(() => {
    const app = document.querySelector('[data-gui-ready="true"]');
    if (app) {
      const valid = app.dataset.page === 'main' && app.querySelector('.sidebar footer button')?.textContent.trim() === 'Update' && app.textContent.includes('Welcome to Corinthos') && app.querySelector('textarea');
      document.title = 'Corinthos desktop smoke: ' + (valid ? 'pass' : 'fail [layout]');
      clearInterval(timer);
    } else if (performance.now() > deadline) {
      document.title = 'Corinthos desktop smoke: fail [backend]';
      clearInterval(timer);
    }
  }, 100);
})();
