// solver-gateway: high-performance Turnstile job queue + result store.
//
// Polyglot hybrid stack:
//   Go   — HTTP API, job queue, concurrency, fan-out to Python workers
//   Rust — solver-watchdog (RSS / host pressure → recycle workers)
//   C++  — solver-util (pressure score, token shape, recycle policy)
//   Python — browser solve only (patchright/d3vin), auto GC + context close
//
// Compatible API with Theyka / D3-vin:
//   GET  /turnstile?url=&sitekey=&action=&cdata=
//   GET  /result?id=
//   GET  /health  /stats  /
//
// Extra:
//   POST /v1/solve          JSON body
//   POST /v1/worker/recycle force worker recycle
//   GET  /v1/memory         host + worker RSS snapshot
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"
)

const version = "0.3.0"

type Job struct {
	ID        string `json:"id"`
	URL       string `json:"url"`
	Sitekey   string `json:"sitekey"`
	Action    string `json:"action,omitempty"`
	CData     string `json:"cdata,omitempty"`
	Proxy     string `json:"proxy,omitempty"`
	CreatedAt float64
}

type Result struct {
	ID          string  `json:"id"`
	Status      string  `json:"status"` // pending|success|fail|error|expired
	Value       string  `json:"value,omitempty"`
	ElapsedSec  float64 `json:"elapsed_time,omitempty"`
	Error       string  `json:"error,omitempty"`
	Worker      int     `json:"worker,omitempty"`
	UpdatedAt   float64 `json:"updated_at,omitempty"`
	Recycled    bool    `json:"recycled,omitempty"`
}

type Stats struct {
	Engine          string  `json:"engine"`
	Version         string  `json:"version"`
	QueueDepth      int     `json:"queue_depth"`
	Pending         int64   `json:"pending"`
	Solved          int64   `json:"solved"`
	Failed          int64   `json:"failed"`
	Recycles        int64   `json:"recycles"`
	Workers         int     `json:"workers"`
	WorkerAlive     int     `json:"worker_alive"`
	Concurrency     int     `json:"concurrency"`
	EffectiveSlots  int     `json:"effective_slots"`
	CPUCores        int     `json:"cpu_cores"`
	GOMAXPROCS      int     `json:"gomaxprocs"`
	AvgSolveSec     float64 `json:"avg_solve_sec"`
	UptimeSec       float64 `json:"uptime_sec"`
	HostPressure    int     `json:"host_pressure"`
	HostAvailableMB float64 `json:"host_available_mb"`
	PrefetchOK      int     `json:"prefetch_ok"`
}

type Gateway struct {
	mu             sync.RWMutex
	results        map[string]*Result
	queue          chan Job
	workers        int
	concurrency    int // async pages per browser worker
	workerProcs    []*workerProc
	aliveWorkers   atomic.Int64
	prefetchOK     atomic.Int64
	solveTimeout   time.Duration
	resultTTL      time.Duration
	started        time.Time
	solved         atomic.Int64
	failed         atomic.Int64
	pending        atomic.Int64
	recycles       atomic.Int64
	solveSumMs     atomic.Int64
	solveCount     atomic.Int64
	pythonBin      string
	workerScript   string
	workDir        string
	utilBin        string
	watchdogBin    string
	softMB         int
	hardMB         int
	maxSolves      int // recycle worker after N solves
	browserType    string
	headless       bool
	proxyFile      string
	prefetch       bool
	ctx            context.Context
	cancel         context.CancelFunc
	wg             sync.WaitGroup
	utilOK         bool
}

type workerProc struct {
	id         int
	cmd        *exec.Cmd
	stdin      io.WriteCloser
	stdout     *json.Decoder
	solves     int
	fails      int
	mu         sync.Mutex
	alive      bool
	lastRSS    uint64
	recycled   int
	busy       atomic.Bool
	lastUsed   atomic.Int64 // unix nano
	startedAt  time.Time
}

func env(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}

func envBool(key string, def bool) bool {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	switch strings.ToLower(v) {
	case "1", "true", "yes", "on":
		return true
	case "0", "false", "no", "off":
		return false
	default:
		return def
	}
}

func now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

func newID() string {
	return fmt.Sprintf("%d%x", time.Now().UnixNano(), time.Now().Unix()&0xffff)
}

func (g *Gateway) putResult(r *Result) {
	r.UpdatedAt = now()
	g.mu.Lock()
	g.results[r.ID] = r
	g.mu.Unlock()
}

func (g *Gateway) getResult(id string) *Result {
	g.mu.RLock()
	defer g.mu.RUnlock()
	r := g.results[id]
	if r == nil {
		return nil
	}
	cp := *r
	return &cp
}

func (g *Gateway) purgeExpired() {
	cutoff := now() - g.resultTTL.Seconds()
	g.mu.Lock()
	for id, r := range g.results {
		if r.UpdatedAt > 0 && r.UpdatedAt < cutoff && r.Status != "pending" {
			delete(g.results, id)
		}
	}
	g.mu.Unlock()
}

