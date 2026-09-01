/* Графики на голом SVG.
 *
 * Своя реализация вместо библиотеки: внешние CDN недоступны из строгого CSP,
 * а вендорить мегабайт минифицированного кода в репозиторий ради четырёх форм
 * не стоит. Нужны ровно funnel, line, bars и расходящийся bar.
 *
 * Спецификации марок взяты из руководства по визуализации: полосы не толще
 * 24px со скруглением 4px на конце данных, линии 2px, маркеры от 8px с
 * кольцом цвета поверхности, сетка — сплошная волосяная линия. Цвет несут
 * марки; подписи, легенды и оси — текстовые токены.
 */
(function (global) {
  'use strict';

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var BAR_MAX = 24;
  var BAR_RADIUS = 4;
  var GAP = 2;          // зазор поверхностью между соседними марками
  var nf = new Intl.NumberFormat('ru-RU');

  function fmtNum(value) {
    if (value === null || value === undefined || isNaN(value)) return '—';
    return nf.format(Math.round(value));
  }

  function fmtMoney(value) {
    if (!isFinite(value)) return '—';
    var abs = Math.abs(value);
    if (abs >= 1e9) return (value / 1e9).toFixed(2).replace('.', ',') + ' млрд ₽';
    if (abs >= 1e6) return (value / 1e6).toFixed(1).replace('.', ',') + ' млн ₽';
    if (abs >= 1e3) return fmtNum(value / 1e3) + ' тыс ₽';
    return fmtNum(value) + ' ₽';
  }

  function el(name, attrs, parent) {
    var node = document.createElementNS(SVG_NS, name);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  function text(parent, x, y, value, className, anchor) {
    var node = el('text', {
      x: x, y: y, class: className || 'value-label',
      'text-anchor': anchor || 'start', 'dominant-baseline': 'middle'
    }, parent);
    node.textContent = value;
    return node;
  }

  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /* Полоса со скруглением только на конце данных: у базовой линии угол
   * остаётся прямым, иначе полоса выглядит оторванной от оси. */
  function hBarPath(x0, y, width, height, radius) {
    var r = Math.max(0, Math.min(radius, width, height / 2));
    if (width <= 0) return '';
    return 'M' + x0 + ',' + y +
      'H' + (x0 + width - r) +
      'a' + r + ',' + r + ' 0 0 1 ' + r + ',' + r +
      'V' + (y + height - r) +
      'a' + r + ',' + r + ' 0 0 1 ' + (-r) + ',' + r +
      'H' + x0 + 'Z';
  }

  // ------------------------------------------------------------ подсказка

  var tip = null;

  function tooltip() {
    if (!tip) {
      tip = document.createElement('div');
      tip.className = 'tooltip';
      tip.setAttribute('role', 'status');
      document.body.appendChild(tip);
    }
    return tip;
  }

  function showTip(event, html) {
    var node = tooltip();
    node.innerHTML = html;
    node.classList.add('visible');
    var box = node.getBoundingClientRect();
    var x = event.clientX + 14;
    var y = event.clientY + 14;
    if (x + box.width > window.innerWidth - 8) x = event.clientX - box.width - 14;
    if (y + box.height > window.innerHeight - 8) y = event.clientY - box.height - 14;
    node.style.left = Math.max(8, x) + 'px';
    node.style.top = Math.max(8, y) + 'px';
  }

  function hideTip() {
    if (tip) tip.classList.remove('visible');
  }

  function tipRows(title, rows) {
    var html = '<div class="tooltip-title">' + escapeHtml(title) + '</div>';
    rows.forEach(function (row) {
      html += '<div class="tooltip-row"><span>' + escapeHtml(row[0]) +
        '</span><span>' + escapeHtml(String(row[1])) + '</span></div>';
    });
    return html;
  }

  function escapeHtml(value) {
    return String(value).replace(/[&<>"']/g, function (ch) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
    });
  }

  function hookTip(node, title, rows) {
    node.style.cursor = 'default';
    node.addEventListener('mousemove', function (event) {
      showTip(event, tipRows(title, rows));
    });
    node.addEventListener('mouseleave', hideTip);
  }

  // ------------------------------------------------------------- воронка

  /* Воронка — упорядоченная величина, а не набор категорий, поэтому один тон
   * на все полосы: длина уже кодирует величину, красить её ещё и оттенком
   * значило бы сжечь единственный свободный канал на то, что и так видно. */
  function funnel(host, data) {
    var rows = (data.stages || []).filter(function (row) {
      return row.reached > 0 || row.count_now > 0;
    });
    if (!rows.length) return empty(host, 'Нет данных за период');

    var width = host.clientWidth || 640;
    var rowHeight = 34;
    var padTop = 8;
    var labelWidth = Math.min(190, Math.max(110, width * 0.28));
    var valueWidth = 116;
    var plotWidth = Math.max(40, width - labelWidth - valueWidth - 12);
    var height = padTop * 2 + rows.length * rowHeight;

    var svg = surface(host, width, height);
    var max = Math.max.apply(null, rows.map(function (r) { return r.reached; })) || 1;
    var barHeight = Math.min(BAR_MAX, rowHeight - 12);

    rows.forEach(function (row, index) {
      var y = padTop + index * rowHeight + (rowHeight - barHeight) / 2;
      var barWidth = Math.max(0, (row.reached / max) * plotWidth);

      text(svg, 0, y + barHeight / 2, truncate(row.name, labelWidth), 'axis-label');

      // Дорожка показывает, какую долю от вершины воронки занимает стадия.
      el('rect', {
        x: labelWidth, y: y, width: plotWidth, height: barHeight,
        rx: 3, fill: token('--seq-0')
      }, svg);

      var bar = el('path', {
        d: hBarPath(labelWidth, y, barWidth, barHeight, BAR_RADIUS),
        fill: token('--series-1')
      }, svg);

      hookTip(bar, row.name, [
        ['Дошли до стадии', fmtNum(row.reached)],
        ['Доля от когорты', String(row.conversion_from_start).replace('.', ',') + '%'],
        ['Стоит сейчас', fmtNum(row.count_now)],
        ['Сумма открытых', fmtMoney(row.amount_open)]
      ]);

      // Значение у кончика полосы — форма «полоса → значение на конце».
      text(svg, labelWidth + plotWidth + 10, y + barHeight / 2,
        fmtNum(row.reached), 'value-label-strong');
      text(svg, labelWidth + plotWidth + 62, y + barHeight / 2,
        String(row.conversion_from_start).replace('.', ',') + '%', 'value-label');
    });

    return svg;
  }

  // ------------------------------------------- горизонтальные полосы

  function bars(host, data) {
    var rows = data.rows || [];
    if (!rows.length) return empty(host, 'Нет данных за период');

    var width = host.clientWidth || 640;
    var rowHeight = 30;
    var padTop = 8;
    var labelWidth = Math.min(200, Math.max(110, width * 0.3));
    var valueWidth = 78;
    var plotWidth = Math.max(40, width - labelWidth - valueWidth - 12);
    var height = padTop * 2 + rows.length * rowHeight;

    var svg = surface(host, width, height);
    var max = Math.max.apply(null, rows.map(function (r) { return Math.abs(r.value); })) || 1;
    var barHeight = Math.min(BAR_MAX, rowHeight - 12 - GAP);

    rows.forEach(function (row, index) {
      var y = padTop + index * rowHeight + (rowHeight - barHeight) / 2;
      var barWidth = Math.max(0, (Math.abs(row.value) / max) * plotWidth);

      text(svg, 0, y + barHeight / 2, truncate(row.name, labelWidth), 'axis-label');
      var bar = el('path', {
        d: hBarPath(labelWidth, y, barWidth, barHeight, BAR_RADIUS),
        fill: token(data.color || '--series-1')
      }, svg);
      hookTip(bar, row.name, row.tip || [[data.valueLabel || 'Значение', row.display]]);
      text(svg, labelWidth + plotWidth + 10, y + barHeight / 2,
        row.display, 'value-label-strong');
    });

    return svg;
  }

  // ------------------------------------------------- расходящиеся полосы

  /* Чистый поток по стадии: вошло минус вышло. Полярность вокруг нуля —
   * именно тот случай, для которого нужна расходящаяся пара тёплый/холодный
   * с нейтральной серединой. */
  function diverging(host, data) {
    var rows = data.rows || [];
    if (!rows.length) return empty(host, 'Нет данных за период');

    var width = host.clientWidth || 640;
    var rowHeight = 30;
    var padTop = 14;
    var labelWidth = Math.min(190, Math.max(110, width * 0.28));
    var plotWidth = Math.max(60, width - labelWidth - 70);
    var height = padTop * 2 + rows.length * rowHeight;
    var zero = labelWidth + plotWidth / 2;

    var svg = surface(host, width, height);
    var max = Math.max.apply(null, rows.map(function (r) { return Math.abs(r.value); })) || 1;
    var barHeight = Math.min(BAR_MAX, rowHeight - 12 - GAP);
    var scale = (plotWidth / 2) / max;

    el('line', {
      x1: zero, y1: padTop - 6, x2: zero, y2: height - padTop + 6, class: 'gridline'
    }, svg);

    rows.forEach(function (row, index) {
      var y = padTop + index * rowHeight + (rowHeight - barHeight) / 2;
      var span = Math.abs(row.value) * scale;
      var positive = row.value >= 0;
      var x0 = positive ? zero : zero - span;

      text(svg, 0, y + barHeight / 2, truncate(row.name, labelWidth), 'axis-label');

      var d = positive
        ? hBarPath(x0, y, span, barHeight, BAR_RADIUS)
        : mirrorPath(zero, y, span, barHeight, BAR_RADIUS);
      var bar = el('path', {
        d: d,
        fill: token(positive ? '--diverge-pos' : '--diverge-neg')
      }, svg);
      hookTip(bar, row.name, row.tip || []);

      var labelX = positive ? zero + span + 8 : zero - span - 8;
      text(svg, labelX, y + barHeight / 2,
        (row.value > 0 ? '+' : '') + fmtNum(row.value), 'value-label-strong',
        positive ? 'start' : 'end');
    });

    return svg;
  }

  function mirrorPath(zero, y, span, height, radius) {
    var r = Math.max(0, Math.min(radius, span, height / 2));
    if (span <= 0) return '';
    var x0 = zero - span;
    return 'M' + zero + ',' + y +
      'H' + (x0 + r) +
      'a' + r + ',' + r + ' 0 0 0 ' + (-r) + ',' + r +
      'V' + (y + height - r) +
      'a' + r + ',' + r + ' 0 0 0 ' + r + ',' + r +
      'H' + zero + 'Z';
  }

  // --------------------------------------------------------------- линия

  function line(host, data) {
    var points = data.points || [];
    var series = data.series || [];
    if (points.length < 2 || !series.length) {
      return empty(host, 'Слишком мало точек для динамики');
    }

    var width = host.clientWidth || 640;
    var height = data.height || 240;
    var pad = { top: 14, right: 62, bottom: 26, left: 46 };
    var plotW = Math.max(40, width - pad.left - pad.right);
    var plotH = Math.max(40, height - pad.top - pad.bottom);

    var svg = surface(host, width, height);
    var peak = 0;
    series.forEach(function (s) {
      points.forEach(function (p) { peak = Math.max(peak, p[s.key] || 0); });
    });
    var scale = niceScale(peak, 4);
    var max = scale.max;

    var stepX = points.length > 1 ? plotW / (points.length - 1) : plotW;
    var xAt = function (i) { return pad.left + i * stepX; };
    var yAt = function (v) { return pad.top + plotH - (v / max) * plotH; };

    // Сетка: сплошная волосяная линия, отступающая на шаг от поверхности.
    for (var value = 0; value <= max + 1e-9; value += scale.step) {
      var y = yAt(value);
      el('line', { x1: pad.left, y1: y, x2: pad.left + plotW, y2: y, class: 'gridline' }, svg);
      text(svg, pad.left - 8, y, fmtNum(value), 'axis-label', 'end');
    }

    var colors = ['--series-1', '--series-2', '--series-3'];
    series.forEach(function (s, si) {
      var color = token(s.color || colors[si % colors.length]);
      var d = points.map(function (p, i) {
        return (i ? 'L' : 'M') + xAt(i) + ',' + yAt(p[s.key] || 0);
      }).join(' ');
      el('path', {
        d: d, fill: 'none', stroke: color, 'stroke-width': 2,
        'stroke-linejoin': 'round', 'stroke-linecap': 'round'
      }, svg);

      var last = points[points.length - 1];
      el('circle', {
        cx: xAt(points.length - 1), cy: yAt(last[s.key] || 0), r: 4,
        fill: color, stroke: token('--surface-1'), 'stroke-width': 2
      }, svg);
      // Подпись только на конце ряда: значение у каждой точки читать невозможно.
      text(svg, xAt(points.length - 1) + 9, yAt(last[s.key] || 0),
        fmtNum(last[s.key] || 0), 'value-label');
    });

    // Подписи оси X — только по краям и середине, иначе они наезжают друг на друга.
    [0, Math.floor(points.length / 2), points.length - 1].forEach(function (i, n, arr) {
      if (arr.indexOf(i) !== n) return;
      text(svg, xAt(i), pad.top + plotH + 14, points[i].label, 'axis-label',
        i === 0 ? 'start' : (i === points.length - 1 ? 'end' : 'middle'));
    });

    addCrosshair(svg, host, points, series, colors, xAt, pad, plotH, plotW, stepX);
    return svg;
  }

  function addCrosshair(svg, host, points, series, colors, xAt, pad, plotH, plotW, stepX) {
    var rule = el('line', {
      x1: 0, y1: pad.top, x2: 0, y2: pad.top + plotH,
      class: 'gridline', opacity: 0
    }, svg);
    var overlay = el('rect', {
      x: pad.left, y: pad.top, width: plotW, height: plotH,
      fill: 'transparent'
    }, svg);

    overlay.addEventListener('mousemove', function (event) {
      var box = svg.getBoundingClientRect();
      var ratio = svg.viewBox.baseVal.width / box.width;
      var localX = (event.clientX - box.left) * ratio;
      var index = Math.max(0, Math.min(points.length - 1,
        Math.round((localX - pad.left) / stepX)));
      var x = xAt(index);
      rule.setAttribute('x1', x);
      rule.setAttribute('x2', x);
      rule.setAttribute('opacity', 1);
      showTip(event, tipRows(points[index].label, series.map(function (s) {
        return [s.label, s.money
          ? fmtMoney(points[index][s.key] || 0)
          : fmtNum(points[index][s.key] || 0)];
      })));
    });
    overlay.addEventListener('mouseleave', function () {
      rule.setAttribute('opacity', 0);
      hideTip();
    });
  }

  // ------------------------------------------------------------- служебное

  function surface(host, width, height) {
    host.textContent = '';
    var svg = el('svg', {
      class: 'chart', viewBox: '0 0 ' + width + ' ' + height,
      width: '100%', height: height, role: 'img'
    }, host);
    return svg;
  }

  function empty(host, message) {
    host.textContent = '';
    var div = document.createElement('div');
    div.className = 'empty';
    div.textContent = message;
    host.appendChild(div);
    return null;
  }

  /* Деления оси должны быть круглыми числами: 0/8/15/23/30 читается хуже,
   * чем 0/10/20/30, а точность от этого не меняется. Подбираем шаг из ряда
   * 1-2-2,5-5-10 и растягиваем верх до кратного шагу. */
  function niceScale(value, ticks) {
    if (!(value > 0)) return { max: 1, step: 0.25 };
    var rough = value / ticks;
    var magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    var normalized = rough / magnitude;
    var step = (normalized <= 1 ? 1 : normalized <= 2 ? 2 :
                normalized <= 2.5 ? 2.5 : normalized <= 5 ? 5 : 10) * magnitude;
    return { max: Math.ceil(value / step) * step, step: step };
  }

  function truncate(value, pixels) {
    var limit = Math.max(8, Math.floor(pixels / 6.6));
    return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
  }

  global.Charts = {
    funnel: funnel,
    bars: bars,
    diverging: diverging,
    line: line,
    fmtNum: fmtNum,
    fmtMoney: fmtMoney
  };
})(window);
