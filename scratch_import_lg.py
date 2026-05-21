import csv
import json
import os
import uuid

# 1. Split lg_urls_list.csv into country-specific files
urls_file = "reports/lg_urls_list.csv"
plan_file = "reports/lg_scheduling_plan.csv"

if not os.path.exists(urls_file) or not os.path.exists(plan_file):
    print("보고서 파일이 없습니다. 크롤링이 완료되었는지 확인하세요.")
    exit()

print("1. Split URLs by country...")
country_urls = {}
with open(urls_file, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        c = row["Country/Site"]
        u = row["URL"]
        if c not in country_urls:
            country_urls[c] = []
        country_urls[c].append(u)

for c, urls in country_urls.items():
    c_safe = "".join([x if x.isalnum() else "_" for x in c])
    out_file = f"reports/lg_urls_{c_safe}.csv"
    with open(out_file, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["URL"])
        for u in urls:
            writer.writerow([u])

print("2. Read audit_data.json and add groups/schedules...")
try:
    with open("audit_data.json", "r", encoding="utf-8") as f:
        audit_data = json.load(f)
except Exception:
    audit_data = {"groups": [], "schedules": []}

if "groups" not in audit_data: audit_data["groups"] = []
if "schedules" not in audit_data: audit_data["schedules"] = []

# Remove existing LG Sitemap groups to avoid duplicates
audit_data["groups"] = [g for g in audit_data["groups"] if not g.get("name", "").startswith("LG Sitemap - ")]
audit_data["schedules"] = [s for s in audit_data["schedules"] if not s.get("name", "").startswith("LG Sitemap - ")]

time_hour = 1
time_minute = 0

with open(plan_file, "r", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    for row in reader:
        c = row["Country/Site"]
        count = int(row["Total URLs"])
        assigned_day = int(row.get("Assigned Day", 1))
        c_safe = "".join([x if x.isalnum() else "_" for x in c])
        
        group_id = f"grp_lg_{c_safe}"
        group_name = f"LG Sitemap - {c}"
        csv_path = f"reports/lg_urls_{c_safe}.csv"
        
        # Add Group
        audit_data["groups"].append({
            "id": group_id,
            "name": group_name,
            "url_count": count,
            "csv_file": csv_path,
            "urls": [] # Empty to save space in UI
        })
        
        # Add Schedule
        schedule_id = f"sch_lg_{c_safe}"
        time_str = f"{time_hour:02d}:{time_minute:02d}"
        
        audit_data["schedules"].append({
            "id": schedule_id,
            "name": f"LG Sitemap - {c} (매월 {assigned_day}일 점검)",
            "group_id": group_id,
            "frequency": f"monthly_day_{assigned_day}",
            "time": time_str,
            "enabled": True,
            "chunk_size": 0,
            "chunk_index": 0
        })
        
        # Increment time by 10 minutes for next schedule
        time_minute += 10
        if time_minute >= 60:
            time_minute = 0
            time_hour += 1
            if time_hour >= 24:
                time_hour = 0

with open("audit_data.json", "w", encoding="utf-8") as f:
    json.dump(audit_data, f, ensure_ascii=False, indent=2)

print("작업이 완료되었습니다. audit_data.json에 스케줄과 그룹이 업데이트되었습니다.")
