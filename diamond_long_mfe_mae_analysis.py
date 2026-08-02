
#!/usr/bin/env python3
# Diamond Trader LONG MFE/MAE v1.0 - alleen-lezen
import csv,time,math
from collections import defaultdict,deque
from datetime import datetime,timezone
from pathlib import Path
import ccxt,pandas as pd
from diamond_bot import load_yaml,get_cfg,enrich_indicators

C=load_yaml('config.yaml'); F=Path(get_cfg(C,'files.trades_file','/var/data/diamond_transactions.csv'))
OUT=Path('/var/data/diamond_long_mfe_mae.csv'); T=900000; H=[30,60,120,240,720,1440]
def n(v,d=0):
    try:return float(v)
    except:return d
def d(v):
    x=datetime.fromisoformat(str(v).replace('Z','+00:00')); return (x if x.tzinfo else x.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
def longs():
    a=[]
    with F.open(encoding='utf-8',newline='') as f:
        for r in csv.DictReader(f):
            try:r['_d']=d(r['ts']);a.append(r)
            except:pass
    a.sort(key=lambda r:r['_d']);q=defaultdict(deque);z=[]
    for r in a:
        s=r.get('market','').upper();side=r.get('side','').upper()
        if side=='BUY':x={'b':r,'s':None,'m':s};z.append(x);q[s].append(x)
        elif side=='SELL' and q[s]:q[s].popleft()['s']=r
    return z
def data(ex,s,ms):
    a=ex.fetch_ohlcv(s,'15m',since=ms-20*3600000,limit=200) or []
    f=pd.DataFrame(a,columns=['ts','open','high','low','close','volume'])
    for c in ['open','high','low','close','volume']:f[c]=pd.to_numeric(f[c],errors='coerce')
    f=f.dropna().sort_values('ts').reset_index(drop=True)
    return enrich_indicators(f,int(n(get_cfg(C,'signals.sma_fast',20),20)),int(n(get_cfg(C,'signals.sma_slow',60),60)),int(n(get_cfg(C,'signals.rsi_len',14),14)),int(n(get_cfg(C,'signals.atr_len',14),14)))
def ev(p,sl,tg):
    for _,c in p.iterrows():
        if n(c.low)<=sl:return 'SL',int(c.ts)
        if n(c.high)>=tg:return 'T',int(c.ts)
    return 'N',None
def one(ex,x,i):
    b,s=x['b'],x['s']; ms=int(b['_d'].timestamp()*1000); e=n(b['price']);f=data(ex,x['m'],ms)
    p=f[(f.ts+T)<=ms];ix=p.index[-1];g=f.loc[ix];a=n(g.atr)
    if a<=0 or math.isnan(a):raise RuntimeError('ATR ontbreekt')
    sl=e-a*n(get_cfg(C,'signals.atr_sl_mult',1.2),1.2);tp=e+a*n(get_cfg(C,'signals.atr_tp_mult',2.6),2.6)
    st=((ms+T-1)//T)*T; post=f[(f.ts>=st)&(f.ts<ms+24*3600000)].copy();p60=f.loc[max(0,ix-3):ix]
    r={'trade':i,'symbol':x['m'],'entry_utc':b['_d'].isoformat(),'entry':e,'atr':a,'atr_pct':a/e*100,'rsi':n(g.rsi),'actual_pnl':n(s.get('net_pnl_quote')) if s else 0,'actual_reason':s.get('reason','') if s else '','pre60_runup_atr':(n(g.close)-n(p60.low.min(),e))/a,'dist_sma20_atr':(n(g.close)-n(g.sma_fast))/a}
    for m in H:
        w=post[post.ts<ms+m*60000]
        if w.empty:r[f'mfe{m}_pct']=r[f'mae{m}_pct']=r[f'mfe{m}_atr']=r[f'mae{m}_atr']=None
        else:
            hi,lo=n(w.high.max(),e),n(w.low.min(),e);r[f'mfe{m}_pct']=max(0,(hi/e-1)*100);r[f'mae{m}_pct']=max(0,(1-lo/e)*100);r[f'mfe{m}_atr']=(hi-e)/a;r[f'mae{m}_atr']=(e-lo)/a
    q1,t1=ev(post,sl,e*1.01);qt,tt=ev(post,sl,tp);r['sl_before_1pct']=q1=='SL';r['sl_before_tp']=qt=='SL'
    w4=post[post.ts<ms+240*60000];r['sl_hit_4h']=bool((w4.low<=sl).any()) if not w4.empty else False
    sts=post[post.low<=sl].ts.tolist();later=post[post.ts>int(sts[0])] if sts else post.iloc[0:0]
    r['recovered_1pct_after_sl']=bool((later.high>=e*1.01).any()) if not later.empty else False;r['recovered_tp_after_sl']=bool((later.high>=tp).any()) if not later.empty else False
    lows=[];r['reached_1atr']=False;r['mae_before_1atr_atr']=None
    for _,c in post.iterrows():
        if n(c.high)>=e+a:r['reached_1atr']=True;r['mae_before_1atr_atr']=(e-min(lows))/a if lows else 0;break
        lows.append(n(c.low,e))
    return r
def av(a,k):
    v=[n(x[k],math.nan) for x in a if x.get(k) is not None];v=[x for x in v if not math.isnan(x)];return sum(v)/len(v) if v else 0

L=longs();print(f'LONG entries gevonden: {len(L)} (verwacht 18)')
E=ccxt.bitvavo({'enableRateLimit':True,'options':{'fetchMarkets':{'types':['spot']}}});E.load_markets();R=[]
for i,x in enumerate(L,1):
    try:
        r=one(E,x,i);R.append(r);print(f"{i:02d} {r['symbol']:7s} pnl={r['actual_pnl']:+6.2f} MFE4h={r['mfe240_pct']:5.2f}% MAE4h={r['mae240_pct']:5.2f}% pre60={r['pre60_runup_atr']:4.2f}ATR SL4h={'JA' if r['sl_hit_4h'] else 'NEE'}")
    except Exception as e:print(f'{i:02d} {x["m"]}: FOUT {e}')
    time.sleep(.1)
if not R:raise SystemExit('Geen resultaten')
with OUT.open('w',encoding='utf-8',newline='') as f:w=csv.DictWriter(f,fieldnames=list(R[0]));w.writeheader();w.writerows(R)
Q=[r for r in R if r['reached_1atr']];N=len(R)
print('\n===== SAMENVATTING =====')
print(f'Geanalyseerd                 : {N}/18')
print(f'Werkelijk winst/verlies      : {sum(r["actual_pnl"]>0 for r in R)}/{sum(r["actual_pnl"]<0 for r in R)}')
print(f'Werkelijke netto PnL         : €{sum(r["actual_pnl"] for r in R):+.4f}')
print(f'Gem. MFE / MAE 60m           : {av(R,"mfe60_pct"):.3f}% / {av(R,"mae60_pct"):.3f}%')
print(f'Gem. MFE / MAE 240m          : {av(R,"mfe240_pct"):.3f}% / {av(R,"mae240_pct"):.3f}%')
print(f'1.2 ATR SL geraakt <=4h      : {sum(r["sl_hit_4h"] for r in R)}/{N}')
print(f'1.2 ATR SL vóór +1%          : {sum(r["sl_before_1pct"] for r in R)}/{N}')
print(f'SL en daarna alsnog +1%      : {sum(r["sl_before_1pct"] and r["recovered_1pct_after_sl"] for r in R)}/{N}')
print(f'SL en daarna alsnog 2.6 ATR  : {sum(r["sl_before_tp"] and r["recovered_tp_after_sl"] for r in R)}/{N}')
print(f'Bereikte +1 ATR <=24h        : {len(Q)}/{N}')
print(f'>1.2 ATR terugval vóór +1ATR : {sum(n(r["mae_before_1atr_atr"])>1.2 for r in Q)}/{len(Q)}')
print(f'Gem. pre-entry run-up 60m    : {av(R,"pre60_runup_atr"):.3f} ATR')
print(f'Pre-entry run-up >=1.0 ATR   : {sum(r["pre60_runup_atr"]>=1 for r in R)}/{N}')
print(f'Pre-entry run-up >=1.5 ATR   : {sum(r["pre60_runup_atr"]>=1.5 for r in R)}/{N}')
print(f'Gem. afstand boven SMA20     : {av(R,"dist_sma20_atr"):.3f} ATR')
print(f'RSI >=67 bij entry           : {sum(r["rsi"]>=67 for r in R)}/{N}')
print('\nPER MUNT')
for s in sorted(set(r['symbol'] for r in R)):
    g=[r for r in R if r['symbol']==s];print(f'{s:7s} n={len(g):2d} pnl=€{sum(r["actual_pnl"] for r in g):+7.3f} MFE4h={av(g,"mfe240_pct"):5.2f}% MAE4h={av(g,"mae240_pct"):5.2f}% SL4h={sum(r["sl_hit_4h"] for r in g)}')
print(f'\nDetails: {OUT}\nGeen config/state/strategie gewijzigd.')
