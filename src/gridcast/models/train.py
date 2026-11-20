"""Train and persist a final day-ahead model through the backtest pipeline."""

from gridcast.models.backtest import run_backtest


def main() -> None:
    """Produce a versioned MLflow run and evaluation report."""
    print(run_backtest())


if __name__ == "__main__":
    main()
