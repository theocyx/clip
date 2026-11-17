from datetime import datetime

from app.web.main import _local_time


def test_local_time_converts_utc_to_paris():
    assert _local_time(datetime(2026, 11, 17, 14, 36)) == "17/11 15:36"
    assert _local_time(datetime(2026, 7, 1, 10, 0)) == "01/07 12:00"
    assert _local_time(None) == ""
