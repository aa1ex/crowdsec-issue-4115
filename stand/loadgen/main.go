// Open-loop POST /v1/alerts load for crowdsec LAPI. Arrivals are driven by a
// fixed-rate ticker and do NOT wait for completions, so the generator does
// not self-throttle when the SUT slows down (slowdowns grow the in-flight
// queue rather than silently throttling offered load).
//
// Each logical request makes up to 1+retries HTTP attempts with a per-attempt
// context deadline. Alerts use Source.Scope=Ip with no embedded Decisions,
// which is what triggers the per-alert allowlist SELECT we are measuring.
//
// Outputs: cumulative counter snapshots in metrics.jsonl (1s cadence) plus
// per-attempt latency rows in latency.jsonl when -latency is set.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"math/rand"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

var (
	target    = flag.String("target", "http://localhost:8080", "LAPI base URL")
	machineID = flag.String("machine", "loadgen", "machine id for JWT login")
	password  = flag.String("password", "loadgenpass", "password for JWT login")
	rate      = flag.Float64("rate", 50, "OFFERED requests/sec (open-loop, not self-throttling)")
	duration  = flag.Duration("duration", 60*time.Second, "how long to offer load")
	deadline  = flag.Duration("deadline", 1*time.Second, "per-request upstream deadline (ctx timeout)")
	retries   = flag.Int("retries", 1, "extra retry attempts on err/timeout/5xx")
	backoff   = flag.Duration("backoff", 100*time.Millisecond, "base backoff between attempts")
	batchSize = flag.Int("batch", 100, "alerts per request body")
	metricsP  = flag.String("metrics", "metrics.jsonl", "path for 1s cumulative metric buckets")
	latencyP  = flag.String("latency", "", "if non-empty, path for per-attempt latency JSONL (one line per HTTP attempt)")
	ceiling   = flag.Int64("inflight-ceiling", 60000, "safety: if in-flight exceeds this, mark run degenerate and stop offering")
)

// One per HTTP attempt. terminal=true means this attempt was the last one
// for the enclosing logical request (either it returned 2xx, or retries ran
// out). outcome ∈ {"ok","timeout","err","non2xx"}.
type latRec struct {
	TOfferedNs int64  `json:"t_offered_ns"`
	TStartNs   int64  `json:"t_start_ns"`
	TEndNs     int64  `json:"t_end_ns"`
	Attempt    int    `json:"attempt"`
	HTTPStatus int    `json:"http_status"` // -1 for transport err / timeout
	Outcome    string `json:"outcome"`
	Terminal   bool   `json:"terminal"`
}

var latCh chan latRec // nil if -latency not set

type loginResp struct {
	Token string `json:"token"`
}
type alertSource struct {
	Scope string `json:"scope"`
	Value string `json:"value"`
	IP    string `json:"ip"`
}
type metaItem struct {
	Key   string `json:"key"`
	Value string `json:"value"`
}
type alertEvent struct {
	Timestamp string     `json:"timestamp"`
	Meta      []metaItem `json:"meta"`
}
type alert struct {
	Scenario        string       `json:"scenario"`
	ScenarioHash    string       `json:"scenario_hash"`
	ScenarioVersion string       `json:"scenario_version"`
	Message         string       `json:"message"`
	EventsCount     int32        `json:"events_count"`
	Events          []alertEvent `json:"events"`
	// Capacity and Simulated are required by crowdsec's OpenAPI validator
	// (POST /v1/alerts rejects with HTTP 500 "field is required" otherwise),
	// even though our synthetic alerts always carry the zero value.
	Capacity  int32 `json:"capacity"`
	Leakspeed string `json:"leakspeed"`
	Simulated bool   `json:"simulated"`
	CreatedAt string `json:"created_at"`
	StartAt   string `json:"start_at"`
	StopAt    string `json:"stop_at"`
	Source    alertSource `json:"source"`
}

var (
	cOffered  atomic.Int64 // logical requests started
	cAttempts atomic.Int64 // HTTP attempts incl. retries
	cGoodput  atomic.Int64 // logical requests that got a 2xx
	cRetries  atomic.Int64 // retry attempts (attempts beyond the first, per logical req)
	cTimeouts atomic.Int64 // attempts ending in deadline/timeout
	cErrors   atomic.Int64 // attempts ending in transport error (non-timeout)
	c5xx      atomic.Int64
	inflight  atomic.Int64
	degenerate atomic.Bool

	tokenMu sync.RWMutex
	token   string
)

