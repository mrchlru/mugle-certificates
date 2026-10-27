/*!
 * card-expired.js — экран «Срок действия карты закончился». Чистый JS, без зависимостей.
 * Подключать после app.js. Стили: card-expired.css. Эталон: reference-prototype.html.
 *
 *   var e = CardExpired.mount({
 *     card: document.querySelector('.wallet-card'), // существующий элемент карты (переносится, не копируется)
 *     expiredOn: '2026-10-12',                      // дата окончания срока (ISO или Date); можно не передавать
 *     firstName: 'Роман',                           // можно не передавать
 *     barcodeSelector: '.wallet-code',              // блок штрихкода, он скрывается
 *     accent: '#19B8A3',                            // акцентный цвет (по умолчанию бирюзовый)
 *     container: null                               // элемент для встроенного показа; иначе на весь экран
 *   });
 *   e.destroy();                                    // убрать экран, вернуть карту на место, показать штрихкод
 *
 * Все тексты вставляются через textContent (безопасно).
 */
(function (global) {
  'use strict';
  var CARD_W = 270;
  var MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'];

  var TEXTS = {
    tag: 'Карта',
    title: 'Срок действия карты закончился',
    sub: function (o, date) {
      var who = o.firstName ? o.firstName + ', карта' : 'Карта';
      return date ? who + ' действовала до ' + date + '. Теперь она неактивна, штрихкод скрыт.' : who + ' больше не действует, штрихкод скрыт.';
    },
    hint: 'Нужна новая или продлённая карта? Обратитесь к тому, кто вам её выдал.'
  };

  function fmtDate(v) {
    if (!v) return '';
    var m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(v));
    var d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(v);
    if (isNaN(d.getTime())) return '';
    return d.getDate() + ' ' + MONTHS[d.getMonth()] + ' ' + d.getFullYear();
  }

  var HTML =
    '<div class="ce__col">' +
      '<div class="ce__tag"></div>' +
      '<div class="ce__stage">' +
        '<div class="ce__card"><div class="ce__fit"></div></div>' +
        '<div class="ce__veil"></div><div class="ce__scan"></div>' +
        '<div class="ce__badge">' +
          '<svg class="ce__ring" viewBox="0 0 86 86" aria-hidden="true"><circle cx="43" cy="43" r="42"/></svg>' +
          '<svg class="ce__glass" viewBox="0 0 24 24" aria-hidden="true">' +
            '<path class="sand sand-top" d="M8 5.2h8L12 11z"/>' +
            '<path class="sand sand-bot" d="M7.2 19h9.6L12 14z"/>' +
            '<line class="drip" x1="12" y1="11" x2="12" y2="14.6"/>' +
            '<path class="frame" d="M6 3h12M6 21h12M7 3c0 4.2 3 5.6 5 9-2 3.4-5 4.8-5 9M17 3c0 4.2-3 5.6-5 9 2 3.4 5 4.8 5 9"/>' +
          '</svg>' +
        '</div>' +
      '</div>' +
      '<div class="ce__text"><h1></h1><p class="ce__sub"></p><p class="ce__hint"></p></div>' +
    '</div>';

  function mount(opts) {
    var o = opts || {};
    var card = o.card;
    if (!card) throw new Error('CardExpired: не передан элемент карты (card)');
    var embedded = !!o.container;
    var nw = o.cardWidth || card.offsetWidth || 380;
    var nh = o.cardHeight || card.offsetHeight || 510;
    var radius = parseFloat(getComputedStyle(card).borderTopLeftRadius) || 26;
    var barSel = o.barcodeSelector || '.wallet-code';
    var home = { parent: card.parentNode, next: card.nextSibling };

    var root = document.createElement('div');
    root.className = 'ce' + (embedded ? ' ce--embedded' : '');
    root.setAttribute('role', 'status');
    root.innerHTML = HTML;
    if (o.accent) root.style.setProperty('--ce-accent', o.accent);
    function q(s) { return root.querySelector(s); }

    q('.ce__tag').textContent = TEXTS.tag;
    q('h1').textContent = TEXTS.title;
    q('.ce__sub').textContent = TEXTS.sub(o, fmtDate(o.expiredOn));
    q('.ce__hint').textContent = o.hint || TEXTS.hint;

    var fit = q('.ce__fit');
    fit.style.width = nw + 'px'; fit.style.height = nh + 'px';
    fit.appendChild(card);

    var prevOverflow = '';
    if (embedded) o.container.appendChild(root);
    else { document.body.appendChild(root); prevOverflow = document.documentElement.style.overflow; document.documentElement.style.overflow = 'hidden'; }

    // размер карты подгоняется под высоту экрана
    function size() {
      var avail = (embedded ? o.container.clientHeight : window.innerHeight) - 64 - 32 - 200 - 92;
      var h270 = CARD_W * nh / nw;
      var k = Math.min(1, Math.max(0.5, avail / h270));
      var w = CARD_W * k, h = h270 * k;
      root.style.setProperty('--ce-w', w + 'px'); root.style.setProperty('--ce-h', h + 'px');
      root.style.setProperty('--ce-r', (radius * w / nw) + 'px');
      fit.style.transform = 'scale(' + (w / nw) + ')';
    }
    window.addEventListener('resize', size);
    size();

    var timers = [], bar = card.querySelector(barSel), done = false;
    var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    function hideBarcode() { if (bar) { bar.classList.add('ce-barcode-off'); bar.setAttribute('aria-hidden', 'true'); } }

    if (reduce) hideBarcode();
    else {
      // старт анимации в следующем кадре, штрихкод скрывается, когда по нему проходит линия
      requestAnimationFrame(function () { root.classList.add('is-play'); });
      timers.push(setTimeout(hideBarcode, 1500));
    }

    function destroy() {
      if (done) return;
      done = true;
      timers.forEach(clearTimeout);
      window.removeEventListener('resize', size);
      if (bar) { bar.classList.remove('ce-barcode-off'); bar.removeAttribute('aria-hidden'); }
      if (home.parent) home.parent.insertBefore(card, home.next);
      if (root.parentNode) root.parentNode.removeChild(root);
      if (!embedded) document.documentElement.style.overflow = prevOverflow;
    }
    return { destroy: destroy, root: root, replay: function () { root.classList.remove('is-play'); void root.offsetWidth; if (bar) bar.classList.remove('ce-barcode-off'); root.classList.add('is-play'); timers.push(setTimeout(hideBarcode, 1500)); } };
  }

  global.CardExpired = { mount: mount };
})(window);
