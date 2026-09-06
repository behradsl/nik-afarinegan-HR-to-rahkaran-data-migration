import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== TBL_PostExtraInfo cols ===")
print(pd.read_sql("""
SELECT COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='TBL_PostExtraInfo' ORDER BY ORDINAL_POSITION
""", src).to_string(index=False))

print("\n=== Sample PostExtraInfo ===")
print(pd.read_sql("SELECT TOP 5 * FROM dbo.TBL_PostExtraInfo", src).to_string(index=False))

print("\n=== HRS_CMSLJobRankMatches cols ===")
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_NAME='HRS_CMSLJobRankMatches' ORDER BY ORDINAL_POSITION
""", src).to_string(index=False))

print("\n=== TBL_JobGrade sample ===")
print(pd.read_sql("SELECT TOP 10 * FROM dbo.TBL_JobGrade", src).to_string(index=False))

print("\n=== RuleDocument Status/Type counts ===")
print(pd.read_sql("""
SELECT HRS_RdStatus, HRS_RdType, HRS_RdActive, COUNT(*) cnt
FROM dbo.HRS_RuleDocument
GROUP BY HRS_RdStatus, HRS_RdType, HRS_RdActive
ORDER BY cnt DESC
""", src).to_string(index=False))

print("\n=== ContractEndDate vs EndDate fill ===")
print(pd.read_sql("""
SELECT
  SUM(CASE WHEN HRS_RdEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdEndDate)) NOT IN ('','0','1499/12/29') THEN 1 ELSE 0 END) EndDateFilled,
  SUM(CASE WHEN HRS_RdContractEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdContractEndDate)) NOT IN ('','0','1499/12/29') THEN 1 ELSE 0 END) ContractEndFilled,
  COUNT(*) Total
FROM dbo.HRS_RuleDocument
""", src).to_string(index=False))

print("\n=== Post Extra lookups exist? ===")
print(pd.read_sql("""
SELECT Type, Code, Value FROM SYS3.Lookup
WHERE Type LIKE N'%Post%Extra%' OR Type IN (N'PostExtra1', N'PostExtra2', N'Extra1', N'Extra2')
ORDER BY Type, Code
""", dst).to_string(index=False))

print("\n=== Dest Post Extra1/2 fill ===")
print(pd.read_sql("""
SELECT
  COUNT(*) total,
  SUM(CASE WHEN Extra1Code IS NOT NULL THEN 1 ELSE 0 END) e1,
  SUM(CASE WHEN Extra2Code IS NOT NULL THEN 1 ELSE 0 END) e2,
  SUM(CASE WHEN UniqueCode IS NOT NULL THEN 1 ELSE 0 END) uc,
  SUM(CASE WHEN RegionalDivisionTypeCode IS NOT NULL THEN 1 ELSE 0 END) rdt
FROM HCM3.Post
""", dst).to_string(index=False))

print("\n=== Dest Dept UniqueCode/AbbrSign ===")
print(pd.read_sql("""
SELECT
  COUNT(*) total,
  SUM(CASE WHEN UniqueCode IS NOT NULL AND UniqueCode<>'' THEN 1 ELSE 0 END) uc,
  SUM(CASE WHEN AbbrSign IS NOT NULL AND AbbrSign<>'' THEN 1 ELSE 0 END) abbr,
  SUM(CASE WHEN Code IS NOT NULL THEN 1 ELSE 0 END) code
FROM HCM3.Department
""", dst).to_string(index=False))

print("\n=== PostJob count ===")
print(pd.read_sql("SELECT COUNT(*) cnt FROM HCM3.PostJob", dst).to_string(index=False))

print("\n=== Job mapping vs source ===")
print(pd.read_sql("SELECT COUNT(*) src FROM dbo.TBL_Job WHERE TBL_JobID>0", src).to_string(index=False))
print(pd.read_sql("SELECT COUNT(*) mapped FROM master.dbo.JobMigrationMapping", dst).to_string(index=False))
print(pd.read_sql("SELECT COUNT(*) dest FROM HCM3.Job", dst).to_string(index=False))
