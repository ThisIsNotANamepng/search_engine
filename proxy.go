// High-performance HTTP/HTTPS forward proxy.
//
//   - HTTP: forwards the absolute-URI request to the origin via the shared
//     http.Transport (connection pooling, HTTP/1.1 keep-alive, HTTP/2 to origins
//     that offer it) and streams the response back.
//   - HTTPS: handles CONNECT, hijacks the client conn, splices it to a fresh
//     TCP conn to the origin. TLS stays end-to-end; the proxy never decrypts.
//   - GET /fetch?url=<target>: URL-forwarding endpoint that supports chaining
//     proxies. If PROXY_UPSTREAM is set, the request is forwarded to
//     <upstream>/fetch?url=<target>; otherwise the target is fetched directly.
//     Origin status, headers, and body are streamed back to the caller.
//
// Designed to run as a Service in k3s. Single binary, no deps. Scale by
// raising the Deployment replica count.
//
// Env vars:
//
//	PROXY_ADDR              listen address                   (default :8888)
//	PROXY_CONNECT_TIMEOUT   dial timeout to origin           (default 10s)
//	PROXY_IDLE_TIMEOUT      idle conn timeout                (default 90s)
//	PROXY_READ_HEADER_TO    header read timeout              (default 10s)
//	PROXY_MAX_IDLE_CONNS    pooled idle conns total          (default 4096)
//	PROXY_MAX_PER_HOST      pooled idle conns per host       (default 64)
//	PROXY_AUTH              optional "user:pass" basic auth  (default none)
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
	"crypto/tls"
	"encoding/base64"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
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
	transport      *http.Transport
	dialer         *net.Dialer
	authHeader     string // "Basic xxx" or ""
	connectTimeout time.Duration
	upstream       *url.URL // next-hop proxy for /fetch chaining; nil = terminal
	debug          bool     // if true, log each request and its duration
}

func newProxy() *proxy {
	connectTO := envDur("PROXY_CONNECT_TIMEOUT", 10*time.Second)
	idleTO := envDur("PROXY_IDLE_TIMEOUT", 90*time.Second)
	maxIdle := envInt("PROXY_MAX_IDLE_CONNS", 4096)
	maxPerHost := envInt("PROXY_MAX_PER_HOST", 64)

	d := &net.Dialer{Timeout: connectTO, KeepAlive: 30 * time.Second}

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

	var auth string
	if a := os.Getenv("PROXY_AUTH"); a != "" {
		auth = "Basic " + base64.StdEncoding.EncodeToString([]byte(a))
	}

	var upstream *url.URL
	if u := os.Getenv("PROXY_UPSTREAM"); u != "" {
		parsed, err := url.Parse(u)
		if err != nil || parsed.Scheme == "" || parsed.Host == "" {
			log.Fatalf("invalid PROXY_UPSTREAM %q: %v", u, err)
		}
		upstream = parsed
	}

	return &proxy{
		transport:      tr,
		dialer:         d,
		authHeader:     auth,
		connectTimeout: connectTO,
		upstream:       upstream,
		debug:          os.Getenv("DEBUG") != "",
	}
}

func (p *proxy) checkAuth(r *http.Request) bool {
	if p.authHeader == "" {
		return true
	}
	return r.Header.Get("Proxy-Authorization") == p.authHeader
}

func (p *proxy) requireAuth(w http.ResponseWriter) {
	w.Header().Set("Proxy-Authenticate", `Basic realm="proxy"`)
	http.Error(w, "proxy auth required", http.StatusProxyAuthRequired)
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
		p.requireAuth(w)
		return
	}
	if r.Method == http.MethodConnect {
		p.handleConnect(w, r)
		return
	}
	if r.URL.Path == "/fetch" {
		p.handleFetch(w, r)
		return
	}
	p.handleHTTP(w, r)
}

