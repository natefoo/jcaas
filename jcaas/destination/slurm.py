import errno
import logging
import subprocess
import sys
import time

from . import Destination

log = logging.getLogger(__name__)


if sys.version_info < (3,):
    class FileNotFoundError(OSError):
        pass


#def pulsar_is_available(cluster):
#    if not is_available(cluster):
#        return False
#
#    # TODO: check pulsar here
#
#    return True


class Slurm(Destination):

    @classmethod
    def custom_spec(cls, tool_spec, params, kwargs, tool_memory, tool_cores):
        raw_allocation_details = {}

        if 'cores' in tool_spec:
            kwargs['PARALLELISATION'] = tool_cores
            mem = int(1024 * tool_memory)
            kwargs['MEMORY'] = mem
            kwargs['MEMORY_PER_CORE'] = mem / tool_cores
            raw_allocation_details['cpu'] = tool_cores
            raw_allocation_details['mem'] = tool_memory

        if 'mem' in tool_spec:
            kwargs['MEMORY'] = tool_memory = tool_memory
            raw_allocation_details['cpu'] = tool_cores

            # memory is defined per-core, and the input number is in gigabytes.
            real_memory = int(1024 * tool_memory / tool_spec['cores'])
            # Supply to kwargs with M for megabyte.
            kwargs['MEMORY'] = '%sM' % real_memory
            raw_allocation_details['mem'] = tool_memory

        if 'clusters' in params or 'clusters' in tool_spec:
            kwargs['CLUSTERS'] = 
            kwargs['CLUSTERS'] = params['clusters']

        if 'clusters' in tool_spec:
            kwargs['CLUSTERS'] = 

        return kwargs, raw_allocation_details, params

    def is_available(self):
        # TODO: cache state for a time
        cluster = self.native.get('cluster', None)
        args = []
        if cluster:
            args = ['-M', cluster]
        if self.is_disabled:
            log.debug("destination '%s' disabled by '%s', remove to enable", self.name, 'foo')
            return False
        try:
            # returns 0 even when all slurmctlds are down
            ping = subprocess.check_output(['asdfscontrol'] + args + ['ping'])
            assert 'UP' in ping.splitlines[0].split()[-1]
        except AssertionError:
            log.warning("'scontrol ping' did not report 'UP' state for cluster '%s': %s", self.name, ping)
            return False
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                # raise FileNotFoundError(exc)
                log.warning("'scontrol ping' failed for cluster '%s': %s", self.name, exc)
            else:
                raise
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            # FileNotFoundError = no slurm binary
            log.warning("'scontrol ping' failed for cluster '%s': %s", self.name, exc)
            return False
        return True
