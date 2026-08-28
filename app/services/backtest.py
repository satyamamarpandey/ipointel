from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import IPO, ScoreSnapshot, PerformanceSnapshot

def brier(rows):
    vals=[]
    for p,y in rows: vals.append((p/100-y)**2)
    return sum(vals)/len(vals) if vals else None

def summarize(db:Session):
    ipos=db.scalars(select(IPO).where(IPO.status=="Listed")).all(); samples=[]; top=[]
    for ipo in ipos:
        score=db.scalar(select(ScoreSnapshot).where(ScoreSnapshot.ipo_id==ipo.id).order_by(ScoreSnapshot.created_at.asc()).limit(1))
        perf=db.scalar(select(PerformanceSnapshot).where(PerformanceSnapshot.ipo_id==ipo.id).order_by(PerformanceSnapshot.created_at.desc()).limit(1))
        if score and perf and perf.listing_return_pct is not None:
            y=1 if perf.listing_return_pct>0 else 0;samples.append((score.listing_gain_probability,y));top.append((score.overall_score,perf.listing_return_pct))
    top.sort(reverse=True); top10=top[:max(1,len(top)//10)] if top else []
    return {"sample_size":len(samples),"brier_score":round(brier(samples),4) if samples else None,"top_decile_avg_listing_return_pct":round(sum(x[1] for x in top10)/len(top10),2) if top10 else None,"status":"calibrated" if len(samples)>=100 else "insufficient sample — calibration display only"}
