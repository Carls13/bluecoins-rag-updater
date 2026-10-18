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


def extract_all_transactions(
    dbname: str,
    output_file: str | None = "all_transactions.csv",
    *,
    verbose: bool = True,
) -> pd.DataFrame:
    """Extract all Bluecoins transactions, optionally writing them to CSV."""
    if not os.path.isfile(dbname):
        raise FileNotFoundError(f"Database file '{dbname}' does not exist.")

    with connect(dbname) as conn:
        total_count = conn.execute(
            "SELECT COUNT(*) FROM TRANSACTIONSTABLE"
        ).fetchone()[0]
        df = pd.read_sql_query(TRANSACTIONS_QUERY, conn)

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
