package main

import (
	"flag"
	"log"
	"net/http"
	_ "net/http/pprof"
	"os"
	"runtime"

	"github.com/cloudwego/kitex/pkg/remote/codec/thrift"
	"github.com/cloudwego/kitex/pkg/stats"
	"github.com/cloudwego/kitex/pkg/utils"
	"github.com/cloudwego/kitex/server"
	"github.com/cloudwego/netpoll"

	echo "demo/kitex_gen/echo/echo"
)

func main() {
	addr := flag.String("addr", ":8888", "listen address")
	useMux := flag.Bool("mux", false, "use mux transport (server side)")
	useFrugal := flag.Bool("frugal", false, "use frugal codec")
	statsLevel := flag.Int("stats", 0, "stats level: 0 disabled, 1 base, 2 detailed")
	pprofAddr := flag.String("pprof", "127.0.0.1:6060", "pprof listen address, empty to disable")
	mutexprof := flag.Int("mutexprof", 0, "runtime.SetMutexProfileFraction")
	blockprof := flag.Int("blockprof", 0, "runtime.SetBlockProfileRate")
	pollers := flag.Int("pollers", 0, "netpoll poller number, 0 = default (GOMAXPROCS/20+1)")
	flag.Parse()

	if *mutexprof > 0 {
		runtime.SetMutexProfileFraction(*mutexprof)
	}
	if *blockprof > 0 {
		runtime.SetBlockProfileRate(*blockprof)
	}
	if *pollers > 0 {
		if err := netpoll.Configure(netpoll.Config{PollerNum: *pollers}); err != nil {
			log.Fatal(err)
		}
	}

	opts := []server.Option{
		server.WithServiceAddr(utils.NewNetAddr("tcp", *addr)),
	}
	if *useMux {
		opts = append(opts, server.WithMuxTransport())
	}
	if *useFrugal {
		opts = append(opts, server.WithPayloadCodec(thrift.NewThriftCodecWithConfig(thrift.FrugalReadWrite)))
	}
	switch *statsLevel {
	case 1:
		opts = append(opts, server.WithStatsLevel(stats.LevelBase))
	case 2:
		opts = append(opts, server.WithStatsLevel(stats.LevelDetailed))
	default:
		opts = append(opts, server.WithStatsLevel(stats.LevelDisabled))
	}

	if *pprofAddr != "" {
		go func() {
			log.Println("pprof on", *pprofAddr, http.ListenAndServe(*pprofAddr, nil))
		}()
	}

	log.Printf("server start addr=%s mux=%v frugal=%v stats=%d pollers=%d gomaxprocs=%d gogc=%s",
		*addr, *useMux, *useFrugal, *statsLevel, *pollers, runtime.GOMAXPROCS(0), os.Getenv("GOGC"))

	svr := echo.NewServer(new(EchoImpl), opts...)
	if err := svr.Run(); err != nil {
		log.Fatal(err)
	}
}
