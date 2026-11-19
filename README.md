# Popularity vs Quality

**Do movies with more ratings have higher ratings?**

We answered this question with MovieLens and IMDb data, first on a laptop with DuckDB and
Parquet, then in the cloud with Amazon S3, AWS Glue and Amazon Athena. Along the way we
measured what each step costs and saves, scaled the data up 317 times, and reorganised it
so the cloud reads less.

**The story in short**

1. **The answer: yes, but only moderately.** Popular movies are rated higher, even after
   checking user habits, release decade, genre and an independent audience (IMDb). On the
   full 32M dataset the effect is weaker than on the 100k sample.
2. **The same answer on a laptop and in the cloud.** The data was moved to S3, catalogued
   with Glue and queried with Athena; every table has exactly the same rows in both places.
3. **Parquet pays off in the cloud.** Athena bills the data it reads; Parquet reads 10 to 67
   times less than CSV for the same question, so it costs 10 to 67 times less.
4. **More data, not much more time.** With 317 times more ratings, Athena read 320 times
   more data but took only 2.4 times longer.
5. **Partitioning by year reads 160 times less** for a question about one year, but does
   not help questions that cover every year.

---

## 1. The question and the data

- **Popularity** = how many ratings a movie received.
- **Quality** = the movie's average rating.

| Dataset                    | What it contains                                               | Size              |
| -------------------------- | -------------------------------------------------------------- | ----------------- |
| MovieLens "latest-small"   | 100,836 ratings by 610 users on 9,742 movies (called **100k**) | 3 MB              |
| MovieLens 32M              | 32,000,204 ratings by 200,948 users on 87,585 movies           | 877 MB of ratings |
| IMDb `title.basics`, `title.ratings` | every IMDb title and its rating and number of votes  | 1.1 GB            |

MovieLens and IMDb are linked through `links.csv`, which gives the IMDb id of every
MovieLens movie. IMDb gives a second, independent audience for the same movies.

## 2. The answer, on a laptop

The analysis is in `src/popularity_vs_quality.ipynb`. The raw CSV files are converted to
Parquet once with DuckDB, loaded into DuckDB tables, and every question is a SQL query.

**On the 100k sample:**

- Movies with **100 or more ratings average 3.82 stars**; movies with 2–4 ratings
  average 3.18.
- Over all movies the link is almost invisible (Spearman correlation 0.04), because most
  movies have only a handful of ratings and their averages are mostly noise. Among movies
  with at least 50 ratings it is clearly positive: **0.31**.
- It survives every check:
  - **User habits**: heavy raters are harsher, but after comparing each rating with the
    rater's own average, about two thirds of the gap remains (correlation 0.28);
  - **Release decade and genre**: positive inside every decade and every genre;
  - **IMDb**: the same movies are also rated higher on IMDb when they are popular on
    MovieLens (0.35).

**On the full 32M dataset**, the direction holds but the effect is weaker:

| Check                                              | 100k        | 32M         |
| -------------------------------------------------- | ----------- | ----------- |
| Average rating, 2–4 ratings vs 100+ ratings        | 3.18 → 3.82 | 2.92 → 3.30 |
| Correlation, movies with ≥ 50 ratings              | 0.31        | 0.22        |
| Same, after removing user habits                   | 0.28        | 0.07        |
| Correlation with the IMDb rating                   | 0.35        | 0.18        |

**Conclusion:** popularity is a signal of quality, not a guarantee. Good movies attract
more viewers and people choose movies they expect to like, so the two go together, but the
link is moderate, and on the large dataset most of it is explained by who does the rating.

## 3. Moving to the cloud

The same data was moved to AWS so the whole group can query one shared copy, without
downloading anything, and paying only for what is queried.

```
              LOCAL (laptop)                                CLOUD (AWS, us-east-1)
 ┌────────────────────────────────────────┐        ┌──────────────────────────────────────────────┐
 │ data/  CSV / TSV                       │ upload │ s3://bdf-movies-data-g23/data/     CSV / TSV │
 │   │  DuckDB (notebook)                 │ ─────▶ │ s3://bdf-movies-data-g23/parquet/  Parquet   │
 │   ▼                                    │        │        │                                     │
 │ src/parquet/  Parquet                  │        │        ▼                                     │
 │   │                                    │        │ Glue Data Catalog                            │
 │   ▼                                    │        │   bdf_project (100k), bdf_project_32m (32M)  │
 │ notebook: SQL + Polars + Plotly        │        │        │                                     │
 └────────────────────────────────────────┘        │        ▼                                     │
                                                   │ Athena (SQL)                                 │
                                                   │   results in s3://bdf-athena-results-g23/    │
                                                   └──────────────────────────────────────────────┘
```