// --- C++ solver-util CLI bridge (no cgo required) ---

type utilPressure struct {
	TotalKB     uint64 `json:"total_kb"`
	AvailableKB uint64 `json:"available_kb"`
	UsedKB      uint64 `json:"used_kb"`
	Pressure    int    `json:"pressure"`
}

func (g *Gateway) hostPressure() utilPressure {
	if g.utilBin != "" {
		out, err := exec.Command(g.utilBin, "pressure").CombinedOutput()
		if err == nil {
			var p utilPressure
			if json.Unmarshal(out, &p) == nil && p.TotalKB > 0 {
				g.utilOK = true
				return p
			}
		}
	}
	// fallback: read /proc/meminfo in Go
	data, err := os.ReadFile("/proc/meminfo")
	if err != nil {
		return utilPressure{}
	}
	var total, avail, free, buffers, cached uint64
	for _, line := range strings.Split(string(data), "\n") {
		var key string
		var val uint64
		if _, err := fmt.Sscanf(line, "%s %d", &key, &val); err != nil {
			continue
		}
		switch key {
		case "MemTotal:":
			total = val
		case "MemAvailable:":
			avail = val
		case "MemFree:":
			free = val
		case "Buffers:":
			buffers = val
		case "Cached:":
			cached = val
		}
	}
	if avail == 0 {
		avail = free + buffers + cached
	}
	used := uint64(0)
	if total > avail {
		used = total - avail
	}
	pressure := 100
	if total > 0 {
		pressure = int((used * 100) / total)
	}
	return utilPressure{TotalKB: total, AvailableKB: avail, UsedKB: used, Pressure: pressure}
}

func (g *Gateway) tokenOK(token string) bool {
	if g.utilBin != "" {
		out, err := exec.Command(g.utilBin, "token", token).CombinedOutput()
		if err == nil {
			var m map[string]any
			if json.Unmarshal(out, &m) == nil {
				if ok, _ := m["ok"].(bool); ok {
					return true
				}
			}
		}
	}
	n := len(token)
	if n < 20 || n > 4096 {
		return false
	}
	for i := 0; i < n; i++ {
		c := token[i]
		ok := (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
			c == '-' || c == '_' || c == '.' || c == '+' || c == '/' || c == '='
		if !ok {
			return false
		}
	}
	return true
}

func (g *Gateway) processRSS(pid int) uint64 {
	if g.utilBin != "" {
		out, err := exec.Command(g.utilBin, "rss", strconv.Itoa(pid)).CombinedOutput()
		if err == nil {
			var m map[string]any
			if json.Unmarshal(out, &m) == nil {
				if v, ok := m["rss_kb"].(float64); ok {
					return uint64(v)
				}
			}
		}
	}
	data, err := os.ReadFile(fmt.Sprintf("/proc/%d/status", pid))
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if strings.HasPrefix(line, "VmRSS:") {
			fields := strings.Fields(line)
			if len(fields) >= 2 {
				n, _ := strconv.ParseUint(fields[1], 10, 64)
				return n
			}
		}
	}
	return 0
}

func (g *Gateway) shouldRecycle(rssKB uint64, pressure int) bool {
	if g.utilBin != "" {
		out, err := exec.Command(
			g.utilBin, "recycle",
			strconv.FormatUint(rssKB/1024, 10),
			strconv.Itoa(g.softMB),
			strconv.Itoa(g.hardMB),
			strconv.Itoa(pressure),
		).CombinedOutput()
		if err == nil {
			var m map[string]any
			if json.Unmarshal(out, &m) == nil {
				if v, ok := m["recycle"].(bool); ok {
					return v
				}
			}
		}
	}
	soft := uint64(g.softMB) * 1024
	hard := uint64(g.hardMB) * 1024
	if hard > 0 && rssKB >= hard {
		return true
	}
	if soft > 0 && rssKB >= soft && pressure >= 70 {
		return true
	}
	if pressure >= 92 {
		return true
	}
	return false
}

// --- Python browser workers (line-oriented JSON IPC) ---

type workerReq struct {
	Cmd     string `json:"cmd"`
	ID      string `json:"id,omitempty"`
	URL     string `json:"url,omitempty"`
	Sitekey string `json:"sitekey,omitempty"`
	Action  string `json:"action,omitempty"`
	CData   string `json:"cdata,omitempty"`
	Proxy   string `json:"proxy,omitempty"`
}

type workerResp struct {
	OK         bool    `json:"ok"`
	ID         string  `json:"id,omitempty"`
	Value      string  `json:"value,omitempty"`
	Error      string  `json:"error,omitempty"`
	ElapsedSec float64 `json:"elapsed_sec,omitempty"`
	RSSMB      float64 `json:"rss_mb,omitempty"`
	Recycled   bool    `json:"recycled,omitempty"`
}

