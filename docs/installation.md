# Installation

Follow [development.md](development.md) to create the local Python environment.
Hardware assembly and electrical installation remain out of scope.

Milestone 12 additionally documents optional local deployment of the read-only
web service in the [health dashboard guide](health-dashboard.md), including a
hardening-oriented systemd example. Project Aurora never copies, installs,
enables, starts, stops, or restarts that unit automatically.

Milestone 17's [configuration-profile guide](configuration-profiles.md)
documents optional operator-created profile and backup directories. Aurora does
not create or discover them. Both directories require mode `0700`; profile,
active, backup, manifest, and lock files require `0600` and effective-operator
ownership. Validate and plan with the prepared process environment before
applying.

Apply and rollback modify only the explicitly supplied YAML file. After success,
restart the externally managed Aurora service separately under the site's normal
administration procedure. Aurora neither executes nor automates that restart.
