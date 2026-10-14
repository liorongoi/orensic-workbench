# Detector validation

Generated 2026-10-14 by `python -m forensic.validation run` on synthetic photo-like scenes generated from fixed seeds (forensic.validation.run.scene).

These numbers come from a seeded synthetic corpus. They show each detector behaves as its method predicts and guard against regressions; they are not real-world accuracy. Run with `--pristine <folder of camera JPEGs>` (or on public datasets) to measure on real images before relying on a detector in casework.

| Detector | Task | Threshold | TPR | FPR | AUC |
|---|---|---|---|---|---|
| A-DJPG | double compression (Q1 < Q2) | 0.05 | 1.0 | 0.0 | 1.0 |
| NA-DJPG grid | crop between saves | 1.0 | 0.55 | 0.0 | 0.8744 |
| PRNU | same camera, full frame | PCE 60.0 | 1.0 | 0.0 | 1.0 |
| PRNU | same camera, quarter crop, searched | PCE 60.0 | 1.0 | 0.0 | 1.0 |

A-DJPG first-quality estimate within ±3: 0.9417 (mean error 0.64). Detection when Q1 > Q2 (expected to fail): 0.25.

A-DJPG splice localisation: mean per-block AUC 0.962, mean F1 0.4144.

NA-DJPG by first quality: {'Q1=60': 0.9722, 'Q1=75': 0.8378, 'Q1=90': 0.0}; offset correct when detected: 1.0.

Full results, conditions and known failure modes are in the detector cards (`backend/forensic/validation/cards/`), which the app shows next to each detector and prints in reports.
