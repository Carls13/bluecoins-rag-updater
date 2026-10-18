from datetime import date
from sqlite3 import Error as SQLiteError, connect
import os
import sys

import pandas as pd


TRANSACTIONS_QUERY = """
SELECT
    T.transactionsTableID as TransactionID,
    T.date as Date,
    I.itemName as Title,
    (CAST(T.amount AS float) / 1000000.0) as Amount,
    T.transactionCurrency as Currency,
    T.conversionRateNew as ConversionRate,
    A.accountName as Account,
    AT.accountTypeName as AccountType,
    C.childCategoryName as Category,
    P.parentCategoryName as ParentCategory,
    CG.categoryGroupName as CategoryGroup,
    TT.transactionTypeName as TransactionType,
    T.notes as Notes,
    T.status as StatusCode,
    T.accountReference as ToAccountID,
    A2.accountName as ToAccount,
    T.deletedTransaction as DeletedFlag,
    T.uidPairID as TransferPairID,
    T.reminderTransaction as RecurringFlag,
    T.creditCardInstallment as CreditCardInstallment
FROM TRANSACTIONSTABLE as T
LEFT JOIN ITEMTABLE as I ON T.itemID = I.itemTableID
LEFT JOIN ACCOUNTSTABLE as A ON T.accountID = A.accountsTableID
LEFT JOIN ACCOUNTTYPETABLE as AT ON A.accountTypeID = AT.accountTypeTableID
LEFT JOIN CHILDCATEGORYTABLE as C ON T.categoryID = C.categoryTableID
LEFT JOIN PARENTCATEGORYTABLE as P ON C.parentCategoryID = P.parentCategoryTableID
LEFT JOIN CATEGORYGROUPTABLE as CG ON P.categoryGroupID = CG.categoryGroupTableID
LEFT JOIN TRANSACTIONTYPETABLE as TT ON T.transactionTypeID = TT.transactionTypeTableID
LEFT JOIN ACCOUNTSTABLE as A2 ON T.accountReference = A2.accountsTableID
ORDER BY T.date DESC
"""


def _parse_transaction_dates(values: pd.Series) -> pd.Series:
    non_null_values = values.dropna()
    if non_null_values.empty:
        return pd.to_datetime(values, errors="coerce", utc=True)

    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        parsed = pd.to_datetime(values, errors="coerce", utc=True)
    else:
        numeric_values = pd.to_numeric(non_null_values, errors="coerce")
        if numeric_values.notna().all():
            magnitude = numeric_values.abs().median()
            if 2_000_000 <= magnitude <= 3_000_000:
                parsed = pd.to_datetime(
                    pd.to_numeric(values, errors="coerce"),
                    unit="D",
                    origin="julian",
                    errors="coerce",
                    utc=True,
                )
            elif 19_000_101 <= magnitude <= 22_991_231:
                parsed = pd.to_datetime(
                    pd.to_numeric(values, errors="coerce")
                    .round()
                    .astype("Int64")
                    .astype("string"),
                    format="%Y%m%d",
                    errors="coerce",
                    utc=True,
                )
            else:
                unit = (
                    "ns"
                    if magnitude >= 1e17
                    else "us"
                    if magnitude >= 1e14
                    else "ms"
                    if magnitude >= 1e11
                    else "s"
                    if magnitude >= 1e8
                    else None
                )
                if unit is None:
                    raise ValueError(
                        "Transaction dates are numeric but are not a recognized "
                        "Unix timestamp or calendar date."
                    )
                parsed = pd.to_datetime(
                    pd.to_numeric(values, errors="coerce"),
                    unit=unit,
                    errors="coerce",
                    utc=True,
                )
        else:
            parsed = pd.to_datetime(
                values.astype("string"),
                format="mixed",
                errors="coerce",
                utc=True,
            )

    invalid_values = values.notna() & parsed.isna()
    if invalid_values.any():
        examples = values.loc[invalid_values].head(3).tolist()
        raise ValueError(
            f"Unable to parse {int(invalid_values.sum())} transaction date(s); "
            f"example values: {examples}"
        )
    return parsed