func login(c *http.Client) (string, error) {
	body, _ := json.Marshal(map[string]string{"machine_id": *machineID, "password": *password})
	resp, err := c.Post(*target+"/v1/watchers/login", "application/json", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		b, _ := io.ReadAll(resp.Body)
		return "", fmt.Errorf("login HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(b)))
	}
	var r loginResp
	if err := json.NewDecoder(resp.Body).Decode(&r); err != nil {
		return "", err
	}
	if r.Token == "" {
		return "", fmt.Errorf("empty token")
	}
	return r.Token, nil
}

func getToken() string {
	tokenMu.RLock()
	defer tokenMu.RUnlock()
	return token
}

func relogin(c *http.Client) {
	tokenMu.Lock()
	defer tokenMu.Unlock()
	if t, err := login(c); err == nil {
		token = t
	}
}

func makeBatch(rng *rand.Rand) []alert {
	now := time.Now().UTC().Format(time.RFC3339)
	out := make([]alert, *batchSize)
	for i := range out {
		ip := fmt.Sprintf("203.0.113.%d", rng.Intn(254)+1) // TEST-NET-3
		out[i] = alert{
			Scenario:        "synthetic/burst",
			ScenarioHash:    "0000000000000000000000000000000000000000000000000000000000000000",
			ScenarioVersion: "1",
			Message:         "synthetic alert",
			EventsCount:     1,
			Events:          []alertEvent{{Timestamp: now, Meta: []metaItem{{Key: "source", Value: "synthetic"}}}},
			Leakspeed:       "0s",
			CreatedAt:       now,
			StartAt:         now,
			StopAt:          now,
			Source:          alertSource{Scope: "Ip", Value: ip, IP: ip},
		}
	}
	return out
}

// one logical request: up to 1+retries attempts, independent of arrivals.
func doLogical(c *http.Client, rng *rand.Rand) {
	inflight.Add(1)
	defer inflight.Add(-1)
	cOffered.Add(1)
	body, _ := json.Marshal(makeBatch(rng))
	tOffered := time.Now().UnixNano()

	maxAttempts := *retries + 1
	for attempt := 0; attempt < maxAttempts; attempt++ {
		if attempt > 0 {
			cRetries.Add(1)
			time.Sleep(*backoff + time.Duration(rng.Int63n(int64(*backoff)+1)))
		}
		cAttempts.Add(1)
		ctx, cancel := context.WithTimeout(context.Background(), *deadline)
		tStart := time.Now().UnixNano()
		req, _ := http.NewRequestWithContext(ctx, "POST", *target+"/v1/alerts", bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+getToken())
		req.Header.Set("Content-Type", "application/json")
		resp, err := c.Do(req)
		tEnd := time.Now().UnixNano()
		if err != nil {
			cancel()
			outcome := "err"
			if ctx.Err() == context.DeadlineExceeded || strings.Contains(err.Error(), "deadline exceeded") || strings.Contains(err.Error(), "Client.Timeout") {
				cTimeouts.Add(1)
				outcome = "timeout"
			} else {
				cErrors.Add(1)
			}
			isTerminal := attempt == maxAttempts-1
			emitLat(tOffered, tStart, tEnd, attempt, -1, outcome, isTerminal)
			continue
		}
		sc := resp.StatusCode
		io.Copy(io.Discard, resp.Body)
		resp.Body.Close()
		cancel()
		if sc >= 200 && sc < 300 {
			cGoodput.Add(1)
			emitLat(tOffered, tStart, tEnd, attempt, sc, "ok", true)
			return
		}
		if sc == 401 {
			relogin(c)
		}
		if sc >= 500 {
			c5xx.Add(1)
		}
		isTerminal := attempt == maxAttempts-1
		emitLat(tOffered, tStart, tEnd, attempt, sc, "non2xx", isTerminal)
	}
}

func emitLat(tOffered, tStart, tEnd int64, attempt, status int, outcome string, terminal bool) {
	if latCh == nil {
		return
	}
	// Non-blocking-ish: if the buffer is wedged we'd rather drop a sample
	// than back-pressure the loadgen and skew the experiment. A drop counter
	// could be added later if we ever observe loss.
	select {
	case latCh <- latRec{TOfferedNs: tOffered, TStartNs: tStart, TEndNs: tEnd, Attempt: attempt, HTTPStatus: status, Outcome: outcome, Terminal: terminal}:
	default:
	}
}

func main() {
	flag.Parse()

	client := &http.Client{
		Transport: &http.Transport{
			MaxIdleConns:        2000,
			MaxIdleConnsPerHost: 2000,
			IdleConnTimeout:     90 * time.Second,
		},
		// No client.Timeout: per-request deadline is enforced via context so
		// slow attempts manifest as ctx cancellation rather than transport close.
	}

	log.Printf("login %s as %q", *target, *machineID)
	t, err := login(client)
	if err != nil {
		log.Fatalf("login failed: %v", err)
	}
	token = t
	log.Printf("token ok (%d chars)", len(t))

	mf, err := os.Create(*metricsP)
	if err != nil {
		log.Fatalf("metrics file: %v", err)
	}
	defer mf.Close()

	var latWG sync.WaitGroup
	var lf *os.File
	if *latencyP != "" {
		lf, err = os.Create(*latencyP)
		if err != nil {
			log.Fatalf("latency file: %v", err)
		}
		// Buffer holds several seconds of samples at the typical 10-100 rps;
		// drops are preferred over back-pressure (see emitLat).
		latCh = make(chan latRec, 4096)
		latWG.Add(1)
		go func() {
			defer latWG.Done()
			enc := json.NewEncoder(lf)
			for r := range latCh {
				enc.Encode(r)
			}
		}()
	}

	var wg sync.WaitGroup
	stop := make(chan struct{})

	// 1s cumulative sampler
	var sampWG sync.WaitGroup
	sampWG.Add(1)
	go func() {
		defer sampWG.Done()
		t0 := time.Now()
		tick := time.NewTicker(1 * time.Second)
		defer tick.Stop()
		enc := json.NewEncoder(mf)
		write := func() {
			enc.Encode(map[string]any{
				"t":          time.Since(t0).Seconds(),
				"offered":    cOffered.Load(),
				"attempts":   cAttempts.Load(),
				"goodput":    cGoodput.Load(),
				"retries":    cRetries.Load(),
				"timeouts":   cTimeouts.Load(),
				"errors":     cErrors.Load(),
				"http5xx":    c5xx.Load(),
				"inflight":   inflight.Load(),
				"degenerate": degenerate.Load(),
			})
			mf.Sync()
		}
		for {
			select {
			case <-tick.C:
				write()
			case <-stop:
				write()
				return
			}
		}
	}()

	// open-loop arrival ticker
	interval := time.Duration(float64(time.Second) / *rate)
	if interval <= 0 {
		interval = time.Microsecond
	}
	at := time.NewTicker(interval)
	defer at.Stop()
	deadlineT := time.After(*duration)
	rngSeed := atomic.Int64{}

arrivals:
	for {
		select {
		case <-deadlineT:
			break arrivals
		case <-at.C:
			if inflight.Load() > *ceiling {
				if !degenerate.Swap(true) {
					log.Printf("DEGENERATE: in-flight %d > ceiling %d at %v — stop offering",
						inflight.Load(), *ceiling, time.Now().Format(time.RFC3339))
				}
				break arrivals
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				rng := rand.New(rand.NewSource(rngSeed.Add(1)))
				doLogical(client, rng)
			}()
		}
	}

	log.Printf("arrivals ended; draining in-flight (bounded by deadline*retries)...")
	wg.Wait()
	close(stop)
	sampWG.Wait()
	if latCh != nil {
		close(latCh)
		latWG.Wait()
		lf.Close()
	}

	fmt.Printf("\n=== open-loop result ===\n")
	fmt.Printf("offered:    %d  (target rate %.1f/s, duration %v)\n", cOffered.Load(), *rate, *duration)
	fmt.Printf("attempts:   %d  (retries %d)\n", cAttempts.Load(), cRetries.Load())
	fmt.Printf("goodput:    %d  (2xx logical)\n", cGoodput.Load())
	fmt.Printf("timeouts:   %d\n", cTimeouts.Load())
	fmt.Printf("errors:     %d\n", cErrors.Load())
	fmt.Printf("http5xx:    %d\n", c5xx.Load())
	fmt.Printf("degenerate: %v\n", degenerate.Load())
	fmt.Printf("metrics:    %s\n", *metricsP)
}
