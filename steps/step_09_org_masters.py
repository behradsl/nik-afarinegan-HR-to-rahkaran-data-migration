"""Step 9: Full org masters — Department, Post, Job, EmploymentType, WorkLocation, Rank."""
import pandas as pd
import warnings
from db_core import get_connections
from utils.org_migration import (
    ensure_departments,
    ensure_employment_types,
    ensure_jobs,
    ensure_places_as_work_locations,
    ensure_post_jobs,
    ensure_posts,
    ensure_rank_codes_from_grades,
)

warnings.filterwarnings('ignore', category=UserWarning)


def run():
    print("\n--- Running Step 9: Organization Masters Migration ---")

    source_cnxn, dest_cnxn = get_connections()
    dest_cursor = dest_cnxn.cursor()

    try:
        print("Migrating all Departments...")
        ensure_departments(source_cnxn, dest_cnxn, dest_cursor)

        print("Migrating all Posts...")
        post_map = ensure_posts(source_cnxn, dest_cnxn, dest_cursor)

        print("Migrating all Jobs...")
        job_map = ensure_jobs(source_cnxn, dest_cnxn, dest_cursor)

        print("Linking Post → Job (PostJob)...")
        ensure_post_jobs(
            source_cnxn, dest_cnxn, dest_cursor, post_map=post_map, job_map=job_map
        )

        print("Migrating Employment Types...")
        ensure_employment_types(source_cnxn, dest_cnxn, dest_cursor)

        print("Migrating Places → WorkLocation lookups...")
        ensure_places_as_work_locations(source_cnxn, dest_cnxn, dest_cursor)

        print("Ensuring Rank codes from RuleDocument personal grades...")
        grades_df = pd.read_sql("""
            SELECT DISTINCT HRS_RdPersonalGrade AS Grade
            FROM dbo.HRS_RuleDocument
            WHERE HRS_RdPersonalGrade IS NOT NULL
              AND HRS_RdPersonalGrade > 0
        """, source_cnxn)
        ensure_rank_codes_from_grades(
            dest_cnxn,
            dest_cursor,
            grades_df['Grade'].tolist() if not grades_df.empty else [],
        )

        dest_cnxn.commit()
        print("Success! Organization masters migrated.")

    except Exception as e:
        dest_cnxn.rollback()
        print(f"Migration failed during Organization Masters step. Transaction rolled back. Error: {e}")
        raise e
    finally:
        source_cnxn.close()
        dest_cnxn.close()
