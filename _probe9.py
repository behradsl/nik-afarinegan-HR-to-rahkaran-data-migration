import json, pyodbc, pandas as pd, warnings
warnings.filterwarnings("ignore")
cfg=json.load(open(r"d:\workspace\nik-afarinegan-HR-to-rahkaran-convert\config.json", encoding="utf-8"))
src=pyodbc.connect(cfg["source_conn"]); dst=pyodbc.connect(cfg["dest_conn"])

print("=== PayBase 70000203 / geographic domain ===")
print(pd.read_sql("""
SELECT HRS_PayBaseID, HRS_PayBaseParentID_fk, HRS_PayBaseName
FROM dbo.HRS_PayBase
WHERE HRS_PayBaseID IN (70000203,700002,70000303,70000103,70,7000,70000200)
   OR HRS_PayBaseParentID_fk IN (70,7000,700002,700001,700003)
   OR HRS_PayBaseName LIKE N'%حوزه جغراف%'
ORDER BY HRS_PayBaseID
""", src).to_string(index=False))

# parent chain for 70000203
ids=[70000203]
for _ in range(5):
  df=pd.read_sql(f"SELECT HRS_PayBaseID, HRS_PayBaseParentID_fk, HRS_PayBaseName FROM dbo.HRS_PayBase WHERE HRS_PayBaseID IN ({','.join(map(str,ids))})", src)
  print(df.to_string(index=False))
  parents=[int(x) for x in df['HRS_PayBaseParentID_fk'].dropna().tolist() if int(x)>0]
  if not parents: break
  ids=parents

print("\n=== ZoneID on Post vs Extra 9513 ===")
print(pd.read_sql("""
SELECT TOP 5 p.TBL_PostID, p.TBL_ZoneID_fk, z.TBL_ZoneName, pei.TBL_PeiValue
FROM dbo.TBL_Post p
LEFT JOIN dbo.TBL_Zone z ON z.TBL_ZoneID=p.TBL_ZoneID_fk
LEFT JOIN dbo.TBL_PostExtraInfo pei ON pei.TBL_PostID_fk=p.TBL_PostID AND pei.HRS_PayBaseID_fk=9513
WHERE p.TBL_PostID>0
""", src).to_string(index=False))

print("\n=== Expiry analysis archive vs current ===")
# Status 0 vs 3 - check delete date / end
print(pd.read_sql("""
SELECT HRS_RdStatus,
  AVG(CASE WHEN HRS_RdEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdEndDate)) NOT IN ('','0','1499/12/29') THEN 1.0 ELSE 0 END) pct_end,
  AVG(CASE WHEN HRS_RdContractEndDate IS NOT NULL AND LTRIM(RTRIM(HRS_RdContractEndDate)) NOT IN ('','0','1499/12/29') THEN 1.0 ELSE 0 END) pct_contract,
  AVG(CASE WHEN HRS_RdDeleteDate IS NOT NULL AND LTRIM(RTRIM(CAST(HRS_RdDeleteDate AS nvarchar(30)))) NOT IN ('','0') THEN 1.0 ELSE 0 END) pct_deleted
FROM dbo.HRS_RuleDocument
GROUP BY HRS_RdStatus
""", src).to_string(index=False))

print("\n=== Dest statute expiry fill ===")
print(pd.read_sql("""
SELECT
 COUNT(*) total,
 SUM(CASE WHEN ExpiryDate IS NOT NULL THEN 1 ELSE 0 END) with_exp,
 SUM(CASE WHEN ExpiryDate IS NULL THEN 1 ELSE 0 END) null_exp
FROM HCM3.EmployeeStatute
""", dst).to_string(index=False))

print("\n=== Compare EndDate vs ContractEnd for status0 active ===")
print(pd.read_sql("""
SELECT TOP 8 HRS_RdID, HRS_RdStatus, HRS_RdActive, HRS_RdEndDate, HRS_RdContractEndDate, HRS_RdDeleteDate
FROM dbo.HRS_RuleDocument
WHERE HRS_RdStatus=0 AND HRS_RdActive=1
""", src).to_string(index=False))
print("--- status 3 ---")
print(pd.read_sql("""
SELECT TOP 8 HRS_RdID, HRS_RdStatus, HRS_RdActive, HRS_RdEndDate, HRS_RdContractEndDate, HRS_RdDeleteDate
FROM dbo.HRS_RuleDocument
WHERE HRS_RdStatus=3
""", src).to_string(index=False))

print("\n=== Duplicate post explanation ===")
print(pd.read_sql("""
SELECT
 (SELECT MIN(PostID) FROM HCM3.Post) min_id,
 (SELECT MAX(PostID) FROM HCM3.Post) max_id,
 (SELECT COUNT(*) FROM HCM3.Post) cnt,
 (SELECT MIN(DestPostID) FROM master.dbo.PostMigrationMapping) map_min,
 (SELECT MAX(DestPostID) FROM master.dbo.PostMigrationMapping) map_max
""", dst).to_string(index=False))
print(pd.read_sql("""
SELECT TOP 5 m.SourcePostID, m.DestPostID, p.Code, p.Title
FROM master.dbo.PostMigrationMapping m
JOIN HCM3.Post p ON p.PostID=m.DestPostID
ORDER BY m.DestPostID
""", dst).to_string(index=False))
# are orphans older?
print(pd.read_sql("""
SELECT
 SUM(CASE WHEN PostID < (SELECT MIN(DestPostID) FROM master.dbo.PostMigrationMapping) THEN 1 ELSE 0 END) older_than_map,
 SUM(CASE WHEN PostID > (SELECT MAX(DestPostID) FROM master.dbo.PostMigrationMapping) THEN 1 ELSE 0 END) newer_than_map,
 SUM(CASE WHEN PostID BETWEEN (SELECT MIN(DestPostID) FROM master.dbo.PostMigrationMapping) AND (SELECT MAX(DestPostID) FROM master.dbo.PostMigrationMapping)
      AND PostID NOT IN (SELECT DestPostID FROM master.dbo.PostMigrationMapping) THEN 1 ELSE 0 END) in_range_unmapped
FROM HCM3.Post
""", dst).to_string(index=False))