func (g *Gateway) startWorker(id int) (*workerProc, error) {
	cmd := exec.Command(g.pythonBin, g.workerScript,
		"--worker-id", strconv.Itoa(id),
		"--browser", g.browserType,
		"--soft-mb", strconv.Itoa(g.softMB),
		"--hard-mb", strconv.Itoa(g.hardMB),
		"--max-solves", strconv.Itoa(g.maxSolves),
		"--concurrency", strconv.Itoa(max(1, g.concurrency)),
	)
	if g.headless {
		cmd.Args = append(cmd.Args, "--headless")
	}
	if g.proxyFile != "" {
		cmd.Args = append(cmd.Args, "--proxy-file", g.proxyFile)
	}
	if g.prefetch {
		cmd.Args = append(cmd.Args, "--prefetch")
	}
	cmd.Dir = g.workDir
	// Own process group so gateway stop kills chromium grandchildren
	if runtime.GOOS != "windows" {
		cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	}
	cmd.Env = append(os.Environ(),
		"PYTHONUNBUFFERED=1",
		"PYTHONDONTWRITEBYTECODE=1",
		// multi-core: let asyncio + chromium use available cores
		"OMP_NUM_THREADS="+strconv.Itoa(max(1, runtime.NumCPU()/max(1, g.workers))),
	)
	// strip outer proxies unless worker uses its own file
	filtered := make([]string, 0, len(cmd.Env))
	for _, e := range cmd.Env {
		up := strings.ToUpper(e)
		if strings.HasPrefix(up, "HTTP_PROXY=") || strings.HasPrefix(up, "HTTPS_PROXY=") ||
			strings.HasPrefix(up, "ALL_PROXY=") {
			continue
		}
		filtered = append(filtered, e)
	}
	cmd.Env = filtered
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	wp := &workerProc{
		id:        id,
		cmd:       cmd,
		stdin:     stdin,
		stdout:    json.NewDecoder(stdout),
		alive:     true,
		startedAt: time.Now(),
	}
	wp.lastUsed.Store(time.Now().UnixNano())
	g.aliveWorkers.Add(1)
	fmt.Fprintf(os.Stderr, "[gateway] worker %d started pid=%d concurrency=%d\n",
		id, cmd.Process.Pid, g.concurrency)

	// async prefetch: warm browser + turnstile script in background
	if g.prefetch {
		go func(w *workerProc) {
			w.mu.Lock()
			defer w.mu.Unlock()
			if !w.alive {
				return
			}
			req := workerReq{Cmd: "prefetch"}
			if err := json.NewEncoder(w.stdin).Encode(req); err != nil {
				return
			}
			var resp workerResp
			if err := w.stdout.Decode(&resp); err != nil {
				return
			}
			if resp.OK {
				g.prefetchOK.Add(1)
				fmt.Fprintf(os.Stderr, "[gateway] worker %d prefetch ok rss=%.1fMB\n", w.id, resp.RSSMB)
			}
		}(wp)
	}
	return wp, nil
}

func killProcessTree(cmd *exec.Cmd) {
	if cmd == nil || cmd.Process == nil {
		return
	}
	pid := cmd.Process.Pid
	// Prefer process group kill so chromium children die with the worker
	if runtime.GOOS != "windows" {
		_ = syscall.Kill(-pid, syscall.SIGTERM)
	}
	_ = cmd.Process.Signal(syscall.SIGTERM)
	done := make(chan struct{})
	go func() {
		_, _ = cmd.Process.Wait()
		close(done)
	}()
	select {
	case <-done:
		return
	case <-time.After(3 * time.Second):
	}
	if runtime.GOOS != "windows" {
		_ = syscall.Kill(-pid, syscall.SIGKILL)
	}
	_ = cmd.Process.Kill()
	<-done
}

func (g *Gateway) stopWorker(wp *workerProc) {
	if wp == nil {
		return
	}
	wp.mu.Lock()
	defer wp.mu.Unlock()
	if !wp.alive {
		return
	}
	// ask graceful recycle first
	if wp.stdin != nil {
		_ = json.NewEncoder(wp.stdin).Encode(workerReq{Cmd: "shutdown"})
		_ = wp.stdin.Close()
	}
	done := make(chan struct{})
	go func() {
		if wp.cmd != nil {
			_ = wp.cmd.Wait()
		}
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(4 * time.Second):
		killProcessTree(wp.cmd)
		<-done
	}
	wp.alive = false
	wp.busy.Store(false)
	g.aliveWorkers.Add(-1)
	g.recycles.Add(1)
}

