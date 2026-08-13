"""Reproducible Spark batch aggregates for the synthetic Iceberg table."""

from __future__ import annotations

from pyspark.sql import SparkSession
from pyspark.sql.functions import avg, count, desc, explode_outer


spark = SparkSession.builder.appName("project-snow-synthetic-batch").getOrCreate()
events = spark.table("snow.analytics.synthetic_events")

(
    events.groupBy("character_id")
    .agg(count("*").alias("requests"), avg("latency_ms").alias("average_latency_ms"))
    .orderBy(desc("requests"))
    .writeTo("snow.analytics.character_retrieval_stats")
    .using("iceberg")
    .createOrReplace()
)
(
    events.groupBy("request_stage", "error_code")
    .count()
    .writeTo("snow.analytics.error_stage_stats")
    .using("iceberg")
    .createOrReplace()
)
(
    events.filter("feedback_topic IS NOT NULL")
    .groupBy("feedback_topic")
    .count()
    .writeTo("snow.analytics.feedback_topic_stats")
    .using("iceberg")
    .createOrReplace()
)
(
    events.select(explode_outer("degraded_services").alias("service"))
    .filter("service IS NOT NULL")
    .groupBy("service")
    .count()
    .writeTo("snow.analytics.degraded_service_stats")
    .using("iceberg")
    .createOrReplace()
)
