/* Подключение графиков и фильтров.
 *
 * Данные приходят островками <script type="application/json">: браузер их не
 * исполняет, поэтому строгий CSP обходится без 'unsafe-inline', а страница
 * не мигает спиннерами — цифры уже отрисованы сервером в таблицах рядом.
 */
(function () {
  'use strict';

  function readData(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return null;
    }
  }

  function renderAll() {
    var hosts = document.querySelectorAll('[data-chart]');
    Array.prototype.forEach.call(hosts, function (host) {
      var kind = host.getAttribute('data-chart');
      var data = readData(host.getAttribute('data-source'));
      if (!data || !window.Charts[kind]) return;
      window.Charts[kind](host, data);
    });
  }

  function debounce(fn, wait) {
    var timer = null;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, wait);
    };
  }

  /* Форма фильтров отправляется по изменению любого поля: отдельная кнопка
   * «Применить» на дашборде лишний клик — период меняют постоянно. */
  function wireFilters() {
    var form = document.querySelector('[data-autosubmit]');
    if (!form) return;
    Array.prototype.forEach.call(form.querySelectorAll('select, input[type="date"]'),
      function (field) {
        field.addEventListener('change', function () { form.submit(); });
      });
  }

  /* Ширина полосы покрытия ставится из JS, а не атрибутом style: строгий CSP
   * (style-src 'self') запрещает инлайновые стили в разметке, а ослаблять его
   * ради одной полосы означало бы открыть подмену интерфейса через внедрённую
   * разметку. На изменение свойств через CSSOM политика не распространяется. */
  function applyCoverageWidths() {
    var bars = document.querySelectorAll('.coverage-fill[data-width]');
    Array.prototype.forEach.call(bars, function (bar) {
      var value = parseFloat(bar.getAttribute('data-width'));
      bar.style.width = (isFinite(value) ? Math.max(0, Math.min(100, value)) : 0) + '%';
    });
  }

  /* Полоса выполнения и засечка срока — по той же причине, что и покрытие:
   * инлайновый style в разметке запрещён политикой, а CSSOM ей не подчинён. */
  function applyPaceWidths() {
    function clamp(node, attribute, property) {
      var value = parseFloat(node.getAttribute(attribute));
      node.style[property] = (isFinite(value) ? Math.max(0, Math.min(100, value)) : 0) + '%';
    }
    Array.prototype.forEach.call(
      document.querySelectorAll('.pace-fill[data-width]'),
      function (bar) { clamp(bar, 'data-width', 'width'); });
    Array.prototype.forEach.call(
      document.querySelectorAll('.pace-mark[data-at]'),
      function (mark) { clamp(mark, 'data-at', 'left'); });
  }

  /* Ссылка с отдела в таблице открывает его блок ниже.
   *
   * Часть браузеров раскрывает <details> при переходе по якорю внутрь него
   * сама, часть — нет, и на «части» экран работает через раз. Восемь строк
   * дешевле, чем объяснять РОПу, что ссылка зависит от браузера. Без
   * скрипта блок всё равно раскрывается кликом — это запасной путь, а не
   * единственный.
   */
  function openTargetDetails() {
    var id = (window.location.hash || '').replace('#', '');
    if (!id) return;
    var node = document.getElementById(id);
    if (node && node.tagName === 'DETAILS') node.open = true;
  }

  /**
   * Кнопка «Сгенерировать» рядом с полем пароля.
   *
   * Пароль придумывает браузер, а не сервер, и это не каприз: сгенерируй
   * его сервер — пришлось бы показать результат в ответе, а обновление
   * страницы выдало бы уже другой пароль, и только что розданный перестал
   * бы работать. Здесь администратор видит значение до отправки, копирует
   * и отправляет; в ответе сервера пароля нет вовсе.
   *
   * crypto.getRandomValues, а не Math.random: второй предсказуем, и пароли
   * из него подбираются, зная примерное время создания.
   *
   * Без скрипта поле остаётся обычным — пароль вводится руками.
   */
  var ALPHABET = 'abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  var PASSWORD_LEN = 16;

  function makePassword() {
    var bytes = new Uint32Array(PASSWORD_LEN);
    window.crypto.getRandomValues(bytes);
    var out = '';
    for (var i = 0; i < PASSWORD_LEN; i++) {
      out += ALPHABET[bytes[i] % ALPHABET.length];
    }
    return out;
  }

  function wirePasswordButtons() {
    if (!window.crypto || !window.crypto.getRandomValues) return;
    var buttons = document.querySelectorAll('[data-generate-password]');
    for (var i = 0; i < buttons.length; i++) {
      (function (button) {
        button.addEventListener('click', function () {
          var form = button.closest('form');
          if (!form) return;
          var field = form.querySelector('[data-password-field]');
          if (!field) return;
          field.value = makePassword();
          field.focus();
          field.select();
        });
      })(buttons[i]);
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    applyCoverageWidths();
    wirePasswordButtons();
    applyPaceWidths();
    openTargetDetails();
    window.addEventListener('hashchange', openTargetDetails);
    renderAll();
    wireFilters();
    // Раскладка подписей зависит от ширины, поэтому перерисовываем целиком.
    window.addEventListener('resize', debounce(renderAll, 180));
  });
})();
