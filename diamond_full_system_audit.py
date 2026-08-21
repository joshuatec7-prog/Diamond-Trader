#!/usr/bin/env python3
from __future__ import annotations
import csv,json,os,re,shutil,subprocess,sys
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path("/opt/render/project/src")
DATA=Path(os.getenv("DIAMOND_DATA_DIR","/var/data"))
NOW=datetime.now(timezone.utc)
ISSUES=[]
AREAS={x:"PASS" for x in [
"Broncode/Git","Runtime","Data","Scanner","SELECTIVE","POST-COVERAGE",
"Market Lead","Binance","Research","Safety","Resources"]}

ACTIVE=[
"agent.py","supervisor_agent.py","closed_candle_runner.py",
"periodic_analysis_runner.py","market_scanner.py",
"scanner_selective_shadow_lab.py","scanner_execution_quality_shadow.py",
"diamond_market_lead_v2.py","diamond_binance_1m_lead_events.py",
"diamond_selective_binance_1m_tracker.py",
"diamond_selective_v2_candidate_tracker.py",
"diamond_selective_post_coverage_tracker.py",
"diamond_master_research_status.py"]

def problem(level,area,msg):
    rank={"PASS":0,"WARN":1,"FAIL":2}
    if rank[level]>rank[AREAS[area]]: AREAS[area]=level
    ISSUES.append((level,area,msg))

def run(cmd):
    try:
        p=subprocess.run(cmd,cwd=ROOT,text=True,stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT,timeout=30,check=False)
        return p.returncode,p.stdout.strip()
    except Exception as e:
        return 99,f"{type(e).__name__}: {e}"