func (g *Gateway) ensureWorker(wp **workerProc, id int) error {
	if *wp != nil && (*wp).alive && (*wp).cmd.Process != nil {
		// check process still running
		if (*wp).cmd.ProcessState == nil {
			// probe by signal 0
			if err := (*wp).cmd.Process.Signal(syscall.Signal(0)); err == nil {
				return nil
			}
		}
	}
	if *wp != nil {
		g.stopWorker(*wp)
	}
	n, err := g.startWorker(id)
	if err != nil {
		return err
	}
	*wp = n
	return nil
}

func (g *Gateway) solveOnWorker(wp *workerProc, job Job) workerResp {
	wp.mu.Lock()
	defer wp.mu.Unlock()
	wp.busy.Store(true)
	defer wp.busy.Store(false)
	wp.lastUsed.Store(time.Now().UnixNano())
	req := workerReq{
		Cmd: "solve", ID: job.ID, URL: job.URL, Sitekey: job.Sitekey,
		Action: job.Action, CData: job.CData, Proxy: job.Proxy,
	}
	if err := json.NewEncoder(wp.stdin).Encode(req); err != nil {
		wp.fails++
		return workerResp{OK: false, ID: job.ID, Error: "worker write: " + err.Error()}
	}
	var resp workerResp
	// decoder blocks; rely on outer timeout via process recycle
	if err := wp.stdout.Decode(&resp); err != nil {
		wp.alive = false
		wp.fails++
		return workerResp{OK: false, ID: job.ID, Error: "worker read: " + err.Error()}
	}
	wp.solves++
	if !resp.OK {
		wp.fails++
	}
	return resp
}

// adaptiveTimeout shortens under high queue pressure, lengthens when idle.
func (g *Gateway) adaptiveTimeout() time.Duration {
	base := g.solveTimeout
	q := len(g.queue)
	capQ := cap(g.queue)
	if capQ <= 0 {
		return base
	}
	// High queue → slightly shorter timeout to fail fast and free workers
	if q*100/capQ >= 70 {
		t := time.Duration(float64(base) * 0.75)
		if t < 25*time.Second {
			t = 25 * time.Second
		}
		return t
	}
	return base
}

func (g *Gateway) workerLoop(id int) {
	defer g.wg.Done()
	var wp *workerProc
	defer func() { g.stopWorker(wp) }()

	// consecutive failures → longer backoff to avoid thrashing chromium
	failStreak := 0

	for {
		select {
		case <-g.ctx.Done():
			return
		case job, ok := <-g.queue:
			if !ok {
				return
			}
			if err := g.ensureWorker(&wp, id); err != nil {
				g.failed.Add(1)
				g.pending.Add(-1)
				g.putResult(&Result{ID: job.ID, Status: "error", Error: err.Error(), Worker: id})
				failStreak++
				if failStreak > 3 {
					time.Sleep(time.Duration(min(failStreak, 8)) * 400 * time.Millisecond)
				}
				continue
			}

			// optional pre-check RSS / solve budget
			if wp.cmd.Process != nil {
				rss := g.processRSS(wp.cmd.Process.Pid)
				wp.lastRSS = rss
				p := g.hostPressure()
				// recycle earlier under high pressure even below soft limit
				force := g.maxSolves > 0 && wp.solves >= g.maxSolves
				force = force || (wp.fails >= 3 && wp.solves > 0)
				if g.shouldRecycle(rss, p.Pressure) || force {
					fmt.Fprintf(os.Stderr, "[gateway] recycle worker %d rss_kb=%d pressure=%d solves=%d fails=%d\n",
						id, rss, p.Pressure, wp.solves, wp.fails)
					g.stopWorker(wp)
					wp = nil
					if err := g.ensureWorker(&wp, id); err != nil {
						g.failed.Add(1)
						g.pending.Add(-1)
						g.putResult(&Result{ID: job.ID, Status: "error", Error: err.Error(), Worker: id})
						continue
					}
				}
			}

			start := time.Now()
			type out struct{ r workerResp }
			ch := make(chan out, 1)
			go func() {
				ch <- out{r: g.solveOnWorker(wp, job)}
			}()
			var resp workerResp
			timeout := g.adaptiveTimeout()
			select {
			case o := <-ch:
				resp = o.r
			case <-time.After(timeout):
				resp = workerResp{OK: false, ID: job.ID, Error: "solve timeout"}
				g.stopWorker(wp)
				wp = nil
			case <-g.ctx.Done():
				return
			}

			elapsed := time.Since(start).Seconds()
			g.pending.Add(-1)
			if resp.OK && resp.Value != "" && g.tokenOK(resp.Value) {
				failStreak = 0
				g.solved.Add(1)
				g.solveSumMs.Add(int64(elapsed * 1000))
				g.solveCount.Add(1)
				g.putResult(&Result{
					ID: job.ID, Status: "success", Value: resp.Value,
					ElapsedSec: elapsed, Worker: id, Recycled: resp.Recycled,
				})
			} else {
				failStreak++
				g.failed.Add(1)
				errMsg := resp.Error
				if errMsg == "" {
					errMsg = "CAPTCHA_FAIL"
				}
				g.putResult(&Result{
					ID: job.ID, Status: "fail", Value: "CAPTCHA_FAIL",
					Error: errMsg, ElapsedSec: elapsed, Worker: id,
				})
				// recycle worker after failure to free browser memory
				if wp != nil {
					g.stopWorker(wp)
					wp = nil
				}
				// brief backoff under consecutive fails (scheduler anti-thrash)
				if failStreak >= 2 {
					time.Sleep(time.Duration(min(failStreak, 5)) * 250 * time.Millisecond)
				}
			}
			if resp.Recycled && wp != nil {
				g.stopWorker(wp)
				wp = nil
			}
		}
	}
}

