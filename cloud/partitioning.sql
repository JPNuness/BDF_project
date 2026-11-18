-- Partitioning the MovieLens 32M ratings by year, in Athena (CTAS)
--
-- Run in the Athena console: Data source AwsDataCatalog, database bdf_project_32m.
-- Run each statement on its own and write down the "Data scanned" shown under it.
-- Results are in the README, section "Partitioning by year".


-- 1. Build the partitioned table (run ONCE: the target folder must be empty).
--    Reads the Parquet ratings, adds a year column and writes one folder per year
--    (year=1995/ ... year=2023/) to S3. The table and its 29 partitions are registered
--    in the Glue Data Catalog automatically.
CREATE TABLE ratings_by_year
WITH (
  format = 'PARQUET',
  write_compression = 'SNAPPY',
  external_location = 's3://bdf-movies-data-g23/parquet/ml-32m/ratings_by_year/',
  partitioned_by = ARRAY['year']
) AS
SELECT userid, movieid, rating, "timestamp",
       year(from_unixtime("timestamp")) AS year
FROM ratings;


-- 2. Checks: 29 partitions (1995 to 2023) and 32,000,204 rows, as in ratings.
SHOW PARTITIONS ratings_by_year;

SELECT COUNT(*) AS n FROM ratings_by_year;


-- 3. One year, before and after. All three return n = 1,743,866, avg_rating = 3.559.

-- 3a. Before: the unpartitioned table has to read every row's timestamp.
SELECT COUNT(*) AS n, ROUND(AVG(rating), 3) AS avg_rating
FROM ratings
WHERE year(from_unixtime("timestamp")) = 2015;

-- 3b. After: filter on the partition column, Athena reads only year=2015/.
SELECT COUNT(*) AS n, ROUND(AVG(rating), 3) AS avg_rating
FROM ratings_by_year
WHERE year = 2015;

-- 3c. Trap: same meaning, but the filter is on the timestamp, so no folder is skipped.
SELECT COUNT(*) AS n, ROUND(AVG(rating), 3) AS avg_rating
FROM ratings_by_year
WHERE year(from_unixtime("timestamp")) = 2015;


-- 4. The notebook's main question (all years): partitions cannot help here.

-- 4a. On ratings
WITH per_movie AS (SELECT movieid, COUNT(*) AS n, AVG(rating) AS avg_rating
                   FROM ratings GROUP BY movieid)
SELECT CASE WHEN n = 1 THEN '1' WHEN n < 5 THEN '2-4' WHEN n < 10 THEN '5-9'
            WHEN n < 20 THEN '10-19' WHEN n < 50 THEN '20-49'
            WHEN n < 100 THEN '50-99' ELSE '100+' END AS popularity,
       COUNT(*) AS movies, ROUND(AVG(avg_rating), 2) AS mean_rating
FROM per_movie GROUP BY 1 ORDER BY MIN(n);

-- 4b. On ratings_by_year
WITH per_movie AS (SELECT movieid, COUNT(*) AS n, AVG(rating) AS avg_rating
                   FROM ratings_by_year GROUP BY movieid)
SELECT CASE WHEN n = 1 THEN '1' WHEN n < 5 THEN '2-4' WHEN n < 10 THEN '5-9'
            WHEN n < 20 THEN '10-19' WHEN n < 50 THEN '20-49'
            WHEN n < 100 THEN '50-99' ELSE '100+' END AS popularity,
       COUNT(*) AS movies, ROUND(AVG(avg_rating), 2) AS mean_rating
FROM per_movie GROUP BY 1 ORDER BY MIN(n);


-- Clean-up (only if the table must be rebuilt): DROP TABLE keeps the files,
-- so also delete the folder parquet/ml-32m/ratings_by_year/ in the S3 console.
-- DROP TABLE ratings_by_year;
