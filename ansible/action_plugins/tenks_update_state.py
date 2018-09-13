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
import abc
from copy import deepcopy
import itertools
import re

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

        args = self._task.args
        self._validate_args(args)

        # Modify the state as necessary.
        self._map_physnets(args['state'], args['hypervisor_vars'])
        self._process_specs(task_vars['hostvars']['localhost'], args)

        # Return the modified state.
        result['result'] = args['state']
        return result

    def _map_physnets(self, state, hypervisor_vars):
        """
        Set physnet mappings for each host.

        Use the specified physnet mappings and any existing physnet indices to
        ensure all physnet mappings exist in `state` with an index.
        """
        for hostname, hostvars in six.iteritems(hypervisor_vars):
            # The desired mappings given in the Tenks configuration. These do
            # not include IDXs which are an implementation detail of Tenks.
            specified_mappings = hostvars['physnet_mappings']
            try:
                # The mappings currently in the state file, including IDXs.
                old_mappings = state[hostname]['physnet_mappings']
            except KeyError:
                # The hypervisor is new since the last run.
                state[hostname] = {}
                old_mappings = {}
            # The new set of mappings to store as state.
            new_mappings = {}
            next_idx = 0
            used_idxs = {pn['idx'] for pn in old_mappings.values()}
            for name, dev in six.iteritems(specified_mappings):
                try:
                    # We need to re-use the IDXs of any existing physnets.
                    idx = old_mappings[name]['idx']
                except KeyError:
                    # New physnet requires a new IDX.
                    while next_idx in used_idxs:
                        next_idx += 1
                    used_idxs.add(next_idx)
                    idx = next_idx
                finally:
                    new_mappings[name] = {'idx': idx, 'device': dev}
            state[hostname]['physnet_mappings'] = new_mappings

    def _process_specs(self, localhost_vars, args):
        """
        Ensure the correct nodes are present in `state`.

        Remove unnecessary nodes by marking as 'absent' and schedule new nodes
        to hypervisors such that the nodes in `state` match what's specified in
        `specs`.
        """
        # Iterate through existing nodes, marking for deletion where necessary.
        for hyp in args['state'].values():
            # Anything already marked as 'absent' should no longer exist.
            hyp['nodes'] = [n for n in hyp.get('nodes', [])
                            if n.get('state') != 'absent']
            for node in hyp['nodes']:
                if ((localhost_vars['cmd'] == 'teardown' or
                     not self._tick_off_node(args['specs'], node))):
                    # We need to delete this node, since it exists but does not
                    # fulfil any spec.
                    node['state'] = 'absent'

        # Now create all the required new nodes.
        self._create_nodes(localhost_vars, args)

    def _tick_off_node(self, specs, node):
        """
        Tick off an existing node as fulfilling a node specification.

        If `node` is required in `specs`, decrement that spec's count and
        return True. Otherwise, return False.
        """
        # Attributes that a spec and a node have to have in common for the node
        # to count as an 'instance' of the spec.
        MATCHING_ATTRS = {'type', 'ironic_config'}
        for spec in specs:
            if ((all(spec[attr] == node[attr] for attr in MATCHING_ATTRS) and
                 spec['count'] > 0)):
                spec['count'] -= 1
                return True
        return False

    def _create_nodes(self, localhost_vars, args):
        """
        Create new nodes to fulfil the specs.
        """
        scheduler = RoundRobinScheduler(args['hypervisor_vars'],
                                        args['state'])
        # Anything left in specs needs to be created.
        for spec in args['specs']:
            for _ in six.moves.range(spec['count']):
                node = self._gen_node(localhost_vars, spec['type'],
                                      args, spec.get('ironic_config'))
                hostname, idx = scheduler.choose_host(node)
                # Set node name based on its index.
                node['name'] = "%s%d" % (args['node_name_prefix'], idx)
                # Set IPMI port using its index as an offset from the lowest
                # port.
                node['ipmi_port'] = (
                    args['hypervisor_vars'][hostname][
                        'ipmi_port_range_start'] + idx)
                # Set up node list structure for this host.
                args['state'].setdefault(hostname, {})
                args['state'][hostname].setdefault('nodes', [])
                args['state'][hostname]['nodes'].append(node)

    def _gen_node(self, localhost_vars, type_name, args, ironic_config=None):
        """
        Generate a node description.

        A name will not be assigned at this point because we don't know which
        hypervisor the node will be scheduled to.
        """
        node_type = args['node_types'][type_name]
        node = deepcopy(node_type)
        # All nodes need an Ironic driver.
        node.setdefault(
            'ironic_driver',
            localhost_vars['default_ironic_driver']
        )
        # Set the type name, for future reference.
        node['type'] = type_name
        # Sequentially number the volume names.
        for vol_idx, vol in enumerate(node['volumes']):
            vol['name'] = (
                "%s%d" % (args['vol_name_prefix'], vol_idx))
        # Ironic config is not mandatory.
        if ironic_config:
            node['ironic_config'] = ironic_config
        return node

    def _validate_args(self, args):
        if args is None:
            args = {}

        REQUIRED_ARGS = {'hypervisor_vars', 'specs', 'node_types'}
        # Var names and their defaults.
        OPTIONAL_ARGS = [
            ('node_name_prefix', 'tk'),
            # state is optional, since if this is the first run there won't be
            # any yet.
            ('state', {}),
            ('vol_name_prefix', 'vol'),
        ]
        for arg in REQUIRED_ARGS:
            if arg not in args:
                e = "The parameter '%s' must be specified." % arg
                raise AnsibleActionFail(to_text(e))

        for arg in OPTIONAL_ARGS:
            if arg[0] not in args:
                args[arg[0]] = arg[1]

        if not args['hypervisor_vars']:
            e = ("There are no hosts in the 'hypervisors' group to which we "
                 "can schedule.")
            raise AnsibleActionFail(to_text(e))

        for spec in args['specs']:
            if 'type' not in spec or 'count' not in spec:
                e = ("All specs must contain a `type` and a `count`. "
                     "Offending spec: %s" % spec)
                raise AnsibleActionFail(to_text(e))


