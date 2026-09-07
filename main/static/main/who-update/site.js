(() => {
  'use strict';
  const menu = document.querySelector('.menu-toggle');
  const mobileNav = document.querySelector('#mobile-nav');
  const closeMenu = () => {
    if (!menu || !mobileNav) return;
    menu.setAttribute('aria-expanded', 'false');
    menu.setAttribute('aria-label', 'Открыть меню');
    mobileNav.hidden = true;
  };
  menu?.addEventListener('click', () => {
    const expanded = menu.getAttribute('aria-expanded') !== 'true';
    menu.setAttribute('aria-expanded', String(expanded));
    menu.setAttribute('aria-label', expanded ? 'Закрыть меню' : 'Открыть меню');
    mobileNav.hidden = !expanded;
  });
  mobileNav?.querySelectorAll('a').forEach(link => link.addEventListener('click', closeMenu));
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && menu?.getAttribute('aria-expanded') === 'true') {
      closeMenu();
      menu.focus();
    }
  });
  window.matchMedia('(min-width: 721px)').addEventListener('change', event => {
    if (event.matches) closeMenu();
  });

  const demo = document.querySelector('.demo');
  if (demo) {
    const modes = {
      delete: {
        index: '01', message: 'Завтра верну 5000',
        event: 'Сообщение удалено', icon: '#i-trash',
        label: 'Алина удалила сообщение',
        saved: 'Завтра верну 5000',
        note: 'В чате исчезло. У вас — нет.'
      },
      edit: {
        index: '02', message: 'Через неделю верну 5000',
        original: 'Завтра верну 5000',
        event: 'Сообщение изменено', icon: '#i-edit',
        label: 'Алина изменила сообщение',
        saved: 'Было: завтра → Стало: через неделю',
        note: 'Срок возврата изменился? Вы заметите.'
      },
      media: {
        index: '03', message: 'Голосовое сообщение · 0:12',
        event: 'Голосовое удалено', icon: '#i-trash',
        label: 'Алина удалила голосовое',
        saved: 'Голосовое сообщение · 0:12',
        note: 'Копия приходит после удаления.'
      }
    };
    const tabs = [...demo.querySelectorAll('[role="tab"]')];
    const replay = demo.querySelector('.replay-button');
    const panel = demo.querySelector('[role="tabpanel"]');
    const message = demo.querySelector('#demo-message');
    const eventLabel = demo.querySelector('#demo-event');
    const status = demo.querySelector('#demo-status');
    const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
    let current = 'delete';
    let timer;
    function render(state) {
      const data = modes[current];
      demo.dataset.state = state;
      message.replaceChildren(document.createTextNode(state === 'before' && data.original ? data.original : data.message));
      const time = document.createElement('span');
      time.textContent = '14:32';
      message.append(time);
      demo.querySelector('#saved-label').textContent = data.label;
      demo.querySelector('#saved-text').textContent = data.saved;
      eventLabel.querySelector('use').setAttribute('href', data.icon);
      eventLabel.lastChild.textContent = ` ${data.event}`;
      demo.querySelector('.demo-note').textContent = data.note;
      replay.disabled = state === 'before';
      if (state === 'after') status.textContent = `${data.event}. WhoUpdate: ${data.saved}`;
    }
    function play() {
      window.clearTimeout(timer);
      status.textContent = '';
      render('before');
      timer = window.setTimeout(() => render('after'), reducedMotion.matches ? 0 : 1100);
    }
    function select(mode, focus = false) {
      current = mode;
      demo.dataset.mode = mode;
      tabs.forEach(tab => {
        const active = tab.dataset.mode === mode;
        tab.setAttribute('aria-selected', String(active));
        tab.tabIndex = active ? 0 : -1;
        if (active && focus) tab.focus();
      });
      panel.setAttribute('aria-labelledby', `tab-${mode}`);
      demo.querySelector('.demo-topline > span:last-child').textContent = `${modes[mode].index} / 03`;
      play();
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener('click', () => select(tab.dataset.mode));
      tab.addEventListener('keydown', event => {
        let next;
        if (event.key === 'ArrowRight') next = (index + 1) % tabs.length;
        if (event.key === 'ArrowLeft') next = (index + tabs.length - 1) % tabs.length;
        if (event.key === 'Home') next = 0;
        if (event.key === 'End') next = tabs.length - 1;
        if (next !== undefined) {
          event.preventDefault();
          select(tabs[next].dataset.mode, true);
        }
      });
    });
    replay.addEventListener('click', play);
  }

  const dialog = document.querySelector('#video-dialog');
  if (dialog) {
    const video = dialog.querySelector('video');
    const fallback = dialog.querySelector('.video-fallback');
    const close = dialog.querySelector('.dialog-close');
    let opener;
    let previousOverflow;
    document.querySelectorAll('[data-demo-video]').forEach(button => {
      button.addEventListener('click', () => {
        opener = button;
        dialog.querySelector('#video-title').textContent = button.dataset.demoTitle;
        const source = button.dataset.demoVideo;
        video.src = source;
        fallback.hidden = true;
        fallback.querySelector('a').href = source;
        video.load();
        previousOverflow = document.body.style.overflow;
        document.body.style.overflow = 'hidden';
        dialog.showModal();
        close.focus();
        // Playback remains under the visitor's control.
      });
    });
    close.addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
    dialog.addEventListener('close', () => {
      video.pause();
      video.removeAttribute('src');
      video.load();
      document.body.style.overflow = previousOverflow || '';
      opener?.focus();
    });
    video.addEventListener('error', () => {
      if (dialog.open && video.hasAttribute('src')) fallback.hidden = false;
    });
  }
})();
