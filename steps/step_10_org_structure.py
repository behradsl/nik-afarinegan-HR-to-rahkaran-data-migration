"""
Step 10: Rebuild HCM3.OrganizationalStructure for Rahkaran UI.

Per OrganizationChart (Oc) era:
  - Department nodes (PostRef NULL) form the tree via department parent links
  - Post nodes hang under their department node
  - InsertionDate = Oc date; DeletionDate = next Oc date (NULL for latest)
  - OrganizationalStructureDescription per Oc ChangeDate
  - OrganizationalStructureItem (مصوب) on each post node
"""
import pandas as pd
import warnings
from datetime import date
from utils.date_helpers import shamsi_to_gregorian
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.org_migration import (
    ensure_departments,
    ensure_posts,
    ensure_table_id,
    setup_org_structure_description_mapping_table,
    setup_org_structure_mapping_table,
    upgrade_org_structure_mapping_schema,
)

warnings.filterwarnings('ignore', category=UserWarning)

OPEN_END_SHAMSI = '1499/12/29'
NODE_DEPT = 'D'
NODE_POST = 'P'
# OrganizationStructurePostType: 1 = مصوب
POST_TYPE_APPROVED = 1


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


def _clear_previous_structure(dest_cursor):
    """Remove previously migrated structure so this step can fully rebuild."""
    # Legacy schema: (SourcePostID, SourceOcID, DestOrganizationalStructureID)
    dest_cursor.execute("""
        IF EXISTS (SELECT * FROM master.sys.tables WHERE name = 'OrgStructureMigrationMapping')
        BEGIN
            UPDATE os SET os.ParentRef = NULL
            FROM HCM3.OrganizationalStructure os
            INNER JOIN master.dbo.OrgStructureMigrationMapping m
                ON os.OrganizationalStructureID = m.DestOrganizationalStructureID

            IF OBJECT_ID('HCM3.OrganizationalStructureItem') IS NOT NULL
            BEGIN
                DELETE i
                FROM HCM3.OrganizationalStructureItem i
                INNER JOIN master.dbo.OrgStructureMigrationMapping m
                    ON i.OrganizationalStructureRef = m.DestOrganizationalStructureID
            END

            IF EXISTS (SELECT * FROM master.sys.tables WHERE name = 'StatuteMigrationMapping')
            BEGIN
                UPDATE s SET s.OrganizationalStructureRef = NULL
                FROM HCM3.EmployeeStatute s
                INNER JOIN master.dbo.OrgStructureMigrationMapping m
                    ON s.OrganizationalStructureRef = m.DestOrganizationalStructureID
            END

            DELETE os
            FROM HCM3.OrganizationalStructure os
            INNER JOIN master.dbo.OrgStructureMigrationMapping m
                ON os.OrganizationalStructureID = m.DestOrganizationalStructureID

            DELETE FROM master.dbo.OrgStructureMigrationMapping
        END
    """)
    dest_cursor.execute("""
        IF EXISTS (
            SELECT * FROM master.sys.tables
            WHERE name = 'OrgStructureDescriptionMigrationMapping'
        )
        BEGIN
            DELETE d
            FROM HCM3.OrganizationalStructureDescription d
            INNER JOIN master.dbo.OrgStructureDescriptionMigrationMapping m
                ON d.OrganizationalStructureDescriptionID = m.DestDescriptionID

            DELETE FROM master.dbo.OrgStructureDescriptionMigrationMapping
        END
    """)


def _collect_dept_ancestors(dept_id, parent_by_dept, needed):
    """Add dept_id and all ancestors into needed set."""
    seen = set()
    current = dept_id
    while current and current not in seen:
        seen.add(current)
        needed.add(current)
        current = parent_by_dept.get(current)


def _insert_dept_nodes(
    dest_cursor,
    oc_id,
    needed_depts,
    parent_by_dept,
    dept_map,
    insertion,
    deletion,
    structure_last_id,
    insert_os_sql,
    insert_map_sql,
):
    """Insert department-only OS nodes; return source_dept_id -> dest_os_id."""
    local = {}
    pending = set(needed_depts)
    inserted = 0
    skipped = 0
    safety = 0
    max_passes = len(pending) + 5

    while pending and safety < max_passes:
        safety += 1
        progress = False
        for source_dept_id in list(pending):
            dest_dept = dept_map.get(source_dept_id)
            if not dest_dept:
                skipped += 1
                pending.discard(source_dept_id)
                progress = True
                continue

            parent_src = parent_by_dept.get(source_dept_id)
            if parent_src and parent_src in needed_depts and parent_src not in local:
                continue

            parent_ref = local.get(parent_src) if parent_src else None
            structure_last_id += 1
            dest_cursor.execute(
                insert_os_sql,
                (structure_last_id, dest_dept, None, parent_ref, insertion, deletion),
            )
            dest_cursor.execute(
                insert_map_sql,
                (oc_id, NODE_DEPT, source_dept_id, structure_last_id),
            )
            local[source_dept_id] = structure_last_id
            pending.discard(source_dept_id)
            inserted += 1
            progress = True

        if not progress:
            for source_dept_id in list(pending):
                dest_dept = dept_map.get(source_dept_id)
                if not dest_dept:
                    skipped += 1
                    pending.discard(source_dept_id)
                    continue
                structure_last_id += 1
                dest_cursor.execute(
                    insert_os_sql,
                    (structure_last_id, dest_dept, None, None, insertion, deletion),
                )
                dest_cursor.execute(
                    insert_map_sql,
                    (oc_id, NODE_DEPT, source_dept_id, structure_last_id),
                )
                local[source_dept_id] = structure_last_id
                pending.discard(source_dept_id)
                inserted += 1
            break

    return local, structure_last_id, inserted, skipped


