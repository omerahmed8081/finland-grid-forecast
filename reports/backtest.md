# Day-ahead backtest

12-month holdout beginning 2025-11-20; 8758 target hours.

| Model | MAE (MWh/h) | RMSE | MAPE |
|---|---:|---:|---:|
| leakage-safe LightGBM | 486.9 | 665.3 | 4.58% |
| actual-weather oracle (upper-bound scenario) | 421.7 | 551.7 | 4.05% |
| seasonal naive | 681.7 | 915.5 | 6.57% |
| Fingrid published forecast | 327.3 | 425.3 | 3.24% |
| Fingrid residual correction | 388.8 | 485.4 | 3.85% |

Each origin is 13:00 Europe/Helsinki, after Fingrid's daily 12:00 publication, and predicts the next local day. The primary model uses origin-safe load/calendar features and weather known at issue time. The oracle row uses observed target-day weather as a hypothetical upper-bound scenario; it is not a deployable forecast or a guarantee that forecast weather alone could achieve the same score.
Fingrid error correction is a separate Ridge residual model calibrated on 122 pre-holdout forecast hours; its short calibration period limits confidence.
The primary model uses consumption_lag_48h, the 168-hour mart mean available at issue time, and heating_degree_hours alongside its existing features. The 168-hour value is taken from the latest completed mart row at the origin, so target-day rolling values cannot leak into the forecast.
Fingrid's published forecast remains stronger than both the primary model and the residual correction. Fingrid can use operational inputs and forecasting information that are not available in this project; the correction also has only a short 122-hour calibration history.

The oracle-versus-primary MAE difference measures this model's response to actual target-day weather; actual weather is not available at issue time. FMI's open point forecast service does not retain a historical forecast archive ([FMI forecast service documentation](https://github.com/fmidev/smartmet-sofp-backend)), so archived snapshots are saved daily for live predictions instead. Fingrid dataset 165 is published at 12:00 the previous day ([catalogue](https://data.fingrid.fi/en/datasets/165)).

The actual-weather oracle scenario reduced MAE by 13.4% (486.9 to 421.7 MWh/h) in this holdout.
| Hour | MAE |
|---:|---:|
| 0 | 404.7 |
| 1 | 412.2 |
| 2 | 419.2 |
| 3 | 421.3 |
| 4 | 426.1 |
| 5 | 436.6 |
| 6 | 452.4 |
| 7 | 456.4 |
| 8 | 467.4 |
| 9 | 494.5 |
| 10 | 493.8 |
| 11 | 499.7 |
| 12 | 508.2 |
| 13 | 511.8 |
| 14 | 506.9 |
| 15 | 511.0 |
| 16 | 519.0 |
| 17 | 541.2 |
| 18 | 538.0 |
| 19 | 552.5 |
| 20 | 541.3 |
| 21 | 525.8 |
| 22 | 532.1 |
| 23 | 513.3 |

| Month | MAE |
|---:|---:|
| 1 | 815.3 |
| 2 | 990.9 |
| 3 | 515.6 |
| 4 | 436.6 |
| 5 | 346.9 |
| 6 | 262.8 |
| 7 | 205.7 |
| 8 | 248.7 |
| 9 | 311.9 |
| 10 | 508.9 |
| 11 | 712.1 |
| 12 | 529.3 |

Plots: `backtest_by_hour.png`, `backtest_by_month.png`, `feature_importance.png`.