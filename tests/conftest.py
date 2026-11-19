import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from svitlo.source import Reading  # noqa: E402

KYIV = ZoneInfo("Europe/Kyiv")
STUB_WIDGET = Path(__file__).resolve().parent / "stub_widget.html"


def reading(ts, grid=True, soc=80, batt_w=0, load_w=400, grid_v=None, capacity_ah=200, cutoff=10):
    if grid_v is None:
        grid_v = 230.0 if grid else 4.7
    return Reading(ts=ts, grid_v=grid_v, grid_w=0, load_w=load_w, batt_temp_c=22.0, batt_v=53.0, soc=soc,
                   batt_w=batt_w, batt_a=0.0, grid_relay=1 if grid else 0, capacity_ah=capacity_ah,
                   cutoff_soc=cutoff, max_charge_a=100)


@pytest.fixture
def tz():
    return KYIV
