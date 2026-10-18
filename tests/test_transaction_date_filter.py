from datetime import date, datetime, timezone
import unittest

import pandas as pd

from lambda_src.bluecoins_transaction_extractor import filter_recent_transactions


class FilterRecentTransactionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.today = date(2026, 10, 18)

    def test_keeps_six_month_boundary_and_today_but_excludes_older_and_future(self) -> None:
        transactions = pd.DataFrame(
            {
                "Date": [
                    "2026-04-17",
                    "2026-04-18",
                    "2026-10-18",
                    "2026-10-19",
                    None,
                ],
                "TransactionID": [1, 2, 3, 4, 5],
            }
        )

        filtered = filter_recent_transactions(
            transactions,
            as_of_date=self.today,
        )

        self.assertEqual(filtered["TransactionID"].tolist(), [2, 3])

    def test_parses_unix_millisecond_dates(self) -> None:
        cutoff_timestamp = int(
            datetime(2026, 4, 18, tzinfo=timezone.utc).timestamp() * 1000
        )
        transactions = pd.DataFrame(
            {"Date": [cutoff_timestamp, cutoff_timestamp - 86_400_000]}
        )

        filtered = filter_recent_transactions(
            transactions,
            as_of_date=self.today,
        )

        self.assertEqual(filtered["Date"].tolist(), [cutoff_timestamp])

    def test_parses_compact_calendar_dates(self) -> None:
        transactions = pd.DataFrame({"Date": [20260418.0, 20261019.0]})

        filtered = filter_recent_transactions(
            transactions,
            as_of_date=self.today,
        )

        self.assertEqual(filtered["Date"].tolist(), [20260418.0])

    def test_rejects_unparseable_non_null_dates(self) -> None:
        transactions = pd.DataFrame({"Date": ["not-a-date"]})

        with self.assertRaisesRegex(ValueError, "Unable to parse"):
            filter_recent_transactions(transactions, as_of_date=self.today)


if __name__ == "__main__":
    unittest.main()
