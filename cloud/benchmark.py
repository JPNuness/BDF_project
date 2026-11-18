"""Benchmark: the same queries on raw CSV/TSV vs Parquet, locally (DuckDB) and in Athena.

Run from the project root after cloud/aws_setup.py:

    python cloud/benchmark.py

Creates the raw_* Athena tables over data/ in S3 (DDL, not the crawler: the crawler would
read links.imdbId as a number and drop its leading zeros), runs every query REPEATS times
per engine and format, and writes cloud/benchmark_results.csv.
"""

import csv
import statistics
import time
from pathlib import Path

import duckdb

from aws_setup import DATA_BUCKET, ROOT, athena_query, check_credentials

REPEATS = 3
ATHENA_USD_PER_TB = 5.0  # on-demand price, us-east-1
ATHENA_MIN_BYTES = 10 * 1024**2  # every query is billed at least 10 MB
S3 = f"s3://{DATA_BUCKET}/data"
OUT = Path(__file__).with_name("benchmark_results.csv")

# ---------------------------------------------------------------- raw tables (Athena)
RAW_DDL = [
    f"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_movies (movieid string, title string, genres string)
        ROW FORMAT SERDE 'org.apache.hadoop.hive.serde2.OpenCSVSerde'
        WITH SERDEPROPERTIES ('separatorChar' = ',', 'quoteChar' = '"')
        LOCATION '{S3}/ml-100k/movies/' TBLPROPERTIES ('skip.header.line.count' = '1')""",
    f"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_ratings
        (userid bigint, movieid bigint, rating double, `timestamp` bigint)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
        LOCATION '{S3}/ml-100k/ratings/' TBLPROPERTIES ('skip.header.line.count' = '1')""",
    f"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_links (movieid bigint, imdbid string, tmdbid bigint)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY ','
        LOCATION '{S3}/ml-100k/links/' TBLPROPERTIES ('skip.header.line.count' = '1')""",
    # IMDb TSV: no quoting, \N for NULL (LazySimpleSerDe's default null marker)
    rf"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_imdb_basics
        (tconst string, titletype string, primarytitle string, originaltitle string,
         isadult string, startyear string, endyear string, runtimeminutes string, genres string)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY '\t'
        LOCATION '{S3}/imdb/title.basics/' TBLPROPERTIES ('skip.header.line.count' = '1')""",
    rf"""CREATE EXTERNAL TABLE IF NOT EXISTS raw_imdb_ratings
        (tconst string, averagerating double, numvotes int)
        ROW FORMAT DELIMITED FIELDS TERMINATED BY '\t'
        LOCATION '{S3}/imdb/title.ratings/' TBLPROPERTIES ('skip.header.line.count' = '1')""",
]

# ---------------------------------------------------------------- table sources per engine/format
ML, IMDB, PQ = ROOT / "data/ml-100k", ROOT / "data/imdb", ROOT / "src/parquet"
TSV = "delim='\t', quote='', header=true, nullstr='\\N', all_varchar=true"
SOURCES = {
    ("duckdb", "raw"): {
        "ratings": f"read_csv('{ML / 'ratings.csv'}')",
        "imdb_basics": f"read_csv('{IMDB / 'title.basics.tsv'}', {TSV})",
        "imdb_ratings": f"(SELECT tconst, CAST(averageRating AS DOUBLE) AS averagerating, "
        f"CAST(numVotes AS INTEGER) AS numvotes FROM read_csv('{IMDB / 'title.ratings.tsv'}', {TSV}))",
        "movie_filter": "AND b.titletype = 'movie'",  # raw IMDb has every title type
    },
    ("duckdb", "parquet"): {
        "ratings": f"read_parquet('{PQ / 'ratings.parquet'}')",
        "imdb_basics": f"read_parquet('{PQ / 'imdb_basics.parquet'}')",
        "imdb_ratings": f"read_parquet('{PQ / 'imdb_ratings.parquet'}')",
        "movie_filter": "",  # already reduced to feature films when converted
    },
    ("athena", "raw"): {
        "ratings": "raw_ratings",
        "imdb_basics": "raw_imdb_basics",
        "imdb_ratings": "raw_imdb_ratings",
        "movie_filter": "AND b.titletype = 'movie'",
    },
    ("athena", "parquet"): {
        "ratings": "ratings",
        "imdb_basics": "imdb_basics",
        "imdb_ratings": "imdb_ratings",
        "movie_filter": "",
    },
}

