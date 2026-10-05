"""
Step 21: Migrate stored statute-factor amounts → HCM3.EmployeeStatuteFactor.

Parent 2 (عوامل حکمی): HRS_RddPrice.
Parent 22 (امتیازات فردی): HRS_RdsSystemAmount.
سابقه خدمت: HRS_RdsUserAmount.

CalcValue is NOT NULL on the destination, so it is stored as 0.
EditedValue carries the migrated amount. Value is computed as
isnull(EditedValue, CalcValue), so it shows the same amount.
The property is the one whose start month is the latest still on or
before the statute apply month.
"""
import bisect
import csv
from pathlib import Path

import jdatetime
import pandas as pd
import warnings

from db_core import get_connections
from utils.date_helpers import shamsi_to_gregorian
from utils.roll_formula_properties import (
    SCORE_PARENT_ID,
    STATUTE_PARENT_ID,
    load_service_history_factor_ids,
)

warnings.filterwarnings('ignore', category=UserWarning)

EXCEPTION_DIR = (
    Path(__file__).resolve().parent.parent
    / "analysis"
    / "statute_factor_values"
)
EXCEPTION_LIMIT = 5000
INSERT_BATCH = 4000

VALUE_DETAIL_PRICE = "detail_price"
VALUE_SCORE_SYSTEM = "score_system"
VALUE_SCORE_USER = "score_user"

_NOT_DELETED_DETAIL = """
    AND (
        d.HRS_RddDeleteDate IS NULL
        OR LTRIM(RTRIM(d.HRS_RddDeleteDate)) IN (N'', N'0')
    )
"""
_NOT_DELETED_SCORE = """
    AND (
        s.HRS_RdsDeleteDate IS NULL
        OR LTRIM(RTRIM(s.HRS_RdsDeleteDate)) IN (N'', N'0')
    )
"""