// requestSummary picks the most useful one-line description of an inbound
// request for debug logging. For /fetch it surfaces the wrapped target URL;
// for absolute-form proxy requests it shows the URL as the client sent it;
// otherwise it falls back to host + request URI.
func requestSummary(r *http.Request) string {
	if r.URL.Path == "/fetch" {
		if t := r.URL.Query().Get("url"); t != "" {
			return "/fetch?url=" + t
		}
	}
	if r.URL.IsAbs() {
		return r.URL.String()
	}
	if r.Method == http.MethodConnect {
		return r.Host
	}
	return r.Host + r.URL.RequestURI()
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
	if r.Method != http.MethodGet && r.Method != http.MethodPost {
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
		log.Printf("fetch upstream error: dialing %s (target=%s): %v", nextURL, target, err)
		http.Error(w, "bad gateway", http.StatusBadGateway)
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

func (p *proxy) handleHTTP(w http.ResponseWriter, r *http.Request) {
	if !r.URL.IsAbs() {
		http.Error(w, "non-absolute request URI; proxy expects absolute-form", http.StatusBadRequest)
		return
	}

	outReq := r.Clone(r.Context())
	outReq.RequestURI = ""
	outReq.Header = cloneAndStripHopByHop(r.Header)

	resp, err := p.transport.RoundTrip(outReq)
	if err != nil {
		log.Printf("upstream error: %s %s: %v", r.Method, r.URL, err)
		http.Error(w, "bad gateway", http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	dst := w.Header()
	for k, vv := range resp.Header {
		if _, drop := hopByHop[http.CanonicalHeaderKey(k)]; drop {
			continue
		}
		dst[k] = append(dst[k][:0], vv...)
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}

func (p *proxy) handleConnect(w http.ResponseWriter, r *http.Request) {
	host := r.URL.Host // "example.com:443"
	if host == "" {
		host = r.Host
	}
	if _, _, err := net.SplitHostPort(host); err != nil {
		http.Error(w, "bad CONNECT target", http.StatusBadRequest)
		return
	}

	dialCtx, cancel := context.WithTimeout(r.Context(), p.connectTimeout)
	defer cancel()
	upstream, err := p.dialer.DialContext(dialCtx, "tcp", host)
	if err != nil {
		log.Printf("CONNECT %s dial failed: %v", host, err)
		http.Error(w, "bad gateway", http.StatusBadGateway)
		return
	}

	hj, ok := w.(http.Hijacker)
	if !ok {
		_ = upstream.Close()
		http.Error(w, "hijack unsupported", http.StatusInternalServerError)
		return
	}
	client, bufrw, err := hj.Hijack()
	if err != nil {
		_ = upstream.Close()
		log.Printf("hijack failed: %v", err)
		return
	}

	if _, err := bufrw.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); err != nil {
		_ = upstream.Close()
		_ = client.Close()
		return
	}
	if err := bufrw.Flush(); err != nil {
		_ = upstream.Close()
		_ = client.Close()
		return
	}

	// Splice both directions. Use TCP CloseWrite for half-close so the peer
	// sees EOF instead of RST when one side finishes.
	go splice(upstream, client)
	splice(client, upstream)
}

func splice(dst, src net.Conn) {
	defer func() {
		if c, ok := dst.(*net.TCPConn); ok {
			_ = c.CloseWrite()
		} else {
			_ = dst.Close()
		}
		if c, ok := src.(*net.TCPConn); ok {
			_ = c.CloseRead()
		}
	}()
	_, _ = io.Copy(dst, src)
}

func cloneAndStripHopByHop(h http.Header) http.Header {
	out := make(http.Header, len(h))
	for k, vv := range h {
		if _, drop := hopByHop[http.CanonicalHeaderKey(k)]; drop {
			continue
		}
		// Per RFC 7230, Connection: header may list other headers to drop.
		if strings.EqualFold(k, "Connection") {
			continue
		}
		clone := make([]string, len(vv))
		copy(clone, vv)
		out[k] = clone
	}
	return out
}

func main() {
	addr := env("PROXY_ADDR", ":8888")
	readHeaderTO := envDur("PROXY_READ_HEADER_TO", 10*time.Second)

	p := newProxy()

	srv := &http.Server{
		Addr:              addr,
		Handler:           p,
		ReadHeaderTimeout: readHeaderTO,
		// No ReadTimeout/WriteTimeout: CONNECT tunnels can legitimately stay
		// open for the lifetime of a long-lived TLS session.
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
