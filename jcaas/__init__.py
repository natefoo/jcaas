from __future__ import absolute_import

from galaxy.jobs import JobDestination
from .destination.condor import Condor
from .destination.sge import Sge
from .destination.local import Local
from .destination.slurm import Slurm
# from galaxy.jobs.mapper import JobMappingException

import backoff
import heapq
import copy
import itertools
import json
import logging
import os
import requests
import yaml

log = logging.getLogger(__name__)

APP_CONFIG = {
    'use_http': True,
}

CONFIG_PATH = os.environ['JCAAS_CONF']
with open(CONFIG_PATH, 'r') as handle:
    APP_CONFIG.update(yaml.load(handle))

# The default / base specification for the different environments.
with open(APP_CONFIG['jcaas']['dests'], 'r') as handle:
    SPECIFICATIONS = yaml.load(handle)

with open(APP_CONFIG['jcaas']['tools'], 'r') as handle:
    TOOL_DESTINATIONS = yaml.load(handle)

TRAINING_MACHINES = {}

#DEST_CONDOR = Condor()
#DEST_DRMAA = Sge()
#DEST_LOCAL = Local()

DEST_MAP = {
    'condor': Condor,
    'drmaa': Sge,
    'local': Local,
    'slurm': Slurm,
}

JCAAS = None