def filter_recent_transactions(
    transactions: pd.DataFrame,
    *,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Keep transactions dated within the last six calendar months through today."""
    if "Date" not in transactions.columns:
        raise ValueError("Transactions are missing the required Date column.")

    today = as_of_date or date.today()
    cutoff = pd.Timestamp(today) - pd.DateOffset(months=6)
    parsed_dates = _parse_transaction_dates(transactions["Date"])
    transaction_days = parsed_dates.dt.tz_convert(None).dt.normalize().dt.date
    keep = (
        transaction_days.notna()
        & (transaction_days >= cutoff.date())
        & (transaction_days <= today)
    )
    return transactions.loc[keep].copy()


def extract_all_transactions(
    dbname: str,
    output_file: str | None = "all_transactions.csv",
    *,
    verbose: bool = True,
    as_of_date: date | None = None,
) -> pd.DataFrame:
    """Extract transactions from the last six months, optionally writing a CSV."""
    if not os.path.isfile(dbname):
        raise FileNotFoundError(f"Database file '{dbname}' does not exist.")

    with connect(dbname) as conn:
        total_count = conn.execute(
            "SELECT COUNT(*) FROM TRANSACTIONSTABLE"
        ).fetchone()[0]
        df = pd.read_sql_query(TRANSACTIONS_QUERY, conn)

    extracted_count = len(df)
    df = filter_recent_transactions(df, as_of_date=as_of_date)

    df["Status"] = df["StatusCode"].map(
        {
            0: "Cleared",
            1: "Pending",
            2: "Void",
        }
    ).fillna("Unknown")

    if output_file is not None:
        df.to_csv(output_file, index=False)

    if verbose:
        print(f"Total transactions in database: {total_count}")
        print(
            f"Kept {len(df)} of {extracted_count} extracted transactions "
            f"from the last six months through {(as_of_date or date.today()).isoformat()}"
        )
        print(f"Successfully exported {len(df)} transactions")
        print(f"Filtered by deleted flag: {len(df[df['DeletedFlag'] != 6])} transactions")

        preview_columns = [
            "TransactionID",
            "Date",
            "Title",
            "Amount",
            "Currency",
            "Account",
            "Category",
            "TransactionType",
            "Status",
        ]
        print("\nPreview of first 5 transactions:")
        print(df[preview_columns].head(5).to_string(index=False))
        print("\nPreview of last 5 transactions:")
        print(df[preview_columns].tail(5).to_string(index=False))

        if len(df) < total_count:
            print(f"\nWARNING: Only extracted {len(df)} of {total_count} transactions!")
            null_counts = df.isnull().sum()
            print("\nNull value counts in key fields:")
            for column in ("Title", "Account", "Category", "TransactionType"):
                print(f"  {column}: {null_counts[column]}")

    return df


def debug_database_structure(dbname: str) -> None:
    """Print table counts and sample transaction rows for database diagnostics."""
    if not os.path.isfile(dbname):
        raise FileNotFoundError(f"Database file '{dbname}' does not exist.")

    with connect(dbname) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = cursor.fetchall()

        print("\n==== DATABASE STRUCTURE DEBUG INFO ====")
        for (table_name,) in tables:
            cursor.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            print(f"{table_name}: {cursor.fetchone()[0]} rows")

        print("\nDirect TRANSACTIONSTABLE query (first 3 rows):")
        cursor.execute(
            "SELECT transactionsTableID, date, amount, transactionTypeID, "
            "deletedTransaction FROM TRANSACTIONSTABLE LIMIT 3"
        )
        for row in cursor.fetchall():
            print(row)


def main() -> None:
    dbname = sys.argv[1] if len(sys.argv) > 1 else "bluecoins.fydb"
    command = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        if command == "debug":
            debug_database_structure(dbname)
        else:
            output_file = command if command is not None else "all_transactions.csv"
            extract_all_transactions(dbname, output_file)
    except (OSError, SQLiteError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
