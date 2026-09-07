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

  document.addEventListener('DOMContentLoaded', function () {
    applyCoverageWidths();
    applyPaceWidths();
    openTargetDetails();
    window.addEventListener('hashchange', openTargetDetails);
    renderAll();
    wireFilters();
    // Раскладка подписей зависит от ширины, поэтому перерисовываем целиком.
    window.addEventListener('resize', debounce(renderAll, 180));
  });
})();
