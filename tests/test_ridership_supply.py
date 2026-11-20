"""Tests for the ridership supply ("客流供数") feature.

Covers the feature flag, supply-only validation that never blocks the classic
publication contract, per-travel-date merging across issue days, revision
replacement / withdrawal, manifest contents, atomic rollback on supply failure,
and one-snapshot packaging.
"""

import io
import zipfile

from conftest import preview_and_confirm, upload
from railapp import db

FULL_HEADER = (
    "ticket_id,passenger_id,train_no,amount,travel_date,"
    "depart_station,arrive_station,passenger_count\n"
)


def make_csv(rows, header=FULL_HEADER):
    return (header + "".join(rows)).encode("utf-8")


def issue_row(ticket, train, travel_day, dep, arr, count, amount=50):
    return f"{ticket},Px,{train},{amount},{travel_day},{dep},{arr},{count}\n"


def enable(client):
    response = client.post("/api/ridership-supply/enable",
                           headers={"Accept": "application/json"})
    assert response.status_code == 200
    return response.get_json()


def publish_issue_day(client, issue_day, rows):
    return preview_and_confirm(client, day=issue_day, data=make_csv(rows))


def get_zip(client, start, end):
    response = client.get(f"/ridership-supply.zip?start={start}&end={end}")
    assert response.status_code == 200, response.data
    return response, zipfile.ZipFile(io.BytesIO(response.data))


def read_member(zf, name):
    return zf.read(name).decode("utf-8")


# ---------------------------------------------------------------------------
# feature flag
# ---------------------------------------------------------------------------
def test_feature_disabled_by_default(client):
    status = client.get("/api/ridership-supply").get_json()
    assert status == {"enabled": False, "enabled_at": None}


def test_enable_is_idempotent_and_keeps_cutoff(client):
    first = enable(client)
    second = enable(client)
    assert first["enabled"] is True and second["enabled"] is True
    assert first["enabled_at"] == second["enabled_at"]


