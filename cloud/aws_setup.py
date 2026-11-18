"""Cloud layer: S3 + Glue Data Catalog + Athena.

Uploads the local files to S3, catalogs the Parquet tables with a Glue crawler and
checks in Athena that the row counts match the local DuckDB tables.

Run from the project root, after the notebook has generated src/parquet/:

    python cloud/aws_setup.py            # every step
    python cloud/aws_setup.py --skip-upload

Credentials: AWS Academy Learner Lab -> AWS Details -> AWS CLI -> copy the block into
~/.aws/credentials. They expire when the lab session ends.

Every step can be run again safely: existing buckets, files, the database and the
crawler are reused.
"""

import argparse
import shutil
import time
from pathlib import Path

import boto3
import duckdb
from botocore.exceptions import ClientError

# ---------------------------------------------------------------- configuration
REGION = "us-east-1"
DATA_BUCKET = "bdf-movies-data-g23"
RESULTS_BUCKET = "bdf-athena-results-g23"
DATABASE = "bdf_project"
CRAWLER = "bdf-parquet-crawler"
ROLE = "LabRole"  # the only role AWS Academy Learner Lab allows

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "s3_upload"  # local mirror of the bucket layout

# S3 layout: one folder per table (Athena reads every file under a table's LOCATION).
RAW_FILES = {  # local file -> S3 key, names as in the README's data/ layout
    "data/ml-100k/movies.csv": "data/ml-100k/movies/movies.csv",
    "data/ml-100k/ratings.csv": "data/ml-100k/ratings/ratings.csv",
    "data/ml-100k/links.csv": "data/ml-100k/links/links.csv",
    "data/imdb/title.basics.tsv": "data/imdb/title.basics/title.basics.tsv",
    "data/imdb/title.ratings.tsv": "data/imdb/title.ratings/title.ratings.tsv",
}
TABLES = ["movies", "ratings", "links", "imdb_basics", "imdb_ratings"]
PARQUET_FILES = {f"src/parquet/{t}.parquet": f"parquet/{t}/{t}.parquet" for t in TABLES}

session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
glue = session.client("glue")
athena = session.client("athena")


def step(title):
    print(f"\n=== {title}")


# ---------------------------------------------------------------- 0. credentials
def check_credentials():
    step("Credentials")
    try:
        ident = session.client("sts").get_caller_identity()
    except Exception as e:  # noqa: BLE001 - any failure here means no usable credentials
        raise SystemExit(
            f"No valid AWS credentials ({e}).\n"
            "Learner Lab -> Start Lab -> AWS Details -> AWS CLI: Show -> "
            "paste the block into ~/.aws/credentials"
        )
    print(f"OK, account {ident['Account']}, region {REGION}")


# ---------------------------------------------------------------- 1. S3
def ensure_bucket(name):
    try:
        s3.head_bucket(Bucket=name)
        print(f"bucket {name}: exists")
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise SystemExit(f"bucket {name}: {e} (owned by another account?)")
        s3.create_bucket(Bucket=name)  # us-east-1 takes no LocationConstraint
        print(f"bucket {name}: created")


def build_staging():
    """Mirror the bucket layout locally in s3_upload/ (APFS clones, no extra disk)."""
    for local, key in {**RAW_FILES, **PARQUET_FILES}.items():
        src, dst = ROOT / local, STAGING / key
        if not src.exists():
            raise SystemExit(f"missing {src} - download the data / run the notebook first")
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def upload():
    step("S3 upload")
    ensure_bucket(DATA_BUCKET)
    ensure_bucket(RESULTS_BUCKET)
    build_staging()
    for path in sorted(p for p in STAGING.rglob("*") if p.is_file() and p.name != ".DS_Store"):
        key = path.relative_to(STAGING).as_posix()
        size = path.stat().st_size
        try:
            if s3.head_object(Bucket=DATA_BUCKET, Key=key)["ContentLength"] == size:
                print(f"  skip  s3://{DATA_BUCKET}/{key} (already there)")
                continue
        except ClientError:
            pass
        t0 = time.time()
        s3.upload_file(str(path), DATA_BUCKET, key)  # multipart for large files
        print(f"  put   s3://{DATA_BUCKET}/{key}  {size / 1e6:,.1f} MB  {time.time() - t0:.1f}s")


