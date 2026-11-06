---
name: use-beeper-local
description: Check an existing local Beeper connection and carry out specifically authorized messaging tasks. Verify the host, account, network, and recipient before reading or sending.
---

# Use Beeper locally

## Prerequisites

Use an existing Beeper Desktop instance and its already configured local client or MCP bridge on the owning computer. Discover the installed client's current help and tool schemas. Do not call a computer's loopback endpoint from another host.

The Python Beeper facade in this repository is a read-only prototype. It exposes bounded chat search and message reads, not message sending. Sending requires a separately available, authorized local Beeper tool that actually supports it.

## Check a connection

For a connection-only request, use status or tool discovery and stop. Do not read contacts or messages, send a test message, start another service, change network bindings, or create credentials merely to prove the connection.

Distinguish unavailable host access, sandbox restrictions, endpoint failure, authentication failure, and missing user authorization. Do not infer that the desktop app stopped from a failed local request.

## Resolve the recipient and task

- Verify the selected computer, Beeper account, requested network, and exact conversation.
- Use the user's stated identity and relevant recent evidence. Similar display names alone are insufficient.
- Keep WhatsApp, Signal, and other networks separate even when they refer to the same person.
- Read only the information needed for the authorized task.
- Ask when competing identity matches or instructions remain unresolved. Do not silently substitute a similar handle or another network.

## Carry out authorized messaging

- Verify that the user authorized sending, including the recipient and purpose. Drafting alone does not authorize a send.
- Match their intended meaning and tone without adding promises or private information.
- Use the discovered tool schema and retain returned message identifiers.
- Check recent relevant messages for duplicates before sending.
- For an uncertain send, check the intended chat on the same host/account before a retry. Do not resend through a fallback while the first result is unknown.
- Distinguish send acceptance, same-chat readback, recipient delivery, and recipient read status. Report only what the tool actually verifies.

Keep contacts, messages, credentials, runtime state, and logs out of source repositories. Do not change persistent access or security settings as an implicit repair. A configured bridge does not authorize unrelated reading, sharing, or future messages.
