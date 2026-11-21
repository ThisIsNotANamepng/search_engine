// Run from supporting_servers/:  go test proxy.go proxy_test.go
package main

import (
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
)

// chain starts the head proxy (A) in front of a last-hop proxy (B), both with password "pw", and returns A's URL.
// B's dial guard is swapped for a plain dialer because the test origin is on 127.0.0.1.
func chain(t *testing.T) string {
	t.Setenv("PROXY_PASSWORD", "pw")
	b := newProxy()
	b.transport.DialContext = (&net.Dialer{}).DialContext
	bSrv := httptest.NewServer(b)
	t.Cleanup(bSrv.Close)

	t.Setenv("PROXY_UPSTREAM", bSrv.URL)
	aSrv := httptest.NewServer(newProxy())
	t.Cleanup(aSrv.Close)
	return aSrv.URL
}

func fetch(t *testing.T, proxyURL, target, password string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(http.MethodGet, proxyURL+"/fetch?url="+url.QueryEscape(target), nil)
	req.Header.Set("X-Proxy-Auth", password)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { resp.Body.Close() })
	return resp
}

func TestChainFetchesPageWithoutLeakingPassword(t *testing.T) {
	var gotAuth string
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("X-Proxy-Auth")
		w.Header().Set("Content-Type", "text/html")
		io.WriteString(w, "<html>hi</html>")
	}))
	defer origin.Close()

	resp := fetch(t, chain(t), origin.URL+"/page", "pw")
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != 200 || string(body) != "<html>hi</html>" || resp.Header.Get("Content-Type") != "text/html" {
		t.Fatalf("got %d %q %q", resp.StatusCode, resp.Header.Get("Content-Type"), body)
	}
	if gotAuth != "" {
		t.Fatalf("the site received the proxy password %q", gotAuth)
	}
}

func TestProxyFaultsAreMarkedSiteFaultsAreNot(t *testing.T) {
	a := chain(t)
	dead := httptest.NewServer(nil)
	deadURL := dead.URL
	dead.Close() // nothing listens here any more
	t.Setenv("PROXY_UPSTREAM", deadURL)
	aDeadUpstream := httptest.NewServer(newProxy())
	defer aDeadUpstream.Close()

	for _, c := range []struct {
		name, proxyURL, target, password string
		status                           int
		marked                           bool
	}{
		{"wrong password", a, "http://example.com/", "nope", 401, true},
		{"upstream proxy down", aDeadUpstream.URL, "http://example.com/", "pw", 502, true},
		{"site down: the page's fault", a, deadURL + "/", "pw", 502, false},
	} {
		resp := fetch(t, c.proxyURL, c.target, c.password)
		if resp.StatusCode != c.status || (resp.Header.Get("X-Proxy-Error") != "") != c.marked {
			t.Errorf("%s: got %d X-Proxy-Error=%q, want %d marked=%v", c.name, resp.StatusCode, resp.Header.Get("X-Proxy-Error"), c.status, c.marked)
		}
	}
}

func TestLastHopRefusesInternalAddresses(t *testing.T) {
	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("the internal site was reached")
	}))
	defer origin.Close()
	t.Setenv("PROXY_PASSWORD", "pw")
	b := httptest.NewServer(newProxy()) // real dial guard
	defer b.Close()

	if resp := fetch(t, b.URL, origin.URL, "pw"); resp.StatusCode != 403 {
		t.Fatalf("got %d, want 403", resp.StatusCode)
	}

	for addr, wantRefused := range map[string]bool{
		"127.0.0.1:80": true, "[::1]:80": true, "[::ffff:127.0.0.1]:80": true, "10.1.2.3:80": true,
		"172.16.0.1:80": true, "192.168.1.1:80": true, "169.254.169.254:80": true, "100.100.100.100:80": true,
		"0.0.0.0:80": true, "[fd00::1]:80": true, "[fe80::1%eth0]:80": true,
		"93.184.215.14:443": false, "[2606:2800:21f:cb07:6820:80da:af6b:8b2c]:443": false,
	} {
		if refused := refuseNonPublic("tcp", addr, nil) != nil; refused != wantRefused {
			t.Errorf("%s: refused=%v, want %v", addr, refused, wantRefused)
		}
	}
}

func TestOnlyFetchIsServed(t *testing.T) {
	t.Setenv("PROXY_PASSWORD", "pw")
	b := httptest.NewServer(newProxy())
	defer b.Close()
	connect, _ := http.NewRequest(http.MethodConnect, b.URL, nil)
	connect.Host = "example.com:443"
	absolute, _ := http.NewRequest(http.MethodGet, "http://example.com/", nil) // forward-proxy form
	absolute.URL.Host = b.Listener.Addr().String()
	absolute.Host = "example.com"
	post, _ := http.NewRequest(http.MethodPost, b.URL+"/fetch?url=http://example.com/", strings.NewReader("q=1"))
	for req, want := range map[*http.Request]int{connect: 404, absolute: 404, post: 405} {
		req.Header.Set("X-Proxy-Auth", "pw")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != want {
			t.Errorf("%s %s: got %d, want %d", req.Method, req.URL, resp.StatusCode, want)
		}
	}
}
