# Popularity vs Quality

Do movies with more ratings have higher ratings? Analysis of MovieLens + IMDb data, first
locally with DuckDB and Parquet, then in the cloud with Amazon S3, AWS Glue and Amazon Athena.

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

This next step moves the same data to AWS, so the whole group can 
query one shared copy, nothing has to be downloaded, and only 
what is queried is paid for. Three services work together:

- **Amazon S3** stores the files: the original CSV/TSV files and the Parquet files.
- **AWS Glue** describes them: a crawler reads the Parquet folders and creates the tables
  in the Glue Data Catalog.
- **Amazon Athena** answers SQL questions directly on the files in S3.

### Credentials

The project uses an AWS Academy Learner Lab account. Start the lab, open
**AWS Details → AWS CLI: Show** and paste the block into `~/.aws/credentials`.
The credentials expire when the lab session ends and have to be copied again.

### Upload, catalog and check: `cloud/aws_setup.py`

```bash
python cloud/aws_setup.py                    # ml-100k
python cloud/aws_setup.py --dataset ml-32m   # MovieLens 32M
python cloud/aws_setup.py --skip-upload      # files already in S3
```

The script can be run again safely: it reuses whatever already exists. It:

1. creates the buckets `bdf-movies-data-g23` (data) and `bdf-athena-results-g23`
   (Athena query results);
