'use strict';
(() => {
  const $ = (s, r = document) => r.querySelector(s),
    $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  } [c]));
  const number = v => new Intl.NumberFormat('ru-RU', {
    maximumFractionDigits: 0
  }).format(Number(v) || 0);
  const rub = v => new Intl.NumberFormat('ru-RU', {
    style: 'currency',
    currency: 'RUB',
    maximumFractionDigits: Number(v) % 1 ? 2 : 0
  }).format(Number(v) || 0);
  const date = (v, time = false) => v ? new Intl.DateTimeFormat('ru-RU', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    ...(time ? {
      hour: '2-digit',
      minute: '2-digit'
    } : {}),
    timeZone: 'Europe/Moscow'
  }).format(new Date(v)) : '—';
  const dayISO = (offset = 0) => {
    const d = new Date(Date.now() + offset * 864e5);
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Europe/Moscow'
    }).format(d)
  };
  const paths = {
    overview: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    payments: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 10h18M7 15h3"/>',
    referrals: '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2M16 5a3 3 0 0 1 0 6M17 14a5 5 0 0 1 4 4v2"/>',
    tokens: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 15v2"/>',
    push: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5M19 9a8 8 0 0 0-13-4L4 7M5 15a8 8 0 0 0 13 4l2-2"/>',
    trend: '<path d="m3 17 6-6 4 4 8-10M15 5h6v6"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>'
  };
  const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name]||paths.overview}</svg>`;
  const tabs = {
    overview: ['Обзор', 'Обзор проекта', 'Главное о вашем проекте в цифрах.'],
    payments: ['Оплаты', 'Оплаты и выручка', 'Каждая покупка, её источник и реферальное начисление.'],
    referrals: ['Рефералы', 'Реферальная программа', 'Начисления, остатки и пользователи, которые привели покупки.'],
    tokens: ['Доступы', 'Доступы и токены', 'Текущие и предстоящие доступы пользователей.'],
    push: ['Пуши', 'Всегда в курсе', 'События двух проектов прямо на ваш телефон.']
  };
  const url = new URL(location.href);
  const state = {
    project: url.searchParams.get('project') === 'ghost' ? 'ghost' : 'who',
    tab: tabs[url.searchParams.get('tab')] ? url.searchParams.get('tab') : 'overview',
    from: dayISO(-29),
    to: dayISO(),
    page: 1,
    q: '',
    status: 'all',
    type: 'real',
    dailyPage: 1,
    refq: '',
    allPartners: false,
    payoutStatus: 'unpaid',
    payoutQ: '',
    payoutPage: 1
  };
  let generation = 0,
    currentData = null,
    swReady = null,
    refreshTimer, toastTimer, formPayload = null,
    selectedUser = null,
    creationBusy = false,
    payoutBusy = false;
  const statuses = {
    paid: 'Оплачен',
    pending: 'Ожидает',
    canceled: 'Отменён',
    failed: 'Ошибка',
    active: 'Активен',
    future: 'Предстоящий',
    expired: 'Завершён',
    disabled: 'Отключён'
  };
  const badge = s => `<span class="badge ${esc(s)}">${esc(statuses[s]||s)}</span>`;
  const adminLink = (href, label = 'Открыть ↗') => `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(label)}</a>`;
  const empty = (title = 'Пока нет данных', text = 'Попробуйте выбрать другой период или изменить фильтры.') => `<div class="empty"><b>${title}</b>${text}</div>`;

  function toast(text) {
    clearTimeout(toastTimer);
    $('#toast').textContent = text;
    $('#toast').hidden = false;
    toastTimer = setTimeout(() => $('#toast').hidden = true, 5000)
  }

  function alert(text = '') {
    $('#page-alert').textContent = text;
    $('#page-alert').hidden = !text
  }

  function csrf() {
    return document.cookie.split('; ').find(x => x.startsWith('csrftoken='))?.split('=')[1] || ''
  }
  async function api(path, options = {}) {
    const response = await fetch('/panel/' + path, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': csrf(),
        ...options.headers
      }
    });
    if (response.status === 401) {
      location.assign('/panel/login/');
      throw Error('Войдите ещё раз.')
    }
    let body;
    try {
      body = await response.json()
    } catch {
      throw Error('Сервер временно недоступен. Попробуйте обновить.')
    }
    if (!response.ok) {
      const e = new Error(body.error || 'Не удалось выполнить запрос.');
      e.fields = body.fields;
      e.status = response.status;
      throw e
    }
    return body
  }

  function metric(title, value, foot, kind = 'trend', featured = false) {
    return `<div class="metric${featured?' featured':''}"><div class="metric-top"><span>${title}</span>${icon(kind)}</div><div class="metric-value">${value}</div><div class="metric-foot">${foot}</div></div>`
  }

  function navigation() {
    for (const id of ['desktop-nav', 'mobile-nav']) $('#' + id).innerHTML = Object.entries(tabs).map(([key, [name]]) => `<button class="nav-button${key===state.tab?' selected':''}" data-tab="${key}" ${key===state.tab?'aria-current="page"':''}>${icon(key)}<span>${key==='tokens'&&state.project==='ghost'?'Токены':name}</span></button>`).join('');
    $$('[data-tab]').forEach(b => b.onclick = () => navigate(b.dataset.tab));
    $$('[data-project]').forEach(b => {
      b.classList.toggle('selected', b.dataset.project === state.project);
      b.setAttribute('aria-pressed', b.dataset.project === state.project);
      b.onclick = () => {
        if (state.project === b.dataset.project) return;
        state.project = b.dataset.project;
        resetFilters();
        navigation();
        load()
      }
    });
    $('#refresh').innerHTML = icon('refresh')
  }

  function resetFilters() {
    state.page = 1;
    state.q = '';
    state.refq = '';
    state.status = state.tab === 'tokens' ? (state.project === 'ghost' ? 'future' : 'active') : 'all';
    state.dailyPage = 1;
    state.allPartners = false;
    state.payoutStatus = 'unpaid';
    state.payoutQ = '';
    state.payoutPage = 1
  }

  function navigate(tab) {
    state.tab = tab;
    resetFilters();
    navigation();
    load();
    const scroller = matchMedia('(max-width: 900px)').matches ? $('.workspace') : window;
    scroller.scrollTo({
      top: 0,
      behavior: 'instant'
    })
  }

  function heading() {
    const data = tabs[state.tab];
    $('#eyebrow').textContent = `${state.project==='who'?'WHO-UPDATE':'GHOSTNOTE'} / ${state.tab==='push'?'УВЕДОМЛЕНИЯ':'АНАЛИТИКА'}`;
    $('#page-title').textContent = state.tab === 'tokens' ? (state.project === 'ghost' ? 'Токены GhostNote' : 'Доступы Who-Update') : data[1];
    $('#page-description').textContent = data[2];
    $('#period').hidden = !['overview', 'payments'].includes(state.tab);
    $('#period').style.display = $('#period').hidden ? 'none' : '';
    $('#heading-action').innerHTML = state.tab === 'tokens' && state.project === 'ghost' ? `<button class="primary" id="create-token">${icon('plus')}Создать токен</button>` : state.tab === 'referrals' ? '<span class="all-time">За всё время</span>' : '';
    if ($('#create-token')) $('#create-token').onclick = openToken;
    const u = new URL(location.href);
    u.searchParams.set('project', state.project);
    u.searchParams.set('tab', state.tab);
    history.replaceState(null, '', u);
    document.title = `${$('#page-title').textContent} · Control`
  }

  function params() {
    return new URLSearchParams({
      from: state.from,
      to: state.to,
      page: state.page,
      q: state.q,
      status: state.status,
      type: state.type
    })
  }
  async function load(quiet = false) {
    if (payoutBusy && state.project === 'ghost' && state.tab === 'referrals') return;
    const ticket = ++generation;
    heading();
    alert();
    if (!quiet) $('#content').innerHTML = '<div class="loading"><span class="spinner"></span>Загружаем данные…</div>';
    $('#refresh').disabled = true;
    try {
      let data;
      if (state.tab === 'push') {
        data = await api('api/push/')
      } else {
        data = await api(`api/${state.project}/${['overview','payments'].includes(state.tab)?'dashboard':state.tab}/?${params()}`)
      }
      if (ticket !== generation) return;
      currentData = data;
      if (state.tab === 'overview') renderOverview(data);
      if (state.tab === 'payments') renderPayments(data);
      if (state.tab === 'referrals') renderReferrals(data);
      if (state.tab === 'tokens') renderTokens(data);
      if (state.tab === 'push') await renderPush(data);
      $('#updated').textContent = 'Обновлено ' + new Intl.DateTimeFormat('ru-RU', {
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'Europe/Moscow'
      }).format(new Date());
    } catch (e) {
      if (ticket !== generation) return;
      alert(e.message);
      if (!quiet) $('#content').innerHTML = empty('Не удалось загрузить данные', 'Нажмите кнопку обновления, чтобы повторить.')
    } finally {
      if (ticket === generation) $('#refresh').disabled = false
    }
  }

  function chart(id, title, subtitle, series, lines, moneyChart = false) {
    return `<section class="card"><div class="card-header"><div><h2>${title}</h2><p>${subtitle}</p></div></div><div class="chart-wrap" id="${id}" data-chart><svg viewBox="0 0 560 245" role="img" aria-label="${title}. Точные значения в таблице по дням."></svg><div class="tooltip" hidden></div></div><div class="chart-legend">${lines.map(l=>`<span><i style="background:${l.color}"></i>${l.name}</span>`).join('')}</div></section>`
  }

  function drawChart(id, series, lines, isMoney = false) {
    const el = $('#' + id);
    if (!el) return;
    const svg = $('svg', el);
    const width = 560,
      height = 245,
      left = 48,
      right = 16,
      top = 15,
      bottom = 32,
      plotW = width - left - right,
      plotH = height - top - bottom;
    const groupSize = Math.max(1, Math.ceil(series.length / 45));
    let data = [];
    for (let i = 0; i < series.length; i += groupSize) {
      const group = series.slice(i, i + groupSize),
        row = {
          date: group[0].date,
          end: group[group.length - 1].date
        };
      for (const l of lines) row[l.key] = group.reduce((n, r) => n + Number(r[l.key] || 0), 0);
      data.push(row)
    }
    const max = Math.max(1, ...data.flatMap(r => lines.map(l => r[l.key])));
    const ceiling = max <= 5 ? 5 : Math.ceil(max / (10 ** Math.floor(Math.log10(max)))) * (10 ** Math.floor(Math.log10(max)));
    const step = plotW / Math.max(1, data.length),
      x = i => left + step * (i + .5),
      y = v => top + plotH - (v / ceiling) * plotH;
    let html = '';
    for (let i = 0; i <= 4; i++) {
      const v = ceiling * i / 4;
      html += `<line class="chart-gridline" x1="${left}" x2="${width-right}" y1="${y(v)}" y2="${y(v)}"/><text class="chart-axis" x="${left-9}" y="${y(v)+3}" text-anchor="end">${v>=1000?number(v/1000)+'к':number(v)}</text>`
    }
    lines.forEach((l, li) => {
      if (li === 0) {
        data.forEach((r, i) => {
          let bw = Math.min(20, step * .62);
          html += `<rect x="${x(i)-bw/2}" y="${y(r[l.key])}" width="${bw}" height="${Math.max(0,top+plotH-y(r[l.key]))}" rx="${Math.min(3,bw/2)}" fill="${l.color}" opacity=".86"/>`
        })
      } else {
        html += `<polyline points="${data.map((r,i)=>`${x(i)},${y(r[l.key])}`).join(' ')}" fill="none" stroke="${l.color}" stroke-width="2" stroke-linejoin="round"/>`;
        if (data.length === 1) html += `<circle cx="${x(0)}" cy="${y(data[0][l.key])}" r="3" fill="${l.color}"/>`
      }
    });
    data.forEach((r, i) => {
      if (i === 0 || i === data.length - 1 || (data.length > 6 && i % Math.ceil(data.length / 5) === 0)) {
        html += `<text class="chart-axis" x="${x(i)}" y="${height-8}" text-anchor="middle">${esc(date(r.date).slice(0,5))}</text>`
      }
      const text = (r.date === r.end ? date(r.date) : date(r.date) + ' — ' + date(r.end)) + ': ' + lines.map(l => `${l.name} ${isMoney?rub(r[l.key]):number(r[l.key])}`).join(', ');
      html += `<rect class="chart-hit" data-index="${i}" x="${left+i*step}" y="${top}" width="${step}" height="${plotH}" fill="transparent" tabindex="0" role="graphics-symbol" aria-label="${esc(text)}"><title>${esc(text)}</title></rect>`
    });
    svg.innerHTML = html;
    const tip = $('.tooltip', el);
    $$('.chart-hit', svg).forEach(hit => {
      const show = () => {
        const i = Number(hit.dataset.index),
          r = data[i];
        tip.innerHTML = `<b>${esc(date(r.date))}${r.end!==r.date?' — '+esc(date(r.end)):''}</b><br>${lines.map(l=>`${l.name}: ${isMoney?rub(r[l.key]):number(r[l.key])}`).join('<br>')}`;
        tip.hidden = false;
        tip.style.left = Math.max(5, Math.min(el.clientWidth - 215, (x(i) / width) * el.clientWidth - 70)) + 'px';
        tip.style.top = '7px'
      };
      hit.addEventListener('pointerenter', show);
      hit.addEventListener('focus', show);
      hit.addEventListener('click', show);
      hit.addEventListener('pointerleave', () => tip.hidden = true);
      hit.addEventListener('blur', () => tip.hidden = true)
    });
    if (groupSize > 1) {
      const p = document.createElement('span');
      p.textContent = `Суммы по ${groupSize} дн.`;
      el.nextElementSibling.appendChild(p)
    }
  }
  const moneyLines = [{
    key: 'revenue',
    name: 'Выручка',
    color: '#4d68df'
  }, {
    key: 'referral_revenue',
    name: 'С рефералов',
    color: '#78c8b2'
  }];
  const userLines = [{
    key: 'connected',
    name: 'Подключения',
    color: '#849cdb'
  }, {
    key: 'started',
    name: '/start',
    color: '#253c65'
  }];

  function renderOverview(data) {
    const s = data.summary,
      tot = k => data.series.reduce((a, r) => a + r[k], 0);
    $('#content').innerHTML = `<div class="metric-grid">${metric('Выручка',rub(s.revenue),`${number(s.payment_count)} оплаченных покупок`,'payments',true)}${metric('Новые подключения',number(tot('connected')),state.project==='who'?'По дням, как в /stat':'Первый запуск программы','referrals')}${metric('Переходы к оплате',number(s.checkout_count),'За выбранный период','arrow')}${metric('С рефералов',rub(s.referral_revenue),`Начислено партнёрам ${rub(s.commission)}`,'trend')}</div><div class="chart-grid">${chart('revenue-chart','Динамика выручки','Оплаченные покупки за выбранный период',data.series,moneyLines,true)}${chart('users-chart','Новые подключения','Подключения и запуски бота по дням',data.series,userLines)}</div><div class="mini-grid"><div class="mini-stat"><small>Пользователей всего</small><b>${number(s.users_total)}</b></div><div class="mini-stat"><small>Активных ${state.project==='who'?'доступов':'токенов'}</small><b>${number(s.active_tokens)}</b></div><div class="mini-stat"><small>${state.project==='who'?'Подключено сейчас':'Предстоящих токенов'}</small><b>${number(state.project==='who'?s.connected_total:s.upcoming_tokens)}</b></div></div><section class="card"><div class="card-header"><div><h2>Статистика по дням</h2><p>Выберите один день в периоде, чтобы увидеть его отдельно.</p></div></div><div id="daily-table"></div><p class="note">${esc(data.definitions)}</p></section><p class="note">${esc(data.checkout_note)}</p>`;
    drawChart('revenue-chart', data.series, moneyLines, true);
    drawChart('users-chart', data.series, userLines);
    dailyTable(data.series)
  }

  function dailyTable(series) {
    const rows = [...series].reverse(),
      start = (state.dailyPage - 1) * 7,
      page = rows.slice(start, start + 7),
      who = state.project === 'who';
    $('#daily-table').innerHTML = `<div class="table-scroll"><table><thead><tr><th>День</th>${who?'<th>Посетители</th><th>Просмотры</th>':'<th>Новые пользователи</th>'}<th>/start</th><th>${who?'Подключились':'Первый запуск'}</th>${who?'<th>Отключились</th>':''}<th>Доступ истёк</th><th>Переходы к оплате</th><th>Оплаты</th><th>Выручка</th></tr></thead><tbody>${page.map(r=>`<tr><td><b>${date(r.date)}</b></td>${who?`<td>${number(r.visitors)}</td><td>${number(r.views)}</td>`:`<td>${number(r.new_users)}</td>`}<td>${number(r.started)}</td><td>${number(r.connected)}</td>${who?`<td>${number(r.disconnected)}</td>`:''}<td>${number(r.expired)}</td><td>${number(r.checkouts)}</td><td>${number(r.payments)}</td><td><b>${rub(r.revenue)}</b></td></tr>`).join('')}</tbody></table></div><div class="pager"><span>${rows.length} дн. · свежие сверху</span><button id="days-prev" ${state.dailyPage===1?'disabled':''}>←</button><span>${state.dailyPage} / ${Math.ceil(rows.length/7)||1}</span><button id="days-next" ${start+7>=rows.length?'disabled':''}>→</button></div>`;
    $('#days-prev').onclick = () => {
      state.dailyPage--;
      dailyTable(series)
    };
    $('#days-next').onclick = () => {
      state.dailyPage++;
      dailyTable(series)
    }
  }

  function pagination(data) {
    return `<div class="pager"><span>${number(data.total)} записей</span><button id="prev-page" ${data.page<=1?'disabled':''}>← Назад</button><span>${data.page} / ${Math.ceil(data.total/50)||1}</span><button id="next-page" ${!data.has_more?'disabled':''}>Далее →</button></div>`
  }

  function bindPagination() {
    if ($('#prev-page')) $('#prev-page').onclick = () => {
      state.page--;
      load()
    };
    if ($('#next-page')) $('#next-page').onclick = () => {
      state.page++;
      load()
    }
  }

  function bindFilters() {
    const search = $('#search-form');
    if (search) search.onsubmit = e => {
      e.preventDefault();
      state.q = $('#search').value;
      state.page = 1;
      load()
    };
    const status = $('#status-filter');
    if (status) status.onchange = () => {
      state.status = status.value;
      state.page = 1;
      load()
    };
    const type = $('#type-filter');
    if (type) type.onchange = () => {
      state.type = type.value;
      state.page = 1;
      load()
    };
    bindPagination()
  }

  function selectStatus(values) {
    return `<select id="status-filter" aria-label="Статус"><option value="all">Все статусы</option>${values.map(v=>`<option value="${v}"${state.status===v?' selected':''}>${statuses[v]}</option>`).join('')}</select>`
  }

  function searchForm(extra = '') {
    return `<form class="table-toolbar" id="search-form"><label class="sr-only" for="search">Поиск пользователя</label><input id="search" type="search" placeholder="Поиск по пользователю или ID" value="${esc(state.q)}">${extra}<button class="small-button" type="submit">Найти</button></form>`
  }

  function paymentTable(p) {
    return p.rows.length ? `<div class="table-scroll"><table><thead><tr><th>Дата</th><th>Покупатель</th><th>Сумма</th><th>Статус</th><th>Пригласил</th><th>Начислено</th><th>Тариф / источник</th><th></th></tr></thead><tbody>${p.rows.map(r=>`<tr><td>${date(r.at,true)}</td><td><b>${esc(r.user||'Без имени')}</b><small>${esc(String(r.id).slice(0,12))}</small></td><td><b>${rub(r.amount)}</b></td><td>${badge(r.status)}</td><td>${esc(r.referrer||'—')}</td><td>${Number(r.commission)?rub(r.commission):'—'}</td><td>${esc(r.plan)}<small>${esc(r.source)}</small></td><td>${adminLink(r.admin_url)}</td></tr>`).join('')}</tbody></table></div>` : empty('Покупок не найдено')
  }

  function renderPayments(d) {
    const s = d.summary;
    $('#content').innerHTML = `<div class="metric-grid">${metric('Выручка',rub(s.revenue),'Только оплаченные покупки','payments',true)}${metric('Покупок',number(s.payment_count),`Средний чек ${rub(s.average)}`,'trend')}${metric('С рефералов',rub(s.referral_revenue),'Входит в общую выручку','referrals')}${metric('Начислено партнёрам',rub(s.commission),'По покупкам за этот период','referrals')}</div>${chart('payments-chart','Оплаты и переходы','Количество событий за выбранный период',d.series,[])}<section class="card"><div class="card-header"><div><h2>Все покупки</h2><p>Фильтры ниже меняют список. Итоги выше — за весь период.</p></div></div>${searchForm(selectStatus(['paid','pending','canceled','failed']))}${paymentTable(d.payments)}${pagination(d.payments)}</section><p class="note">${esc(d.checkout_note)}</p>`;
    const lines = [{
      key: 'checkouts',
      name: 'Переходы к оплате',
      color: '#bdc9ed'
    }, {
      key: 'payments',
      name: 'Оплаты',
      color: '#3b5bdb'
    }];
    $('.chart-legend').innerHTML = lines.map(l => `<span><i style="background:${l.color}"></i>${l.name}</span>`).join('');
    drawChart('payments-chart', d.series, lines);
    bindFilters()
  }

  function renderReferrals(d) {
    const s = d.totals;
    $('#content').innerHTML = `<div class="metric-grid">${metric('Остаток к выплате',rub(s.due),'Начислено минус выплачено','referrals',true)}${metric('Начислено всего',rub(s.earned),'За всё время','trend')}${metric('Выплачено',rub(s.paid),state.project==='ghost'?'Подтверждённые выплаты':'Отмечено в админке','payments')}${metric(state.project==='who'?'В заявках на вывод':'Партнёров с остатком',state.project==='who'?rub(s.reserved):number(d.partners.filter(p=>Number(p.due)>0).length),state.project==='who'?'Входит в остаток к выплате':'Остаток больше нуля','clock')}</div>${state.project==='who'?`<section class="card"><div class="card-header"><div><h2>Заявки на вывод <span class="muted">${d.requests.length}</span></h2><p>Новые заявки. Открыть и отметить выплату можно в админке.</p></div></div>${d.requests.length?`<div class="request-list">${d.requests.map(r=>`<div class="request-item"><span class="avatar">${esc(r.user.replace('@','').charAt(0).toUpperCase())}</span><div class="request-user"><b>${esc(r.user)}</b><small>№${r.id} · ${date(r.at,true)}</small></div><strong>${rub(r.amount)}</strong>${adminLink(r.admin_url,'К заявке ↗')}</div>`).join('')}</div>`:empty('Новых заявок нет','Начисления партнёров показаны ниже.')}</section>`:'<p class="notice">После перевода партнёру отметьте нужные начисления как выплаченные. Отметки сохраняются в панели и админке.</p>'}<section class="card"><div class="card-header"><div><h2>Кому выплатить</h2><p>Все остатки актуальны на момент обновления.</p></div><button class="link-button" id="toggle-partners">${state.allPartners?'Только с остатком':'Все партнёры'}</button></div><div id="partners-table"></div></section>${state.project==='ghost'?ghostPayoutSection():''}<section class="card"><div class="card-header"><div><h2>Приглашённые пользователи</h2><p>Кто кого привёл и сколько принесли покупки.</p></div></div><div class="table-toolbar"><input id="referral-search" type="search" aria-label="Поиск реферала или партнёра" placeholder="Реферал или пригласивший" value="${esc(state.refq)}"></div><div id="referrals-table"></div></section>`;
    partnersTable(d.partners);
    referralsTable(d.referrals);
    if (state.project === 'ghost') {
      payoutsList(d.payouts || []);
      $('#payout-status').onchange = e => {
        state.payoutStatus = e.target.value;
        state.payoutPage = 1;
        payoutsList(currentData.payouts || [])
      };
      $('#payout-search').oninput = e => {
        state.payoutQ = e.target.value;
        state.payoutPage = 1;
        payoutsList(currentData.payouts || [])
      }
    }
    $('#toggle-partners').onclick = () => {
      state.allPartners = !state.allPartners;
      partnersTable(d.partners);
      $('#toggle-partners').textContent = state.allPartners ? 'Только с остатком' : 'Все партнёры'
    };
    $('#referral-search').oninput = e => {
      state.refq = e.target.value;
      referralsTable(d.referrals)
    }
  }

  function partnersTable(rows) {
    const list = rows.filter(p => state.allPartners || Number(p.due) > 0);
    $('#partners-table').innerHTML = list.length ? `<div class="table-scroll"><table><thead><tr><th>Партнёр</th><th>Начислено</th><th>Выплачено</th><th>Остаток</th>${state.project==='who'?'<th>В заявках</th>':''}<th></th></tr></thead><tbody>${list.map(r=>`<tr><td><b>${esc(r.user)}</b></td><td>${rub(r.earned)}</td><td>${rub(r.paid)}</td><td><b>${rub(r.due)}</b></td>${state.project==='who'?`<td>${rub(r.reserved)}</td>`:''}<td>${adminLink(r.admin_url)}</td></tr>`).join('')}</tbody></table></div>` : empty('Всё выплачено', 'Партнёров с положительным остатком нет.')
  }

  function ghostPayoutSection() {
    return `<section class="card" id="ghost-payouts"><div class="card-header"><div><h2>Выплаты партнёрам</h2><p>Каждая строка — начисление за одну покупку. Галочку можно снять, если отметили по ошибке.</p></div></div><div class="table-toolbar"><input id="payout-search" type="search" aria-label="Поиск выплаты по партнёру или рефералу" placeholder="Партнёр или реферал" value="${esc(state.payoutQ)}"><select id="payout-status" aria-label="Статус выплаты">${[['unpaid','К выплате'],['paid','Выплачено'],['all','Все выплаты']].map(([k,v])=>`<option value="${k}" ${k===state.payoutStatus?'selected':''}>${v}</option>`).join('')}</select></div><div id="payout-list"></div></section>`
  }

  function payoutsList(rows) {
    const q = state.payoutQ.trim().toLocaleLowerCase(),
      filtered = rows.filter(r => (state.payoutStatus === 'all' || r.is_paid === (state.payoutStatus === 'paid')) && (!q || (r.user + ' ' + r.buyer).toLocaleLowerCase().includes(q))),
      pages = Math.max(1, Math.ceil(filtered.length / 20));
    state.payoutPage = Math.min(state.payoutPage, pages);
    const list = filtered.slice((state.payoutPage - 1) * 20, state.payoutPage * 20);
    $('#payout-list').innerHTML = list.length ? `<div class="payout-list">${list.map(r=>`<article class="payout-item${r.is_paid?' is-paid':''}"><div class="payout-person"><b>${esc(r.user)}</b><small>За покупку ${esc(r.buyer)} на ${rub(r.payment_amount)}</small><small>${date(r.at,true)} · Токен №${r.token_id} · ${adminLink(r.admin_url,'Начисление №'+r.id+' ↗')}</small></div><strong class="payout-amount">${rub(r.amount)}</strong><label class="payout-check"><input type="checkbox" data-payout-id="${r.id}" aria-label="Выплачено ${esc(r.user)} ${esc(rub(r.amount))}, начисление ${r.id}" ${r.is_paid?'checked':''} ${payoutBusy?'disabled':''}><span>Выплачено</span></label></article>`).join('')}</div><div class="pager"><span>${number(filtered.length)} начислений · ${state.payoutPage} / ${pages}</span><button class="small-button" id="payout-prev" ${state.payoutPage===1?'disabled':''}>←</button><button class="small-button" id="payout-next" ${state.payoutPage===pages?'disabled':''}>→</button></div>` : empty(state.payoutStatus==='unpaid'&&!q?'Всё выплачено':'Начисления не найдены', 'Смените фильтр, чтобы увидеть остальные выплаты.');
    $$('[data-payout-id]').forEach(box => {
      const row = list.find(r => r.id === Number(box.dataset.payoutId));
      box.onchange = () => savePayout(row, box)
    });
    if ($('#payout-prev')) $('#payout-prev').onclick = () => {state.payoutPage--;payoutsList(rows)};
    if ($('#payout-next')) $('#payout-next').onclick = () => {state.payoutPage++;payoutsList(rows)}
  }

  async function savePayout(row, box) {
    if (payoutBusy) {box.checked = row.is_paid;return}
    payoutBusy = true;
    const ticket = ++generation, desired = box.checked;
    $$('[data-payout-id]').forEach(el => el.disabled = true);
    $('#refresh').disabled = true;
    let refreshNeeded = false;
    try {
      const result = await api('api/ghost-payout/', {method: 'POST', body: JSON.stringify({id: row.id, is_paid: desired, revision: row.revision})});
      if (ticket === generation && state.project === 'ghost' && state.tab === 'referrals') {
        currentData = result.data;
        renderReferrals(currentData)
      }
      toast(desired ? 'Выплата отмечена. Остаток пересчитан.' : 'Отметка снята. Сумма снова к выплате.')
    } catch (e) {
      box.checked = row.is_paid;
      refreshNeeded = true;
      toast(e.message)
    } finally {
      payoutBusy = false;
      $$('[data-payout-id]').forEach(el => el.disabled = false);
      if (ticket === generation) $('#refresh').disabled = false;
      if (state.project === 'ghost' && state.tab === 'referrals' && (refreshNeeded || ticket !== generation)) await load(true)
    }
  }

  function referralsTable(rows) {
    const q = state.refq.trim().toLocaleLowerCase(),
      list = rows.filter(r => !q || (r.user + ' ' + r.referrer).toLocaleLowerCase().includes(q));
    $('#referrals-table').innerHTML = list.length ? `<div class="table-scroll"><table><thead><tr><th>Реферал</th><th>Пригласил</th><th>Покупок</th><th>Сумма покупок</th><th>Начислено партнёру</th><th></th></tr></thead><tbody>${list.map(r=>`<tr><td><b>${esc(r.user)}</b></td><td>${esc(r.referrer)}</td><td>${number(r.purchases)}</td><td><b>${rub(r.revenue)}</b></td><td>${rub(r.commission)}</td><td>${adminLink(r.admin_url)}</td></tr>`).join('')}</tbody></table></div><div class="pager"><span>${number(list.length)} пользователей</span></div>` : empty('Рефералы не найдены')
  }

  function renderGhostTokens(d) {
    const filters = selectStatus(['future','active','expired','disabled']) + `<select id="type-filter" aria-label="Тип токена"><option value="real" ${state.type==='real'?'selected':''}>Реальные</option><option value="test" ${state.type==='test'?'selected':''}>Тестовые</option><option value="all" ${state.type==='all'?'selected':''}>Все типы</option></select>`;
    $('#content').innerHTML = `<p class="notice">Откройте карточку, чтобы скопировать токен, изменить срок или настроить доступ. Время указано по Москве.</p><section class="card">${searchForm(filters)}${d.rows.length?`<div class="token-cards">${d.rows.map(r=>`<article class="token-card"><div class="token-card-heading"><div><b>${esc(r.user||'Без пользователя')}</b><small>№${r.id} · ${r.token_type==='test'?'Тестовый':'Реальный'}${r.token_type==='real'?' · '+rub(r.amount):''}</small></div>${badge(r.status)}</div><dl class="token-dates"><div><dt>Начало</dt><dd>${date(r.starts_at,true)}</dd></div><div><dt>Окончание</dt><dd>${date(r.expires_at,true)}</dd></div></dl><p class="muted token-modes">${[r.allow_local?'Локальный':'',r.allow_remote?'Удалённый':'',r.allow_ai===true?'Нейросеть':''].filter(Boolean).join(' · ')}</p>${r.label&&r.label!==r.user?`<p class="token-note">${esc(r.label)}</p>`:''}<button class="secondary token-manage" data-manage-token="${r.id}">Управлять токеном ${icon('arrow')}</button></article>`).join('')}</div>`:empty(state.status==='future'?'Предстоящих токенов нет':'Токены не найдены','Измените фильтры, чтобы посмотреть остальные записи.')}${pagination(d)}</section>`;
    bindFilters();
    $$('[data-manage-token]').forEach(button => button.onclick = () => openManagedToken(Number(button.dataset.manageToken)))
  }

  function renderTokens(d) {
    const ghost = state.project === 'ghost';
    if (ghost) {renderGhostTokens(d);return}
    $('#content').innerHTML = `${ghost?'<p class="notice">Предстоящие — оплаченные реальные токены, у которых ещё не наступило время начала. Переключайте фильтры, чтобы увидеть активные, завершённые и тестовые.</p>':''}<section class="card">${searchForm(selectStatus(ghost?['future','active','expired','disabled']:['active','expired'])+(ghost?`<select id="type-filter" aria-label="Тип токена"><option value="real" ${state.type==='real'?'selected':''}>Реальные</option><option value="test" ${state.type==='test'?'selected':''}>Тестовые</option><option value="all" ${state.type==='all'?'selected':''}>Все типы</option></select>`:''))}${d.rows.length?`<div class="table-scroll"><table><thead><tr><th>Пользователь</th><th>Статус</th><th>Срок</th>${ghost?'<th>Оплата</th><th>Доступ</th>':''}<th></th></tr></thead><tbody>${d.rows.map(r=>`<tr><td><b>${esc(r.user)}</b><small>${ghost?'Токен №':'ID '}${r.id}${ghost&&r.token_type==='test'?' · тестовый':''}</small></td><td>${badge(r.status)}</td><td>${r.token_type==='unlimited'?'Бессрочно':`${ghost?date(r.starts_at,true)+' →<br>':''}${date(r.expires_at,true)}`}</td>${ghost?`<td>${r.token_type==='test'?'—':rub(r.amount)}</td><td>${[r.allow_local?'Локальный':'',r.allow_remote?'Удалённый':'',r.allow_ai===true?'Нейросеть':''].filter(Boolean).join('<br>')}</td>`:''}<td>${adminLink(r.admin_url)}</td></tr>`).join('')}</tbody></table></div>`:empty(ghost&&state.status==='future'?'Предстоящих токенов нет':'Доступы не найдены','Измените фильтры, чтобы посмотреть остальные записи.')}${pagination(d)}</section>`;
    bindFilters()
  }
  async function subscription() {
    if (!('serviceWorker' in navigator)) return null;
    const r = await navigator.serviceWorker.getRegistration('/panel/');
    return r && r.active ? r.pushManager.getSubscription() : null
  }
  async function renderPush(d) {
    const supported = 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
    const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
    const ios = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    let sub = null;
    try {
      sub = await subscription()
    } catch {}
    if (state.tab !== 'push') return;
    $('#content').innerHTML = `<div class="settings-grid"><section class="card"><div class="card-inner"><h2>Уведомления на этом устройстве</h2><p class="muted">Узнавайте о покупках и завершении доступа, даже когда панель закрыта.</p><div class="push-state"><span class="dot ${sub&&Notification.permission==='granted'?'on':''}"></span>${sub&&Notification.permission==='granted'?'Разрешены на устройстве':'Пока не включены'}</div>${ios&&!standalone?'<div class="notice">На iPhone сначала добавьте панель на экран «Домой» и откройте её с новой иконки.</div>':''}${!supported?'<p class="muted">Здесь Web Push недоступен. На iPhone нужен iOS 16.4 или новее и запуск с экрана «Домой».</p>':Notification.permission==='denied'?'<p class="muted">Уведомления запрещены в настройках. Разрешите их для Control в настройках уведомлений устройства или сайта.</p>':`<button id="enable-push" class="primary">${icon('push')}${sub?'Проверить подключение':'Включить уведомления'}</button> ${sub?'<button id="test-push" class="secondary">Пробный пуш</button><br><button id="disable-push" class="link-button danger-button" style="margin-top:18px">Отключить на этом устройстве</button>':''}`}<ul class="notification-types"><li><span class="type-icon">${icon('payments')}</span><div><b>Who-Update</b><small>Новая оплата и переход к оплате</small></div></li><li><span class="type-icon">${icon('payments')}</span><div><b>GhostNote</b><small>Новая оплата и переход к оплате</small></div></li><li><span class="type-icon">${icon('clock')}</span><div><b>Реальный токен закончился</b><small>GhostNote · завершение срока активного реального токена</small></div></li></ul></div></section><section class="card"><div class="card-inner"><div class="eyebrow">УСТАНОВКА НА IPHONE</div><h2>Панель как приложение</h2><p class="muted">Без App Store и платной подписки разработчика.</p><ol class="steps"><li>Откройте эту страницу в <b>Safari</b>.</li><li>Нажмите <b>«Поделиться» → «На экран Домой»</b>. Если есть переключатель «Открывать как веб-приложение», включите его.</li><li>Запустите <b>Control</b> с новой иконки и войдите.</li><li>Откройте <b>«Пуши»</b>, нажмите «Включить уведомления» и разрешите их.</li></ol><p class="muted">Пуши поддерживаются с iOS 16.4. Для работы с оплатами и создания токенов нужен интернет.</p><hr style="border:0;border-top:1px solid var(--line);margin:24px 0"><h3>Обработка событий</h3>${d.states.length?d.states.map(s=>`<div class="status-line"><span>${s.name==='who'?'Who-Update':'GhostNote'}</span><span>${s.last_error?'Ошибка обновления':s.last_poll_at?date(s.last_poll_at,true):'Ожидает запуска'}</span></div>`).join(''):'<p class="muted">Ожидает первого запуска обработчика.</p>'}<p class="form-help" style="margin-top:12px">Подключено устройств: ${d.enabled_devices}. ${d.failed_deliveries?'Недоставленных пушей: '+d.failed_deliveries+'.':''} Сервер проверяет события примерно раз в 30 секунд. Доставка зависит от сети и настроек телефона.</p></div></section><section class="card settings-wide"><div class="card-inner"><div><h3>Вход администратора</h3><p class="muted">При выходе уведомления для этого устройства отключаются.</p></div><button id="logout" class="secondary danger-button">Выйти из аккаунта</button></div></section></div>`;
    if ($('#enable-push')) $('#enable-push').onclick = enablePush;
    if ($('#test-push')) $('#test-push').onclick = async () => {
      try {
        const s = await subscription();
        await api('api/push-test/', {
          method: 'POST',
          body: JSON.stringify({
            endpoint: s.endpoint
          })
        });
        toast('Пробный пуш поставлен в очередь. Обычно приходит в течение минуты.')
      } catch (e) {
        toast(e.message)
      }
    };
    if ($('#disable-push')) $('#disable-push').onclick = async () => {
      try {
        const s = await subscription();
        await api('api/push/', {
          method: 'DELETE',
          body: JSON.stringify({
            endpoint: s.endpoint
          })
        });
        await s.unsubscribe();
        load()
      } catch (e) {
        toast(e.message)
      }
    };
    $('#logout').onclick = async () => {
      try {
        const s = await subscription();
        await api('logout/', {
          method: 'POST',
          body: JSON.stringify({
            endpoint: s?.endpoint || ''
          })
        });
        if (s) await s.unsubscribe();
        location.assign('/panel/login/')
      } catch (e) {
        toast(e.message)
      }
    }
  }
  async function enablePush() {
    const button = $('#enable-push');
    button.disabled = true;
    try {
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') throw Error('Разрешите уведомления, чтобы получать пуши.');
      const registration = await swReady;
      if (!registration) throw Error('Приложение ещё не готово. Обновите страницу.');
      const conf = await api('api/push/');
      if (!conf.public_key) throw Error('Сервер уведомлений не настроен.');
      let s = await registration.pushManager.getSubscription();
      if (!s) {
        const str = conf.public_key.replace(/-/g, '+').replace(/_/g, '/');
        s = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: Uint8Array.from(atob(str + '='.repeat((4 - str.length % 4) % 4)), c => c.charCodeAt(0))
        })
      }
      await api('api/push/', {
        method: 'POST',
        body: JSON.stringify(s.toJSON())
      });
      toast('Уведомления включены. Можно отправить пробный пуш.');
      load()
    } catch (e) {
      toast(e.message);
      button.disabled = false
    }
  }
  const dialog = $('#token-dialog');
  let managementTicket = 0, tokenListDirty = false;
  dialog.addEventListener('close', () => {
    managementTicket++;
    $('#token-body').innerHTML = '';
    if (tokenListDirty && state.project === 'ghost' && state.tab === 'tokens') load(true);
    tokenListDirty = false
  });
  $('#close-dialog').onclick = () => {
    if (!creationBusy) dialog.close()
  };
  dialog.addEventListener('cancel', e => {
    if (creationBusy) e.preventDefault()
  });

  function moscowInput(offset = 0) {
    return new Intl.DateTimeFormat('sv-SE', {
      timeZone: 'Europe/Moscow',
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hourCycle: 'h23'
    }).format(new Date(Date.now() + offset * 60000)).replace(' ', 'T')
  }
  async function openToken() {
    managementTicket++;
    selectedUser = null;
    formPayload = null;
    $('#token-title').textContent = 'Новый токен';
    dialog.showModal();
    renderTokenForm()
  }

  async function openManagedToken(id) {
    const ticket = ++managementTicket;
    $('#token-title').textContent = 'Токен №' + id;
    if (!dialog.open) dialog.showModal();
    $('#token-body').innerHTML = '<div class="loading"><span class="spinner"></span>Загружаем токен…</div>';
    try {
      const token = await api('api/ghost/token/?id=' + encodeURIComponent(id));
      if (ticket !== managementTicket || !dialog.open) return;
      renderManagedToken(token)
    } catch (e) {
      if (ticket !== managementTicket || !dialog.open) return;
      $('#token-body').innerHTML = `<p class="form-error" role="alert">${esc(e.message)}</p><button class="secondary" id="retry-token-detail">Обновить</button>`;
      $('#retry-token-detail').onclick = () => openManagedToken(id)
    }
  }

  function tokenViewerUrl(token) {
    const aiOnly = !token.allow_remote && token.allow_ai === true;
    if (token.status !== 'active' || (!token.allow_remote && !aiOnly)) return '';
    // Current servers select the session and its v2 viewer key. Only old APIs
    // without this field need the legacy token route; null means unavailable.
    const supplied = Object.prototype.hasOwnProperty.call(token, 'viewer_url');
    // AI-only access has a text viewer only while its secure session is active.
    if (aiOnly && !supplied) return '';
    const value = supplied ? token.viewer_url : `https://yc.maksonchik.ru/ghost/viewer/?token=${encodeURIComponent(token.token)}`;
    if (typeof value !== 'string' || !value) return '';
    try {
      const target = new URL(value);
      const uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';
      const viewerPath = new RegExp(`^/ghost/(?:viewer/(?:${uuid}/)?|v2/viewer/${uuid}/)$`, 'i');
      if (target.origin !== 'https://yc.maksonchik.ru' || target.username || target.password || !viewerPath.test(target.pathname)) return '';
      if (aiOnly) {
        const aiPath = new RegExp(`^/ghost/v2/viewer/${uuid}/$`, 'i');
        const keys = target.searchParams.getAll('key');
        const modes = target.searchParams.getAll('mode');
        if (!aiPath.test(target.pathname) || keys.length !== 1 || !/^[0-9a-f]{64}$/i.test(keys[0]) || modes.length !== 1 || modes[0] !== 'text' || target.hash) return '';
      }
      return target.href
    } catch {return ''}
  }

  function renderManagedToken(token, notice = '') {
    const inputDate = value => value.slice(0,19);
    const viewerUrl = tokenViewerUrl(token);
    const textViewer = viewerUrl && new URL(viewerUrl).searchParams.get('mode') === 'text';
    $('#token-title').textContent = 'Токен №' + token.id;
    $('#token-body').innerHTML = `<div class="managed-token-heading"><div><b>${esc(token.user)}</b><p class="muted">${token.token_type==='real'?'Реальный · '+rub(token.amount):'Тестовый'}</p></div>${badge(token.status)}</div><div class="token-value" id="managed-token-value">${esc(token.token)}</div><div class="token-shortcuts"><button class="secondary" id="copy-managed-token">Скопировать токен</button>${viewerUrl?`<a class="secondary" id="open-token-viewer" href="${esc(viewerUrl)}" target="_blank" rel="noopener noreferrer">${textViewer?'Открыть viewer для текста':'Открыть viewer'} ↗</a>`:''}</div>${textViewer?'<p class="form-help">Текст из viewer появится в окне GhostNote.</p>':''}<p id="manage-status" class="form-help" role="status">${esc(notice)}</p><form id="manage-token-form"><div class="form-grid"><label class="date-field">Начало, МСК<input id="manage-starts" type="datetime-local" step="1" value="${esc(inputDate(token.starts_at))}" required></label><label class="date-field">Окончание, МСК<input id="manage-ends" type="datetime-local" step="1" value="${esc(inputDate(token.expires_at))}" required></label><div class="full token-extensions"><span class="form-help">Продлить:</span>${[[30,'+30 мин'],[60,'+1 час'],[1440,'+1 день']].map(([m,label])=>`<button type="button" class="small-button" data-extend="${m}">${label}</button>`).join('')}</div><div class="full check-row"><label class="check-label"><input id="manage-active" type="checkbox" ${token.is_active?'checked':''}>Токен включён</label></div><div class="full"><label>Режимы доступа</label><div class="check-row token-access"><label class="check-label"><input id="manage-local" type="checkbox" ${token.allow_local?'checked':''}>Локальный</label><label class="check-label"><input id="manage-remote" type="checkbox" ${token.allow_remote?'checked':''}>Удалённый</label><label class="check-label"><input id="manage-ai" type="checkbox" ${token.allow_ai===true?'checked':''}>Нейросеть</label></div><p class="form-help">Доступ к нейросети включается вручную. Для новых токенов он выключен.</p></div><label class="full">Заметка<input id="manage-label" type="text" maxlength="128" value="${esc(token.label)}" placeholder="Необязательная заметка"></label></div><p class="form-help">Изменения вступят в силу после сохранения.</p><div id="manage-error" class="form-error" role="alert"></div><button id="reload-managed-token" type="button" class="link-button" hidden>Загрузить актуальные данные</button><div class="dialog-actions"><button id="cancel-managed-token" type="button" class="secondary">Закрыть</button><button id="save-managed-token" type="submit" class="primary">Сохранить</button></div></form><p class="form-help token-history">Создан ${date(token.created_at,true)} · Последнее использование: ${date(token.last_used_at,true)}</p>`;
    $('#copy-managed-token').onclick = async () => {
      try {await navigator.clipboard.writeText(token.token);$('#manage-status').textContent = 'Токен скопирован.'}
      catch {$('#manage-status').textContent = 'Выделите токен выше и скопируйте вручную.'}
    };
    $('#cancel-managed-token').onclick = () => dialog.close();
    $('#reload-managed-token').onclick = () => openManagedToken(token.id);
    $$('[data-extend]').forEach(button => button.onclick = () => {
      const current = new Date($('#manage-ends').value + '+03:00').getTime();
      const at = new Date(Math.max(Number.isFinite(current)?current:0,Date.now()) + Number(button.dataset.extend)*60000);
      $('#manage-ends').value = new Date(at.getTime()+3*3600000).toISOString().slice(0,19)
    });
    $('#manage-token-form').onsubmit = async e => {
      e.preventDefault();
      if (creationBusy) return;
      const starts = $('#manage-starts').value, ends = $('#manage-ends').value;
      const normalized = value => value.length===16?value+':00':value;
      const fullDate = (value, original) => normalized(value)===inputDate(original)?original:normalized(value)+'+03:00';
      if (new Date(fullDate(ends,token.expires_at))<=new Date(fullDate(starts,token.starts_at))) {$('#manage-error').textContent='Окончание должно быть позже начала.';return}
      if (!$('#manage-local').checked && !$('#manage-remote').checked && !$('#manage-ai').checked) {$('#manage-error').textContent='Выберите хотя бы один режим доступа.';return}
      const payload = {id:token.id,revision:token.revision,label:$('#manage-label').value,starts_at:fullDate(starts,token.starts_at),expires_at:fullDate(ends,token.expires_at),is_active:$('#manage-active').checked,allow_local:$('#manage-local').checked,allow_remote:$('#manage-remote').checked,allow_ai:$('#manage-ai').checked};
      creationBusy = true;
      $('#manage-error').textContent = '';
      $$('#manage-token-form input, #manage-token-form button').forEach(el=>el.disabled=true);
      $('#close-dialog').disabled = true;
      $('#save-managed-token').textContent = 'Сохраняем…';
      try {
        const result = await api('api/update-token/', {method:'POST',body:JSON.stringify(payload)});
        tokenListDirty = true;
        renderManagedToken(result, 'Изменения сохранены.');
      } catch (error) {
        $('#manage-error').textContent=error.message+(error.fields?' '+Object.values(error.fields).flat().map(x=>x.message).join(' '):'');
        $$('#manage-token-form input, #manage-token-form button').forEach(el=>el.disabled=false);
        $('#save-managed-token').textContent='Сохранить';
        $('#reload-managed-token').hidden=false;
        if ([404,409].includes(error.status)) $('#save-managed-token').disabled=true
      } finally {
        creationBusy=false;
        $('#close-dialog').disabled=false
      }
    }
  }

  function renderTokenForm(values = {}) {
    $('#token-body').innerHTML = `<form id="token-form"><div id="token-error" class="form-error" role="alert"></div><div class="form-grid"><label class="full">Пользователь<input id="user-query" type="search" placeholder="Имя или @username" autocomplete="off" value="${esc(selectedUser?.label||'')}"><span class="form-help" id="user-selected">${selectedUser?'Выбран: '+esc(selectedUser.label):'Найдите и выберите пользователя из списка.'}</span><div class="user-results" id="user-results"></div></label><label>Тип токена<select id="token-type"><option value="real">Реальный</option><option value="test">Тестовый · 20 мин</option></select></label><label id="amount-label">Оплачено, ₽<input id="token-amount" type="number" inputmode="decimal" min="0.01" step="0.01" max="99999999.99" value="${esc(values.amount||'')}" required placeholder="1500"></label><label class="date-field" id="starts-label">Начало, МСК<input id="token-starts" type="datetime-local" value="${esc(values.starts_at?.slice(0,16)||moscowInput())}" required></label><label class="date-field" id="ends-label">Окончание, МСК<input id="token-ends" type="datetime-local" value="${esc(values.expires_at?.slice(0,16)||moscowInput(60))}" required></label><p class="form-help full" id="test-note" hidden>Тестовый токен начнёт действовать сразу после создания и завершится через 20 минут.</p><div class="full"><label>Режим доступа</label><div class="check-row" style="margin-top:10px"><label class="check-label"><input id="allow-local" type="checkbox" ${values.allow_local===false?'':'checked'}>Локальный</label><label class="check-label"><input id="allow-remote" type="checkbox" ${values.allow_remote===false?'':'checked'}>Удалённый</label><label class="check-label"><input id="allow-ai" type="checkbox" ${values.allow_ai===true?'checked':''}>Нейросеть</label></div><p class="form-help">Нейросеть выключена по умолчанию, в том числе для тестовых токенов.</p></div><p class="form-help full">Реальный токен учитывается в оплатах. Реферальное начисление создаётся по действующим правилам GhostNote.</p></div><div class="dialog-actions"><button type="button" class="secondary" id="cancel-token">Отмена</button><button type="submit" class="primary">Далее ${icon('arrow')}</button></div></form>`;
    $('#cancel-token').onclick = () => dialog.close();
    $('#token-type').value = values.token_type || 'real';
    $('#token-type').onchange = () => {
      const test = $('#token-type').value === 'test';
      for (const id of ['amount', 'starts', 'ends']) {
        $('#token-' + id).required = !test;
        $('#token-' + id).disabled = test;
        $('#' + (id === 'amount' ? 'amount-label' : id === 'starts' ? 'starts-label' : 'ends-label')).hidden = test
      }
      $('#test-note').hidden = !test
    };
    $('#token-type').onchange();
    let userTicket = 0,
      timer;
    async function searchUsers() {
      const t = ++userTicket;
      try {
        const d = await api('api/ghost/users/?q=' + encodeURIComponent($('#user-query').value));
        if (t !== userTicket || !$('#user-results')) return;
        $('#user-results').innerHTML = d.users.length ? d.users.map(u => `<button type="button" class="user-result" data-user-id="${u.id}">${esc(u.label)}${u.name!==u.label?' · '+esc(u.name):''}</button>`).join('') : '<div class="form-help" style="padding:12px">Пользователь не найден. Сначала добавьте его в админке GhostNote.</div>';
        $$('[data-user-id]').forEach(b => b.onclick = () => {
          selectedUser = d.users.find(u => u.id === Number(b.dataset.userId));
          $('#user-query').value = selectedUser.label;
          $('#user-selected').textContent = 'Выбран: ' + selectedUser.label;
          $('#user-results').innerHTML = ''
        })
      } catch (e) {
        if ($('#user-results')) $('#user-results').textContent = e.message
      }
    }
    $('#user-query').oninput = () => {
      selectedUser = null;
      $('#user-selected').textContent = 'Выберите пользователя из списка.';
      clearTimeout(timer);
      timer = setTimeout(searchUsers, 250)
    };
    if (!selectedUser) searchUsers();
    $('#token-form').onsubmit = e => {
      e.preventDefault();
      const err = $('#token-error');
      if (!selectedUser) {
        err.textContent = 'Выберите пользователя из списка.';
        return
      }
      const real = $('#token-type').value === 'real';
      if (real && $('#token-ends').value <= $('#token-starts').value) {
        err.textContent = 'Окончание должно быть позже начала.';
        return
      }
      if (!$('#allow-local').checked && !$('#allow-remote').checked && !$('#allow-ai').checked) {
        err.textContent = 'Выберите хотя бы один режим доступа.';
        return
      }
      formPayload = {
        request_id: crypto.randomUUID(),
        user_id: selectedUser.id,
        token_type: real ? 'real' : 'test',
        amount: real ? $('#token-amount').value : '',
        starts_at: real ? $('#token-starts').value + ':00+03:00' : null,
        expires_at: real ? $('#token-ends').value + ':00+03:00' : null,
        allow_local: $('#allow-local').checked,
        allow_remote: $('#allow-remote').checked,
        allow_ai: $('#allow-ai').checked
      };
      reviewToken()
    }
  }

  function reviewToken() {
    const p = formPayload;
    $('#token-title').textContent = 'Подтвердите создание';
    $('#token-body').innerHTML = `<p class="muted">Проверьте параметры перед созданием токена.</p><div class="review-row"><span>Пользователь</span><b>${esc(selectedUser.label)}</b></div><div class="review-row"><span>Тип</span><b>${p.token_type==='real'?'Реальный':'Тестовый'}</b></div><div class="review-row"><span>Оплата</span><b>${p.token_type==='real'?rub(p.amount):'Без оплаты'}</b></div><div class="review-row"><span>Действует</span><b>${p.token_type==='real'?date(p.starts_at,true)+' → '+date(p.expires_at,true):'20 минут с момента создания'}</b></div><div class="review-row"><span>Режим</span><b>${[p.allow_local?'Локальный':'',p.allow_remote?'Удалённый':'',p.allow_ai?'Нейросеть':''].filter(Boolean).join(', ')}</b></div><div id="token-error" class="form-error" role="alert" style="margin-top:15px"></div><div class="dialog-actions"><button id="edit-token" class="secondary">Изменить</button><button id="confirm-token" class="primary">Создать токен</button></div>`;
    $('#edit-token').onclick = () => {
      $('#token-title').textContent = 'Новый токен';
      renderTokenForm(p)
    };
    $('#confirm-token').onclick = async () => {
      if (creationBusy) return;
      creationBusy = true;
      $('#confirm-token').disabled = true;
      $('#edit-token').disabled = true;
      $('#close-dialog').disabled = true;
      $('#confirm-token').textContent = 'Создаём…';
      try {
        const result = await api('api/create-token/', {
          method: 'POST',
          body: JSON.stringify(formPayload)
        });
        $('#token-title').textContent = 'Токен создан';
        $('#token-body').innerHTML = `<p><b>${esc(result.user)}</b></p><p class="muted">Токен №${result.id} · до ${date(result.expires_at,true)} МСК</p><div class="token-value" id="created-token">${esc(result.token)}</div><p class="form-help">Скопируйте токен и передайте пользователю.</p><div class="dialog-actions"><button id="copy-token" class="secondary">Скопировать</button><button id="done-token" class="primary">Готово</button></div>`;
        $('#copy-token').onclick = async () => {
          try {
            await navigator.clipboard.writeText(result.token);
            toast('Токен скопирован')
          } catch {
            toast('Выделите токен и скопируйте вручную.')
          }
        };
        $('#done-token').onclick = () => {
          dialog.close();
          state.status = 'all';
          load()
        }
      } catch (e) {
        $('#token-error').textContent = e.message + (e.fields ? ' ' + Object.values(e.fields).flat().map(x => x.message).join(' ') : '') + ' При повторном нажатии второй токен не создастся.';
        $('#confirm-token').disabled = false;
        $('#confirm-token').textContent = 'Повторить';
        $('#edit-token').disabled = false
      } finally {
        creationBusy = false;
        $('#close-dialog').disabled = false
      }
    }
  }
  $('#date-from').value = state.from;
  $('#date-to').value = state.to;
  $$('[data-days]').forEach(b => b.onclick = () => {
    state.to = dayISO();
    state.from = b.dataset.days === 'all' ? dayISO(-3650) : dayISO(1 - Number(b.dataset.days));
    $('#date-from').value = state.from;
    $('#date-to').value = state.to;
    $$('[data-days]').forEach(x => x.classList.toggle('selected', x === b));
    state.page = 1;
    state.dailyPage = 1;
    load()
  });
  $('#date-form').onsubmit = e => {
    e.preventDefault();
    const from = $('#date-from').value,
      to = $('#date-to').value;
    if (to < from) {
      alert('Дата окончания должна быть не раньше даты начала.');
      return
    }
    if ((new Date(to) - new Date(from)) / 864e5 > 3650) {
      alert('Максимальный период — 10 лет.');
      return
    }
    state.from = from;
    state.to = to;
    state.page = 1;
    state.dailyPage = 1;
    $$('[data-days]').forEach(b => b.classList.remove('selected'));
    load()
  };
  $('#refresh').onclick = () => load();
  window.addEventListener('offline', () => alert('Нет интернета. Данные не обновляются. Создание токена недоступно.'));
  window.addEventListener('online', () => {
    if (!dialog.open) load(true)
  });
  if ('serviceWorker' in navigator) {
    swReady = navigator.serviceWorker.register('/panel/sw.js', {
      scope: '/panel/'
    }).then(() => Promise.race([navigator.serviceWorker.ready, new Promise(resolve => setTimeout(() => resolve(null), 10000))])).catch(() => null)
  }
  if (state.tab === 'tokens') state.status = state.project === 'ghost' ? 'future' : 'active';
  navigation();
  load();
  refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible' && !dialog.open && !payoutBusy && !['INPUT', 'SELECT'].includes(document.activeElement?.tagName) && state.tab !== 'push') load(true)
  }, 60000);
})();
