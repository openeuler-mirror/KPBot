package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"os"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/cloudwego/kitex/client"
	"github.com/cloudwego/kitex/pkg/remote/codec/thrift"
	"github.com/cloudwego/kitex/pkg/stats"
	"github.com/cloudwego/kitex/transport"

	echo "demo/kitex_gen/echo"
	echocli "demo/kitex_gen/echo/echo"
)

type result struct {
	Clients    int     `json:"clients"`
	DurationS  float64 `json:"duration_s"`
	Size       int     `json:"size"`
	Nums       int     `json:"nums"`
	Mux        int     `json:"mux"`
	TTHeader   bool    `json:"ttheader"`
	Frugal     bool    `json:"frugal"`
	StatsLevel int     `json:"stats"`
	Count      int64   `json:"count"`
	Errors     int64   `json:"errors"`
	QPS        float64 `json:"qps"`
	P50us      int64   `json:"p50_us"`
	P90us      int64   `json:"p90_us"`
	P99us      int64   `json:"p99_us"`
	Maxus      int64   `json:"max_us"`
	Avgus      int64   `json:"avg_us"`
	CPUS       float64 `json:"client_cpu_s"`
	ErrorRate  float64 `json:"error_rate"`
	Capped     int64   `json:"samples_capped"`
	Aborted    bool    `json:"aborted"`
	Valid      bool    `json:"valid"`
}

const (
	maxErrorRate      = 0.01
	earlyAbortRate    = 0.5
	earlyAbortMin     = 200
	sampleBudgetBytes = 64 << 20
	samplePerWorker   = 262144
	sampleMinWorker   = 1024
)

type runResult struct {
	total   int64
	errs    int64
	capped  int64
	aborted bool
	lat     [][]int64
}

func cpuSeconds(ru syscall.Rusage) float64 {
	return float64(ru.Utime.Sec+ru.Stime.Sec) + float64(ru.Utime.Usec+ru.Stime.Usec)/1e6
}