def run():
    print("\n--- Running Step 10: Organizational Structure Migration (rebuild) ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        print("Clearing previously migrated organizational structure...")
        _clear_previous_structure(dest_cursor)

        print("Ensuring mapping schema...")
        upgrade_org_structure_mapping_schema(dest_cursor)
        setup_org_structure_description_mapping_table(dest_cursor)

        print("Ensuring Department / Post masters...")
        dept_map = ensure_departments(source_cnxn, dest_cnxn, dest_cursor)
        post_map = ensure_posts(source_cnxn, dest_cnxn, dest_cursor)

        print("Fetching Organization Charts...")
        oc_df = pd.read_sql("""
            SELECT
                TBL_OcID AS SourceOcID,
                TBL_OcDate AS OcDate,
                TBL_OcDescription AS OcDescription,
                TBL_OcNo AS OcNo
            FROM dbo.TBL_OrganizationChart
            WHERE TBL_OcID > 0
        """, source_cnxn)

        if oc_df.empty:
            print("No organization charts found.")
            dest_cnxn.commit()
            return

        oc_df['_sort'] = oc_df['OcDate'].apply(
            lambda x: _parse_shamsi_date(x) or '1900-01-01'
        )
        oc_df = oc_df.sort_values(by=['_sort', 'SourceOcID']).reset_index(drop=True)

        oc_windows = []
        for i, row in oc_df.iterrows():
            oc_id = int(row['SourceOcID'])
            insertion = _parse_shamsi_date(row['OcDate']) or '1900-01-01'
            if i + 1 < len(oc_df):
                next_date = _parse_shamsi_date(oc_df.iloc[i + 1]['OcDate'])
                deletion = next_date  # superseded when next chart starts
            else:
                deletion = None  # current chart stays open
            oc_windows.append({
                'SourceOcID': oc_id,
                'InsertionDate': insertion,
                'DeletionDate': deletion,
                'OcDescription': row['OcDescription'],
                'OcNo': row['OcNo'],
            })

        depts_df = pd.read_sql("""
            SELECT
                TBL_DepartmentID AS SourceDepartmentID,
                TBL_DepartmentParentID_fk AS SourceParentDepartmentID
            FROM dbo.TBL_Department
            WHERE TBL_DepartmentID > 0
        """, source_cnxn)
        parent_by_dept = {}
        for _, r in depts_df.iterrows():
            did = int(r['SourceDepartmentID'])
            pid = _positive_fk(r['SourceParentDepartmentID'])
            if pid and pid != did:
                parent_by_dept[did] = pid

        posts_df = pd.read_sql("""
            SELECT
                TBL_PostID AS SourcePostID,
                TBL_OcID_fk AS SourceOcID,
                TBL_DepartmentID_fk AS SourceDepartmentID,
                TBL_PostParentID_fk AS SourceParentPostID
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
        item_last_id = ensure_table_id(dest_cursor, 'HCM3.OrganizationalStructureItem', 0)
        desc_last_id = ensure_table_id(
            dest_cursor, 'HCM3.OrganizationalStructureDescription', 0
        )

        insert_os_sql = """
            INSERT INTO HCM3.OrganizationalStructure (
                OrganizationalStructureID, DepartmentRef, PostRef, ParentRef,
                InsertionDate, DeletionDate
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        insert_map_sql = """
            INSERT INTO master.dbo.OrgStructureMigrationMapping (
                SourceOcID, NodeKind, SourceID, DestOrganizationalStructureID
            ) VALUES (?, ?, ?, ?)
        """
        insert_item_sql = """
            INSERT INTO HCM3.OrganizationalStructureItem (
                OrganizationalStructureItemID, OrganizationalStructureRef,
                OrganizationalStructurePostTypeCode, InsertionDate, DeletionDate
            ) VALUES (?, ?, ?, ?, ?)
        """
        insert_desc_sql = """
            INSERT INTO HCM3.OrganizationalStructureDescription (
                OrganizationalStructureDescriptionID, ChangeDate, Description,
                CreationDate, Creator, LastModificationDate, LastModifier
            ) VALUES (?, ?, ?, GETDATE(), 1, GETDATE(), 1)
        """
        insert_desc_map_sql = """
            INSERT INTO master.dbo.OrgStructureDescriptionMigrationMapping (
                SourceOcID, DestDescriptionID
            ) VALUES (?, ?)
        """

        dept_nodes = 0
        post_nodes = 0
        items_inserted = 0
        descs_inserted = 0
        skipped_no_dept = 0
        skipped_no_post = 0
        skipped_no_dept_master = 0

        for oc in oc_windows:
            oc_id = oc['SourceOcID']
            insertion = oc['InsertionDate']
            deletion = oc['DeletionDate']
            print(
                f"  Oc {oc_id}: insert={insertion}, delete={deletion or 'NULL'}..."
            )

            # Description for this chart effective date
            desc_text = clean_persian_text(oc['OcDescription'])
            if not desc_text:
                oc_no = clean_persian_text(oc['OcNo'])
                desc_text = oc_no or f'ساختار سازمانی {oc_id}'
            desc_last_id += 1
            dest_cursor.execute(
                insert_desc_sql,
                (desc_last_id, insertion, desc_text[:2000]),
            )
            dest_cursor.execute(insert_desc_map_sql, (oc_id, desc_last_id))
            descs_inserted += 1

            # For the latest Oc (no deletion), also publish a description on "today"
            # so the UI ChangeDate=today lookup finds a row. Mapped as SourceOcID=0.
            if deletion is None:
                today_str = date.today().strftime('%Y-%m-%d')
                if str(insertion)[:10] != today_str:
                    desc_last_id += 1
                    dest_cursor.execute(
                        insert_desc_sql,
                        (desc_last_id, today_str, desc_text[:2000]),
                    )
                    dest_cursor.execute(insert_desc_map_sql, (0, desc_last_id))
                    descs_inserted += 1

            oc_posts = posts_df[posts_df['SourceOcID'] == oc_id]
            needed_depts = set()
            for _, prow in oc_posts.iterrows():
                source_dept = _positive_fk(prow['SourceDepartmentID'])
                if source_dept:
                    _collect_dept_ancestors(source_dept, parent_by_dept, needed_depts)

            dept_local, structure_last_id, d_ins, d_skip = _insert_dept_nodes(
                dest_cursor,
                oc_id,
                needed_depts,
                parent_by_dept,
                dept_map,
                insertion,
                deletion,
                structure_last_id,
                insert_os_sql,
                insert_map_sql,
            )
            dept_nodes += d_ins
            skipped_no_dept_master += d_skip

            post_local = {}  # SourcePostID -> DestOrganizationalStructureID
            pending_parent = []  # (dest_os_id, source_parent_post_id)

            for _, prow in oc_posts.iterrows():
                source_post_id = int(prow['SourcePostID'])
                dest_post = post_map.get(source_post_id)
                if not dest_post:
                    skipped_no_post += 1
                    continue

                source_dept = _positive_fk(prow['SourceDepartmentID'])
                dest_dept = dept_map.get(source_dept) if source_dept else None
                if not dest_dept:
                    skipped_no_dept += 1
                    continue

                parent_ref = dept_local.get(source_dept) if source_dept else None
                structure_last_id += 1
                dest_cursor.execute(
                    insert_os_sql,
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
                    insert_map_sql,
                    (oc_id, NODE_POST, source_post_id, structure_last_id),
                )
                post_local[source_post_id] = structure_last_id
                source_parent_post = _positive_fk(prow['SourceParentPostID'])
                if source_parent_post and source_parent_post != source_post_id:
                    pending_parent.append((structure_last_id, source_parent_post))
                post_nodes += 1

                item_last_id += 1
                dest_cursor.execute(
                    insert_item_sql,
                    (
                        item_last_id,
                        structure_last_id,
                        POST_TYPE_APPROVED,
                        insertion,
                        deletion,
                    ),
                )
                items_inserted += 1

            # Prefer post→post parent when parent post is on the same chart
            parent_linked = 0
            for dest_os_id, source_parent_post in pending_parent:
                parent_os = post_local.get(source_parent_post)
                if not parent_os:
                    continue
                dest_cursor.execute(
                    """
                    UPDATE HCM3.OrganizationalStructure
                    SET ParentRef = ?
                    WHERE OrganizationalStructureID = ?
                      AND ISNULL(ParentRef, -1) <> ?
                    """,
                    (parent_os, dest_os_id, parent_os),
                )
                if dest_cursor.rowcount:
                    parent_linked += 1
            if parent_linked:
                print(f"    -> Post→post parents linked: {parent_linked}.")

        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? "
            "WHERE TableName = 'HCM3.OrganizationalStructure'",
            (structure_last_id,),
        )
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? "
            "WHERE TableName = 'HCM3.OrganizationalStructureItem'",
            (item_last_id,),
        )
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? "
            "WHERE TableName = 'HCM3.OrganizationalStructureDescription'",
            (desc_last_id,),
        )

        dest_cnxn.commit()
        print(
            f"Success! Dept nodes: {dept_nodes}, Post nodes: {post_nodes}, "
            f"Items: {items_inserted}, Descriptions: {descs_inserted}. "
            f"Skipped (no post master): {skipped_no_post}. "
            f"Skipped (no dept on post): {skipped_no_dept}. "
            f"Skipped (no dept master): {skipped_no_dept_master}."
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
