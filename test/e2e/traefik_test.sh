#!/usr/bin/env bash
# Traefik checks against the running e2e stack (:80/:443): TLS versions and ciphers, the
# HTTP -> HTTPS redirect, security headers and caching on every kind of path, SPA and
# Swagger routing, /internal (token broker) not reachable from outside, loopback-only
# backend ports. Needs curl, openssl and ss. The slow timeout checks (responses after
# 240s/660s) are not part of this suite.
set -u
pass=0; fail=0
check() { # name, command...
    local name="$1"; shift
    if "$@" >/dev/null 2>&1; then echo "PASS $name"; pass=$((pass+1)); else echo "FAIL $name"; fail=$((fail+1)); fi
}
hdr() { curl -sk -o /dev/null -D - "$@" | tr -d '\r'; }
BODY=$(mktemp)
trap 'rm -f "$BODY"' EXIT

# --- TLS
check 'TLS 1.1 refused' bash -c '! echo | openssl s_client -connect localhost:443 -tls1_1 -cipher "DEFAULT@SECLEVEL=0" 2>&1 | grep -q "^New, TLS"'
check 'TLS 1.2 works' bash -c 'echo | openssl s_client -connect localhost:443 -tls1_2 2>&1 | grep -q "^New, TLSv1.2"'
check 'TLS 1.3 works' bash -c 'echo | openssl s_client -connect localhost:443 -tls1_3 2>&1 | grep -q "^New, TLSv1.3"'
check 'no CBC cipher on TLS 1.2' bash -c '! echo | openssl s_client -connect localhost:443 -tls1_2 -cipher ECDHE-RSA-AES128-SHA 2>&1 | grep -q "^New, TLSv1.2"'
check 'certificate from $MOTUZ_DOCKER_ROOT/certs served' bash -c 'echo | openssl s_client -connect localhost:443 2>/dev/null | openssl x509 -noout -subject | grep -q "CN *= *localhost"'

# --- redirect
R=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' 'http://localhost/clouds?x=1')
check 'HTTP -> HTTPS redirect' bash -c "[[ '$R' =~ ^(301|308)\ https://localhost/clouds\?x=1$ ]]"
check 'HTTP /internal redirects only' bash -c "curl -s -o /dev/null -w '%{http_code}' -X POST http://localhost/internal/oauth/token | grep -qE '^(301|308)$'"

# --- security headers everywhere
JS=$(curl -sk https://localhost/ | grep -oE '/js/[A-Za-z0-9_.-]+\.js' | head -1)
echo "    bundle: $JS"
check 'index references a /js bundle' test -n "$JS"
for path in / /clouds /api/system/info/ "$JS" /img/logo.png /api/ /swaggerui/swagger-ui-bundle.js /api/nope; do
    H=$(hdr "https://localhost$path")
    check "security headers on $path" bash -c "echo '$H' | grep -qi '^strict-transport-security: max-age=31536000' && echo '$H' | grep -qi '^x-frame-options: DENY' && echo '$H' | grep -qi '^x-content-type-options: nosniff' && echo '$H' | grep -qi '^referrer-policy: same-origin'"
done
check 'cache: /js 30 days' bash -c "curl -sk -o /dev/null -D - https://localhost$JS | grep -qi '^cache-control: public, max-age=2592000'"
check 'cache: /img 7 days' bash -c "curl -sk -o /dev/null -D - https://localhost/img/logo.png | grep -qi '^cache-control: public, max-age=604800'"
check 'cache: / no-store' bash -c "curl -sk -o /dev/null -D - https://localhost/ | grep -qi '^cache-control: no-store'"
check 'cache: /clouds no-store' bash -c "curl -sk -o /dev/null -D - https://localhost/clouds | grep -qi '^cache-control: no-store'"
check 'js content type' bash -c "curl -sk -o /dev/null -D - https://localhost$JS | grep -qi '^content-type: text/javascript'"
check 'js 304 on revalidation' bash -c "E=\$(curl -sk -o /dev/null -D - https://localhost$JS | grep -i '^etag' | cut -d' ' -f2 | tr -d '\r'); [ \"\$(curl -sk -o /dev/null -w '%{http_code}' -H \"If-None-Match: \$E\" https://localhost$JS)\" = 304 ]"
check 'missing asset 404' bash -c "[ \"\$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/js/nope.js)\" = 404 ]"

# --- SPA and swagger
check 'SPA /clouds loads index' bash -c "curl -sk https://localhost/clouds | grep -q '$JS'"
check 'SPA deep route loads index' bash -c "curl -sk https://localhost/a/b/c | grep -q '$JS'"
check '/swaggerui asset 200' bash -c "[ \"\$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/swaggerui/swagger-ui-bundle.js)\" = 200 ]"
check '/api/ swagger 200' bash -c "curl -sk https://localhost/api/ | grep -q swagger-ui"
check '/api/swagger.json' bash -c "curl -sk https://localhost/api/swagger.json | grep -q '/copy-jobs/'"
check '/api -> /api/' bash -c "curl -sk -o /dev/null -w '%{http_code} %{redirect_url}' https://localhost/api | grep -q '^308 https://localhost/api/'"
check 'trailing slash redirect stays https' bash -c "curl -sk -o /dev/null -w '%{redirect_url}' https://localhost/api/connections | grep -q '^https://localhost/api/connections/'"
check 'unknown /api path is a 404, not the SPA' bash -c "[ \"\$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/api/nope)\" = 404 ]"
check 'no Traefik dashboard/API' bash -c "! curl -sk https://localhost/dashboard/ | grep -qi traefik && ! curl -sk https://localhost/api/rawdata | grep -q routers && ! curl -s -m 2 http://127.0.0.1:8080/api/rawdata | grep -q routers"

# --- /internal (token broker)
for path in /internal/oauth/token //internal/oauth/token /./internal/oauth/token /x/../internal/oauth/token /%69nternal/oauth/token /internal%2Foauth%2Ftoken /internal/oauth/token/ '/internal/oauth/token?a=b'; do
    code=$(curl -sk -o "$BODY" -w '%{http_code}' --path-as-is -X POST -d 'grant_type=refresh_token&refresh_token=x' "https://localhost$path")
    check "not reachable via Traefik: $path" bash -c "[ '$code' != 200 ] && ! grep -q '\"error\"' '$BODY'"
done
for H in 'Host: 127.0.0.1:5000' 'Host: 127.0.0.1:5001'; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "$H" -H 'X-Forwarded-Port: 5001' -H 'X-Forwarded-Host: 127.0.0.1:5001' -X POST -d 'grant_type=refresh_token&refresh_token=x' http://127.0.0.1:5000/internal/oauth/token)
    check "direct :5000 /internal rejected ($H)" test "$code" = 404
done
B=$(curl -s -X POST -d 'grant_type=refresh_token&refresh_token=x' http://127.0.0.1:5001/internal/oauth/token)
check 'broker answers on :5001' bash -c "echo '$B' | grep -q invalid_grant"

# --- listening sockets
check 'motuz ports 5000/5001/5432/5672 loopback only' bash -c "! ss -ltnH | awk '{print \$4}' | grep -vE '^(127\.|\[::1\])' | grep -qE ':(5000|5001|5432|5672|15672|4369|25672)$'"
check '80 and 443 listening' bash -c "ss -ltnH | awk '{print \$4}' | grep -qE '^(\*|0\.0\.0\.0|\[::\]):443$' && ss -ltnH | awk '{print \$4}' | grep -qE '^(\*|0\.0\.0\.0|\[::\]):80$'"

echo; echo "$pass/$((pass + fail)) passed"
[ "$fail" = 0 ]
