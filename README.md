# jenkins-fleet

**Inventory, benchmark and right-size the agents of any Jenkins instance.**

Executor counts are usually guessed once ("it has 8 cores, give it 8") and never
revisited. Then builds slow down on an oversubscribed VM, or a queue builds up
behind an agent that could easily run twice as much. `jenkins-fleet` replaces the
guess with measurements:

1. **Inventory**: every agent's executors, labels, memory, disk and latency, from
   the Jenkins REST API.
2. **Load**: real utilization and queue pressure per node or label, from
   Jenkins' own load statistics. That is up to ~15 days of history in one request,
   with nothing to poll.
3. **Benchmark**: a short CPU, memory and disk probe that runs *inside each
   agent's JVM* through the script console. It needs no plugins, no jobs and no
   setup on the agent.
4. **Recommend**: an executor count per agent that combines hardware capacity
   with observed demand. Every number comes with the reasoning behind it, and
   the tool can write a Groovy script that applies the changes.

It is a single Python package with **no runtime dependencies** (standard library
only), Python 3.10+.

## Install

```bash
pipx install git+https://github.com/ZeroSudoSpell/jenkins-fleet-CLI
```

or, in a virtualenv:

```bash
pip install git+https://github.com/ZeroSudoSpell/jenkins-fleet-CLI
```

## Connect