# ---------------------------------------------------------------- queries (from the notebook)
QUERIES = {
    # Section 2: average rating per popularity bucket (MovieLens, small)
    "ml_rating_by_popularity": """
        WITH s AS (SELECT movieid, COUNT(*) AS n, AVG(rating) AS a FROM {ratings} GROUP BY movieid)
        SELECT CASE WHEN n = 1 THEN '1' WHEN n < 5 THEN '2-4' WHEN n < 10 THEN '5-9'
                    WHEN n < 20 THEN '10-19' WHEN n < 50 THEN '20-49' WHEN n < 100 THEN '50-99'
                    ELSE '100+' END AS bucket,
               COUNT(*) AS movies, ROUND(AVG(a), 2) AS mean_rating
        FROM s GROUP BY 1 ORDER BY MIN(n)""",
    # Section 3c: all IMDb feature films, rating per votes level (IMDb, large join)
    "imdb_rating_by_votes": """
        SELECT CASE WHEN r.numvotes < 1000 THEN '100-999' WHEN r.numvotes < 10000 THEN '1k-9.9k'
                    WHEN r.numvotes < 100000 THEN '10k-99k' WHEN r.numvotes < 1000000 THEN '100k-999k'
                    ELSE '1M+' END AS votes,
               COUNT(*) AS movies, ROUND(AVG(r.averagerating), 2) AS mean_rating
        FROM {imdb_basics} b JOIN {imdb_ratings} r ON r.tconst = b.tconst
        WHERE r.numvotes >= 100 {movie_filter}
        GROUP BY 1 ORDER BY MIN(r.numvotes)""",
    # Column pruning: one column of the 1 GB title.basics
    "imdb_one_column": """
        SELECT COUNT(DISTINCT b.genres) AS genre_combinations
        FROM {imdb_basics} b WHERE 1 = 1 {movie_filter}""",
}


def run_duckdb(sql):
    con = duckdb.connect()  # fresh in-memory connection: no cached tables
    t0 = time.perf_counter()
    rows = con.sql(sql).fetchall()
    ms = (time.perf_counter() - t0) * 1000
    con.close()
    return [tuple(str(v) for v in r) for r in rows], ms, None


def run_athena(sql):
    rows, stats = athena_query(sql)
    return [tuple(r) for r in rows], stats["EngineExecutionTimeInMillis"], stats["DataScannedInBytes"]


def normalise(rows):
    """Compare results across engines: numbers as floats rounded to 2 decimals."""
    out = []
    for r in rows:
        vals = []
        for v in r:
            try:
                vals.append(round(float(v), 2))
            except (TypeError, ValueError):
                vals.append(v)
        out.append(tuple(vals))
    return out


def main():
    check_credentials()
    print("\n=== Raw tables in Athena")
    for ddl in RAW_DDL:
        athena_query(ddl)
        print("  ", ddl.split("EXISTS")[1].split("(")[0].strip())

    results = []
    for qname, template in QUERIES.items():
        print(f"\n=== {qname}")
        reference = None
        for (engine, fmt), src in SOURCES.items():
            sql = template.format(**src)
            runner = run_duckdb if engine == "duckdb" else run_athena
            times, scanned, rows = [], None, None
            for _ in range(REPEATS):
                rows, ms, scanned = runner(sql)
                times.append(ms)
            rows = normalise(rows)
            reference = reference or rows
            same = rows == reference
            ms = statistics.median(times)
            cost = (
                max(scanned, ATHENA_MIN_BYTES) / 1e12 * ATHENA_USD_PER_TB if scanned is not None else None
            )
            results.append(
                dict(query=qname, engine=engine, format=fmt, median_ms=round(ms),
                     bytes_scanned=scanned, usd_per_query=cost, same_result=same)
            )
            scanned_txt = f"{scanned / 1e6:10,.2f} MB" if scanned is not None else f"{'-':>13}"
            cost_txt = f"${cost:.6f}" if cost is not None else "-"
            print(f"  {engine:<7}{fmt:<8}{ms:>9,.0f} ms {scanned_txt}  {cost_txt:<10} "
                  f"{'same result' if same else 'DIFFERENT RESULT'}")

    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=results[0].keys())
        w.writeheader()
        w.writerows(results)
    print(f"\nSaved {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