func (g *Gateway) start() error {
	g.workerProcs = make([]*workerProc, g.workers)
	// multi-core: launch worker loops in parallel (each is a goroutine)
	var launch sync.WaitGroup
	for i := 0; i < g.workers; i++ {
		launch.Add(1)
		g.wg.Add(1)
		go func(id int) {
			defer launch.Done()
			// staggered start to avoid thundering herd on chromium download/launch
			if id > 1 {
				time.Sleep(time.Duration(id-1) * 150 * time.Millisecond)
			}
			g.workerLoop(id)
		}(i + 1)
	}
	// wait briefly so first workers bind stdin (non-blocking overall)
	go func() {
		launch.Wait()
	}()
	// background purge + pressure-aware adaptive notes
	g.wg.Add(1)
	go func() {
		defer g.wg.Done()
		t := time.NewTicker(20 * time.Second)
		defer t.Stop()
		for {
			select {
			case <-g.ctx.Done():
				return
			case <-t.C:
				g.purgeExpired()
				p := g.hostPressure()
				if p.Pressure >= 92 {
					fmt.Fprintf(os.Stderr,
						"[gateway] high pressure=%d avail_mb=%.0f queue=%d pending=%d — workers will recycle aggressively\n",
						p.Pressure, float64(p.AvailableKB)/1024.0, len(g.queue), g.pending.Load())
				}
			}
		}
	}()
	return nil
}

func (g *Gateway) stop() {
	g.cancel()
	// Drain is non-blocking: close queue after cancel so loops exit
	func() {
		defer func() { _ = recover() }()
		close(g.queue)
	}()
	// Hard deadline so main register exit is not blocked by hung chromium
	done := make(chan struct{})
	go func() {
		g.wg.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(12 * time.Second):
		fmt.Fprintln(os.Stderr, "[gateway] stop: force-killing remaining workers")
		for _, wp := range g.workerProcs {
			if wp != nil && wp.cmd != nil {
				killProcessTree(wp.cmd)
			}
		}
		<-done
	}
}

func (g *Gateway) enqueue(job Job) {
	g.pending.Add(1)
	g.putResult(&Result{ID: job.ID, Status: "pending"})
	select {
	case g.queue <- job:
	default:
		// queue full — still try with timeout
		select {
		case g.queue <- job:
		case <-time.After(2 * time.Second):
			g.pending.Add(-1)
			g.putResult(&Result{ID: job.ID, Status: "error", Error: "queue full"})
		}
	}
}

func (g *Gateway) stats() Stats {
	p := g.hostPressure()
	alive := int(g.aliveWorkers.Load())
	if alive < 0 {
		alive = 0
	}
	sc := g.solveCount.Load()
	avg := 0.0
	if sc > 0 {
		avg = float64(g.solveSumMs.Load()) / float64(sc) / 1000.0
	}
	conc := max(1, g.concurrency)
	return Stats{
		Engine:          "hybrid",
		Version:         version,
		QueueDepth:      len(g.queue),
		Pending:         g.pending.Load(),
		Solved:          g.solved.Load(),
		Failed:          g.failed.Load(),
		Recycles:        g.recycles.Load(),
		Workers:         g.workers,
		WorkerAlive:     alive,
		Concurrency:     conc,
		EffectiveSlots:  g.workers * conc,
		CPUCores:        runtime.NumCPU(),
		GOMAXPROCS:      runtime.GOMAXPROCS(0),
		AvgSolveSec:     avg,
		UptimeSec:       now() - float64(g.started.Unix()),
		HostPressure:    p.Pressure,
		HostAvailableMB: float64(p.AvailableKB) / 1024.0,
		PrefetchOK:      int(g.prefetchOK.Load()),
	}
}

