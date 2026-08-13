"""Spark Structured Streaming job for synthetic Project Snow events."""

from __future__ import annotations

import os

from pyspark.sql import SparkSession
from pyspark.sql.functions import col, from_json, to_timestamp
from pyspark.sql.types import ArrayType, IntegerType, StringType, StructField, StructType


schema = StructType(
    [
        StructField("event_id", StringType(), False),
        StructField("event_type", StringType(), False),
        StructField("occurred_at", StringType(), False),
        StructField("character_id", StringType(), False),
        StructField("provider", StringType(), False),
        StructField("model", StringType(), False),
        StructField("request_stage", StringType(), False),
        StructField("error_code", StringType(), True),
        StructField("degraded_services", ArrayType(StringType()), False),
        StructField("latency_ms", IntegerType(), False),
        StructField("feedback_topic", StringType(), True),
    ]
)


spark = (
    SparkSession.builder.appName("project-snow-synthetic-stream")
    .config("spark.sql.catalog.snow", "org.apache.iceberg.spark.SparkCatalog")
    .config("spark.sql.catalog.snow.type", "hive")
    .config("spark.sql.catalog.snow.uri", os.getenv("HIVE_METASTORE_URI", "thrift://hive-metastore:9083"))
    .getOrCreate()
)
spark.sql("CREATE NAMESPACE IF NOT EXISTS snow.analytics")
spark.sql(
    """CREATE TABLE IF NOT EXISTS snow.analytics.synthetic_events (
       event_id string, event_type string, occurred_at timestamp,
       character_id string, provider string, model string,
       request_stage string, error_code string, degraded_services array<string>,
       latency_ms int, feedback_topic string
    ) USING iceberg PARTITIONED BY (days(occurred_at))"""
)

records = (
    spark.readStream.format("kafka")
    .option("kafka.bootstrap.servers", os.getenv("KAFKA_BOOTSTRAP", "kafka:9092"))
    .option("subscribe", "project-snow-synthetic-events")
    .option("startingOffsets", "earliest")
    .load()
    .select(from_json(col("value").cast("string"), schema).alias("event"))
    .select("event.*")
    .withColumn("occurred_at", to_timestamp("occurred_at"))
    .filter(col("event_id").isNotNull() & col("character_id").isNotNull())
)

query = (
    records.writeStream.format("iceberg")
    .outputMode("append")
    .option("checkpointLocation", "s3a://snow-lab/checkpoints/synthetic-events")
    .toTable("snow.analytics.synthetic_events")
)
query.awaitTermination()
