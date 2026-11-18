# Popularity vs Quality

Do movies with more ratings have higher ratings? Analysis of MovieLens + IMDb data, first
locally with DuckDB and Parquet, then in the cloud with Amazon S3, AWS Glue and Amazon Athena.

## Architecture

```
              LOCAL (laptop)                                CLOUD (AWS, us-east-1)
 ┌────────────────────────────────────────┐        ┌──────────────────────────────────────────────┐
 │ data/<dataset>/, data/imdb/  CSV / TSV │ upload │ s3://bdf-movies-data-g23/data/     CSV / TSV │
 │   │  DuckDB COPY (notebook)            │ ─────▶ │ s3://bdf-movies-data-g23/parquet/  Parquet   │
 │   ▼                                    │        │        │                                     │
 │ src/parquet/<dataset>/, imdb/  Parquet │        │        ▼                                     │
 │   │                                    │        │ Glue Data Catalog                            │
 │   ▼                                    │        │   bdf_project (100k), bdf_project_32m (32M)  │
 │ src/popularity_vs_quality_<dataset>    │        │   Parquet tables: Glue crawler               │
 │     .duckdb                            │        │   raw_* (CSV) tables: SQL                    │
 │   │                                    │        │        │                                     │
 │   ▼                                    │        │        ▼                                     │
 │ notebook: SQL + Polars + Plotly        │        │ Athena (SQL), results in                     │
 └────────────────────────────────────────┘        │ s3://bdf-athena-results-g23/                 │
                                                   └──────────────────────────────────────────────┘

<dataset> = ml-100k (default) or ml-32m
```

## Project structure

```
src/popularity_vs_quality.ipynb   analysis notebook (local layer)
cloud/aws_setup.py                S3 upload + Glue crawler + Athena row-count check
cloud/benchmark.py                the same questions in Athena on CSV vs Parquet
cloud/benchmark_results_*.csv     latest benchmark output, one file per dataset
cloud/partitioning.sql            ratings partitioned by year in Athena (CTAS) + before/after queries
requirements.txt                  pinned dependencies
```

Not in git (downloaded or generated): `data/`, `src/parquet/`, `*.duckdb`, `s3_upload/`, `.venv/`.

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

### Upload, catalog and check: `cloud/aws_setup.py`

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

   Before uploading, the script copies the files into this layout in `s3_upload/`
   (not in git; it can be deleted and is rebuilt on the next run).

3. creates the Glue database and crawler (role `LabRole`) over the five Parquet folders
   and runs it: `bdf_project` / `bdf-parquet-crawler` for ml-100k,
   `bdf_project_32m` / `bdf-parquet-crawler-32m` for ml-32m. IMDb tables appear in both
   databases and point to the same `parquet/imdb/` files;
4. counts the rows of each table in Athena and compares them with the local Parquet files.

Result: **all five tables match** the local DuckDB counts, for both datasets.

| Table          | ml-100k   | ml-32m     |
| -------------- | --------- | ---------- |
| movies         | 9,742     | 87,585     |
| ratings        | 100,836   | 32,000,204 |
| links          | 9,742     | 87,585     |
| imdb_basics    | 761,268   | 761,268    |
| imdb_ratings   | 1,733,543 | 1,733,543  |

The count query scanned **0 bytes**: Parquet stores row counts in the file footer,
so Athena answers `COUNT(*)` from metadata alone.

Raw CSV/TSV tables (`raw_*`) are created with SQL in `cloud/benchmark.py` instead of the
crawler, because the crawler would turn `links.imdbId` into a number and lose its leading
zeros (`0114709` → `114709`), which breaks the link to IMDb.

## Performance and scalability evidence

All measurements are made in **Athena**, as in the course labs: under every query Athena
shows the **run time** and the **data scanned**, which is also what it bills ($5 per TB,
at least 10 MB per query).

### What the benchmark does

`cloud/benchmark.py` repeats in Athena what you would otherwise do by hand in the Athena
console: run a query, then write down the run time and the data scanned shown under it.

It asks **two questions from the notebook**:

| Question                                         | Table it reads                                  |
| ------------------------------------------------ | ----------------------------------------------- |
| Average rating per popularity level (MovieLens)  | the ratings (100k or 32M)                       |
| IMDb rating per number of votes (IMDb)           | the IMDb titles (1.1 GB) and the IMDb ratings   |

Each question is asked on **the same data in two formats**:

- **CSV**: the original files in `s3://bdf-movies-data-g23/data/`
  (Athena tables `raw_ratings`, `raw_imdb_basics`, `raw_imdb_ratings`);
- **Parquet**: the converted files in `s3://bdf-movies-data-g23/parquet/`
  (Athena tables `ratings`, `imdb_basics`, `imdb_ratings`).

Step by step, the script:

1. checks the AWS credentials;
2. tells Athena where the CSV files are (the Parquet tables were already made by the
   Glue crawler);
