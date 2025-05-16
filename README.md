# Experimental QoS for Foomuuri Firewall using Internal Macros

This repository contains an experimental setup to explore configuring a complex Quality of Service (QoS) policy for [Foomuuri Firewall](https://github.com/FoobarOy/foomuuri) by defining all parameters as macros directly within the `foomuuri.conf` file. The QoS rules are then applied by a Python script (`qos_engine_macro.py`) that parses these macros.

**DISCLAIMER: This is a Proof-of-Concept / Experimental Approach!**

This method was developed to explore the feasibility of a single-file configuration for both Foomuuri firewall rules and its QoS policy, as per a suggestion.

## Objective of this Experiment

The main goal was to see if a complete QoS policy, including:
* Per-interface total bandwidths (upload/download)
* Default QoS classes per interface
* Service-specific QoS classes (linked to Foomuuri marks) with `rate`, `ceil`, and `priority`
* Per-interface overrides for these service-specific limits

...could be defined solely using Foomuuri macros and then parsed and applied by a Python script.

## How It (Conceptually) Works

1.  **`foomuuri.conf` (Macro Definitions):**
    * A large `macro { ... }` block within `foomuuri.conf` is used to define all QoS parameters.
    * **Interfaces:** Macros like `QOS_IF_ENP1S0_NAME`, `QOS_IF_ENP1S0_TOTAL_UPLOAD_BW`, `QOS_IF_ENP1S0_DEFAULT_UPLOAD_RATE`, etc., define each WAN interface and its defaults.
    * **Service List:** A macro `QOS_SERVICE_LIST` (e.g., `"HTTP SSH HTTPS WEBAPP BULK"`) tells the script which services to configure.
    * **Service Parameters & Overrides:** For each service in the list (e.g., "HTTP"), a series of macros define its `_MARK`, default limits (`_UPLOAD_RATE_DEFAULT`, etc.), and per-interface overrides. For example, to set different HTTP limits for WAN1 (`enp1s0`/`ifb_isp1`) and WAN2 (`enp8s0`/`ifb_isp2`):
        ```bash
        # --- Example Macros for HTTP Service QoS ---
        # (Within the main QoS macro block in foomuuri.conf)

        # HTTP Service Definition
        QOS_SRV_http_MARK                   "0x10"
        QOS_SRV_http_PRIORITY               "5"
        QOS_SRV_http_UPLOAD_SUFFIX          "89"
        QOS_SRV_http_UPLOAD_RATE_DEFAULT    "1Mbit"  # Default upload rate for HTTP
        QOS_SRV_http_UPLOAD_CEIL_DEFAULT    "5Mbit"  # Default upload ceil for HTTP
        QOS_SRV_http_DOWNLOAD_SUFFIX        "89"
        QOS_SRV_http_DOWNLOAD_RATE_DEFAULT  "2Mbit"  # Default download rate for HTTP
        QOS_SRV_http_DOWNLOAD_CEIL_DEFAULT  "10Mbit" # Default download ceil for HTTP
        # ... (filter priorities if needed) ...

        # HTTP Overrides for WAN1 (enp1s0 for upload, ifb_isp1 for download)
        QOS_SRV_http_OVERRIDE_ENP1S0_UPLOAD_RATE "2Mbit"
        QOS_SRV_http_OVERRIDE_ENP1S0_UPLOAD_CEIL "8Mbit"
        QOS_SRV_http_OVERRIDE_IFB_ISP1_DOWNLOAD_RATE "10Mbit"
        QOS_SRV_http_OVERRIDE_IFB_ISP1_DOWNLOAD_CEIL "75Mbit"

        # HTTP Overrides for WAN2 (enp8s0 for upload, ifb_isp2 for download)
        QOS_SRV_http_OVERRIDE_ENP8S0_UPLOAD_RATE "500kbit"
        QOS_SRV_http_OVERRIDE_ENP8S0_UPLOAD_CEIL "1Mbit"
        QOS_SRV_http_OVERRIDE_IFB_ISP2_DOWNLOAD_RATE "1Mbit"
        QOS_SRV_http_OVERRIDE_IFB_ISP2_DOWNLOAD_CEIL "5Mbit"
        ```
    * Foomuuri still handles the actual packet marking (e.g., `http mark_set 0x10 -conntrack`) in its zone rules, which also sets the `connmark`.
2.  **`qos_engine_macro.py` (Python Script):**
    * Called by Foomuuri's `post_start` and `pre_stop` hooks.
    * Reads and parses the `/etc/foomuuri/foomuuri.conf` file.
    * Extracts all macros starting with `QOS_IF_` and `QOS_SRV_` based on the `QOS_SERVICE_LIST`.
    * Reconstructs an internal data structure representing the QoS policy (interfaces, services, defaults, overrides).
    * Validates the extracted parameters (e.g., format of bandwidth values, marks).
    * Applies the `tc` rules (HTB qdiscs, classes, filters) using the same dual-HTB and Connmark logic as the YAML-based solution:
        * Upload shaping on the physical WAN interface (e.g., `enp1s0`) using `tc filter ... match mark ...`.
        * Download shaping on an IFB interface (e.g., `ifb_isp1`), with classification using `tc filter ... match mark ...` after the mark is restored via `ctinfo cpmark` from the `connmark`.

## Files in this Repository

* `foomuuri.conf`: An example of the `/etc/foomuuri/foomuuri.conf` file containing all the QoS parameter macros.
* `qos_engine_macro.py`: The Python script designed to parse the macros in the above `foomuuri.conf` and apply `tc` rules.

## How to (Hypothetically) Test

1.  **Backup your existing Foomuuri setup.**
2.  Place `qos_engine_macro.py` in `/etc/foomuuri/qos/` and make it executable.
3.  Ensure the `hook` section in `/etc/foomuuri/foomuuri.conf` points to this script:
    ```bash
    hook {
        post_start /usr/bin/python3 /etc/foomuuri/qos/qos_engine_macro.py --start --config-file /etc/foomuuri/foomuuri.conf
        pre_stop /usr/bin/python3 /etc/foomuuri/qos/qos_engine_macro.py --stop --config-file /etc/foomuuri/foomuuri.conf
    }
    ```
4.  Run `sudo foomuuri reload`.
5.  Check the script's log (`/var/log/foomuuri-qos-macro.log`) and `tc` commands (`tc -s class show ...`, `tc -s filter show ...`).
6.  Perform `iperf3` tests.

## Results of this Experiment

* **Technical Feasibility:** The script was successfully able to parse the macros and apply the `tc` rules. `iperf3` tests confirmed that bandwidth limits (including per-interface overrides for different services) were being enforced.

## Conclusion of the Experiment

This exploration was valuable for understanding the limitations of using Foomuuri macros for such tasks and for thinking about how a future native QoS system in Foomuuri ("Foojarru") might be designed to handle these complexities more elegantly.

## License

This experimental code is shared under the **GNU General Public License v2.0 (GPLv2)**, to align with Foomuuri Firewall.

## Acknowledgements
* Mika Heino (@kimheino) for Foomuuri and for the insightful discussion on GitHub.
* My internship supervisors for their guidance.
