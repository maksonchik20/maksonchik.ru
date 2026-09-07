(() => {
  'use strict';

  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const finePointer = matchMedia('(hover: hover) and (pointer: fine)');
  if (!('animate' in Element.prototype) || !('IntersectionObserver' in window)) return;

  const active = new Set();
  const seen = new WeakSet();
  const counters = new Map();
  const pendingPointers = new Map();
  const surfaces = [...document.querySelectorAll('.hero-art, .work-visual')];
  const visibleSurfaces = new Set();
  const hero = document.querySelector('.hero-art');
  const ease = 'cubic-bezier(.22, 1, .36, 1)';
  let frame = 0;

  function play(element, keyframes, options = {}) {
    if (!element || reduced.matches || document.hidden || element.contains(document.activeElement)) return;
    const animation = element.animate(keyframes, {
      duration: 680, easing: ease, fill: 'backwards', ...options
    });
    active.add(animation);
    const remove = () => active.delete(animation);
    animation.addEventListener('finish', remove, { once: true });
    animation.addEventListener('cancel', remove, { once: true });
  }

  function rise(element, delay = 0) {
    play(element, [
      { opacity: 0, transform: 'translateY(22px)' },
      { opacity: 1, transform: 'translateY(0)' }
    ], { delay });
  }

  function finishCounter(element) {
    const counter = counters.get(element);
    if (!counter) return;
    cancelAnimationFrame(counter.frame);
    element.textContent = counter.original;
    counters.delete(element);
  }

  function countUp(element) {
    if (!element || reduced.matches || document.hidden) return;
    const original = element.textContent;
    const target = Number(original.replace(/\s/g, ''));
    if (!Number.isFinite(target)) return;
    const counter = { original, frame: 0, started: null };
    counters.set(element, counter);
    function tick(now) {
      if (reduced.matches || document.hidden) return finishCounter(element);
      if (counter.started === null) counter.started = now;
      const progress = Math.min((now - counter.started) / 1150, 1);
      // This number is decorative; the original metric remains in the accessible copy.
      element.textContent = Math.round(target * (1 - Math.pow(1 - progress, 3)))
        .toLocaleString('ru-RU');
      if (progress < 1) counter.frame = requestAnimationFrame(tick);
      else finishCounter(element);
    }
    counter.frame = requestAnimationFrame(tick);
  }

  function enterHero(element) {
    rise(element.querySelector('.hero-overline'));
    element.querySelectorAll('.hero-line').forEach((line, index) => rise(line, 80 + index * 105));
    rise(element.querySelector('.home-hero-lead'), 270);
    rise(element.querySelector('.home-hero-actions'), 330);
    rise(element.querySelector('.hero-price'), 380);
    rise(element.querySelector('.hero-facts'), 400);
    const art = element.querySelector('.hero-sculpture');
    play(art, [
      { opacity: 0, transform: 'translateY(28px) rotate(-17deg) scale(.9)', offset: 0 },
      { opacity: 1, transform: 'translateY(-9px) rotate(-6deg) scale(1)', offset: .28 },
      { opacity: 1, transform: 'translateY(3px) rotate(-10deg) scale(1)', offset: .68 },
      { opacity: 1, transform: 'translateY(0) rotate(-8deg) scale(1)', offset: 1 }
    ], { duration: finePointer.matches ? 4400 : 1200 });
    play(element.querySelector('.hero-note'), [
      { opacity: 0, transform: 'translateY(15px) rotate(3deg)' },
      { opacity: 1, transform: 'translateY(0) rotate(-3deg)' }
    ], { delay: 450, duration: 900 });
  }

  function enterProject(card) {
    rise(card.querySelector('.work-copy'), 100);
    rise(card.querySelector('.work-number'), 100);
    const wordmark = card.querySelector('.market-wordmark');
    if (wordmark) {
      play(wordmark, [
        { opacity: 0, transform: 'translateX(-26px)' },
        { opacity: 1, transform: 'translateX(0)' }
      ], { duration: 850 });
      play(wordmark.querySelector('span'), [
        { transform: 'translateY(22px) rotate(-7deg) scale(.96)' },
        { transform: 'translateY(0) rotate(0deg) scale(1)' }
      ], { delay: 180, duration: 950 });
      countUp(card.querySelector('.work-metric strong'));
    }
    const symbol = card.querySelector('.project-type span');
    const from = card.classList.contains('work-card-2')
      ? 'translate(-25px, 25px) rotate(-12deg)'
      : card.classList.contains('work-card-3')
        ? 'rotate(-135deg) scale(.65)'
        : 'rotate(-90deg) scale(.3)';
    play(symbol, [
      { opacity: 0, transform: from },
      { opacity: 1, transform: 'translate(0, 0) rotate(0deg) scale(1)' }
    ], { delay: 130, duration: 1150 });
  }

  // Animate on entry, never hide content while waiting for JS or an observer.
  const entranceObserver = new IntersectionObserver(entries => {
    entries.forEach(({ target, isIntersecting }) => {
      if (!isIntersecting || seen.has(target)) return;
      seen.add(target);
      entranceObserver.unobserve(target);
      if (reduced.matches || document.hidden) return;
      target.classList.add('motion-entered');
      if (target.classList.contains('home-hero')) enterHero(target);
      else if (target.classList.contains('work-card')) enterProject(target);
      else rise(target, Number(target.dataset.motionDelay || 0));
    });
  }, { threshold: .08, rootMargin: '0px 0px -24px 0px' });

  document.querySelectorAll([
    '.home-hero', '.section-heading', '.service-row', '.work-card', '.price-tile',
    '.included-band', '.guarantee-item', '.solution-row', '.faq-intro', '.faq-item',
    '.about-item', '.contact-section h2', '.contact-bottom', '.blog-card',
    '.landing-page .section-head', '.scenario-card', '.process-card'
  ].join(',')).forEach(element => {
    // Small stagger within repeated rows; never delay a control more than 120ms.
    const siblings = [...element.parentElement.children];
    element.dataset.motionDelay = String((siblings.indexOf(element) % 3) * 60);
    entranceObserver.observe(element);
  });

  const clamp = (value, limit) => Math.max(-limit, Math.min(limit, value));
  function resetSurface(surface) {
    pendingPointers.delete(surface);
    ['--motion-x', '--motion-y', '--motion-turn', '--motion-scroll'].forEach(name => {
      surface.style.removeProperty(name);
    });
  }
  function scheduleFrame() {
    if (!frame && !reduced.matches && finePointer.matches && !document.hidden) {
      frame = requestAnimationFrame(updateSurfaces);
    }
  }
  function updateSurfaces() {
    frame = 0;
    if (reduced.matches || !finePointer.matches || document.hidden) return;
    // Read geometry together before changing styles. No permanent animation loop.
    const updates = [...pendingPointers].map(([surface, point]) => {
      const rect = surface.getBoundingClientRect();
      return { surface, x: clamp((point.x - rect.left) / rect.width - .5, .5),
        y: clamp((point.y - rect.top) / rect.height - .5, .5) };
    });
    const heroRect = hero && visibleSurfaces.has(hero) ? hero.getBoundingClientRect() : null;
    updates.forEach(({ surface, x, y }) => {
      const isHero = surface === hero;
      surface.style.setProperty('--motion-x', `${x * (isHero ? 22 : 16)}px`);
      surface.style.setProperty('--motion-y', `${y * (isHero ? 18 : 12)}px`);
      surface.style.setProperty('--motion-turn', `${x * (isHero ? 7 : 2)}deg`);
    });
    pendingPointers.clear();
    if (heroRect) {
      hero.style.setProperty('--motion-scroll', `${clamp(-heroRect.top / innerHeight, 1) * 28}px`);
    }
  }
  const surfaceObserver = new IntersectionObserver(entries => {
    entries.forEach(({ target, isIntersecting }) => {
      if (isIntersecting) visibleSurfaces.add(target);
      else {
        visibleSurfaces.delete(target);
        resetSurface(target);
        const metric = target.querySelector('.work-metric strong');
        if (metric) finishCounter(metric);
      }
    });
  });
  surfaces.forEach(surface => {
    surfaceObserver.observe(surface);
    surface.addEventListener('pointermove', event => {
      if (!finePointer.matches || reduced.matches || !visibleSurfaces.has(surface)) return;
      pendingPointers.set(surface, { x: event.clientX, y: event.clientY });
      scheduleFrame();
    }, { passive: true });
    surface.addEventListener('pointerleave', () => resetSurface(surface));
    surface.addEventListener('pointercancel', () => resetSurface(surface));
  });
  addEventListener('scroll', () => { if (visibleSurfaces.has(hero)) scheduleFrame(); }, { passive: true });
  addEventListener('resize', () => { surfaces.forEach(resetSurface); scheduleFrame(); }, { passive: true });

  function stopMotion() {
    active.forEach(animation => animation.cancel());
    counters.forEach((_, element) => finishCounter(element));
    cancelAnimationFrame(frame);
    frame = 0;
    surfaces.forEach(resetSurface);
  }
  function syncPreferences() {
    document.body.classList.toggle('motion-enabled', !reduced.matches);
    if (reduced.matches || !finePointer.matches) stopMotion();
  }
  reduced.addEventListener('change', syncPreferences);
  finePointer.addEventListener('change', syncPreferences);
  document.addEventListener('visibilitychange', () => { if (document.hidden) stopMotion(); });
  // A keyboard user must never land on a transparent or moving control.
  document.addEventListener('focusin', event => {
    active.forEach(animation => {
      if (animation.effect?.target?.contains(event.target)) animation.cancel();
    });
  });
  syncPreferences();
})();
