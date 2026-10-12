(() => {
  'use strict';
  const toggle = document.querySelector('.menu-toggle');
  const navigation = document.getElementById('site-navigation');
  function closeMenu() {
    if (!toggle || !navigation) return;
    toggle.setAttribute('aria-expanded', 'false');
    navigation.classList.remove('is-open');
  }
  if (toggle && navigation) {
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') !== 'true';
      toggle.setAttribute('aria-expanded', String(open));
      navigation.classList.toggle('is-open', open);
    });
    navigation.addEventListener('click', event => {
      if (event.target.closest('a')) closeMenu();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && toggle.getAttribute('aria-expanded') === 'true') {
        closeMenu();
        toggle.focus();
      }
    });
    matchMedia('(min-width: 941px)').addEventListener('change', closeMenu);
  }
  // Existing landing pages progressively reveal content; keep it readable without JS.
  document.querySelectorAll('[data-reveal]').forEach(element => element.classList.add('is-visible'));
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    const form = document.getElementById('lead-form');
    if (!link || !form || typeof window.ym !== 'function') return;
    if ((link.getAttribute('href') || '').includes('t.me/maksonchik200')) {
      try { window.ym(Number(form.dataset.metrikaId), 'reachGoal', 'telegram_maksonchik200'); } catch (_) {}
    }
  });
})();
