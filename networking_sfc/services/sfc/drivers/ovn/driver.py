# Copyright 2016 All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import netaddr

# from eventlet import greenthread

from neutron.common import constants as nc_const
from neutron.common import rpc as n_rpc
from neutron import context as n_context
from neutron.db import api as db_api
from neutron import manager

from neutron.i18n import _LE
from neutron.i18n import _LW

from neutron.agent.ovsdb.native import idlutils
from neutron.plugins.common import constants as np_const

from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_serialization import jsonutils

from networking_sfc.extensions import flowclassifier
from networking_sfc.extensions import sfc
from networking_sfc.services.sfc.common import exceptions as exc
from networking_sfc.services.sfc.drivers import base as driver_base
from networking_sfc.services.sfc.drivers.ovs import(
    db as ovs_sfc_db)
from networking_sfc.services.sfc.drivers.ovs import (
    constants as ovs_const)

##from networking_ovn.common import config
#from networking_ovn.common import constants as ovn_const
#from networking_ovn.common import utils
from networking_ovn.ml2 import mech_driver

LOG = logging.getLogger(__name__)


class OVNSfcDriver(driver_base.SfcDriverBase,
                   ovs_sfc_db.OVSSfcDriverDB):
    """Sfc Driver Base Class."""

    def initialize(self):
        super(OVNSfcDriver, self).initialize()

        self.id_pool = ovs_sfc_db.IDAllocation(self.admin_context)


    def _get_subnet(self, core_plugin, tenant_id, cidr):
        filters = {'tenant_id': [tenant_id]}
        subnets = core_plugin.get_subnets(self.admin_context, filters=filters)
        cidr_set = netaddr.IPSet([cidr])

        for subnet in subnets:
            subnet_cidr_set = netaddr.IPSet([subnet['cidr']])
            if cidr_set.issubset(subnet_cidr_set):
                return subnet

    def _get_fc_dst_subnet_gw_port(self, fc):
        core_plugin = manager.NeutronManager.get_plugin()
        subnet = self._get_subnet(core_plugin,
                                  fc['tenant_id'],
                                  fc['destination_ip_prefix'])

        return self._get_port_subnet_gw_info(core_plugin, subnet)

    def _get_port_subnet_gw_info_by_port_id(self, id):
        core_plugin = manager.NeutronManager.get_plugin()
        subnet = self._get_subnet_by_port(core_plugin, id)
        return self._get_port_subnet_gw_info(core_plugin,
                                             subnet)

    def _get_port_subnet_gw_info(self, core_plugin, subnet):
        if (subnet != None):
            filters = dict(fixed_ips=dict(subnet_id=[subnet["id"]]),
                       tenant_id=[subnet['tenant_id']])
            gw_ports = core_plugin.get_ports(self.admin_context, filters=filters)
            if gw_ports is not None:
                for gw_port in gw_ports:
                    if gw_port['device_owner'] in nc_const.ROUTER_INTERFACE_OWNERS:
                        return (gw_port['mac_address'],
                            subnet['cidr'],
                            subnet['network_id'])
            raise exc.SfcNoSubnetGateway(
                type='subnet gateway',
                cidr=subnet['cidr'])
        else:
            return None,None,None

    def _get_subnet_by_port(self, core_plugin, id):
        subnet = None
        port = core_plugin.get_port(self.admin_context, id)
        for ip in port['fixed_ips']:
            subnet = core_plugin.get_subnet(self.admin_context,
                                            ip["subnet_id"])
            # currently only support one subnet for a port
            break

        return subnet

    @log_helpers.log_method_call
    def _get_network_other_active_entry_count(self, host, remote_port_id):
        agent_active_ports = 0
        port_detail = self.get_port_detail_by_filter(
            dict(ingress=remote_port_id))
        for assoc in port_detail['path_nodes']:
            node = self.get_path_node(assoc['pathnode_id'])
            if node['node_type'] != ovs_const.SRC_NODE:
                filter = dict(nsp=node['nsp'],
                              nsi=node['nsi'] + 1)
                pre_node = self.get_path_node_by_filter(filter)
                if not pre_node:
                    continue
                for each in pre_node['portpair_details']:
                    pre_port = self.get_port_detail_by_filter(dict(id=each))
                    if host == pre_port['host_id']:
                        agent_active_ports += 1

        return agent_active_ports

    @log_helpers.log_method_call
    def _get_portpair_ids(self, context, pg_id):
        next_group_members = []
        group_intid = self.id_pool.get_intid_by_uuid('group', pg_id)
        LOG.debug('group_intid: %s', group_intid)
        pg = context._plugin.get_port_pair_group(context._plugin_context,
                                                 pg_id)
        LOG.debug("Port Pairs: %s" % pg['port_pairs'])
        return pg['port_pairs']

    @log_helpers.log_method_call
    def _get_portgroup_members(self, context, pg_id):
        next_group_members = []
        group_intid = self.id_pool.get_intid_by_uuid('group', pg_id)
        LOG.debug('group_intid: %s', group_intid)
        pg = context._plugin.get_port_pair_group(context._plugin_context,
                                                 pg_id)
        LOG.debug("Port Pairs: %s" % pg['port_pairs'])
        for pp_id in pg['port_pairs']:
            pp = context._plugin.get_port_pair(context._plugin_context, pp_id)
            filters = {}
            if pp.get('ingress', None):
                filters = dict(dict(ingress=pp['ingress']), **filters)
            if pp.get('egress', None):
                filters = dict(dict(egress=pp['egress']), **filters)
            pd = self.get_port_detail_by_filter(filters)
            if pd:
                next_group_members.append(
                    dict(portpair_id=pd['id'], weight=1))
        return group_intid, next_group_members

    def _get_port_pair_detail(self, context, port_pair_id):
        pp = context._plugin.get_port_pair(context._plugin_context,
                                           port_pair_id)
        return pp

    def _get_port_pair_detail_by_port_pair(self, context, port_pair_id):
        pp = context._plugin.get_port_pair(context._plugin_context,
                                           port_pair_id)
        filters = {}
        if pp.get('ingress', None):
            filters = dict(dict(ingress=pp['ingress']), **filters)
        if pp.get('egress', None):
            filters = dict(dict(egress=pp['egress']), **filters)
        pd = self.get_port_detail_by_filter(filters)

        return pd

    @log_helpers.log_method_call
    def _add_flowclassifier_port_assoc(self, fc_ids, tenant_id,
                                       src_node, dst_node,
                                       last_sf_node=None):
        dst_ports = []
        for fc in self._get_fcs_by_ids(fc_ids):
            if fc.get('logical_source_port', ''):
                need_assoc = True
                # lookup the source port
                src_pd_filter = dict(egress=fc['logical_source_port'],
                                     tenant_id=tenant_id
                                     )
                src_pd = self.get_port_detail_by_filter(src_pd_filter)

                if not src_pd:
                    # Create source port detail
                    src_pd = self._create_port_detail(src_pd_filter)
                    LOG.debug('create src port detail: %s', src_pd)
                else:
                    for path_node in src_pd['path_nodes']:
                        if path_node['pathnode_id'] == src_node['id']:
                            need_assoc = False
                if need_assoc:
                    # Create associate relationship
                    assco_args = {'portpair_id': src_pd['id'],
                                  'pathnode_id': src_node['id'],
                                  'weight': 1,
                                  }
                    sna = self.create_pathport_assoc(assco_args)
                    LOG.debug('create assoc src port with node: %s', sna)
                    src_node['portpair_details'].append(src_pd['id'])

            if fc.get('logical_destination_port', ''):
                need_assoc = True
                dst_pd_filter = dict(ingress=fc['logical_destination_port'],
                                     tenant_id=tenant_id
                                     )
                dst_pd = self.get_port_detail_by_filter(dst_pd_filter)

                if not dst_pd:
                    # Create dst port detail
                    dst_pd = self._create_port_detail(dst_pd_filter)
                    LOG.debug('create dst port detail: %s', dst_pd)
                else:
                    for path_node in dst_pd['path_nodes']:
                        if path_node['pathnode_id'] == dst_node['id']:
                            need_assoc = False
                if need_assoc:
                    # Create associate relationship
                    dst_assco_args = {'portpair_id': dst_pd['id'],
                                      'pathnode_id': dst_node['id'],
                                      'weight': 1,
                                      }
                    dna = self.create_pathport_assoc(dst_assco_args)
                    LOG.debug('create assoc dst port with node: %s', dna)
                    dst_node['portpair_details'].append(dst_pd['id'])

                dst_ports.append(dict(portpair_id=dst_pd['id'], weight=1))

        if last_sf_node and dst_ports is not None:
            if last_sf_node['next_hop']:
                next_hops = jsonutils.loads(last_sf_node['next_hop'])
                next_hops.extend(dst_ports)
                last_sf_node['next_hop'] = jsonutils.dumps(next_hops)
            # update nexthop info of pre node
            self.update_path_node(last_sf_node['id'],
                                  last_sf_node)
        return dst_ports

    def _remove_flowclassifier_port_assoc(self, fc_ids, tenant_id,
                                          src_node=None, dst_node=None,
                                          last_sf_node=None):
        update_last_sf = False
        if not fc_ids:
            return
        for fc in self._get_fcs_by_ids(fc_ids):
            if fc.get('logical_source_port', ''):
                # delete source port detail
                src_pd_filter = dict(egress=fc['logical_source_port'],
                                     tenant_id=tenant_id
                                     )
                pds = self.get_port_details_by_filter(src_pd_filter)
                if pds:
                    for pd in pds:
                        # update src_node portpair_details refence info
                        if src_node and pd['id'] in src_node[
                            'portpair_details'
                        ]:
                            src_node['portpair_details'].remove(pd['id'])
                            if len(pd['path_nodes']) == 1:
                                self.delete_port_detail(pd['id'])

            if fc.get('logical_destination_port', ''):
                # Create dst port detail
                dst_pd_filter = dict(ingress=fc['logical_destination_port'],
                                     tenant_id=tenant_id
                                     )
                pds = self.get_port_details_by_filter(dst_pd_filter)
                if pds:
                    for pd in pds:
                        # update dst_node portpair_details refence info
                        if dst_node and pd['id'] in dst_node[
                            'portpair_details'
                        ]:
                            # update portpair_details of this node
                            dst_node['portpair_details'].remove(pd['id'])
                            # update last hop(SF-group) next hop info
                            if last_sf_node:
                                next_hop = dict(portpair_id=pd['id'],
                                                weight=1)
                                next_hops = jsonutils.loads(
                                    last_sf_node['next_hop'])
                                next_hops.remove(next_hop)
                                last_sf_node['next_hop'] = jsonutils.dumps(
                                    next_hops)
                                update_last_sf = True
                            if len(pd['path_nodes']) == 1:
                                self.delete_port_detail(pd['id'])

        if last_sf_node and update_last_sf:
            # update nexthop info of pre node
            self.update_path_node(last_sf_node['id'],
                                  last_sf_node)

    @log_helpers.log_method_call
    def _create_portchain_path(self, context, port_chain):
        src_node, src_pd, dst_node, dst_pd = (({}, ) * 4)
        path_nodes, dst_ports = [], []
        # Create an assoc object for chain_id and path_id
        # context = context._plugin_context
        path_id = self.id_pool.assign_intid('portchain', port_chain['id'])

        if not path_id:
            LOG.error(_LE('No path_id available for creating port chain path'))
            return

        next_group_intid, next_group_members = self._get_portgroup_members(
            context, port_chain['port_pair_groups'][0])

        port_pair_groups = port_chain['port_pair_groups']
        sf_path_length = len(port_pair_groups)
        # Create a head node object for port chain
        src_args = {'tenant_id': port_chain['tenant_id'],
                    'node_type': ovs_const.SRC_NODE,
                    'nsp': path_id,
                    'nsi': 0xff,
                    'portchain_id': port_chain['id'],
                    'status': ovs_const.STATUS_BUILDING,
                    'next_group_id': next_group_intid,
                    'next_hop': jsonutils.dumps(next_group_members),
                    }
        src_node = self.create_path_node(src_args)
        LOG.debug('create src node: %s', src_node)
        path_nodes.append(src_node)

        # Create a destination node object for port chain
        dst_args = {
            'tenant_id': port_chain['tenant_id'],
            'node_type': ovs_const.DST_NODE,
            'nsp': path_id,
            'nsi': 0xff - sf_path_length - 1,
            'portchain_id': port_chain['id'],
            'status': ovs_const.STATUS_BUILDING,
            'next_group_id': None,
            'next_hop': None
        }
        dst_node = self.create_path_node(dst_args)
        LOG.debug('create dst node: %s', dst_node)
        path_nodes.append(dst_node)

        dst_ports = self._add_flowclassifier_port_assoc(
            port_chain['flow_classifiers'],
            port_chain['tenant_id'],
            src_node,
            dst_node
        )

        for i in range(sf_path_length):
            cur_group_members = next_group_members
            # next_group for next hop
            if i < sf_path_length - 1:
                next_group_intid, next_group_members = (
                    self._get_portgroup_members(
                        context, port_pair_groups[i + 1])
                )
            else:
                next_group_intid = None
                next_group_members = None if not dst_ports else dst_ports

            # Create a node object
            node_args = {
                'tenant_id': port_chain['tenant_id'],
                'node_type': ovs_const.SF_NODE,
                'nsp': path_id,
                'nsi': 0xfe - i,
                'portchain_id': port_chain['id'],
                'status': ovs_const.STATUS_BUILDING,
                'next_group_id': next_group_intid,
                'next_hop': (
                    None if not next_group_members else
                    jsonutils.dumps(next_group_members)
                )
            }
            sf_node = self.create_path_node(node_args)
            LOG.debug('chain path node: %s', sf_node)
            # Create the assocation objects that combine the pathnode_id with
            # the ingress of the port_pairs in the current group
            # when port_group does not reach tail
            for member in cur_group_members:
                assco_args = {'portpair_id': member['portpair_id'],
                              'pathnode_id': sf_node['id'],
                              'weight': member['weight'], }
                sfna = self.create_pathport_assoc(assco_args)
                LOG.debug('create assoc port with node: %s', sfna)
                sf_node['portpair_details'].append(member['portpair_id'])
            path_nodes.append(sf_node)

        return path_nodes

    def _delete_path_node_port_flowrule(self, node, port, fc_ids):
        # if this port is not binding, don't to generate flow rule
        if not port['host_id']:
            return
        flow_rule = self._build_portchain_flowrule_body(
            node,
            port,
            None,
            fc_ids)

        # TODO Add delete operation

    def _delete_path_node_flowrule(self, node, fc_ids):
        if node['portpair_details'] is None:
            return
        for each in node['portpair_details']:
            port = self.get_port_detail_by_filter(dict(id=each))
            if port:
                self._delete_path_node_port_flowrule(
                    node, port, fc_ids)

    @log_helpers.log_method_call
    def _delete_portchain_path(self, context, portchain_id):
        port_chain = context.current
        first = self.get_path_node_by_filter(
            filters={
                'portchain_id': portchain_id,
                'nsi': 0xff
            }
        )

        # delete flow rules which source port isn't assigned
        # in flow classifier
        if first:
            self._delete_src_node_flowrules(
                first,
                port_chain['flow_classifiers']
            )

        pds = self.get_path_nodes_by_filter(
            dict(portchain_id=portchain_id))
        if pds:
            for pd in pds:
                self._delete_path_node_flowrule(
                    pd,
                    port_chain['flow_classifiers']
                )
            for pd in pds:
                self.delete_path_node(pd['id'])

        # delete the ports on the traffic classifier
        self._remove_flowclassifier_port_assoc(
            port_chain['flow_classifiers'],
            port_chain['tenant_id']
        )

        # Delete the chainpathpair
        intid = self.id_pool.get_intid_by_uuid(
            'portchain', portchain_id)
        self.id_pool.release_intid('portchain', intid)

    def _update_path_node_next_hops(self, flow_rule):
        node_next_hops = []
        if not flow_rule['next_hop']:
            return None
        next_hops = jsonutils.loads(flow_rule['next_hop'])
        if not next_hops:
            return None
        for member in next_hops:
            detail = {}
            port_detail = self.get_port_detail_by_filter(
                dict(id=member['portpair_id']))
            if not port_detail or not port_detail['host_id']:
                continue
            detail['local_endpoint'] = port_detail['local_endpoint']
            detail['weight'] = member['weight']
            detail['mac_address'] = port_detail['mac_address']
            detail['segment_id'] = port_detail['segment_id']
            detail['network_type'] = port_detail['network_type']
            """
            mac, cidr, net_uuid = self._get_port_subnet_gw_info_by_port_id(
                port_detail['ingress']
            )
            detail['gw_mac'] = mac
            detail['cidr'] = cidr
            detail['net_uuid'] = net_uuid
            """
            node_next_hops.append(detail)
        flow_rule['next_hops'] = node_next_hops
        flow_rule.pop('next_hop')

        return node_next_hops

    def _build_portchain_flowrule_body(self, node, port,
                                       add_fc_ids=None, del_fc_ids=None):
        node_info = node.copy()
        node_info.pop('tenant_id')
        node_info.pop('portpair_details')

        port_info = port.copy()
        port_info.pop('tenant_id')
        port_info.pop('id')
        port_info.pop('path_nodes')
        # port_info.pop('host_id')

        flow_rule = dict(node_info, **port_info)
        # if this port is belong to NSH/MPLS-aware vm, only to
        # notify the flow classifier for 1st SF.
        flow_rule['add_fcs'] = self._filter_flow_classifiers(
            flow_rule, add_fc_ids)
        flow_rule['del_fcs'] = self._filter_flow_classifiers(
            flow_rule, del_fc_ids)

        self._update_portchain_group_reference_count(flow_rule,
                                                     port['host_id'])

        # update next hop info
        self._update_path_node_next_hops(flow_rule)

        return flow_rule

    def _filter_flow_classifiers(self, flow_rule, fc_ids):
        """Filter flow classifiers.

        @return: list of the flow classifiers
        """

        fc_return = []

        if not fc_ids:
            return fc_return
        fcs = self._get_fcs_by_ids(fc_ids)
        for fc in fcs:
            new_fc = fc.copy()
            new_fc.pop('id')
            new_fc.pop('name')
            new_fc.pop('tenant_id')
            new_fc.pop('description')

            if ((flow_rule['node_type'] == ovs_const.SRC_NODE and
                 flow_rule['egress'] == fc['logical_source_port']
                 ) or
                (flow_rule['node_type'] == ovs_const.DST_NODE and
                 flow_rule['ingress'] == fc['logical_destination_port']
                 )):
                fc_return.append(new_fc)
            elif flow_rule['node_type'] == ovs_const.SF_NODE:
                fc_return.append(new_fc)

        return fc_return

    def _update_path_node_port_flowrules(self, node, port,
                                         add_fc_ids=None, del_fc_ids=None):
        # if this port is not binding, don't to generate flow rule
        if not port['host_id']:
            return

        flow_rule = self._build_portchain_flowrule_body(
            node,
            port,
            add_fc_ids,
            del_fc_ids)
        #TODO Add update operations
        """
        self.ovs_driver_rpc.ask_agent_to_update_flow_rules(
            self.admin_context,
            flow_rule)

        self._update_agent_fdb_entries(flow_rule)
        """
    def _update_path_node_flowrules(self, node,
                                    add_fc_ids=None, del_fc_ids=None):
        if node['portpair_details'] is None:
            return
        for each in node['portpair_details']:
            port = self.get_port_detail_by_filter(dict(id=each))
            if port:
                self._update_path_node_port_flowrules(
                    node, port, add_fc_ids, del_fc_ids)

    def _thread_update_path_nodes(self, nodes,
                                  add_fc_ids=None, del_fc_ids=None):
        for node in nodes:
            self._update_path_node_flowrules(node, add_fc_ids, del_fc_ids)
        self._update_src_node_flowrules(nodes[0], add_fc_ids, del_fc_ids)

    def _get_portchain_fcs(self, port_chain):
        return self._get_fcs_by_ids(port_chain['flow_classifiers'])

    def _get_fcs_by_ids(self, fc_ids):
        flow_classifiers = []
        if not fc_ids:
            return flow_classifiers

        # Get the portchain flow classifiers
        fc_plugin = (
            manager.NeutronManager.get_service_plugins().get(
                flowclassifier.FLOW_CLASSIFIER_EXT)
        )
        if not fc_plugin:
            LOG.warning(_LW("Not found the flow classifier service plugin"))
            return flow_classifiers

        for fc_id in fc_ids:
            fc = fc_plugin.get_flow_classifier(self.admin_context, fc_id)
            flow_classifiers.append(fc)

        return flow_classifiers

    @log_helpers.log_method_call
    def create_port_chain(self, context,port_chain):
        ovn_dict = {}
        port_chain = context.current
        path_nodes = self._create_portchain_path(context, port_chain)
        #
        # Create OVN dict
        #
        LOG.debug("Context: %s" % context)
        LOG.debug("Path Nodes: %s" % path_nodes)
        LOG.debug("port chain %s" % port_chain)

        LOG.debug("ID is %s" % port_chain['id'])
        LOG.debug("Name is %s" % port_chain['name'])
        #
        # Use the port chain id/name as the identifiers in OVN
        #
        ovn_dict['id'] = port_chain['id']
        ovn_dict['name'] = port_chain['name']
        #
        # Loop over port-pair group gettting individual port-pairs for VNF
        #
        for port_item in port_chain['port_pair_groups']:
            port_pair_id =  self._get_portpair_ids(context,port_item)
            LOG.debug("Port Pair Id: %s " % port_pair_id)
            port_pair_detail = self._get_port_pair_detail(context, port_pair_id)
            LOG.debug("Port Pair Info: %s" % port_pair_detail)
            LOG.debug("Ingress port id: %s " % port_pair_detail['ingress'])
            LOG.debug("Egress port id: %s " % port_pair_detail['egress'])
            #
            # TODO JED 
            # This assumes only one port-pair if their are a 
            # sequence of VNFs need to address or generate error
            #
            ovn_dict['in_port_id'] = port_pair_detail['ingress']
            ovn_dict['out_port_id'] = port_pair_detail['egress']
        #
        # Get flow classifier
        #
        # TODO:
        # Convert classification rules into OVN ACL Rules
        # Still need the destination port id to drive the OVN rules
        #
        fcs = self._get_portchain_fcs(port_chain)
        LOG.debug("FLOW CLASSIFIER %s" % fcs[0])
        app_port =  fcs[0]['logical_source_port']
        LOG.debug("App Port ID %s" % app_port)
        #
        # TODO - specific destination port
        #
        ovn_dict['app_port_id'] = fcs[0]['logical_source_port']
        #
        # Get the network id from the application port
        #
        # TODO: In cases where the VNF and Application are on seperate
        #       networks need to change logic - at minimum add an error
        #       handler that limits VNFs and Applications to same core.
        #
        core_plugin = manager.NeutronManager.get_plugin()
        port = core_plugin.get_port(self.admin_context, app_port)
        LOG.debug("App port Info %s " % port)
        LOG.debug("Network ID %s " % port['network_id'])
        ovn_dict['network_id'] = port['network_id']
        #
        # Create a new set of rules in OVN to insert VNF
        #
        LOG.debug("Create OVN Rules for VNF: %s " % ovn_dict)
        
        #
        # TODO Call OVN and pass in sfc dict struct
        #
        #status = create_ovn_sfc(ovn_dict)
        if (status == False):
            LOG.error("Could not create port_chain in ovn %s: " % ovn_dict)

       
    @log_helpers.log_method_call
    def delete_port_chain(self, context):
        status = True
        port_chain = context.current
        portchain_id = port_chain['id']
        LOG.debug("to delete portchain path")
        #
        # Delete OVN entries
        #status = self._delete_ovn_vnf(context,port_chain)
        if (status == False):
            LOG.error("Failed to delete portchain id: %s" % portchain_id)
        self._delete_portchain_path(context, portchain_id)

    def _get_diff_set(self, orig, cur):
        orig_set = set(item for item in orig)
        cur_set = set(item for item in cur)

        to_del = orig_set.difference(cur_set)
        to_add = cur_set.difference(orig_set)

        return to_del, to_add

    @log_helpers.log_method_call
    def update_port_chain(self, context):
        port_chain = context.current
        orig = context.original

        del_fc_ids, add_fc_ids = self._get_diff_set(
            orig['flow_classifiers'],
            port_chain['flow_classifiers']
        )
        path_nodes = self.get_path_nodes_by_filter(
            dict(portchain_id=port_chain['id'])
        )
        if not path_nodes:
            return

        sort_path_nodes = sorted(path_nodes,
                                 key=lambda x: x['nsi'],
                                 reverse=True)
        if del_fc_ids:
            #TODO Insert lservice update operation
            """
            self._thread_update_path_nodes(sort_path_nodes,
                                           None,
                                           del_fc_ids)
            """
            self._remove_flowclassifier_port_assoc(del_fc_ids,
                                                   port_chain['tenant_id'],
                                                   sort_path_nodes[0],
                                                   sort_path_nodes[-1],
                                                   sort_path_nodes[-2])

        if add_fc_ids:
            self._add_flowclassifier_port_assoc(add_fc_ids,
                                                port_chain['tenant_id'],
                                                sort_path_nodes[0],
                                                sort_path_nodes[-1],
                                                sort_path_nodes[-2])

            # notify agent with async thread
            # current we don't use greenthread.spawn
            #TODO insert ovn service operations
            """
            self._thread_update_path_nodes(sort_path_nodes,
                                           add_fc_ids,
                                           None)
            """
    @log_helpers.log_method_call
    def create_port_pair_group(self, context):
        group = context.current
        self.id_pool.assign_intid('group', group['id'])

    @log_helpers.log_method_call
    def delete_port_pair_group(self, context):
        group = context.current
        group_intid = self.id_pool.get_intid_by_uuid('group', group['id'])
        if group_intid:
            self.id_pool.release_intid('group', group_intid)

    @log_helpers.log_method_call
    def update_port_pair_group(self, context):
        current = context.current
        original = context.original

        if set(current['port_pairs']) == set(original['port_pairs']):
            return

        # Update the path_nodes and flows for each port chain that
        # contains this port_pair_group
        # Note: _get_port_pair_group is temporarily used here.
        ppg_obj = context._plugin._get_port_pair_group(context._plugin_context,
                                                       current['id'])
        port_chains = [assoc.portchain_id for assoc in
                       ppg_obj.chain_group_associations]

        for chain_id in port_chains:
            port_chain = context._plugin.get_port_chain(
                context._plugin_context, chain_id)
            group_intid = self.id_pool.get_intid_by_uuid('group',
                                                         current['id'])
            # Get the previous node
            prev_node = self.get_path_node_by_filter(
                filters={'portchain_id': chain_id,
                         'next_group_id': group_intid})
            if not prev_node:
                continue

            before_update_prev_node = prev_node.copy()
            # Update the previous node
            _, curr_group_members = self._get_portgroup_members(context,
                                                                current['id'])
            prev_node['next_hop'] = (
                jsonutils.dumps(curr_group_members)
                if curr_group_members else None
            )
            # update next hop to database
            self.update_path_node(prev_node['id'], prev_node)
            if prev_node['node_type'] == ovs_const.SRC_NODE:
                self._delete_src_node_flowrules(
                    before_update_prev_node, port_chain['flow_classifiers'])
                self._update_src_node_flowrules(
                    prev_node, port_chain['flow_classifiers'], None)
            self._delete_path_node_flowrule(
                before_update_prev_node, port_chain['flow_classifiers'])
            self._update_path_node_flowrules(
                prev_node, port_chain['flow_classifiers'], None)

            # Update the current node
            # to find the current node by using the node's next_group_id
            # if this node is the last, next_group_id would be None
            curr_pos = port_chain['port_pair_groups'].index(current['id'])
            curr_node = self.get_path_node_by_filter(
                filters={'portchain_id': chain_id,
                         'nsi': 0xfe - curr_pos})
            if not curr_node:
                continue

            # Add the port-pair-details into the current node
            for pp_id in (
                set(current['port_pairs']) - set(original['port_pairs'])
            ):
                ppd = self._get_port_pair_detail_by_port_pair(context,
                                                              pp_id)
                if not ppd:
                    LOG.debug('No port_pair_detail for the port_pair: %s',
                              pp_id)
                    LOG.debug("Failed to update port-pair-group")
                    return

                assco_args = {'portpair_id': ppd['id'],
                              'pathnode_id': curr_node['id'],
                              'weight': 1, }
                self.create_pathport_assoc(assco_args)
                self._update_path_node_port_flowrules(
                    curr_node, ppd, port_chain['flow_classifiers'])

            # Delete the port-pair-details from the current node
            for pp_id in (
                set(original['port_pairs']) - set(current['port_pairs'])
            ):
                ppd = self._get_port_pair_detail_by_port_pair(context,
                                                              pp_id)
                if not ppd:
                    LOG.debug('No port_pair_detail for the port_pair: %s',
                              pp_id)
                    LOG.debug("Failed to update port-pair-group")
                    return
                self._delete_path_node_port_flowrule(
                    curr_node, ppd, port_chain['flow_classifiers'])
                self.delete_pathport_assoc(curr_node['id'], ppd['id'])

    @log_helpers.log_method_call
    def _get_portpair_detail_info(self, portpair_id):
        """Get port detail.

        @param: portpair_id: uuid
        @return: (host_id, local_ip, network_type, segment_id,
        service_insert_type): tuple
        """

        core_plugin = manager.NeutronManager.get_plugin()
        port_detail = core_plugin.get_port(self.admin_context, portpair_id)
        host_id, local_ip, network_type, segment_id, mac_address = (
            (None, ) * 5)

        if port_detail:
            host_id = port_detail['binding:host_id']
            network_id = port_detail['network_id']
            mac_address = port_detail['mac_address']
            network_info = core_plugin.get_network(
                self.admin_context, network_id)
            #network_type = network_info['provider:network_type']
            network_type = None
            #segment_id = network_info['provider:segmentation_id']
            segment_id = None
        #if network_type != np_const.TYPE_VXLAN:
        #    LOG.warning(_LW("Currently only support vxlan network"))
        #    return ((None, ) * 5)
        #elif not host_id:
        if not host_id:
            LOG.warning(_LW("This port has not been binding"))
            return ((None, ) * 5)
        else:
            #driver = core_plugin.type_manager.drivers.get(network_type)
            #host_endpoint = driver.obj.get_endpoint_by_host(host_id)
            #local_ip = host_endpoint['ip_address']
            local_ip = None

        return host_id, local_ip, network_type, segment_id, mac_address

    @log_helpers.log_method_call
    def _create_port_detail(self, port_pair):
        # since first node may not assign the ingress port, and last node may
        # not assign the egress port. we use one of the
        # port as the key to get the SF information.
        port = None
        if port_pair.get('ingress', None):
            port = port_pair['ingress']
        elif port_pair.get('egress', None):
            port = port_pair['egress']

        host_id, local_endpoint, network_type, segment_id, mac_address = (
            self._get_portpair_detail_info(port))
        port_detail = {
            'ingress': port_pair.get('ingress', None),
            'egress': port_pair.get('egress', None),
            'tenant_id': port_pair['tenant_id'],
            'host_id': host_id,
            'segment_id': segment_id,
            'network_type': network_type,
            'local_endpoint': local_endpoint,
            'mac_address': mac_address
        }
        r = self.create_port_detail(port_detail)
        LOG.debug('create port detail: %s', r)
        return r

    @log_helpers.log_method_call
    def create_port_pair(self, context):
        port_pair = context.current
        self._create_port_detail(port_pair)

    @log_helpers.log_method_call
    def delete_port_pair(self, context):
        port_pair = context.current

        pd_filter = dict(ingress=port_pair.get('ingress', None),
                         egress=port_pair.get('egress', None),
                         tenant_id=port_pair['tenant_id']
                         )
        pds = self.get_port_details_by_filter(pd_filter)
        if pds:
            for pd in pds:
                self.delete_port_detail(pd['id'])

    @log_helpers.log_method_call
    def update_port_pair(self, context):
        pass

    def get_flowrules_by_host_portid(self, context, host, port_id):
        port_chain_flowrules = []
        sfc_plugin = (
            manager.NeutronManager.get_service_plugins().get(
                sfc.SFC_EXT
            )
        )
        if not sfc_plugin:
            return port_chain_flowrules
        try:
            port_detail_list = []
            # one port only may be in egress/ingress port once time.
            ingress_port = self.get_port_detail_by_filter(
                dict(ingress=port_id))
            egress_port = self.get_port_detail_by_filter(
                dict(egress=port_id))
            if not ingress_port and not egress_port:
                return None
            # SF migrate to other host
            if ingress_port:
                port_detail_list.append(ingress_port)
                if ingress_port['host_id'] != host:
                    ingress_port.update(dict(host_id=host))

            if egress_port:
                port_detail_list.append(egress_port)
                if egress_port['host_id'] != host:
                    egress_port.update(dict(host_id=host))

            # this is a SF if there are both egress and engress.
            for i, ports in enumerate(port_detail_list):
                nodes_assocs = ports['path_nodes']
                for assoc in nodes_assocs:
                    # update current path flow rule
                    node = self.get_path_node(assoc['pathnode_id'])
                    port_chain = sfc_plugin.get_port_chain(
                        context,
                        node['portchain_id'])
                    flow_rule = self._build_portchain_flowrule_body(
                        node,
                        ports,
                        add_fc_ids=port_chain['flow_classifiers']
                    )
                    port_chain_flowrules.append(flow_rule)

                    # update the pre-path node flow rule
                    # if node['node_type'] != ovs_const.SRC_NODE:
                    #    node_filter = dict(nsp=node['nsp'],
                    #                       nsi=node['nsi'] + 1
                    #                       )
                    #    pre_node_list = self.get_path_nodes_by_filter(
                    #        node_filter)
                    #    if not pre_node_list:
                    #        continue
                    #    for pre_node in pre_node_list:
                    #        self._update_path_node_flowrules(
                    #            pre_node,
                    #            add_fc_ids=port_chain['flow_classifiers'])

            return port_chain_flowrules

        except Exception as e:
            LOG.exception(e)
            LOG.error(_LE("get_flowrules_by_host_portid failed"))

    def get_flow_classifier_by_portchain_id(self, context, portchain_id):
        try:
            flow_classifier_list = []
            sfc_plugin = (
                manager.NeutronManager.get_service_plugins().get(
                    sfc.SFC_EXT
                )
            )
            if not sfc_plugin:
                return []

            port_chain = sfc_plugin.get_port_chain(
                context,
                portchain_id)
            flow_classifier_list = self._get_portchain_fcs(port_chain)
            return flow_classifier_list
        except Exception as e:
            LOG.exception(e)
            LOG.error(_LE("get_flow_classifier_by_portchain_id failed"))

    def update_flowrule_status(self, context, id, status):
        try:
            flowrule_status = dict(status=status)
            self.update_path_node(id, flowrule_status)
        except Exception as e:
            LOG.exception(e)
            LOG.error(_LE("update_flowrule_status failed"))

    def _update_src_node_flowrules(self, node,
                                   add_fc_ids=None, del_fc_ids=None):
        flow_rule = self._get_portchain_src_node_flowrule(node,
                                                          add_fc_ids,
                                                          del_fc_ids)
        if not flow_rule:
            return

        core_plugin = manager.NeutronManager.get_plugin()
        #TODO call ovn plugin to update service
        """
        pc_agents = core_plugin.get_agents(
            self.admin_context,
            filters={'agent_type': [nc_const.AGENT_TYPE_OVS]})
        if not pc_agents:
            return

        for agent in pc_agents:
            if agent['alive']:
                # update host info to flow rule
                flow_rule['host'] = agent['host']
                self.ovs_driver_rpc.ask_agent_to_update_src_node_flow_rules(
                    self.admin_context,
                    flow_rule)
        """
    def _delete_src_node_flowrules(self, node, del_fc_ids=None):
        flow_rule = self._get_portchain_src_node_flowrule(node,
                                                          None, del_fc_ids)
        if not flow_rule:
            return

        core_plugin = manager.NeutronManager.get_plugin()
        #TODO Call OVN plugin and delete service
        """
        pc_agents = core_plugin.get_agents(
            self.admin_context, filters={
                'agent_type': [nc_const.AGENT_TYPE_OVS]})
        if not pc_agents:
            return

        for agent in pc_agents:
            if agent['alive']:
                # update host info to flow rule
                self._update_portchain_group_reference_count(flow_rule,
                                                             agent['host'])
                self.ovs_driver_rpc.ask_agent_to_delete_src_node_flow_rules(
                    self.admin_context,
                    flow_rule)
        """
    def get_all_src_node_flowrules(self, context):
        sfc_plugin = (
            manager.NeutronManager.get_service_plugins().get(
                sfc.SFC_EXT
            )
        )
        if not sfc_plugin:
            return []
        try:
            frs = []
            port_chains = sfc_plugin.get_port_chains(context)

            for port_chain in port_chains:
                # get the first node of this chain
                node_filters = dict(portchain_id=port_chain['id'], nsi=0xff)
                portchain_node = self.get_path_node_by_filter(node_filters)
                if not portchain_node:
                    continue
                flow_rule = self._get_portchain_src_node_flowrule(
                    portchain_node,
                    port_chain['flow_classifiers']
                )
                if not flow_rule:
                    continue
                frs.append(flow_rule)
            return frs
        except Exception as e:
            LOG.exception(e)
            LOG.error(_LE("get_all_src_node_flowrules failed"))

    def _get_portchain_src_node_flowrule(self, node,
                                         add_fc_ids=None, del_fc_ids=None):
        try:
            add_fc_rt = []
            del_fc_rt = []

            if add_fc_ids:
                for fc in self._get_fcs_by_ids(add_fc_ids):
                    if not fc.get('logical_source_port', None):
                        add_fc_rt.append(fc)

            if del_fc_ids:
                for fc in self._get_fcs_by_ids(del_fc_ids):
                    if not fc.get('logical_source_port', None):
                        del_fc_rt.append(fc)

            if not add_fc_rt and not del_fc_rt:
                return None

            return self._build_portchain_flowrule_body_without_port(
                node, add_fc_rt, del_fc_rt)
        except Exception as e:
            LOG.exception(e)
            LOG.error(_LE("_get_portchain_src_node_flowrule failed"))

    def _update_portchain_group_reference_count(self, flow_rule, host):
        group_refcnt = 0
        flow_rule['host'] = host

        if flow_rule['next_group_id'] is not None:
            all_nodes = self.get_path_nodes_by_filter(
                filters={'next_group_id': flow_rule['next_group_id'],
                         'nsi': 0xff})
            if all_nodes is not None:
                for node in all_nodes:
                    if not node['portpair_details']:
                        group_refcnt += 1

            port_details = self.get_port_details_by_filter(
                dict(host_id=flow_rule['host']))
            if port_details is not None:
                for pd in port_details:
                    for path in pd['path_nodes']:
                        path_node = self.get_path_node(path['pathnode_id'])
                        if (
                            path_node['next_group_id'] ==
                            flow_rule['next_group_id']
                        ):
                            group_refcnt += 1

        flow_rule['group_refcnt'] = group_refcnt

        return group_refcnt

    def _build_portchain_flowrule_body_without_port(self,
                                                    node,
                                                    add_fcs=None,
                                                    del_fcs=None):
        flow_rule = node.copy()
        flow_rule.pop('tenant_id')
        flow_rule.pop('portpair_details')

        # according to the first sf node get network information
        if not node['next_hop']:
            return None

        flow_rule['ingress'] = None
        flow_rule['egress'] = None
        flow_rule['add_fcs'] = add_fcs
        flow_rule['del_fcs'] = del_fcs

        # update next hop info
        self._update_path_node_next_hops(flow_rule)
        return flow_rule

