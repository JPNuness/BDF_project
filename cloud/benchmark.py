"""Benchmark in Athena: is Parquet better than CSV?

It asks 2 questions from the notebook in Athena, once on the CSV files and once on the
Parquet files. For each one it writes down the run time, the data scanned (what Athena
reads and bills) and the cost, and checks that both answers are the same.

How to run (after cloud/aws_setup.py):

    python cloud/benchmark.py                    # MovieLens 100k
    python cloud/benchmark.py --dataset ml-32m   # MovieLens 32M

Result: cloud/benchmark_results_<dataset>.csv (opens in Excel).
"""

import argparse
import csv
from pathlib import Path

import aws_setup
from aws_setup import DATA_BUCKET, ROOT, athena_query

# ---------------------------------------------------------------------------------------
# The 2 questions. {ratings}, {titles} and {votes} are replaced by the CSV or the Parquet
# version of each table. {only_films} is needed for the IMDb CSV, which has every kind of
# title (series, episodes...); the Parquet file already holds films only.
# ---------------------------------------------------------------------------------------
QUESTIONS = {
    "Average rating per popularity level (MovieLens)": """
        WITH per_movie AS (
            SELECT movieid, COUNT(*) AS n, AVG(rating) AS avg_rating
            FROM {ratings} GROUP BY movieid)
        SELECT CASE WHEN n = 1 THEN '1' WHEN n < 5 THEN '2-4' WHEN n < 10 THEN '5-9'
                    WHEN n < 20 THEN '10-19' WHEN n < 50 THEN '20-49'
                    WHEN n < 100 THEN '50-99' ELSE '100+' END AS popularity,
               COUNT(*) AS movies, ROUND(AVG(avg_rating), 2) AS mean_rating
        FROM per_movie GROUP BY 1 ORDER BY MIN(n)""",
    "IMDb rating per number of votes (IMDb)": """
        SELECT CASE WHEN v.numvotes < 1000 THEN '100-999' WHEN v.numvotes < 10000 THEN '1k-9.9k'
                    WHEN v.numvotes < 100000 THEN '10k-99k'
                    WHEN v.numvotes < 1000000 THEN '100k-999k' ELSE '1M+' END AS votes,
               COUNT(*) AS films, ROUND(AVG(v.averagerating), 2) AS mean_rating
        FROM {titles} t JOIN {votes} v ON v.tconst = t.tconst
        WHERE v.numvotes >= 100 {only_films}
        GROUP BY 1 ORDER BY MIN(v.numvotes)""",
}


# The Athena tables for each format (same database: bdf_project or bdf_project_32m)
TABLES = {
    "CSV": dict(ratings="raw_ratings", titles="raw_imdb_basics", votes="raw_imdb_ratings",
                only_films="AND t.titletype = 'movie'"),
    "Parquet": dict(ratings="ratings", titles="imdb_basics", votes="imdb_ratings", only_films=""),
}


def create_csv_tables_in_athena(dataset):
    """Tell Athena where the CSV files are. (The Parquet tables were made by the crawler.)"""
    s3 = f"s3://{DATA_BUCKET}/data"
    skip_header = "TBLPROPERTIES ('skip.header.line.count' = '1')"
    athena_query(f"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_ratings
        (userid bigint, movieid bigint, rating double, `timestamp` bigint)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
        LOCATION '{s3}/{dataset}/ratings/' {skip_header}""")
    athena_query(rf"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_imdb_basics
        (tconst string, titletype string, primarytitle string, originaltitle string, isadult string,
         startyear string, endyear string, runtimeminutes string, genres string)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY '\t'
        LOCATION '{s3}/imdb/title.basics/' {skip_header}""")
    athena_query(rf"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_imdb_ratings
        (tconst string, averagerating double, numvotes int)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY '\t'
        LOCATION '{s3}/imdb/title.ratings/' {skip_header}""")


def ask(sql):
    """Ask Athena one question. Returns (answer, run time in ms, MB scanned)."""
    answer, stats = athena_query(sql)
    return answer, stats["EngineExecutionTimeInMillis"], stats["DataScannedInBytes"] / 1e6


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=aws_setup.DATASETS, default="ml-100k")
    dataset = parser.parse_args().dataset

    aws_setup.set_dataset(dataset)
    aws_setup.check_credentials()
    create_csv_tables_in_athena(dataset)

    results = []
    for question, sql in QUESTIONS.items():
        print(f"\n{question}")
        first_answer = None
        for file_format, tables in TABLES.items():
            # ask 3 times and keep the middle time, so one slow run does not count
            runs = [ask(sql.format(**tables)) for _ in range(3)]
            answer, _, mb_scanned = runs[0]
            ms = sorted(run[1] for run in runs)[1]
            # Athena: $5 per TB scanned, at least 10 MB per question
            cost = max(mb_scanned, 10) / 1e6 * 5

            first_answer = first_answer or answer
            same = answer == first_answer
            results.append(dict(dataset=dataset, question=question, format=file_format, time_ms=ms,
                                mb_scanned=round(mb_scanned, 2), cost_usd=cost, same_answer=same))
            print(f"  {file_format:<8}{ms:>8,} ms{mb_scanned:>12,.2f} MB scanned  ${cost:.6f}"
                  + ("" if same else "   DIFFERENT ANSWER!"))

    output = Path(__file__).with_name(f"benchmark_results_{dataset}.csv")
    with output.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=results[0].keys())
        writer.writeheader()
        writer.writerows(results)
    print(f"\nSaved {output.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
