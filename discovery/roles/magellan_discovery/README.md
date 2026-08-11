# Magellan Discovery Role

## Overview

Generates Omnia PXE mapping and xnames files from a user-provided admin
inventory CSV. This is the "Magellan" discovery mechanism used when running
`discovery.yml` with `discovery_mechanism=magellan`.

## Purpose

- Read the admin inventory CSV (`admin_inventory_path`)
- Validate and expand the sparse CSV to a complete inventory with `BMC_IP` and `USLOT`
- Query each iDRAC via Redfish for service tag, BMC MAC, Ethernet MAC, and InfiniBand NIC details
- Generate timestamped `bmc_pxe_mapping_file_<timestamp>.csv`
- Generate timestamped `xnames_mapping_file_<timestamp>.csv` (when location data is present)
- Generate a timestamped BMC discovery report

## Workflow

1. Run discovery:

   ```bash
   ansible-playbook discovery/discovery.yml -e "discovery_mechanism=magellan"
   ```

2. Review and edit the generated files in `/opt/omnia/input/project_default/`:

   - `bmc_pxe_mapping_file_YYYYMMDDTHHMMSS.csv`
   - `xnames_mapping_file_YYYYMMDDTHHMMSS.csv`

3. Copy the reviewed files to the non-timestamped names used by `provision.yml`:

   ```bash
   cd /opt/omnia/input/project_default
   cp bmc_pxe_mapping_file_YYYYMMDDTHHMMSS.csv pxe_mapping_file.csv
   cp xnames_mapping_file_YYYYMMDDTHHMMSS.csv xnames_mapping_file.csv
   ```

4. Update the following parameter in `provision_config.yml`:

   ```yaml
   pxe_mapping_file_path: "/opt/omnia/input/project_default/pxe_mapping_file.csv"
   ```

5. Run provision:

   ```bash
   ansible-playbook provision/provision.yml
   ```

## Input Format

The admin inventory CSV must contain the following sparse columns:

```csv
SERVICE_TAG,GROUP_NAME,FUNCTIONAL_GROUP_NAME,ROW,RACK,USLOT,RANGE
```

- `USLOT` may be left empty to be auto-assigned per `(ROW, RACK)`.
- `ROW` and `RACK` may be left empty for rows where location data is not required; xnames generation is skipped for those rows.
- `RANGE` must be an IPv4 range in `start-end` format (e.g. `172.27.5.1-172.27.5.254`).

The role validates the sparse input, expands it to a complete inventory
(`admin_complete_inventory.csv`) with derived `BMC_IP` and `USLOT` values, and then
queries each iDRAC via Redfish for MAC/IB NIC details.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `admin_inventory_path` | `{{ hostvars['localhost']['input_project_dir'] \| default('/opt/omnia/input/project_default') }}/admin_inventory.csv` | Path to sparse admin inventory CSV |
| `admin_complete_inventory_path` | `{{ hostvars['localhost']['input_project_dir'] \| default('/opt/omnia/input/project_default') }}/admin_complete_inventory.csv` | Path to the generated complete inventory CSV |
| `pxe_mapping_output_file` | `{{ input_project_dir }}/bmc_pxe_mapping_file_<timestamp>.csv` | Timestamped PXE mapping output file |
| `xnames_mapping_file` | `{{ input_project_dir }}/xnames_mapping_file_<timestamp>.csv` | Timestamped xnames mapping output file |

## Output

Timestamped mapping files are written to the project input directory (default `/opt/omnia/input/project_default`):

- `bmc_pxe_mapping_file_YYYYMMDDTHHMMSS.csv`
- `xnames_mapping_file_YYYYMMDDTHHMMSS.csv`
