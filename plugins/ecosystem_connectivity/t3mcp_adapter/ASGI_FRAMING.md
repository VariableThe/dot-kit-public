# ASGI request framing boundary

This is a deliberately narrow hosting contract, not a general-purpose ASGI
compatibility layer or proof of wire-protocol safety. The T3 adapter validates
raw header byte pairs before decoding them.
Previously it rejected duplicate names but accepted malformed raw headers,
Transfer-Encoding, and Content-Length values that did not match received body
bytes. Such requests could reach the synchronous handler. This change prevents
those ambiguous application-level requests from being dispatched.

The boundary accepts only list/tuple header collections containing list/tuple
byte pairs. Other ASGI-compliant iterables are intentionally unsupported. The
host must supply this representation; arbitrary iterables are not consumed.

The boundary rejects:

- More than 128 header fields or aggregate header bytes above the configured cap.
- Malformed pairs, non-byte names/values, invalid name syntax, control bytes,
  and case-insensitive duplicate names, including identical Content-Lengths and
  otherwise legal repeated HTTP/2 Cookie fields. This sacrifices compatibility
  deliberately; callers must use a single Cookie field where appropriate.
- Underscores in names and HTAB in values, even where legal HTTP syntax permits
  them (conservative restrictions, including avoiding normalization aliases).
- Any Transfer-Encoding field. Chunked HTTP/1 framing is deliberately unsupported
  by this deployment contract if the server exposes that field to the app, even
  after correct dechunking. The host must safely enforce and normalize framing
  before dispatch, rather than rely on this application to parse wire chunks.
- Non-digit, combined, signed, whitespace-padded, empty or overly long
  Content-Length fields and declared bodies above the configured maximum.
- A completed body shorter or longer than the declared length. A body exceeding
  the declared length is rejected without waiting for more chunks.

Content-Length remains optional for valid ASGI requests without that header
(e.g. HTTP/2), but aggregate body-byte and ingress-time bounds still apply.
A supplied length must match exactly. Both configured HTTP byte limits must
be positive integers; booleans, fractions and nonfinite values are rejected.

This is defense in depth, not a wire-protocol server. A trusted ASGI server must
still reject ambiguous wire framing before consuming it; a proxy/server may
already have coalesced or removed fields before supplying the ASGI scope. TLS,
canonical routing, finite ingress and response-send timeouts (including stalled
clients), and connection/worker supervision
remain hosting prerequisites. No server, listener or network configuration is
created. Beeper's WSGI interface cannot recover duplicates erased by WSGI
normalization and continues to require gateway-side duplicate-header rejection.

## Offline checks

Seven new isolated unittest cases pass against the real ASGI ingress, with tool
imports/dispatch replaced by synthetic stubs. They verify that duplicate,
malformed and excessive headers are rejected before body reads; contradictory
body lengths never dispatch; exact/absent lengths still dispatch; and the narrow
representation, repeatable-field, HTAB and dechunking restrictions remain explicit. Socket
construction is prohibited. Python compilation passes. These are boundary tests,
not full authentication, full repository, real HTTP server or live T3 tests.