@six.add_metaclass(abc.ABCMeta)
class Scheduler():
    """
    Abstract class representing a 'method' of scheduling nodes to hosts.
    """
    def __init__(self, hostvars, state):
        self.hostvars = hostvars
        self.state = state

        self._host_free_idxs = {}

    @abc.abstractmethod
    def choose_host(self, node):
        """Abstract method to choose a host to which we can schedule `node`.

        Returns a tuple of the hostname of the chosen host and the index of
        this node on the host.
        """
        raise NotImplementedError()

    def host_next_idx(self, hostname):
        """
        Return the next available index for a node on this host.

        If the free indices are not cached for this host, they will be
        calculated.

        :param hostname: The name of the host in question
        :returns: The next available index, or None if none is available
        """
        if hostname not in self._host_free_idxs:
            self._calculate_free_idxs(hostname)
        try:
            return self._host_free_idxs[hostname].pop(0)
        except IndexError:
            return None

    def host_passes(self, node, hostname):
        """
        Perform checks to ascertain whether this host can support this node.
        """
        # Check that the host is connected to all physical networks that the
        # node requires.
        return all(pn in self.hostvars[hostname]['physnet_mappings'].keys()
                   for pn in node['physical_networks'])

    def _calculate_free_idxs(self, hostname):
        # The maximum number of nodes this host can have is the number of
        # IPMI ports it has available.
        all_idxs = six.moves.range(
            self.hostvars[hostname]['ipmi_port_range_end'] -
            self.hostvars[hostname]['ipmi_port_range_start'] + 1)
        get_idx = (
            lambda n: int(re.match(r'[A-Za-z]*([0-9]+)$', n).group(1)))
        used_idxs = {get_idx(n['name']) for n in self.state[hostname]['nodes']
                     if n.get('state') != 'absent'}
        self._host_free_idxs[hostname] = sorted([i for i in all_idxs
                                                 if i not in used_idxs])


class RoundRobinScheduler(Scheduler):
    """
    Schedule nodes in a round-robin fashion to hosts.
    """
    def __init__(self, hostvars, state):
        super(RoundRobinScheduler, self).__init__(hostvars, state)
        self.hostvars = hostvars
        self._host_cycle = itertools.cycle(hostvars.keys())

    def choose_host(self, node):
        idx = None
        count = 0
        while idx is None:
            # Ensure we don't get into an infinite loop if no hosts are
            # available.
            if count >= len(self.hostvars):
                e = ("No hypervisors are left that can support the node %s."
                     % node)
                raise AnsibleActionFail(to_text(e))
            count += 1
            hostname = next(self._host_cycle)
            if self.host_passes(node, hostname):
                idx = self.host_next_idx(hostname)
        return hostname, idx
