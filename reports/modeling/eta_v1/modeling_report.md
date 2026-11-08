# ETA regression baseline results

Target: `target_remaining_hours` (hours remaining from `predicted_at` to actual delivery).
The target and actual delivery time were used only as labels, never as predictors.

## Chronological split

| Split | Rows | Shipments | Prediction time range (UTC) |
|---|---:|---:|---|
| Train | 35,000 | 35,000 | 2026-01-01 05:43:54+00:00 to 2026-07-30 09:50:00+00:00 |
| Validation | 7,500 | 7,500 | 2026-07-30 09:56:00+00:00 to 2026-09-13 02:58:54+00:00 |
| Test | 7,500 | 7,500 | 2026-09-13 03:15:00+00:00 to 2026-10-28 05:56:00+00:00 |

Train cutoff (inclusive): `2026-07-30 09:50:00+00:00`.
Validation cutoff (inclusive): `2026-09-13 02:58:54+00:00`.
Shipment overlap across splits: **0**.

## Training and validation metrics

MAE and RMSE are prediction errors in hours; lower is better. R-squared is unitless; values closer to 1 indicate a closer fit.
| Model | Split | MAE (hours) | RMSE (hours) | R-squared |
|---|---|---:|---:|---:|
| Dummy | Train | 9.9006 | 11.6751 | -0.0071 |
| Dummy | Validation | 9.8550 | 11.7433 | -0.0175 |
| Ridge | Train | 1.5532 | 2.0038 | 0.9703 |
| Ridge | Validation | 1.6134 | 2.0623 | 0.9686 |

Selected using validation MAE (RMSE breaks ties): **Ridge**.
The test window was not used for fitting or selection. After selection, only the chosen model was evaluated on the test window once.

## Selected model test result

- MAE: **1.6038 hours**
- RMSE: **2.0468 hours**
- R-squared: **0.9697**

Ridge improves substantially over the median baseline on validation: validation MAE is lower by 8.2415 hours. The held-out test metrics are also strong for this generated dataset, but they do not establish real-world performance; validate on representative production data before relying on ETA estimates.

## Predictor and preprocessing decisions

Used 32 operational predictors: 27 numeric inputs (including 8 boolean 0/1 flags) and 5 categorical inputs.
Numeric values use median imputation and standard scaling. Categories use most-frequent imputation and one-hot encoding with unseen categories ignored. Every transformation was fit inside the Ridge pipeline on training data only.
Six constant predictors and nine numeric predictor pairs with absolute Pearson correlation at least 0.95 were inspected and retained for this baseline. Constants contribute no variation; the correlated route/time fields may be redundant. Ridge regularization addresses some redundancy, but a later feature ablation should measure whether removing these fields helps.
Identifiers (`shipment_id`, `eta_id`, `predicted_at`) were preserved only for split assignment/output. Actual outcomes, targets, ground truth and anomaly audit/evidence fields were excluded from predictor columns.

## Reproduction

Run `python -m src.prediction.train_eta` to reproduce this training workflow. It refuses to overwrite the existing model/report output directories.
