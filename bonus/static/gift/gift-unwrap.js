/*!
 * gift-unwrap.js — экран распаковки подарочной карты. Чистый JS, без зависимостей.
 * Подключать после app.js. Стили: gift-unwrap.css. Эталон: reference-prototype.html.
 *
 *   var u = GiftUnwrap.mount({
 *     card: document.querySelector('.wallet-card'),  // существующий элемент карты (переносится, не копируется)
 *     kind: 'birthday' | 'gift',                     // тексты (см. TEXTS ниже)
 *     color: '#FF5A00',                              // цвет карточки (то же значение, что в --card)
 *     firstName: 'Роман',                            // имя, без фамилии; можно не передавать
 *     segmentName: 'Бартер на все рестораны',        // для kind:'gift' показывается под заголовком
 *     brand: 'Mugle Rest',
 *     sealText: 'mr',
 *     onGo: function () { ... },                     // нажали «Перейти к карте» или ×
 *     container: null                                // элемент для встроенного предпросмотра; иначе на весь экран
 *   });
 *   u.destroy();                                     // убрать экран, вернуть карту на место, onGo НЕ вызывается
 *
 * Все пользовательские тексты вставляются через textContent (безопасно).
 */
(function (global) {
  'use strict';

  var TIMING = { flap: 250, flapBack: 650, card: 650, reveal: 2350 }; // мс от нажатия «Открыть»
  var STEPS = ['is-seal', 'is-flap', 'is-flapback', 'is-card', 'is-reveal'];
  var CARD_W = 270;

  var TEXTS = {
    birthday: {
      introTitle: 'Вам подарок!',
      introText: function (o) { return o.brand + ' приготовил кое-что ко дню рождения'; },
      revealTitle: function (o) { return o.firstName ? 'С днём рождения, ' + o.firstName + '!' : 'С днём рождения!'; },
      revealText: function () { return 'Ваша подарочная карта уже ждёт'; }
    },
    gift: {
      introTitle: 'Вам подарок!',
      introText: function (o) { return o.brand + ' приготовил для вас карту'; },
      revealTitle: function (o) { return o.firstName ? 'Ваша карта, ' + o.firstName : 'Ваша карта'; },
      revealText: function (o) { return o.segmentName || 'Подарочная карта уже ждёт'; }
    }
  };

  /* ---------- цвета ---------- */
  function hex2rgb(h) {
    h = String(h || '').trim().replace('#', '');
    if (/^[0-9a-f]{3}$/i.test(h)) h = h.split('').map(function (c) { return c + c; }).join('');
    if (!/^[0-9a-f]{6}$/i.test(h)) return [185, 28, 28];
    return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
  }
  function mix(a, b, t) { // a в сторону b на долю t
    return [0, 1, 2].map(function (i) { return Math.round(a[i] + (b[i] - a[i]) * t); });
  }
  function css(rgb, alpha) { return alpha == null ? 'rgb(' + rgb.join(',') + ')' : 'rgba(' + rgb.join(',') + ',' + alpha + ')'; }
  function lum(rgb) {
    var c = rgb.map(function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); });
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
  }
  var INK = [20, 8, 9], WHITE = [255, 255, 255], BLACK = [0, 0, 0];

  /** Переменные фона, печати и конфетти из цвета карточки. Работает для тёмных, светлых и чёрных цветов. */
  function palette(hex) {
    var a = hex2rgb(hex);
    var vis = a;
    for (var i = 0; i < 4 && lum(vis) < 0.08; i++) vis = mix(vis, WHITE, 0.16); // тёмный цвет: поднять, чтобы свечение было видно
    var seal = a;
    var light = lum(a) > 0.45;
    return {
      bgTop: css(mix(INK, vis, 0.3)),
      bgBottom: css(mix(INK, vis, 0.06)),
      glow: css(vis, 0.5),
      seal: css(seal),
      sealHi: css(mix(seal, WHITE, 0.25)),
      sealLo: css(mix(seal, BLACK, 0.4)),
      sealInk: light ? '#2A1A0A' : '#F7D9D2',
      confetti: [css(a), css(mix(a, WHITE, 0.35)), css(mix(a, WHITE, 0.65)), '#F4EBDD', '#FFFFFF', css(mix(a, BLACK, 0.25))]
    };
  }
  var BIRTHDAY_CONFETTI = ['#19B8A3', '#D81B60', '#F4EBDD', '#F2B544', '#E04040', '#8FE3D6'];

  /* ---------- конфетти ---------- */
  function confetti(canvas, o) {
    var ctx = canvas.getContext('2d');
    if (!ctx) return function () {};
    var P = [], i, raf = 0, stopped = false, t0 = performance.now(), last = t0;
    for (i = 0; i < 120; i++) {
      var a = -Math.PI / 2 + (Math.random() - 0.5) * Math.PI * 1.1, v = 4 + Math.random() * 7;
      P.push({
        x: o.x + (Math.random() - 0.5) * o.spread, y: o.y, vx: Math.cos(a) * v, vy: Math.sin(a) * v,
        r: Math.random() * 6.28, vr: (Math.random() - 0.5) * 0.16, ph: Math.random() * 6.28,
        col: o.colors[i % o.colors.length], kind: i % 5 === 0 ? 2 : i % 2,
        w: 5 + Math.random() * 5, h: 8 + Math.random() * 7, delay: Math.random() * 450, age: 0, max: 3600 + Math.random() * 1200
      });
    }
    function step(now) {
      if (stopped) return;
      var dt = Math.min(40, now - last) / 16.67, el = now - t0, alive = 0;
      last = now;
      ctx.clearRect(0, 0, o.w, o.h);
      P.forEach(function (p) {
        if (el < p.delay) { alive++; return; }
        p.age += dt * 16.67;
        if (p.age > p.max) return;
        alive++;
        p.vy += 0.09 * dt;
        var d = Math.pow(0.985, dt);
        p.vx *= d; p.vy *= d;
        p.x += p.vx * dt + Math.sin(p.ph * 0.7) * 0.35 * dt;
        p.y += p.vy * dt + Math.min(p.age / 1200, 1) * 0.35 * dt;
        p.r += p.vr * dt; p.ph += 0.06 * dt;
        var fin = Math.min(1, p.age / 180), fout = p.age > p.max - 900 ? (p.max - p.age) / 900 : 1;
        ctx.globalAlpha = Math.max(0, Math.min(fin, fout));
        ctx.fillStyle = p.col; ctx.strokeStyle = p.col;
        ctx.save(); ctx.translate(p.x, p.y); ctx.rotate(p.r);
        if (p.kind === 2) {
          ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.beginPath();
          for (var k = 0; k <= 10; k++) ctx.lineTo(k * 2.6 - 13, Math.sin(k * 0.9 + p.ph) * 4);
          ctx.stroke();
        } else {
          ctx.scale(1, Math.abs(Math.cos(p.ph)) * 0.8 + 0.2);
          ctx.fillRect(-p.w / 2, -p.h / 2, p.w, p.kind ? p.h : p.w);
        }
        ctx.restore();
      });
      ctx.globalAlpha = 1;
      if (alive) raf = requestAnimationFrame(step); else ctx.clearRect(0, 0, o.w, o.h);
    }
    raf = requestAnimationFrame(step);
    return function () { stopped = true; cancelAnimationFrame(raf); ctx.clearRect(0, 0, o.w, o.h); };
  }

  /* ---------- экран ---------- */
  var HTML =
    '<div class="gu__col">' +
      '<div class="gu__bar"><span class="gu__tag">Подарок</span><button class="gu__x" type="button" aria-label="Закрыть">×</button></div>' +
      '<div class="gu__heads">' +
        '<div class="gu__head gu__head--a"><h1></h1><p></p></div>' +
        '<div class="gu__head gu__head--b" aria-live="polite"><h1></h1><p></p></div>' +
      '</div>' +
      '<div class="gu__giftwrap"><div class="gu__gift">' +
        '<div class="gu__part gu__env-back"></div>' +
        '<div class="gu__card"><div class="gu__fit"></div></div>' +
        '<div class="gu__part gu__env-front"></div>' +
        '<div class="gu__part gu__flap"><i class="gu__flap-out"></i><i class="gu__flap-in"></i></div>' +
        '<div class="gu__seal"><span></span></div>' +
        '<button class="gu__hit" type="button" aria-label="Открыть подарок"></button>' +
      '</div></div>' +
      '<div class="gu__foot">' +
        '<div class="gu__layer gu__layer--open"><button class="gu__btn gu__btn--pulse gu__open" type="button"></button></div>' +
        '<div class="gu__layer gu__layer--cta"><button class="gu__btn gu__go" type="button"><span></span>' +
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button></div>' +
      '</div>' +
      '<canvas class="gu__fx" aria-hidden="true"></canvas>' +
    '</div>';

  function mount(opts) {
    var o = opts || {};
    var card = o.card;
    if (!card) throw new Error('GiftUnwrap: не передан элемент карты (card)');
    o.kind = o.kind === 'birthday' ? 'birthday' : 'gift';
    o.brand = o.brand || 'Mugle Rest';
    var T = TEXTS[o.kind];
    var pal = palette(o.color);

    // размер карты в натуральном виде, до переноса
    var nw = o.cardWidth || card.offsetWidth || 380;
    var nh = o.cardHeight || card.offsetHeight || 510;
    var cardH = Math.round(CARD_W * nh / nw);

    var home = { parent: card.parentNode, next: card.nextSibling };
    var embedded = !!o.container;
    var root = document.createElement('div');
    root.className = 'gu is-idle' + (embedded ? ' gu--embedded' : '');
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-label', 'Подарок');
    root.innerHTML = HTML;
    var s = root.style;
    s.setProperty('--gu-bg-top', pal.bgTop); s.setProperty('--gu-bg-bottom', pal.bgBottom); s.setProperty('--gu-glow', pal.glow);
    s.setProperty('--gu-seal', pal.seal); s.setProperty('--gu-seal-hi', pal.sealHi); s.setProperty('--gu-seal-lo', pal.sealLo); s.setProperty('--gu-seal-ink', pal.sealInk);

    function q(sel) { return root.querySelector(sel); }
    q('.gu__head--a h1').textContent = T.introTitle;
    q('.gu__head--a p').textContent = T.introText(o);
    q('.gu__head--b h1').textContent = T.revealTitle(o);
    q('.gu__head--b p').textContent = T.revealText(o);
    q('.gu__seal span').textContent = o.sealText || 'mr';
    q('.gu__open').textContent = o.openLabel || 'Открыть';
    q('.gu__go span').textContent = o.goLabel || 'Перейти к карте';

    var cardBox = q('.gu__card'), fit = q('.gu__fit'), canvas = q('.gu__fx'), giftwrap = q('.gu__giftwrap');
    cardBox.style.height = cardH + 'px';
    fit.style.width = nw + 'px'; fit.style.height = nh + 'px';
    fit.style.transform = 'scale(' + (CARD_W / nw) + ')';
    fit.appendChild(card); // переносим сам элемент: обработчики наклона и штрихкод остаются рабочими

    var prevOverflow = '';
    if (embedded) o.container.appendChild(root);
    else { document.body.appendChild(root); prevOverflow = document.documentElement.style.overflow; document.documentElement.style.overflow = 'hidden'; }

    var step = 0, timers = [], stopConfetti = null, done = false;
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    function fitStage() {
      var col = q('.gu__col');
      var k = Math.min(1, Math.max(0.5, (col.clientHeight - 190 - 120) / cardH));
      root.style.setProperty('--gu-k', k);
      var dpr = Math.min(2, window.devicePixelRatio || 1);
      canvas.width = col.clientWidth * dpr; canvas.height = col.clientHeight * dpr;
      canvas.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    window.addEventListener('resize', fitStage);
    fitStage();

    function setStep(n) {
      step = n;
      STEPS.forEach(function (c, i) { root.classList.toggle(c, i < n); });
      if (n > 0) root.classList.remove('is-idle');
    }
    function at(ms, fn) { timers.push(setTimeout(fn, ms)); }

    function launchConfetti() {
      var col = q('.gu__col').getBoundingClientRect(), c = cardBox.getBoundingClientRect();
      stopConfetti = confetti(canvas, {
        x: c.left - col.left + c.width / 2, y: c.top - col.top + c.height * 0.5, spread: c.width * 0.6,
        w: col.width, h: col.height, colors: o.kind === 'birthday' ? BIRTHDAY_CONFETTI : pal.confetti
      });
    }

    function open() {
      if (step !== 0 || done) return;
      if (reduce) { setStep(5); return; }          // без анимации и конфетти
      try { if (navigator.vibrate) navigator.vibrate(12); } catch (e) { /* не поддерживается */ }
      setStep(1);
      at(TIMING.flap, function () { setStep(2); });
      at(TIMING.flapBack, function () { setStep(3); });
      at(TIMING.card, function () { setStep(4); });  // карта растёт и выезжает, конверт растворяется одновременно
      at(TIMING.reveal, function () { setStep(5); launchConfetti(); });
    }

    function teardown() {
      if (done) return;
      done = true;
      timers.forEach(clearTimeout);
      if (stopConfetti) stopConfetti();
      window.removeEventListener('resize', fitStage);
      if (home.parent) home.parent.insertBefore(card, home.next); // вернуть карту на место
      if (root.parentNode) root.parentNode.removeChild(root);
      if (!embedded) document.documentElement.style.overflow = prevOverflow;
    }
    function go() { teardown(); if (typeof o.onGo === 'function') o.onGo(); }

    q('.gu__open').addEventListener('click', open);
    q('.gu__hit').addEventListener('click', open);
    q('.gu__go').addEventListener('click', go);
    q('.gu__x').addEventListener('click', go);
    setTimeout(function () { try { q('.gu__open').focus({ preventScroll: true }); } catch (e) { /* ничего */ } }, 0);

    return { destroy: teardown, open: open, root: root };
  }

  global.GiftUnwrap = { mount: mount, palette: palette };
})(window);
