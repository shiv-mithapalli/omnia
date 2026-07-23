# Copyright 2025 Dell Inc. or its subsidiaries. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# pylint: disable=import-error,no-name-in-module,line-too-long
#!/usr/bin/python

import os
import pandas as pd
from ansible.module_utils.basic import AnsibleModule

def generate_xname_in_mapping_file(mapping_file_path, module):
    """
    Generates xname in mapping file:
    Parameters:
        mapping_file_path (str): The path to the mapping file.
        module (AnsibleModule): The Ansible module instance for handling exit and failure.
    """
    try:
        csv_file = pd.read_csv(mapping_file_path)
        if len(csv_file) == 0:
            module.fail_json(msg="Please provide details in mapping file.")

        # Strip whitespace from column values and names
        csv_file = csv_file.apply(lambda x: x.str.strip() if x.dtype == 'object' else x)
 
        # Derive the path to xnames.csv from the same directory as the mapping file.
        # idrac_discover.py is responsible for generating this file before provision.yml runs.
        xnames_file_path = os.path.join(os.path.dirname(mapping_file_path), "xnames.csv")

        # Fallback xname generation if xnames.csv is not present.
        if not os.path.exists(xnames_file_path):
            xname_values = []
            for i in range(len(csv_file)):
                # `c` will be based on i // 100 (every 100 entries we increment `c`)
                c_index = i // 100
                # `s` will be based on i // 10 (every 10 entries we increment `s`)
                s_index = (i // 10) % 10
                # `digit` cycles from 0 to 9
                digit = i % 10
                # Build the 'xname' with updated logic for `c` and `s` indices
                xname = f'x1000c{c_index}s{s_index}b{digit}n0'
                xname_values.append(xname)

            csv_file["XNAME"] = xname_values
            csv_file.to_csv(mapping_file_path, index=False)
            module.exit_json(changed=True, msg="Xnames are generated successfully in the mapping file using fallback logic.")

        # Load xnames.csv and trim whitespace so IP-based lookups are reliable.
        xnames_csv = pd.read_csv(xnames_file_path)
        xnames_csv = xnames_csv.apply(lambda x: x.str.strip() if x.dtype == 'object' else x)

        # Validate the expected columns are present before attempting lookups.
        if "BMC_IP" not in xnames_csv.columns or "XNAME" not in xnames_csv.columns:
            module.fail_json(
                msg=f"xnames.csv at {xnames_file_path} must contain BMC_IP and XNAME columns."
            )

        # Build a lookup table: each configured BMC IP maps to its physical-location xname.
        xname_map = dict(zip(xnames_csv["BMC_IP"], xnames_csv["XNAME"]))

        # Compare the sets of BMC IPs between the mapping file and xnames.csv.
        # Perfect one-to-one correspondence is required to avoid mismatched hardware metadata.
        mapping_bmc_ips = set(csv_file["BMC_IP"])
        xnames_bmc_ips = set(xname_map.keys())

        missing_in_xnames = mapping_bmc_ips - xnames_bmc_ips
        if missing_in_xnames:
            module.fail_json(
                msg="The following BMC_IPs from the mapping file were not found in xnames.csv: "
                    f"{', '.join(sorted(missing_in_xnames))}"
            )

        extra_in_xnames = xnames_bmc_ips - mapping_bmc_ips
        if extra_in_xnames:
            module.fail_json(
                msg="The following BMC_IPs in xnames.csv were not found in the mapping file: "
                    f"{', '.join(sorted(extra_in_xnames))}"
            )

        # Populate the XNAME column by looking up each mapping row's BMC_IP in xname_map.
        csv_file["XNAME"] = csv_file["BMC_IP"].map(xname_map)

        # Reject duplicate XNAMEs. If the same xname is assigned to multiple
        # BMCs, SMD will later refuse the second RedfishEndpoint/Component.
        dup_xnames = csv_file[csv_file["XNAME"].duplicated(keep=False)]["XNAME"].unique().tolist()
        if dup_xnames:
            module.fail_json(
                msg="Duplicate XNAME values found in the mapping file: "
                    f"{', '.join(sorted(dup_xnames))}"
            )

        # Persist the enriched mapping file back to disk.
        csv_file.to_csv(mapping_file_path, index=False)

        # If all checks pass
        module.exit_json(changed=True, msg="Xnames are generated successfully in the mapping file.")

    except Exception as e:
        module.fail_json(msg=str(e))

def main():
    """
	Validate a mapping file.

	Parameters:
		mapping_file_path (str): The path to the mapping file.

	"""
    module_args = {
        'mapping_file_path': {'type': 'path', 'required': True }
    }

    module = AnsibleModule(argument_spec=module_args, supports_check_mode=False)
    mapping_file_path = module.params.get('mapping_file_path')

    generate_xname_in_mapping_file(mapping_file_path, module)


if __name__ == "__main__":
    main()