// autoWorkers picks browser process count from CPU cores + free RAM.
// Memory-first: each worker ≈ softMB RSS budget.
func autoWorkers(softMB int) int {
	cores := runtime.NumCPU()
	if cores < 1 {
		cores = 1
	}
	// default soft budget
	if softMB <= 0 {
		softMB = 700
	}
	// read free MB
	availMB := 0
	if data, err := os.ReadFile("/proc/meminfo"); err == nil {
		var availKB uint64
		for _, line := range strings.Split(string(data), "\n") {
			var key string
			var val uint64
			if _, e := fmt.Sscanf(line, "%s %d", &key, &val); e != nil {
				continue
			}
			if key == "MemAvailable:" {
				availKB = val
				break
			}
		}
		availMB = int(availKB / 1024)
	}
	// reserve ~1.2GB for register/dashboard/OS
	budget := availMB - 1200
	if budget < softMB {
		return 1
	}
	byMem := budget / softMB
	if byMem < 1 {
		byMem = 1
	}
	// CPU: leave 1 core for gateway/OS; cap at cores-1
	byCPU := cores - 1
	if byCPU < 1 {
		byCPU = 1
	}
	// hard cap to avoid OOM storms
	capN := envInt("SOLVER_GATEWAY_WORKERS_MAX", 8)
	if capN < 1 {
		capN = 8
	}
	n := byMem
	if byCPU < n {
		n = byCPU
	}
	if n > capN {
		n = capN
	}
	if n < 1 {
		n = 1
	}
	return n
}

func autoConcurrency(workers int) int {
	// async pages per browser; more concurrency = higher throughput but more RAM
	cores := runtime.NumCPU()
	// prefer 2 on multi-core when few workers, else 1
	if workers <= 2 && cores >= 4 {
		return envInt("SOLVER_WORKER_CONCURRENCY", 2)
	}
	return envInt("SOLVER_WORKER_CONCURRENCY", 1)
}

func parseWorkersFlag(raw string, softMB int) int {
	raw = strings.TrimSpace(strings.ToLower(raw))
	if raw == "" || raw == "auto" || raw == "0" {
		return autoWorkers(softMB)
	}
	n, err := strconv.Atoi(raw)
	if err != nil || n <= 0 {
		return autoWorkers(softMB)
	}
	return n
}

// --- HTTP ---


// Optional shared-secret auth for public HF / internet exposure.
// Set SOLVER_API_TOKEN or TURNSTILE_SOLVER_TOKEN; clients send:
//   Authorization: Bearer <token>  or  X-API-Key: <token>  or  ?token=
func apiToken() string {
	return strings.TrimSpace(env("SOLVER_API_TOKEN", env("TURNSTILE_SOLVER_TOKEN", "")))
}

func authorized(r *http.Request) bool {
	want := apiToken()
	if want == "" {
		return true
	}
	if h := r.Header.Get("Authorization"); strings.HasPrefix(strings.ToLower(h), "bearer ") {
		if strings.TrimSpace(h[7:]) == want {
			return true
		}
	}
	if r.Header.Get("X-API-Key") == want || r.Header.Get("X-Solver-Token") == want {
		return true
	}
	if r.URL.Query().Get("token") == want {
		return true
	}
	return false
}

