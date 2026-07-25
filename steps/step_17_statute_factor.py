"""
Step 17: Migrate PAY_PayrollFactor (used on rule docs) → HCM3.StatuteFactor.

Only factors referenced by HRS_RuleDocumentDetail or HRS_RuleDocumentScores.
Properties / statute value links are out of scope for this step.
"""
import pandas as pd
import warnings
from db_core import get_connections
from utils.data_helpers import clean_persian_text

warnings.filterwarnings('ignore', category=UserWarning)

# StatuteFactorPeriod / StatuteFactorType / StatuteFactorRelatedStatuteType
PERIOD_MONTHLY = 1
TYPE_PRIMARY = 1
TYPE_SECONDARY = 2
RELATED_IN_SERVICE = 1


def setup_statute_factor_mapping_table(cursor):
    cursor.execute("""
        IF NOT EXISTS (
            SELECT * FROM master.sys.tables WHERE name = 'StatuteFactorMigrationMapping'
        )
        BEGIN
            CREATE TABLE master.dbo.StatuteFactorMigrationMapping (
                SourcePayrollFactorID INT PRIMARY KEY,
                DestStatuteFactorID BIGINT NOT NULL,
                MigrationDate DATETIME DEFAULT GETDATE()
            )
        END
    """)
    cursor.commit()


def _ensure_table_id(cursor, table_name, default_last_id=0):
    cursor.execute("""
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
    """, (table_name,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, ?)",
            (table_name, default_last_id),
        )
        return default_last_id
    return int(row[0])


def _unique_title(base_title, source_id, used_titles):
    title = (base_title or f'عامل {source_id}')[:400]
    if title not in used_titles:
        used_titles.add(title)
        return title
    suffix = f' ({source_id})'
    title = (title[: 400 - len(suffix)] + suffix)
    used_titles.add(title)
    return title


def run():
    print("\n--- Running Step 17: Statute Factor Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_statute_factor_mapping_table(dest_cursor)

        print("Fetching payroll factors used on rule documents...")
        source_df = pd.read_sql("""
            SELECT
                pf.PAY_PfID AS SourcePayrollFactorID,
                pf.PAY_PfName AS FactorName,
                pf.PAY_PfNote AS FactorNote,
                pf.PAY_PfFieldName AS FieldName,
                CASE WHEN d.PAY_PfID_fk IS NOT NULL THEN 1 ELSE 0 END AS InDetail,
                CASE WHEN s.PAY_PfID_fk IS NOT NULL THEN 1 ELSE 0 END AS InScore
            FROM dbo.PAY_PayrollFactor pf
            LEFT JOIN (
                SELECT DISTINCT PAY_PfID_fk
                FROM dbo.HRS_RuleDocumentDetail
                WHERE PAY_PfID_fk > 0
            ) d ON d.PAY_PfID_fk = pf.PAY_PfID
            LEFT JOIN (
                SELECT DISTINCT PAY_PfID_fk
                FROM dbo.HRS_RuleDocumentScores
                WHERE PAY_PfID_fk > 0
            ) s ON s.PAY_PfID_fk = pf.PAY_PfID
            WHERE pf.PAY_PfID > 0
              AND (d.PAY_PfID_fk IS NOT NULL OR s.PAY_PfID_fk IS NOT NULL)
            ORDER BY pf.PAY_PfID
        """, source_cnxn)

        if source_df.empty:
            print("No statute-related payroll factors found.")
            return

        source_df['FactorName'] = source_df['FactorName'].apply(clean_persian_text)
        source_df['FactorNote'] = source_df['FactorNote'].apply(
            lambda x: clean_persian_text(x) if pd.notna(x) else None
        )

        mapped_df = pd.read_sql(
            "SELECT SourcePayrollFactorID, DestStatuteFactorID "
            "FROM master.dbo.StatuteFactorMigrationMapping",
            dest_cnxn,
        )
        already = set(int(x) for x in mapped_df['SourcePayrollFactorID'].tolist())
        pending = source_df[~source_df['SourcePayrollFactorID'].isin(already)].copy()

        print(
            f"  -> Candidates: {len(source_df)}. "
            f"Already mapped: {len(already)}. To insert: {len(pending)}."
        )
        if pending.empty:
            print("Success! All statute factors already mapped.")
            return

        existing_titles = set(
            pd.read_sql("SELECT Title FROM HCM3.StatuteFactor", dest_cnxn)['Title']
            .dropna()
            .tolist()
        )
        used_titles = {str(t) for t in existing_titles}

        last_id = _ensure_table_id(dest_cursor, 'HCM3.StatuteFactor', 0)
        insert_sql = """
            INSERT INTO HCM3.StatuteFactor (
                StatuteFactorID, Name, Title, PeriodCode, TypeCode,
                RelatedStatuteTypeCode, Description,
                VisibleInStatute, VisibleInEmployee,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, GETDATE(), 1, GETDATE(), 1)
        """
        map_sql = """
            INSERT INTO master.dbo.StatuteFactorMigrationMapping (
                SourcePayrollFactorID, DestStatuteFactorID
            ) VALUES (?, ?)
        """

        inserted = 0
        for _, row in pending.iterrows():
            source_id = int(row['SourcePayrollFactorID'])
            title = _unique_title(row['FactorName'], source_id, used_titles)
            name = f'Mig_Pf_{source_id}'
            # Detail (price) factors as primary; score-only as secondary.
            type_code = TYPE_PRIMARY if int(row['InDetail']) == 1 else TYPE_SECONDARY
            note = row['FactorNote']
            if note and len(note) > 0:
                description = note
            else:
                field = row['FieldName']
                description = str(field).strip() if pd.notna(field) and str(field).strip() not in ('', '0') else None

            last_id += 1
            dest_cursor.execute(
                insert_sql,
                (
                    last_id,
                    name,
                    title,
                    PERIOD_MONTHLY,
                    type_code,
                    RELATED_IN_SERVICE,
                    description,
                ),
            )
            dest_cursor.execute(map_sql, (source_id, last_id))
            inserted += 1

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (last_id, 'HCM3.StatuteFactor'),
        )
        dest_cnxn.commit()
        print(
            f"Success! StatuteFactors inserted: {inserted}. "
            f"Skipped (already mapped): {len(already)}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Statute Factor step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        dest_cnxn.close()
        source_cnxn.close()
