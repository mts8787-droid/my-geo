import json
import os

for fname in ['scoring_config.json', 'scoring_config.default.json']:
    if not os.path.exists(fname):
        continue
    with open(fname, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    total_points = 0
    for cat_key, cat_data in data.items():
        if cat_key == 'grade': continue
        cat_total = 0
        for cr in cat_data.get('criteria', []):
            cr['points'] = 1
            cat_total += 1
        cat_data['max'] = cat_total
        total_points += cat_total
        
    data['grade'] = {
        'good': int(total_points * 0.9),
        'need_improvement': int(total_points * 0.7)
    }
    
    with open(fname, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
