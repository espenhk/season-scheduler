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

  // ------------------------------------------------------- roving focus (#41)
  //
  // A round holds a dozen cards and badges, a season hundreds. Each list of them is one
  // `[data-rove-group]` and one tab stop: Tab enters at the last item used, the arrow keys
  // walk the rest, and Tab leaves for whatever follows.

  function roveGroup(node) {
    node.setAttribute('data-rove-group', '');
    return node;
  }

  // The group's own items that are on screen (a closed round or chip hides its items).
  function roveItems(group) {
    return Array.prototype.filter.call(group.querySelectorAll('[data-rove]'), function (item) {
      return item.closest('[data-rove-group]') === group && !item.disabled && item.getClientRects().length > 0;
    });
  }

  // Exactly one on-screen item per group keeps tabindex 0: the one that already had it, else the first.
  function refreshRove(scope) {
    scope.querySelectorAll('[data-rove-group]').forEach(function (group) {
      var items = roveItems(group);
      var stop = items.filter(function (item) { return item.tabIndex === 0; })[0] || items[0];
      items.forEach(function (item) { item.tabIndex = item === stop ? 0 : -1; });
    });
  }

  // For changes that hide or show items after the draw (a folded round, an opened chip).
  var roveFrame = 0;
  function scheduleRove() {
    if (roveFrame) return;
    roveFrame = requestAnimationFrame(function () {
      roveFrame = 0;
      refreshRove(document);
    });
  }

  document.addEventListener('focusin', function (e) {
    var item = e.target;
    var group = item.matches && item.matches('[data-rove]') ? item.closest('[data-rove-group]') : null;
    if (group) roveItems(group).forEach(function (other) { other.tabIndex = other === item ? 0 : -1; });
  });
  document.addEventListener('keydown', function (e) {
    var item = e.target;
    if (!item.matches || !item.matches('[data-rove]') || e.altKey || e.ctrlKey || e.metaKey) return;
    var group = item.closest('[data-rove-group]');
    if (!group) return;
    var items = roveItems(group);
    var at = items.indexOf(item);
    var to = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: items.length - 1 }[e.key];
    if (to === undefined) return;
    e.preventDefault();
    if (items[to]) items[to].focus();
  });
  // Toggled sections change which items show.
  document.addEventListener('toggle', scheduleRove, true);

  var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function dayIndex(iso) {
    return new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10))).getUTCDay();
  }

  function weekday(iso) {
    return WEEKDAYS[dayIndex(iso)];
  }

  // One date format throughout: "Sat 4 Apr", with the year only where a date stands alone (#41).
  function shortDate(iso) {
    return weekday(iso) + ' ' + (+iso.slice(8, 10)) + ' ' + MONTHS[+iso.slice(5, 7) - 1];
  }
  function longDate(iso) {
    return shortDate(iso) + ' ' + iso.slice(0, 4);
  }

  // The same date in full with no zero before the day, as the month view names it.
  var WEEKDAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  function longDateUnpadded(iso) {
    return WEEKDAY_NAMES[dayIndex(iso)] + ' ' + (+iso.slice(8, 10)) + ' ' + MONTH_NAMES[+iso.slice(5, 7) - 1] + ' ' + iso.slice(0, 4);
  }

  // ---------------------------------------------------------- fixture model
  //
  // Every wire entry (league match, cup round, European leg) is turned into one
  // shape, so each representation below handles all three without branching on
  // kind:
  //   uid, kind, date, time, comp, clubs[], teams[], venue,
  //   home / away: {team, name, full, cls, shape}  (away is null for cup/European)
  //   versus: text after "v" when there is no away side, round: round name
  //   flags[]: scoring badges, one per rule {rule, delta, items[{delta, detail, n, group}]};
  //   rules: their rule ids; delta: their net; tint: 'good' | 'cost' | ''

  var SHAPE_BY_VENUE_TYPE = { home: 'square', away: 'diamond', neutral: 'circle' };

  function side(teamId, shape) {
    var team = DATA.teams[teamId];
    return { team: teamId, name: team.short, full: team.label, club: team.club, cls: DATA.clubs[team.club], shape: shape };
  }

  // The club's teams, in the order the wire lists them.
  function teamsOf(club) {
    return Object.keys(DATA.teams).filter(function (teamId) { return DATA.teams[teamId].club === club; });
  }

  // Dual clubs (a men's and a women's senior team) are the ones the timeline is for.
  function isDual(club) {
    return DATA.dualClubs.indexOf(club) !== -1;
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
      uid: uid, key: m.c + '-' + m.d + '-' + m.h + '-' + m.a, venueId: m.v || '',
      kind: 'league', date: m.d, time: m.t || '', comp: m.c, roundNo: m.r,
      clubs: [home.club, away.club], teams: [m.h, m.a], home: home, away: away, versus: '', round: '',
      venue: DATA.venues[m.v || DATA.teams[m.h].venue]
    }, m.f, notes, groups);
  }

  function sharedFixture(uid, e) {
    var european = e.k === 'eu';
    // A tie whose venue is not decided yet gets a dimmed square.
    var shape = SHAPE_BY_VENUE_TYPE[e.vt] || 'square-dim';
    var home = side(e.t, shape);
    return withFlags({
      uid: uid, key: e.c + '-' + e.d + '-' + e.t, venueId: '',
      kind: european ? 'european' : 'cup', date: e.d, time: '', comp: e.c,
      clubs: [home.club], teams: [e.t], home: home, away: null, versus: european ? e.o : 'TBD', round: e.n,
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
    var all = sharedFixtures.concat(optionData.matches.map(function (m, i) {
      return leagueFixture(offset + i, m, optionData.notes, optionData.groups);
    }));
    // A key doubles as the fixture's DOM id, so a repeat (a team twice on a date in a competition) is numbered.
    var seen = {};
    all.forEach(function (fx) {
      var n = seen[fx.key] = (seen[fx.key] || 0) + 1;
      if (n > 1) fx.key += '-' + n;
    });
    return all;
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

  // -- text for assistive technology (#41)
  //
  // Swatches, abbreviations and edge colours say nothing to a screen reader, so every fixture
  // also carries one visually hidden sentence naming date, teams, home or away, ground and net points.

  var VENUE_WORDS = { square: 'at home', diamond: 'away', circle: 'on neutral ground', 'square-dim': 'venue not decided' };

  // "SK Brann at home to Viking FK"; a cup or European entry has its opponent as text.
  function matchupText(fx) {
    if (fx.away) return fx.home.full + ' at home to ' + fx.away.full;
    var where = VENUE_WORDS[fx.home.shape];
    return fx.home.full + (where ? ' ' + where : '') + ' against ' + fx.versus;
  }

  function fixtureSentence(fx) {
    var comp = DATA.competitions[fx.comp].name;
    var when = WEEKDAY_NAMES[dayIndex(fx.date)] + ' ' + (+fx.date.slice(8, 10)) + ' ' + MONTH_NAMES[+fx.date.slice(5, 7) - 1] +
      (fx.time ? ' ' + fx.time : '');
    var parts = [when, matchupText(fx)];
    if (fx.venue) parts.push(fx.venue);
    var text = parts.join(', ') + '. ' + comp + (fx.kind === 'league' ? ' round ' + fx.roundNo : ', ' + fx.round) + '.';
    if (fx.broken) text += ' Breaks a hard rule.';
    else if (fx.flags.length) text += ' Net ' + signed(fx.delta) + ' points.';
    return text;
  }

  // A fixture's one control: the visual line is hidden from speech and the sentence stands in for it.
  // It is a stop in its container's roving tab order, see below.
  function fixtureControl(className, visual, sentence) {
    visual.setAttribute('aria-hidden', 'true');
    var button = el('button', className);
    button.type = 'button';
    button.tabIndex = -1;
    button.setAttribute('data-rove', '');
    button.setAttribute('aria-expanded', 'false');
    return append(button, visual, el('span', 'sr-only', sentence));
  }

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

  // With a club chosen a fixture is read from that club's side: "H vs KFU", "A at RBK" (#39).
  // `opp` is the opposing side (null for a cup or European entry, whose opponent is text),
  // `where` is H / A / N, or '' while a tie's venue is undecided.
  var WHERE_BY_SHAPE = { square: 'H', diamond: 'A', circle: 'N' };
  var WHERE_NAME = { H: 'Home', A: 'Away', N: 'Neutral ground' };

  function perspective(fx, club) {
    if (!club) return null;
    var own = fx.home.club === club ? fx.home : fx.away && fx.away.club === club ? fx.away : null;
    if (!own) return null;
    var opp = own === fx.home ? fx.away : fx.home;
    var where = fx.away ? (own === fx.home ? 'H' : 'A') : WHERE_BY_SHAPE[own.shape] || '';
    return { where: where, word: where === 'A' ? 'at' : 'vs', opp: fx.away ? opp : null, oppText: fx.versus };
  }

  function wherePill(where) {
    var pill = el('abbr', 'ha-pill' + (where === 'H' ? ' home' : ''), where);
    pill.title = WHERE_NAME[where];
    return pill;
  }

  // The opponent in full: a chosen club leaves room for names rather than abbreviations.
  function opponentNodes(view) {
    return view.opp
      ? [marker(view.opp), named({ name: view.opp.full, full: view.opp.full }, '')]
      : [el('span', '', view.oppText)];
  }

  function tag(node, fx) {
    node.id = 'fx-' + fx.key;
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
  // The width below which the page is laid out for a phone (matches the stylesheet's phone rules).
  var PHONE = window.matchMedia('(max-width: 600px)');
  var flagTip = null;
  var flagTipOwner = null;

  function signed(delta) {
    var text = (Math.round(delta * 10) / 10).toFixed(1);
    return delta > 0 ? '+' + text : text.replace('-', '\u2212');
  }

  function flagPoints(flag) {
    return flag.rule.hard ? 'broken rule' : flag.delta === 0 ? 'reward forgone' : signed(flag.delta) + ' pts';
  }

  function flagBadge(flag) {
    var polarity = flag.rule.hard ? 'broken' : flag.delta > 0 ? 'good' : 'cost';
    var badge = el('button', 'info-wrap flag flag-' + polarity, flag.rule.label);
    badge.type = 'button';
    badge.setAttribute('data-rule', flag.rule.id);
    badge.tabIndex = -1;
    badge.setAttribute('data-rove', '');
    // Starts with the visible label; the rest says what the colour and sign only show. Also
    // keeps the tooltip child out of the accessible name.
    badge.setAttribute('aria-label', flag.rule.label + ', ' + flag.rule.name + ', ' + flagPoints(flag));
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
    append(flagTip, el('b', 'tt-head', flag.rule.name + ' · ' + flagPoints(flag)));
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

  // The pill used in "By competition" and "Combined · calendar"; the day heading above it gives the date.
  // A click opens its detail line (full names and venue), see toggleDetail.
  function fixtureCard(fx, showCompetition, club) {
    var card = tag(el('span', 'fx has-detail' + edgeClass(fx)), fx);
    card.setAttribute('data-competition', fx.comp);
    var view = perspective(fx, club);
    var visual = append(el('span', 'fx-visual'), fx.time ? el('span', 'd', fx.time) : null, showCompetition ? competitionTag(fx) : null);
    if (view) {
      if (view.where) visual.appendChild(wherePill(view.where));
      append(visual, el('span', 'vs', view.word));
      opponentNodes(view).forEach(function (node) { visual.appendChild(node); });
    } else {
      append(visual, marker(fx.home), named(fx.home, 'h'));
      if (fx.away) {
        append(visual, el('span', 'vs', 'v'), marker(fx.away), named(fx.away, ''));
      } else {
        append(visual, el('span', 'vs', versusText(fx)));
      }
    }
    return append(card, fixtureControl('fx-main', visual, fixtureSentence(fx)), flagBox(fx));
  }

  // The detail line a card opens: both teams in full, and where they play.
  function detailLine(fx) {
    var teams = fx.away ? fx.home.full + ' v ' + fx.away.full : fx.home.full + ' ' + versusText(fx);
    // The control's sentence already says all of this, so speech skips the visible copy.
    var line = append(el('span', 'fx-detail'), el('b', '', teams), document.createTextNode(fx.venue ? ' · ' + fx.venue : ''));
    line.setAttribute('aria-hidden', 'true');
    return line;
  }

  function toggleDetail(card) {
    var open = card.querySelector('.fx-detail');
    var control = card.querySelector('[aria-expanded]');
    if (control) control.setAttribute('aria-expanded', String(!open));
    var opt = optionOf(card);
    // The card whose detail is open is what a copied link points at.
    opt.setTarget(open ? '' : card.id);
    if (open) {
      open.remove();
      return;
    }
    var fx = opt.fx[+card.getAttribute('data-uid')];
    var line = detailLine(fx);
    card.insertBefore(line, card.querySelector(':scope > .flags'));
  }

  // Round text: a league's round number, or a cup or European round's name.
  function roundText(fx) {
    return fx.kind === 'league' ? 'R' + fx.roundNo : fx.round;
  }

  // What the list's text filter matches against, lower-cased.
  function searchText(fx) {
    var comp = DATA.competitions[fx.comp];
    var parts = [fx.home.full, fx.away ? fx.away.full : fx.versus, comp.name, comp.short, fx.venue, roundText(fx), longDate(fx.date)];
    fx.flags.forEach(function (flag) { parts.push(flag.rule.name); });
    return parts.join(' ').toLowerCase();
  }

  // A row of "Combined · list": time, round, competition, team / opponent, venue,
  // rules, score. Every row has this shape, so a single-team cup row reads like a
  // league row with its opponent still to be drawn.
  function fixtureRow(fx, club) {
    var row = tag(el('tr', 'fx-row' + edgeClass(fx)), fx);
    row.setAttribute('data-competition', fx.comp);
    row.setAttribute('data-search', searchText(fx));
    // The text-node space after a marker is part of the cell's inline layout.
    var space = function () { return document.createTextNode(' '); };
    var team = el('td', 'fx-team');
    var visual = el('span');
    visual.setAttribute('aria-hidden', 'true');
    var view = perspective(fx, club);
    if (view) {
      if (view.where) append(visual, wherePill(view.where), space());
      append(visual, el('span', 'vs', view.word), space());
      opponentNodes(view).forEach(function (node) { visual.appendChild(node); });
    } else {
      append(visual, marker(fx.home), space(), named(fx.home, ''), space());
      if (fx.away) {
        append(visual, el('span', 'vs', 'v'), space(), marker(fx.away), space(), named(fx.away, ''));
      } else {
        append(visual, el('span', 'vs', versusText(fx)));
      }
    }
    append(team, visual, el('span', 'sr-only', matchupText(fx)));
    return append(
      row,
      el('td', 'fx-time', fx.time || '–'),
      el('td', 'fx-round', roundText(fx)),
      append(el('td'), competitionTag(fx)),
      team,
      el('td', 'fx-venue', fx.venue),
      append(el('td'), flagBox(fx)),
      scoreCell(fx)
    );
  }

  // A line of the month calendar's day detail.
  function dayRow(fx, inGroup, club) {
    var row = tag(el('div', 'day-row' + edgeClass(fx)), fx);
    var main = el('span', 'day-main');
    main.setAttribute('aria-hidden', 'true');
    var view = perspective(fx, club);
    append(main, inGroup ? null : competitionTag(fx));
    if (view) {
      if (view.where) main.appendChild(wherePill(view.where));
      append(main, el('span', 'vs', view.word));
      opponentNodes(view).forEach(function (node) { main.appendChild(node); });
    } else {
      append(main, marker(fx.home), el('span', '', fx.home.name));
      if (fx.away) {
        append(main, el('span', 'vs', 'v'), marker(fx.away), el('span', '', fx.away.name));
      } else {
        append(main, el('span', 'vs', versusText(fx)));
      }
    }
    if (fx.time) main.appendChild(el('span', 'd', fx.time));
    return append(row, main, el('span', 'sr-only', fixtureSentence(fx)), flagBox(fx));
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

  // The parts of a round or week total, as text plus an optional class for the
  // net, each omitted when it would read zero (the net only when a visible badge
  // is on the games). Shared by the visible label and the accessible name.
  function summaryParts(sum) {
    var parts = [];
    if (sum.badged) parts.push({ text: netText(sum.net) + ' pts', net: true, cls: netClass(sum.net) });
    parts.push({ text: plural(sum.games, 'game', 'games') });
    if (sum.costs) parts.push({ text: sum.costs + ' with costs' });
    if (sum.top && sum.top.n >= 2) parts.push({ text: sum.top.n + ' × ' + sum.top.rule.name });
    if (sum.broken) parts.push({ text: plural(sum.broken, 'hard break', 'hard breaks') });
    return parts;
  }

  // The same parts as one line of text, midweek included: a strip cell's name.
  function summaryText(sum) {
    var texts = summaryParts(sum).map(function (part) { return part.text; });
    if (sum.midweek) texts.push('midweek');
    return texts.join(' · ');
  }

  // Visible label; the parts come from summaryParts.
  function summaryNode(sum, midweekTag) {
    var node = el('div', 'rsum');
    node.title = 'Net points from the rule badges on the games shown (hidden rewards are not counted)';
    summaryParts(sum).forEach(function (part, i) {
      if (i) node.appendChild(document.createTextNode(' · '));
      node.appendChild(part.net ? el('span', 'rnet' + (part.cls ? ' ' + part.cls : ''), part.text) : document.createTextNode(part.text));
    });
    if (midweekTag && sum.midweek) {
      var tagNode = el('span', 'rtag', 'midweek');
      tagNode.title = 'Midweek: every game falls Monday to Thursday';
      node.appendChild(tagNode);
    }
    return node;
  }

  // A day's game count, with its net on a second line when a visible badge sits on
  // one of its games. Each line is its own element so the two never run together.
  function dayFigure(sum) {
    var figure = el('span', 'cal-net');
    figure.setAttribute('aria-hidden', 'true');
    figure.appendChild(el('span', 'cal-count', String(sum.games)));
    if (sum.badged) figure.appendChild(el('span', 'cal-value ' + netClass(sum.net), netText(sum.net)));
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
    return el('div', 'desc', 'No fixtures match the filters selected.');
  }

  // Competitions by fixture kind: league matches, cup rounds, European ties. Cups and
  // Europe start hidden, so the calendar opens on the two leagues.
  var ALL_KINDS = ['league', 'cup', 'european'];
  var DEFAULT_KINDS = ['league'];
  var KIND_LABEL = { league: 'Leagues', cup: 'Cups', european: 'Europe' };

  function kindsText(kinds) {
    return kinds.length ? kinds.map(function (kind) { return KIND_LABEL[kind]; }).join(' + ') : 'none';
  }

  function sameKinds(a, b) {
    return a.join() === b.join();
  }

  // A cup or European round date: one team entry per team, all sharing this key.
  function groupKey(fx) {
    return fx.comp + '|' + fx.round + '|' + fx.date;
  }

  // The visible fixtures of a view as the items it draws, in reading order: by date,
  // then kickoff (untimed last), then the wire's order. A round date with more than
  // one team showing is one group, drawn as a chip; a lone team is an ordinary card.
  function drawItems(ctx, ids) {
    var items = [];
    var groups = {};
    ctx.visible(ids).forEach(function (uid) {
      var fx = ctx.fx[uid];
      if (fx.kind === 'league') {
        items.push({ fxs: [fx] });
        return;
      }
      var key = groupKey(fx);
      if (!groups[key]) {
        groups[key] = { fxs: [] };
        items.push(groups[key]);
      }
      groups[key].fxs.push(fx);
    });
    items.forEach(function (item) {
      item.date = item.fxs[0].date;
      item.time = item.fxs[0].time;
      item.group = item.fxs.length > 1;
    });
    items.sort(function (a, b) {
      if (a.date !== b.date) return a.date < b.date ? -1 : 1;
      var ta = a.time || '99:99';
      var tb = b.time || '99:99';
      if (ta !== tb) return ta < tb ? -1 : 1;
      return a.fxs[0].uid - b.fxs[0].uid;
    });
    return items;
  }

  function offDayNote(off) {
    return 'Not the preferred weekday of ' + off.map(function (id) {
      return DATA.competitions[id].name + ' (' + WEEKDAY_NAMES[DATA.preferred[id]] + ')';
    }).join(' and ');
  }

  // The competitions playing on `iso` whose preferred weekday it is not.
  function offDayCompetitions(iso, items) {
    var index = dayIndex(iso);
    var off = [];
    items.forEach(function (item) {
      item.fxs.forEach(function (fx) {
        var preferred = DATA.preferred[fx.comp];
        if (fx.kind === 'league' && preferred !== undefined && preferred !== index && off.indexOf(fx.comp) === -1) off.push(fx.comp);
      });
    });
    return off;
  }

  // The weekday, boxed when a league playing that day prefers another (#39).
  function weekdayNode(iso, items) {
    var off = offDayCompetitions(iso, items);
    var node = el('span', off.length ? 'wd off' : 'wd', weekday(iso));
    if (off.length) node.title = offDayNote(off);
    return node;
  }

  // The box says it to the eye; this says it to everyone else, after the heading's own words (#41).
  function offDaySpeech(iso, items) {
    var off = offDayCompetitions(iso, items);
    return off.length ? el('span', 'sr-only', ' (' + offDayNote(off).toLowerCase() + ')') : null;
  }

  // "Sun 15 Mar — 14:00 ×4 · 18:00 ×1 · cup/UEFA ×2" (lone teams; chips are not counted)
  // `level` keeps the heading order unbroken under whichever heading the view sits below (#41).
  function dayHeading(date, items, level) {
    var timed = {};
    var untimed = {};
    items.forEach(function (item) {
      var n = item.fxs.length;
      if (item.time) {
        timed[item.time] = (timed[item.time] || 0) + n;
      } else if (!item.group) {
        // A chip says how many teams it holds, so it is not counted again here.
        var label = item.fxs[0].kind === 'league' ? 'no kickoff' : 'cup/UEFA';
        untimed[label] = (untimed[label] || 0) + 1;
      }
    });
    var parts = Object.keys(timed).sort().map(function (time) { return time + ' ×' + timed[time]; });
    Object.keys(untimed).forEach(function (label) { parts.push(label + ' ×' + untimed[label]); });
    var rest = ' ' + (+date.slice(8, 10)) + ' ' + MONTHS[+date.slice(5, 7) - 1] + (parts.length ? ' — ' + parts.join(' · ') : '');
    return append(el('h' + level, 'day-head'), weekdayNode(date, items), document.createTextNode(rest), offDaySpeech(date, items));
  }

  function itemsByDay(items) {
    var days = [];
    items.forEach(function (item) {
      var last = days[days.length - 1];
      if (last && last.date === item.date) last.items.push(item);
      else days.push({ date: item.date, items: [item] });
    });
    return days;
  }

  // Appends each item: a card or row from `rowOf`, or a group chip that opens to one per team
  // (`rowOf(fx, true)` for those, whose chip already names the competition).
  function appendItems(box, items, rowOf) {
    items.forEach(function (item) {
      if (!item.group) {
        box.appendChild(rowOf(item.fxs[0]));
        return;
      }
      var chip = el('details', 'fx-group');
      var body = roveGroup(el('div', 'fx-group-body fixtures'));
      item.fxs.forEach(function (fx) { body.appendChild(rowOf(fx, true)); });
      chip.appendChild(el('summary', '', DATA.competitions[item.fxs[0].comp].name + ' · ' + item.fxs[0].round + ' · ' + plural(item.fxs.length, 'team', 'teams')));
      box.appendChild(append(chip, body));
    });
    return box;
  }

  // The drawn fixtures of `ids`, under a heading per date.
  function fixturesOf(ctx, ids, showCompetition, level) {
    var box = roveGroup(el('div', 'fixtures'));
    itemsByDay(drawItems(ctx, ids)).forEach(function (day) {
      box.appendChild(dayHeading(day.date, day.items, level));
      appendItems(box, day.items, function (fx, inGroup) { return fixtureCard(fx, showCompetition && !inGroup, ctx.club); });
    });
    return box;
  }

  // Opens the collapsed sections a drawn node sits in, so it can be seen and scrolled to.
  function openAround(node) {
    var round = node.closest('.round.collapsible');
    if (round && !round.classList.contains('is-open')) round.setOpen(true);
    var section = node.closest('details');
    while (section && section.closest('.pane-body')) {
      section.open = true;
      section = section.parentElement.closest('details');
    }
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

  // A round (or week) block. On a phone it folds under its heading, which keeps the
  // round summary in view; a club's own rounds start open since each holds one game.
  function roundBlock(ctx, key, label, fixtures) {
    var block = append(el('div', 'round'), label, fixtures);
    block.id = 'rd-' + key.toLowerCase().replace(/[^a-z0-9_]+/g, '-');
    if (!PHONE.matches) return block;
    var state = ctx.opt.state;
    var open = state.rounds[key] !== undefined ? state.rounds[key] : state.allRounds !== null ? state.allRounds : !!ctx.club;
    var title = label.querySelector('b');
    var toggle = el('button', 'round-toggle', title.textContent);
    toggle.type = 'button';
    title.textContent = '';
    title.appendChild(toggle);
    block.classList.add('collapsible');
    block.setOpen = function (value) {
      state.rounds[key] = value;
      block.classList.toggle('is-open', value);
      toggle.setAttribute('aria-expanded', String(value));
      scheduleRove();
    };
    block.setOpen(open);
    toggle.addEventListener('click', function () { block.setOpen(!block.classList.contains('is-open')); });
    return block;
  }

  // "Expand all rounds" / "Collapse all rounds", on phones only.
  function roundTools(ctx, root) {
    if (!PHONE.matches) return;
    var state = ctx.opt.state;
    var expanded = state.allRounds === true;
    var button = el('button', 'bar-btn expand-all', expanded ? 'Collapse all rounds' : 'Expand all rounds');
    button.type = 'button';
    button.addEventListener('click', function () {
      state.allRounds = !expanded;
      state.rounds = {};
      ctx.opt.render();
    });
    root.appendChild(append(el('div', 'round-tools'), button));
  }

  var KIND_GROUPS = [['league', 'Leagues'], ['cup', 'Cups'], ['european', 'Europe']];

  function competitionSection(ctx, view, rounds) {
    var state = ctx.opt.state;
    var section = el('details', 'comp');
    section.open = !state.closed[view.id];
    section.addEventListener('toggle', function () {
      state.closed[view.id] = !section.open;
    });
    var head = el('summary', 'comp-head');
    append(head, el('h4', 'comp-name', view.name), el('span', 'meta', view.meta));
    section.appendChild(head);
    rounds.forEach(function (round) {
      section.appendChild(roundBlock(
        ctx,
        view.id + '|' + round.title,
        roundLabel(round.title, round.text, round.note, totals(ctx.fx, ctx.visible(round.ids))),
        fixturesOf(ctx, round.ids, false, 5)
      ));
    });
    return section;
  }

  // Leagues, then cups, then Europe: each competition a collapsible section whose
  // heading sticks under the bar, with a jump list above them all.
  function byCompetitionView(ctx) {
    var root = document.createDocumentFragment();
    var jumpState = ctx.opt.state;
    var shown = [];
    ctx.opt.data.byCompetition.concat(DATA.sharedByCompetition).forEach(function (view) {
      // Under a filter a round (or a whole competition) with nothing left collapses rather than leaving a bare heading.
      var rounds = view.rounds.filter(function (round) {
        return !ctx.filtering || !round.ids.length || ctx.visible(round.ids).length;
      });
      if (ctx.filtering && view.rounds.length && !rounds.length) return;
      shown.push({ view: view, section: competitionSection(ctx, view, rounds) });
    });
    if (!shown.length) return root;
    var jump = el('nav', 'comp-jump');
    jump.setAttribute('aria-label', 'Jump to a competition');
    shown.forEach(function (entry) {
      var button = el('button', 'comp-jump-btn', entry.view.name);
      button.type = 'button';
      button.addEventListener('click', function () {
        entry.section.open = true;
        jumpState.closed[entry.view.id] = false;
        entry.section.scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
      });
      jump.appendChild(button);
    });
    root.appendChild(jump);
    roundTools(ctx, root);
    KIND_GROUPS.forEach(function (group) {
      var sections = shown.filter(function (entry) { return entry.view.kind === group[0]; });
      if (!sections.length) return;
      root.appendChild(el('h3', 'group-head', group[1]));
      sections.forEach(function (entry) { root.appendChild(entry.section); });
    });
    return root;
  }

  function combinedCalendarView(ctx) {
    var root = document.createDocumentFragment();
    var any = false;
    ctx.opt.data.weeks.forEach(function (week) {
      var shown = ctx.visible(week.ids);
      if (!shown.length) return;
      if (!any) roundTools(ctx, root);
      any = true;
      root.appendChild(roundBlock(
        ctx, 'week|' + week.label, roundLabel(week.label, week.dates, null, totals(ctx.fx, shown)), fixturesOf(ctx, week.ids, true, 3)
      ));
    });
    return root;
  }

  var LIST_COLUMNS = ['Time', 'Round', 'Competition', 'Team / Opponent', 'Venue', 'Rules', 'Score'];

  function dateRow(date, items) {
    var th = append(el('th'), weekdayNode(date, items), document.createTextNode(shortDate(date).slice(weekday(date).length)), offDaySpeech(date, items));
    th.colSpan = LIST_COLUMNS.length;
    return append(el('tr', 'date-row'), th);
  }

  // Whether a list chip is open: the reader's choice once made, else open while a text query is on.
  function listGroupOpen(state, key) {
    return state.open[key] === undefined ? !!state.query.trim() : state.open[key];
  }

  // A cup or European round date in the list: a chip row that opens to its team rows.
  function groupRows(body, item, state, refresh, club) {
    var first = item.fxs[0];
    var key = groupKey(first);
    var toggle = el('button', 'fx-group-toggle', DATA.competitions[first.comp].name + ' · ' + first.round + ' · ' + plural(item.fxs.length, 'team', 'teams'));
    toggle.type = 'button';
    toggle.addEventListener('click', function () {
      state.open[key] = !listGroupOpen(state, key);
      refresh();
    });
    var cell = append(el('td'), toggle);
    cell.colSpan = LIST_COLUMNS.length;
    var row = el('tr', 'fx-group-row');
    row.setAttribute('data-group', key);
    row.setAttribute('data-search', item.fxs.map(searchText).join(' '));
    body.appendChild(append(row, cell));
    item.fxs.forEach(function (fx) {
      var member = fixtureRow(fx, club);
      member.classList.add('fx-member');
      member.setAttribute('data-group', key);
      body.appendChild(member);
    });
  }

  // Hides the rows the text filter does not match. A chip whose teams match opens
  // while the filter is on; a date row shows only with a row under it.
  function applyListFilter(table, state) {
    var q = state.query.trim().toLowerCase();
    var matches = function (row) { return !q || row.getAttribute('data-search').indexOf(q) !== -1; };
    var open = {};
    var dateRowNode = null;
    var dateShown = false;
    Array.prototype.forEach.call(table.tBodies[0].rows, function (row) {
      if (row.classList.contains('date-row')) {
        if (dateRowNode) dateRowNode.hidden = !dateShown;
        dateRowNode = row;
        dateShown = false;
        return;
      }
      var key = row.getAttribute('data-group');
      var show;
      if (row.classList.contains('fx-group-row')) {
        show = matches(row);
        var expanded = listGroupOpen(state, key);
        open[key] = expanded;
        row.querySelector('.fx-group-toggle').setAttribute('aria-expanded', String(expanded));
      } else if (row.classList.contains('fx-member')) {
        show = !!open[key] && matches(row);
      } else {
        show = matches(row);
      }
      row.hidden = !show;
      if (show) dateShown = true;
    });
    if (dateRowNode) dateRowNode.hidden = !dateShown;
    scheduleRove();
  }

  // "Combined · list": a sticky header over one table, a date row above each day, a row
  // per fixture, and a cup or European round date folded into one chip row.
  function combinedListView(ctx) {
    var state = ctx.opt.state;
    var items = drawItems(ctx, ctx.opt.data.list);
    if (!items.length) return noMatches();
    var body = roveGroup(el('tbody'));
    var head = el('tr');
    LIST_COLUMNS.forEach(function (name) { head.appendChild(el('th', name === 'Score' ? 'fx-score' : '', name)); });
    var table = append(el('table', 'fx-table'), append(el('thead'), head), body);
    var refresh = function () { applyListFilter(table, state); };
    var filter = el('input', 'list-filter');
    filter.type = 'search';
    filter.placeholder = 'Filter by team, competition, round, venue or rule';
    filter.setAttribute('aria-label', 'Filter the list');
    filter.value = state.query;
    filter.addEventListener('input', function () {
      state.query = filter.value;
      refresh();
      syncHash();
    });
    itemsByDay(items).forEach(function (day) {
      body.appendChild(dateRow(day.date, day.items));
      day.items.forEach(function (item) {
        if (item.group) groupRows(body, item, state, refresh, ctx.club);
        else body.appendChild(fixtureRow(item.fxs[0], ctx.club));
      });
    });
    refresh();
    var root = el('div');
    append(root, append(el('div', 'list-tools'), filter), table);
    return root;
  }

  // The month calendar. One button per date. Months with a league fixture get a
  // week grid; the rest are listed under "Other dates". The day detail opens as
  // a block directly under the week row (or other-dates row) that holds the day.
  function monthCalendarView(ctx) {
    var data = ctx.opt.data;
    var state = ctx.opt.state;
    var days = {};
    data.days.forEach(function (day) { days[day.date] = day; });
    var maybes = DATA.maybe || {};
    if (!data.months.length) return el('div', 'desc', 'Nothing scheduled yet.');
    var root = el('div');
    var cells = {};

    // The visible games of a day, counted per competition (in the day's own competition order).
    function countsOf(day) {
      var counts = {};
      ctx.visible(day.ids).forEach(function (uid) {
        var comp = ctx.fx[uid].comp;
        counts[comp] = (counts[comp] || 0) + 1;
      });
      return day.comps.filter(function (comp) { return counts[comp]; }).map(function (comp) {
        return { comp: comp, n: counts[comp] };
      });
    }

    // A date button: the day number (grid cells only), a dot and count per
    // competition, and the day's figure. The label names the counts.
    function dayButton(iso, className, withNumber) {
      var day = days[iso];
      var comps = day ? countsOf(day) : [];
      var shownToday = day ? ctx.visible(day.ids) : [];
      var sum = shownToday.length ? totals(ctx.fx, shownToday) : null;
      var button = el('button', className);
      button.type = 'button';
      button.setAttribute('data-date', iso);
      button.setAttribute('aria-expanded', 'false');
      // One tab stop for the whole calendar; the arrow keys walk the days (#41).
      button.tabIndex = -1;
      var label = longDateUnpadded(iso) + (maybes[iso] ? ' (a team may play, if it advances)' : '') +
        (comps.length ? ': ' + comps.map(function (c) {
          return DATA.competitions[c.comp].name + ' ' + c.n;
        }).join(', ') : '') +
        (sum ? '; ' + plural(sum.games, 'game', 'games') + (sum.badged ? '; net ' + netText(sum.net) + ' pts' : '') : '');
      button.setAttribute('aria-label', label);
      button.setAttribute('data-label', label);
      if (withNumber) append(button, el('span', 'cal-daynum', String(+iso.slice(8))));
      var box = el('span', 'cal-comps');
      comps.forEach(function (c) {
        var meta = DATA.competitions[c.comp];
        var item = el('span', 'cal-comp ' + meta.cls);
        var dot = el('span', 'cal-dot ' + meta.cls);
        dot.setAttribute('aria-hidden', 'true');
        dot.title = meta.name;
        item.appendChild(dot);
        item.appendChild(el('span', 'cal-lbl', meta.short));
        if (c.n > 1) item.appendChild(el('span', 'cal-n', String(c.n)));
        box.appendChild(item);
      });
      button.appendChild(box);
      if (maybes[iso]) {
        var maybeMark = el('span', 'cal-maybe', '?');
        maybeMark.setAttribute('aria-hidden', 'true');
        maybeMark.title = 'A team plays here only if it advances';
        button.appendChild(maybeMark);
      }
      if (sum) button.appendChild(dayFigure(sum));
      cells[iso] = button;
      return button;
    }

    // A month with a league fixture is drawn as a grid, whatever the filters hide.
    function hasLeague(month) {
      return month.weeks.some(function (week) {
        return week.some(function (iso) {
          return iso && days[iso] && days[iso].ids.some(function (uid) { return ctx.fx[uid].kind === 'league'; });
        });
      });
    }

    // Short label for the jump bar: the year is added to January and the first month.
    function shortLabel(month, first) {
      var name = MONTHS[+month.key.slice(5) - 1].slice(0, 3);
      return (first || month.key.slice(5) === '01') ? name + ' ' + month.key.slice(0, 4) : name;
    }

    var jump = el('nav', 'month-jump');
    jump.setAttribute('aria-label', 'Jump to a month');
    var details = el('div', 'day-details');
    details.id = 'cal-day-detail';
    details.setAttribute('role', 'region');
    // The detail is rebuilt under another week on every pick, so a separate, lasting region does the announcing.
    var live = el('div', 'sr-only');
    live.setAttribute('role', 'status');
    var others = [];

    data.months.forEach(function (month, index) {
      var dated = month.weeks.some(function (week) {
        return week.some(function (iso) { return iso && days[iso]; });
      });
      if (!dated) return;
      var jumpButton = el('button', '', shortLabel(month, index === 0));
      jumpButton.type = 'button';
      jumpButton.setAttribute('aria-label', month.label);
      jumpButton.setAttribute('data-jump', month.key);
      jump.appendChild(jumpButton);
      if (!hasLeague(month)) { others.push(month); return; }

      var wrap = el('div', 'month');
      wrap.setAttribute('data-month-key', month.key);
      var grid = el('div', 'month-grid');
      var head = el('div', 'month-grid-row month-grid-head');
      WEEKDAYS.slice(1).concat(WEEKDAYS[0]).forEach(function (name) { head.appendChild(el('span', '', name)); });
      grid.appendChild(head);
      month.weeks.forEach(function (week) {
        var row = el('div', 'month-grid-row');
        week.forEach(function (iso) {
          // A neighbouring month's day: empty space, so the date is not listed twice.
          if (!iso) {
            var pad = el('span', 'cal-day cal-pad');
            pad.setAttribute('aria-hidden', 'true');
            row.appendChild(pad);
            return;
          }
          row.appendChild(dayButton(iso, 'cal-day', true));
        });
        grid.appendChild(row);
      });
      append(wrap, el('h3', '', month.label), grid);
      root.appendChild(wrap);
    });

    if (others.length) {
      var section = el('section', 'other-dates');
      append(section, el('h3', '', 'Other dates'));
      others.forEach(function (month) {
        var row = el('div', 'other-row');
        row.setAttribute('data-month-key', month.key);
        var chips = el('div', 'other-chips');
        var placed = 0;
        month.weeks.forEach(function (week) {
          week.forEach(function (iso) {
            if (!iso || !days[iso]) return;
            var chip = dayButton(iso, 'cal-day other-date', false);
            chip.insertBefore(el('span', 'other-day', weekday(iso).slice(0, 3) + ' ' + String(+iso.slice(8))), chip.firstChild);
            chips.appendChild(chip);
            placed++;
          });
        });
        if (!placed) return;
        append(row, el('span', 'other-month', month.label), chips);
        section.appendChild(row);
      });
      if (section.querySelector('.other-row')) root.appendChild(section);
    }
    root.insertBefore(jump, root.firstChild);

    // Previous and next match day: the nearest dates with a visible game.
    function matchDayFrom(iso, step) {
      var found = null;
      data.days.forEach(function (day) {
        if (day.date === iso) return;
        var inRange = step < 0 ? day.date < iso : day.date > iso;
        if (inRange && ctx.visible(day.ids).length && (found === null || (step < 0 ? day.date > found : day.date < found))) {
          found = day.date;
        }
      });
      return found;
    }

    // The date `n` days from `iso`, as an ISO string.
    function shiftDate(iso, n) {
      var d = new Date(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10) + n));
      return d.toISOString().slice(0, 10);
    }

    // Redraws the detail for `iso` (or closes it when `iso` is ''), under the row
    // holding that date. Returns the block, or null when none is open.
    function placeDetails(iso) {
      details.remove();
      details.textContent = '';
      root.querySelectorAll('button[data-date]').forEach(function (button) {
        var open = button.dataset.date === iso;
        button.classList.toggle('selected', open);
        button.setAttribute('aria-expanded', String(open));
        if (open) button.setAttribute('aria-controls', details.id);
        else button.removeAttribute('aria-controls');
      });
      var anchor = iso && cells[iso];
      if (!anchor) return null;
      details.setAttribute('aria-label', 'Games on ' + longDateUnpadded(iso));
      var day = days[iso];
      var ids = day ? ctx.visible(day.ids) : [];
      var head = append(el('div', 'rlabel'), el('b', '', day ? day.label : longDateUnpadded(iso)));
      var body = append(el('div', 'day-detail'), head);
      if (ids.length) {
        var box = appendItems(roveGroup(el('div', 'fixtures')), drawItems(ctx, day.ids), function (fx, inGroup) { return dayRow(fx, inGroup, ctx.club); });
        append(body, summaryNode(totals(ctx.fx, ids), false), box);
      } else {
        append(body, el('div', 'day-detail-empty', 'No games on this day.'));
      }
      if (maybes[iso]) {
        var maybeList = el('ul', 'day-maybe');
        maybes[iso].forEach(function (line) { maybeList.appendChild(el('li', '', line)); });
        body.appendChild(maybeList);
      }
      append(details, body, dayStep(iso));
      anchor.closest('.month-grid-row, .other-row').insertAdjacentElement('afterend', details);
      return details;
    }

    // "‹ Previous match day" and "Next match day ›" in the open detail.
    function dayStep(iso) {
      var box = el('div', 'day-step');
      [[-1, '‹ Previous match day'], [1, 'Next match day ›']].forEach(function (pair) {
        var target = matchDayFrom(iso, pair[0]);
        var button = el('button', '', pair[1]);
        button.type = 'button';
        button.disabled = target === null;
        button.setAttribute('data-step', String(pair[0]));
        if (target !== null) button.addEventListener('click', function () { openDay(target, true); });
        box.appendChild(button);
      });
      return box;
    }

    // Says what a pick opened, since the detail sits below the week and focus stays on the day.
    function announce(iso) {
      if (!iso) {
        live.textContent = 'Day details closed.';
        return;
      }
      var day = days[iso];
      var ids = day ? ctx.visible(day.ids) : [];
      live.textContent = longDateUnpadded(iso) + ': ' + (ids.length
        ? plural(ids.length, 'game', 'games') + ' shown just below. Tab moves into the list.'
        : 'no games.');
    }

    function openDay(iso, moveFocus) {
      state.date = iso;
      syncHash();
      var shown = placeDetails(iso);
      if (!shown) return;
      announce(iso);
      ctx.opt.markLens(shown);
      if (moveFocus) cells[iso].focus({ preventScroll: true });
      shown.scrollIntoView({ block: 'nearest', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
    }

    root.addEventListener('click', function (event) {
      var jumpTo = event.target.closest('[data-jump]');
      if (jumpTo) {
        var target = root.querySelector('[data-month-key="' + jumpTo.getAttribute('data-jump') + '"]');
        if (target) target.scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
        return;
      }
      var button = event.target.closest('button[data-date]');
      if (!button) return;
      var iso = button.dataset.date;
      if (state.date === iso) {
        state.date = '';
        syncHash();
        placeDetails('');
        announce('');
        return;
      }
      openDay(iso, false);
    });

    // Arrow keys walk the date buttons: left and right by a day, up and down by a
    // week, skipping empty days and crossing months.
    root.addEventListener('keydown', function (event) {
      var steps = { ArrowLeft: -1, ArrowRight: 1, ArrowUp: -7, ArrowDown: 7 };
      var step = steps[event.key];
      if (!event.target.matches || !event.target.matches('button[data-date]')) return;
      if (event.key === 'Home' || event.key === 'End') {
        var dates = Object.keys(cells).sort();
        event.preventDefault();
        cells[dates[event.key === 'Home' ? 0 : dates.length - 1]].focus();
        return;
      }
      if (!step) return;
      var start = event.target.dataset.date;
      var target = null;
      // Enough steps to reach across the whole calendar, far past its last date.
      for (var k = 1; k <= 1000 && !target; k++) {
        var candidate = shiftDate(start, k * step);
        if (cells[candidate]) target = candidate;
      }
      if (!target) return;
      event.preventDefault();
      cells[target].focus();
    });

    // The tab stop is the picked day, else the first one.
    root.addEventListener('focusin', function (event) {
      if (!event.target.matches || !event.target.matches('button[data-date]')) return;
      Object.keys(cells).forEach(function (key) { cells[key].tabIndex = cells[key] === event.target ? 0 : -1; });
    });
    var dated = Object.keys(cells).sort();
    var stop = cells[state.date] || cells[dated[0]];
    if (stop) stop.tabIndex = 0;
    root.appendChild(live);
    placeDetails(state.date);
    return root;
  }

  // ------------------------------------------------------ club timeline
  //
  // A club's teams side by side, one row per date. Rest gaps and the pairing
  // tags are worked out from each team's own fixtures, hidden or not, so a
  // filter narrows what is drawn without changing what the gaps mean.

  var DAY_MS = 86400000;

  function daysBetween(from, to) {
    var start = Date.UTC(+from.slice(0, 4), +from.slice(5, 7) - 1, +from.slice(8, 10));
    var end = Date.UTC(+to.slice(0, 4), +to.slice(5, 7) - 1, +to.slice(8, 10));
    return Math.round((end - start) / DAY_MS);
  }

  function byDateThenUid(a, b) {
    return a.date < b.date ? -1 : a.date > b.date ? 1 : a.uid - b.uid;
  }

  // Where a team plays a fixture: 'home', 'away', 'neutral', or 'unknown' for a
  // tie whose venue is not decided yet. Only home and away count towards tags.
  var VENUE_OF_SHAPE = { square: 'home', diamond: 'away', circle: 'neutral', 'square-dim': 'unknown' };
  var VENUE_MARK = {
    home: ['H', 'Home'], away: ['A', 'Away'], neutral: ['N', 'Neutral venue'], unknown: ['?', 'Venue not decided']
  };

  function venueFor(fx, teamId) {
    if (fx.away) return fx.home.team === teamId ? 'home' : 'away';
    return VENUE_OF_SHAPE[fx.home.shape] || 'unknown';
  }

  function opponentText(fx, teamId) {
    if (!fx.away) return fx.round + ' · ' + fx.versus;
    return fx.home.team === teamId ? fx.away.name : fx.home.name;
  }

  function timelineCard(fx, teamId, gap) {
    var mark = VENUE_MARK[venueFor(fx, teamId)];
    var card = tag(el('div', 'tl-fx has-detail' + edgeClass(fx)), fx);
    var venue = el('span', 'tl-ha', mark[0]);
    venue.title = mark[1];
    var gapText = gap === null ? 'first match' : plural(gap, 'day', 'days') + ' since last match';
    var visual = append(
      el('span', 'tl-visual'),
      append(el('span', 'tl-top'), competitionTag(fx), venue),
      el('span', 'tl-opp', opponentText(fx, teamId)),
      el('span', 'tl-gap', (fx.time ? fx.time + ' · ' : '') + gapText)
    );
    return append(card, fixtureControl('tl-main', visual, fixtureSentence(fx) + ' ' + gapText + '.'), flagBox(fx));
  }

  // Tags for each date over the club's teams: two of them playing that day
  // (flagged when both are at home), and the back-to-back pairs with the day
  // before or after, both at home or both away.
  function timelineNotes(own, teams) {
    var notes = {};
    function note(date, text, bad) {
      var list = notes[date] || (notes[date] = []);
      if (!list.some(function (n) { return n.text === text; })) list.push({ text: text, bad: bad });
    }
    var byDate = {};
    own.forEach(function (fx) { (byDate[fx.date] || (byDate[fx.date] = [])).push(fx); });
    var dates = Object.keys(byDate).sort();
    dates.forEach(function (date) {
      var playing = [];
      var atHome = [];
      teams.forEach(function (t) {
        var mine = byDate[date].filter(function (fx) { return fx.teams.indexOf(t) !== -1; });
        if (!mine.length) return;
        playing.push(t);
        if (mine.some(function (fx) { return venueFor(fx, t) === 'home'; })) atHome.push(t);
      });
      if (playing.length < 2) return;
      if (atHome.length === playing.length) note(date, 'both home the same day', true);
      else note(date, 'same day', false);
    });
    dates.forEach(function (date, i) {
      var next = dates[i + 1];
      if (!next || daysBetween(date, next) !== 1) return;
      byDate[date].forEach(function (a) {
        byDate[next].forEach(function (b) {
          teams.forEach(function (t) {
            teams.forEach(function (u) {
              if (u === t || a.teams.indexOf(t) === -1 || b.teams.indexOf(u) === -1) return;
              var va = venueFor(a, t);
              var vb = venueFor(b, u);
              var kind = va === 'home' && vb === 'home' ? 'home pair' : va === 'away' && vb === 'away' ? 'away pair' : '';
              if (!kind) return;
              note(date, kind, false);
              note(next, kind, false);
            });
          });
        });
      });
    });
    return notes;
  }

  function clubTimelineView(ctx) {
    var club = ctx.club;
    if (!club) return dualClubChooser(ctx.opt);
    var teams = teamsOf(club);
    // A picked team gets its own lane; the pairs and gaps still read both teams.
    var lanes = ctx.team ? [ctx.team] : teams;
    var own = ctx.fx.filter(function (fx) { return fx.clubs.indexOf(club) !== -1; });
    var gap = {};
    teams.forEach(function (teamId) {
      var mine = own.filter(function (fx) { return fx.teams.indexOf(teamId) !== -1; }).sort(byDateThenUid);
      mine.forEach(function (fx, i) { gap[fx.uid + ':' + teamId] = i ? daysBetween(mine[i - 1].date, fx.date) : null; });
    });
    var notes = timelineNotes(own, teams);
    var byDate = {};
    own.forEach(function (fx) { (byDate[fx.date] || (byDate[fx.date] = [])).push(fx); });

    var root = el('div');
    var legend = 'One row per date. Each card is one team’s match that day, with the days since that team last played. ' +
      'Tags mark two of the club’s teams on the same day, or on back-to-back days.';
    if (ctx.team) legend += ' With one team picked, the tags and gaps still count both teams.';
    root.appendChild(el('p', 'desc tl-legend', legend));
    var grid = roveGroup(el('div', 'tl-grid'));
    grid.style.setProperty('--lanes', String(lanes.length));
    var head = el('div', 'tl-head');
    head.appendChild(el('span', 'tl-date-h', 'Date'));
    lanes.forEach(function (teamId) { head.appendChild(el('span', 'tl-lane', DATA.teams[teamId].label)); });
    grid.appendChild(head);

    var rows = 0;
    Object.keys(byDate).sort().forEach(function (date) {
      var shown = {};
      ctx.visible(byDate[date].map(function (fx) { return fx.uid; })).forEach(function (uid) { shown[uid] = true; });
      if (!Object.keys(shown).length) return;
      rows++;
      var dayNotes = notes[date] || [];
      var row = el('div', 'tl-row' + (dayNotes.some(function (n) { return n.bad; }) ? ' tl-clash' : ''));
      var dateCell = append(el('div', 'tl-date'), el('b', '', shortDate(date)));
      dayNotes.forEach(function (n) { dateCell.appendChild(el('span', 'tl-tag' + (n.bad ? ' tl-tag-bad' : ''), n.text)); });
      row.appendChild(dateCell);
      lanes.forEach(function (teamId) {
        var cell = el('div', 'tl-cell');
        byDate[date].filter(function (fx) { return fx.teams.indexOf(teamId) !== -1 && shown[fx.uid]; })
          .forEach(function (fx) { cell.appendChild(timelineCard(fx, teamId, gap[fx.uid + ':' + teamId])); });
        row.appendChild(cell);
      });
      grid.appendChild(row);
    });
    if (!rows) return noMatches();
    root.appendChild(grid);
    return root;
  }

  // Shown when no club is picked: the six dual clubs as ways in.
  function dualClubChooser(opt) {
    var box = el('div', 'tl-choose');
    box.appendChild(el('p', 'desc', 'Pick a club in Filters to see its two teams on one timeline, or start from a dual club:'));
    var list = el('div', 'tl-choices');
    DATA.dualClubs.forEach(function (club) {
      var button = el('button', 'tl-club-btn', DATA.clubNames[club]);
      button.type = 'button';
      button.addEventListener('click', function () { opt.setClub(club); });
      list.appendChild(button);
    });
    box.appendChild(list);
    return box;
  }

  var VIEWS = {
    'by-competition': byCompetitionView,
    'month-calendar': monthCalendarView,
    'combined-calendar': combinedCalendarView,
    'combined-list': combinedListView,
    'club-timeline': clubTimelineView
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

  function ruleName(rule) { return rule.name; }

  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : many); }

  // A rule total as a whole number, one decimal when it is small.
  function points(total) {
    var text = Math.abs(total) < 10 ? (Math.round(total * 10) / 10).toFixed(1) : String(Math.round(total));
    return total > 0 ? '+' + text : text.replace('-', '−');
  }

  function initialState(panel) {
    return {
      view: PHONE.matches ? 'month-calendar' : panel.querySelector('.view-btn').dataset.view,
      club: '', team: '', date: '', rule: '', only: false,
      kinds: DEFAULT_KINDS.slice(), query: '', open: {}, closed: {}, rounds: {}, allRounds: null
    };
  }

  function Option(index, panel) {
    this.data = DATA.options[index];
    this.panel = panel;
    this.fx = null;
    this.stats = null;
    // The date last read off the calendar, kept as the reader scrolls: switching
    // tabs means scrolling up to them, when no fixture is on screen any more.
    this.anchor = '';
    this.state = initialState(panel);
    this.target = null;
    this.clubSelect = panel.querySelector('.club-select');
    this.lensSelect = panel.querySelector('.lens-select');
    this.lensOnly = panel.querySelector('.lens-only-box');
    this.lensCaption = panel.querySelector('.lens-caption');
    this.headlines = panel.querySelector('.headlines');
    this.strip = roveGroup(panel.querySelector('.season-strip'));
    this.scopeBanner = panel.querySelector('.scope-banner');
    this.scopeText = panel.querySelector('.scope-text');
    this.scopeCard = panel.querySelector('.scope-card');
    this.teamFilter = panel.querySelector('.team-filter');
    this.teamSelect = panel.querySelector('.team-select');
    this.scopeCount = panel.querySelector('.scope-count');
    this.lensCount = panel.querySelector('.lens-count');
    var self = this;
    // Each competition button toggles its kind; the chip in the bar resets them.
    panel.querySelectorAll('.kind-btn').forEach(function (button) {
      button.addEventListener('click', function () {
        var kind = button.dataset.kind;
        var kinds = self.state.kinds;
        self.setKinds(kinds.indexOf(kind) !== -1 ? kinds.filter(function (k) { return k !== kind; }) : kinds.concat(kind));
      });
    });
    panel.querySelectorAll('.kind-hint-btn').forEach(function (button) {
      button.addEventListener('click', function () {
        var missing = button.parentNode.getAttribute('data-missing').split(' ');
        self.setKinds(self.state.kinds.concat(missing));
      });
    });
    panel.querySelector('.kind-clear').addEventListener('click', function () {
      self.setKinds(DEFAULT_KINDS);
      self.focusFilter(panel.querySelector('.kind-btn'));
    });
    panel.querySelectorAll('.view-btn').forEach(function (button) {
      button.addEventListener('click', function () {
        self.state.view = button.dataset.view;
        pushNext = true;
        self.render();
      });
    });
    this.clubSelect.addEventListener('change', function () { self.setClub(self.clubSelect.value); });
    this.teamSelect.addEventListener('change', function () { self.setTeam(self.teamSelect.value); });
    panel.querySelector('.scope-clear').addEventListener('click', function () { self.setClub(''); });
    panel.querySelector('.team-clear').addEventListener('click', function () {
      self.setTeam('');
      self.focusFilter(self.teamSelect);
    });
    this.lensSelect.addEventListener('change', function () { self.setLens(self.lensSelect.value); });
    this.lensOnly.addEventListener('change', function () { self.setLens(self.state.rule, self.lensOnly.checked); });
    panel.querySelector('.lens-clear').addEventListener('click', function () {
      self.setLens('');
      self.focusFilter(self.lensSelect);
    });
    panel.querySelector('.club-clear').addEventListener('click', function () {
      self.setClub('');
      self.focusFilter(self.clubSelect);
    });
  }

  // The one place the club filter changes. A dual club opens on the timeline;
  // clearing the club leaves the timeline, which has nothing to draw without one.
  Option.prototype.setClub = function (club) {
    var state = this.state;
    state.club = club || '';
    state.team = '';
    // Picking a club opens its rounds, whatever was folded before.
    if (state.club) {
      state.rounds = {};
      state.allRounds = null;
    }
    this.clubSelect.value = state.club;
    // A dual club opens on the timeline, unless the reader is already on another view.
    if (state.club && isDual(state.club) && state.view === 'by-competition') state.view = 'club-timeline';
    else if (!state.club && state.view === 'club-timeline') state.view = 'by-competition';
    this.render();
  };

  Option.prototype.setKinds = function (kinds) {
    this.state.kinds = ALL_KINDS.filter(function (kind) { return kinds.indexOf(kind) !== -1; });
    this.render();
  };

  // The line above each view naming the competitions that are hidden, with a button to show them.
  Option.prototype.renderHints = function () {
    var kinds = this.state.kinds;
    var missing = ALL_KINDS.filter(function (kind) { return kinds.indexOf(kind) === -1; });
    var names = missing.map(function (kind) { return KIND_LABEL[kind]; });
    var list = names.length > 1 ? names.slice(0, -1).join(', ') + ' and ' + names[names.length - 1] : names[0];
    var plural = missing.length > 1 || missing[0] !== 'european';
    var text = list + (plural ? ' are hidden' : ' is hidden');
    var button = plural ? 'Show them' : 'Show it';
    this.panel.querySelectorAll('.kind-hint').forEach(function (hint) {
      hint.hidden = !missing.length;
      hint.querySelector('.kind-hint-text').textContent = text;
      hint.querySelector('.kind-hint-btn').textContent = button;
      hint.setAttribute('data-missing', missing.join(' '));
    });
  };

  Option.prototype.renderKinds = function () {
    var kinds = this.state.kinds;
    this.panel.querySelectorAll('.kind-btn').forEach(function (button) {
      button.setAttribute('aria-pressed', String(kinds.indexOf(button.dataset.kind) !== -1));
    });
  };

  Option.prototype.setTeam = function (team) {
    this.state.team = team || '';
    this.render();
  };

  // Option tabs share the filters, view, rule lens and selected day, so the same
  // slice of the season can be flipped between options.
  Option.prototype.adopt = function (shared) {
    this.state.club = shared.club;
    this.state.team = shared.team;
    this.state.view = shared.view;
    this.state.rule = shared.rule;
    this.state.only = shared.only;
    this.state.date = shared.date;
    this.state.kinds = shared.kinds.slice();
    this.state.query = shared.query;
    this.state.open = Object.assign({}, shared.open);
    this.state.closed = Object.assign({}, shared.closed);
    this.state.rounds = Object.assign({}, shared.rounds);
    this.state.allRounds = shared.allRounds;
    this.clubSelect.value = shared.club;
  };

  // The date of the first fixture drawn in the viewport, below the sticky bar.
  // '' when no fixture is on screen.
  Option.prototype.anchorDate = function () {
    var body = this.activeBody();
    var bar = this.panel.querySelector('.controls');
    if (!body) return '';
    var top = bar ? bar.getBoundingClientRect().bottom : 0;
    var nodes = body.querySelectorAll('[data-uid]');
    for (var i = 0; i < nodes.length; i++) {
      var rect = nodes[i].getBoundingClientRect();
      if (rect.bottom > top && rect.top < window.innerHeight) return this.fx[+nodes[i].getAttribute('data-uid')].date;
    }
    return '';
  };

  // Scrolls the drawn view to its first fixture on or after `date`.
  Option.prototype.scrollToDate = function (date) {
    var body = this.activeBody();
    if (!body) return;
    var nodes = body.querySelectorAll('[data-uid]');
    for (var i = 0; i < nodes.length; i++) {
      if (this.fx[+nodes[i].getAttribute('data-uid')].date >= date) {
        (nodes[i].closest('.round') || nodes[i]).scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
        return;
      }
    }
  };

  // Focus goes to the filter's own select, or to the Filters button while a
  // narrow bar keeps the selects collapsed, so removing a chip never drops focus.
  Option.prototype.focusFilter = function (select) {
    (select.offsetParent ? select : this.panel.querySelector('.filters-toggle')).focus();
  };

  Option.prototype.context = function () {
    var club = this.state.club;
    var team = this.state.team;
    var rule = this.state.rule;
    var only = this.state.only;
    var kinds = this.state.kinds;
    var fx = this.fx;
    function matches(f) { return !rule || f.rules.indexOf(rule) !== -1; }
    function show(f) {
      return kinds.indexOf(f.kind) !== -1 && (!club || f.clubs.indexOf(club) !== -1) &&
        (!team || f.teams.indexOf(team) !== -1) && (!only || matches(f));
    }
    return {
      opt: this,
      fx: fx,
      club: club,
      team: team,
      rule: rule,
      filtering: !!club || !!team || only || kinds.length !== ALL_KINDS.length,
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
  };

  // ------------------------------------------------------- club scorecard
  //
  // With a club selected, the upsides, problems and breakdown are rebuilt from
  // the club's share of each rule: the points of the badges on its fixtures
  // (items naming one of its teams or the club itself) plus the season-level
  // events attributed to it (`clubUnpinned`, which no fixture carries). Rules
  // scored per competition (preferred weekday) belong to no club and show none.
  // The score, hard-rule list and fairness stay season-wide.

  function groupNamesOf(club) {
    var names = {};
    names[DATA.clubNames[club]] = true;
    teamsOf(club).forEach(function (teamId) { names[DATA.teams[teamId].label] = true; });
    return names;
  }

  // The club's flag items by rule id: {fx, flag, item}.
  Option.prototype.clubItems = function (club) {
    var cache = this.clubCache || (this.clubCache = {});
    if (cache[club]) return cache[club];
    var names = groupNamesOf(club);
    var byRule = {};
    this.fx.forEach(function (fx) {
      if (fx.clubs.indexOf(club) === -1) return;
      fx.flags.forEach(function (flag) {
        if (flag.rule.inv) return;
        flag.items.forEach(function (item) {
          if (!names[item.group]) return;
          (byRule[flag.rule.id] || (byRule[flag.rule.id] = [])).push({ fx: fx, flag: flag, item: item });
        });
      });
    });
    cache[club] = byRule;
    return byRule;
  };

  // The club's figure for every rule, in breakdown order. `fixtures` counts the
  // fixtures carrying its items, `loose` the events attributed to it that no
  // fixture carries, and `count` the events (an event shared by n fixtures counts once).
  Option.prototype.clubScores = function (club) {
    var items = this.clubItems(club);
    var loose = {};
    (this.data.clubUnpinned[club] || []).forEach(function (row) { loose[row[0]] = row; });
    var ranks = this.data.clubRanks[club] || [];
    var rows = this.data.order.map(function (index) {
      var rule = DATA.rules[index];
      var total = 0;
      var count = 0;
      var fixtures = {};
      (items[rule.id] || []).forEach(function (entry) {
        total += entry.item.delta;
        count += 1 / entry.item.n;
        fixtures[entry.fx.uid] = true;
      });
      var extra = loose[index];
      if (extra) {
        total += extra[1];
        count += extra[2];
      }
      total = Math.round(total * 100) / 100;
      return {
        rule: rule,
        ranks: ranks.filter(function (row) { return row.id === rule.id; }),
        total: total,
        share: null,
        count: Math.round(count),
        fixtures: Object.keys(fixtures).length,
        loose: extra ? extra[2] : 0
      };
    });
    // A club's share is of its own movement: the sum of its soft totals' magnitudes.
    var gross = rows.reduce(function (sum, row) { return row.rule.hard ? sum : sum + Math.abs(row.total); }, 0);
    rows.forEach(function (row) {
      if (!row.rule.hard) row.share = gross ? 100 * Math.abs(row.total) / gross : 0;
    });
    return rows;
  };

  function ordinal(n) {
    if (n % 100 >= 11 && n % 100 <= 13) return n + 'th';
    return n + (['th', 'st', 'nd', 'rd'][n % 10] || 'th');
  }

  // "Men 3rd of 16 teams (5 events); Women 1st of 12 teams (9 events)".
  function rankText(ranks) {
    var parts = [];
    ranks.forEach(function (row) {
      var unit = row.by === 'club' ? 'dual clubs' : 'teams';
      row.found.forEach(function (f) {
        parts.push(f[0] + ' ' + ordinal(f[2]) + ' of ' + row.of + ' ' + unit + ' (' + plural(f[1], 'event', 'events') + ')');
      });
    });
    return parts.join('; ');
  }

  function ruleActions(score) {
    var box = el('div', 'rule-actions');
    if (!score.fixtures) {
      box.appendChild(el('span', 'desc', 'Not tied to one fixture, so nothing to show in the calendar.'));
      return box;
    }
    var button = el('button', 'lens-link', 'Show in calendar');
    button.type = 'button';
    button.setAttribute('data-rule', score.rule.id);
    return append(box, button);
  }

  // The rule's name with its description on hover. The description is also printed beside the name,
  // so the tooltip is hidden from speech; inside a <summary> the icon is a picture, not a button.
  function ruleNameRow(rule, inSummary) {
    var row = el('span', 'name-row');
    row.appendChild(el('span', 'name', ruleName(rule)));
    var wrap = el('span', 'info-wrap');
    var icon = el(inSummary ? 'span' : 'button', 'info-icon', 'i');
    var tooltip = el('span', 'tooltip', rule.desc);
    tooltip.setAttribute('aria-hidden', 'true');
    if (inSummary) {
      icon.setAttribute('aria-hidden', 'true');
    } else {
      icon.type = 'button';
      icon.setAttribute('aria-label', ruleName(rule) + ' description');
    }
    return append(row, append(wrap, icon, tooltip));
  }

  // A fixtures list mounted under a rule, filled by `toggle` when opened.
  function ruleFixturesList(score) {
    var list = el('details', 'fx-list');
    list.setAttribute('data-rule', score.rule.id);
    list.appendChild(el('summary', '', plural(score.fixtures, 'fixture', 'fixtures') + ', by team'));
    list.appendChild(el('div', 'rule-fixtures'));
    return list;
  }

  // A rule's share of the season's movement as a bar with its signed share as
  // text, drawn the same way the server draws it; hard rules are counted instead.
  function contributionNode(total, share, count) {
    if (share === null) return el('span', 'delta', plural(count, 'violation', 'violations'));
    var polarity = total > 0 ? 'gain' : total < 0 ? 'cost' : 'neutral';
    var track = el('span', 'share-track');
    track.setAttribute('aria-hidden', 'true');
    var fill = el('span', 'share-fill');
    fill.style.width = (Math.round(share * 10) / 10) + '%';
    append(track, fill);
    var sign = total > 0 ? '+' : total < 0 ? '\u2212' : '';
    var text = Math.round(share) === 0 && total !== 0 ? sign + '<1%' : (Math.round(share) === 0 ? '' : sign) + Math.round(share) + '%';
    return append(el('span', 'contrib ' + polarity), track, el('span', 'share-text', text));
  }

  function scopeItem(score, kind) {
    var item = el('details', 'item ' + kind);
    if (score.fixtures) item.setAttribute('data-rule', score.rule.id);
    var summary = el('summary', 'top');
    var names = append(el('span', 'name-block'), ruleNameRow(score.rule, true), el('span', 'rule-desc', score.rule.desc));
    append(summary, names, contributionNode(score.total, score.share, score.count));
    var body = append(el('div', 'item-body'));
    if (score.share !== null) body.appendChild(el('div', 'desc', 'Raw score change ' + signed(score.total) + '.'));
    body.appendChild(ruleActions(score));
    if (score.fixtures) body.appendChild(el('div', 'rule-fixtures'));
    if (score.loose) body.appendChild(el('div', 'desc', 'Includes ' + plural(score.loose, 'event', 'events') + ' not tied to one fixture.'));
    if (score.ranks.length) body.appendChild(el('div', 'desc', 'Rank by events, 1st = most: ' + rankText(score.ranks) + '.'));
    return append(item, summary, body);
  }

  function scopeColumn(title, scores, kind, empty) {
    var col = append(el('div', 'col'), el('h3', '', title));
    if (!scores.length) col.appendChild(el('div', 'desc', empty));
    scores.forEach(function (score) { col.appendChild(scopeItem(score, kind)); });
    return col;
  }

  function contributionCell(score) {
    var cell = el('td', 'num');
    cell.setAttribute('data-label', 'Contribution');
    if (score.share === null) {
      cell.textContent = score.total ? signed(score.total) : '—';
      return cell;
    }
    append(cell, contributionNode(score.total, score.share, score.count), el('div', 'raw', signed(score.total)));
    return cell;
  }

  // The cell's column name, which a phone shows beside its value.
  function labelled(cell, name) {
    cell.setAttribute('data-label', name);
    return cell;
  }

  Option.prototype.scopeBreakdown = function (scores, club) {
    var details = el('details', 'scope-breakdown');
    details.appendChild(el('summary', '', 'Full score breakdown — ' + DATA.clubNames[club] + '’s share'));
    var head = el('tr');
    ['Rule', 'Type', 'Contribution', 'Events', 'Rank', 'Fixtures'].forEach(function (name, i) {
      var cls = i === 2 || i === 3 ? 'num' : i === 1 ? 'col-type' : i >= 4 ? 'col-extra' : '';
      head.appendChild(el('th', cls, name));
    });
    var satisfied = scores.filter(function (score) { return score.rule.hard && !score.count; });
    if (satisfied.length) {
      var collapsed = el('details', 'hard-ok');
      collapsed.appendChild(el('summary', '', satisfied.length + ' hard rule' + (satisfied.length === 1 ? '' : 's') + ' satisfied ✓'));
      collapsed.appendChild(el('p', 'desc', satisfied.map(function (score) { return ruleName(score.rule); }).join(', ')));
      details.appendChild(collapsed);
    }
    var body = el('tbody');
    scores.forEach(function (score) {
      var rule = score.rule;
      if (rule.hard && !score.count) return;
      var row = el('tr');
      if (rule.hard && score.count) row.className = 'violated';
      var last = el('td', 'col-extra');
      last.setAttribute('data-label', 'Fixtures');
      if (rule.inv) last.appendChild(el('span', 'desc', 'Scored per competition, not per team.'));
      else if (score.fixtures) append(last, ruleActions(score), ruleFixturesList(score));
      else last.appendChild(document.createTextNode('—'));
      append(
        row,
        append(el('td'), ruleNameRow(rule), el('div', 'rule-desc phone-desc', rule.desc)),
        labelled(append(el('td', 'col-type'), el('span', 'kind ' + (rule.hard ? 'hard' : 'soft'), rule.hard ? 'hard' : 'soft')), 'Type'),
        contributionCell(score),
        labelled(el('td', 'num', String(score.count)), 'Events'),
        labelled(el('td', 'col-extra', score.ranks.length ? rankText(score.ranks) : '—'), 'Rank'),
        last
      );
      body.appendChild(row);
    });
    var table = append(el('table', 'breakdown'), append(el('thead'), head), body);
    details.appendChild(append(el('div', 'cal-wrap'), table));
    return details;
  };

  // Draws the club's scorecard, or hides it again for the season-wide one. What
  // the filters leave is counted in the banner while a club is picked, and under
  // the bar when a rule filter alone is on.
  Option.prototype.renderTeamUi = function () {
    var club = this.state.club;
    var dual = !!club && isDual(club);
    this.teamFilter.hidden = !dual;
    if (!dual) return;
    if (this.teamSelect.getAttribute('data-club') !== club) {
      this.teamSelect.textContent = '';
      this.teamSelect.setAttribute('data-club', club);
      var all = el('option', '', 'All');
      all.value = '';
      this.teamSelect.appendChild(all);
      teamsOf(club).forEach(function (teamId) {
        var gender = DATA.teams[teamId].gender;
        var option = el('option', '', gender === 'women' ? 'Women' : gender === 'men' ? 'Men' : DATA.teams[teamId].label);
        option.value = teamId;
        this.teamSelect.appendChild(option);
      }, this);
    }
    this.teamSelect.value = this.state.team;
  };

  Option.prototype.renderScope = function () {
    var club = this.state.club;
    var team = this.state.team;
    var ctx = this.context();
    this.panel.querySelectorAll('.season-part').forEach(function (node) { node.hidden = !!club; });
    this.scopeBanner.hidden = !club;
    this.scopeCard.textContent = '';
    if (club) {
      var matches = (this.data.clubHeadlines[club] || [])[0];
      this.scopeText.textContent = '';
      append(
        this.scopeText,
        el('b', '', DATA.clubNames[club] + ' — ' + (matches ? plural(+matches.value, 'match', 'matches') : 'no matches')),
        document.createTextNode('. Upsides, problems and the breakdown below count only this club’s share of each rule, as a share of this club’s own movement. ' +
          'Preferred weekday is scored per competition and left out. The score, the hard-rule list and fairness stay season-wide.')
      );
      if (team) {
        var teamName = this.teamSelect.options[this.teamSelect.selectedIndex].text;
        this.scopeText.appendChild(document.createTextNode(' With ' + teamName + ' picked, the fixtures and timeline narrow to that team; the scorecard figures stay the club’s.'));
      }
      var scores = this.clubScores(club);
      var limit = this.data.limit;
      var ups = scores.filter(function (s) { return s.total > 0; })
        .sort(function (a, b) { return b.total - a.total; }).slice(0, limit);
      var downs = scores.filter(function (s) { return s.total < 0; })
        .sort(function (a, b) { return (a.rule.hard ? 0 : 1) - (b.rule.hard ? 0 : 1) || a.total - b.total; })
        .slice(0, limit);
      var cols = el('div', 'cols');
      cols.appendChild(scopeColumn(this.data.full ? 'Every upside' : 'Biggest upsides', ups, 'up', 'Nothing scored positively for this club.'));
      cols.appendChild(scopeColumn(
        this.data.full ? 'Every problem (including the hard rules above)' : 'Biggest problems',
        downs, 'down', 'Nothing scored negatively for this club.'
      ));
      append(this.scopeCard, cols, this.scopeBreakdown(scores, club));
    }
    var filtering = ctx.filtering;
    var shown = this.fx.filter(ctx.show).length;
    var count = filtering ? shown + ' of ' + this.fx.length + ' fixtures shown' : '';
    this.scopeCount.textContent = club ? count : '';
    this.scopeCount.hidden = !club || !filtering;
    this.lensCount.textContent = club ? '' : count;
    this.lensCount.hidden = !!club || !filtering;
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
    root.querySelectorAll('.cal-day[data-date]').forEach(function (button) {
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
    var kindChip = this.panel.querySelector('.kind-clear');
    var clubName = state.club ? this.clubSelect.options[this.clubSelect.selectedIndex].text : '';
    var lensName = stats ? ruleName(stats.rule) + (state.only ? ' (only)' : '') : '';
    var teamChip = this.panel.querySelector('.team-clear');
    var teamName = state.team ? this.teamSelect.options[this.teamSelect.selectedIndex].text : '';
    var changedKinds = !sameKinds(state.kinds, DEFAULT_KINDS);
    teamChip.hidden = !state.team;
    teamChip.querySelector('.chip-text').textContent = teamName;
    teamChip.setAttribute('aria-label', 'Remove team filter: ' + teamName);
    clubChip.hidden = !state.club;
    lensChip.hidden = !stats;
    kindChip.hidden = !changedKinds;
    clubChip.querySelector('.chip-text').textContent = clubName;
    lensChip.querySelector('.chip-text').textContent = lensName;
    kindChip.querySelector('.chip-text').textContent = 'Shown: ' + kindsText(state.kinds);
    clubChip.setAttribute('aria-label', 'Remove club filter: ' + clubName);
    lensChip.setAttribute('aria-label', 'Clear highlighted rule: ' + lensName);
    kindChip.setAttribute('aria-label', 'Reset competitions shown to ' + kindsText(DEFAULT_KINDS));
    var count = (state.club ? 1 : 0) + (state.team ? 1 : 0) + (stats ? 1 : 0) + (changedKinds ? 1 : 0);
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
    // A team filter can hide a target in a club's list, so it is lifted.
    if (state.team && fx.teams.indexOf(state.team) === -1) {
      state.team = '';
      this.teamSelect.value = '';
      redraw = true;
    }
    if (state.only && fx.rules.indexOf(state.rule) === -1) {
      state.only = false;
      redraw = true;
    }
    // So is a competition the reader has switched off, and a list filter that hides it.
    if (state.kinds.indexOf(fx.kind) === -1) {
      state.kinds = ALL_KINDS.filter(function (kind) { return kind === fx.kind || state.kinds.indexOf(kind) !== -1; });
      redraw = true;
    }
    if (state.query && searchText(fx).indexOf(state.query.trim().toLowerCase()) === -1) {
      state.query = '';
      redraw = true;
    }
    // In the list a cup or European team may sit in a closed round-date chip.
    if (fx.kind !== 'league' && state.view === 'combined-list' && !state.open[groupKey(fx)]) {
      state.open[groupKey(fx)] = true;
      redraw = true;
    }
    if (state.view === 'month-calendar' && state.date !== fx.date) {
      state.date = fx.date;
      redraw = true;
    }
    if (redraw) this.render();
    var node = this.activeBody().querySelector('[data-uid="' + uid + '"]');
    if (!node) return;
    openAround(node);
    node.classList.remove('flash');
    void node.offsetWidth;
    node.classList.add('flash');
    // Focus lands on the card's own control, so its group's tab stop moves with it (#41).
    var control = node.querySelector('[data-rove]');
    if (!control) node.tabIndex = -1;
    (control || node).focus({ preventScroll: true });
    node.scrollIntoView({ block: 'center', behavior: REDUCED_MOTION || restoring ? 'auto' : 'smooth' });
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

  // Whole points keep a cell's figure inside its border; the full figure is in its label.
  function stripNet(net) {
    var whole = Math.round(net);
    return whole > 0 ? '+' + whole : whole < 0 ? '\u2212' + Math.abs(whole) : '0';
  }

  function stripCell(opt, ctx, round) {
    var button = el('button', 'strip-cell');
    button.type = 'button';
    button.tabIndex = -1;
    button.setAttribute('data-rove', '');
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
      button.appendChild(el('span', 'strip-net', stripNet(sum.net)));
    }
    if (sum.broken) {
      var hard = el('span', 'strip-hard', String(sum.broken));
      hard.setAttribute('aria-hidden', 'true');
      button.appendChild(hard);
    }
    var label = round.title + ': ' + summaryText(sum);
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
        openAround(nodes[i]);
        var round = nodes[i].closest('.round');
        this.setTarget(round.id);
        round.scrollIntoView({ block: 'start', behavior: REDUCED_MOTION ? 'auto' : 'smooth' });
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
    // A lens carried over from another option may name a rule this one never scores.
    if (state.rule && !(this.ruleStats()[state.rule] || {}).fixtures) {
      state.rule = '';
      state.only = false;
    }
    var ctx = this.context();
    hideFlagTip();
    this.panel.setAttribute('data-current-view', state.view);
    this.panel.querySelectorAll('.view-btn').forEach(function (button) {
      var active = button.dataset.view === state.view;
      button.classList.toggle('active', active);
      button.setAttribute('aria-checked', String(active));
      button.tabIndex = active ? 0 : -1;
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
    this.renderKinds();
    this.renderHints();
    this.renderTeamUi();
    this.renderScope();
    this.renderLensUi();
    this.markLens(this.activeBody());
    this.renderStrip(ctx);
    this.renderExport();
    refreshRove(this.panel);
    syncHash();
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

  var tabs = document.querySelectorAll('.tab');

  function showOption(index) {
    tabs.forEach(function (other, i) {
      other.setAttribute('aria-selected', String(i === index));
      other.tabIndex = i === index ? 0 : -1;
    });
    var previous = option(current);
    var anchor = jumping ? '' : previous.anchor;
    previous.release();
    panels[current].hidden = true;
    current = index;
    panels[current].hidden = false;
    var next = option(current);
    next.adopt(previous.state);
    next.render();
    if (anchor) {
      next.scrollToDate(anchor);
      next.anchor = anchor;
    }
    watchBar(panels[current]);
  }

  tabs.forEach(function (tab, index) {
    tab.addEventListener('click', function () {
      pushNext = true;
      showOption(index);
    });
  });

  // Tabs and views: the arrow keys move within the group, Home and End go to its ends.
  // The view buttons are radios, so moving selects; a tab opens a whole option, which is
  // heavy and moves the page to the calendar, so moving only focuses it and Enter or Space opens (#41).
  function arrowTarget(buttons, from, key) {
    var last = buttons.length - 1;
    var to = { ArrowRight: from + 1, ArrowDown: from + 1, ArrowLeft: from - 1, ArrowUp: from - 1, Home: 0, End: last }[key];
    if (to === undefined) return null;
    return buttons[to < 0 ? last : to > last ? 0 : to];
  }
  // Leaving the tabs puts the tab stop back on the open one, so Shift+Tab returns to it.
  document.addEventListener('focusout', function (e) {
    var list = e.target.closest ? e.target.closest('[role="tablist"]') : null;
    if (!list || list.contains(e.relatedTarget)) return;
    list.querySelectorAll('[role="tab"]').forEach(function (tab) {
      tab.tabIndex = tab.getAttribute('aria-selected') === 'true' ? 0 : -1;
    });
  });
  document.addEventListener('keydown', function (e) {
    var group = e.target.closest ? e.target.closest('[role="tablist"], [role="radiogroup"]') : null;
    if (!group || e.altKey || e.ctrlKey || e.metaKey) return;
    var buttons = Array.prototype.slice.call(group.querySelectorAll('[role="tab"], [role="radio"]'));
    var target = arrowTarget(buttons, buttons.indexOf(e.target), e.key);
    if (!target) return;
    e.preventDefault();
    if (target.getAttribute('role') === 'tab') {
      buttons.forEach(function (other) { other.tabIndex = other === target ? 0 : -1; });
    }
    target.focus();
    if (target.getAttribute('role') === 'radio') target.click();
  });

  // The anchor is read once scrolling settles, so a scroll up to the tabs does
  // not walk it back to the start of the calendar on the way.
  var anchorTimer = 0;
  window.addEventListener('scroll', function () {
    clearTimeout(anchorTimer);
    anchorTimer = setTimeout(function () {
      var opt = options[current];
      var date = opt && opt.fx ? opt.anchorDate() : '';
      if (date) opt.anchor = date;
    }, 200);
  }, { passive: true });

  // The comparison table's option names open that option's tab at the tabs,
  // not at the reader's calendar position, so only the one scroll happens.
  var jumping = false;
  document.addEventListener('click', function (e) {
    var open = e.target.closest ? e.target.closest('.cmp-open') : null;
    if (!open) return;
    var index = +open.getAttribute('data-option');
    jumping = true;
    document.querySelectorAll('.tab')[index].click();
    jumping = false;
    option(index).showScore();
  });
  // ------------------------------------------------------------ export (#42)
  //
  // The selected option as a file, built here so the page stays self-contained. A club or team
  // filter narrows both files. The CSV is the shape `external_schedule.py` reads back (league
  // fixtures only: it has no cup or European rounds); the calendar holds whatever competitions
  // are shown, with a kickoff time where the league has one and the whole day where it does not.

  var CSV_HEADER = ['competition', 'date', 'home_team', 'away_team', 'venue'];
  var MATCH_MINUTES = 120;

  function exportFixtures(opt, kinds) {
    var state = opt.state;
    return opt.fx.filter(function (fx) {
      return kinds.indexOf(fx.kind) !== -1 && (!state.club || fx.clubs.indexOf(state.club) !== -1) &&
        (!state.team || fx.teams.indexOf(state.team) !== -1);
    }).sort(function (a, b) {
      return a.date < b.date ? -1 : a.date > b.date ? 1 : (a.time < b.time ? -1 : a.time > b.time ? 1 : a.uid - b.uid);
    });
  }

  function csvField(value) {
    return /[",\r\n]/.test(value) ? '"' + value.replace(/"/g, '""') + '"' : value;
  }

  // A venue is written only where it differs from the home team's own ground, as the loader defaults it.
  function csvText(fixtures) {
    var rows = [CSV_HEADER].concat(fixtures.map(function (fx) {
      return [fx.comp, fx.date, fx.teams[0], fx.teams[1], fx.venueId];
    }));
    return rows.map(function (row) { return row.map(csvField).join(','); }).join('\r\n') + '\r\n';
  }

  // Kickoffs are Oslo wall-clock times; the calendar carries them in UTC so no zone definition is needed.
  function lastSundayUtc(year, month) {
    var last = new Date(Date.UTC(year, month + 1, 0));
    return Date.UTC(year, month, last.getUTCDate() - last.getUTCDay(), 1);
  }

  function osloToUtc(iso, time) {
    var wall = Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10), +time.slice(0, 2), +time.slice(3, 5));
    var summer = wall - 2 * 3600000;
    var year = +iso.slice(0, 4);
    return summer >= lastSundayUtc(year, 2) && summer < lastSundayUtc(year, 9) ? summer : wall - 3600000;
  }

  function icsStamp(ms) {
    return new Date(ms).toISOString().replace(/[-:]/g, '').replace(/\.\d{3}/, '');
  }

  function icsDate(ms) { return icsStamp(ms).slice(0, 8); }

  function icsText(value) {
    return value.replace(/\\/g, '\\\\').replace(/;/g, '\\;').replace(/,/g, '\\,').replace(/\r?\n/g, '\\n');
  }

  // Content lines are cut at 75 octets, never inside a character; the next line starts with a space.
  function foldLine(line) {
    var encoder = new TextEncoder();
    var lines = [];
    var current = '';
    var octets = 0;
    Array.from(line).forEach(function (char) {
      var size = encoder.encode(char).length;
      if (octets + size > 75) {
        lines.push(current);
        current = ' ';
        octets = 1;
      }
      current += char;
      octets += size;
    });
    lines.push(current);
    return lines.join('\r\n');
  }

  function icsEvent(fx, uidPrefix, stamp) {
    var comp = DATA.competitions[fx.comp].name;
    var summary = fx.away ? fx.home.full + ' v ' + fx.away.full : fx.home.full + ' ' + versusText(fx);
    var lines = ['BEGIN:VEVENT', 'UID:' + uidPrefix + fx.key + '@terminliste', 'DTSTAMP:' + stamp];
    if (fx.time) {
      var start = osloToUtc(fx.date, fx.time);
      lines.push('DTSTART:' + icsStamp(start), 'DTEND:' + icsStamp(start + MATCH_MINUTES * 60000));
    } else {
      var day = Date.UTC(+fx.date.slice(0, 4), +fx.date.slice(5, 7) - 1, +fx.date.slice(8, 10));
      lines.push('DTSTART;VALUE=DATE:' + icsDate(day), 'DTEND;VALUE=DATE:' + icsDate(day + 86400000));
    }
    lines.push('SUMMARY:' + icsText(summary));
    lines.push('DESCRIPTION:' + icsText(comp + ', ' + (fx.kind === 'league' ? 'round ' + fx.roundNo : fx.round)));
    if (fx.venue) lines.push('LOCATION:' + icsText(fx.venue));
    lines.push('END:VEVENT');
    return lines;
  }

  function icsCalendar(fixtures, name, uidPrefix) {
    var stamp = icsStamp(Date.now());
    var lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//terminliste//season report//EN', 'CALSCALE:GREGORIAN', 'X-WR-CALNAME:' + icsText(name)];
    fixtures.forEach(function (fx) { lines = lines.concat(icsEvent(fx, uidPrefix, stamp)); });
    lines.push('END:VCALENDAR');
    return lines.map(foldLine).join('\r\n') + '\r\n';
  }

  function download(name, type, text) {
    var url = URL.createObjectURL(new Blob([text], { type: type + ';charset=utf-8' }));
    var link = el('a');
    link.href = url;
    link.download = name;
    // Kept from the outside-click handler, which would close the export panel and drop focus.
    link.addEventListener('click', function (e) { e.stopPropagation(); });
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // What the export is of, for the file names and the panel: "Option 2, SK Brann".
  Option.prototype.exportScope = function () {
    var state = this.state;
    var label = this.panel.querySelector('.bar-opt').textContent;
    var club = state.club ? DATA.clubNames[state.club] : '';
    var team = state.team ? DATA.teams[state.team].label : '';
    return { label: label, club: club, team: team, text: [label, team || club].filter(Boolean).join(', ') };
  };

  Option.prototype.exportFileName = function (extension) {
    var state = this.state;
    var slug = [DATA.export.season, 'option-' + (DATA.options.indexOf(this.data) + 1), state.club, state.team]
      .filter(Boolean).join('-');
    return slug + '.' + extension;
  };

  Option.prototype.renderExport = function () {
    var csv = exportFixtures(this, ['league']).length;
    var calendar = exportFixtures(this, this.state.kinds).length;
    var panel = this.panel;
    panel.querySelector('.export-scope').textContent =
      this.exportScope().text + ': ' + plural(csv, 'league fixture', 'league fixtures') + ' in the CSV, ' +
      plural(calendar, 'fixture', 'fixtures') + ' in the calendar.';
    panel.querySelector('.export-csv').disabled = !csv;
    panel.querySelector('.export-ics').disabled = !calendar;
    panel.querySelector('.export-note').textContent = '';
  };

  Option.prototype.exportFile = function (kind) {
    var scope = this.exportScope();
    var name;
    if (kind === 'csv') {
      name = this.exportFileName('csv');
      download(name, 'text/csv', csvText(exportFixtures(this, ['league'])));
    } else {
      name = this.exportFileName('ics');
      var title = DATA.export.season + ' ' + scope.text;
      var uidPrefix = 'option' + (DATA.options.indexOf(this.data) + 1) + '-';
      download(name, 'text/calendar', icsCalendar(exportFixtures(this, this.state.kinds), title, uidPrefix));
    }
    this.panel.querySelector('.export-note').textContent = 'Downloaded ' + name + '.';
  };

  document.addEventListener('click', function (e) {
    var button = e.target.closest ? e.target.closest('.export-csv, .export-ics') : null;
    if (button) optionOf(button).exportFile(button.classList.contains('export-csv') ? 'csv' : 'ics');
  });

  // ------------------------------------------------------- link state (#42)
  //
  // The URL hash holds what a reader would otherwise have to describe: the option, view, club,
  // team, rule lens, competitions shown, list filter, picked day, and the round or fixture last
  // opened, e.g. #option=2&view=combined-list&club=brann&rule=rest_comfort. It is written as the
  // page changes and read on load and on hashchange. Changing the option or the view adds a
  // history entry, so Back steps through those; every other change rewrites the current one.
  // A value that names nothing in this report is dropped and the hash rewritten without it.

  var booted = false;
  var restoring = false;
  var pushNext = false;
  var HASH_TEXT_MAX = 80;

  function unescapeHash(text) {
    try { return decodeURIComponent(text); } catch (e) { return ''; }
  }

  // The hash as {key: value}; null for one that is not ours (#main, an author's own anchor).
  function parseHash(hash) {
    var text = hash.replace(/^#/, '');
    var found = {};
    if (!text) return found;
    if (text.indexOf('=') === -1) {
      if (!/^(fx|rd)-/.test(text)) return null;
      found.target = unescapeHash(text);
      return found;
    }
    text.split('&').forEach(function (pair) {
      var at = pair.indexOf('=');
      if (at > 0) found[unescapeHash(pair.slice(0, at))] = unescapeHash(pair.slice(at + 1));
    });
    return found;
  }

  function sameKinds(a, b) { return a.join() === b.join(); }

  // What a state changes in a link; the target is dropped once anything else moves.
  function stateSignature(state) {
    return [state.view, state.club, state.team, state.rule, state.only, state.kinds.join(), state.query].join('|');
  }

  function hashOf(opt) {
    var state = opt.state;
    var pairs = [['option', current + 1], ['view', state.view]];
    if (state.club) pairs.push(['club', state.club]);
    if (state.team) pairs.push(['team', state.team]);
    if (state.rule) pairs.push(['rule', state.rule]);
    if (state.rule && state.only) pairs.push(['only', '1']);
    if (!sameKinds(state.kinds, DEFAULT_KINDS)) pairs.push(['show', state.kinds.join(',')]);
    if (state.query.trim()) pairs.push(['q', state.query.trim().slice(0, HASH_TEXT_MAX)]);
    if (state.date && state.view === 'month-calendar') pairs.push(['date', state.date]);
    if (opt.target && opt.target.sig === stateSignature(state)) pairs.push(['target', opt.target.id]);
    return '#' + pairs.map(function (pair) {
      return pair[0] + '=' + encodeURIComponent(pair[1]).replace(/%2C/g, ',');
    }).join('&');
  }

  function syncHash() {
    if (!booted || restoring) return;
    var hash = hashOf(option(current));
    var push = pushNext;
    pushNext = false;
    if (hash === location.hash) return;
    // A page opened from a file or a sandbox may refuse to touch its history; the links are then just not kept.
    try { history[push ? 'pushState' : 'replaceState'](null, '', hash); } catch (e) { /* no link state */ }
  }

  Option.prototype.setTarget = function (id) {
    this.target = id ? { id: id, sig: stateSignature(this.state) } : null;
    syncHash();
  };

  // Starts again from the page's defaults and applies whatever `found` holds that is valid here.
  Option.prototype.restore = function (found) {
    var panel = this.panel;
    var previous = this.state;
    var state = this.state = initialState(panel);
    // Folds and open sections are the reader's, not the link's, so Back and Forward keep them.
    state.rounds = previous.rounds;
    state.allRounds = previous.allRounds;
    state.open = previous.open;
    state.closed = previous.closed;
    this.target = null;
    if (found.club && DATA.clubs[found.club] !== undefined) state.club = found.club;
    if (state.club && found.team && DATA.teams[found.team] && DATA.teams[found.team].club === state.club) state.team = found.team;
    if (found.rule && DATA.rules.some(function (rule) { return rule.id === found.rule; })) {
      state.rule = found.rule;
      state.only = found.only === '1';
    }
    var kinds = (found.show || '').split(',').filter(function (kind) { return ALL_KINDS.indexOf(kind) !== -1; });
    if (kinds.length) state.kinds = ALL_KINDS.filter(function (kind) { return kinds.indexOf(kind) !== -1; });
    var views = Array.prototype.map.call(panel.querySelectorAll('.view-btn'), function (button) { return button.dataset.view; });
    // The timeline draws one club, so without one it is not a view to land on.
    if (views.indexOf(found.view) !== -1 && (found.view !== 'club-timeline' || state.club)) state.view = found.view;
    if (/^\d{4}-\d{2}-\d{2}$/.test(found.date || '') && this.data.days.some(function (day) { return day.date === found.date; })) {
      state.date = found.date;
    }
    state.query = (found.q || '').slice(0, HASH_TEXT_MAX);
    this.clubSelect.value = state.club;
  };

  // Brings the linked fixture or round into view: a fixture by `reveal`, a round by opening it
  // (a phone folds them) and scrolling to it. A round of the other round view switches to it.
  Option.prototype.applyTarget = function (id) {
    if (!id) return;
    var state = this.state;
    var fx = this.fx.filter(function (f) { return 'fx-' + f.key === id; })[0];
    if (fx) {
      // A club filter written into the link that hides its own target gives way, as the other filters do.
      if (state.club && fx.clubs.indexOf(state.club) === -1) this.setClub('');
      this.reveal(fx.uid);
      this.setTarget(id);
      return;
    }
    if (id.indexOf('rd-') !== 0) return;
    var wantsWeeks = id.indexOf('rd-week-') === 0;
    var roundView = wantsWeeks ? 'combined-calendar' : 'by-competition';
    if (state.view !== roundView && (state.view === 'by-competition' || state.view === 'combined-calendar')) {
      state.view = roundView;
      this.render();
    }
    var block = this.activeBody().querySelector('[id="' + id + '"]');
    if (!block) return;
    openAround(block);
    this.setTarget(id);
    block.tabIndex = -1;
    block.focus({ preventScroll: true });
    block.scrollIntoView({ block: 'start', behavior: 'auto' });
  };

  function applyHash(e) {
    var found = parseHash(location.hash);
    if (!found) return;
    restoring = true;
    var opt;
    try {
      var wanted = parseInt(found.option, 10);
      var index = wanted >= 1 && wanted <= panels.length ? wanted - 1 : 0;
      if (index !== current) showOption(index);
      opt = option(current);
      opt.restore(found);
      opt.render();
      opt.applyTarget(found.target);
    } finally {
      restoring = false;
    }
    // The redraw can remove the focused control; the view radio is the nearest place to carry on from (#41).
    if (e && e.type === 'hashchange' && (!document.body.contains(document.activeElement) || document.activeElement === document.body ||
        document.activeElement.classList.contains('view-btn'))) {
      var radio = panels[current].querySelector('.view-btn[aria-checked="true"]');
      if (radio) radio.focus({ preventScroll: true });
    }
    // An empty hash is the page as it opens; leave it be rather than write the defaults in.
    if (location.hash) syncHash();
  }

  window.addEventListener('hashchange', applyHash);

  if (panels.length) {
    option(0).render();
    watchBar(panels[0]);
    booted = true;
    if (location.hash) applyHash();
    // Rounds fold only on a phone, so crossing the breakpoint redraws the open view.
    var redrawOnResize = function () { if (options[current]) options[current].render(); };
    if (PHONE.addEventListener) PHONE.addEventListener('change', redrawOnResize);
    else PHONE.addListener(redrawOnResize);
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
    // A mouse has hover and the keyboard has :focus-visible; only a touch needs the toggle. Opening
    // it on a mouse click would leave it stuck once the pointer moved away (#39).
    if (!touch) {
      closeTooltips();
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
  // Delegated: a click on a card opens or closes its detail line. A badge keeps its own click.
  document.addEventListener('click', function (e) {
    var card = e.target.closest ? e.target.closest('.fx, .tl-fx') : null;
    if (card && !e.target.closest('.flag, .fx-detail')) toggleDetail(card);
  });
  // The page prints what is on screen: the drawn view's collapsed sections are opened for the print and shut again after (#39).
  var openedForPrint = [];
  window.addEventListener('beforeprint', function () {
    document.querySelectorAll('.panel:not([hidden]) .pane-body details:not([open])').forEach(function (node) {
      node.open = true;
      openedForPrint.push(node);
    });
  });
  window.addEventListener('afterprint', function () {
    openedForPrint.forEach(function (node) { node.open = false; });
    openedForPrint = [];
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
    button.tabIndex = -1;
    button.setAttribute('data-rove', '');
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

  // Every flag item of a rule across the option, as {fx, flag, item}.
  Option.prototype.allRuleItems = function (ruleId) {
    var out = [];
    this.fx.forEach(function (fx) {
      fx.flags.forEach(function (flag) {
        if (flag.rule.id !== ruleId) return;
        flag.items.forEach(function (item) { out.push({ fx: fx, flag: flag, item: item }); });
      });
    });
    return out;
  };

  // Lists a rule's fixtures grouped by team (or dual club), biggest group first,
  // each group headed by its count. With a club selected, only its share.
  Option.prototype.fillRuleList = function (mount, ruleId) {
    var club = this.state.club;
    var stats = this.ruleStats()[ruleId];
    var entries = club ? this.clubItems(club)[ruleId] || [] : this.allRuleItems(ruleId);
    var groups = {};
    entries.forEach(function (entry) {
      (groups[entry.item.group] || (groups[entry.item.group] = [])).push(entry);
    });
    var names = Object.keys(groups).sort(function (a, b) {
      return groups[b].length - groups[a].length || (a < b ? -1 : 1);
    });
    var total = entries.length;
    var box = roveGroup(el('div', 'occ-groups'));
    var wording = statsWording(stats);
    if (club) {
      var fixtures = {};
      entries.forEach(function (entry) { fixtures[entry.fx.uid] = true; });
      wording = DATA.clubNames[club] + ': ' + plural(Object.keys(fixtures).length, 'fixture', 'fixtures') + ' with this rule';
    }
    var summary = el('p', 'occ-summary', wording + '. Select a row to find it in the calendar.');
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
    refreshRove(mount);
  };

  // One rule's per-team counts as bars, scaled to the largest count.
  function fairnessRow(row) {
    var rule = DATA.rules[row[0]];
    var entries = row[2];
    var max = entries.length ? entries[0][1] : 0;
    var min = entries.length ? entries[entries.length - 1][1] : 0;
    var head = append(el('div', 'head'), el('span', 'fname', rule.name));
    if (row[1]) head.appendChild(el('span', 'flag', '\u26a0 uneven \u2014 ' + min + '\u2013' + max + ' across entries'));
    var bars = el('div', 'fairness-bars');
    entries.forEach(function (entry) {
      var label = el('span', 'flabel', entry[0]);
      label.title = entry[0];
      var fill = el('span', 'ffill');
      fill.style.width = (max ? Math.round(1000 * entry[1] / max) / 10 : 0) + '%';
      append(bars, append(el('div', 'fairness-bar'), label, append(el('span', 'ftrack'), fill), el('span', 'fcount', String(entry[1]))));
    });
    return append(el('div', 'fairness-row' + (row[1] ? ' flagged' : '')), head, el('div', 'desc', rule.desc), bars);
  }

  // The fairness section is drawn the first time it is opened (#42).
  function fillFairness(details) {
    var mount = details.querySelector('.fairness-rows');
    if (!mount || mount.firstChild) return;
    optionOf(details).data.fairness.forEach(function (row) { mount.appendChild(fairnessRow(row)); });
  }

  // `toggle` does not bubble, hence the capture phase.
  document.addEventListener('toggle', function (e) {
    var details = e.target;
    if (details.open && details.classList && details.classList.contains('fairness')) fillFairness(details);
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
    var button = wrap.querySelector('.legend-toggle, .export-toggle');
    wrap.querySelector('.legend-panel, .export-panel').hidden = !open;
    button.setAttribute('aria-expanded', String(open));
  }
  document.addEventListener('click', function (e) {
    var target = e.target;
    var toggle = target.closest ? target.closest('.legend-toggle, .export-toggle, .filters-toggle') : null;
    document.querySelectorAll('.legend-wrap').forEach(function (wrap) {
      if (!toggle || !wrap.contains(toggle)) {
        if (!wrap.contains(target)) setLegend(wrap, false);
      }
    });
    if (!toggle) return;
    if (!toggle.classList.contains('filters-toggle')) {
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
      var button = wrap.querySelector('.legend-toggle, .export-toggle');
      if (button.getAttribute('aria-expanded') !== 'true') return;
      setLegend(wrap, false);
      button.focus();
    });
  });
})();
