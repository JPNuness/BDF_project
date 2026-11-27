"""Cloud layer: S3 + Glue Data Catalog + Athena.

Uploads the local files to S3, catalogs the Parquet tables with a Glue crawler and
checks in Athena that the row counts match the local Parquet files.

Run from the project root, after the notebook has generated src/parquet/ for the dataset:

    python cloud/aws_setup.py                      # ml-100k (ml-latest-small)
    python cloud/aws_setup.py --dataset ml-32m
    python cloud/aws_setup.py --skip-upload        # files already in S3

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
ROLE = "LabRole"  # the only role AWS Academy Learner Lab allows
DATASETS = ["ml-100k", "ml-32m"]

ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / "s3_upload"  # local mirror of the bucket layout
ML_TABLES = ["movies", "ratings", "links"]
IMDB_TABLES = ["imdb_basics", "imdb_ratings"]
TABLES = ML_TABLES + IMDB_TABLES

# Set by set_dataset(); ml-100k keeps the original names (database bdf_project).
DATASET = DATABASE = CRAWLER = None


def set_dataset(dataset):
    global DATASET, DATABASE, CRAWLER
    suffix = "" if dataset == "ml-100k" else "_" + dataset.removeprefix("ml-")
    DATASET = dataset
    DATABASE = f"bdf_project{suffix}"  # bdf_project, bdf_project_32m
    CRAWLER = f"bdf-parquet-crawler{suffix.replace('_', '-')}"  # bdf-parquet-crawler(-32m)


set_dataset("ml-100k")


def parquet_dir(table):
    """S3 folder (and src/parquet subfolder) of a table: IMDb is shared by every dataset."""
    return "imdb" if table in IMDB_TABLES else DATASET


def files():
    """local file -> S3 key. One folder per table: Athena reads every file under a
    table's LOCATION. File names stay as in data/ and src/parquet/."""
    raw = {f"data/{DATASET}/{t}.csv": f"data/{DATASET}/{t}/{t}.csv" for t in ML_TABLES}
    raw |= {f"data/imdb/{n}.tsv": f"data/imdb/{n}/{n}.tsv" for n in ("title.basics", "title.ratings")}
    pq = {
        f"src/parquet/{parquet_dir(t)}/{t}.parquet": f"parquet/{parquet_dir(t)}/{t}/{t}.parquet"
        for t in TABLES
    }
    return raw | pq


session = boto3.Session(region_name=REGION)
s3 = session.client("s3")
glue = session.client("glue")
athena = session.client("athena")


def step(title):
    print(f"\n=== {title}")


# ---------------------------------------------------------------- 0. credentials
def check_credentials():
    step(f"Credentials ({DATASET} -> database {DATABASE})")
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
    for local, key in files().items():
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
    for key in sorted(files().values()):
        path = STAGING / key
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

    # Drop the crawler's tables first, so a table whose S3 folder moved is recreated
    # under the same name instead of the crawler adding a second, suffixed one.
    for t in TABLES:
        try:
            glue.delete_table(DatabaseName=DATABASE, Name=t)
        except glue.exceptions.EntityNotFoundException:
            pass

    config = dict(
        Role=ROLE,
        DatabaseName=DATABASE,
        Targets={
            "S3Targets": [{"Path": f"s3://{DATA_BUCKET}/parquet/{parquet_dir(t)}/{t}/"} for t in TABLES]
        },
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
        print(f"  {t['Name']:<16} {cols}")


# ---------------------------------------------------------------- 3. Athena
def athena_query(sql):
    """Run one statement in DATABASE, wait, return (rows, stats)."""
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
    step("Athena vs local Parquet: row counts")
    sql = " UNION ALL ".join(f"SELECT '{t}', COUNT(*) FROM {t}" for t in TABLES)
    rows, stats = athena_query(sql)
    cloud = {name: int(n) for name, n in rows}
    print(f"{'table':<14}{'athena':>14}{'duckdb':>14}")
    ok = True
    for t in TABLES:
        local_file = ROOT / f"src/parquet/{parquet_dir(t)}/{t}.parquet"
        local = duckdb.sql(f"SELECT COUNT(*) FROM '{local_file}'").fetchall()[0][0]
        ok &= cloud.get(t) == local
        print(f"{t:<14}{cloud.get(t, 0):>14,}{local:>14,}  {'OK' if cloud.get(t) == local else 'DIFF'}")
    print(
        f"{'all match' if ok else 'MISMATCH'} - "
        f"{stats['EngineExecutionTimeInMillis']} ms, {stats['DataScannedInBytes']:,} bytes scanned"
    )


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", choices=DATASETS, default="ml-100k")
    ap.add_argument("--skip-upload", action="store_true", help="files are already in S3")
    ap.add_argument("--skip-crawler", action="store_true", help="tables are already in the catalog")
    args = ap.parse_args()

    set_dataset(args.dataset)
    check_credentials()
    if not args.skip_upload:
        upload()
    if not args.skip_crawler:
        run_crawler()
    compare_counts()
