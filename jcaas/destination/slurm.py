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
    MAX_CORES = 8
    MAX_MEM = 32 - 2
    TRAINING_MACHINES = {}

    @classmethod
    def custom_spec(cls, tool_spec, params, kwargs, tool_memory, tool_cores):
        raw_allocation_details = {}
        if 'cores' in tool_spec:
            kwargs['PARALLELISATION'] = tool_cores
            raw_allocation_details['cpu'] = tool_cores
        else:
            pass
            # This *seems* unnecessary, we should just request a single CPU
            # core. Not sure why del.
            # del params['request_cpus']

        if 'mem' in tool_spec:
            raw_allocation_details['mem'] = tool_memory

        if 'requirements' in tool_spec:
            params['requirements'] = tool_spec['requirements']

        if 'rank' in tool_spec:
            params['rank'] = tool_spec['rank']
        return kwargs, raw_allocation_details, params

    def is_available(self):
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

    @classmethod
    def get_training_machines(cls, group='training'):
        # IF more than 60 seconds out of date, refresh.
        # Define the group if it doesn't exist.
        if group not in cls.TRAINING_MACHINES:
            cls.TRAINING_MACHINES[group] = {
                'updated': 0,
                'machines': [],
            }

        if time.time() - cls.TRAINING_MACHINES[group]['updated'] > \
                cls.STALE_CONDOR_HOST_INTERVAL:
            # Fetch a list of machines
            try:
                machine_list = subprocess.check_output(['condor_status',
                                                        '-long', '-attributes',
                                                        'Machine']).decode('utf-8')
            except subprocess.CalledProcessError:
                machine_list = ''
            except FileNotFoundError:
                machine_list = ''

            # Strip them
            cls.TRAINING_MACHINES[group]['machines'] = [
                x[len("Machine = '"):-1]
                for x in machine_list.strip().split('\n\n')
                if '-' + group + '-' in x
            ]
            # And record that this has been updated recently.
            cls.TRAINING_MACHINES[group]['updated'] = time.time()
        return cls.TRAINING_MACHINES[group]['machines']

    @classmethod
    def avoid_machines(cls, permissible=None):
        """
        Obtain a list of the special training machines in the form that can be
        used in a rank/requirement expression.

        :param permissible: A list of training groups that are permissible to
                            the user and shouldn't be included in the expression
        :type permissible: list(str) or None

        """
        if permissible is None:
            permissible = []
        machines = set(cls.get_training_machines())
        # List of those to remove.
        to_remove = set()
        # Loop across permissible machines in order to remove them from the
        # machine dict.
        for allowed in permissible:
            for m in machines:
                if allowed in m:
                    to_remove = to_remove.union(set([m]))
        # Now we update machine list with removals.
        machines = machines.difference(to_remove)
        # If we want to NOT use the machines, construct a list with `!=`
        data = ['(machine != "%s")' % m for m in sorted(machines)]
        if len(data):
            return '( ' + ' && '.join(data) + ' )'
        return ''

    @classmethod
    def prefer_machines(cls, training_identifiers, machine_group='training'):
        """
        Obtain a list of the specially tagged machines in the form that can be
        used in a rank/requirement expression.

        :param training_identifiers: A list of training groups that are
                                     permissible to the user and shouldn't be
                                     included in the expression
        :type training_identifiers: list(str) or None
        """
        if training_identifiers is None:
            training_identifiers = []

        machines = set(cls.get_training_machines(group=machine_group))
        allowed = set()
        for identifier in training_identifiers:
            for m in machines:
                if identifier in m:
                    allowed = allowed.union(set([m]))

        # If we want to use the machines, construct a list with `==`
        data = ['(machine == "%s")' % m for m in sorted(allowed)]
        if len(data):
            return '( ' + ' || '.join(data) + ' )'
        return ''

    @classmethod
    def reroute_to_dedicated(cls, tool_spec, user_roles):
        """
        Re-route users to correct destinations. Some users will be part of a
        role with dedicated training resources.
        """
        # Collect their possible training roles identifiers.
        training_roles = [role[len('training-'):] for role in user_roles
                          if role.startswith('training-')]

        # No changes to specification.
        if len(training_roles) == 0:
            # However if it is running on condor, make sure that it doesn't run
            # on the training machines.
            if 'runner' in tool_spec and tool_spec['runner'] == 'condor':
                # Require that the jobs do not run on these dedicated training
                # machines.
                return {'requirement': cls.avoid_machines()}
            # If it isn't running on condor, no changes.
            return {}

        # Otherwise, the user does have one or more training roles.
        # So we must construct a requirement / ranking expression.
        return {
            # We require that it does not run on machines that the user is not
            # in the role for.
            'requirements': cls.avoid_machines(permissible=training_roles),
            # We then rank based on what they *do* have the roles for
            'rank': cls.prefer_machines(training_roles),
            'runner': 'condor',
        }

    @classmethod
    def convert(cls, tool_spec):
        tool_spec['runner'] = 'condor'
        return tool_spec