class JCaaS:

    def __init__(self):
        self.destinations = None
        self.build_destinations()

    def build_destinations(self):
        ct = itertools.count()
        self.destinations = {}
        dest_specs = SPECIFICATIONS['destinations']
        for name, conf in dest_specs.items():
            meta = conf.get('meta', None)
            if meta is True:
                # meta-destination, do not load directly
                continue
            elif meta is not None:
                assert meta in dest_specs, "Unknown meta-destination: %s" % meta
                meta_conf = copy.deepcopy(dest_specs[meta])
                meta_conf.update(conf)
                conf = meta_conf
            else:
                meta = name
            dest = DEST_MAP[meta](name, conf)
            self.destinations[name] = dest
        for name, dest in self.destinations.items():
            for alternative in dest.alternatives:
                assert alternative in self.destinations, \
                    "Alternative defined for destination '%s' is not a defined destination!: %s" % (name, alternative)

    def priority_destination(destinations):
        return heap.nlargest(1, destinations, key=lambda x: self.destinations[x].priority)[0]

    def finalize_spec(self, requested_destination_name, destination, tool_spec):
        env = dict(SPECIFICATIONS.get(requested_destination_name, {'env': {}})['env'])
        params = dict(SPECIFICATIONS.get(requested_destination_name, {'params': {}})['params'])
        # A dictionary that stores the "raw" details that went into the template.
        raw_allocation_details = {}

        # We define the default memory and cores for all jobs. This is
        # semi-internal, and may not be properly propagated to the end tool
        tool_memory = tool_spec.get('mem', 4)
        tool_cores = tool_spec.get('cores', 1)
        # We apply some constraints to these values, to ensure that we do not
        # produce unschedulable jobs, requesting more ram/cpu than is available in a
        # given location. Currently we clamp those values rather than intelligently
        # re-scheduling to a different location due to TaaS constraints.
        tool_memory = min(tool_memory, destination.MAX_MEM)
        tool_cores = min(tool_cores, destination.MAX_CORES)

        kwargs = {
            # Higher numbers are lower priority, like `nice`.
            'PRIORITY': tool_spec.get('priority', 128),
            'MEMORY': str(tool_memory) + 'G',
            'PARALLELISATION': "",
            'NATIVE_SPEC_EXTRA': "",
        }
        # Allow more human-friendly specification
        if 'nativeSpecification' in params:
            params['nativeSpecification'] = params['nativeSpecification'].replace('\n', ' ').strip()

        # We have some destination specific kwargs. `nativeSpecExtra` and `tmp` are only defined for SGE
        kw_update, raw_alloc_update, params_update = destination.custom_spec(tool_spec, params, kwargs, tool_memory, tool_cores)
        kwargs.update(kw_update)
        raw_allocation_details.update(raw_alloc_update)
        params.update(params_update)

        # Update env and params from kwargs.
        env.update(tool_spec.get('env', {}))
        env = {k: str(v).format(**kwargs) for (k, v) in env.items()}
        params.update(tool_spec.get('params', {}))
        params = {k: str(v).format(**kwargs) for (k, v) in params.items()}

        env = [dict(name=k, value=v) for (k, v) in env.items()]
        return env, params, raw_allocation_details

    def handle_downed_runners(self, tool_spec, destination, runner_name):
        """
        In the event that it was going to condor and condor is unavailable,
        re-schedule to sge
        """
        avail = {}
        for name, dest in self.destinations.items():
            avail[name] = dest.is_available()

        if not any(avail.values()):
            # TODO: use JobMappingException?
            raise Exception("All destinations are currently down")

        # TODO: should we do any validation on runner_name?
        if not avail[runner_name] and not destination.alternatives:
            raise Exception("Destination '%s' is down and no alternatives are defined" % runner_name)
        elif not avail[runner_name] and not any(filter(lambda x: avail[x], destination.alternatives)):
            raise Exception("Destination '%s' and all defined alternatives are down: %s" % (runner_name, destination.alternatives))
        elif not avail[runner_name]:
            destination = self.priority_destination(filter(lambda x: avail[x], destination.alternatives))
            runner_name = destination.name
            tool_spec = destination.convert(tool_spec)

        return tool_spec, destination, runner_name

    def gateway(self, tool_id, user_roles, user_email, memory_scale=1.0):
        # Find the 'short' tool ID which is what is used in the .yaml file.
        tool = get_tool_id(tool_id)
        # Pull the tool specification (i.e. job destination configuration for this tool)
        tool_spec = copy.deepcopy(TOOL_DESTINATIONS['tools'].get(tool, {}))

        # Get a reference to the destination
        requested_destination_name = tool_spec.get('runner', SPECIFICATIONS['default_destination'])
        try:
            destination = self.destinations[requested_destination_name]
        except KeyError:
            raise Exception("Invalid destination for tool '%s': %s" % (tool_id, requested_destination_name))
        runner_name = destination.name
        # FIXME: here .eu has a different runner name from dest name, and also allows substrings:
        #if requested_destination_name == 'sge':
        #    destination = DEST_DRMAA
        #    runner_name = 'drmaa'
        #elif 'condor' in requested_destination_name:
        #    destination = DEST_CONDOR
        #    runner_name = 'condor'
        #else:
        #    destination = DEST_LOCAL
        #    runner_name = 'local'

        # Only two tools are truly special.
        # TODO: why isn't this in the config?
        if tool_id in ('upload1', '__SET_METADATA__'):
            destination = DEST_CONDOR
            runner_name = 'condor'

            machine_group = 'upload' if tool_id == 'upload1' else 'metadata'
            tool_spec = {
                'mem': 0.3,
                'runner': 'condor',
                'rank': destination.prefer_machines([machine_group], machine_group=machine_group),
                'env': {
                    'TEMP': '/data/1/galaxy_db/tmp/'
                }
            }

        tool_spec, destination, runner_name = self.handle_downed_runners(tool_spec, destination, runner_name)

        # Send special users to certain destinations temporarily.
        if 'gx-admin-force-jobs-to-condor' in user_roles:
            tool_spec = DEST_CONDOR.convert(tool_spec)
        elif 'gx-admin-force-jobs-to-drmaa' in user_roles:
            tool_spec = DEST_DRMAA.convert(tool_spec)
        elif 'gx-admin-force-jobs-to-local' in user_roles:
            tool_spec = DEST_LOCAL.convert(tool_spec)

        # Handle downed runners

        # Update the tool specification with any training resources that are
        # available. Won't change the runner, just applies restrictions within the
        # runner.
        tool_spec.update(destination.reroute_to_dedicated(tool_spec, user_roles))
        # Increase memory, as necessary.
        tool_spec['mem'] = tool_spec.get('mem', 4) * memory_scale

        # Will raise exception if unacceptable
        tool_spec = enforce_authorization(tool_spec, user_email, user_roles)

        # Now build the full spec
        env, params, _ = self.finalize_spec(runner_name, destination, tool_spec)

        return env, params, runner_name, tool_spec


