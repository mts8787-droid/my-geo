import csv, random, json
try:
    with open('reports/lg_urls_uk.csv', 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        urls = [r['URL'] for r in reader if 'URL' in r]
    selected = random.sample(urls, min(100, len(urls)))
    
    with open('data/audit_data.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
        
    group_id = 'grp_test_uk_100'
    schedule_id = 'sch_test_uk_100'
    
    data['groups'] = [g for g in data.get('groups', []) if g.get('id') != group_id]
    data['schedules'] = [s for s in data.get('schedules', []) if s.get('id') != schedule_id]
    
    data['groups'].insert(0, {
        'id': group_id,
        'name': 'UK Test - 100 Random',
        'urls': selected,
        'url_count': len(selected)
    })
    
    data['schedules'].insert(0, {
        'id': schedule_id,
        'name': 'UK Test - 100 Random',
        'group_id': group_id,
        'frequency': 'daily',
        'time': '12:00',
        'enabled': True,
        'chunk_size': 0
    })
    
    with open('data/audit_data.json', 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        
    print('Successfully created test group and schedule with 100 random UK URLs.')
except Exception as e:
    print('Error:', e)
