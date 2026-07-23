"""Step 10: Migrate all OrganizationChart eras into HCM3.OrganizationalStructure."""
import pandas as pd
import warnings
from utils.date_helpers import shamsi_to_gregorian
from db_core import get_connections
from utils.org_migration import (
    ensure_departments,
    ensure_posts,
    ensure_table_id,
    setup_org_structure_mapping_table,
)

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'


def _parse_shamsi_date(raw):
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    date_part = text.split()[0]
    if date_part in ('', '0', '____/__/__', '/  /', '//', '0/0/0', OPEN_END_SHAMSI):
        return None
    if '_' in date_part or date_part.count('/') != 2:
        return None
    return shamsi_to_gregorian(date_part)


def _positive_fk(val):
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    try:
        num = int(float(val))
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def run():
    print("\n--- Running Step 10: Organizational Structure Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        setup_org_structure_mapping_table(dest_cursor)

        print("Ensuring Department / Post masters...")
        dept_map = ensure_departments(source_cnxn, dest_cnxn, dest_cursor)
        post_map = ensure_posts(source_cnxn, dest_cnxn, dest_cursor)

        existing_df = pd.read_sql("""
            SELECT SourcePostID, SourceOcID, DestOrganizationalStructureID
            FROM master.dbo.OrgStructureMigrationMapping
        """, dest_cnxn)
        already = {
            (int(r['SourcePostID']), int(r['SourceOcID'])): int(r['DestOrganizationalStructureID'])
            for _, r in existing_df.iterrows()
        }

        print("Fetching Organization Charts and Posts...")
        oc_df = pd.read_sql("""
            SELECT TBL_OcID AS SourceOcID, TBL_OcDate AS OcDate
            FROM dbo.TBL_OrganizationChart
            WHERE TBL_OcID > 0
        """, source_cnxn)
        oc_dates = {
            int(r['SourceOcID']): _parse_shamsi_date(r['OcDate'])
            for _, r in oc_df.iterrows()
        }

        posts_df = pd.read_sql("""
            SELECT
                TBL_PostID AS SourcePostID,
                TBL_OcID_fk AS SourceOcID,
                TBL_DepartmentID_fk AS SourceDepartmentID,
                TBL_PostParentID_fk AS SourceParentPostID,
                TBL_PostCreateDate AS PostCreateDate,
                TBL_PostExpireDate AS PostExpireDate
            FROM dbo.TBL_Post
            WHERE TBL_PostID > 0
              AND TBL_OcID_fk IS NOT NULL
              AND TBL_OcID_fk > 0
        """, source_cnxn)

        if posts_df.empty:
            print("No posts with organization chart found.")
            dest_cnxn.commit()
            return

        structure_last_id = ensure_table_id(dest_cursor, 'HCM3.OrganizationalStructure', 0)
        insert_sql = """
            INSERT INTO HCM3.OrganizationalStructure (
                OrganizationalStructureID, DepartmentRef, PostRef, ParentRef,
                InsertionDate, DeletionDate
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        insert_mapping_sql = """
            INSERT INTO master.dbo.OrgStructureMigrationMapping (
                SourcePostID, SourceOcID, DestOrganizationalStructureID
            ) VALUES (?, ?, ?)
        """

        inserted = 0
        skipped_no_dept = 0
        skipped_no_post = 0
        skipped_already = 0
        deferred_forced = 0

        for oc_id, group in posts_df.groupby('SourceOcID'):
            oc_id = int(oc_id)
            oc_insertion = oc_dates.get(oc_id)
            post_ids_in_oc = set(int(p) for p in group['SourcePostID'].tolist())

            # Build row lookup
            rows_by_post = {
                int(r['SourcePostID']): r for _, r in group.iterrows()
            }

            pending = set(post_ids_in_oc)
            local_map = {
                pid: already[(pid, oc_id)]
                for pid in post_ids_in_oc
                if (pid, oc_id) in already
            }
            for pid in list(local_map.keys()):
                pending.discard(pid)
                skipped_already += 1

            safety = 0
            while pending and safety < len(post_ids_in_oc) + 5:
                safety += 1
                progress = False
                for source_post_id in list(pending):
                    row = rows_by_post[source_post_id]
                    parent_src = _positive_fk(row['SourceParentPostID'])
                    if (
                        parent_src
                        and parent_src in post_ids_in_oc
                        and parent_src not in local_map
                    ):
                        continue

                    dest_post = post_map.get(source_post_id)
                    if not dest_post:
                        skipped_no_post += 1
                        pending.discard(source_post_id)
                        progress = True
                        continue

                    source_dept = _positive_fk(row['SourceDepartmentID'])
                    dest_dept = dept_map.get(source_dept) if source_dept else None
                    if not dest_dept:
                        skipped_no_dept += 1
                        pending.discard(source_post_id)
                        progress = True
                        continue

                    parent_ref = None
                    if parent_src and parent_src in local_map:
                        parent_ref = local_map[parent_src]

                    insertion = oc_insertion or _parse_shamsi_date(row['PostCreateDate'])
                    if insertion is None:
                        insertion = '1900-01-01'
                    deletion = _parse_shamsi_date(row['PostExpireDate'])

                    structure_last_id += 1
                    dest_cursor.execute(
                        insert_sql,
                        (
                            structure_last_id,
                            dest_dept,
                            dest_post,
                            parent_ref,
                            insertion,
                            deletion,
                        ),
                    )
                    dest_cursor.execute(
                        insert_mapping_sql,
                        (source_post_id, oc_id, structure_last_id),
                    )
                    local_map[source_post_id] = structure_last_id
                    already[(source_post_id, oc_id)] = structure_last_id
                    pending.discard(source_post_id)
                    inserted += 1
                    progress = True

                if not progress:
                    # Force remaining with null parent to break cycles
                    for source_post_id in list(pending):
                        row = rows_by_post[source_post_id]
                        dest_post = post_map.get(source_post_id)
                        source_dept = _positive_fk(row['SourceDepartmentID'])
                        dest_dept = dept_map.get(source_dept) if source_dept else None
                        if not dest_post or not dest_dept:
                            if not dest_post:
                                skipped_no_post += 1
                            if not dest_dept:
                                skipped_no_dept += 1
                            pending.discard(source_post_id)
                            continue
                        insertion = oc_insertion or _parse_shamsi_date(row['PostCreateDate'])
                        if insertion is None:
                            insertion = '1900-01-01'
                        deletion = _parse_shamsi_date(row['PostExpireDate'])
                        structure_last_id += 1
                        dest_cursor.execute(
                            insert_sql,
                            (
                                structure_last_id,
                                dest_dept,
                                dest_post,
                                None,
                                insertion,
                                deletion,
                            ),
                        )
                        dest_cursor.execute(
                            insert_mapping_sql,
                            (source_post_id, oc_id, structure_last_id),
                        )
                        local_map[source_post_id] = structure_last_id
                        already[(source_post_id, oc_id)] = structure_last_id
                        pending.discard(source_post_id)
                        inserted += 1
                        deferred_forced += 1
                    break

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? "
            "WHERE TableName = 'HCM3.OrganizationalStructure'",
            (structure_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! OrganizationalStructure inserted: {inserted}. "
            f"Already mapped: {skipped_already}. "
            f"Skipped (no post): {skipped_no_post}. "
            f"Skipped (no dept): {skipped_no_dept}. "
            f"Forced null parent (cycles): {deferred_forced}."
        )

    except Exception as e:
        dest_cnxn.rollback()
        print(
            f"Migration failed during Organizational Structure step. "
            f"Transaction rolled back. Error: {e}"
        )
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
