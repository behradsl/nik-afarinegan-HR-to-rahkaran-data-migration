"""Write department_managers.csv from source post titles and head-level job grades."""
from db_core import get_connections
from utils.department_managers import (
    build_manager_rows,
    department_managers_path,
    write_manager_csv,
)


def main():
    source_cnxn, dest_cnxn = get_connections()
    try:
        rows = build_manager_rows(source_cnxn)
        path = department_managers_path()
        write_manager_csv(path, rows)
        approved = sum(1 for row in rows if row['Status'] == 'approved')
        review = sum(1 for row in rows if row['Status'] == 'review')
        print(f"Wrote {path}")
        print(f"  approved: {approved}")
        print(f"  review (not applied until Status is approved): {review}")
    finally:
        source_cnxn.close()
        dest_cnxn.close()


if __name__ == '__main__':
    main()