def setup_value_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables
            WHERE name = 'EmployeeStatuteFactorMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.EmployeeStatuteFactorMigrationMapping (
                SourceRuleDocumentID INT NOT NULL,
                SourcePayrollFactorID INT NOT NULL,
                ValueSource NVARCHAR(20) NOT NULL,
                DestEmployeeStatuteFactorID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE(),
                PRIMARY KEY (SourceRuleDocumentID, SourcePayrollFactorID)
            )
        END
    """)
    cursor.commit()


def _ensure_table_id(cursor, table_name):
    cursor.execute("""
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
    """, (table_name,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, 0)",
            (table_name,),
        )
        return 0
    return int(row[0])


def _clear_previous_values(dest_cursor):
    dest_cursor.execute("""
        IF OBJECT_ID('master.dbo.EmployeeStatuteFactorMigrationMapping') IS NOT NULL
            DELETE esf
            FROM HCM3.EmployeeStatuteFactor esf
            INNER JOIN master.dbo.EmployeeStatuteFactorMigrationMapping m
                ON esf.EmployeeStatuteFactorID = m.DestEmployeeStatuteFactorID
    """)
    dest_cursor.execute("""
        IF OBJECT_ID('master.dbo.EmployeeStatuteFactorMigrationMapping') IS NOT NULL
            DELETE FROM master.dbo.EmployeeStatuteFactorMigrationMapping
    """)


def _apply_year_month(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            year, month, _day = text[:10].split("-")
            greg = jdatetime.date.fromgregorian(
                year=int(year), month=int(month), day=int(_day)
            )
            return greg.year * 100 + greg.month
        except (TypeError, ValueError):
            return None
    try:
        moment = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
        greg = moment.date() if hasattr(moment, "date") else moment
        shamsi = jdatetime.date.fromgregorian(date=greg)
        return shamsi.year * 100 + shamsi.month
    except (TypeError, ValueError, OverflowError):
        return None


def _as_date_text(value):
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d")
    text = str(value).strip()
    return text[:10] if text else None


def _report_date_mismatches(source_cnxn, statutes):
    """Compare destination issue/apply dates with the source حکم dates."""
    source_dates = pd.read_sql(
        """
        SELECT HRS_RdID AS SourceRuleDocumentID,
               HRS_RdExportDate AS ExportDate,
               HRS_RdExcuteDate AS ExecuteDate
        FROM dbo.HRS_RuleDocument
        WHERE HRS_RdID > 0
        """,
        source_cnxn,
    )
    source_dates["SourceRuleDocumentID"] = source_dates["SourceRuleDocumentID"].astype(int)
    by_id = source_dates.set_index("SourceRuleDocumentID")
    issue_mismatch = 0
    apply_mismatch = 0
    missing_source = 0
    samples = []
    for rd_id, row in statutes.items():
        if rd_id not in by_id.index:
            missing_source += 1
            continue
        src = by_id.loc[rd_id]
        if isinstance(src, pd.DataFrame):
            src = src.iloc[0]
        export_g = shamsi_to_gregorian(src["ExportDate"])
        execute_g = shamsi_to_gregorian(src["ExecuteDate"])
        issue = _as_date_text(row["IssueDate"])
        apply = _as_date_text(row["ApplyDate"])
        issue_bad = bool(export_g) and issue != export_g
        apply_bad = bool(execute_g) and apply != execute_g
        if issue_bad:
            issue_mismatch += 1
        if apply_bad:
            apply_mismatch += 1
        if (issue_bad or apply_bad) and len(samples) < 10:
            samples.append(
                f"Rd {rd_id}: issue dest {issue} source {export_g}; "
                f"apply dest {apply} source {execute_g}"
            )
    print(
        f"  -> Date check: issue mismatches={issue_mismatch}, "
        f"apply mismatches={apply_mismatch}, "
        f"source حکم missing={missing_source}."
    )
    for line in samples:
        print(f"     {line}")
    if issue_mismatch or apply_mismatch:
        print(
            "  -> Property lookup still uses the destination apply date."
        )


def _load_property_index(dest_cnxn):
    frame = pd.read_sql(
        """
        SELECT SourcePayrollFactorID, EffectiveFromMonth,
               DestEmploymentTypeID, DestStatuteFactorPropertyID
        FROM master.dbo.StatuteFactorPropertyMigrationMapping
        """,
        dest_cnxn,
    )
    grouped = {}
    for (pf, et), part in frame.groupby(
        ["SourcePayrollFactorID", "DestEmploymentTypeID"], sort=False
    ):
        part = part.sort_values(["EffectiveFromMonth", "DestStatuteFactorPropertyID"])
        months = [int(x) for x in part["EffectiveFromMonth"].tolist()]
        ids = [int(x) for x in part["DestStatuteFactorPropertyID"].tolist()]
        grouped[(int(pf), int(et))] = (months, ids)
    return grouped


def _choose_property(index, factor_id, employment_type_id, apply_ym):
    """Return (property id, None) or (None, reason)."""
    pair = index.get((int(factor_id), int(employment_type_id)))
    if not pair:
        return None, "no_property"
    months, prop_ids = pair
    pos = bisect.bisect_right(months, int(apply_ym)) - 1
    if pos < 0:
        return None, "no_property"
    chosen = months[pos]
    if (pos > 0 and months[pos - 1] == chosen) or (
        pos + 1 < len(months) and months[pos + 1] == chosen
    ):
        return None, "ambiguous_property"
    return prop_ids[pos], None


def _load_statutes(dest_cnxn):
    frame = pd.read_sql(
        """
        SELECT m.SourceRuleDocumentID, m.DestEmployeeStatuteID,
               s.EmploymentTypeRef, s.ApplyDate, s.IssueDate
        FROM master.dbo.StatuteMigrationMapping m
        INNER JOIN HCM3.EmployeeStatute s
            ON s.EmployeeStatuteID = m.DestEmployeeStatuteID
        """,
        dest_cnxn,
    )
    statutes = {}
    for _, row in frame.iterrows():
        et = row["EmploymentTypeRef"]
        statutes[int(row["SourceRuleDocumentID"])] = {
            "DestEmployeeStatuteID": int(row["DestEmployeeStatuteID"]),
            "EmploymentTypeRef": None if pd.isna(et) else int(et),
            "ApplyDate": row["ApplyDate"],
            "IssueDate": row["IssueDate"],
            "ApplyYearMonth": _apply_year_month(row["ApplyDate"]),
        }
    return statutes


def _load_detail_prices(source_cnxn):
    return pd.read_sql(
        f"""
        SELECT
            d.HRS_RdID_fk AS SourceRuleDocumentID,
            d.PAY_PfID_fk AS SourcePayrollFactorID,
            CAST(d.HRS_RddPrice AS decimal(28, 6)) AS Amount,
            d.HRS_RddRegisterDate AS RegisterDate
        FROM dbo.HRS_RuleDocumentDetail d
        INNER JOIN dbo.PAY_PayrollFactor pf ON pf.PAY_PfID = d.PAY_PfID_fk
        WHERE pf.PAY_PfParentID_fk = {STATUTE_PARENT_ID}
          AND d.PAY_PfID_fk > 0
          {_NOT_DELETED_DETAIL}
        """,
        source_cnxn,
    )


def _load_score_amounts(source_cnxn, parent_id, amount_column):
    return pd.read_sql(
        f"""
        SELECT
            s.HRS_RdID_fk AS SourceRuleDocumentID,
            s.PAY_PfID_fk AS SourcePayrollFactorID,
            TRY_CAST(s.{amount_column} AS decimal(28, 6)) AS Amount,
            LTRIM(RTRIM(s.{amount_column})) AS RawAmount,
            s.HRS_RdsRegisterDate AS RegisterDate
        FROM dbo.HRS_RuleDocumentScores s
        INNER JOIN dbo.PAY_PayrollFactor pf ON pf.PAY_PfID = s.PAY_PfID_fk
        WHERE pf.PAY_PfParentID_fk = {int(parent_id)}
          AND s.PAY_PfID_fk > 0
          AND NULLIF(LTRIM(RTRIM(s.{amount_column})), N'') IS NOT NULL
          {_NOT_DELETED_SCORE}
        """,
        source_cnxn,
    )


def _load_service_user_amounts(source_cnxn, factor_ids):
    if not factor_ids:
        return pd.DataFrame(
            columns=[
                "SourceRuleDocumentID",
                "SourcePayrollFactorID",
                "Amount",
                "RawAmount",
                "RegisterDate",
            ]
        )
    id_list = ",".join(str(int(i)) for i in sorted(factor_ids))
    return pd.read_sql(
        f"""
        SELECT
            s.HRS_RdID_fk AS SourceRuleDocumentID,
            s.PAY_PfID_fk AS SourcePayrollFactorID,
            TRY_CAST(s.HRS_RdsUserAmount AS decimal(28, 6)) AS Amount,
            LTRIM(RTRIM(s.HRS_RdsUserAmount)) AS RawAmount,
            s.HRS_RdsRegisterDate AS RegisterDate
        FROM dbo.HRS_RuleDocumentScores s
        WHERE s.PAY_PfID_fk IN ({id_list})
          AND NULLIF(LTRIM(RTRIM(s.HRS_RdsUserAmount)), N'') IS NOT NULL
          {_NOT_DELETED_SCORE}
        """,
        source_cnxn,
    )


class _ExceptionLog:
    def __init__(self):
        self.counts = {}
        self.rows = []

    def add(self, reason, rd_id, factor_id, source_name, detail=""):
        self.counts[reason] = self.counts.get(reason, 0) + 1
        if len(self.rows) < EXCEPTION_LIMIT:
            self.rows.append({
                "Reason": reason,
                "SourceRuleDocumentID": rd_id,
                "SourcePayrollFactorID": factor_id,
                "ValueSource": source_name,
                "Detail": detail,
            })

    def write(self):
        EXCEPTION_DIR.mkdir(parents=True, exist_ok=True)
        path = EXCEPTION_DIR / "exceptions.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "Reason",
                    "SourceRuleDocumentID",
                    "SourcePayrollFactorID",
                    "ValueSource",
                    "Detail",
                ],
            )
            writer.writeheader()
            writer.writerows(self.rows)
        summary = ", ".join(
            f"{key}={self.counts[key]}" for key in sorted(self.counts)
        ) or "none"
        print(f"  -> Exceptions: {summary}.")
        print(f"  -> Exception sample written to {path}.")


def _dedupe(frame, exceptions, source_name):
    if frame.empty:
        return frame
    work = frame.copy()
    work["RegisterDate"] = work["RegisterDate"].fillna("").astype(str).str.strip()
    work = work.sort_values(
        ["SourceRuleDocumentID", "SourcePayrollFactorID", "RegisterDate"],
        kind="mergesort",
    )
    duplicate = work.duplicated(
        ["SourceRuleDocumentID", "SourcePayrollFactorID"], keep="last"
    )
    dropped = work[duplicate]
    for _, row in dropped.head(EXCEPTION_LIMIT).iterrows():
        exceptions.add(
            "duplicate_source_line",
            int(row["SourceRuleDocumentID"]),
            int(row["SourcePayrollFactorID"]),
            source_name,
            "kept the latest register date",
        )
    extra = int(duplicate.sum()) - len(dropped.head(EXCEPTION_LIMIT))
    if extra > 0:
        exceptions.counts["duplicate_source_line"] = (
            exceptions.counts.get("duplicate_source_line", 0) + extra
        )
    return work[~duplicate]


def _flush(dest_cursor, factor_rows, map_rows):
    if not factor_rows:
        return
    dest_cursor.fast_executemany = True
    dest_cursor.executemany(
        """
        INSERT INTO HCM3.EmployeeStatuteFactor (
            EmployeeStatuteFactorID, EmployeeStatuteRef, StatuteFactorRef,
            StatuteFactorPropertyRef, CalcValue, EditedValue,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, 0, ?, GETDATE(), 1, GETDATE(), 1)
        """,
        factor_rows,
    )
    dest_cursor.executemany(
        """
        INSERT INTO master.dbo.EmployeeStatuteFactorMigrationMapping (
            SourceRuleDocumentID, SourcePayrollFactorID, ValueSource,
            DestEmployeeStatuteFactorID
        ) VALUES (?, ?, ?, ?)
        """,
        map_rows,
    )


def _migrate_frame(
    frame,
    source_name,
    statutes,
    factor_map,
    properties,
    exceptions,
    dest_cursor,
    next_id,
):
    inserted = 0
    factor_rows = []
    map_rows = []
    for row in frame.itertuples(index=False):
        rd_id = int(row.SourceRuleDocumentID)
        pf_id = int(row.SourcePayrollFactorID)
        statute = statutes.get(rd_id)
        if statute is None:
            exceptions.add("no_statute", rd_id, pf_id, source_name)
            continue
        dest_factor = factor_map.get(pf_id)
        if dest_factor is None:
            exceptions.add("no_factor", rd_id, pf_id, source_name)
            continue
        if pd.isna(row.Amount):
            exceptions.add(
                "bad_amount",
                rd_id,
                pf_id,
                source_name,
                getattr(row, "RawAmount", ""),
            )
            continue
        et_id = statute["EmploymentTypeRef"]
        apply_ym = statute["ApplyYearMonth"]
        if et_id is None or apply_ym is None:
            exceptions.add("no_apply_or_employment_type", rd_id, pf_id, source_name)
            continue
        prop_id, prop_reason = _choose_property(properties, pf_id, et_id, apply_ym)
        if prop_id is None:
            exceptions.add(prop_reason, rd_id, pf_id, source_name, str(apply_ym))
            continue
        amount = float(row.Amount)
        next_id += 1
        factor_rows.append((
            next_id,
            statute["DestEmployeeStatuteID"],
            dest_factor,
            prop_id,
            amount,
        ))
        map_rows.append((rd_id, pf_id, source_name, next_id))
        inserted += 1
        if len(factor_rows) >= INSERT_BATCH:
            _flush(dest_cursor, factor_rows, map_rows)
            factor_rows = []
            map_rows = []
            if inserted % 40000 == 0:
                print(f"     {source_name}: {inserted} inserted...", flush=True)
    _flush(dest_cursor, factor_rows, map_rows)
    return next_id, inserted


def run():
    print("\n--- Running Step 21: Employee Statute Factor Values ---")
    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()
    exceptions = _ExceptionLog()
    try:
        setup_value_mapping_table(dest_cursor)
        _clear_previous_values(dest_cursor)
        dest_cnxn.commit()
        dest_cursor.close()

        statutes = _load_statutes(dest_cnxn)
        print(f"  -> Migrated statutes: {len(statutes)}.")
        _report_date_mismatches(source_cnxn, statutes)

        factor_map = {
            int(r["SourcePayrollFactorID"]): int(r["DestStatuteFactorID"])
            for _, r in pd.read_sql(
                "SELECT SourcePayrollFactorID, DestStatuteFactorID "
                "FROM master.dbo.StatuteFactorMigrationMapping",
                dest_cnxn,
            ).iterrows()
        }
        properties = _load_property_index(dest_cnxn)
        print(
            f"  -> Mapped factors: {len(factor_map)}. "
            f"Property keys: {len(properties)}."
        )

        dest_cursor = dest_cnxn.cursor()
        next_id = _ensure_table_id(dest_cursor, "HCM3.EmployeeStatuteFactor")
        total = 0

        sources = [
            (
                VALUE_DETAIL_PRICE,
                _load_detail_prices(source_cnxn),
            ),
            (
                VALUE_SCORE_SYSTEM,
                _load_score_amounts(
                    source_cnxn, SCORE_PARENT_ID, "HRS_RdsSystemAmount"
                ),
            ),
            (
                VALUE_SCORE_USER,
                _load_service_user_amounts(
                    source_cnxn, load_service_history_factor_ids(source_cnxn)
                ),
            ),
        ]
        for source_name, frame in sources:
            frame = _dedupe(frame, exceptions, source_name)
            print(f"  -> {source_name}: {len(frame)} source rows.")
            next_id, inserted = _migrate_frame(
                frame,
                source_name,
                statutes,
                factor_map,
                properties,
                exceptions,
                dest_cursor,
                next_id,
            )
            total += inserted
            print(f"  -> {source_name}: inserted {inserted}.")

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (next_id, "HCM3.EmployeeStatuteFactor"),
        )
        exceptions.write()
        dest_cnxn.commit()
        print(f"Success! Employee statute factor values inserted: {total}.")
    except Exception as exc:
        dest_cnxn.rollback()
        print(
            "Migration failed during Employee Statute Factor values. "
            f"Transaction rolled back. Error: {exc}"
        )
        raise
    finally:
        dest_cnxn.close()
        source_cnxn.close()
