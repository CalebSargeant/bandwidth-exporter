// gobench: minimal Go TCP and HTTP throughput tool, protocol-compatible with py_tcp.py and
// py_http_server.py / py_http_client.py so native and Python ends can be mixed.
//
//	gobench tcp-server  -port 5301
//	gobench tcp-client  -port 5301 -dir UP|DOWN -t 10 -chunk 1048576 -P 1
//	gobench http-server -port 8081 [-tls]
//	gobench http-client -port 8081 -dir down|up -t 10 [-tls]
package main

import (
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

const hdrLen = 64

func cpuSelf() float64 {
	var ru syscall.Rusage
	syscall.Getrusage(syscall.RUSAGE_SELF, &ru)
	return float64(ru.Utime.Sec+ru.Stime.Sec) + float64(ru.Utime.Usec+ru.Stime.Usec)/1e6
}

func randBuf(n int) []byte {
	b := make([]byte, n)
	rand.Read(b)
	return b
}

func sendFor(w io.Writer, secs float64, buf []byte) (int64, error) {
	deadline := time.Now().Add(time.Duration(secs * float64(time.Second)))
	var n int64
	for time.Now().Before(deadline) {
		m, err := w.Write(buf)
		n += int64(m)
		if err != nil {
			return n, err
		}
	}
	return n, nil
}

func sinkAll(r io.Reader, chunk int) int64 {
	buf := make([]byte, chunk)
	var n int64
	for {
		m, err := r.Read(buf)
		n += int64(m)
		if err != nil {
			return n
		}
	}
}

// ------------------------------------------------------------------ raw TCP
func tcpServer(port int) {
	ln, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		panic(err)
	}
	for {
		c, err := ln.Accept()
		if err != nil {
			continue
		}
		go func(c *net.TCPConn) {
			defer c.Close()
			hdr := make([]byte, hdrLen)
			if _, err := io.ReadFull(c, hdr); err != nil {
				return
			}
			f := strings.Fields(string(hdr))
			secs, _ := strconv.ParseFloat(f[1], 64)
			chunk, _ := strconv.Atoi(f[2])
			if f[0] == "UP" {
				n := sinkAll(c, chunk)
				out := make([]byte, 8)
				binary.BigEndian.PutUint64(out, uint64(n))
				c.Write(out)
			} else {
				sendFor(c, secs, randBuf(chunk))
				c.CloseWrite()
			}
		}(c.(*net.TCPConn))
	}
}

func tcpClient(port int, dir string, secs float64, chunk, parallel int) (int64, float64) {
	var wg sync.WaitGroup
	var mu sync.Mutex
	var total int64
	var maxT float64
	for i := 0; i < parallel; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, err := net.Dial("tcp", fmt.Sprintf("127.0.0.1:%d", port))
			if err != nil {
				panic(err)
			}
			tc := c.(*net.TCPConn)
			defer tc.Close()
			tc.Write([]byte(fmt.Sprintf("%-64s", fmt.Sprintf("%s %g %d", dir, secs, chunk))))
			t0 := time.Now()
			var n int64
			if dir == "UP" {
				sendFor(tc, secs, randBuf(chunk))
				tc.CloseWrite()
				out := make([]byte, 8)
				io.ReadFull(tc, out)
				n = int64(binary.BigEndian.Uint64(out))
			} else {
				n = sinkAll(tc, chunk)
			}
			el := time.Since(t0).Seconds()
			mu.Lock()
			total += n
			if el > maxT {
				maxT = el
			}
			mu.Unlock()
		}()
	}
	wg.Wait()
	return total, maxT
}

// ------------------------------------------------------------------ HTTP
func certDir() string {
	exe, _ := os.Executable()
	return filepath.Join(filepath.Dir(exe), "..", "certs")
}

