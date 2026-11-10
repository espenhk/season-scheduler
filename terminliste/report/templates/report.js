// Client-side renderer for the season report. Inlined into the page by
// render.py; the fixture data it draws is the JSON in #report-data, whose shape
// is documented in payload.py.
//
// Layout of this file:
//   1. helpers          2. fixture model (hydration)   3. fixture representations
//   4. views            5. option controller            6. rule tooltips
//
// Only the visible option's active view is ever in the DOM (#42). Data strings
// reach the page via textContent / setAttribute only, never innerHTML.
(function () {
  'use strict';

  var DATA = JSON.parse(document.getElementById('report-data').textContent);

  // ---------------------------------------------------------------- helpers

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      if (arguments[i]) parent.appendChild(arguments[i]);
    }
    return parent;
  }

  var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function weekday(iso) {
    return WEEKDAYS[new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10))).getUTCDay()];
  }

  // "Sat 04/04" on cards, "Sat 04 Apr 2026" in the list.
  function shortDate(iso) {
    return weekday(iso) + ' ' + iso.slice(8, 10) + '/' + iso.slice(5, 7);
  }
  function longDate(iso) {
    return weekday(iso) + ' ' + iso.slice(8, 10) + ' ' + MONTHS[+iso.slice(5, 7) - 1] + ' ' + iso.slice(0, 4);
  }

  // ---------------------------------------------------------- fixture model
  //
  // Every wire entry (league match, cup round, European leg) is turned into one
  // shape, so each representation below handles all three without branching on
  // kind:
  //   uid, kind, date, time, comp, clubs[], venue, paired,
  //   home / away: {name, full, cls, shape}  (away is null for cup/European)
  //   versus: text after "v" when there is no away side, round: round name

  var SHAPE_BY_VENUE_TYPE = { home: 'square', away: 'diamond', neutral: 'circle' };

  function side(teamId, shape) {
    var team = DATA.teams[teamId];
    return { name: team.short, full: team.label, club: team.club, cls: DATA.clubs[team.club], shape: shape };
  }

  function leagueFixture(uid, m) {
    var home = side(m.h, 'square');
    var away = side(m.a, 'diamond');
    return {
      uid: uid, kind: 'league', date: m.d, time: m.t || '', comp: m.c,
      clubs: [home.club, away.club], home: home, away: away, versus: '', round: '',
      venue: DATA.venues[m.v || DATA.teams[m.h].venue], paired: !!m.p
    };
  }

  function sharedFixture(uid, e) {
    var european = e.k === 'eu';
    // A European leg with no confirmed home/away gets a dimmed square; a cup
    // round is always one of the three.
    var shape = SHAPE_BY_VENUE_TYPE[e.vt] || (european ? 'square-dim' : 'circle');
    var home = side(e.t, shape);
    return {
      uid: uid, kind: european ? 'european' : 'cup', date: e.d, time: '', comp: e.c,
      clubs: [home.club], home: home, away: null, versus: european ? e.o : 'TBD', round: e.n,
      venue: e.vl, paired: false
    };
  }

  var sharedFixtures = null;

  // The option's fixtures indexed by uid: shared cup/European entries first,
  // then its own league matches, matching the ids the views refer to.
  function hydrate(optionData) {
    if (!sharedFixtures) {
      sharedFixtures = DATA.shared.map(function (e, uid) { return sharedFixture(uid, e); });
    }
    var offset = sharedFixtures.length;
    return sharedFixtures.concat(optionData.matches.map(function (m, i) {
      return leagueFixture(offset + i, m);
    }));
  }

  // ------------------------------------------------- fixture representations
  //
  // One function per way a fixture is drawn. A new per-fixture element (a
  // badge, a data attribute) is added to the fixture model above and then to
  // these three, nowhere else.

  var SHAPE_CLASS = {
    square: 'swatch', 'square-dim': 'swatch swatch-dim', diamond: 'swatch-diamond', circle: 'swatch-neutral'
  };

  function marker(who) {
    var node = el('span', SHAPE_CLASS[who.shape] + ' ' + who.cls);
    node.setAttribute('aria-hidden', 'true');
    return node;
  }

  function named(who, className) {
    var node = el('span', className, who.name);
    node.title = who.full;
    return node;
  }

  function competitionTag(fx) {
    var comp = DATA.competitions[fx.comp];
    var tag = el('span', 'comp-tag ' + comp.cls, comp.short);
    tag.title = comp.name;
    return tag;
  }

  function versusText(fx) {
    return 'v ' + fx.versus;
  }

  // The pill used in "By competition" and "Combined · calendar".
  function fixtureCard(fx, showCompetition) {
    var card = el('span', fx.paired ? 'fx paired' : 'fx');
    card.title = fx.venue;
    card.setAttribute('data-uid', fx.uid);
    card.setAttribute('data-clubs', fx.clubs.join(' '));
    card.setAttribute('data-competition', fx.comp);
    append(
      card,
      el('span', 'd', shortDate(fx.date) + (fx.time ? ' ' + fx.time : '')),
      showCompetition ? competitionTag(fx) : null,
      marker(fx.home),
      named(fx.home, 'h')
    );
    if (fx.away) {
      append(card, el('span', 'vs', 'v'), marker(fx.away), named(fx.away, ''));
    } else {
      append(card, el('span', 'vs', versusText(fx)));
    }
    if (fx.paired) {
      var badge = el('span', 'pair-badge');
      badge.setAttribute('role', 'img');
      badge.setAttribute('aria-label', "back-to-back home day with the club's other team");
      card.appendChild(badge);
    }
    return card;
  }

  // A row of "Combined · list".
  function fixtureRow(fx) {
    var row = el('tr', 'fx-row');
    row.setAttribute('data-uid', fx.uid);
    row.setAttribute('data-clubs', fx.clubs.join(' '));
    row.setAttribute('data-competition', fx.comp);
    // The text-node space after a marker is part of the cell's inline layout.
    var space = function () { return document.createTextNode(' '); };
    var homeCell = append(el('td'), marker(fx.home), space(), named(fx.home, ''));
    var awayCell = el('td');
    if (fx.away) {
      append(awayCell, marker(fx.away), space(), named(fx.away, ''));
    } else {
      awayCell.className = 'muted';
      awayCell.textContent = fx.versus + ' (' + fx.round + ')';
    }
    return append(
      row,
      el('td', '', longDate(fx.date) + (fx.time ? ' ' + fx.time : '')),
      append(el('td'), competitionTag(fx)),
      homeCell,
      el('td', 'muted', 'v'),
      awayCell,
      el('td', '', fx.venue)
    );
  }

  // A line of the month calendar's day detail.
  function dayRow(fx) {
    var row = el('div', 'day-row');
    row.setAttribute('data-uid', fx.uid);
    row.setAttribute('data-clubs', fx.clubs.join(' '));
    append(row, competitionTag(fx), marker(fx.home), el('span', '', fx.home.name));
    if (fx.away) {
      append(row, el('span', 'vs', 'v'), marker(fx.away), el('span', '', fx.away.name));
    } else {
      append(row, el('span', 'vs', versusText(fx)));
    }
    if (fx.time) row.appendChild(el('span', 'd', fx.time));
    return row;
  }

  // ------------------------------------------------------------------ views
  //
  // A view is `function (ctx) -> Node`, drawn into its pane's .pane-body when
  // the pane is activated. `ctx.visible(ids)` is the single place the club
  // filter applies; later filters belong in `ctx.show`.

  function fixturesOf(ctx, ids, showCompetition) {
    var box = el('div', 'fixtures');
    ctx.visible(ids).forEach(function (uid) {
      box.appendChild(fixtureCard(ctx.fx[uid], showCompetition));
    });
    return box;
  }

  function roundLabel(title, text, note) {
    var label = append(el('div', 'rlabel'), el('b', '', title), document.createTextNode(text));
    if (note) label.title = note;
    return label;
  }

  function byCompetitionView(ctx) {
    var root = document.createDocumentFragment();
    ctx.opt.data.byCompetition.concat(DATA.sharedByCompetition).forEach(function (view) {
      // Under a club filter a round (or a whole competition) with nothing left
      // collapses rather than leaving a bare heading.
      var rounds = view.rounds.filter(function (round) {
        return !ctx.club || !round.ids.length || ctx.visible(round.ids).length;
      });
      if (ctx.club && view.rounds.length && !rounds.length) return;
      var comp = append(el('div', 'comp'), el('h3', '', view.name), el('div', 'meta', view.meta));
      rounds.forEach(function (round) {
        append(
          comp,
          append(el('div', 'round'), roundLabel(round.title, round.text, round.note), fixturesOf(ctx, round.ids, false))
        );
      });
      root.appendChild(comp);
    });
    return root;
  }

  function combinedCalendarView(ctx) {
    var root = document.createDocumentFragment();
    ctx.opt.data.weeks.forEach(function (week) {
      if (!ctx.visible(week.ids).length) return;
      append(
        root,
        append(el('div', 'round'), roundLabel(week.label, week.dates), fixturesOf(ctx, week.ids, true))
      );
    });
    return root;
  }

  function combinedListView(ctx) {
    var head = el('tr');
    ['Date', 'Competition', 'Home', '', 'Away', 'Venue'].forEach(function (name) {
      head.appendChild(el('th', '', name));
    });
    var body = el('tbody');
    ctx.visible(ctx.opt.data.list).forEach(function (uid) {
      body.appendChild(fixtureRow(ctx.fx[uid]));
    });
    return append(el('table'), append(el('thead'), head), body);
  }

  function monthCalendarView(ctx) {
    var data = ctx.opt.data;
    var state = ctx.opt.state;
    var days = {};
    data.days.forEach(function (day) { days[day.date] = day; });
    var root = el('div');

    // Competitions with something visible that day, for the dots.
    function dotsOf(day) {
      if (!day) return [];
      return day.comps.filter(function (comp) {
        return ctx.visible(day.ids).some(function (uid) { return ctx.fx[uid].comp === comp; });
      });
    }

    data.months.forEach(function (month) {
      var grid = el('div', 'month-grid');
      var head = el('div', 'month-grid-row month-grid-head');
      WEEKDAYS.slice(1).concat(WEEKDAYS[0]).forEach(function (name) { head.appendChild(el('span', '', name)); });
      grid.appendChild(head);
      month.weeks.forEach(function (week) {
        var row = el('div', 'month-grid-row');
        week.forEach(function (iso) {
          var dots = dotsOf(days[iso]);
          var button = el('button', 'cal-day' + (iso.slice(0, 7) === month.key ? '' : ' out-month'));
          button.type = 'button';
          button.setAttribute('data-date', iso);
          var selected = iso === state.date;
          if (selected) button.classList.add('selected');
          button.setAttribute('aria-pressed', String(selected));
          button.setAttribute(
            'aria-label',
            iso + (dots.length ? ': ' + dots.map(function (c) { return DATA.competitions[c].name; }).join(', ') : '')
          );
          var dotBox = el('span', 'cal-dots');
          dots.forEach(function (comp) {
            var dot = el('span', 'cal-dot ' + DATA.competitions[comp].cls);
            dot.title = DATA.competitions[comp].name;
            dot.setAttribute('aria-hidden', 'true');
            dotBox.appendChild(dot);
          });
          append(button, el('span', 'cal-daynum', String(+iso.slice(8))), dotBox);
          row.appendChild(button);
        });
        grid.appendChild(row);
      });
      append(root, append(el('div', 'month'), el('h4', '', month.label), grid));
    });

    var details = el('div', 'day-details');
    root.appendChild(details);

    // Tapping a day only redraws the detail block, not the grid.
    function showDay() {
      details.textContent = '';
      var day = state.date && days[state.date];
      var ids = day ? ctx.visible(day.ids) : [];
      if (ids.length) {
        var box = el('div', 'fixtures');
        ids.forEach(function (uid) { box.appendChild(dayRow(ctx.fx[uid])); });
        append(details, append(el('div', 'day-detail'), append(el('div', 'rlabel'), el('b', '', day.label)), box));
      } else {
        details.appendChild(el('div', 'day-detail-empty', state.date ? 'No games on this day.' : 'Tap a day above to see its games here.'));
      }
      return details.firstChild;
    }

    root.addEventListener('click', function (event) {
      var button = event.target.closest('.cal-day');
      if (!button) return;
      state.date = button.dataset.date;
      root.querySelectorAll('.cal-day').forEach(function (other) {
        // A date can appear twice: in its month and as the next month's padding.
        var isSelected = other.dataset.date === state.date;
        other.classList.toggle('selected', isSelected);
        other.setAttribute('aria-pressed', String(isSelected));
      });
      // The calendar can span well over a year (cup rounds run into next
      // spring), so the detail block sits far below a day tapped in an early
      // month; bring it into view.
      showDay().scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
    showDay();
    return root;
  }

  var VIEWS = {
    'by-competition': byCompetitionView,
    'month-calendar': monthCalendarView,
    'combined-calendar': combinedCalendarView,
    'combined-list': combinedListView
  };

  // ------------------------------------------------------ option controller
  //
  // One controller per option panel, each with its own view and club state.
  // `render()` is the single place a view is activated: it draws the active
  // view into its pane and empties the others.

  function Option(index, panel) {
    this.data = DATA.options[index];
    this.panel = panel;
    this.fx = null;
    this.state = { view: panel.querySelector('.view-btn').dataset.view, club: '', date: '' };
    this.clubSelect = panel.querySelector('.club-select');
    this.headlines = panel.querySelector('.headlines');
    this.headlineScope = panel.querySelector('.headline-scope');
    var self = this;
    panel.querySelectorAll('.view-btn').forEach(function (button) {
      button.addEventListener('click', function () {
        self.state.view = button.dataset.view;
        self.render();
      });
    });
    this.clubSelect.addEventListener('change', function () {
      self.state.club = self.clubSelect.value;
      self.render();
    });
  }

  Option.prototype.context = function () {
    var club = this.state.club;
    var fx = this.fx;
    function show(f) { return !club || f.clubs.indexOf(club) !== -1; }
    return {
      opt: this,
      fx: fx,
      club: club,
      show: show,
      visible: function (ids) { return ids.filter(function (uid) { return show(fx[uid]); }); }
    };
  };

  // Headline numbers restated for the selected club, in place of the
  // season-wide ones.
  Option.prototype.renderHeadlines = function () {
    var club = this.state.club;
    var rows = (club && this.data.clubHeadlines[club]) || this.data.headlines;
    this.headlines.textContent = '';
    var box = this.headlines;
    rows.forEach(function (row) {
      append(box, append(el('div', 'headline'), el('div', 'v', row.value), el('div', 'l', row.label)));
    });
    this.headlineScope.textContent = club && this.data.clubHeadlines[club]
      ? this.clubSelect.options[this.clubSelect.selectedIndex].text + '’s matches only:'
      : '';
  };

  Option.prototype.render = function () {
    if (!this.fx) this.fx = hydrate(this.data);
    var state = this.state;
    var ctx = this.context();
    this.panel.querySelectorAll('.view-btn').forEach(function (button) {
      var active = button.dataset.view === state.view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    });
    this.panel.querySelectorAll('.view-pane').forEach(function (pane) {
      var active = pane.dataset.view === state.view;
      pane.hidden = !active;
      var body = pane.querySelector('.pane-body');
      body.textContent = '';
      if (active) body.appendChild(VIEWS[state.view](ctx));
    });
    this.renderHeadlines();
  };

  // Frees the panel's view while another option is shown.
  Option.prototype.release = function () {
    this.panel.querySelectorAll('.pane-body').forEach(function (body) { body.textContent = ''; });
  };

  var panels = document.querySelectorAll('.panel');
  var options = [];
  var current = 0;

  // Only the first option's panel is in the DOM at load; the others arrive as
  // inert <template> content and are instantiated when their tab is first opened.
  function option(index) {
    if (!options[index]) {
      var template = panels[index].querySelector(':scope > template');
      if (template) {
        panels[index].appendChild(template.content);
        template.remove();
      }
      options[index] = new Option(index, panels[index]);
    }
    return options[index];
  }

  document.querySelectorAll('.tab').forEach(function (tab, index) {
    tab.addEventListener('click', function () {
      document.querySelectorAll('.tab').forEach(function (other) {
        other.setAttribute('aria-selected', String(other === tab));
      });
      option(current).release();
      panels[current].hidden = true;
      current = index;
      panels[current].hidden = false;
      option(current).render();
    });
  });
  if (panels.length) option(0).render();

  // ---------------------------------------------------------- rule tooltips
  //
  // Hover/focus works via CSS alone; this adds tap-to-toggle for touch devices
  // (no hover state), positions the (position:fixed) tooltip against its icon
  // so an ancestor's overflow can't clip it, and keeps it inside the viewport
  // and attached to its icon while the page scrolls.
  function positionTooltip(wrap) {
    var icon = wrap.querySelector('.info-icon');
    var tooltip = wrap.querySelector('.tooltip');
    if (!icon || !tooltip) return;
    var iconRect = icon.getBoundingClientRect();
    tooltip.style.left = iconRect.left + 'px';
    tooltip.style.top = (iconRect.bottom + 6) + 'px';
    var rect = tooltip.getBoundingClientRect();
    var rightOverflow = rect.right - (window.innerWidth - 8);
    var leftOverflow = 8 - rect.left;
    if (rightOverflow > 0) {
      tooltip.style.left = (iconRect.left - rightOverflow) + 'px';
    } else if (leftOverflow > 0) {
      tooltip.style.left = leftOverflow + 'px';
    }
  }
  function closeTooltips() {
    document.querySelectorAll('.info-wrap.open').forEach(function (w) { w.classList.remove('open'); });
  }
  function repositionVisibleTooltips() {
    document.querySelectorAll('.info-wrap:hover, .info-wrap:focus-within, .info-wrap.open').forEach(positionTooltip);
  }
  // Delegated, since panels other than the first are added to the page later.
  function tooltipOwner(event) {
    return event.target.closest ? event.target.closest('.info-wrap') : null;
  }
  ['mouseover', 'focusin'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var wrap = tooltipOwner(e);
      if (wrap) positionTooltip(wrap);
    });
  });
  document.addEventListener('click', function (e) {
    var icon = e.target.closest ? e.target.closest('.info-icon') : null;
    if (!icon) {
      closeTooltips();
      return;
    }
    // Some icons sit inside a <summary>; without this the click would also
    // toggle the enclosing <details>.
    e.preventDefault();
    var wrap = icon.closest('.info-wrap');
    var wasOpen = wrap.classList.contains('open');
    closeTooltips();
    if (!wasOpen) {
      wrap.classList.add('open');
      positionTooltip(wrap);
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeTooltips();
  });
  // Capture phase so this also fires for scrolling inside .cal-wrap, whose
  // 'scroll' event doesn't bubble.
  window.addEventListener('scroll', repositionVisibleTooltips, true);
  window.addEventListener('resize', repositionVisibleTooltips);
})();
