# ETA regression — realistic_v2

This model estimates hours from the prediction timestamp until actual delivery. Training inputs came only from data/realistic_v2.

## Leakage controls

- Used 50,000 feature/target rows and 40 declared predictors.
- Actual delivery, target timestamps, identifiers, ground truth, and anomaly annotations are excluded from predictor inputs.
- The feature bundle passed its point-in-time validation, including event_time and ingestion_time cutoffs.
- Imputation, scaling, and encoding were fitted using training data only; model selection used validation MAE.

## Chronological split

| Split | Rows | Shipments | predicted_at UTC range |
|---|---:|---:|---|
| train | 35,000 | 35,000 | 2026-01-01T05:43:54+00:00 to 2026-07-30T09:50:00+00:00 |
| validation | 7,500 | 7,500 | 2026-07-30T09:56:00+00:00 to 2026-09-13T02:58:54+00:00 |
| test | 7,500 | 7,500 | 2026-09-13T03:15:00+00:00 to 2026-10-28T05:56:00+00:00 |

Cutoffs: train inclusive 2026-07-30T09:50:00+00:00; validation inclusive 2026-09-13T02:58:54+00:00.
Shipment overlap: 0.

## Metrics

MAE and RMSE are in hours; R-squared is unitless.

| Model | Split | MAE hours | RMSE hours | R-squared |
|---|---|---:|---:|---:|
| dummy | train | 10.2786 | 12.1054 | -0.0100 |
| dummy | validation | 10.2773 | 12.2119 | -0.0194 |
| ridge | train | 0.9757 | 1.3708 | 0.9870 |
| ridge | validation | 0.9841 | 1.3702 | 0.9872 |

Selected on validation MAE: ridge.
Selected model test metrics, evaluated once: MAE 1.0091 hours; RMSE 1.4125 hours; R-squared 0.9866.

## Test subgroup errors

Priority and on-time/delayed labels are joined from this dataset's separate ground-truth file only after test predictions. See test_subgroup_errors.csv.

## Preprocessing

Numeric/boolean fields (35) use median imputation and standard scaling. Categorical fields (5) use most-frequent imputation and one-hot encoding. Ridge alpha=1.0.

## Limitations

This is a first model trained on synthetic data. Generator rules may create easier or different patterns than real logistics operations. These results do not establish real-world generalization or production readiness.
