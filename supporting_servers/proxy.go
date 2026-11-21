// The crawlers' fetch proxy. One endpoint:
//
//	GET /fetch?url=<target>
//
// With PROXY_UPSTREAM set (the head node, db01:8888) the request goes on to <upstream>/fetch?url=<target>.
// Without it (the cloud VPS, the last hop) the target is fetched, and only public internet addresses may be
// dialed. Redirects aren't followed: the scraper checks each hop itself. Status, headers and body are streamed
// back unchanged. Errors that are the proxy's own fault carry an X-Proxy-Error header, so the scraper retries
// the url later instead of dropping it. Single binary, standard library only.
//
// Setup: deploy/vps/install_proxy.sh (last hop) and deploy/db01/connect_vps_proxy.sh (head).
//
// Env vars:
//
//	PROXY_ADDR              listen address                   (default :8888)
//	PROXY_CONNECT_TIMEOUT   dial timeout to origin           (default 5s)
//	PROXY_IDLE_TIMEOUT      idle conn timeout                (default 90s)
//	PROXY_READ_HEADER_TO    header read timeout              (default 10s)
//	PROXY_MAX_IDLE_CONNS    pooled idle conns total          (default 4096)
//	PROXY_MAX_PER_HOST      pooled idle conns per host       (default 64)
//	PROXY_PASSWORD          shared-secret required on every  (default none)
//	                        inbound request as the
//	                        X-Proxy-Auth header. Also sent
//	                        upstream on /fetch chain hops.
//	PROXY_UPSTREAM          next-hop proxy URL for /fetch    (default none)
//	PROXY_TLS_CERT          PEM cert path; if set with KEY,  (default unset)
//	                        listener serves HTTPS instead of HTTP
//	PROXY_TLS_KEY           PEM key path (paired with CERT)  (default unset)
//	PROXY_TLS_INSECURE      if set, this proxy skips TLS     (default unset)
//	                        verification when dialing
//	                        PROXY_UPSTREAM (self-signed OK)
//	DEBUG                   if set, log each request + time  (default unset)
package main

import (
	"context"
	"crypto/subtle"
	"crypto/tls"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"
)

var hopByHop = map[string]struct{}{
	"Connection":          {},
	"Proxy-Connection":    {},
	"Keep-Alive":          {},
	"Proxy-Authenticate":  {},
	"Proxy-Authorization": {},
	"Te":                  {},
	"Trailer":             {},
	"Transfer-Encoding":   {},
	"Upgrade":             {},
}

func env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envDur(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil {
			return d
		}
	}
	return def
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}

type proxy struct {
	transport *http.Transport
	password  string   // shared secret; empty disables auth
	upstream  *url.URL // next-hop proxy for /fetch chaining; nil = terminal
	debug     bool     // if true, log each request and its duration
}

func newProxy() *proxy {
	connectTO := envDur("PROXY_CONNECT_TIMEOUT", 5*time.Second) // same as the scraper's own connect timeout
	idleTO := envDur("PROXY_IDLE_TIMEOUT", 90*time.Second)
	maxIdle := envInt("PROXY_MAX_IDLE_CONNS", 4096)
	maxPerHost := envInt("PROXY_MAX_PER_HOST", 64)

	var upstream *url.URL
	if u := os.Getenv("PROXY_UPSTREAM"); u != "" {
		parsed, err := url.Parse(u)
		if err != nil || parsed.Scheme == "" || parsed.Host == "" {
			log.Fatalf("invalid PROXY_UPSTREAM %q: %v", u, err)
		}
		upstream = parsed
	}

	d := &net.Dialer{Timeout: connectTO, KeepAlive: 30 * time.Second}
	if upstream != nil {
		// A powered-off upstream sends no RST and no ACKs: without this, requests on the pooled connection hang for
		// ~15 minutes of kernel retries. With it they fail in seconds, before the scraper's 10s timeout, as a marked 502.
		d.Control = func(_, _ string, c syscall.RawConn) error {
			var serr error
			if err := c.Control(func(fd uintptr) {
				serr = syscall.SetsockoptInt(int(fd), syscall.IPPROTO_TCP, tcpUserTimeout, 3000)
			}); err != nil {
				return err
			}
			return serr
		}
	} else {
		d.Control = refuseNonPublic // this proxy fetches the pages itself
	}

	tr := &http.Transport{
		Proxy:                 nil,
		DialContext:           d.DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          maxIdle,
		MaxIdleConnsPerHost:   maxPerHost,
		MaxConnsPerHost:       0,
		IdleConnTimeout:       idleTO,
		TLSHandshakeTimeout:   connectTO,
		ExpectContinueTimeout: 1 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
		DisableCompression:    true, // pass bytes through; let the scraper decode
	}
	if os.Getenv("PROXY_TLS_INSECURE") != "" {
		// Used when PROXY_UPSTREAM is https:// with a self-signed cert.
		// Encrypts the hop but does not authenticate the peer.
		tr.TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
	}

	password := strings.TrimSpace(os.Getenv("PROXY_PASSWORD"))

	return &proxy{
		transport: tr,
		password:  password,
		upstream:  upstream,
		debug:     os.Getenv("DEBUG") != "",
	}
}

