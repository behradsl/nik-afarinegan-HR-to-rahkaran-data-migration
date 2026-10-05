"""
Step 10: Rebuild HCM3.OrganizationalStructure for Rahkaran UI.

Per OrganizationChart (Oc) era:
  - One node per post. ParentRef follows TBL_PostParentID_fk on the same
    chart. Parent 0, or a parent not on this chart, stays null.
  - Department nodes come from department_managers.csv (one reviewed manager
    post per department per chart). Each department node hangs under that
    manager post. PostRef on a department node is null.
  - The manager post and every post below him take that department as
    DepartmentRef, until the next manager post. Posts that report directly
    to the manager and belong to that department are reparented under the
    department node; posts further down keep their post parent, so they
    sit inside the same folder. Source TBL_DepartmentID_fk on those posts
    is not used. Posts outside every manager subtree keep their source
    department. Posts with no department use one technical department
    because DepartmentRef cannot be null.
  - InsertionDate = Oc date; DeletionDate = next Oc date (NULL for latest)
  - OrganizationalStructureDescription per Oc ChangeDate
  - OrganizationalStructureItem (مصوب) on each post node
"""
import pandas as pd
import warnings
from utils.date_helpers import shamsi_to_gregorian
from db_core import get_connections
from utils.data_helpers import clean_persian_text
from utils.department_managers import (
    assign_departments_under_managers,
    department_managers_path,
    load_approved_managers,
)
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
NODE_POST = 'P'
NODE_DEPT = 'D'
# OrganizationStructurePostType: 1 = مصوب
POST_TYPE_APPROVED = 1
# HCM3.OrganizationalStructure.DepartmentRef is NOT NULL. Posts with no
# source department still need a department id; they are roots (ParentRef NULL).
NO_DEPT_CODE = 'MIG-NO-DEPT'
NO_DEPT_TITLE = 'بدون واحد سازمانی'


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


def _chain_reaches(parent_of, start_id, target_id):
    """True when walking ParentRef from start_id reaches target_id."""
    seen = set()
    current = start_id
    while current and current not in seen:
        if current == target_id:
            return True
        seen.add(current)
        current = parent_of.get(current)
    return False


def _ensure_unassigned_department(dest_cursor):
    """Department id used only so post nodes without a unit can be inserted."""
    dest_cursor.execute(
        "SELECT DepartmentID FROM HCM3.Department WHERE Code = ?",
        (NO_DEPT_CODE,),
    )
    row = dest_cursor.fetchone()
    if row:
        return int(row[0])

    last_id = ensure_table_id(dest_cursor, 'HCM3.Department', 0) + 1
    dest_cursor.execute(
        """
        INSERT INTO HCM3.Department (
            DepartmentID, Code, UniqueCode, Title, AbbrSign, RegionalDivisionRef, Status,
            CreationDate, Creator, LastModificationDate, LastModifier
        ) VALUES (?, ?, ?, ?, ?, NULL, 1, GETDATE(), 1, GETDATE(), 1)
        """,
        (last_id, NO_DEPT_CODE, NO_DEPT_CODE, NO_DEPT_TITLE, NO_DEPT_CODE),
    )
    dest_cursor.execute(
        "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = 'HCM3.Department'",
        (last_id,),
    )
    print(f"  -> Technical department for posts with no unit: {last_id}.")
    return last_id


