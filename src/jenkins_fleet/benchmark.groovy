// jenkins-fleet agent benchmark.
//
// Runs inside the agent JVM through the Jenkins script console
// (POST /computer/<name>/scriptText). Uses only JDK classes plus Groovy,
// leaves nothing behind, and prints a single "JFLEET_RESULT {json}" line.
//
// Placeholders (@...@) are filled in by jenkins_fleet/benchmark.py.

import java.lang.management.ManagementFactory
import java.security.MessageDigest
import java.util.concurrent.Callable
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import javax.management.ObjectName

def cpuSeconds = @CPU_SECONDS@
def diskMb = @DISK_MB@
def smallFiles = @SMALL_FILES@
def diskPath = @DISK_PATH@

def MB = 1024L * 1024L
def result = [:]
def errors = []

def rt = Runtime.getRuntime()
result.cores = rt.availableProcessors()
result.os = System.getProperty("os.name") + " " + System.getProperty("os.version")
result.arch = System.getProperty("os.arch")
result.java = System.getProperty("java.version")
result.jvm_max_heap_mb = (long) (rt.maxMemory() / MB)

// Read OS metrics through JMX attributes rather than casting to
// com.sun.management.* (avoids module-access trouble on newer JDKs).
// These are container-aware: inside a cgroup they report the limits.
def mbs = ManagementFactory.getPlatformMBeanServer()
def osName = new ObjectName("java.lang:type=OperatingSystem")
def osAttr = { String... names ->
    for (n in names) {
        try { return mbs.getAttribute(osName, n) } catch (Throwable ignored) { }
    }
    return null
}
def totalMem = osAttr("TotalMemorySize", "TotalPhysicalMemorySize")
def freeMem = osAttr("FreeMemorySize", "FreePhysicalMemorySize")
def loadAvg = osAttr("SystemLoadAverage")
if (totalMem != null) result.total_memory_mb = (long) (totalMem / MB)
if (freeMem != null) result.free_memory_mb = (long) (freeMem / MB)
if (loadAvg != null && loadAvg >= 0) result.load_average = Math.round(loadAvg * 100) / 100.0d

// ---------------------------------------------------------------- CPU
// SHA-256 over a 64 KiB buffer until the deadline; score = MiB hashed/sec.
def buf = new byte[64 * 1024]
new Random(42).nextBytes(buf)
def hashFor = { long millis ->
    def md = MessageDigest.getInstance("SHA-256")
    long end = System.nanoTime() + millis * 1000000L
    long bytes = 0
    while (System.nanoTime() < end) {
        md.update(buf)
        bytes += buf.length
        if ((bytes & 0xFFFFF) == 0) md.digest()
    }
    md.digest()
    return bytes
}
try {
    hashFor(200L) // JIT warm-up
    long ms = (long) (cpuSeconds * 1000)
    long t0 = System.nanoTime()
    long single = hashFor(ms)
    double singleSec = (System.nanoTime() - t0) / 1e9d
    result.cpu_single_mbps = Math.round(single / MB / singleSec * 10) / 10.0d

    int n = result.cores
    def pool = Executors.newFixedThreadPool(n)
    try {
        def tasks = (1..n).collect { { -> hashFor(ms) } as Callable<Long> }
        t0 = System.nanoTime()
        long total = pool.invokeAll(tasks).collect { it.get() }.sum() as long
        double multiSec = (System.nanoTime() - t0) / 1e9d
        result.cpu_multi_mbps = Math.round(total / MB / multiSec * 10) / 10.0d
    } finally {
        pool.shutdownNow()
        pool.awaitTermination(5, TimeUnit.SECONDS)
    }
    if (result.cpu_single_mbps > 0) {
        result.cpu_scaling = Math.round(result.cpu_multi_mbps / (result.cpu_single_mbps * n) * 1000) / 1000.0d
    }
} catch (Throwable t) {
    errors << ("cpu: " + t)
}

// --------------------------------------------------------------- Disk
// Use the requested directory; with none given, the first writable one of
// the JVM's working directory and its temp directory.
def candidates = (diskPath ? [diskPath] : [System.getProperty("user.dir"), System.getProperty("java.io.tmpdir")])
def dir = null
for (c in candidates) {
    def d = new File(c, ".jenkins-fleet-bench-" + System.nanoTime())
    if (d.mkdirs()) { dir = d; result.disk_path = c; break }
}
try {
    if (dir == null) throw new IOException("cannot create a scratch directory in " + candidates)
    result.disk_free_mb = (long) (dir.getUsableSpace() / MB)

    if (diskMb > 0) {
        def f = new File(dir, "seq.bin")
        def chunk = new byte[(int) MB]
        new Random(7).nextBytes(chunk)
        long t0 = System.nanoTime()
        def fos = new FileOutputStream(f)
        try {
            for (int i = 0; i < diskMb; i++) fos.write(chunk)
            fos.getFD().sync()
        } finally {
            fos.close()
        }
        double sec = (System.nanoTime() - t0) / 1e9d
        result.disk_write_mbps = Math.round(diskMb / sec * 10) / 10.0d
        f.delete()
    }

    if (smallFiles > 0) {
        // Checkouts and dependency caches are mostly small files; this
        // measures create+write+delete throughput (metadata-heavy).
        def small = new byte[4096]
        long t0 = System.nanoTime()
        for (int i = 0; i < smallFiles; i++) {
            def sf = new File(dir, "f" + i)
            def o = new FileOutputStream(sf)
            try { o.write(small) } finally { o.close() }
        }
        for (int i = 0; i < smallFiles; i++) new File(dir, "f" + i).delete()
        double sec = (System.nanoTime() - t0) / 1e9d
        result.small_files_per_sec = Math.round(smallFiles / sec)
    }
} catch (Throwable t) {
    errors << ("disk: " + t)
} finally {
    dir?.listFiles()?.each { it.delete() }
    dir?.delete()
}

result.errors = errors
println "JFLEET_RESULT " + groovy.json.JsonOutput.toJson(result)
return null
