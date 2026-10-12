(() => {
  'use strict';
  const tg = window.Telegram?.WebApp;
  const $ = id => document.getElementById(id);
  let current, spinning = false, clockOffset = 0, wheelAngle = 0;
  const canSpinNow = () => current && (current.can_spin || Date.parse(current.next_spin_at) <= Date.now() + clockOffset);
  const countdown = seconds => [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(n => String(n).padStart(2, '0')).join(':');
  const rubles = amount => `${Number(amount).toLocaleString('ru-RU', {maximumFractionDigits: 2})} ₽`;
  const text = (tag, content, className) => {
    const el = document.createElement(tag); el.textContent = content;
    if (className) el.className = className;
    return el;
  };
  function changed(parent, previous, next) {
    if (previous !== next) { parent.append(text('s', previous), document.createTextNode(' ')); }
    parent.append(document.createTextNode(next));
  }
  async function api(action) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(`/bot/wheel/api/${action}/`, {
        method: 'POST', headers: {Authorization: `tma ${tg?.initData || ''}`},
        signal: controller.signal, cache: 'no-store', credentials: 'omit',
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Не удалось загрузить колесо. Открой его заново.');
      clockOffset = Date.parse(data.server_time) - Date.now();
      return data;
    } finally { clearTimeout(timeout); }
  }
  function tick() {
    if (current && !spinning) {
      const available = canSpinNow();
      $('spin').disabled = !available;
      $('spin').textContent = available ? 'Крутить бесплатно ↗' : 'Сегодня уже крутили';
      const wait = Math.max(0, Math.ceil((Date.parse(current.next_spin_at) - Date.now() - clockOffset) / 1000));
      $('spin-note').textContent = available ? '1 вращение в день · новая попытка в 00:00 МСК' : `Следующее вращение через ${countdown(wait)} · в 00:00 МСК`;
    }
    if (!current?.reward || current.reward.status !== 'active') return;
    const remaining = Math.max(0, Math.ceil((Date.parse(current.reward.expires_at) - Date.now() - clockOffset) / 1000));
    if (current.reward.reserved_plan) {
      $('timer').textContent = 'Бонус закреплён за созданной оплатой'; return;
    }
    if (!remaining) {
      $('timer').textContent = 'Время бонуса истекло';
      for (const el of $('plans').children) { el.removeAttribute('href'); el.setAttribute('aria-disabled', 'true'); }
      return;
    }
    const hh = String(Math.floor(remaining / 3600)).padStart(2, '0');
    const mm = String(Math.floor(remaining % 3600 / 60)).padStart(2, '0');
    const ss = String(remaining % 60).padStart(2, '0');
    $('timer').textContent = `Бонус действует ещё ${hh}:${mm}:${ss}`;
  }
  function render(data) {
    current = data;
    $('gate').hidden = true; $('app').hidden = false;
    const reward = data.reward;
    $('spin').parentElement.hidden = false;
    if (!reward) { tick(); return; }
    $('result').hidden = false; $('tariffs').hidden = Boolean(reward.instant_days);
    $('reward-label').textContent = reward.label;
    if (reward.instant_days) {
      $('plans').replaceChildren();
      $('timer').hidden = true;
      $('result-caption').textContent = reward.status === 'granted' ? 'ДНИ УЖЕ НАЧИСЛЕНЫ!' : 'ДНИ ДОСТУПА В ПОДАРОК';
      const until = reward.access_expires_at_after ? new Date(reward.access_expires_at_after).toLocaleString('ru-RU', {
        timeZone: 'Europe/Moscow', day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit',
      }) : '';
      $('reward-note').textContent = reward.status === 'granted'
        ? (reward.access_unlimited ? 'Твой бессрочный доступ сохранён.' : `Доступ продлён до ${until} МСК. Ничего оплачивать не нужно.`)
        : 'Этот приз не требует оплаты. Если дни ещё не начислены, обратись в поддержку.';
      tick();
      return;
    }
    $('result-caption').textContent = reward.status === 'used' ? 'БОНУС ИСПОЛЬЗОВАН' : reward.status === 'expired' ? 'ВРЕМЯ БОНУСА ИСТЕКЛО' : 'ДА, ЭТО ТВОЙ ПРИЗ!';
    $('reward-note').textContent = reward.status === 'active' ? 'Выбирай срок ниже — подарок уже в тарифах.' : 'Ниже — действующие тарифы без бонуса.';
    $('timer').hidden = reward.status !== 'active';
    document.querySelector('.section-heading span').textContent = reward.status === 'active' ? 'Бонус уже учтён' : 'Обычные тарифы';
    $('plans').replaceChildren();
    for (const plan of data.plans) {
      const card = document.createElement('a'); card.className = 'plan'; card.href = plan.checkout_url;
      const top = text('div', '', 'plan-top'), name = text('div', '', 'plan-name'), price = text('div', '', 'plan-price');
      changed(name, plan.original_label, plan.label);
      changed(price, rubles(plan.original_amount), rubles(plan.amount));
      top.append(name, price);
      const buy = text('span', '', 'buy');
      buy.append(document.createTextNode(reward.reserved_plan === plan.id ? 'Продолжить: ' : 'Выбрать: '));
      changed(buy, plan.original_label, plan.label); buy.append(document.createTextNode(' · '));
      changed(buy, rubles(plan.original_amount), rubles(plan.amount));
      card.append(top, buy);
      if (reward.status === 'active' && reward.reserved_plan && reward.reserved_plan !== plan.id) {
        card.removeAttribute('href'); card.setAttribute('aria-disabled', 'true');
        buy.textContent = 'Бонус закреплён за другим тарифом';
      }
      card.addEventListener('click', event => {
        if (!card.hasAttribute('href')) { event.preventDefault(); return; }
        // Let checkout navigation handle errors in the same WebView; no payment is made automatically.
      });
      $('plans').append(card);
    }
    tick();
  }
  function failure(error) {
    $('error').textContent = error.name === 'AbortError' ? 'Ответ задержался. Нажми ещё раз — сохранённый приз не изменится.' : error.message || 'Ошибка сети. Попробуй ещё раз.';
    $('error').hidden = false;
  }
  $('spin').addEventListener('click', async () => {
    if (spinning || !canSpinNow()) return;
    spinning = true; $('spin').disabled = true; $('error').hidden = true;
    $('spin').textContent = 'Выбираем твой бонус…';
    try {
      const data = await api('spin');
      const target = 360 - (data.reward.index * 60 + 30);
      const angle = wheelAngle + 360 * 6 + ((target - wheelAngle % 360 + 360) % 360);
      const duration = matchMedia('(prefers-reduced-motion: reduce)').matches ? 150 : 4700;
      tg?.HapticFeedback?.impactOccurred('light');
      const animation = $('wheel').animate([{transform: `rotate(${wheelAngle}deg)`}, {transform: `rotate(${angle}deg)`}], {
        duration, easing: 'cubic-bezier(0.12, 0.74, 0.13, 1)', fill: 'forwards',
      });
      await animation.finished;
      wheelAngle = angle % 360;
      $('wheel').style.transform = `rotate(${wheelAngle}deg)`; animation.cancel();
      render(data); tg?.HapticFeedback?.notificationOccurred('success');
      $('result').scrollIntoView({behavior: duration === 150 ? 'instant' : 'smooth', block: 'nearest'});
    } catch (error) {
      failure(error); $('spin').disabled = false; $('spin').textContent = 'Попробовать ещё раз ↗';
    } finally { spinning = false; tick(); }
  });
  async function start() {
    tg?.ready(); tg?.expand();
    try {
      const data = await api('state'); render(data);
      if (data.reward) {
        wheelAngle = 360 - (data.reward.index * 60 + 30);
        $('wheel').style.transform = `rotate(${wheelAngle}deg)`;
      }
    } catch (error) {
      $('gate').replaceChildren(text('h1', 'Колесо пока закрыто'), text('p', error.message || 'Открой приложение из меню бота в Telegram.'));
      const retry = text('button', 'Попробовать ещё раз'); retry.addEventListener('click', start); $('gate').append(retry);
    }
  }
  setInterval(tick, 1000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && current && !spinning) api('state').then(render).catch(failure);
  });
  start();
})();