func (p *proxy) checkAuth(r *http.Request) bool {
	if p.password == "" {
		return true
	}
	got := r.Header.Get("X-Proxy-Auth")
	return subtle.ConstantTimeCompare([]byte(got), []byte(p.password)) == 1
}

const tcpUserTimeout = 0x12 // TCP_USER_TIMEOUT on linux; package syscall doesn't name it

var errNotPublic = errors.New("not a public address")

// IPv4 ranges netip doesn't flag that still aren't the public internet: "this network", CGNAT (also Tailscale),
// IETF protocol assignments, benchmarking, reserved.
var nonPublic = []netip.Prefix{
	netip.MustParsePrefix("0.0.0.0/8"),
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("240.0.0.0/4"),
}

// refuseNonPublic runs on every connection the last-hop proxy opens. It sees the IP actually being dialed, after DNS,
// so a page or redirect can't reach our own network or the cloud metadata service (169.254.169.254), even with a
// name that resolved to a public address for the scraper (DNS rebinding).
func refuseNonPublic(_, address string, _ syscall.RawConn) error {
	ap, err := netip.ParseAddrPort(address)
	if err != nil {
		return err
	}
	ip := ap.Addr().Unmap() // ::ffff:127.0.0.1 is 127.0.0.1
	if !ip.IsGlobalUnicast() || ip.IsPrivate() {
		return errNotPublic
	}
	for _, p := range nonPublic {
		if p.Contains(ip) {
			return errNotPublic
		}
	}
	return nil
}

// proxyError answers with an error that is the proxy's fault, not the page's. The X-Proxy-Error header tells the
// scraper to retry the url later instead of dropping it; hops in front of this one pass it through unchanged.
func proxyError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("X-Proxy-Error", "1")
	http.Error(w, msg, code)
}

func (p *proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if p.debug {
		start := time.Now()
		summary := requestSummary(r)
		log.Printf("→ %s %s from %s", r.Method, summary, r.RemoteAddr)
		defer func() {
			log.Printf("← %s %s in %s", r.Method, summary, time.Since(start))
		}()
	}
	if !p.checkAuth(r) {
		proxyError(w, "proxy auth required", http.StatusUnauthorized)
		return
	}
	if r.URL.Path != "/fetch" {
		http.NotFound(w, r) // also CONNECT and absolute-form requests: this isn't a general-purpose proxy
		return
	}
	p.handleFetch(w, r)
}

// requestSummary is the debug log's one-line description of a request: the target url, unescaped, for /fetch.
func requestSummary(r *http.Request) string {
	if t := r.URL.Query().Get("url"); r.URL.Path == "/fetch" && t != "" {
		return "/fetch?url=" + t
	}
	return r.RequestURI
}

// fetchHeaders is the set of client request headers that get forwarded along
// each chain hop and ultimately to the origin. Hop-by-hop and proxy-control
// headers are deliberately excluded.
var fetchHeaders = []string{
	"User-Agent",
	"From",
	"Accept",
	"Accept-Language",
	"Accept-Encoding",
	"Cookie",
	"Referer",
	"If-None-Match",
	"If-Modified-Since",
}

