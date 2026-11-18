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
cloud/benchmark.py                same queries on CSV vs Parquet, DuckDB vs Athena
cloud/benchmark_results_*.csv     latest benchmark output, one file per dataset
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

```bash
python cloud/benchmark.py                    # MovieLens 100k
python cloud/benchmark.py --dataset ml-32m   # MovieLens 32M
```

We asked the same questions from the notebook in four ways: on the original **CSV** files
and on **Parquet** files, on a **laptop** (DuckDB) and in the **cloud** (Athena). All four
always gave the same answer. Each run was repeated 3 times and the middle time is shown.
Full numbers: `cloud/benchmark_results_ml-100k.csv` and `cloud/benchmark_results_ml-32m.csv`.

### 1. Parquet files are much smaller

| File                    | CSV     | Parquet             | Smaller by |
| ----------------------- | ------- | ------------------- | ---------- |
| MovieLens 32M ratings   | 877 MB  | 219 MB              | 4×         |
| IMDb list of titles     | 1.1 GB  | 18 MB (films only)  | 60×        |

Uploading the 32M ratings to the cloud took 27 s as CSV and 6 s as Parquet.

### 2. In the cloud, Parquet reads less data, so it costs less

Athena charges for the data it reads ($5 per TB). Parquet stores each column separately,
so Athena reads only the columns a question needs; a CSV file has to be read in full.

| Question (MovieLens 32M)               | Data read: CSV | Parquet | Cost per 1,000 runs: CSV | Parquet |
| -------------------------------------- | -------------- | ------- | ------------------------ | ------- |
| Average rating per popularity level    | 877 MB         | 86 MB   | $4.39                    | $0.43   |
| Rating vs the user's usual rating      | 877 MB         | 88 MB   | $4.39                    | $0.44   |
| IMDb rating per number of votes        | 1,158 MB       | 17 MB   | $5.79                    | $0.09   |
| Number of IMDb genres (one column)     | 1,128 MB       | 0.9 MB  | $5.64                    | $0.05   |

Counting rows read **0 MB**: a Parquet file stores its row count, so Athena does not need
to open the data at all.

### 3. On the laptop, Parquet is 6 to 100 times faster

| Question (MovieLens 32M)               | CSV    | Parquet |
| -------------------------------------- | ------ | ------- |
| Average rating per popularity level    | 395 ms | 60 ms   |
| Rating vs the user's usual rating      | 853 ms | 124 ms  |
| IMDb rating per number of votes        | 357 ms | 23 ms   |
| Number of IMDb genres (one column)     | 286 ms | 3 ms    |

### 4. 317 times more data, only 3 times more time in the cloud

Going from MovieLens 100k to 32M (317× more ratings), question "average rating per
popularity level", Parquet:

|                         | 100k    | 32M      | Growth |
| ----------------------- | ------- | -------- | ------ |
| Data read by Athena     | 0.27 MB | 86 MB    | 320×   |
| Time in Athena          | 0.55 s  | 1.8 s    | 3.3×   |
| Time on the laptop      | 4 ms    | 60 ms    | 15×    |

Athena splits the work among many machines, so its time grows much more slowly than the
data. The whole notebook also still runs on 32M in about 6 seconds on the laptop.

### 5. Limits: where the cloud does not help (yet)

- **At this size, the laptop is faster.** DuckDB answers in 3–124 ms; Athena takes 0.5–4 s,
  because it needs about half a second to start any query. 32M ratings (about 1 GB) still
  fit on one computer.
- **Athena was slower on Parquet than on CSV for the two `ratings` questions**
  (1.8 s vs 1.6 s and 4.0 s vs 2.4 s). The likely reason is that the Parquet file is one
  big file cut into very small blocks, which is harder for Athena to split. Not tested yet.
- **Small questions cost the same in both formats**: Athena charges at least 10 MB per query.
- **What the cloud adds**: one shared copy of the data for the whole group, nothing to
  download, paying only for what is queried, and room to grow to much larger datasets
  (for example MovieLens 1B), where one computer is no longer enough.

## Progress

Done:

- [x] Local layer: notebook with DuckDB, Parquet, Polars and Plotly, reproducible from a
      clean clone
- [x] Notebook works with MovieLens 100k and 32M (`BDF_DATASET`)
- [x] Cloud layer: data in S3, tables in the Glue Data Catalog, queries in Athena, for
      both datasets; row counts match the laptop
- [x] Performance and scalability evidence: CSV vs Parquet, laptop vs cloud, 100k vs 32M

To do:

- [ ] Slides: questions, architecture, evidence, performance/scalability, conclusions
- [ ] Delete the old Parquet folders in S3, no longer used: `parquet/movies/`,
      `parquet/ratings/`, `parquet/links/`, `parquet/imdb_basics/`, `parquet/imdb_ratings/`
- [ ] (Optional) Test other file layouts in Athena: bigger blocks, several files, partitions
