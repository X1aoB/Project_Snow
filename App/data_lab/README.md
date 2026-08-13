# Project Snow data lab

This local-only profile demonstrates a reproducible data engineering pipeline. It is not a production multi-host cluster.

Architecture: synthetic chat/feedback events enter Kafka KRaft; Spark Structured Streaming consumes and normalizes them; Iceberg tables are stored in MinIO and registered through Hive Metastore; batch jobs produce character retrieval, error-stage and feedback-topic aggregates. Two Spark worker containers simulate distributed execution.

Production API events, raw feedback and QQ values must never enter this lab. Generate only schema-compatible synthetic records with `scripts/generate_synthetic_events.py`.

The reference jobs are `scripts/stream_to_iceberg.py` and `scripts/batch_aggregates.py`. The Spark image must be started with matching Iceberg, Kafka and Hadoop AWS packages; package coordinates are intentionally kept in the launch command so version upgrades are explicit and reviewable. MinIO uses the `snow-lab` bucket for warehouse/checkpoint data and Hive Metastore provides the Iceberg catalog.

Resume wording: “Built a Docker Compose data lab using Kafka KRaft, Spark Standalone with two workers, Hive Metastore, MinIO and Iceberg; implemented reproducible synthetic-event streaming and batch aggregation.” Do not describe it as a production multi-machine cluster.
