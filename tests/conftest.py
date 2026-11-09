from pathlib import Path

import pytest

from terminliste.model.loader import load_world
from terminliste.model.travel import ApiTravelModel

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = PROJECT_ROOT / "data"


@pytest.fixture(scope="session")
def world():
    return load_world(DATA_ROOT)


@pytest.fixture(scope="session")
def season(world):
    return world.season("2026")


@pytest.fixture(scope="session")
def competitions(world, season):
    return [world.competition(c) for c in season.competitions]


@pytest.fixture(scope="session")
def travel(world):
    return ApiTravelModel(world)
