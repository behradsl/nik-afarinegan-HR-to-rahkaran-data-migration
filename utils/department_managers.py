"""
Unit managers for each organization-chart version.

The source has no department-manager field. A reviewed CSV names, per chart,
the post that manages each department. Step 10 reads that file and does not
guess at runtime.

Rows are proposed by build_manager_rows:
  - the post job grade is a unit-head grade (مدیر کل / مدیر عامل / مدیر دفتر /
    مدیر امور / رئیس اداره / رئیس گروه / رئیس / معاون / مجری طرح)
  - the longest department name is contained in the post title after
    comparison cleanup: spaces removed, and معاونت treated as معاون, so
    «مدیر عامل» matches «مدیرعامل» and «معاونت منابع انسانی» matches
    «معاون منابع انسانی»
  - when several departments share that name, the post's own department id
    picks the one whose parent department is that id
  - a department or a post that still matches more than one row is left as
    review and is not applied
"""
import csv
import os
import sys
from collections import defaultdict

import pandas as pd

from utils.data_helpers import normalize_persian

CSV_NAME = 'department_managers.csv'
# TBL_JobGrade ids that head a unit. Advisors and مسئول desks are excluded.
# معاون (102) heads a معاونت. مجری طرح (105) heads a unit named مجری طرح.
HEAD_JOB_GRADE_IDS = (2, 101, 102, 103, 104, 105, 201, 202, 206)
MIN_DEPT_NAME_LEN = 6

CSV_FIELDS = (
    'SourceOcID',
    'SourceDepartmentID',
    'ManagerPostID',
    'PostTitle',
    'DepartmentName',
    'JobGradeId',
    'JobGradeName',
    'Status',
)


def department_managers_path():
    """CSV beside the exe when frozen, otherwise the project root."""
    if getattr(sys, 'frozen', False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, CSV_NAME)


def _norm(text):
    if text is None or (isinstance(text, float) and pd.isna(text)):
        return ''
    return normalize_persian(str(text).strip()) or ''


def _compare_key(text):
    """Spelling-insensitive key: no spaces, معاونت collapsed to معاون."""
    key = _norm(text).replace('معاونت', 'معاون')
    return ''.join(key.split())


def assign_departments_under_managers(child_to_parent, managers):
    """
    managers: {manager SourcePostID: SourceDepartmentID}
    child_to_parent: {child SourcePostID: parent SourcePostID} for links that
    were actually stored.

    The manager post and every post below him take his department, until the
    walk meets another manager post. That next manager keeps his own unit.
    """
    children = defaultdict(list)
    for child, parent in child_to_parent.items():
        if parent:
            children[parent].append(child)

    manager_posts = set(managers)
    assigned = {}
    for manager_id, dept_id in managers.items():
        assigned[manager_id] = dept_id
        stack = list(children.get(manager_id, []))
        while stack:
            node = stack.pop()
            if node in manager_posts:
                continue
            assigned[node] = dept_id
            stack.extend(children.get(node, []))
    return assigned


def _disambiguate(candidates, post_dept_id):
    if len(candidates) <= 1:
        return candidates
    if post_dept_id:
        by_parent = [c for c in candidates if c['parent'] == post_dept_id]
        if len(by_parent) == 1:
            return by_parent
        by_self = [c for c in candidates if c['id'] == post_dept_id]
        if len(by_self) == 1:
            return by_self
    return candidates


