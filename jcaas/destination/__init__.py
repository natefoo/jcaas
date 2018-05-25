import os
from abc import (
    ABCMeta,
    abstractmethod
)

import six


six.add_metaclass(ABCMeta)
class Destination(object):

    # TODO: when we drop Python 2.7 support, add the classmethod decorators

    #@classmethod
    @abstractmethod
    def custom_spec(cls):
        raise NotImplementedError()
        #pass

    #@classmethod
    #@abstractmethod
    # FIXME: is classmethod in .eu dests
    def is_available(cls):
        raise NotImplementedError()
        #return False

    #@classmethod
    @abstractmethod
    def reroute_to_dedicated(cls, tool_spec, user_roles):
        raise NotImplementedError()
        #return {}

    def __init__(self, name, conf):
        self.name = name
        self.conf = conf or {}
        self.disable_path = conf.get('disable_path', None)
        self.max_cores = conf.get('max_cores', self.__class__.MAX_CORES)
        self.max_mem = conf.get('max_cores', self.__class__.MAX_MEM)
        self.priority = conf.get('priority', 0)
        self.alternatives = conf.get('alternatives', [])
        self.native = conf.get('native', {})

    @property
    def is_disabled(self):
        try:
            os.stat(self.disable_path)
            #log.debug("cluster '%s' disabled by '%s', remove to enable", cluster, disable)
            return True
        except TypeError:
            # self.disable_path is None
            return False
        except OSError:
            return False
