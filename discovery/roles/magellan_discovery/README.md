# Magellan Discovery Role

## Overview

Generates Omnia PXE mapping and xnames files from a user-provided admin
inventory CSV. This is the "Magellan" discovery mechanism used when running
`discovery.yml` with `discovery_mechanism=magellan`.

## Purpose

- Read the admin inventory CSV (`admin_inventory_path`)
- Validate every row (MACs, IPs, hostnames, and physical location fields)
- Generate timestamped `pxe_mapping_file_<timestamp>.csv`
- Generate timestamped `xnames_<timestamp>.csv`

## Workflow

1. Run discovery:

   ```bash
   ansible-playbook discovery/discovery.yml -e "discovery_mechanism=magellan"
   ```

2. Review and edit the generated files in `/opt/omnia/input/project_default/`:

   - `pxe_mapping_file_YYYYMMDDTHHMMSS.csv`
   - `xnames_YYYYMMDDTHHMMSS.csv`

3. Rename the generated `pxe_mapping_file_YYYYMMDDTHHMMSS.csv` to `pxe_mapping_file.csv`
   in `/opt/omnia/input/project_default/` and update `provision_config.yml` if needed.

4. Run provision:

   ```bash
   ansible-playbook provision/provision.yml
   ```

## Input Format

The admin inventory CSV must contain at least the following columns:

```csv
SERVICE_TAG,ADMIN_MAC,ADMIN_IP,BMC_MAC,BMC_IP,HOSTNAME,FUNCTIONAL_GROUP_NAME,GROUP_NAME,ROW,RACK,USLOT
```

Optional columns:

- `PARENT_SERVICE_TAG`
- `IB_MAC`
- `IB_IP`

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `admin_inventory_path` | `{{ hostvars['localhost']['input_project_dir'] \| default('/opt/omnia/input/project_default') }}/admin_inventory.csv` | Path to admin inventory CSV |
| `magellan_output_dir` | `/opt/omnia/input/project_default` | Output directory for generated files |

## Output

Timestamped mapping files are written to `/opt/omnia/input/project_default`:

- `pxe_mapping_file_YYYYMMDDTHHMMSS.csv`
- `xnames_YYYYMMDDTHHMMSS.csv`
