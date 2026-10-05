# Trusted client identity

The edge is the first trusted forwarding hop. Both HTTPS server blocks replace
`X-Forwarded-For` with the TCP peer (`$remote_addr`), replace `X-Real-IP`, and remove
RFC 7239 `Forwarded`. Client-supplied forwarding chains never enter through the edge.

Frontend and admin retain their existing app-owned proxy configurations. They
append their TCP peer to XFF. Uvicorn receives a chain such as `client, edge` from
an app proxy. Its explicit trust list contains only the edge, frontend and admin
addresses. It scans from the right and selects the first untrusted hop, then sets
the ASGI client address. The backend's `TRUST_PROXY_HEADERS` setting is forced to
false in Compose so the limiter uses that resolved address rather than rereading
the raw leftmost XFF value. A stale `.env` value cannot enable raw-header trust.

The trust list does not include the Docker gateway, backend, data services,
arbitrary containers or an entire subnet. Direct requests from an untrusted peer
cannot change the ASGI client by supplying headers. Direct requests to frontend
or admin append their actual peer, so spoofed prefixes also do not select identity.

## Network and rollout

`proxy_net` is a separate transport network. Frontend, admin and edge join only
this network; backend joins it and the existing `app_net`. Redis, PostgreSQL and
worker remain on `app_net`. Backend can still reach its broker and database, and
app proxies resolve backend over their only shared network. Default addresses:

| Service | proxy_net address | Trusted forwarding hop |
| --- | --- | --- |
| Edge | 172.31.240.2 | Yes |
| Frontend | 172.31.240.3 | Yes |
| Admin | 172.31.240.4 | Yes |
| Backend | 172.31.240.5 | No |

The default subnet is `172.31.240.0/28`. All four services have reserved addresses
so Docker cannot allocate backend into a trusted proxy slot before edge startup.
If the subnet overlaps host, VPN or Docker routes, set `PROXY_NET_SUBNET` and all
four `*_PROXY_IP` values together in `.env`. Use distinct addresses inside that
subnet. Do not widen the Uvicorn list to `*` or a whole subnet.

Before production rollout, adopt the A01 verified release state and confirm the
running backend supports Uvicorn's explicit trust-chain handling. The regression
probe uses the application's current Uvicorn 0.51.0 dependency. Inspect the current
runtime version with:

```bash
bash scripts/compose.sh exec -T backend python -c 'import uvicorn; print(uvicorn.__version__)'
```

Apply through the normal infrastructure PR workflow, preserving recorded image digests. Verify application health after
convergence. Adding the network recreates app/proxy containers; plan a brief
interruption. The existing data network and data volumes do not need recreation.

Port bindings and the intentional admin certificate/access policy are unchanged.
HTTPS scheme preservation remains separate work in A11. Do not put another ingress
proxy before this edge without revisiting the trust boundary: currently its TCP
peer is the effective client, regardless of incoming XFF.

## Backend handoff

The infrastructure override makes the deployed chain safe without rewriting
business logic. Backend ownership should consolidate identity handling:

- Deprecate direct leftmost-XFF parsing in `app/core/rate_limit.py`. Consume the
  trusted ASGI client address consistently for login, registration and media limits.
- Change the image's default `--forwarded-allow-ips=*` to explicit deployment
  configuration. A backend started outside this Compose configuration still has
  the old unsafe defaults; the infrastructure override does not fix standalone use.
- Keep unit coverage for untrusted direct peers, duplicate/comma-separated headers,
  multiple trusted hops, IPv4/IPv6 and missing client metadata.

App proxy owners should retain append semantics only while backend uses the explicit
right-to-left trust chain. A future change to raw leftmost parsing must not be
deployed. Update the pinned regression snapshots whenever app configs or the
production limiter change. No application source was modified by this infra PR.

## Verification

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 tests/smoke_client_identity.py
bash tests/smoke-proxy.sh
docker compose --env-file .env.example config --quiet
```

The isolated smoke stack uses actual edge configuration, snapshots of both app
Nginx configurations and the unmodified production limiter (provenance in
`tests/fixtures/client-identity/sources.json`). FastAPI probe routes apply the same
login/registration/upload limits without database/authentication side effects.
The probe uses real Redis DB 2 and current pinned FastAPI/Redis/Uvicorn versions.
It tests both TLS paths, distinct TCP clients, duplicate and rotating spoofed XFF,
direct frontend/admin/backend access and an untrusted data-network peer. It checks
429 and separate Redis identity keys across all three limit types. Every Nginx
config passes `nginx -t`. Test containers publish no host ports and cleanup removes
only the uniquely named disposable project and its volumes/images.

Sources: [Uvicorn 0.51.0 trusted proxy handling](https://github.com/Kludex/uvicorn/blob/0.51.0/uvicorn/middleware/proxy_headers.py),
[Nginx proxy_set_header](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header).
