# VIGIL on Hadoop — Streaming MapReduce

This directory contains the real Hadoop Streaming mappers and reducers. They are plain
stdin→stdout programs with **no VIGIL imports**, because Hadoop ships them to the task nodes with
`-files` and runs them under the cluster's Python.

| Job | Mapper | Reducer | Input | Output |
|---|---|---|---|---|
| `symbol_year_profile` | `mapper_symbol_year.py` | `reducer_symbol_year.py` (also combiner) | `date symbol open high low close volume` TSV | `symbol year sessions avg_close total_volume year_high year_low range_pct` |
| `market_breadth` | `mapper_breadth.py` | `reducer_breadth.py` | same OHLCV TSV | `date advancers decliners unchanged breadth_pct total_volume` |
| `news_inverted_index` | `mapper_news_terms.py` | `reducer_news_terms.py` | `news_id symbol headline` TSV | `term document_frequency symbol_count symbols` |

## Two engines, never confused

```
MAPREDUCE_ENGINE=local    # default — vigil/mapreduce/framework.py (real splits/shuffle/reduce,
                          #           separate OS processes, POSIX Parquet blocks as splits)
MAPREDUCE_ENGINE=hadoop   # hadoop jar hadoop-streaming.jar, submitted to a real cluster
```

The Observatory and `/api/health` report `MapReduce Engine: LOCAL FALLBACK` or
`MapReduce Engine: HADOOP`. If `hadoop` is requested without a usable client, the pipeline
**fails with an explanation** — it never relabels the local engine as Hadoop.

## Running the jobs without a cluster (what this machine can do)

```bash
python scripts/run_hadoop_job.py --status          # which engine is active and why
python scripts/run_hadoop_job.py --validate        # compile + cat | mapper | sort | reducer
python scripts/run_hadoop_job.py --print-command   # the exact submission command line
```

`--validate` executes the real mapper and reducer over sample records through a sorted shuffle.
It proves the streaming contract; it is reported as **SCRIPTS VALIDATED LOCALLY — NOT A HADOOP
RUN** and is never counted as a Hadoop execution.

You can also reproduce a job end-to-end with Unix pipes on the exported TSV:

```bash
python - <<'PY'
from vigil.config import load_config
from vigil.mapreduce.hadoop_runner import JOBS, export_streaming_input
from pathlib import Path
print(export_streaming_input(load_config(), JOBS["market_breadth"], Path("data/processed/hadoop_input")))
PY
cat data/processed/hadoop_input/market_breadth.tsv \
  | python3 hadoop/mapper_breadth.py | sort | python3 hadoop/reducer_breadth.py | head
```

## Provisioning a cluster (single node is enough)

```bash
# 1. Java + Hadoop
sudo apt-get install -y openjdk-17-jdk
curl -O https://downloads.apache.org/hadoop/common/hadoop-3.4.1/hadoop-3.4.1.tar.gz
tar xzf hadoop-3.4.1.tar.gz -C /opt && export HADOOP_HOME=/opt/hadoop-3.4.1
export JAVA_HOME=$(dirname $(dirname $(readlink -f $(which java))))
export PATH=$PATH:$HADOOP_HOME/bin:$HADOOP_HOME/sbin
export HADOOP_STREAMING_JAR=$HADOOP_HOME/share/hadoop/tools/lib/hadoop-streaming-3.4.1.jar

# 2. Pseudo-distributed HDFS (core-site.xml: fs.defaultFS = hdfs://localhost:9000)
hdfs namenode -format -force
start-dfs.sh && start-yarn.sh
hdfs dfs -mkdir -p /vigil

# 3. Point VIGIL at it and run
export STORAGE_MODE=hdfs HDFS_URI=hdfs://localhost:9000 MAPREDUCE_ENGINE=hadoop
export CLASSPATH=$(hadoop classpath --glob)
python scripts/storage_check.py            # NameNode connectivity + parquet write/read test
python scripts/run_hadoop_job.py --run     # genuine Hadoop Streaming submission
```

`--run` stages the TSV input with `hdfs dfs -put`, submits each job, parses the Hadoop counters
(`Map input records`, `Map output records`, `Reduce output records`) and writes
`reports/results/hadoop_jobs.json`. Those counters are the only thing VIGIL will ever present as
evidence of a Hadoop run.