func httpServer(port int, useTLS bool) {
	payloads := map[int][]byte{}
	var pmu sync.Mutex
	get := func(n int) []byte {
		pmu.Lock()
		defer pmu.Unlock()
		if b, ok := payloads[n]; ok {
			return b
		}
		payloads[n] = randBuf(n)
		return payloads[n]
	}
	mux := http.NewServeMux()
	mux.HandleFunc("/download", func(w http.ResponseWriter, r *http.Request) {
		secs, err := strconv.ParseFloat(r.URL.Query().Get("seconds"), 64)
		if err != nil {
			secs = 10
		}
		chunk, err := strconv.Atoi(r.URL.Query().Get("chunk"))
		if err != nil {
			chunk = 1 << 20
		}
		w.Header().Set("Content-Type", "application/octet-stream")
		w.Header().Set("Cache-Control", "no-store")
		sendFor(w, secs, get(chunk))
	})
	mux.HandleFunc("/upload", func(w http.ResponseWriter, r *http.Request) {
		n := sinkAll(r.Body, 1<<20)
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, "{\"bytes\": %d}", n)
	})
	srv := &http.Server{Addr: fmt.Sprintf("127.0.0.1:%d", port), Handler: mux}
	if useTLS {
		panic(srv.ListenAndServeTLS(filepath.Join(certDir(), "cert.pem"), filepath.Join(certDir(), "key.pem")))
	}
	panic(srv.ListenAndServe())
}

type timedReader struct {
	deadline time.Time
	buf      []byte
}

func (t *timedReader) Read(p []byte) (int, error) {
	if time.Now().After(t.deadline) {
		return 0, io.EOF
	}
	return copy(p, t.buf), nil
}

func (t *timedReader) WriteTo(w io.Writer) (int64, error) {
	var n int64
	for time.Now().Before(t.deadline) {
		m, err := w.Write(t.buf)
		n += int64(m)
		if err != nil {
			return n, err
		}
	}
	return n, nil
}

func httpClient(port int, dir string, secs float64, chunk int, useTLS bool) (int64, float64) {
	tr := &http.Transport{DisableCompression: true}
	scheme := "http"
	if useTLS {
		scheme = "https"
		pem, err := os.ReadFile(filepath.Join(certDir(), "cert.pem"))
		if err != nil {
			panic(err)
		}
		pool := x509.NewCertPool()
		pool.AppendCertsFromPEM(pem)
		tr.TLSClientConfig = &tls.Config{RootCAs: pool}
	}
	cl := &http.Client{Transport: tr}
	base := fmt.Sprintf("%s://127.0.0.1:%d", scheme, port)
	t0 := time.Now()
	var n int64
	if dir == "down" {
		resp, err := cl.Get(fmt.Sprintf("%s/download?seconds=%g&chunk=%d", base, secs, chunk))
		if err != nil {
			panic(err)
		}
		n = sinkAll(resp.Body, chunk)
		resp.Body.Close()
	} else {
		body := &timedReader{deadline: time.Now().Add(time.Duration(secs * float64(time.Second))), buf: randBuf(chunk)}
		req, _ := http.NewRequest("POST", base+"/upload", io.NopCloser(body))
		req.Header.Set("Content-Type", "application/octet-stream")
		resp, err := cl.Do(req)
		if err != nil {
			panic(err)
		}
		var out struct {
			Bytes int64 `json:"bytes"`
		}
		json.NewDecoder(resp.Body).Decode(&out)
		resp.Body.Close()
		n = out.Bytes
	}
	return n, time.Since(t0).Seconds()
}

func main() {
	if len(os.Args) < 2 {
		fmt.Println("usage: gobench tcp-server|tcp-client|http-server|http-client [flags]")
		os.Exit(2)
	}
	mode := os.Args[1]
	fs := flag.NewFlagSet(mode, flag.ExitOnError)
	port := fs.Int("port", 5301, "port")
	dir := fs.String("dir", "UP", "UP|DOWN for tcp, down|up for http")
	secs := fs.Float64("t", 10, "seconds")
	chunk := fs.Int("chunk", 1<<20, "write/read size")
	par := fs.Int("P", 1, "parallel streams (tcp)")
	useTLS := fs.Bool("tls", false, "https")
	fs.Parse(os.Args[2:])
	c0 := cpuSelf()
	var n int64
	var el float64
	switch mode {
	case "tcp-server":
		tcpServer(*port)
		return
	case "http-server":
		httpServer(*port, *useTLS)
		return
	case "tcp-client":
		n, el = tcpClient(*port, *dir, *secs, *chunk, *par)
	case "http-client":
		n, el = httpClient(*port, *dir, *secs, *chunk, *useTLS)
	}
	out, _ := json.Marshal(map[string]any{"tool": "gobench", "mode": mode, "dir": *dir, "parallel": *par, "tls": *useTLS,
		"chunk": *chunk, "bytes": n, "seconds": el, "gbps": float64(n) * 8 / el / 1e9, "client_cpu_self_s": cpuSelf() - c0})
	fmt.Println(string(out))
}
