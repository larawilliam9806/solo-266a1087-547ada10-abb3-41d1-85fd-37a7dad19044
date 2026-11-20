"""Ridership supply ("客流供数") validation and packaging rules.

The passenger-flow system backfills numbers by *travel date* (乘车日), while one
travel date's detail rows may be spread across several daily settlements, each
published for its own *issue date* (出票日).  This module implements the rules
that decide which published batches may feed the supply dataset and how the
downloaded ZIP is assembled.

Supply columns are fixed here on purpose: the ridership *contract* used for the
classic downstream CSV is user-editable, whereas the supply feed has a stable
external schema.  A batch qualifies only when all five columns are present,
every row's ``travel_date`` is a real ``YYYY-MM-DD`` calendar date and every
``passenger_count`` is a non-negative integer.  Failing batches keep their
normal publication (original + settlement + ridership blobs); they are simply
not registered as supply, and replace (i.e. withdraw) any supply details the
same issue date had contributed under an earlier revision.
"""

import csv
import io
import zipfile
from datetime import date, timedelta

from .errors import AppError

# Fixed external schema of the supply feed; the order is the CSV column order.
SUPPLY_COLUMNS = (
    "train_no",
    "depart_station",
    "arrive_station",
    "travel_date",
    "passenger_count",
)
SUPPLY_COLUMN_SET = frozenset(SUPPLY_COLUMNS)
MAX_RANGE_DAYS = 7

# Stable machine-readable reason codes (Chinese explanations live in the UI).
REASON_FEATURE_DISABLED = "feature_disabled"
REASON_PUBLISHED_BEFORE_ENABLED = "published_before_feature_enabled"
REASON_MISSING_COLUMNS = "missing_supply_columns"
REASON_INVALID_ROWS = "invalid_supply_rows"

REASON_LABELS = {
    REASON_FEATURE_DISABLED: "供数功能未启用",
    REASON_PUBLISHED_BEFORE_ENABLED: "批次发布于功能启用之前",
    REASON_MISSING_COLUMNS: "缺少供数必需列",
    REASON_INVALID_ROWS: "存在不符合供数要求的行（日期或人数）",
}

# Reasons under which a batch may still contribute zero rows legitimately.
NON_QUALIFYING_REASONS = (
    REASON_FEATURE_DISABLED,
    REASON_PUBLISHED_BEFORE_ENABLED,
    REASON_MISSING_COLUMNS,
    REASON_INVALID_ROWS,
)


class SupplyValidation:
    """Result of validating one parsed batch against the supply schema."""

    def __init__(self, eligible, reason=None, reason_detail=None, rows=None,
                 missing_columns=None, issues=None):
        self.eligible = eligible
        self.reason = reason
        self.reason_detail = reason_detail
        # Rows in SUPPLY_COLUMNS order with canonical values, ready to insert.
        self.rows = rows or []
        self.missing_columns = missing_columns or []
        self.issues = issues or []

    def as_preview(self):
        return {
            "eligible": self.eligible,
            "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason) if self.reason else None,
            "reason_detail": self.reason_detail,
            "missing_columns": self.missing_columns,
            "issues": self.issues[:50],
            "issues_truncated": len(self.issues) > 50,
            "row_count": len(self.rows),
            "columns": list(SUPPLY_COLUMNS),
        }


def _is_iso_date(value):
    """Strict YYYY-MM-DD calendar date check (rejects 2026-1-1, 2026-02-30)."""
    if not isinstance(value, str) or len(value) != 10:
        return False
    if value[4] != "-" or value[7] != "-":
        return False
    year_text, month_text, day_text = value[:4], value[5:7], value[8:10]
    if not (year_text.isdigit() and month_text.isdigit() and day_text.isdigit()):
        return False
    try:
        date(int(year_text), int(month_text), int(day_text))
    except ValueError:
        return False
    return True


def _is_non_negative_int(value):
    text = (value or "").strip()
    if not text or not text.isdigit():
        return False
    # Reject absurdly long numbers; the feed contract is ordinary counts.
    if len(text) > 18:
        return False
    return True


