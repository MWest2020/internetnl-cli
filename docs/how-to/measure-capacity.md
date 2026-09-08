---
status: current
last_reviewed: 2026-09-08
---

# Measuring what your instance can actually carry

The upstream batch stack ships a full monitoring set — Prometheus, Grafana,
a node exporter, a Celery exporter and more — and on the reference
deployment it retains five years. So the question "how much can this box
take" does not need new tooling. It needs the right three queries, and a
load event to point them at.

This page is the queries. It assumes the deployment from
[deploy-instance-vps.md](deploy-instance-vps.md); adjust container names if
yours differ.

## The three numbers that matter

**Available memory** is the first wall you hit. Each Celery worker is capped
by `WORKER_MEMORY_LIMIT` (1 GB by default) and the stack runs several, on top
of routinator's several hundred megabytes of RPKI data.

**Queue depth** tells you whether work is piling up or being consumed. If
`celery_queue_length` stays at zero under load, the bottleneck is not a
backlog — the workers are keeping up and something else is the limit.

**Load average** says whether the CPUs are saturated. Read it against your
core count: 3.75 on two cores is nearly 2× subscribed.

## Running the queries

Prometheus listens inside the compose network, with `/prometheus/` as its
external path. From the host:

```sh
q() {
  docker exec internetnl-prod-prometheus-1 sh -c \
    "wget -qO- \"http://127.0.0.1:9090/prometheus/api/v1/query?query=$1\"" \
    | sed -e 's/.*"value":\[[0-9.]*,"//' -e 's/"\].*//'
}
```

Current state:

```sh
q "node_memory_MemAvailable_bytes/1024/1024"   # MB free
q "sum(celery_queue_length)"                   # total queue depth
q "node_load1"                                 # 1-minute load
```

Looking back at a window that has already passed — this is the useful one,
because you rarely watch a load event live. `@ <unix-timestamp>` anchors the
query to the end of the window you care about:

```sh
E=$(date -u -d '2026-09-08 09:10' +%s)         # end of the window
q "min_over_time(node_memory_MemAvailable_bytes[110m]%20@%20${E})/1024/1024"
q "max_over_time(sum(celery_queue_length)[110m:]%20@%20${E})"
q "max_over_time(node_load1[110m]%20@%20${E})"
```

Note the `%20` — the query goes in a URL, so spaces must be encoded. And note
the `[110m:]` with a colon in the queue query: `sum(...)` produces an instant
vector, so it needs a *subquery* range, not a plain range selector.

## Reading the answer honestly

A measurement of your own sequential batch tells you about sequential
batches. It does not tell you what happens when two tenants submit at the
same moment — for that you need either real concurrent users or a deliberate
concurrent test.

Worked example, from the reference deployment on a 2-core, 3.7 GB VPS
measuring 362 domains in blocks of 25:

| | Idle | Under load |
|---|---|---|
| Available memory | ~995 MB | 520 MB |
| Queue depth | 0 | 0 |
| `node_load1` | ~0.3 | 3.75 |

Conclusion drawn from it: memory is the constraint, the workers kept up, and
the CPUs were saturated but coping. About half the memory headroom went to a
single sequential user — which is a reason to be careful about promising
concurrency, not a reason to believe there is 520 MB spare.
