#!/usr/bin/env python3
import runpy

v = runpy.run_path(
    "diamond_quality_ranking_v1_1.py"
)

groups = v["groups"]
metrics = v["metrics"]
prospective = v["prospective_closed"]
errors = v.get("errors", [])
friction = v.get("EXTRA_FRICTION", 0.30)

ranking = []

for name, rows in groups.items():
    n, w, l, pnl, pf = metrics(rows, 0.0)
    sn, sw, sl, spnl, spf = metrics(
        rows,
        friction,
    )

    sort_pf = (
        999.0
        if spf == "inf"
        else float(spf)
    )

    ranking.append((
        sort_pf,
        name,
        n, w, l,
        pnl, pf,
        spnl, spf,
    ))

ranking.sort(
    key=lambda x: x[0],
    reverse=True,
)

def pf_text(value):
    if value == "inf":
        return "inf"
    return f"{float(value):.2f}"


print("=" * 88)
print(" DIAMOND TRADER QUALITY RANKING v1.2")
print("=" * 88)
print()
print(
    f"Ranking op PF na +{friction:.2f}% "
    "extra frictie per kant."
)
print(
    "LONG/SHORT prospectief = werkelijk "
    "gesloten trades na eigen baseline."
)
print(
    "SELECTIVE/STRONG = bestaand shadow-totaal; "
    "niet dezelfde prospectieve gate."
)
print()

for i, item in enumerate(ranking, 1):
    (
        _,
        name,
        n, w, l,
        pnl, pf,
        spnl, spf,
    ) = item

    if name in {
        "SELECTIVE",
        "STRONG",
    }:
        status = (
            f"shadow_totaal={n} | "
            "prospectief=N.V.T."
        )
    else:
        closed = len(
            prospective.get(name, [])
        )
        status = f"prospectief={closed}/20"

    print(
        f"{i}. {name:<24} "
        f"hist={n:>2} W/L={w:>2}/{l:<2} "
        f"pnl=€{pnl:+.2f} "
        f"PF={pf_text(pf):<5} | "
        f"stress=€{spnl:+.2f} "
        f"PF={pf_text(spf):<5} | "
        f"{status}"
    )

print()
print(f"Candle/API fouten: {len(errors)}")

for error in errors[:10]:
    print(" -", error)

print(
    "GEEN live-goedkeuring: "
    "prospectieve gates blijven leidend."
)
print(
    "Orders: NEE | Config: NEE | "
    "Live wijziging: NEE"
)
