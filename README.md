# Remote Relay Client

Outbound-only secure relay client for Home Assistant API and WebSocket access.

This add-on enables secure remote access to Home Assistant without exposing inbound ports or requiring VPNs. It follows a **cloud-assisted, outbound-tunnel architecture**, conceptually similar to Home Assistant Cloud (Nabu Casa), while remaining independent and self-contained.

The solution is designed for **production use**, **multi-device deployments**, and **future SaaS operation**.

---

## Overview

Remote Relay Client runs inside Home Assistant as an add-on and establishes an outbound, authenticated tunnel to a managed relay service. All user interactions continue to use standard Home Assistant authentication and authorization mechanisms.

At no point does this add-on expose Home Assistant directly to the public internet.

---

## Authentication Model

### Home Assistant Authentication

The add-on provides a configuration UI and uses Home Assistant authentication (Auth API).

* User authentication is fully handled by Home Assistant
* No Home Assistant credentials are accessed, stored, or transmitted by the add-on
* Authorization and permissions remain enforced by Home Assistant

### Device Authentication

Each Home Assistant instance authenticates to the relay service using a unique device identifier and a per-device secret.

All communication between the add-on and the relay service is cryptographically authenticated.

---

## Security Design (High-Level)

This solution follows a **least-privilege, defense-in-depth** approach:

* Outbound-only connections from Home Assistant
* No inbound network exposure
* No privileged container access
* No host filesystem access
* No direct access to Docker, devices, or system services

Internal implementation details of the relay service are intentionally abstracted and may evolve over time.

---

## Home Assistant Add-on Characteristics

* Uses Home Assistant Core API
* Uses Home Assistant Auth API for UI access
* Does not require host networking
* Does not expose any ports

Security Rating: **6 / 8** (appropriate for a remote access component)

---

## Installation

### Pilot / GitHub Installation

This add-on is currently distributed as a **pilot project** via a public GitHub repository and is **not yet available in the official Home Assistant Add-on Store**.

To install:

1. Open Home Assistant and navigate to:

   * **Settings → Add-ons → Add-on Store → Repositories**

2. Add the following repository URL:

```
https://github.com/jurisaro/ha-remote-addon.git
```

3. After adding the repository, locate **Remote Relay Client** in the Add-on Store and install it.

4. Configure the add-on using the Home Assistant UI:

   * `relay_url` – URL of the relay service
   * `device_id` – Unique identifier for this Home Assistant instance
   * `secret` – Per-device shared secret

5. Start the add-on.

Once started, the add-on will establish an outbound connection to the relay service.

No inbound ports, firewall rules, or router configuration changes are required.

---

## Configuration

The add-on is configured exclusively through the Home Assistant UI.

Required options:

* `relay_url` – URL of the relay service
* `device_id` – Unique identifier for this Home Assistant instance
* `secret` – Per-device shared secret

---

## Intended Use

Remote Relay Client is intended to be used as:

* A managed remote access service
* A private or enterprise deployment
* A foundation for a SaaS offering

The add-on is designed to integrate cleanly into Home Assistant environments without altering existing security assumptions.

---

## Notes on Trust

As with any remote access solution, users should only connect to relay services they trust. The add-on itself does not weaken Home Assistant’s internal security model and relies on Home Assistant’s native authentication and authorization layers.

---

## License & Support

License and support terms depend on the distribution model of the service.