def test_batch_published_before_enablement_is_never_supply(client):
    publish_issue_day(client, "2026-11-10",
                      [issue_row("T1", "G1", "2026-11-01", "A", "B", 3)])
    enable(client)
    # A later, eligible batch.
    publish_issue_day(client, "2026-11-11",
                      [issue_row("T2", "G2", "2026-11-01", "C", "D", 4)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    assert "G1" not in daily          # pre-enablement batch excluded
    assert "G2" in daily
    exclusions = read_member(zf, "manifest-exclusions.csv")
    assert "2026-11-10" in exclusions
    assert "feature_disabled" in exclusions


def test_disabled_feature_export_is_header_only_and_explained(client):
    publish_issue_day(client, "2026-11-10",
                      [issue_row("T1", "G1", "2026-11-01", "A", "B", 3)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    assert daily.strip() == (
        "train_no,depart_station,arrive_station,travel_date,passenger_count"
    )
    assert "feature_disabled" in read_member(zf, "manifest-exclusions.csv")
    assert "feature_enabled,no" in read_member(zf, "MANIFEST.txt")


# ---------------------------------------------------------------------------
# fixed five-column supply schema
# ---------------------------------------------------------------------------
def test_zip_has_one_csv_per_day_with_fixed_header_even_when_empty(client):
    enable(client)
    response, zf = get_zip(client, "2026-11-01", "2026-11-03")
    names = zf.namelist()
    assert names[:3] == [
        "ridership-2026-11-01.csv",
        "ridership-2026-11-02.csv",
        "ridership-2026-11-03.csv",
    ]
    assert "manifest-sources.csv" in names
    assert "manifest-exclusions.csv" in names
    header = "train_no,depart_station,arrive_station,travel_date,passenger_count"
    for day in ("2026-11-01", "2026-11-02", "2026-11-03"):
        assert read_member(zf, f"ridership-{day}.csv").strip() == header
    assert int(response.headers["Content-Length"]) == len(response.data)
    assert response.headers["Cache-Control"] == "no-store"


def test_rows_from_multiple_issue_days_merge_for_one_travel_date(client):
    enable(client)
    publish_issue_day(client, "2026-11-11", [
        issue_row("T1", "G101", "2026-11-01", "北京南", "上海虹桥", 2),
        issue_row("T2", "G101", "2026-11-02", "北京南", "济南西", 1),
    ])
    publish_issue_day(client, "2026-11-12", [
        issue_row("T3", "G22", "2026-11-01", "天津", "北京南", 7),
    ])
    _, zf = get_zip(client, "2026-11-01", "2026-11-02")
    day1 = read_member(zf, "ridership-2026-11-01.csv").splitlines()
    assert day1[0] == (
        "train_no,depart_station,arrive_station,travel_date,passenger_count"
    )
    # Deterministic ordering: source issue day, then source row order.
    assert day1[1].startswith("G101,北京南,上海虹桥,2026-11-01,2")
    assert day1[2].startswith("G22,天津,北京南,2026-11-01,7")
    sources = read_member(zf, "manifest-sources.csv")
    # The day-1 file lists both source batches with issue day and formal rev.
    assert "ridership-2026-11-01.csv,2026-11-01,1,2026-11-11,1," in sources
    assert "ridership-2026-11-01.csv,2026-11-01,1,2026-11-12,1," in sources
    # The day-2 file is fed by only one batch.
    assert "ridership-2026-11-02.csv,2026-11-02,1,2026-11-11,1," in sources


def test_invalid_travel_date_batch_publishes_but_is_excluded(client):
    enable(client)
    _, confirmed_response = publish_issue_day(
        client, "2026-11-13", [issue_row("T1", "G1", "2026-13-99", "A", "B", 1)]
    )
    # Classic publication still happened.
    assert confirmed_response.get_json()["rev"] == 1
    detail = client.get("/api/days/2026-11-13").get_json()
    supply = detail["revisions"][0]["ridership_supply"]
    assert supply["eligible"] is False
    assert supply["reason_code"] == "invalid_supply_rows"
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    exclusions = read_member(zf, "manifest-exclusions.csv")
    assert "2026-11-13" in exclusions and "invalid_supply_rows" in exclusions
    assert "G1" not in read_member(zf, "ridership-2026-11-01.csv")


def test_non_integer_and_negative_counts_are_rejected_for_supply(client):
    enable(client)
    # Editable ridership contract treats the count as text so publishing works.
    client.put("/api/contracts/ridership", json={"columns": [
        {"name": "train_no", "type": "text"},
        {"name": "travel_date", "type": "text"},
        {"name": "passenger_count", "type": "text"},
    ]})
    for bad in ("1.5", "-2", "", "abc"):
        preview, confirmed = preview_and_confirm(
            client,
            day="2026-11-14",
            data=make_csv([issue_row("T1", "G1", "2026-11-01", "A", "B", bad)]),
        )
        assert confirmed.status_code == 201, confirmed.get_json()
        reg = client.get("/api/days/2026-11-14").get_json()
        assert reg["revisions"][0]["ridership_supply"]["eligible"] is False


def test_zero_count_and_canonical_integers_are_accepted(client):
    enable(client)
    publish_issue_day(client, "2026-11-11", [
        issue_row("T1", "G1", "2026-11-01", "A", "B", 0),
        issue_row("T2", "G2", "2026-11-01", "C", "D", "007"),
    ])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    body = read_member(zf, "ridership-2026-11-01.csv").splitlines()
    assert body[1].endswith(",0")
    assert body[2].endswith(",7")     # canonicalised, no leading zeros


def test_missing_supply_columns_excludes_but_keeps_original_contract(client):
    enable(client)
    client.put("/api/contracts/settlement",
               json={"columns": [{"name": "ticket_id", "type": "text"}]})
    client.put("/api/contracts/ridership", json={"columns": [
        {"name": "train_no", "type": "text"},
        {"name": "travel_date", "type": "text"},
        {"name": "passenger_count", "type": "integer"},
    ]})
    raw = b"ticket_id,train_no,travel_date,passenger_count\nT8,G8,2026-11-01,1\n"
    response = upload(client, day="2026-11-15", data=raw)
    assert response.status_code == 201
    pid = response.get_json()["preview_id"]
    confirmed = client.post(f"/api/previews/{pid}/confirm",
                            headers={"Accept": "application/json"})
    assert confirmed.status_code == 201
    reg = client.get("/api/days/2026-11-15").get_json()
    supply = reg["revisions"][0]["ridership_supply"]
    assert supply["eligible"] is False
    assert supply["reason_code"] == "missing_supply_columns"
    # The ridership download under the original contract still works.
    classic = client.get("/download/2026-11-15/1/ridership")
    assert classic.status_code == 200
    assert classic.data.decode().splitlines()[0] == "train_no,travel_date,passenger_count"


def test_preview_reports_supply_validation(client):
    enable(client)
    response = upload(client, data=make_csv([
        issue_row("T1", "G1", "2026-11-01", "A", "B", 1)
    ]))
    payload = response.get_json()
    supply = payload["ridership_supply"]
    assert supply["feature"]["enabled"] is True
    assert supply["validation"]["eligible"] is True
    assert supply["validation"]["columns"] == [
        "train_no", "depart_station", "arrive_station", "travel_date",
        "passenger_count",
    ]
    assert supply["validation"]["row_count"] == 1


# ---------------------------------------------------------------------------
# revision replacement and withdrawal
# ---------------------------------------------------------------------------
def test_new_revision_replaces_old_supply_details_on_same_issue_day(client):
    enable(client)
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T1", "G-OLD", "2026-11-01", "A", "B", 7)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    assert "G-OLD" in read_member(zf, "ridership-2026-11-01.csv")

    # Revision 2 changes the detail for the same issue day.
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T2", "G-NEW", "2026-11-01", "A", "B", 9)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    assert "G-OLD" not in daily
    assert "G-NEW" in daily


def test_ineligible_new_revision_still_withdraws_old_details(client):
    enable(client)
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T1", "G-OLD", "2026-11-01", "A", "B", 7)])
    # New revision fails supply validation (bad calendar date).
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T2", "G-NEW", "2026-02-30", "C", "D", 1)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    assert "G-OLD" not in daily and "G-NEW" not in daily
    exclusions = read_member(zf, "manifest-exclusions.csv")
    assert "2026-11-12,2" in exclusions and "invalid_supply_rows" in exclusions
    # The official publication itself advanced normally.
    day = client.get("/api/days/2026-11-12").get_json()
    assert day["current_rev"] == 2


def test_supply_write_failure_rolls_back_official_revision(client, app, monkeypatch):
    enable(client)
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T1", "G1", "2026-11-01", "A", "B", 1)])

    # Force the supply registration write inside confirm to fail.  Because it
    # runs in the same BEGIN IMMEDIATE transaction, the whole publication must
    # roll back: official rev and supply details both stay as they were.
    def boom(*args, **kwargs):
        raise OSError("simulated supply write failure")

    monkeypatch.setattr(db, "insert_supply_rows", boom)
    response = upload(
        client,
        day="2026-11-12",
        data=make_csv([issue_row("T9", "G9", "2026-11-01", "Z", "Z", 2)]),
    )
    confirm = client.post(
        f"/api/previews/{response.get_json()['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    assert confirm.status_code == 500
    day = client.get("/api/days/2026-11-12").get_json()
    assert day["current_rev"] == 1
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    assert "G1" in daily and "G9" not in daily


# ---------------------------------------------------------------------------
# packaging rules
# ---------------------------------------------------------------------------
def test_range_must_be_one_to_seven_consecutive_days(client):
    enable(client)
    base = "/api/ridership-supply/plan"
    assert client.get(f"{base}?start=2026-11-01&end=2026-11-08").status_code == 400
    assert client.get(f"{base}?start=2026-11-03&end=2026-11-01").status_code == 400
    assert client.get(f"{base}?start=2026-02-30&end=2026-11-01").status_code == 400
    assert client.get(f"{base}?start=20261101&end=2026-11-01").status_code == 400
    ok = client.get(f"{base}?start=2026-11-01&end=2026-11-07")
    assert ok.status_code == 200
    assert len(ok.get_json()["travel_days"]) == 7


def test_zip_and_plan_share_one_snapshot_and_manifest_matches_files(client):
    enable(client)
    publish_issue_day(client, "2026-11-11", [
        issue_row("T1", "G1", "2026-11-01", "A", "B", 2),
        issue_row("T2", "G2", "2026-11-02", "C", "D", 4),
    ])
    plan = client.get(
        "/api/ridership-supply/plan?start=2026-11-01&end=2026-11-02"
    ).get_json()
    response, zf = get_zip(client, "2026-11-01", "2026-11-02")
    # The snapshot timestamp is advertised on the ZIP response.
    assert response.headers["X-Supply-Snapshot-At"] == plan["generated_at"]
    # Row counts in the plan equal actual CSV data rows in the ZIP.
    for item in plan["files"]:
        csv_text = read_member(zf, item["file"])
        data_rows = max(0, len(csv_text.splitlines()) - 1)
        assert data_rows == item["row_count"]
    # Manifest bytes in the ZIP equal the plan's manifest rows (CSV is text).
    import csv as csvmod
    sources = list(csvmod.reader(
        io.StringIO(read_member(zf, "manifest-sources.csv"))))
    assert sources[1:] == [[str(value) for value in row]
                           for row in plan["manifest_sources"]]
    exclusions = list(csvmod.reader(
        io.StringIO(read_member(zf, "manifest-exclusions.csv"))))
    assert exclusions[1:] == [[str(value) for value in row]
                              for row in plan["manifest_exclusions"]]


def test_snapshot_read_is_stable_while_a_new_batch_confirms(app, client):
    """Packaging reads registered data only; a mid-packaging confirm cannot
    make the CSVs and the manifest disagree."""
    enable(client)
    publish_issue_day(client, "2026-11-11",
                      [issue_row("T1", "G1", "2026-11-01", "A", "B", 2)])
    # Take a plan snapshot, then publish more data, then take the ZIP for the
    # same window.  Each artifact is internally consistent; the ZIP simply
    # reflects whichever snapshot it read first.
    plan_before = client.get(
        "/api/ridership-supply/plan?start=2026-11-01&end=2026-11-01"
    ).get_json()
    publish_issue_day(client, "2026-11-12",
                      [issue_row("T2", "G2", "2026-11-01", "C", "D", 5)])
    _, zf = get_zip(client, "2026-11-01", "2026-11-01")
    daily = read_member(zf, "ridership-2026-11-01.csv")
    exclusions = read_member(zf, "manifest-exclusions.csv")
    if "G2" in daily:
        # Newer snapshot: manifest must mention 2026-11-12 as a source.
        assert "2026-11-12,1" in read_member(zf, "manifest-sources.csv")
    else:
        # Older snapshot: 2026-11-12 must appear in exclusions, not files.
        assert "2026-11-12" in exclusions
    assert plan_before["travel_days"] == ["2026-11-01"]


def test_eligible_batch_outside_window_listed_as_no_rows_in_window(client):
    enable(client)
    publish_issue_day(client, "2026-11-11",
                      [issue_row("T1", "G1", "2026-12-25", "A", "B", 2)])
    plan = client.get(
        "/api/ridership-supply/plan?start=2026-11-01&end=2026-11-01"
    ).get_json()
    matches = [row for row in plan["manifest_exclusions"] if row[0] == "2026-11-11"]
    assert matches and matches[0][3] == "no_rows_in_window"


def test_html_ridership_page_renders(client):
    response = client.get("/ridership")
    assert response.status_code == 200
    assert "客流系统".encode("utf-8") in response.data


def test_concurrent_confirms_register_exactly_one_current_supply(app):
    """Two simultaneous confirmations for one issue day: exactly one becomes
    the current official revision and therefore the only supply source."""
    import threading

    enable(app.test_client())
    client = app.test_client()
    first = upload(
        client, day="2026-11-20",
        data=make_csv([issue_row("T1", "G1", "2026-11-01", "A", "B", 1)]),
    ).get_json()
    second = upload(
        client, day="2026-11-20",
        data=make_csv([issue_row("T2", "G2", "2026-11-01", "C", "D", 2)]),
    ).get_json()
    results = []
    barrier = threading.Barrier(2)

    def confirm(preview_id):
        local = app.test_client()
        barrier.wait()
        response = local.post(
            f"/api/previews/{preview_id}/confirm",
            headers={"Accept": "application/json"},
        )
        results.append(response.status_code)

    threads = [
        threading.Thread(target=confirm, args=(first["preview_id"],)),
        threading.Thread(target=confirm, args=(second["preview_id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [201, 409]

    plan = client.get(
        "/api/ridership-supply/plan?start=2026-11-01&end=2026-11-01"
    ).get_json()
    # One source registration for the single current revision.
    assert len(plan["manifest_sources"]) == 1
    assert plan["files"][0]["row_count"] == 1