# ---------------------------------------------------------------- 2. Glue
def run_crawler():
    step("Glue crawler")
    try:
        glue.create_database(DatabaseInput={"Name": DATABASE})
        print(f"database {DATABASE}: created")
    except glue.exceptions.AlreadyExistsException:
        print(f"database {DATABASE}: exists")

    config = dict(
        Role=ROLE,
        DatabaseName=DATABASE,
        Targets={"S3Targets": [{"Path": f"s3://{DATA_BUCKET}/parquet/{t}/"} for t in TABLES]},
    )
    try:
        glue.create_crawler(Name=CRAWLER, **config)
        print(f"crawler {CRAWLER}: created")
    except glue.exceptions.AlreadyExistsException:
        glue.update_crawler(Name=CRAWLER, **config)
        print(f"crawler {CRAWLER}: updated")

    try:
        glue.start_crawler(Name=CRAWLER)
    except glue.exceptions.CrawlerRunningException:
        pass
    print("running", end="", flush=True)
    while (c := glue.get_crawler(Name=CRAWLER)["Crawler"])["State"] != "READY":
        print(".", end="", flush=True)
        time.sleep(10)
    last = c.get("LastCrawl", {})
    print(f" {last.get('Status')} {last.get('ErrorMessage', '')}")

    for t in glue.get_tables(DatabaseName=DATABASE)["TableList"]:
        cols = ", ".join(f"{c['Name']} {c['Type']}" for c in t["StorageDescriptor"]["Columns"])
        print(f"  {t['Name']:<14} {cols}")


# ---------------------------------------------------------------- 3. Athena
def athena_query(sql):
    """Run one statement, wait, return (rows, stats)."""
    qid = athena.start_query_execution(
        QueryString=sql,
        QueryExecutionContext={"Database": DATABASE},
        ResultConfiguration={"OutputLocation": f"s3://{RESULTS_BUCKET}/"},
    )["QueryExecutionId"]
    while True:
        ex = athena.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
        state = ex["Status"]["State"]
        if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    if state != "SUCCEEDED":
        raise SystemExit(f"Athena {state}: {ex['Status'].get('StateChangeReason')}\n{sql}")
    rows = athena.get_query_results(QueryExecutionId=qid)["ResultSet"]["Rows"]
    values = [[c.get("VarCharValue") for c in r["Data"]] for r in rows[1:]]
    return values, ex["Statistics"]


def compare_counts():
    step("Athena vs local DuckDB: row counts")
    sql = " UNION ALL ".join(f"SELECT '{t}', COUNT(*) FROM {t}" for t in TABLES)
    rows, stats = athena_query(sql)
    cloud = {name: int(n) for name, n in rows}
    print(f"{'table':<14}{'athena':>12}{'duckdb':>12}")
    ok = True
    for t in TABLES:
        local = duckdb.sql(f"SELECT COUNT(*) FROM '{ROOT / 'src/parquet' / t}.parquet'").fetchall()[0][0]
        ok &= cloud.get(t) == local
        print(f"{t:<14}{cloud.get(t, 0):>12,}{local:>12,}  {'OK' if cloud.get(t) == local else 'DIFF'}")
    print(
        f"{'all match' if ok else 'MISMATCH'} - "
        f"{stats['EngineExecutionTimeInMillis']} ms, {stats['DataScannedInBytes']:,} bytes scanned"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-upload", action="store_true", help="files are already in S3")
    ap.add_argument("--skip-crawler", action="store_true", help="tables are already in the catalog")
    args = ap.parse_args()

    check_credentials()
    if not args.skip_upload:
        upload()
    if not args.skip_crawler:
        run_crawler()
    compare_counts()