- **Amazon S3** stores the files: the original CSV/TSV files and the Parquet files, one
  folder per table (Athena reads every file in a table's folder, so tables cannot share
  one).
- **AWS Glue** describes them: a **crawler** reads the Parquet folders and creates the
  tables in the **Data Catalog** (database `bdf_project` for 100k, `bdf_project_32m` for
  32M).
- **Amazon Athena** answers SQL questions directly on the files in S3.

```
s3://bdf-movies-data-g23/
├── data/                                   original files
│   ├── ml-100k/  movies/  ratings/  links/
│   ├── ml-32m/   movies/  ratings/  links/
│   └── imdb/     title.basics/  title.ratings/
└── parquet/                                converted files
    ├── ml-100k/  movies/  ratings/  links/
    ├── ml-32m/   movies/  ratings/  links/  ratings_by_year/  (see section 6)
    └── imdb/     imdb_basics/  imdb_ratings/
```

**One design choice**: the tables over the CSV files (`raw_ratings`, `raw_imdb_basics`,
`raw_imdb_ratings`) are created with SQL instead of the crawler. The crawler would read the
IMDb id in `links.csv` as a number and drop its leading zeros (`0114709` → `114709`), which
breaks the link between MovieLens and IMDb.

**Check: the cloud has exactly the same data.** Every table has the same number of rows in
Athena as on the laptop:

| Table         | 100k      | 32M        |
| ------------- | --------- | ---------- |
| movies        | 9,742     | 87,585     |
| ratings       | 100,836   | 32,000,204 |
| links         | 9,742     | 87,585     |
| imdb_basics   | 761,268   | 761,268    |
| imdb_ratings  | 1,733,543 | 1,733,543  |

## 4. Does Parquet pay off in the cloud?

Athena bills the **data it scans** ($5 per TB, at least 10 MB per query) and shows it,
with the **run time**, under every query. So the fair test is to ask the same question on
the CSV files and on the Parquet files, and compare those two numbers.

`cloud/benchmark.py` does exactly that for two questions from the notebook:

- **Average rating per popularity level** (reads the MovieLens ratings);
- **IMDb rating per number of votes** (reads the 1.1 GB IMDb title list and the IMDb ratings).

Each question runs 3 times on CSV and 3 times on Parquet (the middle time is kept), and the
script checks that both formats give the same answer. The results are saved in
`cloud/benchmark_results_ml-100k.csv` and `cloud/benchmark_results_ml-32m.csv`.

**Parquet files are smaller** (each column is stored and compressed separately):

| File                  | CSV    | Parquet            |
| --------------------- | ------ | ------------------ |
| MovieLens 32M ratings | 877 MB | 219 MB (4× smaller) |
| IMDb title list       | 1.1 GB | 18 MB, films only (60× smaller) |

**And Athena reads only the columns a question needs**, so it scans much less:

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
- **Counting rows scans nothing**: a Parquet file stores its row count, so the row check
  in section 3 read 0 MB.

## 5. More data, not much more time

The same question (average rating per popularity level, on Parquet), on 317 times more
ratings:

|              | 100k    | 32M    | Growth |
| ------------ | ------- | ------ | ------ |
| Data scanned | 0.27 MB | 86 MB  | 320×   |
| Run time     | 0.57 s  | 1.36 s | 2.4×   |

Athena splits the work among many machines, so the run time grows far more slowly than
the data. What grows with the data is the cost, which is why the file format, and the way
the files are organised, matter.

## 6. Reading less: partitioning by year

Ratings are an event log: every rating has a date, and many questions are about a period
("ratings in 2015", "the latest years"). If the ratings are stored in **one folder per
year**, Athena can skip the folders a question does not need.

**Why year, and not another column** (number of distinct values in MovieLens 32M):

| Column  | Distinct values | Partition by it?                                     |
| ------- | --------------- | ---------------------------------------------------- |
| year    | 29              | **Yes**: often filtered, few values, large folders   |
| rating  | 10              | No: questions filter by time, not by rating          |
| movieId | 84,432          | No: 84,000 folders of tiny files                     |
| userId  | 200,948         | No: far too many values                              |

This follows Costa, Costa and Santos (2019), *Evaluating partitioning and bucketing
strategies for Hive-based Big Data Warehousing systems*, Journal of Big Data 6:34: partition
by the columns used in filters, prefer time columns, check how many distinct values a
column has, and avoid many small files.

