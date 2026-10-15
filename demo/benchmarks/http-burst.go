// Standalone deployed-demo diagnostic. One request per connection; no application retries.
package main

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"runtime"
	"sort"
	"sync"
	"time"
)

type Row struct {
	Index        int     `json:"index"`
	ConnectStart float64 `json:"connect_start_ms"`
	Connected    float64 `json:"connected_ms"`
	RequestStart float64 `json:"request_start_ms"`
	FirstByte    float64 `json:"first_byte_ms"`
	Complete     float64 `json:"complete_ms"`
	Status       int     `json:"status"`
	Body         string  `json:"body"`
	Error        string  `json:"error,omitempty"`
}

func main() {
	rawURL := flag.String("url", "", "HTTP health endpoint")
	count := flag.Int("count", 1000, "Independent connections")
	pre := flag.Bool("preconnected", false, "Connect all sockets before releasing requests")
	flag.Parse()
	target, err := url.Parse(*rawURL)
	if err != nil || target.Scheme != "http" || target.Host == "" || *count < 1 {
		fmt.Fprintln(os.Stderr, "requires a valid HTTP URL and positive count")
		os.Exit(2)
	}
	address := target.Host
	if target.Port() == "" {
		address = net.JoinHostPort(target.Hostname(), "80")
	}
	rows := make([]Row, *count)
	connections := make([]net.Conn, *count)
	t0 := time.Now()
	stamp := func() float64 { return float64(time.Since(t0).Nanoseconds()) / 1e6 }
	connect := func(i int) error {
		rows[i].ConnectStart = stamp()
		conn, err := net.DialTimeout("tcp", address, 10*time.Second)
		if err == nil {
			connections[i] = conn
			rows[i].Connected = stamp()
			err = conn.SetDeadline(time.Now().Add(20 * time.Second))
		}
		return err
	}
	if *pre {
		var prepared sync.WaitGroup
		for i := range rows {
			prepared.Add(1)
			go func(i int) {
				defer prepared.Done()
				rows[i].Index = i
				if err := connect(i); err != nil {
					rows[i].Error = err.Error()
				}
			}(i)
		}
		prepared.Wait()
	}
	request := []byte(fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n", target.RequestURI(), target.Host))
	gate := make(chan struct{})
	var staged, finished sync.WaitGroup
	for i := range rows {
		staged.Add(1)
		finished.Add(1)
		go func(i int) {
			defer finished.Done()
			staged.Done()
			<-gate
			rows[i].Index = i
			defer func() {
				if connections[i] != nil {
					connections[i].Close()
				}
			}()
			if rows[i].Error != "" {
				return
			}
			fail := func(err error) { rows[i].Error = err.Error() }
			if !*pre {
				if err := connect(i); err != nil {
					fail(err)
					return
				}
			}
			conn := connections[i]
			rows[i].RequestStart = stamp()
			// net.Conn.Write reports a non-nil error for a short write.
			if _, err := conn.Write(request); err != nil {
				fail(err)
				return
			}
			reader := bufio.NewReader(conn)
			if _, err := reader.Peek(1); err != nil {
				fail(err)
				return
			}
			rows[i].FirstByte = stamp()
			response, err := http.ReadResponse(reader, nil)
			if err != nil {
				fail(err)
				return
			}
			body, err := io.ReadAll(io.LimitReader(response.Body, 1024))
			response.Body.Close()
			if err != nil {
				fail(err)
				return
			}
			rows[i].Status = response.StatusCode
			rows[i].Body = string(body)
			rows[i].Complete = stamp()
			if response.StatusCode != 200 || string(body) != "\"ok\"" {
				fail(fmt.Errorf("unexpected response %d: %q", response.StatusCode, body))
			}
		}(i)
	}
	staged.Wait()
	release := stamp()
	close(gate)
	finished.Wait()
	elapsed := stamp()
	successes := 0
	latency := []float64{}
	starts := []float64{}
	for _, row := range rows {
		if row.Error == "" {
			successes++
			start := row.ConnectStart
			if *pre {
				start = release
			}
			latency = append(latency, row.Complete-start)
		}
		if !*pre || row.RequestStart > 0 {
			start := row.ConnectStart
			if *pre {
				start = row.RequestStart
			}
			starts = append(starts, start)
		}
	}
	sort.Float64s(latency)
	sort.Float64s(starts)
	percentile := func(p int) any {
		if len(latency) == 0 {
			return nil
		}
		return latency[(len(latency)-1)*p/100]
	}
	span := 0.0
	if len(starts) > 0 {
		span = starts[len(starts)-1] - starts[0]
	}
	result := map[string]any{
		"rows": rows, "successes": successes, "failures": *count - successes,
		"batch_ms": elapsed, "release_ms": release, "post_release_ms": elapsed - release,
		"dispatch_span_ms": span, "latency_p50_ms": percentile(50), "latency_p95_ms": percentile(95), "latency_p99_ms": percentile(99),
		"client_note": "Gated native goroutines; measured dispatch span, not literally simultaneous kernel connects; no application retries",
		"go_version":  runtime.Version(), "gomaxprocs": runtime.GOMAXPROCS(0),
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		panic(err)
	}
}