2. uploads the raw files and the Parquet files, one folder per table, keeping the file
   names of `data/` and `src/parquet/` (Athena reads every file in a table's folder, so
   tables cannot share one):

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
       ├── ml-32m/                       same three folders, plus ratings_by_year/
       └── imdb/
           ├── imdb_basics/imdb_basics.parquet
           └── imdb_ratings/imdb_ratings.parquet
   ```

3. creates the Glue database and crawler (role `LabRole`) over the Parquet folders and runs
   it: database `bdf_project` for ml-100k and `bdf_project_32m` for ml-32m;
4. counts the rows of every table in Athena and compares them with the local Parquet files.

**One design choice:** the tables over the CSV files (`raw_ratings`, `raw_imdb_basics`,
`raw_imdb_ratings`) are created with SQL instead of the crawler. The crawler would read the
IMDb id in `links.csv` as a number and drop its leading zeros (`0114709` → `114709`), which
breaks the link between MovieLens and IMDb.

## Performance and scalability evidence

With the same data in the cloud, the question becomes: **what does each choice cost?**
Athena bills the **data it scans** ($5 per TB, at least 10 MB per query) and shows it,
with the **run time**, under every query. All measurements below are those two numbers,
taken in Athena.

### 1. Does Parquet pay off? CSV vs Parquet

`cloud/benchmark.py` asks two questions from the notebook, once on the CSV files and once
on the Parquet files:

- **Average rating per popularity level** (reads the MovieLens ratings);
- **IMDb rating per number of votes** (reads the 1.1 GB IMDb title list and the IMDb ratings).

Each question runs 3 times per format (the middle run time is kept), and the script checks
that both formats give the same answer. It saves the run time, data scanned and cost of
each run in `cloud/benchmark_results_ml-100k.csv` and `cloud/benchmark_results_ml-32m.csv`.

```bash
python cloud/benchmark.py                    # ml-100k
python cloud/benchmark.py --dataset ml-32m   # MovieLens 32M
```

About a minute per dataset, less than $0.05 in total. The same numbers can be checked by
hand in the Athena console.

**Parquet files are smaller**, because each column is stored and compressed separately:

| File                    | CSV     | Parquet                          |
| ----------------------- | ------- | -------------------------------- |
| MovieLens 32M ratings   | 877 MB  | 219 MB (4× smaller)              |
| IMDb list of titles     | 1.1 GB  | 18 MB, films only (60× smaller)  |

**And Athena reads only the columns a question needs:**

| Dataset | Question                            | Format  | Run time | Data scanned | Cost per 1,000 runs |
| ------- | ----------------------------------- | ------- | -------- | ------------ | ------------------- |
| 32M     | Average rating per popularity level | CSV     | 1.33 s   | 877 MB       | $4.39               |
| 32M     | Average rating per popularity level | Parquet | 1.36 s   | **86 MB**    | **$0.43**           |
| 32M     | IMDb rating per number of votes     | CSV     | 1.60 s   | 1,158 MB     | $5.79               |
| 32M     | IMDb rating per number of votes     | Parquet | 2.02 s   | **17 MB**    | **$0.09**           |
| 100k    | Average rating per popularity level | CSV     | 0.62 s   | 2.48 MB      | $0.05               |
| 100k    | Average rating per popularity level | Parquet | 0.57 s   | 0.27 MB      | $0.05               |

- **Parquet scans 10× less** for the MovieLens question and **67× less** for the IMDb
  question, so it costs 10× and 67× less. Both formats always gave the same answer.
- **The run time is about the same.** At these sizes most of the time is Athena starting
  the query; Parquet's advantage is the data scanned and the cost.
- **On the 100k sample both formats cost the same**: they read less than the 10 MB
  minimum that Athena bills.

### 2. Does it scale? 100k vs 32M

The same question (average rating per popularity level, on Parquet), on 317 times more
ratings:

|              | 100k    | 32M    | Growth |
| ------------ | ------- | ------ | ------ |
| Data scanned | 0.27 MB | 86 MB  | 320×   |
| Run time     | 0.57 s  | 1.36 s | 2.4×   |

Athena splits the work among many machines, so the run time grows far more slowly than the
data. What grows with the data is the cost, which is why the way the files are organised
matters. That leads to the last step.

### 3. Can it read less? Partitioning by year

Ratings are an event log: every rating has a date, and many questions are about a period
("ratings in 2015", "the latest years"). If the ratings are stored in **one folder per
year**, Athena can skip the folders a question does not need.

**Why year, and not another column** (number of distinct values in MovieLens 32M):

| Column   | Distinct values | Partition by it?                                     |
| -------- | --------------- | ---------------------------------------------------- |
| year     | 29              | **Yes**: often filtered, few values, large folders   |
| rating   | 10              | No: questions filter by time, not by rating          |
| movieId  | 84,432          | No: 84,000 folders of tiny files                     |
| userId   | 200,948         | No: far too many values                              |

This follows Costa, Costa and Santos (2019), *Evaluating partitioning and bucketing
strategies for Hive-based Big Data Warehousing systems*, Journal of Big Data 6:34: partition
by the columns used in filters, prefer time columns, check how many distinct values a
column has, and avoid many small files.

**How it was built:** one Athena `CREATE TABLE ... AS SELECT` (CTAS) statement, in database
`bdf_project_32m`, reads the Parquet ratings, adds a `year` column and writes
`parquet/ml-32m/ratings_by_year/year=1995/` … `year=2023/` to S3. It also registers the
table and its 29 partitions in the Glue Data Catalog. CTAS was preferred to a Glue job
because it is a single SQL statement, starts in seconds and bills only the data it reads.
All statements are in `cloud/partitioning.sql`, to run one by one in the Athena console.

**Before and after** (the three 2015 queries all return 1,743,866 ratings, average 3.559):

| Query                                                       | Data scanned | Run time   |
| ----------------------------------------------------------- | ------------ | ---------- |
| Build the partitioned table (once)                          | 204 MB       | 10.9 s     |
| Ratings of 2015, table without partitions                   | 135 MB       | 1.44 s     |
| **Ratings of 2015, partitioned table, `WHERE year = 2015`** | **0.85 MB**  | **0.87 s** |
| Ratings of 2015, partitioned table, filter on the date      | 123 MB       | 1.34 s     |
| All years (the notebook's question), without partitions     | 82 MB        | 1.70 s     |
| All years (the notebook's question), partitioned table      | 69 MB        | 1.44 s     |

- **A question about one year reads 160× less** and takes 40% less time: Athena opens
  only the `year=2015/` folder.
- **The filter must be written on the partition column.** `WHERE year = 2015` skips the
  other folders. Filtering on the date (`year(from_unixtime(timestamp)) = 2015`) means the
  same, but Athena chooses folders by their names before reading any data, so it reads
  them all.
- **Questions over every year gain nothing from partitions**, because no folder can be
  skipped. The notebook's question read 16% less only because the new files are better
  compressed.
- **The table pays for itself after two one-year questions**: building it read 204 MB
  once, and each such question saves about 134 MB.

### 4. Limits

- **32M ratings (about 1 GB) still fit on one computer**: the notebook runs on the full
  dataset in seconds on a laptop. At this size the cloud's value is one shared copy and
  paying per query; its speed would matter on much larger data (for example MovieLens 1B).
- **Every Athena query takes at least about half a second**, however small, and is billed
  at least 10 MB.
- **Parquet was not faster than CSV in run time**, only cheaper. A likely reason is that
  each Parquet file is one large file split into very small blocks, which is harder for
  Athena to divide among its machines.
- **Partitions only help questions that filter by year**; the notebook's questions cover
  every year.
- **Next step:** sort the ratings by movie inside each year (the "partitioned + sorted"
  layout), so questions about one movie can skip data too.

## Progress

Done:

- [x] Local layer: notebook with DuckDB, Parquet, Polars and Plotly
- [x] Notebook works with MovieLens 100k and 32M (`BDF_DATASET`)
- [x] Cloud layer: data in S3, tables in the Glue Data Catalog, queries in Athena, for
      both datasets; row counts match the laptop
- [x] Performance and scalability evidence in Athena: CSV vs Parquet, 100k vs 32M
- [x] Ratings partitioned by year (Athena CTAS), measured before and after

To do:

- [ ] Slides: questions, architecture, evidence, performance/scalability, conclusions