func withAuth(next http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// health always public for HF readiness probes
		if r.URL.Path == "/health" || r.URL.Path == "/api/health" || r.URL.Path == "/" {
			next(w, r)
			return
		}
		if !authorized(r) {
			writeJSON(w, 401, map[string]any{"ok": false, "error": "unauthorized"})
			return
		}
		next(w, r)
	}
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func (g *Gateway) handleTurnstile(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	url := strings.TrimSpace(q.Get("url"))
	sitekey := strings.TrimSpace(q.Get("sitekey"))
	if url == "" || sitekey == "" {
		// also accept JSON body
		if r.Method == http.MethodPost {
			var body struct {
				URL     string `json:"url"`
				Sitekey string `json:"sitekey"`
				Action  string `json:"action"`
				CData   string `json:"cdata"`
				Proxy   string `json:"proxy"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			url = body.URL
			sitekey = body.Sitekey
			if q.Get("action") == "" {
				q.Set("action", body.Action)
			}
			if q.Get("cdata") == "" {
				q.Set("cdata", body.CData)
			}
			if q.Get("proxy") == "" {
				q.Set("proxy", body.Proxy)
			}
		}
	}
	if url == "" || sitekey == "" {
		writeJSON(w, 400, map[string]any{"error": "url and sitekey required"})
		return
	}
	job := Job{
		ID: newID(), URL: url, Sitekey: sitekey,
		Action: strings.TrimSpace(q.Get("action")),
		CData:  strings.TrimSpace(q.Get("cdata")),
		Proxy:  strings.TrimSpace(q.Get("proxy")),
		CreatedAt: now(),
	}
	g.enqueue(job)
	// d3vin/theyka return task id
	writeJSON(w, 200, map[string]any{"task_id": job.ID, "id": job.ID})
}

func (g *Gateway) handleResult(w http.ResponseWriter, r *http.Request) {
	id := strings.TrimSpace(r.URL.Query().Get("id"))
	if id == "" {
		id = strings.TrimSpace(r.URL.Query().Get("task_id"))
	}
	if id == "" {
		writeJSON(w, 400, map[string]any{"error": "id required"})
		return
	}
	res := g.getResult(id)
	if res == nil {
		writeJSON(w, 200, map[string]any{"status": "error", "value": "CAPTCHA_NOT_READY", "error": "not found"})
		return
	}
	// Compatible shapes
	switch res.Status {
	case "pending":
		writeJSON(w, 200, map[string]any{"status": "process", "value": "CAPTCHA_NOT_READY", "elapsed_time": 0})
	case "success":
		writeJSON(w, 200, map[string]any{
			"status": "success", "value": res.Value, "elapsed_time": res.ElapsedSec,
		})
	default:
		writeJSON(w, 200, map[string]any{
			"status": "fail", "value": res.Value, "error": res.Error, "elapsed_time": res.ElapsedSec,
		})
	}
}

func (g *Gateway) handleHealth(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, map[string]any{"ok": true, "engine": "hybrid", "version": version})
}

func (g *Gateway) handleStats(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, 200, g.stats())
}

func (g *Gateway) handleMemory(w http.ResponseWriter, r *http.Request) {
	p := g.hostPressure()
	writeJSON(w, 200, map[string]any{
		"host": p,
		"soft_mb": g.softMB,
		"hard_mb": g.hardMB,
		"recycles": g.recycles.Load(),
		"util_bin": g.utilBin,
		"util_ok": g.utilOK || g.utilBin != "",
	})
}

func (g *Gateway) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	writeJSON(w, 200, map[string]any{
		"service": "solver-gateway",
		"engine":  "hybrid",
		"version": version,
		"endpoints": []string{"/turnstile", "/result", "/health", "/stats", "/v1/memory"},
	})
}

func findBin(name string, candidates ...string) string {
	for _, c := range candidates {
		if c == "" {
			continue
		}
		if st, err := os.Stat(c); err == nil && !st.IsDir() {
			return c
		}
	}
	if p, err := exec.LookPath(name); err == nil {
		return p
	}
	return ""
}

func main() {
	if len(os.Args) > 1 {
		switch os.Args[1] {
		case "version", "-v", "--version":
			fmt.Println("solver-gateway", version)
			return
		}
	}

	// Use all CPU cores for Go scheduler (HTTP + queue + IPC)
	runtime.GOMAXPROCS(runtime.NumCPU())

	host := flag.String("host", env("SOLVER_GATEWAY_HOST", env("HOST", "0.0.0.0")), "bind host")
	port := flag.Int("port", envInt("PORT", envInt("SOLVER_GATEWAY_PORT", 7860)), "bind port")
	// workers: "auto" or integer; default auto uses multi-core + free RAM
	// auto: size workers by CPU+free RAM; request bursts go into queue
	workersRaw := flag.String("workers", env("SOLVER_GATEWAY_WORKERS", "auto"), "browser workers (auto|N)")
	concurrency := flag.Int("concurrency", envInt("SOLVER_WORKER_CONCURRENCY", 0), "async pages per worker (0=auto)")
	timeout := flag.Int("timeout", envInt("SOLVER_GATEWAY_TIMEOUT", 90), "solve timeout sec")
	queueSize := flag.Int("queue", envInt("SOLVER_GATEWAY_QUEUE", 0), "job queue size (0=auto by request load)")
	soft := flag.Int("soft-mb", envInt("SOLVER_WATCHDOG_SOFT_MB", 700), "worker soft RSS MB")
	hard := flag.Int("hard-mb", envInt("SOLVER_WATCHDOG_HARD_MB", 1100), "worker hard RSS MB")
	maxSolves := flag.Int("max-solves", envInt("SOLVER_WORKER_MAX_SOLVES", 8), "recycle after N solves")
	browser := flag.String("browser", env("TURNSTILE_SOLVER_BROWSER", "chromium"), "browser type")
	headless := flag.Bool("headless", envBool("TURNSTILE_SOLVER_HEADLESS", true), "headless")
	prefetch := flag.Bool("prefetch", envBool("SOLVER_WORKER_PREFETCH", true), "async warm browser+script")
	workDir := flag.String("work-dir", env("SOLVER_GATEWAY_WORK_DIR", ""), "worker cwd")
	flag.Parse()

	root := env("PROJECT_ROOT", "")
	if root == "" {
		// assume binary lives in native/solver-gateway/
		exe, _ := os.Executable()
		root = filepath.Clean(filepath.Join(filepath.Dir(exe), ".."))
	}

	wd := *workDir
	if wd == "" {
		wd = filepath.Join(root, "logs")
	}
	_ = os.MkdirAll(wd, 0o755)

	workerScript := env("SOLVER_WORKER_SCRIPT", filepath.Join(root, "worker/browser_worker.py"))
	if _, err := os.Stat(workerScript); err != nil {
		// fallback relative
		workerScript = filepath.Join(filepath.Dir(exePath()), "../worker/browser_worker.py")
	}

	utilBin := findBin("solver-util",
		env("SOLVER_UTIL_BIN", ""),
		filepath.Join(root, "util/solver-util"),
	)
	watchdogBin := findBin("solver-watchdog",
		env("SOLVER_WATCHDOG_BIN", ""),
		filepath.Join(root, "watchdog/solver-watchdog"),
		filepath.Join(root, "watchdog/target/release/solver-watchdog"),
	)

	nWorkers := parseWorkersFlag(*workersRaw, *soft)
	nConc := *concurrency
	if nConc <= 0 {
		nConc = autoConcurrency(nWorkers)
	}
	if nConc < 1 {
		nConc = 1
	}
	qSize := *queueSize
	if qSize <= 0 {
		// deep queue so multi-worker async load never blocks register
		qSize = max(64, nWorkers*nConc*16)
	}

	ctx, cancel := context.WithCancel(context.Background())
	g := &Gateway{
		results:      make(map[string]*Result),
		queue:        make(chan Job, qSize),
		workers:      max(1, nWorkers),
		concurrency:  nConc,
		solveTimeout: time.Duration(*timeout) * time.Second,
		resultTTL:    15 * time.Minute,
		started:      time.Now(),
		pythonBin:    env("SOLVER_PYTHON", env("PYTHON", "python3")),
		workerScript: workerScript,
		workDir:      wd,
		utilBin:      utilBin,
		watchdogBin:  watchdogBin,
		softMB:       *soft,
		hardMB:       *hard,
		maxSolves:    *maxSolves,
		browserType:  *browser,
		headless:     *headless,
		proxyFile:    env("TURNSTILE_SOLVER_PROXY_FILE", ""),
		prefetch:     *prefetch,
		ctx:          ctx,
		cancel:       cancel,
	}
	fmt.Fprintf(os.Stderr,
		"[gateway] multi-core plan: cpus=%d gomaxprocs=%d workers=%d concurrency=%d slots=%d queue=%d soft=%dMB\n",
		runtime.NumCPU(), runtime.GOMAXPROCS(0), g.workers, g.concurrency,
		g.workers*g.concurrency, qSize, g.softMB,
	)

	if err := g.start(); err != nil {
		fmt.Fprintln(os.Stderr, "start failed:", err)
		os.Exit(1)
	}

	// optional external watchdog on gateway pid
	if g.watchdogBin != "" && envBool("SOLVER_WATCHDOG_ATTACH", true) {
		pidFile := filepath.Join(wd, "gateway.pid")
		_ = os.WriteFile(pidFile, []byte(strconv.Itoa(os.Getpid())), 0o644)
		statusFile := filepath.Join(wd, "watchdog-status.json")
		cmd := exec.Command(g.watchdogBin, "watch",
			"--pid", strconv.Itoa(os.Getpid()),
			"--soft-mb", strconv.Itoa(g.softMB*g.workers+400),
			"--hard-mb", strconv.Itoa(g.hardMB*g.workers+800),
			"--interval-sec", env("SOLVER_WATCHDOG_INTERVAL_SEC", "8"),
			"--grace-sec", "10",
			"--status-file", statusFile,
			"--dry-run", // do not kill gateway itself; workers self-recycle
		)
		cmd.Stdout = os.Stderr
		cmd.Stderr = os.Stderr
		if err := cmd.Start(); err == nil {
			fmt.Fprintf(os.Stderr, "[gateway] watchdog attached pid=%d\n", cmd.Process.Pid)
			go func() { _ = cmd.Wait() }()
		}
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/", g.handleIndex)
	mux.HandleFunc("/turnstile", withAuth(g.handleTurnstile))
	mux.HandleFunc("/result", withAuth(g.handleResult))
	mux.HandleFunc("/health", withAuth(g.handleHealth))
	mux.HandleFunc("/stats", withAuth(g.handleStats))
	mux.HandleFunc("/v1/memory", withAuth(g.handleMemory))
	mux.HandleFunc("/v1/solve", withAuth(g.handleTurnstile))
	mux.HandleFunc("/api/health", withAuth(g.handleHealth))

	addr := net.JoinHostPort(*host, strconv.Itoa(*port))
	srv := &http.Server{Addr: addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}

	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, syscall.SIGINT, syscall.SIGTERM)
		<-ch
		fmt.Fprintln(os.Stderr, "[gateway] shutting down...")
		_ = srv.Shutdown(context.Background())
		g.stop()
	}()

	fmt.Fprintf(os.Stderr,
		"[gateway] hybrid turnstile listening on http://%s workers=%d conc=%d slots=%d util=%q watchdog=%q\n",
		addr, g.workers, g.concurrency, g.workers*g.concurrency, g.utilBin, g.watchdogBin)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func exePath() string {
	p, err := os.Executable()
	if err != nil {
		return "."
	}
	return p
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

