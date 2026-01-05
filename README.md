# HA Remote Relay Client (Add-on)

This Home Assistant add-on enables secure outbound connectivity to a remote relay service, allowing access to a Home Assistant instance without exposing inbound ports or modifying firewall or router configuration.

The add-on is designed for users who require remote access in restricted or CGNAT environments, or who prefer an outbound-only security model.

This project is currently in a public pilot phase.

---

## Overview

The add-on establishes a persistent outbound connection from Home Assistant to a relay service.  
All incoming requests are routed back through this connection.

Key characteristics:
- Outbound-only connection (no inbound ports)
- No port forwarding required
- Works behind NAT, CGNAT, and firewalls
- Device-based authentication
- Designed for multi-instance environments

---

## Requirements

Before installing the add-on, you must register your Home Assistant instance with the relay service.

1. Create an account at:

   https://panel.myhalink.com

2. Register a new device in the portal.

During registration you will receive:
- `relay_url`
- `device_id`
- `secret`

These values are required for add-on configuration.

---

## Installation

1. Open Home Assistant and navigate to:

   **Settings → Add-ons → Add-on Store → Repositories**

2. Add the following repository URL:
   
   https://github.com/jurisaro/ha-remote-addon.git

3. After adding the repository, locate **Remote Relay Client** in the Add-on Store and install it.

---

## Configuration

Configure the add-on using the Home Assistant UI.

Required fields:

- `relay_url`  
The relay service endpoint provided by the portal.

- `device_id`  
Unique identifier for this Home Assistant instance.

- `secret`  
Per-device shared secret used for authentication.

Do not reuse secrets between devices.

---

## Usage

1. Start the add-on.
2. The add-on will establish an outbound connection to the relay service.
3. Once connected, the device becomes reachable via its assigned relay endpoint.

No inbound ports, firewall rules, or router configuration changes are required.

---

## Security Model (High-Level)

- Devices are allowlisted server-side.
- Authentication is device-specific.
- All connections are initiated from the Home Assistant instance.
- Long-lived or high-risk endpoints are restricted by design.

Implementation details are intentionally not exposed.

---

## Status

This add-on is part of a public pilot project.  
Functionality, APIs, and service availability may change.

Feedback from early users is welcome.

---

## Support

For portal access, device registration, and service-related questions:

https://panel.myhalink.com

