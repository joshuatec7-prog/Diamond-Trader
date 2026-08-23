#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,math,statistics,time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import ccxt,yaml

VERSION='1.1'; DATA=Path('/var/data'); PROJ=Path('/opt/render/project/src')
TRADES=DATA/'diamond_scanner_selective_shadow_trades.csv'; CFG=PROJ/'config.yaml'
TF='15m'; TFMS=900000; ATR_LEN=14; MAX_HOLD_MIN=2880; MULTS=(0.8,1.2,1.6,2.0)
ROUTES={('LONG','trend_breakout'):'LONG_TREND',('LONG','momentum'):'LONG_MOM',('SHORT','momentum'):'SHORT_MOM'}

def f(v,d=0.0):
    try:
        x=float(v); return x if math.isfinite(x) else d
    except: return d

def dt(v):
    try:
        x=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        if x.tzinfo is None:x=x.replace(tzinfo=timezone.utc)
        return x.astimezone(timezone.utc)
    except:return None

def fee_pct():
    try:
        c=yaml.safe_load(CFG.read_text()) or {}; return max(0.0,f((c.get('fees') or {}).get('taker_fee_pct'),0.25))
    except:return 0.25

def load_trades(days):
    cutoff=datetime.now(timezone.utc)-timedelta(days=days); out=[]; seen=set()
    with TRADES.open(newline='',encoding='utf-8-sig') as h:
        for r in csv.DictReader(h):
            if str(r.get('variant','')).upper()!='CURRENT' or not str(r.get('closed_at','')).strip():continue
            side=str(r.get('side','')).upper(); strat=str(r.get('strategy','')).lower(); route=ROUTES.get((side,strat))
            if not route:continue
            opened=dt(r.get('opened_at') or r.get('detected_at'))
            if not opened or opened<cutoff:continue
            key=str(r.get('candidate_key') or '') or f"{r.get('symbol')}|{side}|{strat}|{opened.isoformat()}"
            if key in seen:continue
            seen.add(key)
            entry=f(r.get('entry_price')); amount=f(r.get('amount')); stake=f(r.get('stake_eur')); stop=f(r.get('stop_loss'))
            if amount<=0 and entry>0 and stake>0:amount=stake/entry
            if entry<=0 or amount<=0 or stop<=0:continue
            out.append({'symbol':str(r.get('symbol') or ''),'side':side,'route':route,'entry_ms':int(opened.timestamp()*1000),'entry':entry,'amount':amount,'stop':stop,'recorded':f(r.get('net_pnl_eur'))})
    return sorted(out,key=lambda x:x['entry_ms'])

def fetch_all(ex,sym,since,until):
    rows=[]; cur=since
    while cur<=until:
        b=ex.fetch_ohlcv(sym,timeframe=TF,since=cur,limit=1000)
        if not b:break
        for r in b:
            if int(r[0])<=until:rows.append([float(x) for x in r[:6]])
        last=int(b[-1][0]); nxt=last+TFMS
        if nxt<=cur:break
        cur=nxt
        if last>=until:break
        time.sleep(max(0,float(getattr(ex,'rateLimit',0) or 0)/1000))
    d={int(r[0]):r for r in rows}; return [d[k] for k in sorted(d)]

def atrs(c,n=ATR_LEN):
    out=[None]*len(c)
    if len(c)<n+1:return out
    tr=[]
    for i,r in enumerate(c):
        hi,lo=r[2],r[3]
        tr.append(hi-lo if i==0 else max(hi-lo,abs(hi-c[i-1][4]),abs(lo-c[i-1][4])))
    prev=sum(tr[1:n+1])/n; out[n]=prev
    for i in range(n+1,len(c)):
        prev=((prev*(n-1))+tr[i])/n; out[i]=prev
    return out

