import logging
import copy
import json
from constants import *
from pycalico.datastore import DatastoreClient
from pycalico.datastore_datatypes import Endpoint
from netaddr import IPAddress


_log = logging.getLogger("__main__")
client = DatastoreClient()

label_cache = {}
endpoint_cache = {}


def parse_pod(pod):
    """
    Return the labels for this pod.
    """
    # Get Kubernetes labels.
    labels = pod["metadata"].get("labels", {})

    # Extract information.
    namespace = pod["metadata"]["namespace"]
    name = pod["metadata"]["name"]
    workload_id = "%s.%s" % (namespace, name)

    # Add a special label for the Kubernetes namespace.  This is used
    # by selector-based policies to select all pods in a given namespace.
    labels[K8S_NAMESPACE_LABEL] = namespace

    return workload_id, namespace, name, labels


def add_pod(pod):
    """
    Called when a Pod update with type ADDED is received.

    Simply store the pod's labels in the label cache so that we
    can accurately determine if an endpoint must be updated on subsequent
    updates.  The Calico CNI plugin has already configured this pod's
    endpoint with the correct labels, so we don't need to modify the
    endpoint object.
    """
    workload_id, _, _, labels = parse_pod(pod)
    label_cache[workload_id] = labels
    _log.debug("Updated label cache with %s: %s", workload_id, labels)


def update_pod(pod):
    """
    Called when a Pod update with type MODIFIED is received.

    Compares if the labels have changed.  If they have, updates
    the Calico endpoint for this pod.
    """
    # Get Kubernetes labels and metadata.
    workload_id, namespace, name, labels = parse_pod(pod)
    _log.debug("Updating pod: %s", workload_id)

    # Check if the labels have changed for this pod.  If they haven't,
    # do nothing.
    old_labels = label_cache.get(workload_id)
    _log.debug("Compare labels on %s. cached: %s, new: %s",
               workload_id, old_labels, labels)
    if old_labels == labels:
        _log.debug("Ignoring updated for %s with no label change", workload_id)
        return

    # Labels have changed.
    # Check our cache to see if we already know about this endpoint.  If not,
    # re-load the entire cache from etcd and try again.
    _log.info("Labels for %s have been updated", workload_id)
    endpoint = endpoint_cache.get(workload_id)
    if not endpoint:
        # No endpoint in our cache.
        _log.info("No endpoint for %s in cache, loading", workload_id)
        load_caches()
        endpoint = endpoint_cache.get(workload_id)
        if not endpoint:
            # No endpoint in etcd - this means the pod hasn't been
            # created by the CNI plugin yet.  Just wait until it has been.
            # This can only be hit when labels for a pod change before
            # the pod has been deployed, so should be pretty uncommon.
            _log.info("No endpoint for pod %s - wait for creation",
                      workload_id)
            return
    _log.debug("Found endpoint for %s", workload_id)

    # Update the labels on the endpoint.
    endpoint.labels = labels
    endpoint.process_labels()
    client.set_endpoint(endpoint)

    # Update the label cache with the new labels.
    label_cache[workload_id] = labels

    # Update the endpoint cache with the modified endpoint.
    endpoint_cache[workload_id] = endpoint


class KDEndpoint(Endpoint):
    """Slightly patched Endpoint class with support for ipv4_nat field.
    This field is needed to provide proper source IP for outgoing packets
    from pods with public IP's.
    """
    def __init__(self, *args, **kwargs):
        super(KDEndpoint, self).__init__(*args, **kwargs)
        self.ipv4_nat = None

    def to_json(self):
        data = json.loads(super(KDEndpoint, self).to_json())
        if self.ipv4_nat:
            data['ipv4_nat'] = self.ipv4_nat
        return json.dumps(data)

    @classmethod
    def from_endpoint(cls, ep):
        """Creates KDEndpoint object from given Endpoint object
        :param ep: object of class Endpoint
        """
        kd_ep = cls(ep.hostname, ep.orchestrator_id, ep.workload_id,
                    ep.endpoint_id, ep.state, ep.mac)
        kd_ep.name = ep.name
        kd_ep.ipv4_nets = copy.deepcopy(ep.ipv4_nets)
        kd_ep.ipv6_nets = copy.deepcopy(ep.ipv6_nets)
        kd_ep.profile_ids = copy.deepcopy(ep.profile_ids)
        kd_ep._original_json = ep._original_json
        kd_ep.labels = copy.deepcopy(ep.labels)
        kd_ep.process_labels()
        return kd_ep

    def process_labels(self):
        if not self.labels:
            self.ipv4_nat = None
            return
        kd_public_ip = self.labels.get('kuberdock-public-ip', None)
        try:
            # ensure ip address is valid
            IPAddress(kd_public_ip)
        except:
            _log.info("Invalid kuberdock-public-ip: {}".format(kd_public_ip))
            kd_public_ip = None
        if kd_public_ip:
            self.ipv4_nat = [
                {"int_ip": str(ip_net.ip), "ext_ip": kd_public_ip}
                for ip_net in self.ipv4_nets
            ]
        else:
            self.ipv4_nat = None


def load_caches():
    """
    Loads endpoint and label caches from etcd.

    We need to do this when we've received a MODIFIED event
    indicating that labels have changed for a pod that is not
    already in our cache. This can also happen if there are no labels
    cached for the MODIFIED pod.
    """
    endpoints = [KDEndpoint.from_endpoint(ep)
                 for ep in client.get_endpoints(orchestrator_id="k8s")]
    for ep in endpoints:
        endpoint_cache[ep.workload_id] = ep
        label_cache[ep.workload_id] = ep.labels
    _log.info("Loaded endpoint and label caches")


def delete_pod(pod):
    """
    We don't need to do anything when a pod is deleted - the CNI plugin
    handles the deletion of the endpoint.  Just update the caches.
    """
    # Extract information.
    workload_id, _, _, _ = parse_pod(pod)
    _log.debug("Pod deleted: %s", workload_id)

    # Delete from label cache.
    try:
        del label_cache[workload_id]
        _log.debug("Removed %s from label cache", workload_id)
    except KeyError:
        pass

    # Delete from endpoint cache.
    try:
        del endpoint_cache[workload_id]
        _log.debug("Removed %s from endpoint cache", workload_id)
    except KeyError:
        pass
