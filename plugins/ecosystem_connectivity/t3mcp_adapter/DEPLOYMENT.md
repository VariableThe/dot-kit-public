# Deployment boundary

One supported hosting shape for this offline prototype is synchronous WSGI
behind a trusted gateway; it has not been validated as a production deployment. The repository does not start a server or
create credentials.

Required gateway properties:

- finite worker count (one worker per mutable T3 session, unless the operator
  supplies independent session objects);
- finite request, header, body, and upstream timeouts;
- duplicate header rejection before WSGI normalization, especially
  `Authorization`;
- HTTPS at the public resource URL and issuer URL, with no userinfo or
  fragments;
- bounded request-body buffering and connection limits.

The adapter's `create_wsgi_app()` enforces byte-accurate `Content-Length`,
rejects chunked transfer unless the gateway decodes it safely, reads exactly
the declared number of bytes, and never reads bodies for metadata/GET routes.
The WSGI tests exercise real JWT verification and a fake upstream without
opening a listener.

`create_asgi_app()` offloads the complete synchronous handler, including
verification and dispatch, to one dedicated worker. The single worker protects
shared mutable upstream sessions. There is no waiting work queue: a request
arriving while a handler remains active receives 503 with `Retry-After: 1`.
The gateway must still bound connections and request-body buffering.

The request deadline is captured before body ingress and passed unchanged to
dispatch. Body ingress has a fixed (not per-chunk) timeout. A handler that
outlives the response budget returns 504; cancellation or timeout does not
release worker capacity until the synchronous job actually finishes. A 504 or
disconnect does not prove a write failed; use the existing journal/request-id
reconciliation rather than issuing an unrelated retry.

Hosts must drive ASGI lifespan shutdown, or await `application.aclose()` in
embedded use. Shutdown stops admission and waits at most `shutdown_grace_s`
(default 1 second), then closes the executor without waiting. `aclose()` returns
whether work drained; lifespan sends `shutdown.failed` if the grace expires.
Running Python threads cannot be forcibly terminated:
a stuck upstream can outlive shutdown and delay process exit. Configure finite
upstream timeouts and an external process supervisor. The application is bound
to one event loop and cannot be reopened after shutdown. These offline tests
are not a claim of live deployment validation.

OAuth linking remains operator work: configure an external authorization
server, issuer, audience/resource URL, public JWKS or secret reference, and
gateway policy. No credentials, token endpoint, client registration, or live
service is included here.


Validation for this worker integration: nine isolated, socket-free executor
unit tests passed, with the unrelated tools module stubbed for import. The
full dependency-backed suite and real HTTP/JWT/dispatch integration were not
run in this environment because pytest and requests were unavailable. No
production endpoint was exposed or deployed. Run the complete suite with the
normal dependencies before treating this integration as deployment-ready.