def get_tool_id(tool_id):
    """
    Convert ``toolshed.g2.bx.psu.edu/repos/devteam/column_maker/Add_a_column1/1.1.0``
    to ``Add_a_column``

    :param str tool_id: a tool id, can be the short kind (e.g. upload1) or the
                        long kind with the full TS path.

    :returns: a short tool ID.
    :rtype: str
    """
    if tool_id.count('/') == 0:
        # E.g. upload1, etc.
        return tool_id

    # what about odd ones.
    if tool_id.count('/') == 5:
        (server, _, owner, repo, name, version) = tool_id.split('/')
        return name

    # No idea what this is.
    log.warning("Strange tool ID (%s), runner was not sure how to handle it.\n", tool_id)
    return tool_id


def name_it(tool_spec):
    if 'cores' in tool_spec:
        name = '%scores_%sG' % (tool_spec.get('cores', 1), tool_spec.get('mem', 4))
    elif len(tool_spec.keys()) == 0 or (len(tool_spec.keys()) == 1 and 'runner' in tool_spec):
        name = '%s_default' % tool_spec.get('runner', 'sge')
    else:
        name = '%sG_memory' % tool_spec.get('mem', 4)

    if tool_spec.get('tmp', None) == 'large':
        name += '_large'

    if 'name' in tool_spec:
        name += '_' + tool_spec['name']

    return name


def enforce_authorization(tool_spec, user_email, user_roles):
    if 'authorized_users' not in tool_spec:
        return tool_spec

    authorized_users = tool_spec['authorized_users']
    if user_email not in authorized_users:
        raise Exception("Unauthorized")

    del tool_spec['authorized_users']
    return tool_spec


@backoff.on_exception(backoff.fibo,
                      # Parent class of all requests exceptions, should catch
                      # everything.
                      requests.exceptions.RequestException,
                      # https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
                      jitter=backoff.full_jitter,
                      max_tries=8)
def _gateway_http(self, tool_id, user_roles, user_email, memory_scale=1.0):
    payload = {
        'tool_id': tool_id,
        'user_roles': user_roles,
        'email': user_email,
    }
    r = requests.post('http://127.0.0.1:8090', data=json.dumps(payload), timeout=1, headers={'Content-Type': 'application/json'})
    data = r.json()
    return data['env'], data['params'], data['runner'], data['spec']


def gateway(tool_id, user, memory_scale=1.0):
    global JCAAS

    env = None
    params = None
    runner = None
    spec = None

    # And run it.
    if user:
        user_roles = [role.name for role in user.all_roles() if not role.deleted]
        email = user.email
    else:
        user_roles = []
        email = ''

    if APP_CONFIG.get('use_http', True):
        try:
            env, params, runner, spec = _gateway_http(tool_id, user_roles, email, memory_scale=memory_scale)
        except (requests.exceptions.RequestException, AssertionError):
            # We really failed, so fall back to old algo.
            #log.warning()
            pass

    if not spec:
        if JCAAS is None:
            JCAAS = JCaaS()
        env, params, runner, spec = JCAAS.gateway(tool_id, user_roles, email, memory_scale=memory_scale)

    name = name_it(spec)
    return JobDestination(
        id=name,
        runner=runner,
        params=params,
        env=env,
        resubmit=[{
            'condition': 'any_failure',
            'destination': 'resubmit_gateway',
        }]
    )


def resubmit_gateway(tool_id, user):
    """Gateway to handle jobs which have been resubmitted once.

    We don't want to try re-running them forever so the ONLY DIFFERENCE in
    these methods is that this one doesn't include a 'resubmission'
    specification in the returned JobDestination
    """

    job_destination = gateway(tool_id, user, memory_scale=1.5)
    job_destination['resubmit'] = []
    job_destination['id'] = job_destination['id'] + '_resubmit'
    return job_destination


if __name__ == '__main__':
    pass