// handleFetch implements the chained URL-forwarding endpoint:
//
//	GET /fetch?url=<absolute target URL>
//
// If p.upstream is set, the request is forwarded to <upstream>/fetch?url=<target>;
// otherwise the target is fetched directly. Origin status, headers, and body
// are streamed back to the caller. Each Go request runs in its own goroutine,
// so concurrency is unbounded by design — rely on the transport's pool limits.
func (p *proxy) handleFetch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet { // it's always sent on as a GET, so don't accept anything that has a body
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}
	target := r.URL.Query().Get("url")
	if target == "" {
		http.Error(w, "missing url query param", http.StatusBadRequest)
		return
	}
	parsedTarget, err := url.Parse(target)
	if err != nil || !parsedTarget.IsAbs() || (parsedTarget.Scheme != "http" && parsedTarget.Scheme != "https") {
		http.Error(w, "url must be an absolute http(s) URL", http.StatusBadRequest)
		return
	}

	var nextURL string
	if p.upstream != nil {
		u := *p.upstream
		u.Path = "/fetch"
		q := url.Values{}
		q.Set("url", target)
		u.RawQuery = q.Encode()
		nextURL = u.String()
	} else {
		nextURL = target
	}

	outReq, err := http.NewRequestWithContext(r.Context(), http.MethodGet, nextURL, nil)
	if err != nil {
		http.Error(w, "bad request: "+err.Error(), http.StatusBadRequest)
		return
	}
	for _, h := range fetchHeaders {
		if v := r.Header.Get(h); v != "" {
			outReq.Header.Set(h, v)
		}
	}
	if p.upstream != nil && p.password != "" {
		outReq.Header.Set("X-Proxy-Auth", p.password)
	}

	if p.debug {
		hop := "origin"
		if p.upstream != nil {
			hop = "upstream proxy"
		}
		log.Printf("⇒ /fetch forwarding to %s: %s", hop, nextURL)
	}

	roundStart := time.Now()
	resp, err := p.transport.RoundTrip(outReq)
	if err != nil {
		if errors.Is(err, errNotPublic) {
			http.Error(w, "not a public address", http.StatusForbidden)
			return
		}
		log.Printf("fetch upstream error: dialing %s (target=%s): %v", nextURL, target, err)
		if p.upstream != nil {
			proxyError(w, "upstream proxy unreachable", http.StatusBadGateway) // our next hop is down, not the site
		} else {
			http.Error(w, "bad gateway", http.StatusBadGateway)
		}
		return
	}
	defer resp.Body.Close()

	if p.debug {
		log.Printf("⇐ /fetch upstream %s -> %d in %s", nextURL, resp.StatusCode, time.Since(roundStart))
	}

	dst := w.Header()
	for k, vv := range resp.Header {
		if _, drop := hopByHop[http.CanonicalHeaderKey(k)]; drop {
			continue
		}
		clone := make([]string, len(vv))
		copy(clone, vv)
		dst[k] = clone
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func main() {
	addr := env("PROXY_ADDR", ":8888")
	readHeaderTO := envDur("PROXY_READ_HEADER_TO", 10*time.Second)

	p := newProxy()

	srv := &http.Server{
		Addr:              addr,
		Handler:           p,
		ReadHeaderTimeout: readHeaderTO,
		// No WriteTimeout: a response streams for as long as the origin sends it; the scraper's own timeouts end
		// a stalled one, and its disconnect cancels the request through every hop.
		IdleTimeout: envDur("PROXY_IDLE_TIMEOUT", 90*time.Second),
	}

	idleClosed := make(chan struct{})
	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
		<-sigCh
		log.Printf("shutdown signal received")
		ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cancel()
		if err := srv.Shutdown(ctx); err != nil {
			log.Printf("graceful shutdown error: %v", err)
		}
		close(idleClosed)
	}()

	certFile := os.Getenv("PROXY_TLS_CERT")
	keyFile := os.Getenv("PROXY_TLS_KEY")
	tlsEnabled := certFile != "" && keyFile != ""

	scheme := "http"
	if tlsEnabled {
		scheme = "https"
	}
	log.Printf("forward proxy listening on %s://%s", scheme, addr)

	var listenErr error
	if tlsEnabled {
		listenErr = srv.ListenAndServeTLS(certFile, keyFile)
	} else {
		listenErr = srv.ListenAndServe()
	}
	if listenErr != nil && !errors.Is(listenErr, http.ErrServerClosed) {
		log.Fatalf("listen error: %v", listenErr)
	}
	<-idleClosed
	log.Printf("stopped")
}