def run():
    print("\n--- Running Step 10: Organizational Structure Migration (rebuild) ---")

    managers_path = department_managers_path()
    print(f"Reading department managers from {managers_path}...")
    managers_by_oc = load_approved_managers(managers_path)
    print(
        "  -> Approved manager posts: "
        f"{sum(len(v) for v in managers_by_oc.values())} "
        f"across {len(managers_by_oc)} chart(s)."
    )

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
        unassigned_dept_id = _ensure_unassigned_department(dest_cursor)

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

        post_nodes = 0
        dept_nodes = 0
        items_inserted = 0
        descs_inserted = 0
        skipped_no_post = 0
        no_dept_posts = 0
        posts_from_manager = 0
        managers_skipped = 0
        parents_linked = 0
        parents_skipped_cycle = 0

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

            oc_posts = posts_df[posts_df['SourceOcID'] == oc_id]
            post_local = {}  # SourcePostID -> DestOrganizationalStructureID
            pending_parent = []  # (dest_os_id, source_post_id, source_parent_post)
            unassigned_posts = set()

            for _, prow in oc_posts.iterrows():
                source_post_id = int(prow['SourcePostID'])
                dest_post = post_map.get(source_post_id)
                if not dest_post:
                    skipped_no_post += 1
                    continue

                source_dept = _positive_fk(prow['SourceDepartmentID'])
                dest_dept = dept_map.get(source_dept) if source_dept else None
                if not dest_dept:
                    dest_dept = unassigned_dept_id
                    unassigned_posts.add(source_post_id)

                structure_last_id += 1
                dest_cursor.execute(
                    insert_os_sql,
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
                    insert_map_sql,
                    (oc_id, NODE_POST, source_post_id, structure_last_id),
                )
                post_local[source_post_id] = structure_last_id
                source_parent_post = _positive_fk(prow['SourceParentPostID'])
                if source_parent_post and source_parent_post != source_post_id:
                    pending_parent.append(
                        (structure_last_id, source_post_id, source_parent_post)
                    )
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

            # ParentRef is the source parent post on this same chart.
            # A link that would cycle is left null.
            parent_of = {os_id: None for os_id in post_local.values()}
            child_to_parent = {}
            oc_parents_linked = 0
            for dest_os_id, source_post_id, source_parent_post in pending_parent:
                parent_os = post_local.get(source_parent_post)
                if not parent_os:
                    continue
                if _chain_reaches(parent_of, parent_os, dest_os_id):
                    parents_skipped_cycle += 1
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
                parent_of[dest_os_id] = parent_os
                child_to_parent[source_post_id] = source_parent_post
                oc_parents_linked += 1
                parents_linked += 1
            if oc_parents_linked:
                print(f"    -> Post parents linked: {oc_parents_linked}.")

            # DepartmentRef from the reviewed manager list, then a department
            # folder under each manager post.
            oc_managers = {
                post_id: dept_id
                for post_id, dept_id in managers_by_oc.get(oc_id, {}).items()
                if post_id in post_local and dept_map.get(dept_id)
            }
            for post_id, dept_id in managers_by_oc.get(oc_id, {}).items():
                if post_id not in post_local or not dept_map.get(dept_id):
                    managers_skipped += 1
            assigned = assign_departments_under_managers(child_to_parent, oc_managers)
            oc_from_manager = 0
            for source_post_id, source_dept in assigned.items():
                dest_os = post_local.get(source_post_id)
                dest_dept = dept_map.get(source_dept)
                if not dest_os or not dest_dept:
                    continue
                unassigned_posts.discard(source_post_id)
                dest_cursor.execute(
                    """
                    UPDATE HCM3.OrganizationalStructure
                    SET DepartmentRef = ?
                    WHERE OrganizationalStructureID = ?
                    """,
                    (dest_dept, dest_os),
                )
                oc_from_manager += 1
                posts_from_manager += 1

            dept_os_by_manager = {}
            for manager_post_id, source_dept in oc_managers.items():
                structure_last_id += 1
                dest_cursor.execute(
                    insert_os_sql,
                    (
                        structure_last_id,
                        dept_map[source_dept],
                        None,
                        post_local[manager_post_id],
                        insertion,
                        deletion,
                    ),
                )
                dest_cursor.execute(
                    insert_map_sql,
                    (oc_id, NODE_DEPT, source_dept, structure_last_id),
                )
                dept_os_by_manager[manager_post_id] = structure_last_id
                dept_nodes += 1

            # Posts that reported to the manager and belong to his unit
            # move under the department node. Deeper posts keep their
            # post parent, so the chain stays inside that folder.
            # A post that is itself a manager of another unit stays under
            # the manager post, beside the folder.
            moved_under_dept = 0
            for source_post_id, source_dept in assigned.items():
                parent_post = child_to_parent.get(source_post_id)
                if parent_post not in dept_os_by_manager:
                    continue
                if oc_managers.get(parent_post) != source_dept:
                    continue
                dest_cursor.execute(
                    """
                    UPDATE HCM3.OrganizationalStructure
                    SET ParentRef = ?
                    WHERE OrganizationalStructureID = ?
                    """,
                    (dept_os_by_manager[parent_post], post_local[source_post_id]),
                )
                moved_under_dept += 1
            no_dept_posts += len(unassigned_posts)
            if oc_managers:
                print(
                    f"    -> Manager units: {len(oc_managers)} "
                    f"department nodes, posts in those units: {oc_from_manager}, "
                    f"moved directly under the department node: {moved_under_dept}."
                )

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
            f"Success! Post nodes: {post_nodes}, "
            f"Department nodes: {dept_nodes}, "
            f"Items: {items_inserted}, Descriptions: {descs_inserted}. "
            f"Posts placed by manager list: {posts_from_manager}. "
            f"Post parents linked: {parents_linked}. "
            f"Parent links skipped (cycle): {parents_skipped_cycle}. "
            f"Skipped (no post master): {skipped_no_post}. "
            f"Manager rows skipped: {managers_skipped}. "
            f"Posts still without a department: {no_dept_posts}."
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
