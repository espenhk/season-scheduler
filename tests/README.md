# Test layout

`tests/` mirrors `terminliste/`'s package structure so a change to a given
source path maps to one or a few `tests/<area>/` folders — the point being
to always run what could plausibly break, not the whole suite on every edit
(issue #104). The full count varies by environment — `tests/solvers/`
collects extra cases when the optional `ortools` dependency is installed
(`poetry install --with football-scheduler-cpsat`) — so nothing here pins
an exact number.

```
tests/
├── conftest.py       # session-scoped world/season/travel fixtures — applies to every subfolder
├── factories.py       # small hand-built worlds for constraint unit tests
├── model/              # Venue/Team/Club/Competition/Season schema, calendar, travel model
├── rounds/              # round-robin pairing, cup/European resolution, kickoff assignment
├── scoring/              # the hard/soft constraint framework and its scoring math
├── solvers/               # the local-search and CP-SAT backends, and the contract both must satisfy
├── report/                 # the Jinja2 → HTML report renderer
├── refdata/                  # the optional reference-data and travel-time refresh/cache layer
├── scripts/                    # scripts/fetch_real_schedule.py's CSV conversion
└── integration/                 # whole-pipeline tests: baselines, external schedules, generate+score
```

Run one area in isolation, e.g. `poetry run python -m pytest tests/scoring/`.
Every folder passes standalone — none of them depend on another folder's
tests having run first.

## Which folders to run for a given change

| changed path | run |
|---|---|
| `terminliste/model/**` | **all** |
| `terminliste/rounds/cup_schedule.py` | **all** |
| `terminliste/rounds/european_schedule.py` | **all** |
| `terminliste/rounds/round_robin.py`, `terminliste/rounds/kickoff.py` | `rounds`, `solvers`, `integration` |
| `terminliste/scoring/**` | `scoring`, `solvers`, `report`, `integration` |
| `terminliste/solvers/**` | `solvers`, `integration` |
| `terminliste/report/**` | `report` |
| `terminliste/refdata/**` | `refdata`, `scripts` |
| `terminliste/external_schedule.py`, `terminliste/baseline.py` | `integration` |
| `scripts/**` | `scripts`, `integration` (`refresh_baselines.py --check` is asserted by `tests/integration/test_baselines.py`) |
| `cli.py` | `integration` — nothing under `tests/` imports `cli.py` directly; this is a placeholder so the path isn't silently untested, not real coverage |
| `data/**` | **all** |
| `baselines/**` | `integration` |
| `tests/conftest.py`, `tests/factories.py`, `football-scheduler/conftest.py` | **all** |
| `pyproject.toml`, `poetry.lock` | **all** |

`model/schema.py`, `model/loader.py`, `data/**`, and `tests/conftest.py`/
`tests/factories.py` are genuinely global — nearly every module and every
test folder sits downstream of them, so changes there should just run
everything. `rounds/cup_schedule.py` and `rounds/european_schedule.py` are
near-global too: they're pulled in by scoring, solvers, and rendering, plus
`tests/model/test_loader.py`, so anything narrower than "run all" for those
would be dishonest about what they touch.

If `scoring/**` or `rounds/round_robin.py`/`kickoff.py` turn out too noisy
in practice (they're the two largest non-"all" buckets), two follow-ups are
cheap:
- drop `report` from the `scoring/**` row — `test_render.py` only imports
  `scoring.soft` to build violation objects for display, so the coupling is
  thin;
- split `tests/rounds/` into `rounds/` (round_robin + kickoff) and
  `rounds/competitions/` (cup + european), since the latter two are in the
  "run all" tier anyway.

## A note on `import factories`

Every test file that needs a small hand-built world does `import factories
as f` — a bare, non-package import. This still resolves correctly from a
subfolder without needing `tests/__init__.py` or a `from tests.factories
import ...` rewrite: pytest inserts the "basedir" of every `conftest.py` it
loads onto `sys.path`, and `tests/conftest.py`'s basedir is `tests/`
itself — so `tests/` ends up on `sys.path` regardless of which subfolder a
test file lives in.
