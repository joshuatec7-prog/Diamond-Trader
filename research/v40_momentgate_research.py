#!/usr/bin/env python3
"""
v4.0 Momentgate reproducibility research
OFFLINE / RESEARCH ONLY
- Uses the existing master dataset.
- 60d development + 15d validation.
- Final 15d holdout is NOT evaluated.
- 24h purge because the target uses 4h/8h/12h/24h net forward outcomes.
"""
from pathlib import Path
import argparse, json
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

DAY = 86_400_000
HORIZONS = ("240","480","720","1440")
MODEL_PARAMS = dict(
    learning_rate=0.05,
    max_iter=150,
    max_leaf_nodes=15,
    min_samples_leaf=40,
    l2_regularization=2.0,
    random_state=260916,
)

PASS_RULES = {
    "n_at_least": 100,
    "pf_at_least": 1.20,
    "positive_horizons_at_least": 3,
    "distinct_markets_at_least": 20,
    "max_market_share_pct": 20.0,
    "distinct_days_at_least": 8,
    "max_day_share_pct": 20.0,
}

def load_dataset(path):
    data=json.loads(Path(path).read_text(encoding="utf-8"))
    start=data["period"]["signal_start_ms"]
    train_boundary=start+60*DAY
    val_boundary=start+75*DAY
    end=data["period"]["signal_end_ms_exclusive"]
    purge=24*60*60*1000

    recs=[]
    for r in data["rows"]:
        x={
            "market":r["market"], "signal_ms":r["signal_ms"], "route":r["route"],
            "base_score":float(r["base_score"]),
            "relative_strength_vs_btc_1h_pct":float(r["relative_strength_vs_btc_1h_pct"]),
            "net_reward_risk":float(r["net_reward_risk"]),
        }
        for k,v in r["entry_features"].items(): x["ef__"+k]=float(v)
        for k,v in r["market_context"].items(): x["mc__"+k]=float(v)
        for k,v in r["cross_section"].items(): x["cs__"+k]=float(v)

        # Deliberately do not touch final-holdout labels.
        if r["signal_ms"] < val_boundary:
            vals=[]
            for h in HORIZONS:
                val=float(r["forward_labels"][h]["net_close_pct"])
                x["net_"+h]=val
                vals.append(val)
            x["target"]=float(np.mean(vals))
        else:
            for h in HORIZONS: x["net_"+h]=np.nan
            x["target"]=np.nan
        recs.append(x)

    df=pd.DataFrame(recs)
    train=df[(df.signal_ms>=start)&(df.signal_ms<train_boundary-purge)].copy()
    val=df[(df.signal_ms>=train_boundary)&(df.signal_ms<val_boundary-purge)].copy()
    return data, df, train, val, (start, train_boundary, val_boundary, end, purge)

def metric(sel):
    y=sel.target.to_numpy(float)
    pos=y[y>0].sum()
    neg=-y[y<0].sum()
    dates=pd.to_datetime(sel.signal_ms,unit="ms",utc=True).dt.date
    out={
        "n":int(len(sel)),
        "mean_net_pct":float(np.mean(y)) if len(y) else None,
        "median_net_pct":float(np.median(y)) if len(y) else None,
        "profit_factor":float(pos/neg) if neg>0 else None,
        "win_rate_pct":float(np.mean(y>0)*100) if len(y) else None,
        "distinct_markets":int(sel.market.nunique()),
        "distinct_days":int(dates.nunique()),
        "max_market_share_pct":float(sel.market.value_counts(normalize=True).iloc[0]*100) if len(sel) else None,
        "max_day_share_pct":float(dates.value_counts(normalize=True).iloc[0]*100) if len(sel) else None,
    }
    for h in HORIZONS:
        out["mean_net_"+h+"m_pct"]=float(sel["net_"+h].mean())
    return out

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--output", default="v40_momentgate_research_result.json")
    args=ap.parse_args()

    data,df,train,val,bounds=load_dataset(args.dataset)
    start,train_boundary,val_boundary,end,purge=bounds

    exclude={"market","signal_ms","route","target",*[f"net_{h}" for h in HORIZONS]}
    num_cols=[c for c in df.columns if c not in exclude]
    route_names=sorted(df.route.unique())

    def matrix(frame):
        X=frame[num_cols].astype(float).copy()
        for rt in route_names:
            X["route__"+rt]=(frame.route.values==rt).astype(float)
        return X

    def choose_baseline(frame):
        return frame.loc[frame.groupby("signal_ms").base_score.idxmax()].copy()

    def select_scored(frame):
        cand=frame[frame.pred>0].copy()
        if cand.empty: return cand
        return cand.loc[cand.groupby("signal_ms").pred.idxmax()].copy()

    baseline_train=metric(choose_baseline(train))
    baseline_val=metric(choose_baseline(val))

    # Expanding blocked OOF on development period only.
    oof=[]
    for a,b in ((30,40),(40,50),(50,59)):
        fit_end=start+a*DAY-purge
        ev_start=start+a*DAY
        ev_end=min(start+b*DAY, train_boundary-purge)
        fit=train[(train.signal_ms>=start)&(train.signal_ms<fit_end)]
        ev=train[(train.signal_ms>=ev_start)&(train.signal_ms<ev_end)].copy()
        model=HistGradientBoostingRegressor(**MODEL_PARAMS)
        model.fit(matrix(fit),fit.target)
        ev["pred"]=model.predict(matrix(ev))
        oof.append(ev)
    oof_sel=select_scored(pd.concat(oof).sort_values("signal_ms"))
    oof_metrics=metric(oof_sel)

    final=HistGradientBoostingRegressor(**MODEL_PARAMS)
    final.fit(matrix(train),train.target)
    scored=val.copy()
    scored["pred"]=final.predict(matrix(val))
    selected=select_scored(scored)
    vm=metric(selected)

    pos_h=sum(vm[f"mean_net_{h}m_pct"]>0 for h in HORIZONS)
    checks={
        "n_at_least_100":vm["n"]>=100,
        "mean_net_positive":vm["mean_net_pct"]>0,
        "pf_at_least_1_20":vm["profit_factor"] is not None and vm["profit_factor"]>=1.20,
        "positive_horizons_at_least_3_of_4":pos_h>=3,
        "distinct_markets_at_least_20":vm["distinct_markets"]>=20,
        "max_market_share_at_most_20pct":vm["max_market_share_pct"]<=20,
        "distinct_days_at_least_8":vm["distinct_days"]>=8,
        "max_day_share_at_most_20pct":vm["max_day_share_pct"]<=20,
    }

    result={
        "mode":"OFFLINE_RESEARCH_ONLY",
        "active_paper_changed":False,
        "live_orders_possible":False,
        "holdout_15d_evaluated":False,
        "dataset_version":data["version"],
        "dataset_candidates":data["candidates"],
        "markets_completed":data["markets_completed"],
        "target":"mean net close return across 4h/8h/12h/24h",
        "purge_hours":24,
        "feature_count":len(num_cols)+len(route_names),
        "model":"HistGradientBoostingRegressor fixed parameters",
        "selection":"one candidate per signal_ms; require predicted net target > 0",
        "baseline_training":baseline_train,
        "baseline_validation":baseline_val,
        "nonlinear_oof_training":oof_metrics,
        "nonlinear_validation":vm,
        "pass_checks":checks,
        "validation_pass":all(checks.values()),
        "decision":"REJECT_NONLINEAR_SELECTOR" if not all(checks.values()) else "FREEZE_FOR_ONE_TIME_HOLDOUT",
    }
    Path(args.output).write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result,indent=2))

if __name__=="__main__":
    main()