def build_manager_rows(source_cnxn):
    """Propose manager rows from source posts and departments."""
    posts = pd.read_sql("""
        SELECT
            p.TBL_PostID AS SourcePostID,
            p.TBL_OcID_fk AS SourceOcID,
            p.TBL_PostTitle AS PostTitle,
            p.TBL_DepartmentID_fk AS SourceDepartmentID,
            p.TBL_JgID_fk AS JobGradeId,
            g.TBL_JgDescription AS JobGradeName
        FROM dbo.TBL_Post p
        LEFT JOIN dbo.TBL_JobGrade g ON g.TBL_JgID = p.TBL_JgID_fk
        WHERE p.TBL_PostID > 0
          AND p.TBL_OcID_fk > 0
          AND p.TBL_JgID_fk IN ({})
    """.format(','.join(str(i) for i in HEAD_JOB_GRADE_IDS)), source_cnxn)

    depts = pd.read_sql("""
        SELECT
            TBL_DepartmentID AS SourceDepartmentID,
            TBL_DepartmentName AS DepartmentName,
            TBL_DepartmentParentID_fk AS ParentDepartmentID
        FROM dbo.TBL_Department
        WHERE TBL_DepartmentID > 0
    """, source_cnxn)

    by_name = defaultdict(list)
    for _, row in depts.iterrows():
        name = _norm(row['DepartmentName'])
        key = _compare_key(row['DepartmentName'])
        if len(name) < MIN_DEPT_NAME_LEN or not key:
            continue
        parent = row['ParentDepartmentID']
        try:
            parent = int(parent) if pd.notna(parent) else 0
        except (TypeError, ValueError):
            parent = 0
        by_name[key].append({
            'id': int(row['SourceDepartmentID']),
            'name': str(row['DepartmentName']).strip(),
            'parent': parent,
        })
    names_longest_first = sorted(by_name, key=len, reverse=True)

    rows = []
    for _, post in posts.iterrows():
        title_key = _compare_key(post['PostTitle'])
        if not title_key:
            continue
        matched = next((n for n in names_longest_first if n in title_key), None)
        if not matched:
            continue
        post_dept = post['SourceDepartmentID']
        try:
            post_dept = int(post_dept) if pd.notna(post_dept) else 0
        except (TypeError, ValueError):
            post_dept = 0
        chosen = _disambiguate(by_name[matched], post_dept)
        grade_id = post['JobGradeId']
        try:
            grade_id = int(grade_id) if pd.notna(grade_id) else ''
        except (TypeError, ValueError):
            grade_id = ''
        grade_name = '' if pd.isna(post['JobGradeName']) else str(post['JobGradeName']).strip()
        title = '' if pd.isna(post['PostTitle']) else str(post['PostTitle']).strip()
        status = 'approved' if len(chosen) == 1 else 'review'
        for cand in chosen:
            rows.append({
                'SourceOcID': int(post['SourceOcID']),
                'SourceDepartmentID': cand['id'],
                'ManagerPostID': int(post['SourcePostID']),
                'PostTitle': title,
                'DepartmentName': cand['name'],
                'JobGradeId': grade_id,
                'JobGradeName': grade_name,
                'Status': status,
            })

    _demote_conflicts(rows)
    rows.sort(key=lambda r: (
        r['SourceOcID'],
        r['DepartmentName'],
        r['ManagerPostID'],
        r['SourceDepartmentID'],
    ))
    return rows


def _demote_conflicts(rows):
    """A chart may have only one approved manager per department and per post."""
    approved = [r for r in rows if r['Status'] == 'approved']
    dept_counts = defaultdict(int)
    post_counts = defaultdict(int)
    for row in approved:
        dept_counts[(row['SourceOcID'], row['SourceDepartmentID'])] += 1
        post_counts[(row['SourceOcID'], row['ManagerPostID'])] += 1
    for row in approved:
        if dept_counts[(row['SourceOcID'], row['SourceDepartmentID'])] > 1:
            row['Status'] = 'review'
        elif post_counts[(row['SourceOcID'], row['ManagerPostID'])] > 1:
            row['Status'] = 'review'


def write_manager_csv(path, rows):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def load_approved_managers(path):
    """
    Returns {SourceOcID: {ManagerPostID: SourceDepartmentID}} for Status=approved.
    The first row wins if the file still contains a duplicate.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Department manager list not found at {path}. "
            f"Place {CSV_NAME} next to the program (or run build_department_managers.py)."
        )
    frame = pd.read_csv(path, encoding='utf-8-sig')
    missing = [col for col in ('SourceOcID', 'SourceDepartmentID', 'ManagerPostID', 'Status') if col not in frame.columns]
    if missing:
        raise ValueError(f"{path} is missing column(s): {', '.join(missing)}")

    approved = frame[frame['Status'].astype(str).str.strip().str.lower() == 'approved']
    by_oc = {}
    seen_depts = defaultdict(set)
    for _, row in approved.iterrows():
        oc_id = int(row['SourceOcID'])
        post_id = int(row['ManagerPostID'])
        dept_id = int(row['SourceDepartmentID'])
        bucket = by_oc.setdefault(oc_id, {})
        if post_id in bucket:
            print(
                f"  -> Manager list: chart {oc_id} post {post_id} is repeated. "
                f"Keeping department {bucket[post_id]}."
            )
            continue
        if dept_id in seen_depts[oc_id]:
            print(
                f"  -> Manager list: chart {oc_id} department {dept_id} is repeated. "
                f"Skipping post {post_id}."
            )
            continue
        bucket[post_id] = dept_id
        seen_depts[oc_id].add(dept_id)
    return by_oc
