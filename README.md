# Popularity vs Quality

Do movies with more ratings have higher ratings? Analysis of MovieLens + IMDb data, first
locally with DuckDB and Parquet, then in the cloud with Amazon S3, AWS Glue and Amazon Athena.

## Architecture

```
                LOCAL                                          CLOUD (AWS, us-east-1)
 ┌──────────────────────────────────────┐        ┌──────────────────────────────────────────────┐
 │ data/  raw CSV / TSV                 │ upload │ s3://bdf-movies-data-g23/data/     raw       │
 │   │  DuckDB COPY (notebook)          │ ─────▶ │ s3://bdf-movies-data-g23/parquet/  Parquet   │
 │   ▼                                  │        │        │                                     │
 │ src/parquet/  Parquet                │        │        ▼                                     │
 │   │                                  │        │ Glue Data Catalog, database bdf_project      │
 │   ▼                                  │        │   Parquet tables: Glue crawler               │
 │ src/popularity_vs_quality.duckdb     │        │   raw_* tables:   Athena DDL                 │
 │   │                                  │        │        │                                     │
 │   ▼                                  │        │        ▼                                     │
 │ notebook: SQL + Polars + Plotly      │        │ Athena (SQL), results in                     │
 └──────────────────────────────────────┘        │ s3://bdf-athena-results-g23/                 │
                                                 └──────────────────────────────────────────────┘
```

## Project structure

```
src/popularity_vs_quality.ipynb   analysis notebook (local layer)
cloud/aws_setup.py                S3 upload + Glue crawler + Athena row-count check
cloud/benchmark.py                same queries on CSV vs Parquet, DuckDB vs Athena
cloud/benchmark_results.csv       latest benchmark output
requirements.txt                  pinned dependencies
```

Generated and git-ignored: `data/`, `src/parquet/`, `*.duckdb`, `s3_upload/`.

## Setup