3. runs each question **3 times on CSV and 3 times on Parquet**, and keeps the middle run
   time, so that one slow run does not change the result;
4. reads, for each question, the **run time** and the **data scanned** that Athena reports,
   and works out the **cost** ($5 per TB scanned, at least 10 MB per query);
5. checks that CSV and Parquet give **exactly the same answer**;
6. saves everything in `cloud/benchmark_results_<dataset>.csv`.

How to run it (from the project folder, with the AWS credentials saved; about 1 minute
per dataset, cost under $0.05):

```bash
source .venv/bin/activate
python cloud/benchmark.py                    # MovieLens 100k
python cloud/benchmark.py --dataset ml-32m   # MovieLens 32M
```

The same numbers can be checked by hand: run the query in the Athena console on the
`raw_*` table and on the Parquet table, and compare the run time and data scanned shown
under each result.

### The result files

`cloud/benchmark_results_ml-100k.csv` and `cloud/benchmark_results_ml-32m.csv` (open in
Excel) have one row per question and format, 4 rows each:

| Column        | Meaning                                                              |
| ------------- | -------------------------------------------------------------------- |
| `dataset`     | `ml-100k` or `ml-32m`                                                |
| `question`    | which of the two questions                                           |
| `format`      | `CSV` or `Parquet`                                                   |
| `time_ms`     | run time in Athena, in milliseconds (middle of 3 runs)               |
| `mb_scanned`  | data Athena read, in MB: what it bills                               |
| `cost_usd`    | cost of one run, in dollars                                          |
| `same_answer` | `True` when CSV and Parquet gave the same answer                     |

The latest results (all answers identical in both formats):

| Dataset | Question                            | Format  | Run time | Data scanned | Cost per run |
| ------- | ----------------------------------- | ------- | -------- | ------------ | ------------ |
| 100k    | Average rating per popularity level | CSV     | 0.62 s   | 2.48 MB      | $0.00005     |
| 100k    | Average rating per popularity level | Parquet | 0.57 s   | 0.27 MB      | $0.00005     |
| 100k    | IMDb rating per number of votes     | CSV     | 1.85 s   | 1,158 MB     | $0.00579     |
| 100k    | IMDb rating per number of votes     | Parquet | 1.95 s   | 17 MB        | $0.00009     |
| 32M     | Average rating per popularity level | CSV     | 1.33 s   | 877 MB       | $0.00439     |
| 32M     | Average rating per popularity level | Parquet | 1.36 s   | 86 MB        | $0.00043     |
| 32M     | IMDb rating per number of votes     | CSV     | 1.60 s   | 1,158 MB     | $0.00579     |
| 32M     | IMDb rating per number of votes     | Parquet | 2.02 s   | 17 MB        | $0.00009     |

How to read them:

- **Data scanned is the big difference.** Parquet reads 10× less for the MovieLens 32M
  question (86 MB instead of 877 MB) and 67× less for the IMDb question (17 MB instead of
  1,158 MB), so it costs 10× and 67× less.
- **Run time is about the same** in both formats (differences of a few tenths of a
  second). At these sizes the time is mostly Athena starting the query.
- **The IMDb question is the same in both files** because the IMDb data does not change
  with the MovieLens dataset.
- **The 100k MovieLens question costs the same in both formats**: both read less than
  10 MB, the minimum Athena bills.

The sections below summarise what these results show.

### 1. Parquet files are much smaller

| File                    | CSV     | Parquet             | Smaller by |
| ----------------------- | ------- | ------------------- | ---------- |
| MovieLens 32M ratings   | 877 MB  | 219 MB              | 4×         |
| IMDb list of titles     | 1.1 GB  | 18 MB (films only)  | 60×        |

Uploading the 32M ratings to S3 took 27 s as CSV and 6 s as Parquet.

### 2. Parquet scans less data, so it costs less

Parquet stores each column separately, so Athena reads only the columns a question needs;
a CSV file has to be read in full.

| Question (MovieLens 32M)               | Data scanned: CSV | Parquet | Cost per 1,000 runs: CSV | Parquet |
| -------------------------------------- | ----------------- | ------- | ------------------------ | ------- |
| Average rating per popularity level    | 877 MB            | 86 MB   | $4.39                    | $0.43   |
| IMDb rating per number of votes        | 1,158 MB          | 17 MB   | $5.79                    | $0.09   |

Counting rows scans **0 MB**: a Parquet file stores its row count, so Athena does not need
to open the data at all.

### 3. 317 times more data, only 2.4 times more time

Going from MovieLens 100k to 32M (317× more ratings), question "average rating per
popularity level", on Parquet:

|                   | 100k    | 32M     | Growth |
| ----------------- | ------- | ------- | ------ |
| Data scanned      | 0.27 MB | 86 MB   | 320×   |
| Run time          | 0.57 s  | 1.36 s  | 2.4×   |

