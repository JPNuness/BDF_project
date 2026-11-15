# Popularity vs Quality

Do movies with more ratings have higher ratings? Analysis of MovieLens + IMDb data with DuckDB.

## Setup

1. Create a virtual environment and install the dependencies:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

   On Windows, use `python` instead of `python3` and activate with `.venv\Scripts\activate`.

2. Download the data. The `data/` folder is git-ignored, so it is not in the repo and
   has to be set up by hand once:

   - **MovieLens:** download `ml-latest-small.zip` from
     <https://grouplens.org/datasets/movielens/latest/>, unzip it, rename the extracted
     `ml-latest-small` folder to `ml-100k` and put it inside `data/`.
   - **IMDb:** download `title.basics.tsv.gz` and `title.ratings.tsv.gz` from
     <https://datasets.imdbws.com/>, extract both and put the `.tsv` files in `data/imdb/`.

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
   `src/popularity_vs_quality.duckdb`.