Use an [API token](https://www.jenkins.io/doc/book/using/remote-access-api/)
(*User → Security → API Token*). Settings come from flags or the environment:

```bash
export JENKINS_URL=https://ci.example.com/
export JENKINS_USER=alice
export JENKINS_API_TOKEN=11aa22bb...
```

| command     | permission needed                     |
|-------------|----------------------------------------|
| `nodes`     | Overall/Read (+ Agent/ExtendedRead for monitor data on some setups) |
| `load`      | Overall/Read                           |
| `benchmark` | **Overall/Administer** (script console) |
| `recommend` | Overall/Read; Administer with `--run-benchmark` |

Use `--ca-bundle ca.pem` for a private CA. CSRF crumbs are handled automatically.

## Usage

### `nodes`: what's in the fleet

```text
$ jenkins-fleet nodes
NODE           STATUS   EXEC  BUSY  ARCH             MEM_MiB  DISK_FREE_MiB  PING_ms  LABELS
-------------  -------  ----  ----  ---------------  -------  -------------  -------  ------------
Built-In Node  online   2     0     Linux (amd64)    8192     51200          0        built-in
linux-big      online   8     2     Linux (amd64)    32768    204800         12       docker,linux
linux-small    online   6     0     Linux (aarch64)  8192     20480          40       linux
win 01         offline  2     0     -                -        -              -        windows

4 nodes, 18 executors, 2 busy now
```

### `load`: how busy it really is

```text
$ jenkins-fleet load --by label --window long
SCOPE         SAMPLES  ONLINE  BUSY_AVG  BUSY_P95  BUSY_MAX  UTIL_AVG  UTIL_P95  QUEUE_AVG  QUEUE_P95
------------  -------  ------  --------  --------  --------  --------  --------  ---------  ---------
label:docker  100      8       7.75      7.75      7.75      0.969     0.969     2          2
label:linux   100      14      8         8         8         0.571     0.571     2          2
overall       100      16      8.5       9         9         0.531     0.562     3          3
```

`--window short` covers the last ~6 hours at one-minute resolution. `--window long`
covers ~15 days at one-hour resolution. Jenkins keeps these as exponential
moving averages, so they show sustained load rather than single-second spikes.

### `benchmark`: what each agent can actually do

```text
$ jenkins-fleet benchmark --label linux --save bench.json
benchmarking 2 node(s), 1 at a time ...
  linux-big: ok (11.2s)
  linux-small: ok (14.8s)
NODE         OK   CORES  MEM_MiB  CPU_1T  CPU_NT  SCALING  DISK_MBps  FILES/s  LOADAVG  SECS  ERROR
-----------  ---  -----  -------  ------  ------  -------  ---------  -------  -------  ----  -----
linux-big    yes  16     32768    1000    15200   0.95     900        20000    0.4      11.2  -
linux-small  yes  4      8192     800     1600    0.5      120        3000     0.3      14.8  -
```

What the probe measures (source: [`benchmark.groovy`](src/jenkins_fleet/benchmark.groovy)):

| column | meaning |
|--------|---------|
| `CORES`, `MEM_MiB` | what the agent JVM sees. Container-aware, so cgroup limits apply. |
| `CPU_1T` | single-thread SHA-256 throughput, MiB/s |
| `CPU_NT` | the same on all cores at once |
| `SCALING` | `CPU_NT / (CPU_1T × cores)`. **The interesting one.** Close to 1.0 means the cores are real. Well below 1.0 means an oversubscribed hypervisor, CPU throttling or noisy neighbours: "8 vCPUs" that behave like 4. |
| `DISK_MBps` | sequential write with `fsync`, in the agent's root directory (`--disk-path` to change) |
| `FILES/s` | create, write and delete 4 KiB files. Checkouts and dependency caches are mostly small files. |

Defaults take ~10–20 s per agent: `--cpu-seconds 3` per CPU phase, a 256 MiB
write test and 2,000 small files. The probe deletes its temporary directory when
it finishes. Agents run **one at a time** by default, because agents on the same
host would skew each other's numbers (`--parallel N` to override). Results from
agents that are running builds are flagged, since their numbers will be low. Use
`--skip-busy`, or run outside busy hours.

### `recommend`: how many executors each agent should have

```text
$ jenkins-fleet recommend --benchmark bench.json --explain
NODE         CURRENT  RECOMMENDED  DELTA  CAPACITY  LIMIT   VERDICT        UTIL_P95  QUEUE_AVG  CONFIDENCE
-----------  -------  -----------  -----  --------  ------  -------------  --------  ---------  ----------
linux-big    8        15           7      15        memory  scale-up       1         2          medium
linux-small  6        2            -4     2         cpu     overcommitted  0.167     2          high

linux-big:
  - cpu: 16 cores / 1 per executor = 16
  - memory: (32768 - 1024 reserved) MiB / 2048 per executor = 15
  - load (long window, 100 samples): p95 utilization 100%, p95 busy 8, mean queue 2
  - label:docker has a mean queue of 2: builds are waiting for nodes like this one
  - demand is unmet and memory capacity allows 15

linux-small:
  - cpu: 4 cores, but multi-thread scaling is only 50% -> ~2 effective / 1 per executor = 2
  - memory: (8192 - 1024 reserved) MiB / 2048 per executor = 3
  - load (long window, 100 samples): p95 utilization 17%, p95 busy 1, mean queue 0
  - label:linux has a mean queue of 2: builds are waiting for nodes like this one
  - 6 executors exceeds cpu capacity of 2: builds will contend

fleet: 14 executors now, 17 recommended, 17 hardware capacity, p95 demand 9 busy (~12 with headroom)
  * builds queued at p95 (queue 3); real demand exceeds what busy executors show
  * overcommitted: linux-small
```

Without benchmark data, `recommend` still works from the memory reported by
Jenkins' node monitors plus load, with lower confidence. Pass `--run-benchmark`
to probe and recommend in one step.

#### The sizing model

**Capacity** is the smallest of these caps, computed from a per-build profile
you can tune:

| cap | formula | flag (default) |
|-----|---------|----------------|
| CPU | `cores ÷ cpus-per-executor`, with cores × scaling when scaling < 90% | `--cpus-per-executor` (1.0) |
| memory | `(total − reserve) ÷ mem-per-executor` | `--mem-per-executor` (2048 MiB), `--reserve-mem` (1024 MiB) |
| disk | `free ÷ disk-per-executor` | `--disk-per-executor` (0 = off) |

**Demand** comes from load statistics: p95 utilization (busy ÷ online executors)
and mean queue length. The queue is the larger of the node's own queue and the
queues of its labels. Jenkins only counts a waiting build against a node when it
can run nowhere else, so a build waiting for `linux` shows up only in the
`linux` label's queue.

Capacity is never below 1. Scaling between 90% and 100% is treated as noise
rather than lost cores.

| situation | verdict | recommendation |
|-----------|---------|----------------|
| more executors than capacity | `overcommitted` | capacity |
| saturated (p95 ≥ `--saturation`, 0.85) or queueing, room to grow | `scale-up` | capacity |
| saturated or queueing, already at capacity | `at-capacity` | keep, and add agents for that label |
| p95 utilization ≤ 25% | `underused` | keep, and consider consolidating |
| otherwise | `ok` | keep |

Tune the profile to your workload. A C++ build using `make -j4` is closer to
`--cpus-per-executor 4`. A Gradle build with a 4 GB heap needs
`--mem-per-executor 5120`.

#### Applying changes

```bash
jenkins-fleet recommend --benchmark bench.json --emit-groovy apply.groovy
```

writes a script for *Manage Jenkins → Script Console*. Review it before you run it:

```groovy
// linux-small: 6 -> 2 (overcommitted)
j.getNode('linux-small')?.with { n ->
  n.setNumExecutors(2); j.updateNode(n)
  println 'linux-small: 6 -> 2'
}
```

`jenkins-fleet` itself never changes your configuration.

#### Running it on a schedule

`--check` exits with status **3** when any node should change, so a weekly job
can flag drift. See [`examples/Jenkinsfile`](examples/Jenkinsfile).

### Output formats

Every command takes `-o table|json|csv`. Progress and warnings go to stderr,
so `-o json | jq` works as expected.

## Limitations

- **Cloud agents** (Kubernetes, EC2, …) are short-lived and get their executor
  count from a template. Use the recommendation to change the template, not
  the node.
- Load statistics are moving averages kept in memory. They start at zero and
  reset when the controller restarts, so a young history understates load.
  With fewer than 30 samples, `recommend` says so and lowers its confidence.
- Container-aware numbers come from the benchmark. Jenkins' own memory monitor
  reports the *host's* memory for agents running in containers, so a
  recommendation made without `--run-benchmark` can overestimate capacity.
- The probe measures the agent's own CPU and disk. Builds that run in Docker
  on another host, or that mostly wait on the network, need a profile tuned
  for that.
- The benchmark uses the script console, so it needs an administrator token.
  Treat that token with care.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest            # unit tests + CLI tests against an in-process fake Jenkins
ruff check . && ruff format --check .
```

To also run the Groovy probe on a local JVM, the way the script console runs it
(requires `java`):

```bash
curl -LO https://repo1.maven.org/maven2/org/codehaus/groovy/groovy-all/2.4.21/groovy-all-2.4.21.jar
JFLEET_GROOVY_JAR=$PWD/groovy-all-2.4.21.jar pytest tests/test_probe.py
```

Issues and pull requests are welcome.

## License

[MIT](LICENSE)
