// Client-side renderer for the season report. Inlined into the page by
// render.py; the fixture data it draws is the JSON in #report-data, whose shape
// is documented in payload.py.
//
// Layout of this file:
//   1. helpers          2. fixture model (hydration)   3. fixture representations
//   4. views            5. option controller (incl. the rule lens)
//   6. rule tooltips    7. scorecard links and fixture lists
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

  function dayIndex(iso) {
    return new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10))).getUTCDay();
  }

  function weekday(iso) {
    return WEEKDAYS[dayIndex(iso)];
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
  //   uid, kind, date, time, comp, clubs[], venue,
  //   home / away: {name, full, cls, shape}  (away is null for cup/European)
  //   versus: text after "v" when there is no away side, round: round name
  //   flags[]: scoring badges, one per rule {rule, delta, items[{delta, detail, n, group}]};
  //   rules: their rule ids; delta: their net; tint: 'good' | 'cost' | ''

  var SHAPE_BY_VENUE_TYPE = { home: 'square', away: 'diamond', neutral: 'circle' };

  function side(teamId, shape) {
    var team = DATA.teams[teamId];
    return { name: team.short, full: team.label, club: team.club, cls: DATA.clubs[team.club], shape: shape };
  }

  // Net points a fixture needs to tint its card edge (set in render.py).
  var TINT_GOOD = DATA.tint.good;
  var TINT_COST = DATA.tint.cost;

  // The wire's flags (see payload.py) grouped into one badge per rule, in
  // rule-table order.
  function decodeFlags(wireFlags, notes, groups) {
    var byRule = {};
    var flags = [];
    (wireFlags || []).forEach(function (w) {
      var rule = DATA.rules[w[0]];
      var flag = byRule[rule.id];
      if (!flag) {
        flag = byRule[rule.id] = { rule: rule, delta: 0, items: [] };
        flags.push(flag);
      }
      flag.delta += w[1];
      flag.items.push({ delta: w[1], detail: notes[w[2] || 0], n: w[3] || 1, group: groups[w[4] || 0] });
    });
    return flags;
  }

  function withFlags(fx, wireFlags, notes, groups) {
    fx.flags = decodeFlags(wireFlags, notes, groups);
    fx.rules = fx.flags.map(function (flag) { return flag.rule.id; });
    fx.delta = fx.flags.reduce(function (sum, flag) { return sum + flag.delta; }, 0);
    fx.broken = fx.flags.some(function (flag) { return flag.rule.hard; });
    fx.tint = fx.delta >= TINT_GOOD ? 'good' : fx.delta <= TINT_COST ? 'cost' : '';
    return fx;
  }

  function leagueFixture(uid, m, notes, groups) {
    var home = side(m.h, 'square');
    var away = side(m.a, 'diamond');
    return withFlags({
      uid: uid, kind: 'league', date: m.d, time: m.t || '', comp: m.c,
      clubs: [home.club, away.club], home: home, away: away, versus: '', round: '',
      venue: DATA.venues[m.v || DATA.teams[m.h].venue]
    }, m.f, notes, groups);
  }

  function sharedFixture(uid, e) {
    var european = e.k === 'eu';
    // A European leg with no confirmed home/away gets a dimmed square; a cup
    // round is always one of the three.
    var shape = SHAPE_BY_VENUE_TYPE[e.vt] || (european ? 'square-dim' : 'circle');
    var home = side(e.t, shape);
    return withFlags({
      uid: uid, kind: european ? 'european' : 'cup', date: e.d, time: '', comp: e.c,
      clubs: [home.club], home: home, away: null, versus: european ? e.o : 'TBD', round: e.n,
      venue: e.vl
    }, null, null, null);
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
      return leagueFixture(offset + i, m, optionData.notes, optionData.groups);
    }));
  }

  // ------------------------------------------------- fixture representations
  //
  // One function per way a fixture is drawn. A new per-fixture element (a
  // badge, a data attribute) is added to the fixture model above and then to
  // these three, nowhere else.
  //
  // Every representation carries data-uid, data-clubs and data-rules /
  // data-delta: the space-separated ids of the rules badged on the fixture and
  // their net points ("" and "0" when none), for filters and summaries (#29).

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

  function tag(node, fx) {
    node.setAttribute('data-uid', fx.uid);
    node.setAttribute('data-clubs', fx.clubs.join(' '));
    node.setAttribute('data-rules', fx.rules.join(' '));
    node.setAttribute('data-delta', String(Math.round(fx.delta * 100) / 100));
    return node;
  }

  // The card edge, by what the fixture's rules net out to.
  function edgeClass(fx) {
    return fx.broken ? ' edge-broken' : fx.tint ? ' edge-' + fx.tint : '';
  }

  // -- scoring badges
  //
  // One badge per rule. Its tooltip (the rule-description tooltip's style and
  // positioning) is built on first use, so a page full of badges stays light.

  var flagOf = new WeakMap();
  var HOVER_NONE = window.matchMedia('(hover: none)').matches;
  var flagTip = null;
  var flagTipOwner = null;

  function signed(delta) {
    var text = (Math.round(delta * 10) / 10).toFixed(1);
    return delta > 0 ? '+' + text : text.replace('-', '\u2212');
  }

  function flagBadge(flag) {
    var polarity = flag.rule.hard ? 'broken' : flag.delta > 0 ? 'good' : 'cost';
    var badge = el('button', 'info-wrap flag flag-' + polarity, flag.rule.label);
    badge.type = 'button';
    badge.setAttribute('data-rule', flag.rule.id);
    // Keeps the tooltip child out of the button's accessible name.
    badge.setAttribute('aria-label', flag.rule.label);
    flagOf.set(badge, flag);
    return badge;
  }

  // One tooltip shared by every badge, hosted on <body>: a badge inside a
  // dimmed fixture would otherwise fade its own tooltip along with the card.
  function showFlagTip(badge) {
    var flag = flagOf.get(badge);
    if (!flagTip) {
      flagTip = el('span', 'tooltip');
      flagTip.id = 'flag-tooltip';
      flagTip.setAttribute('role', 'tooltip');
      document.body.appendChild(flagTip);
    }
    hideFlagTip();
    flagTip.textContent = '';
    var points = flag.rule.hard ? 'broken rule' : flag.delta === 0 ? 'reward forgone' : signed(flag.delta) + ' pts';
    append(flagTip, el('b', 'tt-head', flag.rule.label + ' · ' + points));
    flag.items.forEach(function (item) {
      var text = item.detail;
      if (item.n > 1) text += ' (this fixture’s share of ' + signed(item.delta * item.n) + ', split over ' + item.n + ' fixtures)';
      if (text) flagTip.appendChild(el('span', 'tt-detail', text));
    });
    append(flagTip, el('span', 'tt-hint', (HOVER_NONE ? 'Tap again' : 'Click') + ' to highlight this rule in the calendar'));
    badge.setAttribute('aria-describedby', flagTip.id);
    flagTipOwner = badge;
    flagTip.classList.add('show');
    placeTooltip(badge, flagTip);
  }

  function hideFlagTip() {
    if (!flagTip) return;
    flagTip.classList.remove('show');
    if (flagTipOwner) flagTipOwner.removeAttribute('aria-describedby');
    flagTipOwner = null;
  }

  function flagBox(fx) {
    if (!fx.flags.length) return null;
    var box = el('span', 'flags');
    fx.flags.forEach(function (flag) { box.appendChild(flagBadge(flag)); });
    return box;
  }

  // The "Combined · list" score column: the fixture's visible net points.
  function scoreCell(fx) {
    var cell = el('td', 'fx-score ' + netClass(fx.delta), netText(fx.delta));
    cell.title = 'Net points from this fixture’s rule badges';
    return cell;
  }

  // The pill used in "By competition" and "Combined · calendar".
  function fixtureCard(fx, showCompetition) {
    var card = tag(el('span', 'fx' + edgeClass(fx)), fx);
    card.title = fx.venue;
    card.setAttribute('data-competition', fx.comp);
    var main = append(
      el('span', 'fx-main'),
      el('span', 'd', shortDate(fx.date) + (fx.time ? ' ' + fx.time : '')),
      showCompetition ? competitionTag(fx) : null,
      marker(fx.home),
      named(fx.home, 'h')
    );
    if (fx.away) {
      append(main, el('span', 'vs', 'v'), marker(fx.away), named(fx.away, ''));
    } else {
      append(main, el('span', 'vs', versusText(fx)));
    }
    return append(card, main, flagBox(fx));
  }

  // A row of "Combined · list".
  function fixtureRow(fx) {
    var row = tag(el('tr', 'fx-row' + edgeClass(fx)), fx);
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
      el('td', '', fx.venue),
      append(el('td'), flagBox(fx)),
      scoreCell(fx)
    );
  }

  // A line of the month calendar's day detail.
  function dayRow(fx) {
    var row = tag(el('div', 'day-row' + edgeClass(fx)), fx);
    append(row, competitionTag(fx), marker(fx.home), el('span', '', fx.home.name));
    if (fx.away) {
      append(row, el('span', 'vs', 'v'), marker(fx.away), el('span', '', fx.away.name));
    } else {
      append(row, el('span', 'vs', versusText(fx)));
    }
    if (fx.time) row.appendChild(el('span', 'd', fx.time));
    return append(row, flagBox(fx));
  }

  // ------------------------------------------------------------ indicators
  //
  // Totals over the fixtures a view shows: the net of their visible badges (the
  // sum of data-delta, hidden rewards excluded). "Midweek" means no Fri-Sun game.

  function netClass(n) { return n > 0 ? 'good' : n < 0 ? 'cost' : ''; }

  // Signed points, one decimal only when fractional; zero is "0".
  function netText(n) {
    var mag = Math.round(Math.abs(n) * 10) / 10;
    if (mag === 0) return '0';
    return (n > 0 ? '+' : '−') + (Number.isInteger(mag) ? String(mag) : mag.toFixed(1));
  }

  // `badged`: some shown game carries a badge, so the net means something.
  // `costs`: shown games with a visible badge that takes points off (a hard
  // break is not a cost); `top` is the cost badge on the most of them.
  function totals(fx, uids) {
    var sum = { games: uids.length, net: 0, badged: false, costs: 0, top: null, broken: 0, midweek: uids.length > 0 };
    var costRules = {};
    var costOrder = [];
    uids.forEach(function (uid) {
      var f = fx[uid];
      sum.net += f.delta;
      if (f.flags.length) sum.badged = true;
      if (f.broken) sum.broken++;
      var day = dayIndex(f.date);
      if (day === 0 || day > 4) sum.midweek = false;
      var seen = {};
      var costly = false;
      f.flags.forEach(function (flag) {
        if (flag.rule.hard || flag.delta >= 0) return;
        costly = true;
        if (seen[flag.rule.id]) return;
        seen[flag.rule.id] = true;
        if (!costRules[flag.rule.id]) {
          costRules[flag.rule.id] = { rule: flag.rule, n: 0 };
          costOrder.push(flag.rule.id);
        }
        costRules[flag.rule.id].n++;
      });
      if (costly) sum.costs++;
    });
    costOrder.forEach(function (id) {
      if (!sum.top || costRules[id].n > sum.top.n) sum.top = costRules[id];
    });
    sum.net = Math.round(sum.net * 100) / 100;
    return sum;
  }

  // Parts are joined with " · " and each omitted when it would read zero: the net
  // only when a visible badge is on the games, costs and the repeated badge only
  // when there are any.
  function summaryNode(sum, midweekTag) {
    var node = el('div', 'rsum');
    node.title = 'Net points from the rule badges on the games shown (hidden rewards are not counted)';
    var parts = [];
    if (sum.badged) parts.push(el('span', 'rnet ' + netClass(sum.net), netText(sum.net) + ' pts'));
    parts.push(plural(sum.games, 'game', 'games'));
    if (sum.costs) parts.push(sum.costs + ' with costs');
    if (sum.top && sum.top.n >= 2) parts.push(sum.top.n + ' × ' + sum.top.rule.label);
    if (sum.broken) parts.push(plural(sum.broken, 'hard break', 'hard breaks'));
    parts.forEach(function (part, i) {
      if (i) node.appendChild(document.createTextNode(' · '));
      node.appendChild(typeof part === 'string' ? document.createTextNode(part) : part);
    });
    if (midweekTag && sum.midweek) {
      var tagNode = el('span', 'rtag', 'midweek');
      tagNode.title = 'Midweek: every game falls Monday to Thursday';
      node.appendChild(tagNode);
    }
    return node;
  }

  // A day's game count, with its net when a visible badge sits on one of its games.
  function dayFigure(sum) {
    var figure = el('span', 'cal-net');
    figure.setAttribute('aria-hidden', 'true');
    figure.appendChild(el('span', 'cal-count', String(sum.games)));
    if (sum.badged) {
      figure.appendChild(document.createTextNode(' · '));
      figure.appendChild(el('span', 'cal-value ' + netClass(sum.net), netText(sum.net)));
    }
    return figure;
  }

  // ------------------------------------------------------------------ views
  //
  // A view is `function (ctx) -> Node`, drawn into its pane's .pane-body when
  // the pane is activated. `ctx.visible(ids)` is the single place the filters
  // (club, and the lens's "only show matching") apply; they live in `ctx.show`.
  // `ctx.filtering` is true while either hides anything, so rounds and days left
  // empty collapse.

  function noMatches() {
    return el('div', 'desc', 'No fixtures match the club and rule selected.');
  }

  function fixturesOf(ctx, ids, showCompetition) {
    var box = el('div', 'fixtures');
    ctx.visible(ids).forEach(function (uid) {
      box.appendChild(fixtureCard(ctx.fx[uid], showCompetition));
    });
    return box;
  }

  // `sum` is the round's or week's `totals`; its edge takes the net's colour.
  function roundLabel(title, text, note, sum) {
    var label = append(el('div', 'rlabel'), el('b', '', title), document.createTextNode(text));
    if (note) label.title = note;
    if (sum.games) {
      var edge = sum.badged ? netClass(sum.net) : '';
      if (edge) label.classList.add('rl-' + edge);
      label.appendChild(summaryNode(sum, true));
    }
    return label;
  }

  function byCompetitionView(ctx) {
    var root = document.createDocumentFragment();
    ctx.opt.data.byCompetition.concat(DATA.sharedByCompetition).forEach(function (view) {
      // Under a filter a round (or a whole competition) with nothing left
      // collapses rather than leaving a bare heading.
      var rounds = view.rounds.filter(function (round) {
        return !ctx.filtering || !round.ids.length || ctx.visible(round.ids).length;
      });
      if (ctx.filtering && view.rounds.length && !rounds.length) return;
      var comp = append(el('div', 'comp'), el('h3', '', view.name), el('div', 'meta', view.meta));
      rounds.forEach(function (round) {
        append(
          comp,
          append(
            el('div', 'round'),
            roundLabel(round.title, round.text, round.note, totals(ctx.fx, ctx.visible(round.ids))),
            fixturesOf(ctx, round.ids, false)
          )
        );
      });
      root.appendChild(comp);
    });
    return root;
  }

  function combinedCalendarView(ctx) {
    var root = document.createDocumentFragment();
    ctx.opt.data.weeks.forEach(function (week) {
      var shown = ctx.visible(week.ids);
      if (!shown.length) return;
      append(
        root,
        append(el('div', 'round'), roundLabel(week.label, week.dates, null, totals(ctx.fx, shown)), fixturesOf(ctx, week.ids, true))
      );
    });
    return root;
  }

  function combinedListView(ctx) {
    var head = el('tr');
    ['Date', 'Competition', 'Home', '', 'Away', 'Venue', 'Rules', 'Score'].forEach(function (name) {
      head.appendChild(el('th', name === 'Score' ? 'fx-score' : '', name));
    });
    var body = el('tbody');
    var shown = ctx.visible(ctx.opt.data.list);
    if (!shown.length) return noMatches();
    shown.forEach(function (uid) {
      body.appendChild(fixtureRow(ctx.fx[uid]));
    });
    return append(el('table'), append(el('thead'), head), body);
  }

  function monthCalendarView(ctx) {
    var data = ctx.opt.data;
    var state = ctx.opt.state;
    var days = {};
    data.days.forEach(function (day) { days[day.date] = day; });
    if (!data.months.length) return el('div', 'desc', 'Nothing scheduled yet.');
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
          var shownToday = days[iso] ? ctx.visible(days[iso].ids) : [];
          var sum = shownToday.length ? totals(ctx.fx, shownToday) : null;
          var label = iso + (dots.length ? ': ' + dots.map(function (c) { return DATA.competitions[c].name; }).join(', ') : '') +
            (sum ? '; ' + plural(sum.games, 'game', 'games') + (sum.badged ? '; net ' + netText(sum.net) + ' pts' : '') : '');
          button.setAttribute('aria-label', label);
          button.setAttribute('data-label', label);
          var dotBox = el('span', 'cal-dots');
          dots.forEach(function (comp) {
            var dot = el('span', 'cal-dot ' + DATA.competitions[comp].cls);
            dot.title = DATA.competitions[comp].name;
            dot.setAttribute('aria-hidden', 'true');
            dotBox.appendChild(dot);
          });
          append(button, el('span', 'cal-daynum', String(+iso.slice(8))), dotBox);
          if (sum) button.appendChild(dayFigure(sum));
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
        var head = append(el('div', 'rlabel'), el('b', '', day.label));
        append(details, append(el('div', 'day-detail'), head, summaryNode(totals(ctx.fx, ids), false), box));
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
      var shown = showDay();
      ctx.opt.markLens(details);
      shown.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
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
  // One controller per option panel, each with its own state:
  //   view, club, date (month calendar's selected day), and the rule lens:
  //   rule (a rule id, '' for none) and only (hide instead of dim).
  // `render()` is the single place a view is activated: it draws the active
  // view into its pane and empties the others. The panel carries the lens as
  // data-lens-rule / data-lens-only, and each fixture element is marked
  // .lens-hit or .lens-dim by `markLens`.

  var REDUCED_MOTION = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  function ruleName(rule) { return rule.id.replace(/_/g, ' '); }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  // A rule total as a whole number, one decimal when it is small.
  function points(total) {
    var text = Math.abs(total) < 10 ? (Math.round(total * 10) / 10).toFixed(1) : String(Math.round(total));
    return total > 0 ? '+' + text : text.replace('-', '−');
  }

  function Option(index, panel) {
    this.data = DATA.options[index];
    this.panel = panel;
    this.fx = null;
    this.stats = null;
    this.state = { view: panel.querySelector('.view-btn').dataset.view, club: '', date: '', rule: '', only: false };
    this.clubSelect = panel.querySelector('.club-select');
    this.lensSelect = panel.querySelector('.lens-select');
    this.lensOnly = panel.querySelector('.lens-only-box');
    this.lensCaption = panel.querySelector('.lens-caption');
    this.headlines = panel.querySelector('.headlines');
    this.headlineScope = panel.querySelector('.headline-scope');
    this.strip = panel.querySelector('.season-strip');
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
    this.lensSelect.addEventListener('change', function () { self.setLens(self.lensSelect.value); });
    this.lensOnly.addEventListener('change', function () { self.setLens(self.state.rule, self.lensOnly.checked); });
    panel.querySelector('.lens-clear').addEventListener('click', function () {
      self.setLens('');
      self.focusFilter(self.lensSelect);
    });
    panel.querySelector('.club-clear').addEventListener('click', function () {
      self.state.club = '';
      self.clubSelect.value = '';
      self.render();
      self.focusFilter(self.clubSelect);
    });
  }

  // Focus goes to the filter's own select, or to the Filters button while a
  // narrow bar keeps the selects collapsed, so removing a chip never drops focus.
  Option.prototype.focusFilter = function (select) {
    (select.offsetParent ? select : this.panel.querySelector('.filters-toggle')).focus();
  };

  Option.prototype.context = function () {
    var club = this.state.club;
    var rule = this.state.rule;
    var only = this.state.only;
    var fx = this.fx;
    function matches(f) { return !rule || f.rules.indexOf(rule) !== -1; }
    function show(f) { return (!club || f.clubs.indexOf(club) !== -1) && (!only || matches(f)); }
    return {
      opt: this,
      fx: fx,
      club: club,
      rule: rule,
      filtering: !!club || only,
      matches: matches,
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

  // ------------------------------------------------------------- rule lens
  //
  // The lens picks one rule. Its fixtures are the ones badged with it, so for
  // an inverse rule (preferred_weekday, `inv` in the rule table) that is the
  // off-day fixtures the badges show, not the on-day ones that earned the
  // rule's total; every count and caption below says so.

  // Per rule with fixtures in this option: {rule, fixtures, onDay, total}.
  Option.prototype.ruleStats = function () {
    if (this.stats) return this.stats;
    var stats = {};
    function entry(rule) {
      return stats[rule.id] || (stats[rule.id] = { rule: rule, fixtures: 0, onDay: 0, total: 0 });
    }
    this.fx.forEach(function (f) { f.flags.forEach(function (flag) { entry(flag.rule).fixtures++; }); });
    this.data.matches.forEach(function (m) {
      (m.q || []).forEach(function (quiet) { entry(DATA.rules[quiet[0]]).onDay++; });
    });
    this.data.totals.forEach(function (t) { entry(DATA.rules[t[0]]).total = t[1]; });
    this.stats = stats;
    return stats;
  };

  function statsWording(s) {
    if (s.rule.inv) {
      // preferred_weekday: the badge is the exception, the total is earned by the rest.
      return plural(s.fixtures, 'fixture', 'fixtures') + ' off the preferred day (rule total ' +
        points(s.total) + ' from ' + plural(s.onDay, 'on-day fixture', 'on-day fixtures') + ')';
    }
    return plural(s.fixtures, 'fixture', 'fixtures') + (s.rule.hard ? ' breaking it' : ', ' + points(s.total) + ' pts');
  }

  Option.prototype.buildLensOptions = function () {
    var stats = this.ruleStats();
    var list = Object.keys(stats).map(function (id) { return stats[id]; }).filter(function (s) { return s.fixtures; });
    list.sort(function (a, b) {
      return (b.rule.hard ? 1 : 0) - (a.rule.hard ? 1 : 0) || Math.abs(b.total) - Math.abs(a.total) ||
        (ruleName(a.rule) < ruleName(b.rule) ? -1 : 1);
    });
    var select = this.lensSelect;
    list.forEach(function (s) {
      var text = ruleName(s.rule) + ' (';
      if (s.rule.inv) text += s.fixtures + ' off-day';
      else text += (s.rule.hard ? 'broken' : points(s.total)) + ', ' + s.fixtures;
      var option = el('option', '', text + ')');
      option.value = s.rule.id;
      select.appendChild(option);
    });
  };

  Option.prototype.setLens = function (rule, only) {
    var state = this.state;
    var redraw = state.only;
    state.rule = rule || '';
    state.only = state.rule ? (only === undefined ? state.only : only) : false;
    // Hiding changes the layout, so a view is redrawn; dimming only re-marks
    // it, which keeps a focused badge in place.
    if (redraw || state.only) {
      this.render();
    } else {
      this.markLens(this.activeBody());
      this.renderLensUi();
    }
  };

  Option.prototype.activeBody = function () {
    return this.panel.querySelector('.view-pane:not([hidden]) .pane-body');
  };

  Option.prototype.dayIndex = function () {
    if (!this.days) {
      this.days = {};
      var days = this.days;
      this.data.days.forEach(function (day) { days[day.date] = day; });
    }
    return this.days;
  };

  // Marks what the lens shows among the fixtures drawn under `root`.
  Option.prototype.markLens = function (root) {
    var rule = this.state.rule;
    if (!root) return;
    root.querySelectorAll('[data-uid]').forEach(function (node) {
      var hit = (' ' + node.getAttribute('data-rules') + ' ').indexOf(' ' + rule + ' ') !== -1;
      node.classList.toggle('lens-hit', !!rule && hit);
      node.classList.toggle('lens-dim', !!rule && !hit);
    });
    root.querySelectorAll('.flag[data-rule]').forEach(function (badge) {
      badge.classList.toggle('lens-rule', !!rule && badge.getAttribute('data-rule') === rule);
    });
    var ctx = this.context();
    var days = this.dayIndex();
    var name = rule ? ruleName(DATA.rules.filter(function (r) { return r.id === rule; })[0]) : '';
    root.querySelectorAll('.cal-day').forEach(function (button) {
      var day = days[button.dataset.date];
      var n = rule && day ? ctx.visible(day.ids).filter(function (uid) { return ctx.matches(ctx.fx[uid]); }).length : 0;
      button.classList.toggle('lens-hit', n > 0);
      button.classList.toggle('lens-dim', !!rule && !n);
      var chip = button.querySelector('.cal-hits');
      if (chip) chip.remove();
      if (n) {
        var count = el('span', 'cal-hits', String(n));
        count.setAttribute('aria-hidden', 'true');
        button.appendChild(count);
      }
      button.setAttribute('aria-label', button.dataset.label + (n ? ': ' + plural(n, 'fixture', 'fixtures') + ' with ' + name : ''));
    });
  };

  // Active filters as removable chips, so a collapsed or scrolled-away control
  // never hides the state of the calendar.
  Option.prototype.renderChips = function (stats) {
    var state = this.state;
    var clubChip = this.panel.querySelector('.club-clear');
    var lensChip = this.panel.querySelector('.lens-clear');
    var clubName = state.club ? this.clubSelect.options[this.clubSelect.selectedIndex].text : '';
    var lensName = stats ? ruleName(stats.rule) + (state.only ? ' (only)' : '') : '';
    clubChip.hidden = !state.club;
    lensChip.hidden = !stats;
    clubChip.querySelector('.chip-text').textContent = clubName;
    lensChip.querySelector('.chip-text').textContent = lensName;
    clubChip.setAttribute('aria-label', 'Remove club filter: ' + clubName);
    lensChip.setAttribute('aria-label', 'Clear highlighted rule: ' + lensName);
    var count = (state.club ? 1 : 0) + (stats ? 1 : 0);
    this.panel.querySelector('.filters-toggle').textContent = count ? 'Filters ' + count : 'Filters';
  };

  Option.prototype.renderLensUi = function () {
    var state = this.state;
    var stats = state.rule && this.ruleStats()[state.rule];
    this.renderChips(stats);
    this.lensSelect.value = state.rule;
    this.lensOnly.checked = state.only;
    this.lensOnly.disabled = !state.rule;
    this.panel.setAttribute('data-lens-rule', state.rule);
    this.panel.setAttribute('data-lens-only', String(state.only));
    this.lensCaption.hidden = !stats;
    if (!stats) return;
    var text = this.panel.querySelector('.lens-text');
    text.textContent = '';
    var rule = stats.rule;
    append(text, el('b', '', ruleName(rule) + ': '), document.createTextNode(statsWording(stats) + '. ' + rule.desc));
    if (state.club) {
      var club = state.club;
      var inClub = this.fx.filter(function (f) { return f.clubs.indexOf(club) !== -1 && f.rules.indexOf(rule.id) !== -1; }).length;
      var name = this.clubSelect.options[this.clubSelect.selectedIndex].text;
      append(text, document.createTextNode(' ' + name + ': ' + plural(inClub, 'such fixture', 'such fixtures') + '.'));
    }
    append(text, document.createTextNode(' Badge: “' + rule.label + '”.'));
  };

  // Brings fixture `uid` into view in the current view and flashes it. A filter
  // that would hide it is lifted first; the month calendar opens its day.
  Option.prototype.reveal = function (uid) {
    var fx = this.fx[uid];
    var state = this.state;
    var redraw = false;
    if (state.club && fx.clubs.indexOf(state.club) === -1) {
      state.club = '';
      this.clubSelect.value = '';
      redraw = true;
    }
    if (state.only && fx.rules.indexOf(state.rule) === -1) {
      state.only = false;
      redraw = true;
    }
    if (state.view === 'month-calendar' && state.date !== fx.date) {
      state.date = fx.date;
      redraw = true;
    }
    if (redraw) this.render();
    var node = this.activeBody().querySelector('[data-uid="' + uid + '"]');
    if (!node) return;
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
    node.tabIndex = -1;
    node.focus({ preventScroll: true });
    node.scrollIntoView({ block: 'center', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
    setTimeout(function () { node.classList.remove('flash'); }, 2400);
  };

  // Scrolls so the drawn view starts just under the sticky bar (the root's
  // scroll-padding keeps it clear).
  Option.prototype.showCalendar = function () {
    this.panel.querySelector('.view-pane:not([hidden])').scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  };

  Option.prototype.showScore = function () {
    // The option tabs sit just above the panel; 8px of air keeps them from touching the edge.
    var top = document.querySelector('.tabs').getBoundingClientRect().top + window.pageYOffset - 8;
    window.scrollTo({ top: Math.max(0, top), behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
  };

  // Season strip: one button per league round, above the views that list rounds.
  // Its figures come from the same visible fixtures as the round labels.
  Option.prototype.renderStrip = function (ctx) {
    var strip = this.strip;
    var shown = this.state.view === 'by-competition' || this.state.view === 'combined-calendar';
    strip.hidden = !shown;
    strip.textContent = '';
    if (!shown) return;
    strip.appendChild(el('div', 'strip-caption', 'Net points per round — click to jump'));
    var self = this;
    this.data.byCompetition.forEach(function (view) {
      var cells = el('div', 'strip-cells');
      view.rounds.forEach(function (round) { cells.appendChild(stripCell(self, ctx, round)); });
      append(strip, append(el('div', 'strip-row'), el('span', 'strip-name', view.name), cells));
    });
  };

  function stripCell(opt, ctx, round) {
    var button = el('button', 'strip-cell');
    button.type = 'button';
    button.appendChild(el('span', 'strip-round', round.title.replace(/^Round\s*/, '')));
    var shown = ctx.visible(round.ids);
    if (!shown.length) {
      button.disabled = true;
      button.setAttribute('aria-label', round.title + ': no fixtures shown');
      return button;
    }
    var sum = totals(ctx.fx, shown);
    if (sum.badged) {
      var edge = netClass(sum.net);
      if (edge) button.classList.add('sc-' + edge);
      button.appendChild(el('span', 'strip-net', netText(sum.net)));
    }
    if (sum.broken) {
      var hard = el('span', 'strip-hard', String(sum.broken));
      hard.setAttribute('aria-hidden', 'true');
      button.appendChild(hard);
    }
    var label = round.title + ': ' + summaryNode(sum, true).textContent;
    button.setAttribute('aria-label', label);
    button.title = label;
    button.addEventListener('click', function () { opt.jumpTo(round.ids); });
    return button;
  }

  // Scrolls to the first drawn fixture of `ids` and its round or week block.
  Option.prototype.jumpTo = function (ids) {
    var wanted = {};
    ids.forEach(function (uid) { wanted[uid] = true; });
    var nodes = this.activeBody().querySelectorAll('[data-uid]');
    for (var i = 0; i < nodes.length; i++) {
      if (wanted[nodes[i].getAttribute('data-uid')]) {
        nodes[i].closest('.round').scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
        return;
      }
    }
  };

  Option.prototype.render = function () {
    if (!this.fx) {
      this.fx = hydrate(this.data);
      this.buildLensOptions();
    }
    var state = this.state;
    var ctx = this.context();
    hideFlagTip();
    this.panel.setAttribute('data-current-view', state.view);
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
      if (active) {
        var drawn = VIEWS[state.view](ctx);
        body.appendChild(drawn.childNodes.length ? drawn : noMatches());
      }
    });
    this.renderHeadlines();
    this.renderLensUi();
    this.markLens(this.activeBody());
    this.renderStrip(ctx);
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

  // The visible panel's bar height, published as --bar-h for the scroll offsets in CSS.
  var bar = null;
  var barWatch = window.ResizeObserver ? new ResizeObserver(syncBar) : null;
  function syncBar() {
    if (bar) document.documentElement.style.setProperty('--bar-h', bar.offsetHeight + 'px');
  }
  function watchBar(panel) {
    if (barWatch && bar) barWatch.unobserve(bar);
    bar = panel.querySelector('.controls');
    if (barWatch && bar) barWatch.observe(bar);
    syncBar();
  }
  window.addEventListener('resize', syncBar);

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
      watchBar(panels[current]);
    });
  });
  if (panels.length) {
    option(0).render();
    watchBar(panels[0]);
  }

  // ---------------------------------------------------------- rule tooltips
  //
  // An icon's tooltip shows on hover/focus via CSS alone; a fixture badge's
  // shared tooltip (above) is shown by the handlers below. This adds
  // tap-to-toggle for touch devices (no hover state), positions the
  // (position:fixed) tooltip against its anchor so an ancestor's overflow can't
  // clip it, and keeps it inside the viewport and attached while the page
  // scrolls.
  function positionTooltip(wrap) {
    var icon = wrap.querySelector('.info-icon');
    var tooltip = wrap.querySelector('.tooltip');
    if (icon && tooltip) placeTooltip(icon, tooltip);
  }
  function placeTooltip(icon, tooltip) {
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
    hideFlagTip();
  }
  function repositionVisibleTooltips() {
    document.querySelectorAll('.info-wrap:not(.flag):hover, .info-wrap:not(.flag):focus-within, .info-wrap:not(.flag).open').forEach(positionTooltip);
    if (flagTipOwner) placeTooltip(flagTipOwner, flagTip);
  }
  // Delegated, since panels other than the first are added to the page later.
  function tooltipOwner(event) {
    return event.target.closest ? event.target.closest('.info-wrap') : null;
  }
  ['mouseover', 'focusin'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var wrap = tooltipOwner(e);
      if (!wrap) return;
      if (flagOf.has(wrap)) showFlagTip(wrap);
      else positionTooltip(wrap);
    });
  });
  ['mouseout', 'focusout'].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var wrap = tooltipOwner(e);
      if (wrap && flagOf.has(wrap) && !wrap.classList.contains('open') && !wrap.contains(e.relatedTarget)) hideFlagTip();
    });
  });
  document.addEventListener('click', function (e) {
    var icon = e.target.closest ? e.target.closest('.info-icon, .flag') : null;
    if (!icon) {
      closeTooltips();
      return;
    }
    // Some icons sit inside a <summary>; without this the click would also
    // toggle the enclosing <details>.
    e.preventDefault();
    var wrap = icon.closest('.info-wrap');
    if (!wrap) return;
    // A fixture's badge picks its rule for the lens. Its tooltip is already up
    // on hover or focus; a touch has neither, so the first tap shows the
    // tooltip and the second picks the rule. Where click is a plain MouseEvent
    // (no pointerType), a click with a press (detail > 0) on a no-hover device
    // is a touch; Enter has detail 0.
    var isBadge = flagOf.has(wrap);
    var touch = e.pointerType ? e.pointerType !== 'mouse' : e.detail > 0 && HOVER_NONE;
    if (isBadge && (!touch || wrap.classList.contains('open'))) {
      closeTooltips();
      optionOf(wrap).setLens(wrap.getAttribute('data-rule'));
      if (!touch && wrap.isConnected) showFlagTip(wrap);
      return;
    }
    var wasOpen = wrap.classList.contains('open');
    closeTooltips();
    if (!wasOpen) {
      wrap.classList.add('open');
      if (isBadge) showFlagTip(wrap);
      else positionTooltip(wrap);
    }
  });
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') closeTooltips();
  });
  // Capture phase so this also fires for scrolling inside .cal-wrap, whose
  // 'scroll' event doesn't bubble.
  window.addEventListener('scroll', repositionVisibleTooltips, true);
  window.addEventListener('resize', repositionVisibleTooltips);

  // ------------------------------------- scorecard links and fixture lists
  //
  // The upsides, problems and breakdown rows name rules; a rule with fixtures
  // gets a "Show in calendar" button and, once its <details> is opened, the
  // list of those fixtures, built here from the fixture flags rather than
  // shipped a second time.

  function optionOf(node) {
    return option(Array.prototype.indexOf.call(panels, node.closest('.panel')));
  }

  function occurrenceRow(fx, flag, item) {
    var button = el('button', 'occ-row');
    button.type = 'button';
    button.setAttribute('data-fx', fx.uid);
    button.setAttribute('data-rule', flag.rule.id);
    var gain = flag.rule.hard ? 'broken' : item.delta > 0 ? 'good' : item.delta < 0 ? 'cost' : 'none';
    var detail = item.detail + (item.n > 1 ? ' (1 of ' + item.n + ' fixtures sharing this)' : '');
    append(
      button,
      el('span', 'occ-date', shortDate(fx.date)),
      el('span', 'occ-fx', fx.home.name + (fx.away ? ' v ' + fx.away.name : ' ' + versusText(fx))),
      el('span', 'occ-delta ' + gain, flag.rule.hard ? 'broken' : item.delta === 0 ? '' : signed(item.delta)),
      el('span', 'occ-detail', detail)
    );
    return append(el('li'), button);
  }

  // Lists a rule's fixtures grouped by team (or dual club), biggest group first,
  // each group headed by its count.
  Option.prototype.fillRuleList = function (mount, ruleId) {
    var stats = this.ruleStats()[ruleId];
    var groups = {};
    this.fx.forEach(function (fx) {
      fx.flags.forEach(function (flag) {
        if (flag.rule.id !== ruleId) return;
        flag.items.forEach(function (item) {
          (groups[item.group] || (groups[item.group] = [])).push({ fx: fx, flag: flag, item: item });
        });
      });
    });
    var names = Object.keys(groups).sort(function (a, b) {
      return groups[b].length - groups[a].length || (a < b ? -1 : 1);
    });
    var total = names.reduce(function (sum, name) { return sum + groups[name].length; }, 0);
    var box = el('div', 'occ-groups');
    var summary = el('p', 'occ-summary', statsWording(stats) + '. Select a row to find it in the calendar.');
    if (total > 14) {
      box.tabIndex = 0;
      box.setAttribute('role', 'region');
      box.setAttribute('aria-label', ruleName(stats.rule) + ' fixtures');
    }
    names.forEach(function (name) {
      var rows = groups[name].sort(function (a, b) { return a.fx.date < b.fx.date ? -1 : a.fx.date > b.fx.date ? 1 : a.fx.uid - b.fx.uid; });
      var list = el('ul');
      rows.forEach(function (r) { list.appendChild(occurrenceRow(r.fx, r.flag, r.item)); });
      if (!name) {
        box.appendChild(list);
        return;
      }
      var group = el('details', 'occ-group');
      group.open = total <= 30;
      append(group, append(el('summary'), document.createTextNode(name + ' '), el('span', 'occ-count', '\u00d7' + rows.length)), list);
      box.appendChild(group);
    });
    append(mount, summary, box);
  };

  // `toggle` does not bubble, hence the capture phase.
  document.addEventListener('toggle', function (e) {
    var details = e.target;
    if (!details.open || !details.dataset || !details.dataset.rule) return;
    var mount = details.querySelector('.rule-fixtures');
    if (mount && !mount.firstChild) optionOf(details).fillRuleList(mount, details.dataset.rule);
  }, true);

  document.addEventListener('click', function (e) {
    var link = e.target.closest ? e.target.closest('.lens-link, .occ-row, .jump-cal, .back-to-score') : null;
    if (!link) return;
    var opt = optionOf(link);
    if (link.classList.contains('jump-cal')) {
      opt.showCalendar();
    } else if (link.classList.contains('back-to-score')) {
      opt.showScore();
    } else if (link.classList.contains('occ-row')) {
      // The list's own rule becomes the lens, so the target is not left dimmed.
      opt.setLens(link.getAttribute('data-rule'));
      opt.reveal(+link.getAttribute('data-fx'));
    } else {
      opt.setLens(link.getAttribute('data-rule'));
      opt.showCalendar();
    }
  });

  // ------------------------------------------------- bar disclosures
  //
  // The legend popover and, on a phone, the collapsed filter fields. Each
  // button owns one region through aria-expanded/aria-controls.
  function setLegend(wrap, open) {
    var button = wrap.querySelector('.legend-toggle');
    wrap.querySelector('.legend-panel').hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  }
  document.addEventListener('click', function (e) {
    var target = e.target;
    var toggle = target.closest ? target.closest('.legend-toggle, .filters-toggle') : null;
    document.querySelectorAll('.legend-wrap').forEach(function (wrap) {
      if (!toggle || !wrap.contains(toggle)) {
        if (!wrap.contains(target)) setLegend(wrap, false);
      }
    });
    if (!toggle) return;
    if (toggle.classList.contains('legend-toggle')) {
      setLegend(toggle.parentNode, toggle.getAttribute('aria-expanded') !== 'true');
    } else {
      setFilters(toggle.closest('.controls'), toggle.getAttribute('aria-expanded') !== 'true');
    }
  });
  // A phone's filter fields close once used, on the next real scroll, or on
  // Escape, so the bar does not stay tall over the calendar.
  var FILTER_SCROLL_SLACK = 24;
  var filtersOpenAt = 0;
  function setFilters(bar, open, refocus) {
    var toggle = bar.querySelector('.filters-toggle');
    var hadFocus = bar.querySelector('.bar-fields').contains(document.activeElement);
    bar.classList.toggle('filters-open', open);
    toggle.setAttribute('aria-expanded', String(open));
    if (open) filtersOpenAt = window.pageYOffset;
    else if (hadFocus || refocus) toggle.focus();
  }
  document.addEventListener('change', function (e) {
    var bar = e.target.closest ? e.target.closest('.controls.filters-open') : null;
    if (!bar || !bar.querySelector('.bar-fields').contains(e.target)) return;
    // Picking a rule is what enables "Only show matching", so the fields stay open for it.
    if (!e.target.classList.contains('lens-select')) setFilters(bar, false);
  });
  window.addEventListener('scroll', function () {
    var bar = document.querySelector('.controls.filters-open');
    // Focusing a select can nudge the page a little; only a real scroll closes.
    if (bar && Math.abs(window.pageYOffset - filtersOpenAt) > FILTER_SCROLL_SLACK) setFilters(bar, false);
  }, { passive: true });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    var bar = document.querySelector('.controls.filters-open');
    if (bar) setFilters(bar, false, true);
    document.querySelectorAll('.legend-wrap').forEach(function (wrap) {
      var button = wrap.querySelector('.legend-toggle');
      if (button.getAttribute('aria-expanded') !== 'true') return;
      setLegend(wrap, false);
      button.focus();
    });
  });
})();
