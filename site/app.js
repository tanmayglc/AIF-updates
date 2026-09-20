/* SEBI AIF & IFSCA fund management tracker.
 *
 * Reads data/news.json, renders it, and filters entirely in the browser.
 * No dependencies and no build step: the whole site is three static files
 * plus the JSON the scraper writes.
 */
(function () {
  'use strict';

  var DATA_URL = 'data/news.json';
  var NEW_WINDOW_DAYS = 7;   // "new" marker on recently added items
  var PERIODS = [
    { id: 'all', label: 'All time' },
    { id: '30', label: 'Last 30 days' },
    { id: '90', label: 'Last 90 days' },
    { id: '365', label: 'Last year' },
    { id: '1095', label: 'Last 3 years' }
  ];

  var el = {
    meta: document.getElementById('meta'),
    q: document.getElementById('q'),
    reset: document.getElementById('reset'),
    regulator: document.getElementById('f-regulator'),
    type: document.getElementById('f-type'),
    period: document.getElementById('f-period'),
    sort: document.getElementById('sort'),
    count: document.getElementById('count'),
    items: document.getElementById('items'),
    empty: document.getElementById('empty'),
    error: document.getElementById('error'),
    sources: document.getElementById('sources')
  };

  var state = {
    all: [],
    regulator: 'all',
    type: 'all',
    period: 'all',
    query: '',
    sort: 'date-desc'
  };

  // --- helpers ------------------------------------------------------------

  function esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function daysAgo(n) {
    var d = new Date();
    d.setHours(0, 0, 0, 0);
    d.setDate(d.getDate() - n);
    return d.toISOString().slice(0, 10);
  }

  function formatDateTime(iso) {
    var d = new Date(iso);
    if (isNaN(d)) { return iso || 'unknown'; }
    return d.toLocaleString(undefined, {
      day: 'numeric', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit'
    });
  }

  function plural(n, one, many) {
    return n.toLocaleString() + ' ' + (n === 1 ? one : many);
  }

  function terms(query) {
    return query.toLowerCase().split(/\s+/).filter(Boolean);
  }

  function isNew(item) {
    return !!(item.first_seen &&
              item.first_seen.slice(0, 10) >= daysAgo(NEW_WINDOW_DAYS));
  }

  /* Highlight every search term inside a title. Matching happens on the raw
     text and each segment is escaped separately, so a <mark> can never land
     in the middle of an HTML entity and no markup is ever injected. */
  function highlight(text, query) {
    var words = terms(query);
    if (!words.length) { return esc(text); }
    var pattern;
    try {
      pattern = new RegExp(
        words.map(function (w) {
          return w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        }).join('|'), 'gi');
    } catch (e) {
      return esc(text);
    }

    var out = '';
    var last = 0;
    var m;
    while ((m = pattern.exec(text)) !== null) {
      if (m[0] === '') { pattern.lastIndex += 1; continue; }
      out += esc(text.slice(last, m.index)) + '<mark>' + esc(m[0]) + '</mark>';
      last = m.index + m[0].length;
    }
    return out + esc(text.slice(last));
  }

  // --- filtering ----------------------------------------------------------

  function matches(item, opts) {
    opts = opts || {};
    if (!opts.ignoreRegulator && state.regulator !== 'all' &&
        item.regulator !== state.regulator) { return false; }
    if (!opts.ignoreType && state.type !== 'all' &&
        item.doc_type !== state.type) { return false; }
    if (!opts.ignorePeriod && state.period !== 'all') {
      var cutoff = daysAgo(parseInt(state.period, 10));
      if (!item.date || item.date < cutoff) { return false; }
    }
    if (state.query) {
      var haystack = (item.title + ' ' + item.doc_type + ' ' + item.regulator + ' ' +
                      (item.matched_keywords || []).join(' ')).toLowerCase();
      var words = terms(state.query);
      for (var i = 0; i < words.length; i++) {
        if (haystack.indexOf(words[i]) === -1) { return false; }
      }
    }
    return true;
  }

  function sortItems(items) {
    var sorted = items.slice();
    var undated = '0000-00-00';
    if (state.sort === 'title-asc') {
      sorted.sort(function (a, b) { return a.title.localeCompare(b.title); });
    } else if (state.sort === 'date-asc') {
      // Undated items stay at the end in both date orders.
      sorted.sort(function (a, b) {
        if (!a.date && !b.date) { return a.title.localeCompare(b.title); }
        if (!a.date) { return 1; }
        if (!b.date) { return -1; }
        return a.date < b.date ? -1 : a.date > b.date ? 1 : 0;
      });
    } else if (state.sort === 'added-desc') {
      sorted.sort(function (a, b) {
        var x = a.first_seen || '', y = b.first_seen || '';
        if (x !== y) { return x < y ? 1 : -1; }
        return (b.date || undated).localeCompare(a.date || undated);
      });
    } else {
      sorted.sort(function (a, b) {
        var x = a.date || undated, y = b.date || undated;
        if (x !== y) { return x < y ? 1 : -1; }
        return a.title.localeCompare(b.title);
      });
    }
    return sorted;
  }

  // --- filter controls ----------------------------------------------------

  /* Counts for one filter's options, computed with that filter itself ignored
     so the numbers answer "how many would I get if I picked this?". Every
     value the dataset knows about is seeded at 0, so an option keeps its place
     in the list even when nothing currently matches it. */
  function countsFor(field, ignoreKey) {
    var opts = {};
    opts[ignoreKey] = true;
    var counts = { all: 0 };
    if (field) {
      state.all.forEach(function (item) { counts[item[field]] = 0; });
    }
    state.all.forEach(function (item) {
      if (!matches(item, opts)) { return; }
      counts.all += 1;
      if (field) { counts[item[field]] += 1; }
    });
    return counts;
  }

  function renderSegmented(counts) {
    var values = Object.keys(counts)
      .filter(function (k) { return k !== 'all' && (counts[k] > 0 || k === state.regulator); })
      .sort();
    var options = [{ id: 'all', label: 'All' }].concat(values.map(function (v) {
      return { id: v, label: v };
    }));

    el.regulator.innerHTML = '';
    options.forEach(function (o) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'seg';
      btn.setAttribute('aria-pressed', String(o.id === state.regulator));
      btn.innerHTML = esc(o.label) + '<span class="n">' + counts[o.id] + '</span>';
      btn.addEventListener('click', function () {
        state.regulator = o.id;
        render();
      });
      el.regulator.appendChild(btn);
    });
  }

  function renderSelect(node, options, active) {
    node.innerHTML = '';
    options.forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o.id;
      opt.textContent = o.count == null ? o.label : o.label + '  (' + o.count + ')';
      if (o.id === active) { opt.selected = true; }
      node.appendChild(opt);
    });
  }

  function renderFilters() {
    renderSegmented(countsFor('regulator', 'ignoreRegulator'));

    var typeCounts = countsFor('doc_type', 'ignoreType');
    var types = Object.keys(typeCounts)
      .filter(function (k) { return k !== 'all'; })
      .sort(function (a, b) {
        return (typeCounts[b] - typeCounts[a]) || a.localeCompare(b);
      });
    renderSelect(el.type,
      [{ id: 'all', label: 'All types', count: typeCounts.all }].concat(
        types.map(function (t) {
          return { id: t, label: t, count: typeCounts[t] };
        })),
      state.type);

    // Each period option reports what that window alone would return.
    var periodOptions = PERIODS.map(function (p) {
      var saved = state.period;
      state.period = p.id;
      var n = countsFor(null, 'ignoreNothing').all;
      state.period = saved;
      return { id: p.id, label: p.label, count: n };
    });
    renderSelect(el.period, periodOptions, state.period);
  }

  // --- rendering ----------------------------------------------------------

  function renderItem(item) {
    var li = document.createElement('li');
    li.className = 'item';

    var flagNew = !state.suppressNew && isNew(item);
    var keywords = (item.matched_keywords || []).slice(0, 4);

    var date = '<time class="item-date"' +
      (item.date ? ' datetime="' + esc(item.date) + '"' : '') + '>' +
      esc(item.date_display || 'Undated') + '</time>';

    var kicker = '<p class="item-kicker">' +
      '<span class="reg reg--' + esc(item.regulator) + '">' + esc(item.regulator) + '</span>' +
      '<span class="kicker-sep" aria-hidden="true">/</span>' +
      '<span class="kicker-type">' + esc(item.doc_type) + '</span>' +
      (flagNew ? '<span class="flag-new">New</span>' : '') +
      '</p>';

    var title = '<h2 class="item-title"><a href="' + esc(item.url) + '" target="_blank" ' +
      'rel="noopener noreferrer">' + highlight(item.title, state.query) + '</a></h2>';

    var tags = '';
    if (keywords.length || item.source_page) {
      tags = '<p class="item-tags">' +
        keywords.map(function (k) {
          return '<span class="kw">' + esc(k) + '</span>';
        }).join('') +
        (item.source_page
          ? '<a class="src" href="' + esc(item.source_page) + '" target="_blank" ' +
            'rel="noopener noreferrer">' + esc(item.source_name || 'Source listing') + '</a>'
          : '') +
        '</p>';
    }

    li.innerHTML = date + '<div class="item-body">' + kicker + title + tags + '</div>';
    return li;
  }

  function render() {
    renderFilters();

    // On the tracker's first run everything is "new", which is noise rather
    // than news. Only flag arrivals when they are a minority of the dataset.
    var newCount = state.all.filter(isNew).length;
    state.suppressNew = newCount > state.all.length * 0.3;

    var visible = sortItems(state.all.filter(function (i) { return matches(i); }));

    el.items.innerHTML = '';
    var frag = document.createDocumentFragment();
    visible.forEach(function (item) { frag.appendChild(renderItem(item)); });
    el.items.appendChild(frag);

    el.empty.hidden = visible.length > 0;
    el.count.textContent = visible.length === state.all.length
      ? plural(visible.length, 'update', 'updates')
      : plural(visible.length, 'update', 'updates') + ' of ' + state.all.length.toLocaleString();

    el.reset.hidden = !(state.regulator !== 'all' || state.type !== 'all' ||
                        state.period !== 'all' || state.query !== '');
  }

  function renderMeta(payload) {
    var by = (payload.counts && payload.counts.by_regulator) || {};
    var parts = Object.keys(by).sort().map(function (k) { return by[k] + ' ' + k; });
    el.meta.innerHTML =
      '<strong>' + esc(String(payload.counts.total)) + '</strong> updates tracked' +
      (parts.length ? ' &middot; ' + esc(parts.join(', ')) : '') +
      ' &middot; last updated <strong>' + esc(formatDateTime(payload.generated_at)) + '</strong>';

    var failed = (payload.sources && payload.sources.failed) || [];
    var ok = (payload.sources && payload.sources.ok) || [];
    el.sources.textContent = 'Sources checked on the last run: ' + ok.length +
      (failed.length ? '. Unavailable: ' + failed.join(', ') + '.' : '.');
  }

  // --- wiring -------------------------------------------------------------

  function debounce(fn, ms) {
    var t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, ms);
    };
  }

  el.q.addEventListener('input', debounce(function () {
    state.query = el.q.value.trim();
    render();
  }, 140));

  el.type.addEventListener('change', function () { state.type = el.type.value; render(); });
  el.period.addEventListener('change', function () { state.period = el.period.value; render(); });
  el.sort.addEventListener('change', function () { state.sort = el.sort.value; render(); });

  el.reset.addEventListener('click', function () {
    state.regulator = state.type = state.period = 'all';
    state.query = '';
    el.q.value = '';
    render();
    el.q.focus();
  });

  function fail(message) {
    el.error.hidden = false;
    el.error.textContent = message;
    el.meta.textContent = 'Could not load the data file.';
    el.count.textContent = '';
  }

  fetch(DATA_URL, { cache: 'no-cache' })
    .then(function (r) {
      if (!r.ok) { throw new Error('HTTP ' + r.status); }
      return r.json();
    })
    .then(function (payload) {
      state.all = (payload.items || []).filter(function (i) { return i && i.url && i.title; });
      renderMeta(payload);
      render();
    })
    .catch(function (err) {
      fail('Could not load ' + DATA_URL + ' (' + err.message + '). ' +
           'If you are opening this file directly from disk, serve the folder over ' +
           'HTTP instead - for example: python -m http.server --directory site 8000');
    });
})();