def sim(t,c,a,m,fee):
    # Gebruik alleen volledig post-entry candles; zo lekt geen high/low van vóór de entry in de replay.
    first_full=((t['entry_ms']+TFMS-1)//TFMS)*TFMS
    start=next((i for i,r in enumerate(c) if int(r[0])>=first_full),None)
    if start is None:return None
    end=t['entry_ms']+MAX_HOLD_MIN*60000; side=t['side']; entry=t['entry']; stop=t['stop']; hiw=entry; loww=entry; px=None
    for i in range(start,len(c)):
        ts,op,hi,lo,cl,vol=c[i]; ts=int(ts)
        if ts>end:break
        av=a[i]
        if av and av>0:
            if side=='LONG':
                stop=max(stop,hiw-m*av)
                if lo<=stop:px=stop;break
                hiw=max(hiw,hi)
            else:
                stop=min(stop,loww+m*av)
                if hi>=stop:px=stop;break
                loww=min(loww,lo)
        else:
            if side=='LONG' and lo<=stop:px=stop;break
            if side=='SHORT' and hi>=stop:px=stop;break
    if px is None:
        elig=[r for r in c if first_full<=int(r[0])<=end]
        if not elig:return None
        px=elig[-1][4]
    amt=t['amount']; gross=(px-entry)*amt if side=='LONG' else (entry-px)*amt
    return gross-entry*amt*fee/100-px*amt*fee/100

def pf(v):
    gp=sum(x for x in v if x>0); gl=abs(sum(x for x in v if x<0))
    if gl>0:return gp/gl
    return math.inf if gp>0 else None

def pft(x):return 'n/a' if x is None else ('INF' if math.isinf(x) else f'{x:.3f}')
def show(name,vals):
    print(f"{name:12} n={len(vals):3d} W/L={sum(x>0 for x in vals)}/{sum(x<0 for x in vals)} PnL=€{sum(vals):+.3f} PF={pft(pf(vals))} AVG=€{(sum(vals)/len(vals) if vals else 0):+.3f}")

def self_test():
    c=[[0,100,101,99.5,100.5,1],[TFMS,100.5,102,100,101.5,1],[2*TFMS,101.5,103,101,102.5,1],[3*TFMS,102.5,103.5,101.8,102,1]]; a=[1]*4
    t={'entry_ms':1,'side':'LONG','entry':100.0,'stop':95.0,'amount':1.0}
    assert sim(t,c,a,1.0,0.0) is not None; assert abs(pf([2,-1])-2)<1e-12
    print('DIAMOND_ADAPTIVE_ATR_TRAILING_EXIT_RESEARCH_SELF_TEST_OK'); return 0

def run(days):
    fee=fee_pct(); trades=load_trades(days)
    if not trades:print('GEEN GESLOTEN CURRENT TRADES IN PERIODE');return 0
    syms=sorted({t['symbol'] for t in trades}); earliest=min(t['entry_ms'] for t in trades)-(ATR_LEN+5)*TFMS; latest=min(int(datetime.now(timezone.utc).timestamp()*1000),max(t['entry_ms'] for t in trades)+MAX_HOLD_MIN*60000)
    ex=ccxt.bitvavo({'enableRateLimit':True}); ex.load_markets(); cm={}; am={}; errs={}
    for sym in syms:
        try:cm[sym]=fetch_all(ex,sym,earliest,latest);am[sym]=atrs(cm[sym])
        except Exception as e:errs[sym]=f'{type(e).__name__}: {e}'
    ev=[]
    for t in trades:
        if t['symbol'] not in cm:continue
        r=dict(t); ok=True
        for m in MULTS:
            v=sim(t,cm[t['symbol']],am[t['symbol']],m,fee)
            if v is None:ok=False;break
            r[f'a{m:.1f}']=v
        if ok:ev.append(r)
    print('='*108);print(f'DIAMOND ADAPTIVE ATR TRAILING EXIT RESEARCH v{VERSION}');print('='*108)
    print(f'Periode: {days}d | bron={len(trades)} | beoordeeld={len(ev)} | markten={len(syms)} | fee/side={fee:.3f}% | ATR=Wilder14 15m | maxhold=48h')
    print('Replay start: eerste volledige 15m candle NA entry (geen pre-entry intrabar leakage)')
    show('CURRENT',[x['recorded'] for x in ev])
    for m in MULTS:show(f'ATR_{m:.1f}',[x[f'a{m:.1f}'] for x in ev])
    print('\n=== DELTA ==='); cur=sum(x['recorded'] for x in ev)
    for m in MULTS:
        vals=[x[f'a{m:.1f}'] for x in ev]; print(f"ATR_{m:.1f}: PnL delta=€{sum(vals)-cur:+.3f} | trade beter={sum(x[f'a{m:.1f}']>x['recorded'] for x in ev)}/{len(ev)}")
    print('\n=== PER ROUTE ===')
    for route in ('LONG_TREND','LONG_MOM','SHORT_MOM'):
        g=[x for x in ev if x['route']==route]
        if not g:continue
        print('---',route,'---');show('CURRENT',[x['recorded'] for x in g])
        for m in MULTS:show(f'ATR_{m:.1f}',[x[f'a{m:.1f}'] for x in g])
    print('\n=== OORDEELREGEL ===')
    if len(ev)<20:print(f'ONVOLDOENDE BEWIJS: n={len(ev)} < 20. Niet invoeren.')
    else:
        scores=[(sum(x[f'a{m:.1f}'] for x in ev),m) for m in MULTS];best=max(scores)
        print('GEEN ATR-VARIANT VERBETERT TOTALE PnL. Huidige exit behouden.' if best[0]<=cur else f'BESTE RESEARCHVARIANT: ATR_{best[1]:.1f} | PnL verbetering=€{best[0]-cur:+.3f}. Nog NIET LIVE invoeren.')
    if errs:
        print('\nAPI-fouten:');[print(k,v) for k,v in sorted(errs.items())]
    print('\n=== VEILIGHEID ===');print('Orders/private API : NEE');print('Publieke netwerkcalls: JA - alleen Bitvavo 15m candles');print('Config/strategie    : ONGEWIJZIGD');print('LIVE                : ONGEWIJZIGD');return 0

def main():
    p=argparse.ArgumentParser();p.add_argument('--self-test',action='store_true');p.add_argument('--days',type=int,default=7);a=p.parse_args();return self_test() if a.self_test else run(max(1,a.days))
if __name__=='__main__':raise SystemExit(main())