**How it was built:** one Athena `CREATE TABLE ... AS SELECT` (CTAS) statement reads the
32M Parquet ratings, adds a `year` column and writes `ratings_by_year/year=1995/` …
`year=2023/` to S3. It also registers the table and its 29 partitions in the Glue Data
Catalog. CTAS was preferred to a Glue job because it is a single SQL statement, starts in
seconds and bills only the data it reads. The statements are in `cloud/partitioning.sql`.

**Before and after** (Athena console; the three one-year queries all return 1,743,866
ratings with an average of 3.559):

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

## 7. Limits and next steps

- **32M ratings (about 1 GB) still fit on one computer.** The notebook runs on the full
  dataset in seconds on a laptop. The cloud's value at this size is a shared copy and
  pay-per-query; its speed would matter on much larger data (MovieLens has a 1-billion
  ratings version).
- **Every Athena query takes at least about half a second**, however small, and is billed
  at least 10 MB.
- **Parquet was not faster than CSV in run time**, only cheaper. A likely reason is that
  each Parquet file is one large file split into very small blocks, which is harder for
  Athena to divide among its machines.
- **Partitions only help questions that filter by year**; the notebook's questions cover
  every year.
- **The popularity levels (1, 2–4, …, 100+) were designed for the 100k sample.** On 32M
  the "100+" level holds 98% of the ratings, so finer levels would describe it better.
- **Next step:** sort the ratings by movie inside each year, so that questions about one
  movie can skip data too.

---

## Reproduce it

### 1. Install

Python 3.11 or newer is required (`duckdb==2.0.0` does not install on older versions).

```bash
python3 -m venv .venv            # or: uv venv --python 3.13 .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # or: uv pip install -r requirements.txt
```

### 2. Download the data into `data/`

- **MovieLens 100k:** `ml-latest-small.zip` from
  <https://grouplens.org/datasets/movielens/latest/>; unzip it and rename the folder
  `ml-latest-small` to `ml-100k`.
- **MovieLens 32M:** `ml-32m.zip` from <https://grouplens.org/datasets/movielens/32m/>;
  unzip it (folder `ml-32m`).
- **IMDb:** `title.basics.tsv.gz` and `title.ratings.tsv.gz` from
  <https://datasets.imdbws.com/>; extract both into `data/imdb/`. IMDb updates these files
  every day, so counts change slightly; the results above use the files of 2026-11-18.

```
data/
├── ml-100k/   movies.csv, ratings.csv, links.csv
├── ml-32m/    movies.csv, ratings.csv, links.csv
└── imdb/      title.basics.tsv, title.ratings.tsv
```

### 3. Run the analysis on the laptop

```bash
cd src
jupyter lab popularity_vs_quality.ipynb
```

The first run converts the raw files to Parquet in `src/parquet/`. The notebook uses the
100k sample by default; to run it on 32M:

```bash
BDF_DATASET=ml-32m jupyter nbconvert --to notebook --execute popularity_vs_quality.ipynb \
    --output popularity_vs_quality_ml-32m.ipynb
```

### 4. Set up the cloud

The project uses an AWS Academy Learner Lab. Start the lab, open **AWS Details → AWS CLI:
Show** and paste the block into `~/.aws/credentials` (it expires when the lab session ends).
Then, from the project folder:

```bash
python cloud/aws_setup.py                    # 100k: buckets, upload, Glue crawler, row check
python cloud/aws_setup.py --dataset ml-32m   # the same for 32M
```

The script can be run again safely: it reuses the buckets, files, database and crawler that
already exist.

### 5. Measure

```bash
python cloud/benchmark.py                    # CSV vs Parquet in Athena, 100k
python cloud/benchmark.py --dataset ml-32m   # the same for 32M
```

About a minute per dataset, less than $0.05 in total. For the partitioned table, run the
statements in `cloud/partitioning.sql` one by one in the Athena console (database
`bdf_project_32m`).

## Project files

```
src/popularity_vs_quality.ipynb   the analysis (laptop)
cloud/aws_setup.py                uploads to S3, runs the Glue crawler, checks row counts in Athena
cloud/benchmark.py                asks the same questions in Athena on CSV and on Parquet
cloud/benchmark_results_*.csv     benchmark results, one file per dataset
cloud/partitioning.sql            builds the ratings table partitioned by year and measures it
requirements.txt                  Python packages
```

Not in the repository (downloaded or created when the project runs): `data/`,
`src/parquet/`, `*.duckdb`, `s3_upload/`, `.venv/`.

## Still to do

- Slides for the presentation.
- Remove the unused folders `parquet/movies/`, `parquet/ratings/`, `parquet/links/`,
  `parquet/imdb_basics/` and `parquet/imdb_ratings/` from the S3 data bucket.