def validate_batch(parsed):
    """Validate a :class:`convert.ParsedCsv` against the supply schema.

    Never raises for data problems.  Batch-level CSV defects (encoding,
    duplicate headers, row width, ...) are already rejected before preview, so
    here we only check supply-specific column and row requirements.
    """
    header_index = {name: i for i, name in enumerate(parsed.header)}
    missing = [name for name in SUPPLY_COLUMNS if name not in header_index]
    if missing:
        return SupplyValidation(
            eligible=False,
            reason=REASON_MISSING_COLUMNS,
            reason_detail="缺少供数必需列：" + "、".join(missing),
            missing_columns=missing,
        )

    positions = [header_index[name] for name in SUPPLY_COLUMNS]
    issues = []
    rows = []
    for row_no, (line_no, fields) in enumerate(parsed.rows, start=1):
        values = [fields[pos] for pos in positions]
        travel_date = values[3].strip()
        count_text = values[4].strip()
        row_issues = []
        if not _is_iso_date(travel_date):
            row_issues.append({
                "row": row_no,
                "line": line_no,
                "column": "travel_date",
                "value": values[3][:200],
                "reason": "不是有效的 YYYY-MM-DD 日期",
            })
        if not _is_non_negative_int(count_text):
            row_issues.append({
                "row": row_no,
                "line": line_no,
                "column": "passenger_count",
                "value": values[4][:200],
                "reason": "人数须为非负整数",
            })
        if row_issues:
            issues.extend(row_issues)
            continue
        # Canonical values: trimmed date, normalised count, text columns as-is.
        rows.append([
            values[0],
            values[1],
            values[2],
            travel_date,
            str(int(count_text)),
        ])

    if issues:
        return SupplyValidation(
            eligible=False,
            reason=REASON_INVALID_ROWS,
            reason_detail=f"{len({i['row'] for i in issues})} 行不符合供数要求（整批不纳入）",
            issues=issues,
        )
    return SupplyValidation(eligible=True, rows=rows)


def check_date_range(start_day, end_day):
    """Validate the 1..7 consecutive travel-date window and return date objects."""
    try:
        start = date.fromisoformat(start_day)
        end = date.fromisoformat(end_day)
    except (TypeError, ValueError):
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day") from None
    if start.isoformat() != start_day or end.isoformat() != end_day:
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    if end < start:
        raise AppError("结束乘车日不能早于开始乘车日", code="invalid_range")
    span = (end - start).days + 1
    if span > MAX_RANGE_DAYS:
        raise AppError(
            f"一次最多选择 {MAX_RANGE_DAYS} 个连续乘车日（收到 {span} 个）",
            code="range_too_large",
        )
    return start, end


def enumerate_travel_days(start, end):
    days = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def render_daily_csv(rows):
    """Render one travel date's CSV; always emits the fixed header."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(list(SUPPLY_COLUMNS))
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def render_manifest_sources(entries):
    """Per-file section: which issue-date revision supplied each CSV."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "file", "travel_date", "row_count",
        "source_issue_day", "source_revision", "registered_at",
    ])
    writer.writerows(entries)
    return buf.getvalue().encode("utf-8")


def render_manifest_exclusions(entries):
    """Current official batches that did not make it into the package, and why."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "issue_day", "current_revision", "published_at",
        "reason_code", "reason",
    ])
    writer.writerows(entries)
    return buf.getvalue().encode("utf-8")


def build_zip_bytes(*, travel_days, daily_rows, source_entries, exclusion_entries,
                    generated_at, feature_enabled):
    """Assemble the whole ZIP in memory from one database snapshot."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for travel_day in travel_days:
            archive.writestr(
                f"ridership-{travel_day}.csv",
                render_daily_csv(daily_rows.get(travel_day, [])),
            )
        archive.writestr(
            "manifest-sources.csv",
            render_manifest_sources(source_entries),
        )
        archive.writestr(
            "manifest-exclusions.csv",
            render_manifest_exclusions(exclusion_entries),
        )
        readme = (
            "ridership supply export\n"
            f"generated_at,{generated_at}\n"
            f"feature_enabled,{'yes' if feature_enabled else 'no'}\n"
            "travel_days," + ",".join(travel_days) + "\n"
            "columns," + ",".join(SUPPLY_COLUMNS) + "\n"
            "see manifest-sources.csv and manifest-exclusions.csv for provenance\n"
        )
        archive.writestr("MANIFEST.txt", readme.encode("utf-8"))
    return buffer.getvalue()
