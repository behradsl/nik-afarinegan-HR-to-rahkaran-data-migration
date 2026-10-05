# -*- mode: python ; coding: utf-8 -*-
"""
Onefile build for RahkaranMigrator.

Heavy ML/science packages may be installed in the developer Python env but are
NOT used by the migrator. Excluding them shrinks the EXE without changing any
migration step logic (including step 17 RollFormula / UDF property migration).

Required runtime stack stays: pandas, numpy, pyodbc, jdatetime, pyodbc drivers.
"""

# Never imported by main.py / steps / utils — safe to drop from the bundle.
EXCLUDES = [
    # Deep learning / vision (main size offenders)
    'torch',
    'torchvision',
    'torchaudio',
    'torchtext',
    'tensorflow',
    'tensorboard',
    'keras',
    # Heavy science / optional analytics
    'scipy',
    'sklearn',
    'scikit-learn',
    'sympy',
    'numba',
    'llvmlite',
    'cupy',
    'jax',
    'matplotlib',
    'mpl_toolkits',
    'seaborn',
    'plotly',
    'bokeh',
    'statsmodels',
    # Notebook / IDE
    'IPython',
    'ipykernel',
    'jupyter',
    'notebook',
    'nbformat',
    'nbconvert',
    # Unused DB drivers (sqlalchemy optional backends)
    'MySQLdb',
    'psycopg2',
    'pymysql',
    'asyncpg',
    'pymongo',
    # redis-py unused — cache invalidation uses a tiny TCP client in utils/
    'redis',
]

# Ensure step modules and formula helpers are collected even if imports are dynamic.
HIDDENIMPORTS = [
    'steps.step_00_cleanup',
    'steps.step_01_party',
    'steps.step_02_employee',
    'steps.step_03_education',
    'steps.step_04_military',
    'steps.step_05_relatives',
    'steps.step_06_training',
    'steps.step_07_work_record',
    'steps.step_08_warrior_record',
    'steps.step_09_org_masters',
    'steps.step_10_org_structure',
    'steps.step_11_employee_statute',
    'steps.step_12_work_record_from_statute',
    'steps.step_13_employee_research',
    'steps.step_14_employee_reward_punish',
    'steps.step_15_employee_appraisal',
    'steps.step_16_party_address',
    'steps.step_17_statute_factor',
    'steps.step_18_employee_photo',
    'steps.step_19_service_leakage_work_record',
    'steps.step_20_employment_number',
    'steps.step_21_employee_statute_factor',
    'utils.roll_formula_properties',
    'utils.formula_break',
    'utils.org_migration',
    'utils.department_managers',
    'utils.data_helpers',
    'utils.date_helpers',
    'utils.lookup_helpers',
    'utils.hcm_extra_settings',
    'utils.rahkaran_cache',
    'jdatetime',
    'pyodbc',
    'pandas',
]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('utils/formula_uiobject_return0.bin', 'utils'),
    ],
    hiddenimports=HIDDENIMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='RahkaranMigrator',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
