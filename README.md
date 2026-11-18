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
cloud/benchmark_results_*.csv     latest benchmark output, one file per dataset
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
   - **MovieLens 32M (optional, for the scale-up):** download `ml-32m.zip` from
     <https://grouplens.org/datasets/movielens/32m/> and unzip it into `data/` (folder
     `ml-32m`, 32,000,204 ratings; check the files against its `checksums.txt`).
   - **IMDb:** download `title.basics.tsv.gz` and `title.ratings.tsv.gz` from
     <https://datasets.imdbws.com/>, extract both and put the `.tsv` files in `data/imdb/`.
     IMDb updates these files daily, so row counts change slightly between downloads.
     The results below use the files downloaded on **2026-11-18**.

   You should end up with:

   ```
   data/
   ├── ml-100k/   movies.csv, ratings.csv, links.csv (+ tags.csv, README.txt)
   ├── ml-32m/    same files (optional)
   └── imdb/      title.basics.tsv, title.ratings.tsv
   ```

3. Run the notebook:

   ```bash
   cd src
   jupyter lab popularity_vs_quality.ipynb
   ```

   The first run converts the raw files to Parquet (`src/parquet/<dataset>/` for MovieLens,
   `src/parquet/imdb/` for IMDb) and builds `src/popularity_vs_quality_<dataset>.duckdb`.
   A Parquet file is rebuilt automatically when its raw file is newer.

   The MovieLens version is chosen with the `BDF_DATASET` environment variable
   (`ml-100k` by default). The notebook's text describes the `ml-100k` results. To run it on
   32M without changing the saved notebook:

   ```bash
   BDF_DATASET=ml-32m jupyter nbconvert --to notebook --execute popularity_vs_quality.ipynb \
       --output popularity_vs_quality_ml-32m.ipynb
   ```

   Run time: about 12 s for ml-100k and about 6 s for ml-32m once the Parquet files exist
   (32M ratings: `ratings.csv` 877 MB → `ratings.parquet` 219 MB).

## Cloud layer (S3 + Glue + Athena)

### Credentials

The project uses an AWS Academy Learner Lab account. Start the lab, open
**AWS Details → AWS CLI: Show** and paste the block into `~/.aws/credentials`.
The credentials expire when the lab session ends and have to be copied again.

### 1. Upload, catalog and check: `cloud/aws_setup.py`

```bash
python cloud/aws_setup.py                    # ml-100k, every step
python cloud/aws_setup.py --dataset ml-32m   # MovieLens 32M
python cloud/aws_setup.py --skip-upload      # files already in S3
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
   │   ├── ml-32m/                       same three folders
   │   └── imdb/
   │       ├── title.basics/title.basics.tsv
   │       └── title.ratings/title.ratings.tsv
   └── parquet/
       ├── ml-100k/
       │   ├── movies/movies.parquet
       │   ├── ratings/ratings.parquet
       │   └── links/links.parquet
       ├── ml-32m/                       same three folders
       └── imdb/
           ├── imdb_basics/imdb_basics.parquet
           └── imdb_ratings/imdb_ratings.parquet
   ```

   The same layout is mirrored locally in `s3_upload/` (git-ignored).

3. creates the Glue database and crawler (role `LabRole`) over the five Parquet folders
   and runs it: `bdf_project` / `bdf-parquet-crawler` for ml-100k,
   `bdf_project_32m` / `bdf-parquet-crawler-32m` for ml-32m. IMDb tables appear in both
   databases and point to the same `parquet/imdb/` files;
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
python cloud/benchmark.py                    # ml-100k
python cloud/benchmark.py --dataset ml-32m
```

Runs the queries from the notebook on raw CSV/TSV and on Parquet, locally (DuckDB) and
in Athena, three times each (median time). All 12 combinations return the same result.
Output: `cloud/benchmark_results_<dataset>.csv`. A fourth query (rating vs each
rater's own average, a join of `ratings` with itself) was added for the 32M run.

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

## Scale-up: MovieLens 32M

Same notebook, `BDF_DATASET=ml-32m` (87,585 movies, 200,948 users, 32,000,204 ratings).

Local DuckDB, median of 3 runs:

| Query                                    | CSV       | Parquet |
| ---------------------------------------- | --------- | ------- |
| Rating per popularity bucket             | 399 ms    | 63 ms   |
| Rating vs rater's own average (join)     | 866 ms    | 129 ms  |

Results compared with ml-100k:

| Check                                          | ml-100k        | ml-32m         |
| ---------------------------------------------- | -------------- | -------------- |
| Average rating, 2–4 ratings vs 100+ ratings    | 3.18 vs 3.82   | 2.92 vs 3.30   |
| Spearman, all / ≥ 10 / ≥ 50 / ≥ 100 ratings    | 0.04 / 0.25 / 0.31 / 0.32 | 0.16 / 0.25 / 0.22 / 0.19 |
| Spearman ≥ 50 ratings, after removing user bias | 0.28          | 0.07           |
| Gap 2–4 vs 100+ after removing user bias       | 0.44 of 0.64   | 0.18 of 0.38   |
| Spearman inside decades                        | 0.17 to 0.41   | 0.21 to 0.53   |
| Spearman inside genres                         | 0.19 to 0.61   | 0.15 to 0.76   |
| MovieLens popularity vs IMDb rating (≥ 10)     | 0.35           | 0.18           |
| Movies in both Top 20 lists                    | 7              | 4              |

The direction holds at 32M (popular movies are rated higher, inside every decade and genre
and on IMDb), but the effect is weaker, and after removing user bias it almost disappears
(0.07). The popularity buckets were designed for ml-100k: at 32M the 100+ bucket holds
14% of the movies and 98% of the ratings.

## Progress

- [x] Local analysis: notebook with DuckDB, Parquet, Polars and Plotly
- [x] Reproduced from a clean clone (all MovieLens results identical; IMDb differs
      slightly because the dump is newer)
- [x] S3 buckets and upload (raw + Parquet)
- [x] Glue database and crawler for the Parquet tables
- [x] Athena tables for the raw files (DDL)
- [x] Athena row counts match DuckDB
- [x] Benchmark: CSV vs Parquet, DuckDB vs Athena
- [x] Notebook configurable by dataset (`BDF_DATASET`), Parquet rebuilt when stale
- [x] MovieLens 32M: local run and comparison of results
- [ ] MovieLens 32M in S3 / Glue / Athena and Athena benchmark
- [ ] Remove the old flat Parquet folders from S3 (`parquet/movies/`, `parquet/ratings/`,
      `parquet/links/`, `parquet/imdb_basics/`, `parquet/imdb_ratings/`)
- [ ] Partition `ratings` (e.g. by year) and measure partition pruning
- [ ] Slides: architecture, evidence, performance, conclusions