def jload(p): return json.loads(p.read_text(encoding="utf-8"))
def age(p): return max(0,(NOW.timestamp()-p.stat().st_mtime)/60)
def iso(v):
    try:
        s=str(v or "").strip()
        if not s:return None
        d=datetime.fromisoformat(s.replace("Z","+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except:return None
def rule(report,name="CURRENT_ALL"):
    try:
        for r in jload(report).get("rules",[]):
            if r.get("rule")==name:return r
    except:pass
    return None

# BRONCODE/GIT
for f in ACTIVE:
    if not (ROOT/f).is_file(): problem("FAIL","Broncode/Git",f"Actief bestand ontbreekt: {f}")

py=list(sorted(ROOT.glob("*.py"))); bad=[]
for p in py:
    try: compile(p.read_text(errors="replace"),str(p),"exec")
    except SyntaxError as e: bad.append(f"{p.name}:{e.lineno} {e.msg}")
for x in bad[:10]: problem("FAIL","Broncode/Git",f"Syntaxfout: {x}")

_,branch=run(["git","branch","--show-current"])
_,commit=run(["git","log","-1","--oneline"])
rc,gs=run(["git","status","--porcelain"])
if rc==0:
    dirty=[]; untracked=[]
    for line in gs.splitlines():
        if not line.strip():continue
        code,name=line[:2],line[3:].strip()
        if "__pycache__" in name or ".bak_" in name:continue
        if code=="??":
            if name.endswith((".py",".yaml",".yml",".json",".sh")) or name=="chat":
                untracked.append(name)
        else: dirty.append(f"{code} {name}")
    if dirty: problem("WARN","Broncode/Git","Niet-gecommitte wijzigingen: "+", ".join(dirty[:8]))
    if untracked: problem("WARN","Broncode/Git","Niet-gecommitte bronbestanden: "+", ".join(untracked[:8]))
snaps=sorted(DATA.glob("diamond_source_snapshot_*.tar.gz"),key=lambda p:p.stat().st_mtime,reverse=True)
if not snaps: problem("WARN","Broncode/Git","Geen persistente source snapshot")

# RUNTIME
_,ps=run(["ps","-eo","args="]); lines=ps.splitlines()
for script in ["agent.py","supervisor_agent.py","closed_candle_runner.py","periodic_analysis_runner.py"]:
    n=0
    for line in lines:
        toks=re.findall(r"\S+",line)
        if any(Path(t.strip("'\"")).name==script for t in toks): n+=1
    if n==0: problem("FAIL","Runtime",f"Proces ontbreekt: {script}")
    elif n>1: problem("WARN","Runtime",f"Dubbel proces: {script} x{n}")

today=NOW.strftime("%Y-%m-%d"); errs=[]
for p in DATA.glob("*.log"):
    try:
        if age(p)>1440:continue
        with p.open("rb") as f:
            f.seek(max(0,p.stat().st_size-500000))
            txt=f.read().decode("utf-8","replace")
        for line in txt.splitlines():
            if today in line and re.search(r"Traceback|ERROR|CRITICAL|OOM|Killed",line,re.I):
                errs.append(f"{p.name}: {line[-180:]}")
    except:pass
for x in errs[-6:]: problem("WARN","Runtime","Actuele logfout: "+x)

fresh={
"diamond_market_scanner_state.json":(45,120),
"diamond_market_lead_v2_report.json":(45,120),
"diamond_binance_1m_lead_state.json":(45,120),
"diamond_periodic_analysis_state.json":(90,180)}
for name,(w,f) in fresh.items():
    p=DATA/name
    if not p.exists(): problem("WARN","Runtime",f"Actieve state ontbreekt: {name}")
    elif age(p)>f: problem("FAIL","Runtime",f"Stagnatie {name}: {age(p):.0f} min")
    elif age(p)>w: problem("WARN","Runtime",f"Mogelijk traag {name}: {age(p):.0f} min")

# DATA
jsons=list(DATA.glob("*.json")); jbad=[]
for p in jsons:
    try:jload(p)
    except Exception as e:jbad.append(f"{p.name}:{type(e).__name__}")
for x in jbad[:10]: problem("FAIL","Data","Ongeldige JSON: "+x)

tradep=DATA/"diamond_scanner_selective_shadow_trades.csv"; rows=[]
if not tradep.exists(): problem("FAIL","SELECTIVE","SELECTIVE trade-CSV ontbreekt")
else:
    try:
        with tradep.open(encoding="utf-8-sig",newline="") as f:
            rows=[r for r in csv.DictReader(f) if str(r.get("variant") or "").upper()=="SELECTIVE"]
        keys=[str(r.get("candidate_key") or "") for r in rows]
        dups=[k for k,n in Counter(k for k in keys if k).items() if n>1]
        if any(not k for k in keys): problem("WARN","SELECTIVE","SELECTIVE rows zonder candidate_key")
        if dups: problem("FAIL","SELECTIVE",f"{len(dups)} dubbele candidate_key(s)")
    except Exception as e: problem("FAIL","SELECTIVE",f"Trade-CSV onleesbaar: {type(e).__name__}")

# SCANNER
src=(ROOT/"market_scanner.py").read_text(errors="replace") if (ROOT/"market_scanner.py").exists() else ""
for t in ["MOVER_24H","HIGH_VOLUME","ROTATION","selection_reason"]:
    if t not in src: problem("FAIL","Scanner",f"Hybride kenmerk ontbreekt: {t}")
cfg={}
try:
    import yaml
    cfg=yaml.safe_load((ROOT/"config.yaml").read_text()) or {}
    s=cfg.get("scanner",{}) if isinstance(cfg,dict) else {}
    ms=cfg.get("market_scanner",{}) if isinstance(cfg,dict) else {}
    if "top_n_markets" in s and "top_n_markets" not in ms:
        problem("WARN","Scanner",
            f"Config-key mismatch: scanner.top_n_markets={s.get('top_n_markets')} maar runtime gebruikt market_scanner.top_n_markets/default 20")
except Exception as e: problem("WARN","Scanner",f"Config niet gelezen: {type(e).__name__}")

# POST-COVERAGE
pstp=DATA/"diamond_selective_post_coverage_state.json"
prp=DATA/"diamond_selective_post_coverage_report.json"
if not pstp.exists(): problem("FAIL","POST-COVERAGE","State ontbreekt")
else:
    try:
        pst=jload(pstp); baseline={str(x) for x in pst.get("baseline_keys",[])}
        created=iso(pst.get("created_at"))
        if not baseline: problem("FAIL","POST-COVERAGE","Baseline leeg")
        contam=[]
        for r in rows:
            k=str(r.get("candidate_key") or ""); op=iso(r.get("opened_at") or r.get("detected_at")); cl=iso(r.get("closed_at"))
            if k not in baseline and created and op and cl and op<created<cl:
                contam.append(f"{r.get('symbol')} {r.get('side')}")
        if contam: problem("FAIL","POST-COVERAGE","Oude open trade na grens gesloten: "+", ".join(contam[:8]))
        expected=sum(1 for r in rows if str(r.get("candidate_key") or "") not in baseline)
        if prp.exists():
            reported=int(jload(prp).get("prospective_closed_selective",-1))
            if reported!=expected: problem("FAIL","POST-COVERAGE",f"Report={reported}, CSV verwacht={expected}")
    except Exception as e: problem("FAIL","POST-COVERAGE",f"Controle faalde: {type(e).__name__}: {e}")

selst=DATA/"diamond_scanner_selective_shadow_state.json"
if selst.exists() and pstp.exists():
    try:
        opens=jload(selst).get("open_positions",{}); created=iso(jload(pstp).get("created_at")); cross=[]
        if isinstance(opens,dict) and created:
            for v in opens.values():
                if isinstance(v,dict):
                    op=iso(v.get("opened_at") or v.get("detected_at"))
                    if op and op<created:cross.append(str(v.get("symbol") or "?"))
        if cross: problem("WARN","POST-COVERAGE","Nog PRE-COVERAGE open positie(s): "+", ".join(cross))
    except:pass

# MARKET LEAD
ml=DATA/"diamond_market_lead_v2_report.json"
if not ml.exists(): problem("FAIL","Market Lead","Report ontbreekt")
else:
    try:
        d=jload(ml)
        if int(d.get("events",0))<=0: problem("FAIL","Market Lead","0 events")
        if int(d.get("early_events",0))<=0: problem("WARN","Market Lead","0 early events")
        ex=d.get("exchange_status",{})
        for name in ["bitvavo","binance","kraken"]:
            if ex.get(name) is not True:problem("WARN","Market Lead",f"Exchange niet OK: {name}")
    except Exception as e: problem("FAIL","Market Lead",f"Report onleesbaar: {type(e).__name__}")

# BINANCE
bsrc=(ROOT/"diamond_selective_binance_1m_tracker.py").read_text(errors="replace")
if re.search(r'get\(["\']asset["\']\)\s*==\s*asset',bsrc):
    problem("FAIL","Binance","Oude foutieve same-asset koppeling aanwezig")
if "direction" not in bsrc or "120000" not in bsrc:
    problem("WARN","Binance","Direction/120s koppeling niet herkenbaar")
bstate=DATA/"diamond_binance_1m_lead_state.json"
if bstate.exists():
    try:
        ev=jload(bstate).get("events",[])
        if not isinstance(ev,list) or not ev:problem("FAIL","Binance","Lead-state zonder events")
    except Exception as e:problem("FAIL","Binance",f"Lead-state onleesbaar: {type(e).__name__}")

# RESEARCH
if prp.exists():
    try:
        d=jload(prp); n=int(d.get("prospective_closed_selective",-1)); r=rule(prp)
        if r and int(r.get("n",-2))!=n:problem("FAIL","Research","POST CURRENT_ALL n wijkt af van prospective count")
    except:pass

# SAFETY
order_re=re.compile(r"\b(create_order|create_market_buy_order|create_market_sell_order|cancel_order)\s*\(",re.I)
for name in ["market_scanner.py","diamond_market_lead_v2.py","diamond_binance_1m_lead_events.py",
             "diamond_selective_binance_1m_tracker.py","diamond_selective_post_coverage_tracker.py"]:
    p=ROOT/name
    if p.exists() and order_re.search(p.read_text(errors="replace")):
        problem("FAIL","Safety",f"Order-call in researchcomponent: {name}")
if prp.exists():
    try:
        s=jload(prp).get("safety",{})
        for k in ["orders","private_api","network","strategy_change","config_change","stake_change","live_change"]:
            if s.get(k) not in (False,None):problem("FAIL","Safety",f"{k}={s.get(k)}")
    except:pass

# RESOURCES
try:
    total,used,free=shutil.disk_usage(DATA); dp=used/total*100
    if dp>=85:problem("FAIL","Resources",f"Schijf {dp:.1f}%")
    elif dp>=70:problem("WARN","Resources",f"Schijf {dp:.1f}%")
except:dp=None
mem=None
try:mem=int(Path("/sys/fs/cgroup/memory.current").read_text())/1024/1024
except:pass
if mem is not None:
    if mem>=1800:problem("FAIL","Resources",f"Geheugen {mem:.0f} MiB")
    elif mem>=1400:problem("WARN","Resources",f"Geheugen {mem:.0f} MiB")

# OUTPUT
print("="*72)
print(" DIAMOND FULL SYSTEM AUDIT v1.0")
print(" "+NOW.isoformat())
print("="*72)
for a in AREAS: print(f"{a:<20}: {AREAS[a]}")
print("\n=== KERNSTATUS ===")
print(f"Python syntax      : {len(py)-len(bad)}/{len(py)} PASS")
print(f"JSON geldig        : {len(jsons)-len(jbad)}/{len(jsons)} PASS")
print(f"Git branch         : {branch or '?'}")
print(f"Git commit         : {commit or '?'}")
print(f"SELECTIVE rows     : {len(rows)}")
if pstp.exists():
    try:
        pst=jload(pstp); print(f"POST baseline      : {len(pst.get('baseline_keys',[]))}"); print(f"POST created       : {pst.get('created_at')}")
    except:pass
if ml.exists():
    try:
        d=jload(ml); print(f"Market Lead        : events={d.get('events')} early={d.get('early_events')} age={age(ml):.0f}m")
    except:pass
if mem is not None:print(f"Geheugen           : {mem:.1f} MiB")
if dp is not None:print(f"Schijf             : {dp:.1f}%")

fails=[x for x in ISSUES if x[0]=="FAIL"]; warns=[x for x in ISSUES if x[0]=="WARN"]
if fails:
    print("\n=== KRITIEK ===")
    for _,a,m in fails:print(f"FAIL [{a}] {m}")
if warns:
    print("\n=== WAARSCHUWINGEN ===")
    for _,a,m in warns:print(f"WARN [{a}] {m}")
if not fails and not warns:print("\nGeen problemen gevonden.")
print()
if fails: final=f"FAIL | {len(fails)} kritiek | {len(warns)} waarschuwing(en)"
elif warns: final=f"GEZOND MET AANDACHTSPUNTEN | {len(warns)} waarschuwing(en)"
else: final="GEZOND | geen aandachtspunten"
print("EINDSTATUS:",final)
print("="*72)
sys.exit(1 if fails else 0)
