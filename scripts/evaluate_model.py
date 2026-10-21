#!/usr/bin/env python3
"""Walk-forward evaluation of the observed-box-score baseline; no future-data leakage.
This does not train slate-specific projections, injuries, or ownership.
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'data/model_evaluation.json'


def read_logs(path):
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    return data.get('game_logs', [])


def walk_forward(logs):
    # The actual current-season game cannot be used to predict itself.
    history = defaultdict(list)
    examples = []
    dedup = {}
    for row in logs:
        ident = str(row.get('id') or row.get('name') or '').strip()
        date = str(row.get('date') or '')
        event = str(row.get('event_id') or '')
        if ident and date and event:
            dedup[(ident, date, event)] = row
    by_day = defaultdict(list)
    for (_, day, _), row in dedup.items():
        by_day[day].append(row)
    for day in sorted(by_day):
        today = []
        for r in by_day[day]:
            player = str(r.get('id') or r.get('name'))
            minutes = float(r.get('minutes') or 0)
            fp = float(r.get('fantasy_points') or 0)
            if minutes <= 0 or not math.isfinite(minutes) or not math.isfinite(fp):
                continue
            prior = history[player]
            if len(prior) >= 5:
                recent = prior[-10:]
                # Past observed minutes and past observed fantasy points per minute.
                expected_minutes = mean(x[0] for x in recent)
                past_fppm = sum(x[1] for x in recent) / sum(x[0] for x in recent)
                predicted = expected_minutes * past_fppm
                if 0 < predicted <= 150:
                    today.append((day, predicted, fp))
        examples.extend(today)
        # All performances from this day are appended only AFTER evaluating the day.
        for r in by_day[day]:
            player = str(r.get('id') or r.get('name'))
            minutes = float(r.get('minutes') or 0)
            fp = float(r.get('fantasy_points') or 0)
            if 0 < minutes < 65 and math.isfinite(fp):
                history[player].append((minutes, fp))
    return examples


def mae(records, factor):
    return mean(abs(pred * factor - actual) for _, pred, actual in records)


def main():
    logs = read_logs(ROOT / 'data/last_season_stats.json') + read_logs(ROOT / 'data/player_stats.json')
    examples = walk_forward(logs)
    # Chronological train/holdout split (no random shuffling of future games).
    cut = int(len(examples) * .75)
    training, holdout = examples[:cut], examples[cut:]
    factor = 1.0
    enabled = False
    if len(training) >= 200 and len(holdout) >= 75:
        # Robust small calibration grid; control magnitude to avoid over-fitting.
        candidate = min((0.95 + i * .005 for i in range(21)), key=lambda f: mae(training, f))
        if mae(holdout, candidate) < mae(holdout, 1.0) * 0.995:
            factor, enabled = round(candidate, 3), True
    output = {
        'schema': 1, 'generated_at': datetime.now(timezone.utc).isoformat(),
        'source': 'Walk-forward evaluation of observed ESPN box-score history',
        'training_games': len(training), 'holdout_games': len(holdout),
        'holdout_mae': round(mae(holdout, factor), 3) if holdout else None,
        'uncalibrated_holdout_mae': round(mae(holdout, 1.0), 3) if holdout else None,
        'calibration_factor': factor, 'calibration_active': enabled,
        'warning': 'Applies only a bounded baseline scaling factor; does not train injury, usage, DvP, or ownership effects.'
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(output, indent=2) + '\n', encoding='utf-8')
    print(f"Evaluated {len(examples)} player-games; factor {factor}; enabled={enabled}")


if __name__ == '__main__':
    main()