1. **Python 3.11 or newer** is required (`duckdb==2.0.0` does not install on older versions;
   macOS ships Python 3.9). Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv            # or: uv venv --python 3.13 .venv
   source .venv/bin/activate
   pip install -r requirements.txt  # or: uv pip install -r requirements.txt
   ```

   On Windows, use `python` instead of `python3` and activate with `.venv\Scripts\activate`.

2. Download the data. The `data/` folder is git-ignored, so it is not in the repo and
   has to be set up by hand once:

   - **MovieLens:** download `ml-latest-small.zip` from
     <https://grouplens.org/datasets/movielens/latest/>, unzip it, rename the extracted
     `ml-latest-small` folder to `ml-100k` and put it inside `data/`.
     (This is the "latest-small" dataset: 100,836 ratings, 610 users, 9,742 movies.)
   - **IMDb:** download `title.basics.tsv.gz` and `title.ratings.tsv.gz` from
     <https://datasets.imdbws.com/>, extract both and put the `.tsv` files in `data/imdb/`.
     IMDb updates these files daily, so row counts change slightly between downloads.
     The results below use the files downloaded on **2026-11-18**.

   You should end up with:

   ```
   data/
   ├── ml-100k/   movies.csv, ratings.csv, links.csv (+ tags.csv, README.txt)
   └── imdb/      title.basics.tsv, title.ratings.tsv
   ```

3. Run the notebook:

   ```bash
   cd src
   jupyter lab popularity_vs_quality.ipynb
   ```

   The first run converts the raw files to Parquet (`src/parquet/`) and builds
   `src/popularity_vs_quality.duckdb`. A full run takes about 12 seconds.

## Cloud layer (S3 + Glue + Athena)

### Credentials

The project uses an AWS Academy Learner Lab account. Start the lab, open
**AWS Details → AWS CLI: Show** and paste the block into `~/.aws/credentials`.
The credentials expire when the lab session ends and have to be copied again.

### 1. Upload, catalog and check: `cloud/aws_setup.py`

```bash
python cloud/aws_setup.py                # every step
python cloud/aws_setup.py --skip-upload  # files already in S3
```

The script can be re-run safely; it reuses anything that already exists. It:

1. creates the buckets `bdf-movies-data-g23` (data) and `bdf-athena-results-g23`
   (Athena query results);
2. uploads the raw files and the Parquet files, one folder per table (Athena reads every
   file under a table's location, so tables cannot share a folder). File names are kept
   as in `data/` and `src/parquet/`:

   ```
   s3://bdf-movies-data-g23/
   ├── data/
   │   ├── ml-100k/
   │   │   ├── movies/movies.csv
   │   │   ├── ratings/ratings.csv
   │   │   └── links/links.csv
   │   └── imdb/
   │       ├── title.basics/title.basics.tsv
   │       └── title.ratings/title.ratings.tsv
   └── parquet/
       ├── movies/movies.parquet
       ├── ratings/ratings.parquet
       ├── links/links.parquet
       ├── imdb_basics/imdb_basics.parquet
       └── imdb_ratings/imdb_ratings.parquet
   ```

3. creates the Glue database `bdf_project` and the crawler `bdf-parquet-crawler`
   (role `LabRole`) over the five Parquet folders, and runs it;
4. counts the rows of each table in Athena and compares them with the local Parquet files.

Result: **all five tables match** the local DuckDB counts.

| Table          | Athena    | DuckDB    |
| -------------- | --------- | --------- |
| movies         | 9,742     | 9,742     |
| ratings        | 100,836   | 100,836   |
| links          | 9,742     | 9,742     |
| imdb_basics    | 761,268   | 761,268   |
| imdb_ratings   | 1,733,543 | 1,733,543 |

The count query scanned **0 bytes**: Parquet stores row counts in the file footer,
so Athena answers `COUNT(*)` from metadata alone.

### 2. Athena tables

| Table                                                  | Over                     | Created by                     |
| ------------------------------------------------------ | ------------------------ | ------------------------------ |
| `movies`, `ratings`, `links`, `imdb_basics`, `imdb_ratings` | `parquet/`               | Glue crawler                   |
| `raw_movies`, `raw_ratings`, `raw_links`, `raw_imdb_basics`, `raw_imdb_ratings` | `data/` (CSV / TSV) | `CREATE EXTERNAL TABLE` in `cloud/benchmark.py` |

The raw tables are defined with DDL rather than the crawler: a crawler would read
`links.imdbId` as a number and drop its leading zeros (`0114709` → `114709`), which breaks
the join with IMDb (`'tt' || imdbId = tconst`). The IMDb TSVs use `\N` for NULL and no
quoting. Note that `timestamp` is a reserved word in Athena and must be written
`"timestamp"` in queries.

### 3. Benchmark: `cloud/benchmark.py`

```bash
python cloud/benchmark.py
```

Runs three queries from the notebook on raw CSV/TSV and on Parquet, locally (DuckDB) and
in Athena, three times each (median time). All 12 combinations return the same result.
Output: `cloud/benchmark_results.csv`.

| Query                                     | Engine | CSV / TSV                     | Parquet                     |
| ----------------------------------------- | ------ | ----------------------------- | --------------------------- |
| Rating per popularity bucket (MovieLens)  | DuckDB | 34 ms                         | 4 ms                        |
|                                           | Athena | 840 ms · 2.48 MB scanned      | 547 ms · 0.27 MB scanned    |
| Rating per IMDb votes level (IMDb join)   | DuckDB | 346 ms                        | 22 ms                       |
|                                           | Athena | 1,576 ms · 1,158 MB · $0.0058 | 2,195 ms · 17 MB · $0.00009 |
| One column of `title.basics`              | DuckDB | 287 ms                        | 3 ms                        |
|                                           | Athena | 1,178 ms · 1,128 MB · $0.0056 | 580 ms · 0.94 MB · $0.00005 |

Findings:

- **Parquet cuts the data Athena scans by 9× to ~1,200×**: it is columnar, so only the
  columns a query uses are read, and the IMDb Parquet files hold feature films only.
- **Cost follows bytes scanned** ($5 per TB, minimum 10 MB per query): the IMDb join is
  67× cheaper on Parquet. For the small MovieLens query both formats hit the 10 MB minimum.
- **At this size, local DuckDB is faster than Athena** (3–22 ms vs 0.5–2 s). Athena has a
  fixed overhead of about 0.5 s per query, and 30 MB of Parquet fits easily on a laptop.
  The cloud layer pays off with larger data and shared access, which is what the
  MovieLens 32M step will test.

## Progress

- [x] Local analysis: notebook with DuckDB, Parquet, Polars and Plotly
- [x] Reproduced from a clean clone (all MovieLens results identical; IMDb differs
      slightly because the dump is newer)
- [x] S3 buckets and upload (raw + Parquet)
- [x] Glue database and crawler for the Parquet tables
- [x] Athena tables for the raw files (DDL)
- [x] Athena row counts match DuckDB
- [x] Benchmark: CSV vs Parquet, DuckDB vs Athena
- [ ] Scale up to MovieLens 32M and repeat the benchmark
- [ ] Partition `ratings` (e.g. by year) and measure partition pruning
- [ ] Slides: architecture, evidence, performance, conclusions
