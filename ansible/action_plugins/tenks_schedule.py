# Copyright (c) 2018 StackHPC Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

# Avoid shadowing of system copy module by copy action plugin.
from __future__ import absolute_import
from copy import deepcopy

from ansible.errors import AnsibleActionFail
from ansible.module_utils._text import to_text
from ansible.plugins.action import ActionBase
import six


class ActionModule(ActionBase):

    def run(self, tmp=None, task_vars=None):
        """
        Schedule specifications of nodes by type onto hypervisors.

        The following task vars are accepted:
            :hypervisor_vars: A dict of hostvars for each hypervisor, keyed
                              by hypervisor hostname. Required.
            :specs: A list of node specifications to be instantiated. Required.
            :node_types: A dict mapping node type names to a dict of properties
                         of that type.
            :node_name_prefix: A string with which to prefix all sequential
                               node names.
            :vol_name_prefix: A string with which to prefix all sequential
                              volume names.
        :returns: A dict containing lists of node details, keyed by the
                  hostname of the hypervisor to which they are scheduled.
        """
        result = super(ActionModule, self).run(tmp, task_vars)
        # Initialise our return dict.
        result['result'] = {}
        del tmp  # tmp no longer has any effect
        self._validate_vars(task_vars)

        idx = 0
        hypervisor_names = task_vars['hypervisor_vars'].keys()
        for spec in task_vars['specs']:
            try:
                typ = spec['type']
                cnt = spec['count']
            except KeyError:
                e = ("All specs must contain a `type` and a `count`. "
                     "Offending spec: %s" % spec)
                raise AnsibleActionFail(to_text(e))
            for _ in six.moves.range(cnt):
                node = deepcopy(task_vars['node_types'][typ])
                # All nodes need an Ironic driver.
                node.setdefault('ironic_driver',
                                task_vars['hostvars']['localhost'][
                                    'default_ironic_driver'])
                # Set the type, for future reference.
                node['type'] = typ
                # Sequentially number the node and volume names.
                node['name'] = "%s%d" % (task_vars['node_name_prefix'], idx)
                for vol_idx, vol in enumerate(node['volumes']):
                    vol['name'] = "%s%d" % (task_vars['vol_name_prefix'],
                                            vol_idx)
                try:
                    node['ironic_config'] = spec['ironic_config']
                except KeyError:
                    # Ironic config is not mandatory.
                    pass
                # Perform round-robin scheduling with node index modulo number
                # of hypervisors.
                hyp_name = hypervisor_names[idx % len(hypervisor_names)]
                try:
                    result['result'][hyp_name].append(node)
                except KeyError:
                    # This hypervisor doesn't yet have any scheduled nodes.
                    result['result'][hyp_name] = [node]
                idx += 1
        return result

    def _validate_vars(self, task_vars):
        if task_vars is None:
            task_vars = {}

        REQUIRED_TASK_VARS = {'hypervisor_vars', 'specs', 'node_types'}
        # Var names and their defaults.
        OPTIONAL_TASK_VARS = {
            ('node_name_prefix', 'tk'),
            ('vol_name_prefix', 'vol'),
        }
        for var in REQUIRED_TASK_VARS:
            if var not in task_vars:
                e = "The parameter '%s' must be specified." % var
                raise AnsibleActionFail(to_text(e))

        for var in OPTIONAL_TASK_VARS:
            if var[0] not in task_vars:
                task_vars[var[0]] = var[1]

        if not task_vars['hypervisor_vars']:
            e = ("There are no hosts in the 'hypervisors' group to which we "
                 "can schedule.")
            raise AnsibleActionFail(to_text(e))
