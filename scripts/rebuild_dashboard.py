#!/usr/bin/env python3
"""
rebuild_dashboard.py
Fetches latest data from Google Apps Script, rebuilds all embedded JSON
blobs (EMBEDDED, REVERSIONS, ALL_FLAGS) in index.html, and saves the file.
Run by GitHub Actions daily — keeps the dashboard permanently up to date.
"""

import os, sys, json, re, urllib.request
import pandas as pd
import numpy as np

APPS_URL = os.environ.get('APPS_SCRIPT_URL', '')
if not APPS_URL:
    print("ERROR: APPS_SCRIPT_URL environment variable not set")
    sys.exit(1)

# ── Fetch raw data ─────────────────────────────────────────────────────────────
print("Fetching data from Apps Script...")
try:
    url = APPS_URL + "?sheet=database&callback=__data__"
    with urllib.request.urlopen(url, timeout=120) as r:
        raw = r.read().decode('utf-8')
    data = json.loads(raw[len('__data__('):-1])
    rows = data['rows']
    print(f"Fetched {len(rows)} rows")
except Exception as e:
    print(f"ERROR fetching data: {e}")
    sys.exit(1)

# ── Build dataframe ────────────────────────────────────────────────────────────
df = pd.DataFrame(rows)
df['Month_IST']       = pd.to_datetime(df['Month '], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
df['MonthStr']        = df['Month_IST'].dt.strftime('%Y-%m')
df['launch_date_IST'] = pd.to_datetime(df['Launch date'], errors='coerce', utc=True).dt.tz_convert('Asia/Kolkata')
df['Production']      = pd.to_numeric(df['Fortified atta production volume (MT)'], errors='coerce')
df['Icheck']          = pd.to_numeric(df['Icheck result  (mg/kg)'], errors='coerce')
df['Beneficiaries']   = pd.to_numeric(df['Beneficiaries reached'], errors='coerce')
df['ProdDev']         = pd.to_numeric(df['% Deviation from 3-Month Avg'], errors='coerce')
df['Avg3M']           = pd.to_numeric(df['Avg Production Last 3 Months (MT)'], errors='coerce')
df['qo_orig']         = df['Quality Officer'].str.strip().fillna('')
df['qo_cto']          = df['Quality Officer from SurveyCTO'].str.strip().fillna('')
df['qo_final']        = np.where(df['qo_cto'] != '', df['qo_cto'], df['qo_orig'])
df['nm_flag']         = df['Selected for Non-Monthly Visit'].map({'Yes':1,'No':0}).fillna(0).astype(int)
df['launch_age_months'] = ((df['Month_IST'] - df['launch_date_IST']).dt.days / 30.44).round(0)
df = df[df['Mill Code'].notna() & (df['Mill Code'] != '')].copy()

def age_bucket(a):
    if pd.isna(a) or a < 0: return 'Unknown'
    elif a <= 6:  return '0-6 months'
    elif a <= 12: return '6-12 months'
    elif a <= 24: return '12-24 months'
    else:         return '24+ months'
df['age_bucket'] = df['launch_age_months'].apply(age_bucket)

RCA_COLS = {
    'Human/Ops':   'RCA: Human / Operational / Protocol',
    'Equipment':   'RCA: Equipment / Mechanical / Electrical',
    'Microfeeder': 'RCA: Microfeeder Calibration / Set-up',
    'Sampling':    'RCA: Sampling Issue',
    'Premix':      'RCA: Premix Issue',
    'No Issue':    'RCA: No Issue / Retesting',
}

def rca_bits(row):
    b = 0
    for i, col in enumerate(RCA_COLS.values()):
        if row.get(col, '') == 'Yes': b |= (1 << i)
    return b

def ich_s(v):
    if pd.isna(v) or v == 0: return 0
    if v < 14:    return 1
    if v <= 21.25:return 2
    if v <= 28:   return 3
    return 4

# ── Determine month cutoff ─────────────────────────────────────────────────────
# Include up to latest month in data (IST)
max_month = df['MonthStr'].max()
print(f"Data runs to: {max_month} (IST)")

base = df[(df['MonthStr'] >= '2022-07') & (df['MonthStr'] <= max_month)].copy()
print(f"Base records: {len(base)}")

# ── Pipeline counts ────────────────────────────────────────────────────────────
latest_per_mill = df.sort_values('MonthStr').groupby('Mill Code').last().reset_index()
launched   = int((latest_per_mill['Mill Stage'] == 'Launched').sum())
pre_launch = int((latest_per_mill['Mill Stage'] == 'Pre-Launch').sum())
terminated = int((latest_per_mill['Mill Stage'] == 'Terminated').sum())
states_n   = int(latest_per_mill['State Name'].nunique())
clusters_n = int(latest_per_mill['Cluster Name'].nunique())

# Active mills = mills with production in the last month that has production data
prod_months = df[df['Production'] > 0].groupby('MonthStr').size()
last_prod_month = prod_months.index[-1] if len(prod_months) else max_month
active_mills = int(df[(df['MonthStr'] == last_prod_month) & (df['Production'] > 0)]['Mill Code'].nunique())
print(f"Pipeline: Launched={launched}, Pre-Launch={pre_launch}, Terminated={terminated}")
print(f"Active mills as of {last_prod_month}: {active_mills}")

# ── Build lookup tables ────────────────────────────────────────────────────────
states      = sorted(base['State Name'].dropna().unique().tolist())
clusters    = sorted(base['Cluster Name'].dropna().unique().tolist())
mills       = sorted(base['Mill Code'].dropna().unique().tolist())
mill_names  = dict(zip(base['Mill Code'], base['Mill Name']))
qos         = sorted(base['qo_final'][base['qo_final'] != ''].unique().tolist())
pos_list    = sorted([p for p in base['Program Officer'].str.strip().fillna('').unique() if p])
cap_cats    = ['Below 100 MT/Month','100-300 MT/Month','300-1000 MT/Month','More than 1000 MT/ Month']
months_list = sorted(base['MonthStr'].unique().tolist())

state_idx   = {s:i for i,s in enumerate(states)}
cluster_idx = {c:i for i,c in enumerate(clusters)}
mill_idx    = {m:i for i,m in enumerate(mills)}
qo_idx      = {q:i for i,q in enumerate(qos)}
po_idx      = {p:i for i,p in enumerate(pos_list)}
cap_idx     = {c:i for i,c in enumerate(cap_cats)}
month_idx   = {m:i for i,m in enumerate(months_list)}
age_idx_map = {'0-6 months':0,'6-12 months':1,'12-24 months':2,'24+ months':3,'Unknown':4}

# ── Build records ──────────────────────────────────────────────────────────────
records = []
for _, row in base.iterrows():
    mc = row['Mill Code']; ms = row['MonthStr']
    if mc not in mill_idx or ms not in month_idx: continue
    ich_v = None if (pd.isna(row['Icheck']) or row['Icheck'] == 0) else round(float(row['Icheck']), 2)
    prod  = None if (pd.isna(row['Production']) or row['Production'] == 0) else round(float(row['Production']), 1)
    pdev  = None if pd.isna(row['ProdDev']) else round(float(row['ProdDev']), 1)
    bene  = None if (pd.isna(row['Beneficiaries']) or row['Beneficiaries'] == 0) else float(row['Beneficiaries'])
    avg3m = None if (pd.isna(row['Avg3M']) or row['Avg3M'] == 0) else round(float(row['Avg3M']), 1)
    qo = row['qo_final']
    po = row['Program Officer'].strip() if pd.notna(row['Program Officer']) else ''
    records.append([
        month_idx[ms], mill_idx[mc],
        state_idx.get(row['State Name'], -1),
        cluster_idx.get(row['Cluster Name'], -1),
        cap_idx.get(row['Mill Capacity Category'], -1),
        age_idx_map.get(row['age_bucket'], 4),
        ich_v, ich_s(row['Icheck']),
        prod, pdev, int(row['nm_flag']),
        rca_bits(dict(row)),
        bene, avg3m,
        qo_idx.get(qo, -1),
        po_idx.get(po, -1),
    ])

embedded = {
    'months': months_list, 'states': states, 'clusters': clusters,
    'mills': mills, 'mill_names': mill_names, 'cap_cats': cap_cats,
    'rca_labels': list(RCA_COLS.keys()), 'qos': qos, 'pos': pos_list,
    'pipeline': {
        'launched': launched, 'pre_launch': pre_launch, 'terminated': terminated,
        'active': active_mills, 'states': states_n, 'clusters': clusters_n
    },
    'records': records,
}
embedded_json = json.dumps(embedded, separators=(',', ':'))
print(f"EMBEDDED: {len(embedded_json)//1024}KB, {len(records)} records")

# ── Build ALL_FLAGS ────────────────────────────────────────────────────────────
all_m = months_list
last3 = all_m[-3:]
prev3 = all_m[-6:-3]

mill_ich = base[base['Icheck'].notna() & (base['Icheck'] != 0)].copy()
mill_ich['oor'] = ~((mill_ich['Icheck'] >= 14) & (mill_ich['Icheck'] <= 21.25))
persist = {}
for mc, grp in mill_ich.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr', ascending=False)
    streak = 0
    for _, r in sg.iterrows():
        if r['oor']: streak += 1
        else: break
    persist[mc] = streak

prod_flags = []
for mc, grp in base.groupby('Mill Code'):
    cur = grp[grp['MonthStr'].isin(last3)]['Production'].replace(0, np.nan).dropna()
    prv = grp[grp['MonthStr'].isin(prev3)]['Production'].replace(0, np.nan).dropna()
    if len(cur) >= 1 and len(prv) >= 1:
        ca, pa = cur.mean(), prv.mean()
        pct = round((ca - pa) / pa * 100, 1) if pa > 0 else 0
        pstatus = 'Declining' if pct <= -20 else ('Improving' if pct >= 30 else 'Stable')
    elif len(cur) >= 1:
        ca = cur.mean(); pa = None; pct = None; pstatus = 'New'
    else:
        ca = None; pa = None; pct = None; pstatus = 'No Data'
    ich_d = grp[grp['Icheck'].notna() & (grp['Icheck'] != 0)].sort_values('MonthStr', ascending=False)
    if len(ich_d):
        iv = ich_d.iloc[0]['Icheck']; im = ich_d.iloc[0]['MonthStr']
        ist = 'Below Range' if iv < 14 else ('Within Range' if iv <= 21.25 else ('21.26-28' if iv <= 28 else 'Above 28'))
    else:
        iv = im = None; ist = 'No Data'
    prod_flags.append({
        'mc': mc, 'name': grp['Mill Name'].iloc[0],
        'state': grp['State Name'].iloc[0], 'cluster': grp['Cluster Name'].iloc[0],
        'qo': str(grp['qo_final'].iloc[-1]),
        'po': str(grp['Program Officer'].iloc[-1]) if pd.notna(grp['Program Officer'].iloc[-1]) else '',
        'cur_avg': round(ca, 1) if ca else None, 'prv_avg': round(pa, 1) if pa else None,
        'pct': pct, 'pstatus': pstatus,
        'ich_val': round(iv, 2) if iv else None, 'ich_month': im,
        'ich_streak': persist.get(mc, 0), 'ichstatus': ist
    })
flags_json = json.dumps(prod_flags, separators=(',', ':'))
print(f"ALL_FLAGS: {len(flags_json)//1024}KB, {len(prod_flags)} mills")

# ── Build REVERSIONS ──────────────────────────────────────────────────────────
reversions = []
for mc, grp in base.groupby('Mill Code'):
    sg = grp.sort_values('MonthStr').reset_index(drop=True)
    was_nm = False; nm_start = None
    for i, row in sg.iterrows():
        nm = row['Selected for Non-Monthly Visit'] == 'Yes'
        if nm and not was_nm:
            was_nm = True; nm_start = row['MonthStr']
        elif not nm and was_nm:
            rm = row['MonthStr']
            pre = sg[sg['MonthStr'] < rm].tail(3)
            pre_ich = pre[pre['Icheck'].notna() & (pre['Icheck'] != 0)]['Icheck'].tolist()
            pre_s   = ['Below' if v < 14 else 'In Range' if v <= 21.25 else 'Above' for v in pre_ich]
            pre_pd  = pre[pre['ProdDev'].notna()]['ProdDev'].tolist()
            reasons = []
            oor_c = sum(1 for s in pre_s if s != 'In Range')
            if oor_c >= 1: reasons.append(f"iCheck OOR ({oor_c} of {len(pre_s)} months)")
            if pre_pd:
                avg_dev = np.mean(pre_pd)
                if avg_dev < -20:  reasons.append(f"Production declining (avg {avg_dev:.1f}%)")
                elif avg_dev > 30: reasons.append(f"Production surge (avg {avg_dev:.1f}%)")
            if not reasons: reasons.append("No clear signal")
            nm_m = sg[(sg['MonthStr'] >= nm_start) & (sg['MonthStr'] < rm)]['MonthStr'].nunique()
            reversions.append({
                'mc': mc, 'name': sg['Mill Name'].iloc[0],
                'state': sg['State Name'].iloc[0], 'cluster': sg['Cluster Name'].iloc[0],
                'qo': str(sg['qo_final'].iloc[-1]),
                'po': str(sg['Program Officer'].iloc[-1]) if pd.notna(sg['Program Officer'].iloc[-1]) else '',
                'nm_start': nm_start, 'revert_month': rm, 'nm_duration': nm_m,
                'pre_ich': [round(v, 2) for v in pre_ich], 'pre_ich_status': pre_s,
                'pre_pdev': [round(v, 1) for v in pre_pd], 'trigger': '; '.join(reasons)
            })
            was_nm = False; nm_start = None
revs_json = json.dumps(reversions, separators=(',', ':'))
print(f"REVERSIONS: {len(revs_json)//1024}KB, {len(reversions)} events")

# ── Inject into index.html ────────────────────────────────────────────────────
with open('index.html') as f:
    html = f.read()

apps_idx  = html.find("const APPS_URL='https://")
close_tag = html.rfind('</script>')
js        = html[apps_idx:close_tag]

emb_pos   = js.find('const EMBEDDED=')
rev_pos   = js.find('const REVERSIONS=')
flags_pos = js.find('const ALL_FLAGS=')
f_pos     = js.find('const F=')

new_js = (
    js[:emb_pos] +
    'const EMBEDDED='   + embedded_json + ';\n' +
    'const REVERSIONS=' + revs_json     + ';\n' +
    'const ALL_FLAGS='  + flags_json    + ';\n' +
    js[f_pos:]
)

# Escape </ sequences in JS
new_js = new_js.replace('</', '<\\/')

# Update reversion count in HTML
rev_count = len(reversions)
rev_mills = len(set(r['mc'] for r in reversions))
html_part  = html[:apps_idx]
last_gt    = html_part.rfind('>')
clean_html = html_part[:last_gt+1]
clean_html = re.sub(
    r'\(\d+ events across \d+ mills, independent of global filters\)',
    f'({rev_count} events across {rev_mills} mills, independent of global filters)',
    clean_html
)

# Restore CDN script tag if needed
if 'chart.umd.js' not in clean_html:
    clean_html = clean_html.replace('</title>\n<style>',
        '</title>\n<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>\n<style>')

new_html = clean_html + '\n<script>\n' + new_js + '\n</script>\n</body>\n</html>'

with open('index.html', 'w') as f:
    f.write(new_html)

print(f"\n✅ index.html updated successfully!")
print(f"   Size: {len(new_html)//1024}KB")
print(f"   Data current as of: {max_month} (IST)")
print(f"   Mills: {len(mills)} | iCheck records: {sum(1 for r in records if r[6] is not None)}")
print(f"   Within range: {sum(1 for r in records if r[7]==2)}")
