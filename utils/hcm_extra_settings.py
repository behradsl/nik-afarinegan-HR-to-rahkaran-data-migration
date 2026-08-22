"""
Rahkaran HCM general-settings for Extra fields.

Titles and visibility are stored under SYS3.ConfigurationGroup paths such as:
  systemgroup.hcm.staff\\WorkRecordExtra1Title
  systemgroup.hcm.staff\\ShowWorkRecordExtra1
  systemgroup.hcm.organization\\PostExtra1Title
  systemgroup.hcm.organization\\ShowPostExtra1

Values live in SYS3.ConfigurationValue with Key='Admin' (Scope=2).
LookupInfo.Title is also updated so lookup screens show the same label.
"""
import html
from typing import Optional
from utils.lookup_helpers import ensure_lookup_info

CONFIG_VALUE_IDGEN = 'SYS3.ConfigurationValue'

_BOOL_TRUE_XML = (
    '<Data Type="System.Boolean, mscorlib">'
    '<Value>&lt;boolean&gt;true&lt;/boolean&gt;</Value></Data>'
)
_BOOL_FALSE_XML = (
    '<Data Type="System.Boolean, mscorlib">'
    '<Value>&lt;boolean&gt;false&lt;/boolean&gt;</Value></Data>'
)


def _string_config_xml(title: str) -> str:
    # Inner payload is XML-escaped so <string>text</string> survives as text nodes.
    safe = html.escape(title or '', quote=False)
    return (
        '<Data Type="System.String, mscorlib">'
        f'<Value>&lt;string&gt;{safe}&lt;/string&gt;</Value></Data>'
    )


def _get_group_id(dest_cursor, path: str):
    dest_cursor.execute(
        "SELECT ConfigurationGroupID FROM SYS3.ConfigurationGroup WHERE Path = ?",
        (path,),
    )
    row = dest_cursor.fetchone()
    return int(row[0]) if row else None


def _upsert_admin_config_value(dest_cursor, group_id: int, data_xml: str):
    dest_cursor.execute(
        """
        SELECT ConfigurationValueID, Data
        FROM SYS3.ConfigurationValue
        WHERE ConfigurationGroupRef = ? AND [Key] = N'Admin'
        """,
        (group_id,),
    )
    row = dest_cursor.fetchone()
    if row:
        existing = row[1]
        existing_text = (
            existing.decode('utf-8', errors='ignore')
            if isinstance(existing, (bytes, bytearray, memoryview))
            else (str(existing) if existing is not None else '')
        )
        if existing_text == data_xml:
            return False
        dest_cursor.execute(
            """
            UPDATE SYS3.ConfigurationValue
            SET Data = ?
            WHERE ConfigurationValueID = ?
            """,
            (data_xml, int(row[0])),
        )
        return True

    dest_cursor.execute(
        """
        SELECT LastId
        FROM SYS3.tableIdGen WITH (UPDLOCK, HOLDLOCK)
        WHERE TableName = ?
        """,
        (CONFIG_VALUE_IDGEN,),
    )
    id_row = dest_cursor.fetchone()
    last_id = int(id_row[0]) if id_row else 0
    last_id += 1
    dest_cursor.execute(
        """
        INSERT INTO SYS3.ConfigurationValue (
            ConfigurationValueID, UserRef, CompanyRef, Scope, [Key], Data, ConfigurationGroupRef
        ) VALUES (?, NULL, NULL, 2, N'Admin', ?, ?)
        """,
        (last_id, data_xml, group_id),
    )
    if id_row:
        dest_cursor.execute(
            "UPDATE SYS3.tableIdGen SET LastId = ? WHERE TableName = ?",
            (last_id, CONFIG_VALUE_IDGEN),
        )
    else:
        dest_cursor.execute(
            "INSERT INTO SYS3.tableIdGen (TableName, LastId) VALUES (?, ?)",
            (CONFIG_VALUE_IDGEN, last_id),
        )
    return True


def ensure_hcm_extra_field(
    dest_cursor,
    *,
    lookup_type: str,
    title: str,
    title_path: Optional[str] = None,
    show_path: Optional[str] = None,
    visible: bool = True,
):
    """
    Make an Extra field visible in HCM general settings and assign its title.
    Also syncs SYS3.LookupInfo.Title for the matching lookup type.
    """
    ensure_lookup_info(dest_cursor, lookup_type, title)

    changed = []
    if title_path:
        group_id = _get_group_id(dest_cursor, title_path)
        if group_id is None:
            print(f"  -> Config path not found (skip title): {title_path}")
        elif _upsert_admin_config_value(dest_cursor, group_id, _string_config_xml(title)):
            changed.append(f"title={title}")

    if show_path:
        group_id = _get_group_id(dest_cursor, show_path)
        if group_id is None:
            print(f"  -> Config path not found (skip visibility): {show_path}")
        else:
            xml = _BOOL_TRUE_XML if visible else _BOOL_FALSE_XML
            if _upsert_admin_config_value(dest_cursor, group_id, xml):
                changed.append(f"visible={visible}")

    if changed:
        print(f"  -> HCM Extra '{lookup_type}' settings: {', '.join(changed)}")
    return True


# Canonical extras used by this migration (lookup type + settings paths + title)
HCM_EXTRA_FIELD_SPECS = {
    'PostExtra1': {
        'title': 'کد توانیر',
        'title_path': r'systemgroup.hcm.organization\PostExtra1Title',
        'show_path': r'systemgroup.hcm.organization\ShowPostExtra1',
    },
    'PostExtra2': {
        'title': 'شناسه HRS',
        'title_path': r'systemgroup.hcm.organization\PostExtra2Title',
        'show_path': r'systemgroup.hcm.organization\ShowPostExtra2',
    },
    'WorkRecordExtra1': {
        'title': 'وضعیت فعال بودن',
        'title_path': r'systemgroup.hcm.staff\WorkRecordExtra1Title',
        'show_path': r'systemgroup.hcm.staff\ShowWorkRecordExtra1',
    },
    'WorkRecordExtra2': {
        'title': 'حق سرپرستی',
        'title_path': r'systemgroup.hcm.staff\WorkRecordExtra2Title',
        'show_path': r'systemgroup.hcm.staff\ShowWorkRecordExtra2',
    },
    'EmployeeTrainingExtra1': {
        'title': 'تعلق امتیاز',
        'title_path': r'systemgroup.hcm.staff\TrainingExtra1Title',
        'show_path': r'systemgroup.hcm.staff\ShowTrainingExtra1',
    },
    'EmployeeTrainingExtra2': {
        'title': 'وضعیت آموزش',
        'title_path': r'systemgroup.hcm.staff\TrainingExtra2Title',
        'show_path': r'systemgroup.hcm.staff\ShowTrainingExtra2',
    },
    # No Show/Title config groups for relatives Extra — LookupInfo title only
    'EmployeeRelativeExtra1': {
        'title': 'علت ایجاد تحت تکفل',
        'title_path': None,
        'show_path': None,
    },
}


def ensure_hcm_extra_fields(dest_cursor, lookup_types):
    """Ensure title + visibility for each lookup type in lookup_types."""
    for lookup_type in lookup_types:
        spec = HCM_EXTRA_FIELD_SPECS.get(lookup_type)
        if not spec:
            print(f"  -> No HCM Extra settings spec for {lookup_type}")
            continue
        ensure_hcm_extra_field(
            dest_cursor,
            lookup_type=lookup_type,
            title=spec['title'],
            title_path=spec.get('title_path'),
            show_path=spec.get('show_path'),
            visible=True,
        )
