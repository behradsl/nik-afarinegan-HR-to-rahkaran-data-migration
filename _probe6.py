import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== Search توانیر in PayBase/extra ===")
print(pd.read_sql("""
SELECT HRS_PayBaseID, HRS_PayBaseParentID_fk, HRS_PayBaseName
FROM dbo.HRS_PayBase
WHERE HRS_PayBaseName LIKE N'%توانیر%' OR HRS_PayBaseName LIKE N'%توانير%'
   OR HRS_PayBaseName LIKE N'%کد سازمانی%' OR HRS_PayBaseName LIKE N'%کد سازمان%'
""", src).to_string(index=False))

print("\n=== PostOrganizationPostCode fill ===")
print(pd.read_sql("""
SELECT
 COUNT(*) total,
 SUM(CASE WHEN TBL_PostOrganazationPostCode IS NOT NULL AND LTRIM(RTRIM(TBL_PostOrganazationPostCode)) NOT IN ('','0') THEN 1 ELSE 0 END) filled
FROM dbo.TBL_Post WHERE TBL_PostID>0
""", src).to_string(index=False))
print(pd.read_sql("""
SELECT TOP 10 TBL_PostID, TBL_PostTitle, TBL_PostCode, TBL_PostOrganazationPostCode, TBL_JobID_fk
FROM dbo.TBL_Post WHERE TBL_PostID>0
 AND TBL_PostOrganazationPostCode IS NOT NULL AND LTRIM(RTRIM(TBL_PostOrganazationPostCode)) NOT IN ('','0')
""", src).to_string(index=False))

print("\n=== Sample Extra 9505 / 9513 values ===")
print(pd.read_sql("""
SELECT TOP 8 pei.HRS_PayBaseID_fk, pb.HRS_PayBaseName, pei.TBL_PeiValue
FROM dbo.TBL_PostExtraInfo pei
JOIN dbo.HRS_PayBase pb ON pb.HRS_PayBaseID=pei.HRS_PayBaseID_fk
WHERE pei.HRS_PayBaseID_fk IN (9505,9513,9540)
""", src).to_string(index=False))

print("\n=== Zone table ===")
print(pd.read_sql("""
SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME='TBL_Zone' ORDER BY ORDINAL_POSITION
""", src).to_string(index=False))
print(pd.read_sql("SELECT TOP 15 * FROM dbo.TBL_Zone", src).to_string(index=False))

print("\n=== RegionalDivisionType lookup ===")
print(pd.read_sql("SELECT Code, Value FROM SYS3.Lookup WHERE Type=N'RegionalDivisionType' ORDER BY Code", dst).to_string(index=False))

print("\n=== RdStatus meaning guess via EndDate/Active ===")
print(pd.read_sql("""
SELECT HRS_RdStatus, HRS_RdActive,
  SUM(CASE WHEN HRS_RdEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdEndDate)) NOT IN ('','0','1499/12/29') THEN 1 ELSE 0 END) end_filled,
  SUM(CASE WHEN HRS_RdContractEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdContractEndDate)) NOT IN ('','0','1499/12/29') THEN 1 ELSE 0 END) contract_filled,
  COUNT(*) cnt
FROM dbo.HRS_RuleDocument
GROUP BY HRS_RdStatus, HRS_RdActive
ORDER BY HRS_RdStatus, HRS_RdActive
""", src).to_string(index=False))
