# Day-ahead backtest

12-month holdout beginning 2025-11-20; 8758 target hours.

| Model | MAE (MWh/h) | RMSE | MAPE |
|---|---:|---:|---:|
| model | 470.2 | 636.4 | 4.45% |
| seasonal_naive | 681.7 | 915.5 | 6.57% |
| fingrid_forecast | 327.3 | 425.3 | 3.24% |

Each origin is 13:00 Europe/Helsinki and predicts the next local day. Features use the prior week's same-hour load and the mean of the 24 hours observed by issue time; no target-relative future lag or actual future weather is used.

| Hour | MAE |
|---:|---:|
| 0 | 392.1 |
| 1 | 391.4 |
| 2 | 400.2 |
| 3 | 405.4 |
| 4 | 413.8 |
| 5 | 421.8 |
| 6 | 436.6 |
| 7 | 452.3 |
| 8 | 465.0 |
| 9 | 492.0 |
| 10 | 490.0 |
| 11 | 491.8 |
| 12 | 487.7 |
| 13 | 485.2 |
| 14 | 473.2 |
| 15 | 483.8 |
| 16 | 493.2 |
| 17 | 517.7 |
| 18 | 516.0 |
| 19 | 532.6 |
| 20 | 525.3 |
| 21 | 510.5 |
| 22 | 513.0 |
| 23 | 494.4 |

| Month | MAE |
|---:|---:|
| 1 | 755.0 |
| 2 | 812.4 |
| 3 | 538.2 |
| 4 | 432.2 |
| 5 | 344.3 |
| 6 | 280.8 |
| 7 | 210.8 |
| 8 | 236.8 |
| 9 | 307.2 |
| 10 | 476.3 |
| 11 | 724.7 |
| 12 | 553.3 |

Plots: `backtest_by_hour.png`, `backtest_by_month.png`, `feature_importance.png`.