func main() {
	target := flag.String("target", "127.0.0.1:8888", "server address")
	conc := flag.Int("c", 64, "concurrent clients")
	dur := flag.Duration("d", 10*time.Second, "measure duration")
	warm := flag.Duration("warmup", 2*time.Second, "warmup duration")
	size := flag.Int("size", 256, "payload bytes")
	nums := flag.Int("nums", 8, "list length")
	mux := flag.Int("mux", 0, "mux connection num, 0 = off")
	tt := flag.Bool("ttheader", false, "use TTHeader transport")
	frugal := flag.Bool("frugal", false, "use frugal codec")
	statsLevel := flag.Int("stats", 0, "stats level")
	out := flag.String("o", "", "write json result to file")
	flag.Parse()

	if *conc <= 0 {
		log.Fatal("-c must be > 0")
	}
	if *dur <= 0 {
		log.Fatal("-d must be > 0")
	}
	if *warm < 0 {
		log.Fatal("-warmup must be >= 0")
	}

	opts := []client.Option{
		client.WithHostPorts(*target),
	}
	if *mux > 0 {
		opts = append(opts, client.WithMuxConnection(*mux))
	}
	if *tt {
		opts = append(opts, client.WithTransportProtocol(transport.TTHeader))
	}
	if *frugal {
		opts = append(opts, client.WithPayloadCodec(thrift.NewThriftCodecWithConfig(thrift.FrugalReadWrite)))
	}
	switch *statsLevel {
	case 1:
		opts = append(opts, client.WithStatsLevel(stats.LevelBase))
	case 2:
		opts = append(opts, client.WithStatsLevel(stats.LevelDetailed))
	default:
		opts = append(opts, client.WithStatsLevel(stats.LevelDisabled))
	}

	cli, err := echocli.NewClient("echo", opts...)
	if err != nil {
		log.Fatal(err)
	}

	payload := strings.Repeat("x", *size)
	baseNums := make([]int64, *nums)
	for i := range baseNums {
		baseNums[i] = int64(i)
	}

	perWorker := sampleBudgetBytes / (*conc * 8)
	if perWorker < sampleMinWorker {
		perWorker = sampleMinWorker
	}
	if perWorker > samplePerWorker {
		perWorker = samplePerWorker
	}

	run := func(d time.Duration, collect bool, bufs [][]int64) runResult {
		var total, errs, capped, attempts int64
		var aborted int32
		var wg sync.WaitGroup
		deadline := time.Now().Add(d)
		for i := 0; i < *conc; i++ {
			wg.Add(1)
			go func(id int) {
				defer wg.Done()
				req := &echo.EchoRequest{Msg: "hello", Id: int64(id), Nums: baseNums, Payload: payload}
				var local []int64
				if collect {
					local = bufs[id]
				}
				ctx := context.Background()
				for time.Now().Before(deadline) {
					if atomic.LoadInt32(&aborted) == 1 {
						break
					}
					t0 := time.Now()
					resp, err := cli.Echo(ctx, req)
					el := time.Since(t0).Microseconds()
					n := atomic.AddInt64(&attempts, 1)
					if err != nil || resp == nil {
						atomic.AddInt64(&errs, 1)
					} else {
						atomic.AddInt64(&total, 1)
						if collect {
							if len(local) < cap(local) {
								local = append(local, el)
							} else {
								atomic.AddInt64(&capped, 1)
							}
						}
					}
					if n%256 == 0 && n >= earlyAbortMin && float64(atomic.LoadInt64(&errs))/float64(n) > earlyAbortRate {
						atomic.StoreInt32(&aborted, 1)
						break
					}
				}
				if collect {
					bufs[id] = local
				}
			}(i)
		}
		wg.Wait()
		return runResult{total: total, errs: errs, capped: capped, aborted: aborted == 1, lat: bufs}
	}

	_ = run(*warm, false, nil)

	latBuf := make([][]int64, *conc)
	for i := range latBuf {
		latBuf[i] = make([]int64, 0, perWorker)
	}

	var ru0, ru1 syscall.Rusage
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru0)
	start := time.Now()
	res := run(*dur, true, latBuf)
	elapsed := time.Since(start).Seconds()
	_ = syscall.Getrusage(syscall.RUSAGE_SELF, &ru1)

	all := make([]int64, 0, res.total)
	for _, s := range res.lat {
		all = append(all, s...)
	}
	sort.Slice(all, func(i, j int) bool { return all[i] < all[j] })
	pct := func(p float64) int64 {
		if len(all) == 0 {
			return 0
		}
		i := int(float64(len(all)) * p)
		if i >= len(all) {
			i = len(all) - 1
		}
		return all[i]
	}
	var sum int64
	for _, v := range all {
		sum += v
	}
	r := result{
		Clients: *conc, DurationS: elapsed, Size: *size, Nums: *nums,
		Mux: *mux, TTHeader: *tt, Frugal: *frugal, StatsLevel: *statsLevel,
		Count: res.total, Errors: res.errs, QPS: float64(res.total) / elapsed,
		P50us: pct(0.50), P90us: pct(0.90), P99us: pct(0.99), Maxus: pct(1.0),
		Capped: res.capped, Aborted: res.aborted,
	}
	if len(all) > 0 {
		r.Avgus = sum / int64(len(all))
	}
	r.CPUS = cpuSeconds(ru1) - cpuSeconds(ru0)
	if attempts := res.total + res.errs; attempts > 0 {
		r.ErrorRate = float64(res.errs) / float64(attempts)
	}
	r.Valid = res.total > 0 && !res.aborted && r.ErrorRate <= maxErrorRate

	b, _ := json.Marshal(r)
	fmt.Println(string(b))
	if *out != "" {
		_ = os.WriteFile(*out, append(b, '\n'), 0644)
	}
	if !r.Valid {
		os.Exit(2)
	}
}