Athena splits the work among many machines, so its run time grows much more slowly than
the data. The cost grows with the data scanned, which is why the file format matters.

### 4. Partitioning by year

The ratings are an event log: every rating has a time, and questions are often about a
period ("ratings in 2015", "the latest years"). Following the course (W6, W7), the
MovieLens 32M ratings were rewritten as a table **partitioned by year**: one S3 folder per
year, so Athena can skip the folders a question does not need.

**Why year, and not another column** (number of distinct values in MovieLens 32M):

| Column   | Distinct values | Partition by it? |
| -------- | --------------- | ---------------- |
| year     | 29              | **Yes**: filtered often, few values, large folders |
| rating   | 10              | No: questions filter by time, not by rating |
| movieId  | 84,432          | No: 84k folders of tiny files |
| userId   | 200,948         | No: far too many values |

This follows the guidelines of Costa, Costa and Santos (2019), *Evaluating partitioning
and bucketing strategies for Hive-based Big Data Warehousing systems*, Journal of Big
Data 6:34: partition by the attributes used in filters, prefer time attributes, check
the number of distinct values first, and avoid many small files.

**How it was built**: one Athena **CTAS** statement (`CREATE TABLE ... AS SELECT`) in the
Athena console, database `bdf_project_32m`. It reads the Parquet ratings, adds a `year`
column and writes `parquet/ml-32m/ratings_by_year/year=1995/ ... year=2023/` to S3, and
registers the table and its 29 partitions in the Glue Data Catalog (no crawler needed).
CTAS was chosen over a Glue job because it is one SQL statement, starts in seconds and
only bills the data it reads; 29 partitions are within Athena's limit of 100. All
statements are in `cloud/partitioning.sql`.

**Results** (Athena console, one run each; queries 3a–3c all return 1,743,866 ratings,
average 3.559):

| Query                                                        | Data scanned | Run time |
| ------------------------------------------------------------ | ------------ | -------- |
| Build the partitioned table (CTAS, once)                     | 204 MB       | 10.9 s   |
| One year (2015), table without partitions                    | 135 MB       | 1.44 s   |
| **One year (2015), partitioned table, `WHERE year = 2015`**  | **0.85 MB**  | **0.87 s** |
| One year (2015), partitioned table, filter on the timestamp  | 123 MB       | 1.34 s   |
| All years (notebook question), table without partitions     | 82 MB        | 1.70 s   |
| All years (notebook question), partitioned table             | 69 MB        | 1.44 s   |

What this shows:

- **A question about one year reads 160× less data** (135 MB → 0.85 MB) and takes 40% less
  time: Athena opens only the `year=2015/` folder.
- **The filter has to be written on the partition column.** `WHERE year = 2015` skips the
  other folders; `WHERE year(from_unixtime(timestamp)) = 2015` means the same but reads
  every folder (123 MB), because Athena decides which folders to skip from their names
  before reading any data.
- **Questions over all years gain nothing from the partitions**: no folder can be skipped.
  The notebook's main question still read 16% less (82 → 69 MB), because the files Athena
  wrote compress better, not because of the partitions.
- **The table pays for itself quickly**: building it read 204 MB once; every one-year
  question saves about 134 MB, so it is paid back after 2 such questions.

### 5. Limits

- **Parquet is not faster than CSV in run time** at this size (1.36 s vs 1.33 s and
  2.0 s vs 1.6 s): its advantage is the data scanned and the cost. A possible cause is that
  the Parquet file is one large file split into very small blocks, which is harder for
  Athena to divide.
- **Partitions only help questions that filter by year.** The notebook's questions cover
  all years, so for them the partitioned table reads about the same data.
- **Every query takes at least about half a second**, however small: Athena needs that
  time to start.
- **Small questions cost the same in both formats**: Athena bills at least 10 MB per query.
- **What the cloud adds**: one shared copy of the data for the whole group, nothing to
  download, paying only for what is queried, and room to grow to much larger datasets
  (for example MovieLens 1B).

## Progress

Done:

- [x] Local layer: notebook with DuckDB, Parquet, Polars and Plotly, reproducible from a
      clean clone
- [x] Notebook works with MovieLens 100k and 32M (`BDF_DATASET`)
- [x] Cloud layer: data in S3, tables in the Glue Data Catalog, queries in Athena, for
      both datasets; row counts match the laptop
- [x] Performance and scalability evidence in Athena: CSV vs Parquet, 100k vs 32M
- [x] Ratings partitioned by year (Athena CTAS), measured before and after

To do:

- [ ] Slides: questions, architecture, evidence, performance/scalability, conclusions
- [ ] Delete the old Parquet folders in S3, no longer used: `parquet/movies/`,
      `parquet/ratings/`, `parquet/links/`, `parquet/imdb_basics/`, `parquet/imdb_ratings/`
- [ ] Future work (optional): sort the ratings by movieId inside each year (the course's
      "partitioned + sorted" layout), so questions about one movie can skip data too
