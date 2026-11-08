"""NBA EDGE · DraftKings (paper mode) shared pieces: scoring, roster rules, optimizer.

DraftKings NBA classic scoring:
  point +1, made three +0.5, rebound +1.25, assist +1.5, steal +2, block +2, turnover -0.5,
  double-double +1.5, triple-double +3 (a triple-double also earns the double-double bonus).
Roster: PG, SG, SF, PF, C, G (PG/SG), F (SF/PF), UTIL; $50,000 cap; players from 2+ games.
"""
import json
import pathlib

import highspy

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
DK = DATA / 'dk'
THREES = DK / 'threes.json'
CAP = 50000
SLOTS = ['PG', 'SG', 'SF', 'PF', 'C', 'G', 'F', 'UTIL']
SLOT_OK = {'PG': {'PG'}, 'SG': {'SG'}, 'SF': {'SF'}, 'PF': {'PF'}, 'C': {'C'},
           'G': {'PG', 'SG'}, 'F': {'SF', 'PF'}, 'UTIL': {'PG', 'SG', 'SF', 'PF', 'C'}}
LEAGUE_RATIO_DEFAULT = 1.0   # DK/FD points, used only until threes are in the box scores
SHRINK_FD_POINTS = 120       # prior weight (in FanDuel points) pulling each player's ratio to the league's


def dk_points(r):
    """DraftKings points for one box-score row, or None if threes made are unknown."""
    if r.get('fg3m') is None:
        return None
    cats = [r.get(k, 0) or 0 for k in ('pts', 'reb', 'ast', 'stl', 'blk')]
    tens = sum(1 for c in cats if c >= 10)
    bonus = (1.5 if tens >= 2 else 0) + (3 if tens >= 3 else 0)
    return (r.get('pts', 0) + 0.5 * r['fg3m'] + 1.25 * r.get('reb', 0) + 1.5 * r.get('ast', 0)
            + 2 * r.get('stl', 0) + 2 * r.get('blk', 0) - 0.5 * r.get('tov', 0) + bonus)


def fd_points(r):
    return r.get('pts', 0) + 1.2 * r.get('reb', 0) + 1.5 * r.get('ast', 0) + 3 * r.get('stl', 0) + 3 * r.get('blk', 0) - r.get('tov', 0)


def ratio_model(logs, key):
    """Per-player DK/FD points ratio from this season's box scores, shrunk toward the league ratio.
    `key(row)` gives the player key. Returns (league_ratio, {key: ratio}, rows_used)."""
    tot_dk = tot_fd = 0.0
    per = {}
    used = 0
    for r in logs:
        dk = dk_points(r)
        fd = fd_points(r)
        if dk is None or (r.get('minutes') or 0) <= 0:
            continue
        used += 1
        tot_dk += dk
        tot_fd += fd
        a = per.setdefault(key(r), [0.0, 0.0])
        a[0] += dk
        a[1] += fd
    league = tot_dk / tot_fd if tot_fd > 0 and used >= 200 else LEAGUE_RATIO_DEFAULT
    ratios = {k: (d + SHRINK_FD_POINTS * league) / (f + SHRINK_FD_POINTS) for k, (d, f) in per.items() if f > -SHRINK_FD_POINTS / 2}
    return league, ratios, used


def positions(p):
    return {x for x in str(p.get('position', '')).split('/') if x}


def optimize(pool, score, exclude_sets=(), min_unique=3):
    """Best DK classic lineup for `score(player)`. pool: dicts with position, salary, team, game.
    exclude_sets: earlier lineups (sets of pool indexes) a new one must differ from by >= min_unique.
    Returns [(pool_index, slot)] or []."""
    h = highspy.Highs()
    h.silent()
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    cols = {}
    for i, p in enumerate(pool):
        pos = positions(p)
        for s in SLOTS:
            if pos & SLOT_OK[s]:
                cols[(i, s)] = len(cols)
                h.addCol(float(score(p)), 0.0, 1.0, 0, [], [])
    n = len(cols)
    if not n:
        return []
    h.changeColsIntegrality(n, list(range(n)), [highspy.HighsVarType.kInteger] * n)

    def row(idx, lo, hi, vals=None):
        if idx:
            h.addRow(lo, hi, len(idx), idx, vals or [1.0] * len(idx))
    for s in SLOTS:
        idx = [c for (i, t), c in cols.items() if t == s]
        if not idx:
            return []
        row(idx, 1, 1)
    by_player = {}
    for (i, s), c in cols.items():
        by_player.setdefault(i, []).append(c)
    for idx in by_player.values():
        row(idx, 0, 1)
    ids = list(cols.values())
    row(ids, 0, CAP, [float(pool[i]['salary']) for (i, s) in cols])
    # at least two games: one binary per game, y_g <= players from game g, sum y_g >= 2
    games = sorted({p.get('game') for p in pool})
    if len(games) >= 2:
        start = n
        for _ in games:
            h.addCol(0.0, 0.0, 1.0, 0, [], [])
        h.changeColsIntegrality(len(games), list(range(start, start + len(games))), [highspy.HighsVarType.kInteger] * len(games))
        for k, g in enumerate(games):
            idx = [c for (i, s), c in cols.items() if pool[i].get('game') == g]
            h.addRow(0.0, highspy.kHighsInf, len(idx) + 1, idx + [start + k], [1.0] * len(idx) + [-1.0])
        h.addRow(2.0, highspy.kHighsInf, len(games), list(range(start, start + len(games))), [1.0] * len(games))
    for prev in exclude_sets:
        idx = [c for (i, s), c in cols.items() if i in prev]
        row(idx, 0, len(SLOTS) - min_unique)
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return []
    x = h.getSolution().col_value
    return [(i, s) for (i, s), c in cols.items() if x[c] > 0.5]


def load(path, default=None):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_logs():
    """This season's box-score rows, with threes made filled in from data/dk/threes.json where the
    stored row predates threes being recorded."""
    logs = (load(DATA / 'season_logs.json', {}) or {}).get('game_logs', [])
    threes = load(THREES, {}) or {}
    for r in logs:
        if r.get('fg3m') is None:
            v = threes.get(str(r.get('event_id')), {}).get(str(r.get('id')))
            if v is not None:
                r['fg3m'] = v
    return logs
